"""Compile a reviewed dialogue prompt; optionally synthesize once with DashScope.

Run from the repo root: uv run --no-project python -m examples.dashscope_ssml --help
This bounded audio workflow does not create a video or infer characters in Python.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from tools.tool_registry import registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="UTF-8 text manuscript")
    parser.add_argument("--model", help="Configured model profile or full ID; defaults to project configuration")
    parser.add_argument("--ssml", action="store_true", help="Explicit legacy single-voice SSML workflow")
    parser.add_argument("--roles", type=Path, required=True, help="JSON role-to-library-name or reference audio mapping")
    parser.add_argument("--segments", type=Path, help="Agent-reviewed JSON list of source spans and delivery annotations")
    parser.add_argument("--source-format", choices=["plain", "labelled"])
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--artifact-name", default="dialogue")
    parser.add_argument("--synthesize", action="store_true", help="Make one paid request; default only compiles locally")
    parser.add_argument("--gap-ms", type=float, default=300, help="Flash turn gap in milliseconds")
    parser.add_argument("--subtitle-model", default="small", choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"])
    parser.add_argument("--language", default="zh")
    args = parser.parse_args(argv)
    registry.discover("tools.audio")
    tool = registry.get("dashscope_ssml" if args.ssml else "dashscope_dialogue")
    # Validate the artifact name before using it in output paths.
    import jsonschema
    jsonschema.validate(args.artifact_name, tool.input_schema["properties"]["artifact_name"])
    project = args.project_dir.resolve()
    receipt = project / "artifacts" / f"{args.artifact_name}.synthesis.json"
    if receipt.exists():
        print(f"Prior synthesis attempt exists: {receipt}. Review it before choosing a new artifact name.")
        return 1
    with args.source.open(encoding="utf-8-sig", newline="") as manuscript:
        inputs = {"source_text": manuscript.read(),
                  "roles": json.loads(args.roles.read_text(encoding="utf-8-sig")),
                  "project_dir": str(project), "artifact_name": args.artifact_name}
    if args.segments:
        inputs["segments"] = json.loads(args.segments.read_text(encoding="utf-8-sig"))
    if args.source_format:
        inputs["source_format"] = args.source_format
    if args.model:
        inputs["model"] = args.model
    compilation = tool.execute(inputs)
    if not compilation.success:
        print(compilation.error)
        return 1
    print(json.dumps({"artifacts": compilation.artifacts, "validation": compilation.data["validation"],
                      "synthesis_validation": compilation.data["synthesis_validation"],
                      "characters": sum(len(s["text"]) for s in compilation.data["segments"])}, ensure_ascii=False), flush=True)
    if not args.synthesize:
        return 0
    if compilation.data.get("mode") == "segmented":
        renderer = registry.get("dashscope_dialogue_render")
        print(f"Flash segmented synthesis: {len(compilation.data['chunks'])} requests; voices "
              f"{[v['voice_id'] for v in compilation.data['roles'].values()]}", flush=True)
        result = renderer.execute({"plan": compilation.data,
                                   "output_dir": str(project / "assets" / "audio" / args.artifact_name),
                                   "gap_ms": args.gap_ms, "model_size": args.subtitle_model,
                                   "language": args.language})
        print(json.dumps({"success": result.success, "error": result.error, "data": result.data}, ensure_ascii=False))
        return 0 if result.success else 1
    if not compilation.data["synthesis_validation"]["allowed"]:
        print(compilation.data["synthesis_validation"]["reason"])
        return 1
    tts = registry.get("dashscope_tts")
    if tts.get_status().value != "available":
        print(tts.install_instructions)
        return 1
    output = project / "assets" / "audio" / f"{args.artifact_name}.wav"
    if output.exists():
        print(f"Refusing to overwrite audio: {output}")
        return 1
    synthesis = {**compilation.data["synthesis_inputs"], "output_path": str(output), "format": "wav"}
    record = {"status": "request_started", "started_at": datetime.now(timezone.utc).isoformat(),
              "inputs": synthesis, "result": None, "automatic_retries": 0,
              "cost_status": "Consult Aliyun bill; framework cost_usd=0 is not a price quote"}
    # Exclusive receipt prevents reruns and concurrent commands from submitting twice.
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    print(f"Single request: dashscope_tts / {synthesis['model']} / roles {list(compilation.data['roles'])}", flush=True)
    result = tts.execute(synthesis)
    record.update(status="succeeded" if result.success else "failed", result=asdict(result))
    receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": result.success, "output": str(output) if result.success else None,
                      "error": result.error, "request_receipt": str(receipt)}, ensure_ascii=False), flush=True)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
