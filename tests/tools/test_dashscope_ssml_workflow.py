import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from examples import dashscope_ssml as workflow
from lib.dashscope_ssml import MODEL
from tools.audio.dashscope_ssml import DashScopeSSML
from tools.base_tool import ToolResult


@pytest.fixture
def run_args(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes("甲：你好。\r\n再等一下。\r\n乙：好的。".encode())
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({r: {"voice_id": MODEL + "-shared-test", "model": MODEL}
                                for r in ["甲", "乙"]}), encoding="utf-8")
    tts = MagicMock()
    tts.get_status.return_value.value = "available"
    tts.execute.return_value = ToolResult(success=False, error="provider rejected SSML",
                                          data={"request_id": "req-rejected"})
    tools = {"dashscope_ssml": DashScopeSSML(), "dashscope_tts": tts}
    monkeypatch.setattr(workflow.registry, "discover", lambda _: None)
    monkeypatch.setattr(workflow.registry, "get", tools.get)
    project = tmp_path / "project"
    return ["--source", str(source), "--roles", str(roles), "--project-dir", str(project), "--ssml"], tts, project, source


def test_compile_only_never_synthesizes_and_preserves_crlf(run_args):
    args, tts, project, source = run_args
    assert workflow.main(args) == 0
    tts.execute.assert_not_called()
    assert (project / "artifacts/dialogue.source.txt").read_bytes() == source.read_bytes()
    assert not (project / "assets").exists()


def test_provider_failure_keeps_artifacts_and_receipt_and_blocks_retry(run_args):
    args, tts, project, _ = run_args
    assert workflow.main(args + ["--synthesize"]) == 1
    assert tts.execute.call_count == 1
    assert workflow.main(args) == 1  # Even compile-only cannot replace reviewed provenance.
    assert tts.execute.call_count == 1
    inputs = tts.execute.call_args.args[0]
    assert inputs["enable_ssml"] is True and len(inputs["ssml_voice_models"]) == 1
    assert str(project / "assets/audio") in inputs["output_path"]
    record = json.loads((project / "artifacts/dialogue.synthesis.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["result"]["data"]["request_id"] == "req-rejected"
    assert (project / "artifacts/dialogue.ssml").exists()
    assert workflow.main(args + ["--synthesize"]) == 1
    assert tts.execute.call_count == 1


def test_started_receipt_blocks_ambiguous_prior_attempt(run_args):
    args, tts, project, _ = run_args
    (project / "artifacts").mkdir(parents=True)
    (project / "artifacts/dialogue.synthesis.json").write_text('{"status":"request_started"}')
    assert workflow.main(args + ["--synthesize"]) == 1
    tts.execute.assert_not_called()


def test_bad_source_prevents_synthesis(run_args):
    args, tts, project, source = run_args
    source.write_text("尚未确定说话人的正文。", encoding="utf-8")
    assert workflow.main(args + ["--synthesize"]) == 1
    tts.execute.assert_not_called()
    assert not (project / "artifacts/dialogue.synthesis.json").exists()


def test_multi_voice_compile_succeeds_but_synthesis_never_calls_provider(run_args):
    args, tts, project, _ = run_args
    roles_path = Path(args[args.index("--roles") + 1])
    roles_path.write_text(json.dumps({r: {"voice_id": MODEL + f"-{r}-test", "model": MODEL}
                                     for r in ["甲", "乙"]}), encoding="utf-8")
    assert workflow.main(args) == 0
    artifact = json.loads((project / "artifacts/dialogue.json").read_text(encoding="utf-8"))
    assert artifact["validation"]["valid"] is True
    assert artifact["synthesis_validation"]["allowed"] is False
    assert workflow.main(args + ["--synthesize"]) == 1
    tts.execute.assert_not_called()
    assert (project / "artifacts/dialogue.ssml").exists()
    assert not (project / "artifacts/dialogue.synthesis.json").exists()


def test_success_records_usage_without_second_call(run_args):
    args, tts, project, _ = run_args
    def generate(inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True)
        output.write_bytes(b"test-routing-only")
        return ToolResult(success=True, artifacts=[str(output)],
                          data={"request_id": "req-ok", "usage": {"input_tokens": 10}})
    tts.execute.side_effect = generate
    assert workflow.main(args + ["--synthesize"]) == 0
    assert tts.execute.call_count == 1
    record = json.loads((project / "artifacts/dialogue.synthesis.json").read_text(encoding="utf-8"))
    assert record["status"] == "succeeded"
    assert record["result"]["data"]["usage"]["input_tokens"] == 10
