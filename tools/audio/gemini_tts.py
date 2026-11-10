"""Gemini Interactions single-speaker WAV synthesis."""
from pathlib import Path
import time

import jsonschema

from lib.gemini_speech import (GeminiSpeechClient, api_key, output_audio,
                               receipt_metadata, write_receipt)
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier, RetryPolicy


class GeminiTTS(BaseTool):
    name = "gemini_tts"
    provider = "gemini"
    capability = "tts"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.API
    agent_skills = ["gemini-tts"]
    dependencies = ["requests"]
    install_instructions = "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env."
    retry_policy = RetryPolicy(max_retries=0)
    capabilities = ["text_to_speech", "voice_selection", "instruction_control"]
    supports = {"voice_cloning": True, "multilingual": True, "offline": False,
                "native_audio": True, "timestamps": False, "streaming": False, "ssml": False}
    best_for = ["expressive single-speaker narration", "Gemini replicated voices"]
    not_good_for = ["word timestamps", "multi-speaker generation", "offline production"]
    side_effects = ["calls paid Gemini API", "writes WAV audio and receipt"]
    input_schema = {"type": "object", "required": ["text"], "properties": {
        "text": {"type": "string", "minLength": 1},
        "voice_id": {"type": "string", "minLength": 1},
        "model_id": {"type": "string", "minLength": 1},
        "speech_style": {"type": "string"}, "output_path": {"type": "string", "minLength": 1}}}

    def get_status(self):
        return ToolStatus.AVAILABLE if api_key() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs):
        # Conservative serving-limit reservation, using standard rates after
        # the 2026 introductory discount: 8192 text + 16384 audio tokens.
        # This is a budget bound, never an actual billing claim.
        return (8192 * 1.0 + 16384 * 18.0) / 1_000_000

    def dry_run(self, inputs):
        return {**super().dry_run(inputs), "estimate_kind": "conservative_request_limit",
                "cost_status": "estimate_only", "actual_cost_usd": None}

    def execute(self, inputs):
        start = time.monotonic()
        try:
            jsonschema.validate(inputs, self.input_schema)
            if not inputs["text"].strip():
                raise ValueError("Text must not be blank.")
            path = Path(inputs.get("output_path", "gemini_tts_output.wav"))
            if path.suffix.lower() != ".wav":
                raise ValueError("Gemini single-speaker output_path must end in .wav.")
            client = GeminiSpeechClient()
            model = inputs.get("model_id", client.settings.model)
            if model != client.settings.model:
                raise ValueError("model_id must match audio.gemini.model; configure an explicit model change first.")
            voice = inputs.get("voice_id", client.settings.default_voice)
            part = {"type": "text", "text": inputs["text"]}
            if inputs.get("speech_style"):
                part["annotations"] = [{"type": "speech_metadata", "style": inputs["speech_style"]}]
            body = {"model": model, "input": [{"type": "user_input", "content": [part]}],
                    "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
                    "generation_config": {"speech_config": [{"voice": voice}]}}
            payload, request_id = client.request("POST", "interactions", body)
            audio, info = output_audio(payload)
            receipt = path.with_suffix(".receipt.json")
            data = {"provider": self.provider, "model": model, "voice_id": voice,
                    "output": str(path), "format": "WAV", **info,
                    **receipt_metadata(payload, request_id), "timestamps": None}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio)
            write_receipt(receipt, data)
            return ToolResult(success=True, data=data, artifacts=[str(path), str(receipt)],
                              model=model, duration_seconds=time.monotonic() - start)
        except (ValueError, OSError, KeyError, TypeError, jsonschema.ValidationError) as exc:
            # ValidationError messages can contain the entire invalid input.
            error = "Invalid Gemini TTS input." if isinstance(exc, jsonschema.ValidationError) else str(exc)
            key = api_key()
            return ToolResult(success=False, error=error.replace(key, "[REDACTED]") if key else error)
