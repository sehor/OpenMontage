"""Shared Gemini speech REST transport and validated audio/receipt handling."""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
from pathlib import Path
import wave

import requests

from lib.config_model import OpenMontageConfig
from lib.env_loader import load_env

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def speech_settings():
    config = os.environ.get("OPENMONTAGE_CONFIG")
    return OpenMontageConfig.load(Path(config) if config else None).audio.gemini


def api_key() -> str:
    load_env()
    return (os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip())


class GeminiSpeechError(ValueError):
    """Safe, actionable error; never includes provider bodies or request audio."""


class GeminiSpeechClient:
    def __init__(self):
        self.key = api_key()
        self.settings = speech_settings()

    def request(self, method: str, resource: str, body=None) -> tuple[dict, str | None]:
        if not self.key:
            raise GeminiSpeechError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env.")
        try:
            response = requests.request(
                method, f"{BASE_URL}/{resource}",
                headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
                json=body, timeout=(10, self.settings.timeout_seconds), allow_redirects=False,
            )
        except requests.RequestException:
            raise GeminiSpeechError("Gemini network/timeout failure; no automatic retry. Check connectivity before retrying.") from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not 200 <= response.status_code < 300 or (isinstance(payload, dict) and payload.get("error")):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            # Inspect messages only to classify; never copy untrusted service text to logs.
            message = str(error).lower()
            status = response.status_code
            if "consent" in message or "voice mismatch" in message:
                hint = "Consent verification failed. Use authentic recordings of the same adult speaker reciting the exact consent phrase."
            elif status in (401, 403):
                hint = "Check the API key, project permissions and supported region; voice replication requires Paid Tier 1+."
            elif status == 429:
                hint = "Quota/rate limit reached. Check project quota before retrying."
            elif status in (404, 410):
                hint = "Model or voice unavailable/expired. Check model access and the voice ID."
            elif status == 400:
                hint = "Invalid speech request. Check text limits, voice ID and recording requirements."
            else:
                hint = "Provider request failed; check service availability before retrying."
            raise GeminiSpeechError(f"Gemini HTTP {status}: {hint} No automatic retry.")
        if not isinstance(payload, dict):
            raise GeminiSpeechError("Gemini returned an invalid JSON object.")
        request_id = response.headers.get("x-request-id") or response.headers.get("x-goog-request-id")
        return payload, request_id


def wav_info(audio: bytes) -> dict:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getnframes() == 0:
                raise ValueError("Empty or compressed WAV")
            frames = wav.getnframes()
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            if len(wav.readframes(frames)) != expected:
                raise ValueError("Truncated WAV")
            return {"sample_rate": wav.getframerate(), "channels": wav.getnchannels(),
                    "sample_width": wav.getsampwidth(), "audio_duration_seconds": frames / wav.getframerate()}
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise GeminiSpeechError("Expected a complete, non-empty PCM WAV file.") from None


def reference_audio(path: str, *, reference: bool, max_bytes: int) -> bytes:
    source = Path(path)
    if source.suffix.lower() != ".wav" or not source.is_file():
        raise GeminiSpeechError("Provide an existing WAV recording.")
    if source.stat().st_size > max_bytes:
        raise GeminiSpeechError(f"Recording exceeds the configured {max_bytes}-byte limit.")
    audio = source.read_bytes()
    info = wav_info(audio)
    if (info["sample_rate"], info["channels"], info["sample_width"]) != (24000, 1, 2):
        raise GeminiSpeechError("Convert recording to 24 kHz mono 16-bit PCM WAV; no automatic conversion is performed.")
    if reference and not 10 <= info["audio_duration_seconds"] <= 30:
        raise GeminiSpeechError("Reference recording must contain 10–30 seconds of natural speech.")
    return audio


def output_audio(payload: dict) -> tuple[bytes, dict]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise GeminiSpeechError("Gemini returned invalid interaction steps.")
    blocks = [part for step in steps if isinstance(step, dict) and step.get("type") == "model_output"
              and isinstance(step.get("content"), list)
              for part in step["content"] if isinstance(part, dict) and part.get("type") == "audio"]
    if payload.get("status") not in (None, "completed"):
        raise GeminiSpeechError("Gemini interaction did not complete; no audio saved.")
    if not blocks:
        raise GeminiSpeechError("Gemini returned no audio; request may be blocked or incomplete.")
    block = blocks[-1]  # Interactions output_audio convenience property has the same semantics.
    if str(block.get("mime_type", "")).split(";")[0] not in ("audio/wav", "audio/x-wav"):
        raise GeminiSpeechError("Expected WAV response; refusing to label another encoding as WAV.")
    try:
        audio = base64.b64decode(block["data"], validate=True)
    except (KeyError, TypeError, binascii.Error):
        raise GeminiSpeechError("Gemini returned invalid base64 audio.") from None
    info = wav_info(audio)
    if (info["sample_rate"], info["channels"], info["sample_width"]) != (24000, 1, 2):
        raise GeminiSpeechError("Unexpected Gemini audio format; expected 24 kHz mono PCM16 WAV.")
    return audio, info


def write_receipt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def receipt_metadata(payload: dict, request_id: str | None) -> dict:
    # Allowlist billing counters, excluding input text, audio and provider debug objects.
    usage = payload.get("usage", {})
    usage = {key: value for key, value in usage.items()
             if isinstance(value, (int, float)) and not isinstance(value, bool)} if isinstance(usage, dict) else {}
    return {"request_id": request_id, "interaction_id": payload.get("id"), "usage": usage,
            "cost_status": "unknown", "actual_cost_usd": None,
            "cost_note": "ToolResult.cost_usd=0 is a framework default, not a billing quote."}
