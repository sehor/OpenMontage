"""Create a Qwen-Audio voice and save the provider's preview audio."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Any

import requests

from tools.audio.dashscope_tts import DashScopeTTS
from lib.dashscope_config import speech_model
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier


class DashScopeVoiceDesign(BaseTool):
    name = "dashscope_voice_design"
    tier = ToolTier.VOICE
    capability = "voice_design"
    provider = "dashscope"
    runtime = ToolRuntime.API
    dependencies = ["env:DASHSCOPE_API_KEY"]
    agent_skills = ["qwen-tts"]
    install_instructions = DashScopeTTS.install_instructions
    input_schema = {
        "type": "object", "required": ["voice_prompt", "preview_text", "output_path"],
        "properties": {
            "voice_prompt": {"type": "string", "minLength": 1, "maxLength": 500},
            "preview_text": {"type": "string", "minLength": 15, "maxLength": 200},
            "output_path": {"type": "string", "minLength": 1},
            "prefix": {"type": "string", "pattern": "^[A-Za-z0-9]{1,10}$"},
        },
    }
    side_effects = ["creates a reusable voice in the Aliyun account", "writes preview WAV"]

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
            if not inputs["voice_prompt"].strip() or not inputs["preview_text"].strip():
                raise ValueError("Voice description and preview text cannot be blank")
            prefix = inputs.get("prefix", "robotai")
            if not re.fullmatch(r"[A-Za-z0-9]{1,10}", prefix):
                raise ValueError("Invalid voice prefix")
            output = Path(inputs["output_path"])
            if output.suffix.lower() != ".wav":
                raise ValueError("Preview output must be a WAV file")
            endpoint = DashScopeTTS._endpoint().replace("/SpeechSynthesizer", "/customization")
            body = {"model": "voice-enrollment", "input": {
                "action": "create_voice", "target_model": model,
                "voice_prompt": inputs["voice_prompt"], "preview_text": inputs["preview_text"],
                "prefix": prefix, "language_hints": ["zh"]},
                "parameters": {"sample_rate": 24000, "response_format": "wav"}}
            with requests.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                               json=body, timeout=(10, 120), allow_redirects=False) as response:
                payload = response.json()
                if not 200 <= response.status_code < 300 or payload.get("code"):
                    raise ValueError(f"Aliyun HTTP {response.status_code}: {payload.get('code', 'error')} {payload.get('message', '')}")
            voice_id = payload["output"]["voice_id"]
            audio = base64.b64decode(payload["output"]["preview_audio"]["data"], validate=True)
            if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                raise ValueError(f"Invalid preview WAV; created voice ID: {voice_id}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(audio)
            return ToolResult(success=True, artifacts=[str(output)], model=model,
                              data={"voice_id": voice_id, "output": str(output),
                                    "request_id": payload.get("request_id"), "usage": payload.get("usage", {})})
        except jsonschema.ValidationError as exc:
            return ToolResult(success=False, error=f"Invalid input: {exc.message}".replace(key, "[REDACTED]"))
        except requests.RequestException as exc:
            return ToolResult(success=False, error=f"Voice design request failed ({type(exc).__name__}); no automatic retry")
        except (ValueError, KeyError, TypeError, OSError, binascii.Error) as exc:
            return ToolResult(success=False, error=f"Voice design failed: {exc}".replace(key, "[REDACTED]"))
