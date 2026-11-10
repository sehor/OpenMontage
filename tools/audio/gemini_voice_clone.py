"""Create a persistent Gemini voice from reference and genuine consent recordings."""
import base64
from pathlib import Path

import jsonschema

from lib.gemini_speech import (GeminiSpeechClient, api_key, reference_audio,
                               receipt_metadata, write_receipt)
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier, RetryPolicy


class GeminiVoiceClone(BaseTool):
    name = "gemini_voice_clone"
    provider = "gemini"
    capability = "voice_clone"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.API
    agent_skills = ["gemini-tts"]
    dependencies = ["requests"]
    install_instructions = "Set GEMINI_API_KEY (or GOOGLE_API_KEY); replication requires Paid Tier 1+."
    retry_policy = RetryPolicy(max_retries=0)
    side_effects = ["uploads reference and consent recordings", "creates a stored Google voice", "writes voice receipt"]
    input_schema = {"type": "object", "additionalProperties": False,
                    "required": ["reference_path", "consent_path", "display_name", "output_path"],
                    "properties": {key: {"type": "string", "minLength": 1} for key in
                                   ("reference_path", "consent_path", "display_name", "output_path")}}

    def get_status(self):
        return ToolStatus.AVAILABLE if api_key() else ToolStatus.UNAVAILABLE

    def dry_run(self, inputs):
        return {**super().dry_run(inputs), "estimated_cost_usd": None,
                "cost_status": "unknown", "actual_cost_usd": None,
                "note": "Creation uploads two recordings and creates a stored voice; no automatic retries."}

    def execute(self, inputs):
        try:
            jsonschema.validate(inputs, self.input_schema)
            path = Path(inputs["output_path"])
            if path.suffix.lower() != ".json":
                raise ValueError("Voice output_path must end in .json.")
            client = GeminiSpeechClient()
            source = reference_audio(inputs["reference_path"], reference=True, max_bytes=client.settings.max_reference_bytes)
            consent = reference_audio(inputs["consent_path"], reference=False, max_bytes=client.settings.max_reference_bytes)
            if path.exists():
                raise ValueError("Voice receipt already exists; use its voice_id or choose another output_path.")
            path.parent.mkdir(parents=True, exist_ok=True)
            body = {"store": True, "voice": {"model": client.settings.model, "type": "replicated",
                    "display_name": inputs["display_name"], "replicated": {
                        "source_audio": {"mime_type": "audio/wav", "data": base64.b64encode(source).decode("ascii")},
                        "consent_audio": {"mime_type": "audio/wav", "data": base64.b64encode(consent).decode("ascii")}}}}
            payload, request_id = client.request("POST", "voices", body)
            voice_id = payload.get("id")
            if not isinstance(voice_id, str) or not voice_id.startswith("voice_"):
                raise ValueError("Voice response missing persistent voice ID; do not blindly repeat creation.")
            data = {"provider": self.provider, "model": client.settings.model,
                    "voice_id": voice_id, "display_name": inputs["display_name"], "store": True,
                    "expire_time": payload.get("expire_time"), **receipt_metadata(payload, request_id)}
            # A clone id is not an interaction id.
            data.pop("interaction_id", None)
            try:
                write_receipt(path, data)
            except OSError:
                return ToolResult(success=False, data=data, model=client.settings.model,
                                  error="Voice created, but receipt could not be saved. Preserve data.voice_id; do not recreate.")
            return ToolResult(success=True, data=data, artifacts=[str(path)], model=client.settings.model)
        except (ValueError, OSError, KeyError, TypeError, jsonschema.ValidationError) as exc:
            error = "Invalid Gemini clone input." if isinstance(exc, jsonschema.ValidationError) else str(exc)
            key = api_key()
            return ToolResult(success=False, error=error.replace(key, "[REDACTED]") if key else error)
