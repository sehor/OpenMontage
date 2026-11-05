"""MiniMax Speech-02 text-to-speech provider tool."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class MiniMaxTTS(BaseTool):
    name = "minimax_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "minimax"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set MINIMAX_API_KEY to a MiniMax API Key.\n"
        "Optional: set MINIMAX_GROUP_ID if you are using a legacy key format."
    )
    fallback = "google_tts"
    fallback_tools = ["google_tts", "elevenlabs_tts", "openai_tts", "doubao_tts", "piper_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "multilingual",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "timestamps": False,
        "long_text_async": False,
    }
    best_for = [
        "highly expressive and emotional Chinese/English narration",
        "MiniMax Speech-02 highly realistic voices",
    ]
    not_good_for = [
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice_id": {
                "type": "string",
                "default": "male-qn-qingse",
                "description": "MiniMax speaker/voice_id (e.g. male-qn-qingse, female-shaonv).",
            },
            "model": {
                "type": "string",
                "default": "speech-01-turbo",
                "description": "MiniMax TTS model version.",
            },
            "format": {
                "type": "string",
                "default": "mp3",
                "enum": ["mp3", "wav", "flac"],
            },
            "speed": {
                "type": "number",
                "default": 1.0,
                "description": "Voice playback speed, e.g. 1.0 for normal, 1.2 for faster.",
            },
            "pitch": {
                "type": "number",
                "default": 0,
                "description": "Voice pitch adjustment.",
            },
            "vol": {
                "type": "number",
                "default": 1.0,
                "description": "Voice volume multiplier.",
            },
            "output_path": {"type": "string"},
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "output": {"type": "string"},
            "audio_duration_seconds": {"type": ["number", "null"]},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=2.0,
        retryable_errors=["timeout", "rate_limit"],
    )
    idempotency_key_fields = ["text", "voice_id", "model", "speed", "pitch"]
    side_effects = [
        "writes audio file to output_path",
        "calls MiniMax TTS API",
    ]
    user_visible_verification = [
        "Listen to generated audio for expressiveness and quality",
    ]

    SUBMIT_URL = "https://api.minimax.chat/v1/t2a_v2"

    def get_status(self) -> ToolStatus:
        if os.environ.get("MINIMAX_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # MiniMax charges per character for Speech-02. Approximate cost.
        return round(len(inputs.get("text", "")) * 0.00001, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("MINIMAX_API_KEY")
        group_id = os.environ.get("MINIMAX_GROUP_ID")
        if not api_key:
            return ToolResult(success=False, error="No MiniMax API key. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs, api_key=api_key, group_id=group_id)
        except Exception as exc:
            return ToolResult(success=False, error=f"MiniMax TTS failed: {self._safe_error(exc)}")

        result.duration_seconds = round(time.time() - start, 2)
        if not result.cost_usd:
            result.cost_usd = self.estimate_cost(inputs)
        return result

    def _generate(self, inputs: dict[str, Any], *, api_key: str, group_id: str | None) -> ToolResult:
        import requests

        text = inputs["text"]
        fmt = inputs.get("format", "mp3")
        voice_id = inputs.get("voice_id", "male-qn-qingse")
        model = inputs.get("model", "speech-01-turbo")
        speed = float(inputs.get("speed", 1.0))
        pitch = int(inputs.get("pitch", 0))
        vol = float(inputs.get("vol", 1.0))

        output_path = Path(inputs.get("output_path", f"minimax_tts.{fmt}"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        url = self.SUBMIT_URL
        if group_id:
            url = f"{url}?GroupId={group_id}"

        payload = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": vol,
                "pitch": pitch,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": fmt,
                "channel": 1,
            }
        }

        response = requests.post(url, headers=headers, json=payload, timeout=(10, 120))

        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"Non-JSON response from MiniMax API: HTTP {response.status_code}")

        # Check for errors in base_resp
        base_resp = data.get("base_resp", {})
        status_code = base_resp.get("status_code", 0)
        if status_code != 0:
            msg = base_resp.get("status_msg", "unknown error")
            raise RuntimeError(f"MiniMax API Error (Code {status_code}): {msg}")

        # Extract hex-encoded audio
        audio_hex = data.get("data", {}).get("audio", "")
        if not audio_hex:
            raise RuntimeError("MiniMax API succeeded but returned empty audio data")

        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError:
            raise RuntimeError("MiniMax API returned invalid hex-encoded audio data")

        output_path.write_bytes(audio_bytes)

        # Attempt to probe duration using existing audio_probe tool
        audio_duration = self._audio_duration(output_path)

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model,
                "voice_id": voice_id,
                "format": fmt,
                "text_length": len(text),
                "audio_duration_seconds": round(audio_duration, 2) if audio_duration else None,
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
            model=model,
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        err_str = str(exc)
        api_key = os.environ.get("MINIMAX_API_KEY", "")
        if api_key:
            err_str = err_str.replace(api_key, "[redacted]")
        return err_str

    @staticmethod
    def _audio_duration(path: Path) -> float | None:
        try:
            from tools.analysis.audio_probe import probe_duration
            return probe_duration(path)
        except Exception:
            return None
