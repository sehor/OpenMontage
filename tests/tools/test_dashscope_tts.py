import io
import wave
from unittest.mock import MagicMock

import pytest
import requests

from tools.audio.dashscope_tts import DashScopeTTS
from tools.base_tool import ToolStatus


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret-key")
    monkeypatch.setenv("DASHSCOPE_MODEL", "flash")
    for name in ("SFM_WORKSPACE_ID", "DASHSCOPE_REGION", "DASHSCOPE_TTS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def response(payload=None, content=b"", status=200, content_type="audio/wav"):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.json.return_value = payload
    result.content = content
    result.headers = {"Content-Type": content_type}
    return result


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


def install_http(monkeypatch, **payload_overrides):
    payload = {"request_id": "req-123", "output": {"audio": {"url": "http://result.oss-cn-beijing.aliyuncs.com/test.wav?signature=private"}},
               "usage": {"input_tokens": 15, "output_tokens": 20}}
    payload.update(payload_overrides)
    post = MagicMock(return_value=response(payload=payload))
    get = MagicMock(return_value=response(content=wav_bytes()))
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr("tools.analysis.audio_probe.probe_duration", lambda _: 0.1)
    return post, get


def test_non_streaming_request_and_download(monkeypatch, tmp_path):
    post, get = install_http(monkeypatch)
    result = DashScopeTTS().execute({"text": "你好", "voice_id": "longshu_v3.1", "instruction": "平静地说",
                                    "rate": 1.1, "pitch": 1, "volume": 50,
                                    "output_path": str(tmp_path / "sample.wav")})
    assert result.success, result.error
    sent = post.call_args.kwargs
    assert sent["json"] == {"model": "qwen-audio-3.1-tts-flash", "input": {
        "text": "你好", "voice": "longshu_v3.1", "format": "wav", "sample_rate": 24000,
        "instruction": "平静地说", "rate": 1.1, "pitch": 1, "volume": 50}}
    assert "stream" not in sent
    assert sent["allow_redirects"] is False
    assert get.call_args.args[0].startswith("https://")
    assert "headers" not in get.call_args.kwargs
    assert (tmp_path / "sample.wav").read_bytes() == wav_bytes()
    assert result.data["usage"]["input_tokens"] == 15
    assert result.data["request_id"] == "req-123"
    assert "signature" not in str(result.data)


def test_missing_key_does_not_call_api(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    post, _ = install_http(monkeypatch)
    assert DashScopeTTS().get_status() == ToolStatus.UNAVAILABLE
    assert not DashScopeTTS().execute({"text": "你好"}).success
    post.assert_not_called()


@pytest.mark.parametrize("inputs", [
    {"text": " "}, {"text": "hi", "rate": 0.1}, {"text": "hi", "sample_rate": 123},
    {"text": "hi", "model": "another-model"}, {"text": "hi", "model_id": "another-model"},
    {"text": "hi", "format": "wav", "output_path": "wrong.mp3"},
    {"text": "hi", "format": "opus", "sample_rate": 44100},
])
def test_invalid_input_fails_before_billing(monkeypatch, inputs):
    post, _ = install_http(monkeypatch)
    assert not DashScopeTTS().execute(inputs).success
    post.assert_not_called()


def test_workspace_endpoint(monkeypatch):
    monkeypatch.setenv("SFM_WORKSPACE_ID", "workspace123")
    monkeypatch.setenv("DASHSCOPE_REGION", "ap-southeast-1")
    assert DashScopeTTS._endpoint() == "https://workspace123.ap-southeast-1.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"


@pytest.mark.parametrize("endpoint", ["http://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
    "https://evil.test/api/v1/services/audio/tts/SpeechSynthesizer",
    "https://dashscope.aliyuncs.com.evil.test/api/v1/services/audio/tts/SpeechSynthesizer"])
def test_api_key_cannot_be_sent_to_arbitrary_host(monkeypatch, endpoint):
    post, _ = install_http(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_TTS_ENDPOINT", endpoint)
    assert not DashScopeTTS().execute({"text": "hi"}).success
    post.assert_not_called()


def test_provider_error_is_redacted(monkeypatch):
    post, get = install_http(monkeypatch)
    post.return_value = response(payload={"code": "InvalidApiKey", "message": "bad test-secret-key", "request_id": "req-error"}, status=401)
    result = DashScopeTTS().execute({"text": "hi"})
    assert not result.success
    assert "InvalidApiKey" in result.error
    assert "test-secret-key" not in result.error
    assert result.data["request_id"] == "req-error"
    get.assert_not_called()


def test_timeout_is_not_retried(monkeypatch):
    post, _ = install_http(monkeypatch)
    post.side_effect = requests.Timeout("URL with private data")
    result = DashScopeTTS().execute({"text": "hi"})
    assert not result.success
    assert "Timeout" in result.error
    assert "private data" not in result.error
    assert post.call_count == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "https://evil.test/file.wav", "file:///secret"])
def test_invalid_download_url(monkeypatch, url):
    _, get = install_http(monkeypatch, output={"audio": {"url": url}})
    assert not DashScopeTTS().execute({"text": "hi"}).success
    get.assert_not_called()


def test_invalid_audio_does_not_overwrite(monkeypatch, tmp_path):
    _, get = install_http(monkeypatch)
    get.return_value = response(content=b"not a wave file")
    output = tmp_path / "sample.wav"
    output.write_bytes(b"previous")
    result = DashScopeTTS().execute({"text": "hi", "output_path": str(output)})
    assert not result.success
    assert output.read_bytes() == b"previous"


def test_registry_and_selector_discover_provider(monkeypatch):
    from tools.tool_registry import registry
    from tools.audio.tts_selector import TTSSelector
    registry.ensure_discovered()
    assert isinstance(registry.get("dashscope_tts"), DashScopeTTS)
    assert any(tool.name == "dashscope_tts" for tool in TTSSelector()._providers())


def test_single_voice_ssml_multiple_fragments_request(monkeypatch, tmp_path):
    post, _ = install_http(monkeypatch)
    voices = ["qwen-audio-3.1-tts-flash-geny-123"]
    text = f'<speak voice="{voices[0]}">你好。<break time="400ms"/></speak>' \
           f'<speak voice="{voices[0]}" rate="0.9">晚上好。</speak>'
    result = DashScopeTTS().execute({"text": text, "enable_ssml": True,
                                    "output_path": str(tmp_path / "sample.wav")})
    assert result.success, result.error
    speech = post.call_args.kwargs["json"]["input"]
    assert speech["text"] == text and speech["enable_ssml"] is True
    assert speech["voice"] == voices[0]
    assert "ssml_voice_models" not in speech
    assert result.data["voice_ids"] == voices
    assert post.call_count == 1


def test_multi_voice_ssml_is_blocked_before_billing(monkeypatch):
    post, get = install_http(monkeypatch)
    text = '<speak voice="qwen-audio-3.1-tts-flash-geny-123">你好。</speak>' \
           '<speak voice="qwen-audio-3.1-tts-flash-man-456">晚上好。</speak>'
    result = DashScopeTTS().execute({"text": text, "enable_ssml": True})
    assert not result.success
    assert "only the request-level voice" in result.error
    post.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize("extra", [
    {}, {"enable_ssml": False},
    {"enable_ssml": True, "ssml_voice_models": {}},
    {"enable_ssml": True, "voice_id": "another-voice"},
    {"enable_ssml": True, "ssml_voice_models": {"qwen-audio-3.1-tts-flash-geny-123": "other"}},
])
def test_bad_ssml_options_never_bill(monkeypatch, extra):
    post, get = install_http(monkeypatch)
    result = DashScopeTTS().execute({"text": '<speak voice="qwen-audio-3.1-tts-flash-geny-123">你好</speak>', **extra})
    assert not result.success
    post.assert_not_called()
    get.assert_not_called()


def test_invalid_ssml_structure_never_bills(monkeypatch):
    post, _ = install_http(monkeypatch)
    result = DashScopeTTS().execute({"text": '<speak voice="qwen-audio-3.1-tts-flash-geny-123"><emotion>开心</emotion></speak>',
                                    "enable_ssml": True})
    assert not result.success
    post.assert_not_called()
