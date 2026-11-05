"""Deterministic media handoff and assembly adapter, never a radar generator.

The agent owns stage decisions/checkpoints. This module validates supplied
inputs, prepares packaging, and dispatches an already reviewed compose request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import jsonschema

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parents[1]
SCHEMA = json.loads((PROJECT / "schemas/assembly.schema.json").read_text(encoding="utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def within(root, relative):
    root = Path(root).resolve()
    if Path(relative).is_absolute():
        raise ValueError("Asset paths must be relative")
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or result == root:
        raise ValueError(f"Path escapes root: {relative}")
    return result


def run(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    if result.returncode:
        raise RuntimeError(result.stderr[-5000:])
    return result.stdout


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {"duration": float(data["format"]["duration"]), "width": video["width"] if video else None,
            "height": video["height"] if video else None,
            "fps": float(Fraction(video["r_frame_rate"])) if video else None,
            "hasAudio": audio is not None, "codec": video["codec_name"] if video else None}


def validate(manifest, episode):
    for name, value in [("manifest", manifest), ("episode", episode)]:
        jsonschema.Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{name}"}).validate(value)
    assets = {a["id"]: a for a in manifest["assets"]}
    clips = {c["id"]: c for c in manifest["clips"]}
    if len(assets) != len(manifest["assets"]) or len(clips) != len(manifest["clips"]):
        raise ValueError("Duplicate asset/clip ID")
    selected = []
    previous = None
    model_ids = set()
    for position, clip_id in enumerate(episode["clipIds"], 1):
        if clip_id not in clips:
            raise ValueError(f"Missing clip: {clip_id}")
        clip = clips[clip_id]
        if clip["assetId"] not in assets or assets[clip["assetId"]]["kind"] != "video":
            raise ValueError(f"Missing video asset: {clip_id}")
        if clip["out"] <= clip["in"]:
            raise ValueError(f"Empty/reversed trim: {clip_id}")
        if clip["modelId"] in model_ids:
            raise ValueError("Duplicate model ID")
        if clip["previousModelId"] != previous:
            raise ValueError(f"Baked previous model mismatch: {clip_id}")
        if clip["bakedPosition"] not in (None, position) or clip["bakedTotal"] not in (None, len(episode["clipIds"])):
            raise ValueError(f"Baked numbering mismatch: {clip_id}")
        if selected and (clip["axes"] != selected[0]["axes"] or clip["dataVersion"] != selected[0]["dataVersion"]):
            raise ValueError("Mixed axes or data scale/version")
        if episode["mode"] == "production" and (clip["status"] != "frozen" or not clip["renderReady"]):
            raise ValueError(f"Production blocked: {clip_id} is not frozen/renderReady")
        selected.append(clip)
        previous = clip["modelId"]
        model_ids.add(previous)
    return selected, assets


def materialize(source, manifest, episode, output):
    selected, assets = validate(manifest, episode)
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Source and output must not overlap")
    if (output / "input/snapshot.json").exists():
        raise ValueError("Snapshot already exists; choose a new episode ID/directory")
    imported = {}
    # Validate ALL selected files first; a missing file cannot leave a valid snapshot.
    for asset_id in dict.fromkeys(c["assetId"] for c in selected):
        asset = assets[asset_id]
        path = within(source, asset["path"])
        if not path.is_file() or digest(path) != asset["sha256"]:
            raise ValueError(f"Missing/changed source asset: {asset_id}")
        media = probe(path)
        if (media["width"], media["height"], media["fps"]) != (episode["width"], episode["height"], episode["fps"]):
            raise ValueError(f"Media format differs from delivery; explicit normalization required: {asset_id}")
        for clip in [c for c in selected if c["assetId"] == asset_id]:
            if clip["out"] > media["duration"] + 1 / episode["fps"]:
                raise ValueError(f"Trim exceeds source duration: {clip['id']}")
            if media["hasAudio"] != (clip["audio"] != "silent"):
                raise ValueError(f"Audio declaration mismatch: {clip['id']}")
        imported[asset_id] = {**asset, "path": f"assets/imported/{asset_id}{path.suffix.lower()}", "probe": media}
    for asset_id, asset in imported.items():
        original = within(source, assets[asset_id]["path"])
        dest = within(output, asset["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, dest)
        if digest(dest) != asset["sha256"] or digest(original) != asset["sha256"]:
            raise ValueError(f"Asset changed while copying: {asset_id}")
    snapshot = {"version": 1, "episode": episode, "clips": selected, "assets": list(imported.values()),
                "notes": manifest["notes"], "sourceManifestSha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
    write_json(output / "input/snapshot.json", snapshot)
    write_json(output / "input/episode.json", episode)
    write_json(output / "input/source-manifest.json", manifest)
    return snapshot


def load_snapshot(output):
    output = Path(output).resolve()
    snapshot = read_json(output / "input/snapshot.json")
    # Recheck configuration AND imported media before every prepare/render.
    manifest = {"version": 1, "notes": snapshot["notes"], "clips": snapshot["clips"],
                "assets": [{k: v for k, v in a.items() if k != "probe"} for a in snapshot["assets"]]}
    validate(manifest, snapshot["episode"])
    for asset in snapshot["assets"]:
        if digest(within(output, asset["path"])) != asset["sha256"]:
            raise ValueError(f"Snapshot file changed: {asset['id']}")
    return snapshot


def ass_time(seconds):
    ticks = round(seconds * 100)
    return f"{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}"


def clean_text(value):
    # Never allow editorial text to inject ASS override commands.
    return value.replace("\\", "／").replace("{", "（").replace("}", "）").replace("\n", " ")


def card(output, name, duration, title, subtitle, lines, fps):
    folder = output / "assets/packaging"
    folder.mkdir(parents=True, exist_ok=True)
    ass = folder / f"{name}.ass"
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,Microsoft YaHei,38,&H00F5F1EB,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def line(text, y, size, color="EBF1F5", bold=0):
        return f"Dialogue: 0,0:00:00.00,{ass_time(duration)},Base,,0,0,0,,{{\\pos(140,{y})\\fs{size}\\b{bold}\\c&H{color}&\\fad(180,180)}}{clean_text(text)}\n"
    content = header + line("LLM RADAR  /  MODEL BRIEF", 120, 26, "EED322")
    content += line(title, 230, 90, bold=1) + line(subtitle, 360, 38, "B8A394")
    for i, text in enumerate(lines):
        content += line(text, 500 + i * 70, 30, "D0C0B0")
    content += line("OPENMONTAGE  ·  MEDIA ASSEMBLY", 945, 24, "847468")
    ass.write_text(content, encoding="utf-8")
    escaped = ass.resolve().as_posix().replace(":", "\\:")
    destination = folder / f"{name}.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=0x080e19:s=1920x1080:r={fps}:d={duration}", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=stereo", "-t", duration, "-vf",
         f"drawbox=x=140:y=430:w=1640:h=2:color=0x243b4d:t=fill,ass='{escaped}'",
         "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", destination])
    return destination


def prepare(output):
    output = Path(output).resolve()
    snap = load_snapshot(output)
    ep, clips = snap["episode"], snap["clips"]
    names = [f"{i + 1:02}   {c['modelName']}" for i, c in enumerate(clips[:5])]
    if len(clips) > 5:
        names[-1] = f"以及另外 {len(clips) - 4} 个模型"
    intro = card(output, "intro", ep["introSeconds"], ep["title"], ep["subtitle"], names, ep["fps"])
    outro = card(output, "outro", ep["outroSeconds"], "素材与制作", "数据与动画来自上游 · 成片由 OpenMontage 组装",
                 ep["credits"], ep["fps"])
    cuts = [{"id": "intro", "source": str(intro), "in_seconds": 0, "out_seconds": ep["introSeconds"]}]
    lookup = {a["id"]: a for a in snap["assets"]}
    chapters, cursor = [], ep["introSeconds"]
    for clip in clips:
        path = str(within(output, lookup[clip["assetId"]]["path"]))
        chapters.append({"clipId": clip["id"], "modelId": clip["modelId"], "start": cursor,
                         "end": cursor + clip["out"] - clip["in"]})
        cursor = chapters[-1]["end"]
        # Adjacent ranges of one pre-rendered source stay continuous, including music.
        if cuts[-1]["source"] == path and abs(cuts[-1]["out_seconds"] - clip["in"]) < 1e-8:
            cuts[-1]["out_seconds"] = clip["out"]
        else:
            cuts.append({"id": clip["id"], "source": path, "in_seconds": clip["in"], "out_seconds": clip["out"]})
    cuts.append({"id": "outro", "source": str(outro), "in_seconds": 0, "out_seconds": ep["outroSeconds"]})
    duration = cursor + ep["outroSeconds"]
    edit = {"version": "1.0", "render_runtime": "ffmpeg", "renderer_family": "documentary-montage", "cuts": cuts,
            "metadata": {"compose_target": {"fps": ep["fps"]}, "proposal_render_runtime": "ffmpeg",
                         "chapters": chapters, "total_duration_seconds": duration, "audio_policy": ep["audioPolicy"]}}
    manifest = {"version": "1.0", "assets": [{"id": c["id"], "type": "video", "path": c["source"],
                 "source_tool": "llm-radar-import" if c["id"] not in ("intro", "outro") else "ffmpeg-packaging",
                 "scene_id": c["id"]} for c in cuts], "total_cost_usd": 0}
    request = {"operation": "render", "edit_decisions": edit, "asset_manifest": manifest,
               "proposal_packet": read_json(output / "artifacts/proposal_packet.json"),
               "output_path": str(output / f"renders/llm-radar-{ep['id']}.mp4"), "crf": 18, "preset": "fast"}
    if ep["mode"] == "preview":
        label = output / "assets/packaging/preview.ass"
        # Explicit canvas/position avoids SRT's default 384px canvas scaling.
        label.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,Microsoft YaHei,26,&H00B8A394,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,9,80,80,24,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + f"Dialogue: 0,0:00:00.00,{ass_time(duration)},Label,,0,0,0,," +
            r"{\an9\pos(1840,24)\fs26\b0\bord0\shad0}组装测试 · 历史素材 · 非最新评测" + "\n", encoding="utf-8")
        edit["subtitles"] = {"enabled": True, "source": str(label)}
        request.update(subtitle_path=str(label), subtitle_style={"font": "Microsoft YaHei", "font_size": 26,
                         "bold": False, "alignment": 9, "margin_v": 24, "outline_width": 0,
                         "primary_color": "&H00B8A394"})
    write_json(output / "artifacts/edit_decisions.json", edit)
    write_json(output / "artifacts/asset_manifest.json", manifest)
    write_json(output / "artifacts/compose-request.json", request)
    write_json(output / "artifacts/timeline.json", {"duration": duration, "chapters": chapters})
    return {"modelCount": len(clips), "duration": duration, "request": str(output / "artifacts/compose-request.json")}


def render(output):
    output = Path(output).resolve()
    load_snapshot(output)
    audit_path = output / "artifacts/score-handoff-audit.json"
    if not audit_path.exists():
        raise ValueError("Score handoff audit required before rendering; historical preview labels are not a substitute")
    audit = read_json(audit_path)
    if audit.get("status") != "data_validated" or not audit.get("visualEvidenceConfirmed") or audit.get("issues"):
        raise ValueError("Radar score/frame evidence has not passed review; obtain matching 0–100 upstream data and clips")
    if digest(output / "input/snapshot.json") != audit.get("snapshotSha256") or digest(audit["scoreDataPath"]) != audit.get("scoreDataSha256"):
        raise ValueError("Score data or snapshot changed after content review")
    sys.path.insert(0, str(REPO))
    from lib.checkpoint import read_checkpoint
    from tools.tool_registry import registry
    ep_id = read_json(output / "input/episode.json")["id"]
    for stage in ("idea", "script", "scene_plan", "assets", "edit"):
        check = read_checkpoint(output, ep_id, stage)
        if not check or check["status"] != "completed":
            raise ValueError(f"Agent must complete/review the {stage} stage first")
    request = read_json(output / "artifacts/compose-request.json")
    edit = read_json(output / "artifacts/edit_decisions.json")
    if request["edit_decisions"] != edit:
        raise ValueError("Compose request no longer matches the reviewed edit")
    checkpoint = read_checkpoint(output, ep_id, "edit")
    if checkpoint["artifacts"]["edit_decisions"] != edit:
        raise ValueError("Edit changed after checkpoint review")
    registry.discover("tools.video")
    result = registry.get("video_compose").execute(request)
    write_json(output / "artifacts/compose-result.json", {"success": result.success, "error": result.error,
               "data": result.data, "artifacts": result.artifacts})
    if not result.success:
        raise RuntimeError(result.error)
    return {"output": request["output_path"], "success": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import", "validate", "prepare", "render"])
    parser.add_argument("--source")
    parser.add_argument("--manifest")
    parser.add_argument("--episode")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "import":
        if not all((args.source, args.manifest, args.episode)):
            parser.error("import requires --source, --manifest and --episode")
        result = materialize(args.source, read_json(args.manifest), read_json(args.episode), args.output)
        result = {"importedAssets": len(result["assets"]), "models": len(result["clips"])}
    elif args.command == "validate":
        result = {"valid": True, "models": len(load_snapshot(args.output)["clips"])}
    elif args.command == "prepare":
        result = prepare(args.output)
    else:
        result = render(args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
