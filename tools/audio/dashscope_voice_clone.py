"""Create a DashScope voice from a locally stored WAV reference."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import requests

from tools.audio.dashscope_tts import DashScopeTTS
from lib.dashscope_config import speech_model
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier


class DashScopeVoiceClone(BaseTool):
    name = "dashscope_voice_clone"
    provider = "dashscope"
    capability = "voice_clone"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.API
    agent_skills = ["qwen-tts"]
    dependencies = ["env:DASHSCOPE_API_KEY"]
    install_instructions = DashScopeTTS.install_instructions
    side_effects = ["creates a voice in the Aliyun account", "uploads reference audio"]
    input_schema = {"type": "object", "required": ["reference_path"], "properties": {
        "reference_path": {"type": "string", "minLength": 1},
        "language": {"type": "string", "enum": ["zh", "en"]},
        "prefix": {"type": "string", "pattern": "^[A-Za-z0-9]{1,10}$"}}}

    def get_status(self) -> ToolStatus:
        return DashScopeTTS().get_status()

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import jsonschema
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            return ToolResult(success=False, error="Missing DASHSCOPE_API_KEY")
        try:
            model = speech_model(purpose="enrollment").model
            jsonschema.validate(inputs, self.input_schema)
            path = Path(inputs["reference_path"])
            if path.suffix.lower() != ".wav" or path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Reference must be a WAV file no larger than 10 MiB")
            audio = path.read_bytes()
            if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                raise ValueError("Invalid WAV reference")
            endpoint = DashScopeTTS._endpoint().replace("/SpeechSynthesizer", "/customization")
            body = {"model": "voice-enrollment", "input": {"action": "create_voice",
                "target_model": model, "prefix": inputs.get("prefix", "robotref"),
                "language_hints": [inputs.get("language", "zh")],
                "audio": "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii")}}
            with requests.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=body,
                               timeout=(10, 120), allow_redirects=False) as response:
                payload = response.json()
                if not 200 <= response.status_code < 300 or payload.get("code"):
                    raise ValueError(f"Aliyun HTTP {response.status_code}: {payload.get('code', 'error')} {payload.get('message', '')}")
            return ToolResult(success=True, model=model, data={
                "voice_id": payload["output"]["voice_id"], "request_id": payload.get("request_id"),
                "usage": payload.get("usage", {})})
        except jsonschema.ValidationError as exc:
            return ToolResult(success=False, error=f"Invalid input: {exc.message}".replace(key, "[REDACTED]"))
        except requests.RequestException as exc:
            return ToolResult(success=False, error=f"Voice clone request failed ({type(exc).__name__}); no automatic retry")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return ToolResult(success=False, error=f"Voice clone failed: {exc}".replace(key, "[REDACTED]"))
