import json
from pathlib import Path

import pytest

from lib.dashscope_ssml import MODEL, compile_ssml, parse_labelled, resolve_voices, validate_ssml
from tools.audio.dashscope_ssml import DashScopeSSML


@pytest.fixture
def roles():
    return {"Geny": {"voice_id": MODEL + "-geny-123", "model": MODEL},
            "男主持": {"voice_id": MODEL + "-man-456", "model": MODEL}}


def test_labelled_multiline_and_special_characters(roles):
    source = 'Geny：你说 "A&B" < 3？\r\n还没有呢。\r\n男主持: 我再试一次。'
    result = compile_ssml(source, roles)
    assert [s["role"] for s in result["segments"]] == ["Geny", "男主持"]
    assert result["segments"][0]["text"] == '你说 "A&B" < 3？\r\n还没有呢。'
    assert "&amp;" in result["ssml"] and "&lt;" in result["ssml"]
    assert "&#13;" in result["ssml"]
    assert "Geny：" not in result["ssml"] and "男主持:" not in result["ssml"]
    assert result["validation"]["text_preserved"] is True
    assert result["synthesis_inputs"]["enable_ssml"] is True
    assert result["synthesis_validation"]["status"] == "blocked_after_listening_failure"


def test_plain_prose_with_ai_assigned_narration_and_emotion(roles):
    source = "他推开门。你来了！"
    segments = [{"role": "男主持", "start": 0, "end": 5},
                {"role": "Geny", "start": 5, "end": len(source), "rate": 1.1,
                 "emotion": "惊喜", "direction": "声音明亮",
                 "breaks": [{"offset": 0, "time_ms": 400}]}]
    result = compile_ssml(source, roles, segments)
    assert 'rate="1.1"' in result["ssml"]
    assert '<break time="400ms"' in result["ssml"]
    assert "惊喜" not in result["ssml"] and "声音明亮" not in result["ssml"]
    assert result["segments"][1]["emotion"] == "惊喜"
    assert validate_ssml(result["ssml"], MODEL)["spoken_text"] == source


@pytest.mark.parametrize("segments, message", [
    ([{"role": "Geny", "start": 1, "end": 4}], "Unassigned"),
    ([{"role": "Geny", "start": 0, "end": 4, "text": "改写原文"}], "exactly"),
    ([{"role": "Geny", "start": 0, "end": 3}, {"role": "Geny", "start": 2, "end": 4}], "overlap"),
    ([{"role": "Geny", "start": 2, "end": 4}, {"role": "Geny", "start": 0, "end": 2}], "reordered"),
    ([{"role": "不确定", "start": 0, "end": 4}], "Unmapped"),
    ([{"role": "Geny", "start": 0, "end": 4, "rate": 3}], "rate"),
    ([{"role": "Geny", "start": 0, "end": 4, "breaks": [{"offset": 8, "time_ms": 400}]}], "offset"),
    ([{"role": "Geny", "start": 0, "end": 4, "breaks": [{"offset": 1, "time_ms": 20}]}], "time_ms"),
])
def test_source_and_annotation_failures(roles, segments, message):
    with pytest.raises(ValueError, match=message):
        compile_ssml("原文内容", roles, segments)


def test_labelled_spans_cannot_include_or_skip_role_prefix(roles):
    source = "Geny：你好。"
    with pytest.raises(ValueError, match="prefix"):
        compile_ssml(source, roles, [{"role": "Geny", "start": 0, "end": len(source)}], "labelled")
    with pytest.raises(ValueError, match="Unassigned"):
        compile_ssml(source, roles, [{"role": "Geny", "start": 5, "end": len(source)}])


def test_unknown_roles_aggregated_and_unlabelled_not_guessed(roles):
    with pytest.raises(ValueError, match="Unmapped.*小张.*小李"):
        compile_ssml("小张：你好。\n小李：晚上好。", roles)
    with pytest.raises(ValueError, match="Unassigned"):
        compile_ssml("普通正文。", roles)
    with pytest.raises(ValueError, match="Unassigned"):
        compile_ssml("旁白在此。\nGeny：你好。", roles)
    with pytest.raises(ValueError, match="AI-assigned"):
        compile_ssml("普通正文。", roles, source_format="plain")


def test_labels_can_be_decorated_after_parsing(roles):
    source = "Geny：先等一下。\n男主持：好。"
    segments = parse_labelled(source, roles)
    segments[0]["breaks"] = [{"offset": 1, "time_ms": 500}]
    assert "500ms" in compile_ssml(source, roles, segments, "labelled")["ssml"]


def test_library_aliases_and_missing_models(tmp_path):
    folder = tmp_path / "man-normal-cn"
    folder.mkdir()
    record = {"success": True, "provider": "dashscope", "model": MODEL,
              "data": {"voice_id": MODEL + "-man-123"}}
    (folder / "voice.json").write_text(json.dumps(record), encoding="utf-8")
    result = resolve_voices({"男主持": "man-normal-cn"}, MODEL, tmp_path)
    assert result["男主持"]["voice_id"] == record["data"]["voice_id"]
    with pytest.raises(ValueError, match="Invalid library"):
        resolve_voices({"男主持": "../other"}, MODEL, tmp_path)
    with pytest.raises(ValueError, match="missing1.*missing2"):
        resolve_voices({"甲": "missing1", "乙": "missing2"}, MODEL, tmp_path)
    record["model"] = "another-model"
    (folder / "voice.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="Voice model"):
        resolve_voices({"男主持": "man-normal-cn"}, MODEL, tmp_path)


@pytest.mark.parametrize("body", [
    '<speak voice="V"><emotion>开心</emotion></speak>',
    '<speak voice="V"><speak voice="V">嵌套</speak></speak>',
    '<speak voice="V" rate="NaN">你好</speak>',
    '<speak voice="V" volume="1.1">你好</speak>',
    '<speak voice="V"><break time="0ms"/>你好</speak>',
    '<speak voice="V"><break time="0.5s"/>你好</speak>',
    '<speak voice="V"><break time="6000ms"/><break time="5000ms"/>你好</speak>',
    '<speak voice="V"><break time="500ms">不能放字</break>你好</speak>',
    '<speak voice="V">你好</speak>外面的正文',
    '<!DOCTYPE speak [<!ENTITY x "你好">]><speak voice="V">&x;</speak>',
    '<speak voice="V">你好<!--注释--></speak>',
    '<speak voice="V"></speak>',
    '<speak>你好</speak>',
    '<speak voice="V">没闭合',
])
def test_invalid_ssml_subset(body):
    with pytest.raises(ValueError):
        validate_ssml(body, MODEL, {"V": MODEL})


def test_inconsistent_voice_model_and_unmapped_system_voice():
    with pytest.raises(ValueError, match="different model"):
        validate_ssml('<speak voice="qwen-audio-3.0-tts-flash-other">你好</speak>', MODEL,
                      {"qwen-audio-3.0-tts-flash-other": MODEL})
    with pytest.raises(ValueError, match="mapped"):
        validate_ssml('<speak voice="longshu_v3.1">你好</speak>', MODEL)


def test_tool_artifacts_are_local_and_failure_writes_nothing(tmp_path, roles, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    tool = DashScopeSSML()
    assert tool.get_status().value == "available"
    result = tool.execute({"source_text": "Geny：你好。\n男主持：晚上好。", "roles": roles,
                           "project_dir": str(tmp_path)})
    assert result.success, result.error
    assert len(result.artifacts) == 3
    assert Path(result.artifacts[0]).read_text(encoding="utf-8") == "Geny：你好。\n男主持：晚上好。"
    stored = json.loads(Path(result.artifacts[2]).read_text(encoding="utf-8"))
    assert stored["roles"]["男主持"]["voice_id"] == roles["男主持"]["voice_id"]
    invalid_dir = tmp_path / "invalid"
    result = tool.execute({"source_text": "无人认领。", "roles": roles, "project_dir": str(invalid_dir)})
    assert not result.success and not invalid_dir.exists()


def test_registry_discovers_local_tool():
    from tools.tool_registry import ToolRegistry
    registry = ToolRegistry()
    registry.discover("tools.audio")
    assert isinstance(registry.get("dashscope_ssml"), DashScopeSSML)


def test_synthesis_receipt_protects_original_artifacts(tmp_path, roles):
    folder = tmp_path / "artifacts"
    folder.mkdir()
    source = folder / "dialogue.source.txt"
    source.write_text("previous source", encoding="utf-8")
    (folder / "dialogue.synthesis.json").write_text('{"status":"request_started"}')
    result = DashScopeSSML().execute({"source_text": "Geny：替换原稿。", "roles": roles,
                                      "project_dir": str(tmp_path)})
    assert not result.success
    assert source.read_text(encoding="utf-8") == "previous source"
