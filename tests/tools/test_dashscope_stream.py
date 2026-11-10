"""SSE transport, provider alignment and resumable Flash integration contracts."""
import base64
from copy import deepcopy
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import wave
from unittest.mock import Mock

import pytest
import requests

from lib.dashscope_dialogue import compile_dialogue
from lib.dashscope_flash_render import digest, render_flash, save
from lib.dashscope_stream import sse_events
from lib.dashscope_timestamps import provider_cues
from tools.audio.dashscope_tts import DashScopeTTS
from tools.base_tool import ToolResult
from tools.subtitle.subtitle_gen import SubtitleGen


MODEL = "qwen-audio-3.1-tts-flash"
ROLES = {"A": {"voice_id": "voice-a", "model": MODEL, "ssml_supported": True},
         "B": {"voice_id": "voice-b", "model": MODEL, "ssml_supported": True}}


def plan(source="A：你好。\nB：好的。", **kwargs):
    return compile_dialogue(source, deepcopy(ROLES), model="flash", **kwargs)


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 24000)
    return buffer.getvalue()


def word(text, begin=100, end=900, index=0):
    return {"text": text, "begin_time": begin, "end_time": end,
            "begin_index": index, "end_index": index+1}


def event(words=None, text="你好。", index=0, kind="sentence-end"):
    return {"type": kind, "original_text": text,
            "sentence": {"index": index, "words": words if words is not None else [word("你好。")]}}


def capture(*events):
    return {"schema_version": 1, "time_base": "request", "unit": "milliseconds", "sentences": list(events)}


def install_stream(monkeypatch, payloads, interrupt=None):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = 200
    response.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    def lines():
        yield b": keepalive"
        yield b""
        for payload in payloads:
            yield ("data: " + json.dumps(payload, ensure_ascii=False)).encode()
            yield b""
        if interrupt:
            raise interrupt
    response.iter_lines.side_effect = lines
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-testing")
    for name in ("DASHSCOPE_TTS_ENDPOINT", "SFM_WORKSPACE_ID", "DASHSCOPE_REGION"):
        monkeypatch.delenv(name, raising=False)
    return post, response


def stream_payloads(stop=True):
    audio = wav_bytes()
    payloads = [{"request_id": "req-stream", "output": event(kind="sentence-begin")}]
    for data in (audio[:50], audio[50:]):
        output = event(kind="sentence-synthesis")
        output["audio"] = {"data": base64.b64encode(data).decode()}
        payloads.append({"request_id": "req-stream", "output": output})
    payloads.append({"request_id": "req-stream", "output": event()})
    if stop:
        payloads.append({"request_id": "req-stream", "output": {"finish_reason": "stop"},
                         "usage": {"input_tokens": 3, "output_tokens": 10}})
    return payloads


def test_sse_framing_multiline_crlf_comment_unicode_and_eof():
    lines = [b": comment\r", b"id: 1\r", b"event: result\r", b'data: {"text":\r',
             'data: "你好"}\r'.encode(), b"\r", b"data: [DONE]"]
    events = list(sse_events(lines))
    assert json.loads(events[0]) == {"text": "你好"}
    assert events[1] == "[DONE]"


def test_stream_writes_audio_raw_events_timestamp_and_receipt(monkeypatch, tmp_path):
    post, _ = install_stream(monkeypatch, stream_payloads())
    output = tmp_path / "sample.wav"
    result = DashScopeTTS().execute({"model": "flash", "text": "你好。", "streaming": True,
                                    "word_timestamp_enabled": True, "output_path": str(output)})
    assert result.success, result.error
    assert output.read_bytes() == wav_bytes()
    assert post.call_count == 1 and post.call_args.kwargs["stream"] is True
    assert post.call_args.kwargs["headers"]["X-DashScope-SSE"] == "enable"
    assert post.call_args.kwargs["json"]["input"]["word_timestamp_enabled"] is True
    assert "secret-for-testing" not in "".join(p.read_text() for p in tmp_path.glob("*.json*"))
    assert result.data["request_id"] == "req-stream"
    assert result.data["usage"]["output_tokens"] == 10
    timestamps = json.loads(Path(result.data["timestamps_path"]).read_text())
    assert provider_cues(timestamps, 1)[0]["end"] == .9
    assert len(Path(result.data["raw_events_path"]).read_text().splitlines()) == 5
    receipt = json.loads(Path(result.data["stream_receipt"]).read_text())
    assert receipt["status"] == "succeeded" and receipt["automatic_retries"] == 0
    # An explicit repeated call with the same output is also protected.
    assert not DashScopeTTS().execute({"model": "flash", "text": "你好。", "streaming": True,
                                     "output_path": str(output)}).success
    assert post.call_count == 1


@pytest.mark.parametrize("failure", ["eof", "timeout", "malformed", "provider_error", "bad_audio", "done"])
def test_partial_failure_preserves_evidence_and_never_retries(monkeypatch, tmp_path, failure):
    payloads = stream_payloads(stop=False)
    interrupt = requests.Timeout("private URL") if failure == "timeout" else None
    if failure == "provider_error":
        payloads.append({"request_id": "req-stream", "code": "Failed", "message": "provider failed"})
    if failure == "bad_audio":
        payloads[1]["output"]["audio"]["data"] = "not base64!"
    post, response = install_stream(monkeypatch, payloads, interrupt)
    if failure in {"malformed", "done"}:
        response.iter_lines.side_effect = lambda: iter([b'data: {"request_id":"req-stream"}', b"",
                                                       b"data: {broken" if failure == "malformed" else b"data: [DONE]", b""])
    inputs = {"model": "flash", "text": "你好。", "streaming": True, "output_path": str(tmp_path / "failed.wav")}
    result = DashScopeTTS().execute(inputs)
    assert not result.success and not (tmp_path / "failed.wav").exists()
    assert result.data["request_id"] == "req-stream"
    receipt = json.loads(Path(result.data["stream_receipt"]).read_text())
    assert receipt["status"] == "failed_or_unknown"
    assert Path(result.data["partial_audio_path"]).is_file()
    assert Path(result.data["raw_events_path"]).is_file()
    assert not DashScopeTTS().execute(inputs).success
    assert post.call_count == 1


def test_stream_http_error_has_request_receipt(monkeypatch, tmp_path):
    post, response = install_stream(monkeypatch, [])
    response.status_code = 429
    response.json.return_value = {"request_id": "throttled", "code": "Throttling"}
    result = DashScopeTTS().execute({"text": "你好", "model": "flash", "streaming": True,
                                    "output_path": str(tmp_path / "error.wav")})
    assert not result.success and result.data["request_id"] == "throttled"
    assert post.call_count == 1


def test_json_instead_of_sse_retains_request_id_without_download_or_retry(monkeypatch, tmp_path):
    post, response = install_stream(monkeypatch, [])
    response.headers = {"Content-Type": "application/json"}
    response.json.return_value = {"request_id": "unexpected-json", "code": "BadRequest", "message": "secret-for-testing"}
    result = DashScopeTTS().execute({"text": "你好", "model": "flash", "streaming": True,
                                    "output_path": str(tmp_path / "error.wav")})
    assert not result.success and result.data["request_id"] == "unexpected-json"
    assert "secret-for-testing" not in Path(result.data["raw_events_path"]).read_text()
    assert post.call_count == 1


def test_streaming_ssml_and_timestamps_are_forwarded_together(monkeypatch, tmp_path):
    post, _ = install_stream(monkeypatch, stream_payloads())
    text = '<speak><sub alias="你好">HELLO</sub><break time="300ms"/></speak>'
    result = DashScopeTTS().execute({"text": text, "model": "flash", "streaming": True,
              "voice_id": MODEL + "-clone",
              "enable_ssml": True, "word_timestamp_enabled": True, "output_path": str(tmp_path / "ssml.wav")})
    assert result.success, result.error
    speech = post.call_args.kwargs["json"]["input"]
    assert speech["enable_ssml"] and speech["word_timestamp_enabled"] and speech["text"] == text
    assert speech["voice"] == MODEL + "-clone"


def test_process_interrupt_retains_started_receipt_and_blocks_resubmission(monkeypatch, tmp_path):
    post, response = install_stream(monkeypatch, stream_payloads(stop=False), KeyboardInterrupt())
    inputs = {"text": "你好", "model": "flash", "streaming": True, "output_path": str(tmp_path / "interrupted.wav")}
    with pytest.raises(KeyboardInterrupt):
        DashScopeTTS().execute(inputs)
    receipt = json.loads((tmp_path / "interrupted.stream.receipt.json").read_text())
    assert receipt["status"] == "request_started" and receipt["request_id"] == "req-stream"
    assert not DashScopeTTS().execute(inputs).success
    assert post.call_count == 1


@pytest.mark.parametrize("options", [{"word_timestamp_enabled": True}, {"model": "next", "streaming": True}])
def test_unsupported_timestamp_modes_fail_before_billing(monkeypatch, options):
    post, _ = install_stream(monkeypatch, [])
    result = DashScopeTTS().execute({"model": "flash", "text": "hi", **options})
    assert not result.success
    post.assert_not_called()


def test_snapshot_deduplication_and_request_relative_time():
    chunk = plan("A：你好。再见。")["chunks"][0]
    events = [event([word("你好", 100, 300)], "你好。", kind="sentence-begin"),
              event([word("你好。", 100, 400)], None, kind="sentence-synthesis"),
              event([word("你好。", 100, 400)], "你好。"),
              event([word("再见。", 900, 1400, 2)], "再见。", 1)]
    cues = provider_cues(capture(*events), 1.5)
    assert [(c["start"], c["end"]) for c in cues] == [(.1, .4), (.9, 1.4)]
    assert "".join(c["text"] for c in cues) == chunk["text"]


def test_returned_original_text_handles_alias_numbers_and_phoneme_directly():
    raw = capture(event([word("A I有网络标准和一百二十三项。", 100, 1000)], "AI有W3C和123项。"),
                  event([word("[ch][�ng][q][�ng]，你好。", 1660, 2300)], "重庆，你好。", 1))
    cues = provider_cues(raw, 2.4)
    assert [c["text"] for c in cues] == ["AI有W3C和123项。", "重庆，你好。"]
    assert all(c["text_source"] == "original_text" and "limitation" not in c for c in cues)
    assert cues[1]["start"] == 1.66


def test_missing_end_original_text_uses_begin_text_without_matching():
    raw = capture(event([word("网络标准")], "W3C", kind="sentence-begin"),
                  event([word("网络标准")], None))
    assert provider_cues(raw, 1)[0]["text"] == "W3C"


def test_missing_original_text_uses_returned_words_without_source_fallback():
    cue = provider_cues(capture(event([word("returned text")], None)), 1)[0]
    assert cue["text"] == "returned text" and cue["text_source"] == "words"
    assert "limitation" in cue


@pytest.mark.parametrize("events", [
    [event(kind="sentence-synthesis")],
    [event([word("你好", 900, 500)])],
    [event([word("你好", 0, 2000)])],
    [event([word("你好", -1, 500)])],
    [event([word("你好", float("nan"), 500)])],
    [event([word("你好", 0, 0)])],
    [event(index=1)],
    [event([])],
    [event([word("")], None)],
])
def test_invalid_or_incomplete_provider_timestamps_are_rejected(events):
    with pytest.raises(ValueError, match="needs_review"):
        provider_cues(capture(*events), 1)


def test_sentence_relative_reset_is_not_silently_offset():
    with pytest.raises(ValueError, match="sentence-relative"):
        provider_cues(capture(event(), event([word("再见。", 100, 600)], "再见。", 1)), 2)


def test_long_provider_sentence_is_kept_without_text_matching_or_time_splitting():
    text = "长句" * 30
    cues = provider_cues(capture(event([word("音素")], text)), 1)
    assert len(cues) == 1 and cues[0]["text"] == text
    assert (cues[0]["start"], cues[0]["end"]) == (.1, .9)


def test_provider_text_is_not_compared_to_word_text():
    cue = provider_cues(capture(event([word("不同的词")], "服务端返回原文。")), 1)[0]
    assert cue["text"] == "服务端返回原文。"


def test_repeated_final_snapshot_uses_latest_provider_text_once():
    cues = provider_cues(capture(event(), event([word("最终词")], "最终原文。")), 1)
    assert len(cues) == 1 and cues[0]["text"] == "最终原文。"


def fake_tts():
    calls = []
    def execute(inputs):
        assert inputs["streaming"] and inputs["word_timestamp_enabled"]
        calls.append(inputs)
        path = Path(inputs["output_path"])
        path.write_bytes(wav_bytes())
        text = "你好。" if inputs["voice_id"] == "voice-a" else "好的。"
        timestamps = path.with_suffix(".timestamps.json")
        save(timestamps, capture(event([word(text)], text)))
        return ToolResult(success=True, data={"timestamps_path": str(timestamps), "request_id": str(len(calls))})
    return Mock(execute=execute), calls


def test_provider_multi_role_assembly_offsets_cache_and_no_asr(tmp_path):
    tts, calls = fake_tts()
    compiled = plan("A：你好。\nB：好的。\nA：你好。")
    compiled["chunks"][0]["gap_after_ms"] = 125
    result = render_flash(compiled, tmp_path, tts, SubtitleGen(), gap_ms=333)
    directory = Path(result["directory"])
    timeline = json.loads((directory / "timeline.json").read_text())
    captions = json.loads((directory / "subtitles.json").read_text())["cues"]
    assert [s["start_sample"] for s in timeline["segments"]] == [0, 27000, 58992]
    assert [c["start"] for c in captions] == [.1, 1.225, 2.558]
    assert timeline["duration_samples"] == 82992
    assert all(s["timing_source"] == "dashscope_sse" for s in timeline["segments"])
    assert len(calls) == 2  # Same request reused in another turn.
    changed = render_flash(compiled, tmp_path, tts, SubtitleGen(), gap_ms=100)
    assert result["version"] != changed["version"] and len(calls) == 2


def test_invalid_provider_timestamps_reuse_audio_after_local_correction(tmp_path):
    tts, calls = fake_tts()
    compiled = plan("A：你好。")
    original = tts.execute
    def incorrect(inputs):
        result = original(inputs)
        save(Path(result.data["timestamps_path"]), capture(event([word("你好。", 100, 2000)])))
        return result
    tts.execute = incorrect
    with pytest.raises(ValueError, match="needs_review"):
        render_flash(compiled, tmp_path, tts, SubtitleGen())
    assert len(calls) == 1 and not (tmp_path / "current.json").exists()
    assert list((tmp_path / "chunks").glob("*.review.json"))
    timestamps = next((tmp_path / "chunks").glob("*.timestamps.json"))
    save(timestamps, capture(event()))
    render_flash(compiled, tmp_path, tts, SubtitleGen())
    assert len(calls) == 1


def test_legacy_cache_preflight_blocks_rebilling_even_when_later_in_plan(tmp_path):
    compiled = plan()
    cache = tmp_path / "chunks"
    cache.mkdir()
    identity = digest(compiled["chunks"][1]["synthesis_inputs"])
    audio = cache / f"{identity}.wav"
    audio.write_bytes(wav_bytes())
    receipt = cache / f"{identity}.receipt.json"
    save(receipt, {"status": "succeeded", "inputs": compiled["chunks"][1]["synthesis_inputs"],
                  "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                  "result": asdict(ToolResult(success=True, data={"request_id": "old-paid"}))})
    original_receipt = receipt.read_bytes()
    tts = Mock()
    with pytest.raises(ValueError, match="legacy audio has no provider timestamps"):
        render_flash(compiled, tmp_path, tts, SubtitleGen())
    tts.execute.assert_not_called()
    assert receipt.read_bytes() == original_receipt and audio.read_bytes() == wav_bytes()
    assert list(cache.glob("*.legacy.review.json"))


def test_unknown_request_and_stale_lock_block_all_new_calls(tmp_path):
    compiled = plan()
    cache = tmp_path / "chunks"
    cache.mkdir()
    identity = digest(compiled["chunks"][1]["synthesis_inputs"])
    save(cache / f"{identity}.receipt.json", {"status": "request_started"})
    tts = Mock()
    with pytest.raises(ValueError, match="unknown outcome"):
        render_flash(compiled, tmp_path, tts, SubtitleGen())
    tts.execute.assert_not_called()
    (tmp_path / ".render.lock").write_text("123")
    with pytest.raises(FileExistsError):
        render_flash(compiled, tmp_path, tts, SubtitleGen())
    assert (tmp_path / ".render.lock").is_file()


def test_registered_renderer_does_not_require_or_discover_whisper(monkeypatch, tmp_path):
    from tools.tool_registry import registry
    from tools.audio.dashscope_dialogue_render import DashScopeDialogueRender
    tts, calls = fake_tts()
    discovered = []
    monkeypatch.setattr(registry, "discover", discovered.append)
    monkeypatch.setattr(registry, "get", {"dashscope_tts": tts, "subtitle_gen": SubtitleGen()}.__getitem__)
    tool = DashScopeDialogueRender()
    assert tool.dependencies == []
    result = tool.execute({"plan": plan(), "output_dir": str(tmp_path)})
    assert result.success, result.error
    assert "tools.analysis" not in discovered and len(calls) == 2


def test_cli_only_passes_provider_render_options(monkeypatch, tmp_path):
    from examples import dashscope_ssml as workflow
    from tools.audio.dashscope_dialogue import DashScopeDialogue
    source = tmp_path / "source.txt"
    source.write_text("A：你好。\nB：好的。", encoding="utf-8")
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps(ROLES), encoding="utf-8")
    renderer = Mock()
    renderer.execute.return_value = ToolResult(success=True, data={"status": "complete"})
    monkeypatch.setattr(workflow.registry, "discover", lambda _: None)
    monkeypatch.setattr(workflow.registry, "get", {"dashscope_dialogue": DashScopeDialogue(),
                                                 "dashscope_dialogue_render": renderer}.__getitem__)
    args = ["--source", str(source), "--roles", str(roles), "--project-dir", str(tmp_path / "project"),
            "--model", "flash", "--synthesize"]
    assert workflow.main(args) == 0
    assert set(renderer.execute.call_args.args[0]) == {"plan", "output_dir", "gap_ms"}


@pytest.mark.parametrize("obsolete", [{"legacy_subtitles": "asr"}, {"model_size": "small"}, {"language": "zh"}])
def test_removed_recognition_options_are_rejected_before_any_request(monkeypatch, tmp_path, obsolete):
    from tools.tool_registry import registry
    from tools.audio.dashscope_dialogue_render import DashScopeDialogueRender
    discover = Mock()
    monkeypatch.setattr(registry, "discover", discover)
    result = DashScopeDialogueRender().execute({"plan": plan(), "output_dir": str(tmp_path), **obsolete})
    assert not result.success
    discover.assert_not_called()


def test_recorded_flash_cloned_voice_ssml_timestamp_responses():
    fixture = Path(__file__).resolve().parents[1] / "fixtures/dashscope/flash_ssml_timestamps.json"
    recording = json.loads(fixture.read_text(encoding="utf-8"))
    captions = []
    for sample in recording["samples"]:
        cues = provider_cues(sample["capture"], sample["duration"])
        assert "".join(c["text"] for c in cues) == sample["chunk"]["text"]
        assert all(0 <= c["start"] < c["end"] <= sample["duration"] for c in cues)
        captions.extend(cues)
    assert len(captions) == 4
    assert captions[1]["text"] == "重庆，你好。"
    assert all(c["text_source"] == "original_text" for c in captions)
    assert (captions[1]["start"], captions[1]["end"]) == (4.66, 6.34)
    assert (captions[-1]["start"], captions[-1]["end"]) == (3.1, 4.22)
