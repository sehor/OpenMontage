"""Configured Aliyun speech protocols, including durable timestamped SSE."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from lib.dashscope_config import speech_model

import jsonschema
import requests

from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)


class DashScopeTTS(BaseTool):
    name = "dashscope_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "dashscope"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies = ["env:DASHSCOPE_API_KEY"]
    install_instructions = (
        "Set DASHSCOPE_API_KEY in the environment or the ignored project .env. "
        "For a workspace endpoint, also set SFM_WORKSPACE_ID and "
        "DASHSCOPE_REGION (cn-beijing or ap-southeast-1). "
        "DASHSCOPE_TTS_ENDPOINT can override the official endpoint. "
        "Use a key from the same region. See "
        "https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api"
    )
    fallback_tools = []
    agent_skills = ["qwen-tts"]
    capabilities = ["text_to_speech", "voice_selection", "multilingual", "instruction_control", "ssml", "reference_audio", "multi_speaker", "streaming", "word_timestamps"]
    supports = {
        "voice_cloning": False, "multilingual": True, "offline": False,
        "native_audio": True, "timestamps": True, "long_text_async": False,
        "streaming": True,
        "non_streaming": True, "instruction_control": True, "ssml": True,
        "multi_voice_ssml": False,
        "reference_audio": True, "multi_speaker": True,
    }
    best_for = ["Reference-conditioned multi-speaker dialogue", "Configured speech synthesis"]
    not_good_for = ["offline generation", "real-time conversations"]
    input_schema = {
        "type": "object", "anyOf": [{"required": ["text"]}, {"required": ["text_prompt"]}],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "model": {"type": "string", "minLength": 1},
            "model_id": {"type": "string", "minLength": 1},
            "voice_id": {"type": "string", "minLength": 1},
            "text_prompt": {"type": "string", "minLength": 1},
            "reference_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "channels": {"type": "integer", "enum": [1, 2]},
            "format": {"type": "string", "enum": ["wav", "mp3", "pcm", "opus"], "default": "wav"},
            "sample_rate": {"type": "integer", "enum": [8000, 12000, 16000, 22050, 24000, 44100, 48000]},
            "instruction": {"type": "string"},
            "streaming": {"type": "boolean", "default": False},
            "word_timestamp_enabled": {"type": "boolean", "default": False},
            "enable_ssml": {"type": "boolean", "default": False},
            "ssml_voice_models": {"type": "object", "additionalProperties": {"type": "string"}},
            "rate": {"type": "number", "minimum": 0.5, "maximum": 2.0},
            "pitch": {"type": "number", "minimum": 0.5, "maximum": 2.0},
            "volume": {"type": "integer", "minimum": 0, "maximum": 100},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300, "default": 120},
            "output_path": {"type": "string", "minLength": 1},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=100, network_required=True)
    # A timeout may have generated billable audio; let the caller decide to retry.
    retry_policy = RetryPolicy(max_retries=0)
    idempotency_key_fields = ["text", "text_prompt", "reference_paths", "channels", "model", "model_id", "voice_id", "format", "sample_rate", "instruction", "rate", "pitch", "volume", "enable_ssml", "ssml_voice_models", "streaming", "word_timestamp_enabled"]
    side_effects = ["calls Aliyun paid speech API", "writes audio file to output_path"]
    user_visible_verification = ["Listen to the generated sample for pronunciation and delivery"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.environ.get("DASHSCOPE_API_KEY", "").strip() else ToolStatus.UNAVAILABLE

    @staticmethod
    def _endpoint() -> str:
        endpoint = os.environ.get("DASHSCOPE_TTS_ENDPOINT", "").strip()
        if not endpoint:
            workspace = os.environ.get("SFM_WORKSPACE_ID", "").strip()
            region = os.environ.get("DASHSCOPE_REGION", "cn-beijing").strip()
            if region not in {"cn-beijing", "ap-southeast-1"}:
                raise ValueError("DASHSCOPE_REGION must be cn-beijing or ap-southeast-1")
            if workspace:
                if not re.fullmatch(r"[A-Za-z0-9-]+", workspace):
                    raise ValueError("Invalid SFM_WORKSPACE_ID")
                host = f"{workspace}.{region}.maas.aliyuncs.com"
            else:
                host = "dashscope-intl.aliyuncs.com" if region == "ap-southeast-1" else "dashscope.aliyuncs.com"
            endpoint = f"https://{host}/api/v1/services/audio/tts/SpeechSynthesizer"
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        valid_host = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or bool(
            re.fullmatch(r"[a-z0-9-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com", host)
        )
        if (parsed.scheme != "https" or not valid_host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or parsed.path != "/api/v1/services/audio/tts/SpeechSynthesizer"):
            raise ValueError("DASHSCOPE_TTS_ENDPOINT must be an official HTTPS TTS endpoint")
        return endpoint

    @staticmethod
    def _audio_url(url: str) -> str:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)
                or not (parsed.hostname or "").endswith(".aliyuncs.com")):
            raise ValueError("Provider returned an invalid audio download URL")
        # Official examples return HTTP OSS URLs. Download over TLS instead.
        return urlunsplit(parsed._replace(scheme="https"))

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            return ToolResult(success=False, error="Missing DASHSCOPE_API_KEY. " + self.install_instructions)
        start = time.monotonic()
        provider_data = {}
        try:
            jsonschema.validate(inputs, self.input_schema)
            if inputs.get("model") and inputs.get("model_id") and inputs["model"] != inputs["model_id"]:
                raise ValueError("model and model_id disagree")
            profile = speech_model(inputs.get("model", inputs.get("model_id")))
            model = profile.model
            streaming = inputs.get("streaming", False)
            if (streaming or inputs.get("word_timestamp_enabled")) and profile.protocol != "tts":
                raise ValueError("SSE word timestamps are only available for the TTS protocol; Next is unchanged")
            if inputs.get("word_timestamp_enabled") and not streaming:
                raise ValueError("word_timestamp_enabled requires streaming=true")
            endpoint = self._endpoint()
            fmt = inputs.get("format", "wav")
            sample_rate = inputs.get("sample_rate", profile.sample_rate)
            if fmt == "opus" and sample_rate not in {8000, 12000, 16000, 24000, 48000}:
                raise ValueError("Unsupported Opus sample rate")
            output = Path(inputs.get("output_path", f"dashscope_tts.{fmt}"))
            if output.suffix.lower() != f".{fmt}":
                raise ValueError("output_path extension must match format")
            ssml_info = None
            references = []
            if profile.protocol == "audio_generation":
                from lib.dashscope_references import next_speech
                speech, references = next_speech(inputs, profile)
                voice = None
            else:
                if not inputs.get("text", "").strip() or "text_prompt" in inputs or "reference_paths" in inputs:
                    raise ValueError("The selected TTS protocol requires text, not reference audio or text_prompt")
                voice = inputs.get("voice_id", profile.default_voice)
                if not voice:
                    raise ValueError("A voice_id or configured default_voice is required")
                speech = {"text": inputs["text"], "voice": voice, "format": fmt, "sample_rate": sample_rate}
            if profile.protocol == "tts" and inputs.get("enable_ssml", False):
                from lib.dashscope_ssml import native_synthesis_status, validate_ssml
                ssml_info = validate_ssml(inputs["text"], model, inputs.get("ssml_voice_models"),
                                          request_voice=inputs.get("voice_id"))
                status = native_synthesis_status(model, ssml_info["voice_ids"])
                if not status["allowed"]:
                    raise ValueError(status["reason"])
                if "voice_id" in inputs and inputs["voice_id"] not in ssml_info["voice_ids"]:
                    raise ValueError("Fallback voice_id must be one of the SSML voices")
            elif profile.protocol == "tts" and re.search(r"<\s*speak\b", inputs["text"], re.IGNORECASE):
                raise ValueError("SSML text requires enable_ssml=true")
            if ssml_info and "voice_id" not in inputs:
                voice = ssml_info["voice_ids"][0]
                speech["voice"] = voice
            if "enable_ssml" in inputs:
                speech["enable_ssml"] = inputs["enable_ssml"]
            for field in ("instruction", "rate", "pitch", "volume"):
                if field in inputs:
                    speech[field] = inputs[field]
            timeout = (10, inputs.get("timeout_seconds", 300 if profile.protocol == "audio_generation" else 120))
            if streaming:
                from lib.dashscope_stream import capture_stream
                speech["word_timestamp_enabled"] = inputs.get("word_timestamp_enabled", False)
                capture_stream(endpoint, key, model, speech, output, timeout, provider_data)
                from tools.analysis.audio_probe import probe_duration
                provider_data.update(provider=self.provider, model=model, voice_id=voice,
                                     format=fmt, sample_rate=sample_rate, output=str(output), protocol=profile.protocol,
                                     audio_duration_seconds=probe_duration(output) if fmt != "pcm" else None,
                                     cost_status="See Aliyun bill; no automatic retries")
                return ToolResult(success=True, model=model, artifacts=[str(output),
                                  provider_data["stream_receipt"], provider_data["raw_events_path"],
                                  provider_data["timestamps_path"]], data=provider_data,
                                  duration_seconds=round(time.monotonic() - start, 2))
            with requests.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                               json={"model": model, "input": speech}, timeout=timeout,
                               allow_redirects=False) as response:
                payload = response.json()
                provider_data = {"request_id": payload.get("request_id"), "usage": payload.get("usage", {})}
                if not 200 <= response.status_code < 300 or payload.get("code"):
                    code = str(payload.get("code", "HTTPError"))
                    message = str(payload.get("message", "Speech synthesis request failed"))
                    raise ValueError(f"Aliyun HTTP {response.status_code} {code}: {message}")
            audio_url = self._audio_url(payload["output"]["audio"]["url"])
            # Never forward the API Authorization header to the download host.
            with requests.get(audio_url, timeout=timeout, allow_redirects=False) as audio:
                if audio.status_code != 200:
                    raise ValueError(f"Audio download failed (HTTP {audio.status_code})")
                content = audio.content
                content_type = audio.headers.get("Content-Type", "").lower()
            if not content or "json" in content_type or "text/" in content_type:
                raise ValueError("Provider returned an empty or non-audio download")
            if fmt == "wav" and not (content.startswith(b"RIFF") and content[8:12] == b"WAVE"):
                raise ValueError("Provider did not return valid WAV data")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            from tools.analysis.audio_probe import probe_duration
            duration = probe_duration(output) if fmt != "pcm" else None
            return ToolResult(success=True, model=model, artifacts=[str(output)],
                              duration_seconds=round(time.monotonic() - start, 2), data={
                                  "provider": self.provider, "model": model, "voice_id": voice,
                                  "format": fmt, "sample_rate": sample_rate,
                                  "text_length": len(speech.get("text_prompt", speech.get("text", ""))), "output": str(output),
                                  "protocol": profile.protocol, "references": references,
                                  "audio_duration_seconds": duration,
                                  "request_id": payload.get("request_id"), "usage": payload.get("usage", {}),
                                  **({"enable_ssml": True, "voice_ids": ssml_info["voice_ids"]} if ssml_info else {}),
                                  "cost_status": "See Aliyun bill; cost_usd is not a quoted price",
                              })
        except jsonschema.ValidationError as exc:
            return ToolResult(success=False, error=f"Invalid TTS input: {exc.message}".replace(key, "[REDACTED]"))
        except requests.RequestException as exc:
            return ToolResult(success=False, data=provider_data, error=f"DashScope request failed ({type(exc).__name__}); no automatic retry")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return ToolResult(success=False, data=provider_data, error=f"DashScope TTS failed: {exc}".replace(key, "[REDACTED]"))
