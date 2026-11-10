"""Composition code stays separate from project-specific video resources."""
import json
from pathlib import Path

import pytest

from tools.analysis import composition_validator
from tools.base_tool import ToolResult
from tools.video import video_compose
from tools.video.hyperframes_compose import HyperFramesCompose


@pytest.mark.parametrize("project", ["demo", "radar/episodes/demo"])
def test_hyperframes_code_uses_separate_workspace(tmp_path, monkeypatch, project):
    monkeypatch.setattr(video_compose, "__file__", str(tmp_path / "tools/video/video_compose.py"))
    monkeypatch.setattr(video_compose.VideoCompose, "_hyperframes_available", lambda self: True)
    captured = {}

    def capture(self, inputs):
        captured.update(inputs)
        return ToolResult(success=False, error="stop before rendering")

    monkeypatch.setattr(HyperFramesCompose, "execute", capture)
    output = tmp_path / "video_projects" / project / "renders/final.mp4"
    video_compose.VideoCompose()._render_via_hyperframes(
        inputs={}, edit_decisions={}, asset_manifest={}, resolved_cuts=[],
        output_path=output, profile=None,
    )
    assert Path(captured["workspace_path"]) == tmp_path / "workspaces" / project / "hyperframes"
    assert captured["output_path"] == str(output)
    assert not output.parent.exists()


@pytest.mark.parametrize("project", ["demo", "radar/episodes/demo"])
def test_validator_finds_assets_in_separate_workspace(tmp_path, monkeypatch, project):
    monkeypatch.setattr(composition_validator, "__file__", str(tmp_path / "tools/analysis/composition_validator.py"))
    monkeypatch.chdir(tmp_path)
    assets = tmp_path / "workspaces" / project / "hyperframes/assets"
    assets.mkdir(parents=True)
    (assets / "hero.png").write_bytes(b"image fixture")
    composition = Path("video_projects") / project / "artifacts/edit_decisions.json"
    composition.parent.mkdir(parents=True)
    composition.write_text(json.dumps({
        "render_runtime": "hyperframes",
        "cuts": [{"id": "c1", "source": "hero.png", "in_seconds": 0, "out_seconds": 3}],
    }), encoding="utf-8")
    result = composition_validator.CompositionValidator().execute({"composition_path": str(composition)})
    assert result.success, result.error
