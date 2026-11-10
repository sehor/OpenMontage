import base64
import io
import json
import wave
from unittest.mock import Mock

import pytest
import requests

from lib import gemini_speech as speech
from lib.config_model import OpenMontageConfig
from tools.audio.gemini_tts import GeminiTTS
from tools.audio.gemini_voice_clone import GeminiVoiceClone
from tools.audio.tts_selector import TTSSelector
from tools.base_tool import ToolResult, ToolStatus


def wav_bytes(seconds=1, rate=24000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        out.writeframes(b"\0" * (int(seconds * rate) * channels * width))
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(speech, "load_env", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-secret")
    monkeypatch.delenv("OPENMONTAGE_CONFIG", raising=False)


def mock_response(monkeypatch, payload, status=200):
    response = Mock(status_code=status, headers={"x-request-id": "req-123"})
    response.json.return_value = payload
    post = Mock(return_value=response)
    monkeypatch.setattr(speech.requests, "request", post)
    return post


def audio_payload(audio=None, **extra):
    return {"id": "interaction-1", "status": "completed", "usage": {"total_tokens": 15},
            "steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav",
                      "data": base64.b64encode(audio or wav_bytes()).decode()}]}], **extra}


def test_config_defaults_and_override(tmp_path, monkeypatch):
    assert OpenMontageConfig().audio.gemini.default_voice == "Kore"
    config = tmp_path / "config.yaml"
    config.write_text("audio:\n  gemini:\n    default_voice: Puck\n", encoding="utf-8")
    monkeypatch.setenv("OPENMONTAGE_CONFIG", str(config))
    assert speech.speech_settings().default_voice == "Puck"


def test_credentials_priority_and_status(monkeypatch):
    assert speech.api_key() == "test-gemini-secret"
    monkeypatch.setenv("GEMINI_API_KEY", " ")
    assert speech.api_key() == "test-google-secret"
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert GeminiTTS().get_status() == ToolStatus.UNAVAILABLE
    assert GeminiVoiceClone().get_status() == ToolStatus.UNAVAILABLE


@pytest.mark.parametrize("voice", ["Kore", "voice_custom123"])
def test_synthesis_payload_and_receipt(tmp_path, monkeypatch, voice):
    request = mock_response(monkeypatch, audio_payload())
    output = tmp_path / "out.wav"
    result = GeminiTTS().execute({"text": "你好，世界。", "speech_style": "温暖自然", "voice_id": voice,
                                  "output_path": str(output)})
    assert result.success, result.error
    body = request.call_args.kwargs["json"]
    part = body["input"][0]["content"][0]
    assert part["text"] == "你好，世界。"
    assert part["annotations"] == [{"type": "speech_metadata", "style": "温暖自然"}]
    assert body["generation_config"]["speech_config"] == [{"voice": voice}]
    assert output.read_bytes() == wav_bytes()
    assert result.data["audio_duration_seconds"] == 1
    receipt = output.with_suffix(".receipt.json").read_text(encoding="utf-8")
    assert "test-gemini-secret" not in receipt
    assert "你好" not in receipt
    assert result.data["actual_cost_usd"] is None
    assert result.data["request_id"] == "req-123"
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["headers"]["x-goog-api-key"] == "test-gemini-secret"


@pytest.mark.parametrize("inputs", [{"text": " "}, {"text": 5}, {"text": "hi", "output_path": "bad.mp3"},
                                   {"text": "hi", "model_id": "another-model"}])
def test_invalid_synthesis_does_not_call_api(monkeypatch, inputs):
    request = mock_response(monkeypatch, {})
    assert not GeminiTTS().execute(inputs).success
    request.assert_not_called()


@pytest.mark.parametrize("payload", [audio_payload(status="failed"), {"steps": []},
    audio_payload(b"not a wav"), audio_payload(wav_bytes()[:-10]), audio_payload(wav_bytes(rate=16000)),
    {"steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": "!bad"}]}]}],
    ids=["failed", "missing", "invalid-wav", "truncated", "wrong-rate", "invalid-base64"])
def test_invalid_audio_not_saved(tmp_path, monkeypatch, payload):
    mock_response(monkeypatch, payload)
    output = tmp_path / "out.wav"
    result = GeminiTTS().execute({"text": "hello", "output_path": str(output)})
    assert not result.success
    assert not output.exists()


@pytest.fixture
def clone_inputs(tmp_path):
    reference = tmp_path / "reference.wav"
    consent = tmp_path / "consent.wav"
    reference.write_bytes(wav_bytes(10))
    consent.write_bytes(wav_bytes(3))
    return {"reference_path": str(reference), "consent_path": str(consent),
            "display_name": "测试音色", "output_path": str(tmp_path / "voice.json")}


def test_clone_request_and_receipt(monkeypatch, clone_inputs):
    request = mock_response(monkeypatch, {"id": "voice_abc", "expire_time": "2027-11-10T00:00:00Z"})
    result = GeminiVoiceClone().execute(clone_inputs)
    assert result.success, result.error
    body = request.call_args.kwargs["json"]
    assert body["store"] is True
    assert body["voice"]["type"] == "replicated"
    assert base64.b64decode(body["voice"]["replicated"]["source_audio"]["data"]) == wav_bytes(10)
    from pathlib import Path
    receipt = Path(clone_inputs["output_path"]).read_text(encoding="utf-8")
    assert json.loads(receipt)["voice_id"] == "voice_abc"
    assert "source_audio" not in receipt and "consent_audio" not in receipt
    assert result.data["expire_time"] == "2027-11-10T00:00:00Z"
    assert not GeminiVoiceClone().execute(clone_inputs).success
    assert request.call_count == 1


@pytest.mark.parametrize("audio", [wav_bytes(9), wav_bytes(31), wav_bytes(10, rate=16000),
                                  wav_bytes(10, channels=2), wav_bytes(10, width=1), b"invalid"],
                         ids=["short", "long", "rate", "stereo", "8bit", "invalid"])
def test_invalid_reference_never_uploaded(monkeypatch, clone_inputs, audio):
    from pathlib import Path
    Path(clone_inputs["reference_path"]).write_bytes(audio)
    request = mock_response(monkeypatch, {})
    assert not GeminiVoiceClone().execute(clone_inputs).success
    request.assert_not_called()


@pytest.mark.parametrize("status,message,expected", [(403, "secret", "permissions"),
    (429, "secret", "Quota"), (404, "secret", "expired"), (500, "Consent flow failed secret", "Consent"),
    (400, "secret", "Invalid"), (503, "secret", "Provider")])
def test_errors_are_actionable_and_redacted(monkeypatch, status, message, expected):
    request = mock_response(monkeypatch, {"error": {"message": message + " test-gemini-secret"}}, status)
    result = GeminiTTS().execute({"text": "hi"})
    assert not result.success
    assert expected in result.error
    assert "secret" not in result.error
    assert request.call_count == 1


def test_timeout_never_leaks_exception_or_retries(monkeypatch):
    request = Mock(side_effect=requests.Timeout("test-gemini-secret audio payload"))
    monkeypatch.setattr(speech.requests, "request", request)
    result = GeminiTTS().execute({"text": "hi"})
    assert not result.success
    assert "test-gemini-secret" not in result.error
    assert request.call_count == 1


def test_registry_discovery_and_explicit_selector(monkeypatch):
    from tools.tool_registry import ToolRegistry
    registry = ToolRegistry()
    registry.discover("tools.audio")
    assert registry.get("gemini_tts").provider == "gemini"
    assert registry.get("gemini_voice_clone").capability == "voice_clone"
    selector = TTSSelector()
    gemini = GeminiTTS()
    monkeypatch.setattr(selector, "_providers", lambda: [gemini])
    monkeypatch.setattr(gemini, "execute", lambda inputs: ToolResult(success=True))
    assert selector.execute({"text": "hi", "preferred_provider": "gemini"}).success
    monkeypatch.setattr(gemini, "get_status", lambda: ToolStatus.UNAVAILABLE)
    assert not selector.execute({"text": "hi", "preferred_provider": "gemini"}).success


def test_clone_receipt_failure_preserves_id(monkeypatch, clone_inputs):
    mock_response(monkeypatch, {"id": "voice_saved"})
    monkeypatch.setattr("tools.audio.gemini_voice_clone.write_receipt", Mock(side_effect=OSError("disk full")))
    result = GeminiVoiceClone().execute(clone_inputs)
    assert not result.success
    assert result.data["voice_id"] == "voice_saved"


def test_explicit_gemini_does_not_fallback(monkeypatch):
    from tools.audio.google_tts import GoogleTTS
    selector = TTSSelector()
    gemini, cloud = GeminiTTS(), GoogleTTS()
    monkeypatch.setattr(selector, "_providers", lambda: [gemini, cloud])
    monkeypatch.setattr(cloud, "get_status", lambda: ToolStatus.AVAILABLE)
    cloud_execute = Mock(return_value=ToolResult(success=True))
    monkeypatch.setattr(cloud, "execute", cloud_execute)
    monkeypatch.setattr(gemini, "execute", lambda inputs: ToolResult(success=False, error="Gemini denied"))
    assert selector.execute({"text": "hi", "preferred_provider": "gemini"}).error == "Gemini denied"
    monkeypatch.setattr(gemini, "get_status", lambda: ToolStatus.UNAVAILABLE)
    assert not selector.execute({"text": "hi", "preferred_provider": "gemini"}).success
    cloud_execute.assert_not_called()


def test_google_cloud_synthesis_unchanged(tmp_path, monkeypatch):
    from tools.audio.google_tts import GoogleTTS
    response = Mock()
    response.json.return_value = {"audioContent": base64.b64encode(b"mp3-test").decode()}
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    result = GoogleTTS().execute({"text": "hello", "output_path": str(tmp_path / "cloud.mp3")})
    assert result.success, result.error
    assert "texttospeech.googleapis.com" in post.call_args.args[0]


@pytest.mark.parametrize("steps", [None, "bad", [None], [{"type": "model_output", "content": None}]])
def test_malformed_steps_are_safe(steps):
    with pytest.raises(speech.GeminiSpeechError):
        speech.output_audio({"steps": steps})


def test_missing_consent_and_oversized_reference(monkeypatch, clone_inputs):
    from pathlib import Path
    request = mock_response(monkeypatch, {})
    consent = Path(clone_inputs["consent_path"])
    consent.unlink()
    assert not GeminiVoiceClone().execute(clone_inputs).success
    consent.write_bytes(wav_bytes())
    monkeypatch.setattr(speech, "speech_settings", lambda: OpenMontageConfig().audio.gemini.model_copy(update={"max_reference_bytes": 100}))
    assert not GeminiVoiceClone().execute(clone_inputs).success
    request.assert_not_called()


def test_budget_estimate_is_not_free_quote():
    assert GeminiTTS().estimate_cost({"text": "hi"}) > 0
    assert GeminiVoiceClone().dry_run({})["estimated_cost_usd"] is None
