import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("radar_assembly", ROOT / "scripts/assemble.py")
assembly = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = assembly
spec.loader.exec_module(assembly)


@pytest.fixture
def payload():
    episode = assembly.read_json(ROOT / "config/episode.example.json")
    asset = {"id": "video", "path": "video.mp4", "sha256": "a" * 64, "kind": "video", "credit": "upstream"}
    clips = []
    previous = None
    for index, clip_id in enumerate(episode["clipIds"]):
        clips.append({"id": clip_id, "assetId": "video", "modelId": clip_id, "modelName": clip_id,
                      "previousModelId": previous, "in": index * 6, "out": (index + 1) * 6,
                      "dataVersion": "archive-v1", "axes": list("abcdef"), "status": "historical",
                      "renderReady": False, "audio": "mixed", "bakedPosition": index + 1, "bakedTotal": 3})
        previous = clip_id
    return {"version": 1, "notes": [], "assets": [asset], "clips": clips}, episode


def test_exactly_three_and_preserve_order(payload):
    manifest, episode = payload
    selected, _ = assembly.validate(manifest, episode)
    assert [c["id"] for c in selected] == ["astra", "claude", "deepseek"]


def test_production_rejects_historical_clips(payload):
    manifest, episode = payload
    episode["mode"] = "production"
    with pytest.raises(ValueError, match="Production blocked"):
        assembly.validate(manifest, episode)


def test_frozen_still_requires_render_ready(payload):
    manifest, episode = payload
    episode["mode"] = "production"
    for clip in manifest["clips"]:
        clip["status"] = "frozen"
    with pytest.raises(ValueError, match="renderReady"):
        assembly.validate(manifest, episode)


def test_reordering_rejects_baked_ghost(payload):
    manifest, episode = payload
    episode["clipIds"] = ["claude", "astra", "deepseek"]
    with pytest.raises(ValueError, match="previous model"):
        assembly.validate(manifest, episode)


@pytest.mark.parametrize("change,match", [
    (lambda m: m["clips"].append(copy.deepcopy(m["clips"][0])), "Duplicate"),
    (lambda m: m["clips"][1].update(axes=list("bacdef")), "Mixed axes"),
    (lambda m: m["clips"][1].update(dataVersion="other"), "Mixed axes"),
    (lambda m: m["clips"][0].update(out=0.1, **{"in": 1}), "reversed"),
    (lambda m: m["clips"][1].update(bakedTotal=24), "numbering"),
    (lambda m: m["clips"][0].update(assetId="missing"), "Missing video"),
])
def test_invalid_handoff(payload, change, match):
    manifest, episode = payload
    change(manifest)
    with pytest.raises(ValueError, match=match):
        assembly.validate(manifest, episode)


@pytest.mark.parametrize("relative", ["../outside.mp4", "sub/../../outside.mp4"])
def test_no_path_escape(tmp_path, relative):
    with pytest.raises(ValueError, match="escapes"):
        assembly.within(tmp_path, relative)


def test_snapshot_is_immutable_and_detects_tampering(tmp_path, monkeypatch, payload):
    manifest, episode = payload
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    (source / "video.mp4").write_bytes(b"media fixture")
    checksum = assembly.digest(source / "video.mp4")
    manifest["assets"][0]["sha256"] = checksum
    monkeypatch.setattr(assembly, "probe", lambda p: {"width": 1920, "height": 1080, "fps": 60,
                                                      "duration": 18, "hasAudio": True})
    assembly.materialize(source, manifest, episode, output)
    assert assembly.digest(source / "video.mp4") == checksum
    assert len(assembly.load_snapshot(output)["clips"]) == 3
    with pytest.raises(ValueError, match="already exists"):
        assembly.materialize(source, manifest, episode, output)
    (output / "assets/imported/video.mp4").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        assembly.load_snapshot(output)


def test_changed_source_rejected_before_snapshot(tmp_path, payload):
    manifest, episode = payload
    source = tmp_path / "source"
    source.mkdir()
    (source / "video.mp4").write_bytes(b"not expected hash")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="changed source"):
        assembly.materialize(source, manifest, episode, output)
    assert not (output / "input/snapshot.json").exists()


def test_no_ass_override_injection():
    assert assembly.clean_text(r"{\pos(0,0)}hello") == "（／pos(0,0)）hello"


def test_score_evidence_rejects_raw_speed_and_missing_values(payload):
    sys.path.insert(0, str(ROOT / "scripts"))
    from audit_score_handoff import findings
    manifest, _ = payload
    checksum = "b" * 64
    for clip in manifest["clips"]:
        clip["scoreDataSha256"] = checksum
    data = {"axes": [{"key": key} for key in "abcdef"], "scaleVersion": "archive-v1",
            "models": [{"id": clip["modelId"], "scores": [50] * 6} for clip in manifest["clips"]]}
    assert findings(data, manifest["clips"], checksum) == []
    data["models"][2]["scores"][4] = 227.8
    assert any("0–100" in issue for issue in findings(data, manifest["clips"], checksum))
    data["models"][2]["scores"] = [None] * 6
    assert any("complete scores unavailable" in issue for issue in findings(data, manifest["clips"], checksum))


def test_score_evidence_rejects_wrong_axes_version_and_fingerprint(payload):
    sys.path.insert(0, str(ROOT / "scripts"))
    from audit_score_handoff import findings
    manifest, _ = payload
    data = {"axes": [{"key": key} for key in ["agentic", "coding", "reasoning", "multimodal", "science", "dataAnalysis"]],
            "scaleVersion": "new", "models": [{"id": clip["modelId"], "scores": [50] * 6} for clip in manifest["clips"]]}
    issues = findings(data, manifest["clips"], "b" * 64)
    assert any("axes do not match" in issue for issue in issues)
    assert any("scale version" in issue for issue in issues)
    assert any("fingerprint" in issue for issue in issues)


def test_render_blocked_without_score_and_frame_review(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "load_snapshot", lambda p: {})
    with pytest.raises(ValueError, match="audit required"):
        assembly.render(tmp_path)
    assembly.write_json(tmp_path / "artifacts/score-handoff-audit.json", {
        "status": "blocked", "visualEvidenceConfirmed": False, "issues": ["wrong axes"]})
    with pytest.raises(ValueError, match="has not passed"):
        assembly.render(tmp_path)
