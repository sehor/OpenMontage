import base64
import io
import json
from pathlib import Path
from unittest.mock import MagicMock
import wave

import pytest
import requests
import yaml

from lib.dashscope_config import speech_model
from lib.dashscope_dialogue import compile_dialogue
from tools.audio.dashscope_dialogue import DashScopeDialogue
from tools.audio.dashscope_tts import DashScopeTTS


@pytest.fixture
def setup_next(tmp_path, monkeypatch):
    config = {"audio": {"dashscope": {"default_model": "dialogue", "ssml_model": "legacy", "enrollment_model": "legacy", "models": {
        "dialogue": {"model": "configured-audio-model", "protocol": "audio_generation", "sample_rate": 48000, "channels": 2},
        "legacy": {"model": "configured-legacy-model", "protocol": "tts", "default_voice": "legacy-system-voice"}}}}}
    path = tmp_path/"config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("OPENMONTAGE_CONFIG", str(path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-secret")
    for name in ["DASHSCOPE_MODEL", "DASHSCOPE_TTS_ENDPOINT", "SFM_WORKSPACE_ID", "DASHSCOPE_REGION"]:
        monkeypatch.delenv(name, raising=False)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setparams((1,2,24000,0,"NONE","not compressed"))
        stream.writeframes(b"\x01\x00"*12000)
    ref = tmp_path/"reference.wav"
    ref.write_bytes(buffer.getvalue())
    other = tmp_path/"other.wav"
    other.write_bytes(buffer.getvalue())
    monkeypatch.setattr("lib.dashscope_references.probe_duration", lambda _: 0.5)
    monkeypatch.setattr("tools.analysis.audio_probe.probe_duration", lambda _: 0.5)
    def response():
        value = MagicMock()
        value.__enter__.return_value = value
        value.status_code = 200
        value.json.return_value = {"request_id":"req-next", "output":{"audio":{"url":"https://result.oss-cn-beijing.aliyuncs.com/sample.wav?private=signed"}}, "usage":{"input_tokens":123}}
        value.content = buffer.getvalue()
        value.headers = {"Content-Type":"audio/wav"}
        return value
    post = MagicMock(return_value=response())
    get = MagicMock(return_value=response())
    monkeypatch.setattr(requests,"post",post)
    monkeypatch.setattr(requests,"get",get)
    return path, config, ref, other, post, get


def test_default_uses_configured_protocol_and_reference_slots(setup_next, tmp_path):
    _, _, ref, other, post, get = setup_next
    result = DashScopeTTS().execute({"text_prompt":'@voice1 说：“你好。”@voice2 回答：“晚上好。”',
        "reference_paths":[str(ref),str(other)], "output_path":str(tmp_path/"out.wav")})
    assert result.success, result.error
    sent = post.call_args.kwargs["json"]
    assert sent["model"] == "configured-audio-model"
    assert sent["input"]["sample_rate"] == 48000 and sent["input"]["channels"] == 2
    assert base64.b64decode(sent["input"]["references"][0]["audio_data"].split(",",1)[1]) == ref.read_bytes()
    assert not {"voice", "text", "enable_ssml", "reference_paths"} & sent["input"].keys()
    assert result.data["references"][1]["slot"] == "@voice2"
    assert "audio_data" not in str(result.data) and "private=signed" not in str(result.data)
    assert "headers" not in get.call_args.kwargs
    assert post.call_count == 1


def test_config_reload_and_explicit_override(setup_next, monkeypatch, tmp_path):
    path, config, _, _, post, _ = setup_next
    config["audio"]["dashscope"]["models"]["dialogue"]["model"] = "changed-without-code-edits"
    path.write_text(yaml.safe_dump(config),encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_MODEL","legacy")
    assert speech_model().model == "configured-legacy-model"
    assert speech_model("dialogue").model == "changed-without-code-edits"
    result = DashScopeTTS().execute({"text":"你好", "model":"dialogue", "output_path":str(tmp_path/"out.wav")})
    assert result.success, result.error
    assert post.call_args.kwargs["json"]["model"] == "changed-without-code-edits"


@pytest.mark.parametrize("extra", [
    {"voice_id":"flash-enrollment-id"}, {"enable_ssml":True}, {"rate":1.1},
    {"format":"opus"}, {"text_prompt":"@voice2 你好"}, {"text_prompt":"<speak>你好</speak>"},
    {"model":"unknown-model"}, {"model":"dialogue","model_id":"legacy"},
    {"text_prompt":"长"*3001},
    {"text_prompt":"先等<break time=\"500ms\"/>一下"},
])
def test_invalid_next_request_is_rejected_before_billing(setup_next, extra, tmp_path):
    _, _, ref, _, post, get = setup_next
    result = DashScopeTTS().execute({"text_prompt":"@voice1 你好", "reference_paths":[str(ref)],
                                    "output_path":str(tmp_path/"out.wav"), **extra})
    assert not result.success
    post.assert_not_called()
    get.assert_not_called()


def test_reference_limits_and_missing_file_prevent_billing(setup_next, monkeypatch, tmp_path):
    _, _, ref, _, post, _ = setup_next
    for paths in [[str(ref)]*4,[str(tmp_path/"missing.wav")]]:
        assert not DashScopeTTS().execute({"text_prompt":"@voice1 你好", "reference_paths":paths}).success
    monkeypatch.setattr("lib.dashscope_references.probe_duration",lambda _:31)
    assert not DashScopeTTS().execute({"text_prompt":"@voice1 你好", "reference_paths":[str(ref)]}).success
    post.assert_not_called()


def test_exact_prompt_length_boundary_is_accepted(setup_next,tmp_path):
    _, _, _, _, post, _=setup_next
    result=DashScopeTTS().execute({"text_prompt":"字"*3000,"output_path":str(tmp_path/"boundary.wav")})
    assert result.success,result.error
    assert len(post.call_args.kwargs["json"]["input"]["text_prompt"])==3000


def test_compiler_counts_guidance_not_only_spoken_text(setup_next):
    path,config,ref,_,post,_=setup_next
    config["audio"]["dashscope"]["models"]["dialogue"]["max_prompt_length"]=120
    path.write_text(yaml.safe_dump(config),encoding="utf-8")
    source="甲："+"字"*80
    assert len(source)<120
    with pytest.raises(ValueError,match="Compiled prompt.*120"):
        compile_dialogue(source,{"甲":{"audio_path":str(ref)}})
    post.assert_not_called()


def test_compiler_rejects_ssml_instead_of_reading_tags(setup_next):
    _,_,ref,_,post,_=setup_next
    with pytest.raises(ValueError,match="not SSML"):
        compile_dialogue('甲：你好<break time="500ms"/>。',{"甲":{"audio_path":str(ref)}})
    post.assert_not_called()


def test_library_uses_original_audio_not_enrolled_id(setup_next, tmp_path):
    _, _, ref, other, _, _ = setup_next
    folder = tmp_path/"library"/"Geny"
    folder.mkdir(parents=True)
    (folder/"voice.json").write_text(json.dumps({"model":"flash-enrollment-only", "source_path":str(ref),
        "data":{"voice_id":"do-not-send-this-id"}}),encoding="utf-8")
    result = compile_dialogue('甲：你说 "A&B"？\n乙：是的。',{"甲":"Geny","乙":{"audio_path":str(other)}},library=folder.parent)
    assert result["synthesis_inputs"]["reference_paths"] == [str(ref),str(other)]
    assert "do-not-send-this-id" not in result["text_prompt"]
    assert result["roles"]["甲"]["origin_enrollment_model"] == "flash-enrollment-only"
    assert "@voice1" in result["text_prompt"] and "@voice2" in result["text_prompt"]
    assert result["validation"]["text_preserved"]


def test_source_spans_and_delivery_metadata(setup_next):
    _, _, ref, _, _, _ = setup_next
    source="他推开门。你来了！"
    roles={"旁白":{"audio_path":str(ref)},"甲":{"audio_path":str(ref)}}
    segments=[{"role":"旁白","start":0,"end":5},
              {"role":"甲","start":5,"end":len(source),"emotion":"惊喜","rate":0.9,
               "breaks":[{"offset":0,"time_ms":400}]}]
    result=compile_dialogue(source,roles,segments)
    assert len(result["synthesis_inputs"]["reference_paths"]) == 1
    assert result["segments"][1]["text"] == source[5:]
    assert "400毫秒" in result["text_prompt"]
    segments[1]["start"]=6
    with pytest.raises(ValueError,match="Unassigned"):
        compile_dialogue(source,roles,segments)


def test_ids_without_reference_audio_are_not_accepted(setup_next):
    with pytest.raises(ValueError,match="reference audio"):
        compile_dialogue("甲：你好",{"甲":{"voice_id":"old-id","model":"old-model"}})


def test_new_local_tool_saves_prompt_without_billing(setup_next,tmp_path):
    _, _, ref, _, post, _=setup_next
    result=DashScopeDialogue().execute({"source_text":"甲：你好。","roles":{"甲":{"audio_path":str(ref)}},"project_dir":str(tmp_path/"project")})
    assert result.success,result.error
    assert any(p.endswith(".prompt.txt") for p in result.artifacts)
    assert not any(p.endswith(".ssml") for p in result.artifacts)
    post.assert_not_called()


def test_workflow_defaults_to_dialogue_compiler(setup_next,tmp_path,monkeypatch):
    from examples import dashscope_ssml as workflow
    _, _, ref, _, post, _=setup_next
    source=tmp_path/"source.txt"
    source.write_text("甲：你好。",encoding="utf-8")
    roles=tmp_path/"roles.json"
    roles.write_text(json.dumps({"甲":{"audio_path":str(ref)}}),encoding="utf-8")
    registry={"dashscope_dialogue":DashScopeDialogue(),"dashscope_tts":DashScopeTTS()}
    monkeypatch.setattr(workflow.registry,"discover",lambda _:None)
    monkeypatch.setattr(workflow.registry,"get",registry.get)
    args=["--source",str(source),"--roles",str(roles),"--project-dir",str(tmp_path/"project")]
    assert workflow.main(args)==0
    post.assert_not_called()
    assert workflow.main(args+["--synthesize"])==0
    assert post.call_count==1
    receipt=json.loads((tmp_path/"project/artifacts/dialogue.synthesis.json").read_text(encoding="utf-8"))
    assert "audio_data" not in str(receipt)
    assert receipt["result"]["model"]=="configured-audio-model"
