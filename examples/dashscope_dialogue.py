"""Small, resumable two-speaker DOCX smoke test, using the registered TTS tool.

Run from the repository root with uv run --no-project python -m examples.dashscope_dialogue.
This is a bounded audio integration test, not a video production pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import wave
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from tools.tool_registry import registry
from lib.dashscope_config import speech_model


def read_turns(docx: Path, limit: int) -> list[dict]:
    with zipfile.ZipFile(docx) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    turns = []
    for paragraph in document.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        match = re.match(r"^(主播|副播(?:[（(]AI[）)])?)\s*[：:]\s*(.+)$", text)
        if match:
            turns.append({"speaker": "副播" if match[1].startswith("副播") else "主播", "text": match[2]})
            if len(turns) == limit:
                break
    if not turns:
        raise ValueError("No 主播 / 副播 dialogue paragraphs found")
    if {turn["speaker"] for turn in turns} != {"主播", "副播"}:
        raise ValueError("Selected sample must contain both speakers")
    return turns


def join_wav(paths: list[Path], output: Path, gap_seconds: float = 0.3) -> list[dict]:
    segments = []
    cursor = 0.0
    with wave.open(str(output), "wb") as joined:
        expected = None
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as source:
                params = source.getparams()
                signature = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
                if expected is None:
                    expected = signature
                    joined.setparams(params)
                elif signature != expected:
                    raise ValueError("Cannot join WAV files with different audio formats")
                if index:
                    gap_frames = round(gap_seconds * params.framerate)
                    silence_byte = b"\x80" if params.sampwidth == 1 else b"\x00"
                    joined.writeframes(silence_byte * gap_frames * params.nchannels * params.sampwidth)
                    cursor += gap_frames / params.framerate
                # Some provider WAVs keep a streaming placeholder in the data
                # chunk size. Time the bytes actually read, not that header.
                frames = source.readframes(params.nframes)
                duration = len(frames) / (params.nchannels * params.sampwidth * params.framerate)
                segments.append({"start_seconds": round(cursor, 6), "end_seconds": round(cursor + duration, 6)})
                joined.writeframes(frames)
                cursor += duration
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--turns", type=int, choices=range(2, 7), default=4)
    parser.add_argument("--host-voice", default="longshu_v3.1")
    parser.add_argument("--cohost-voice", default="longanhuan_v3.1")
    parser.add_argument("--cohost-instruction", default="用普通话自然接话，带一点轻松的吐槽感，语气灵动克制，像与搭档聊天。")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    turns = read_turns(args.docx, args.turns)
    voices = {"主播": args.host_voice, "副播": args.cohost_voice}
    for turn in turns:
        turn["voice_id"] = voices[turn["speaker"]]
        turn["instruction"] = (
            "用普通话自然讲述科技话题，语气沉稳清晰，像与搭档聊天，避免夸张的广告腔。"
            if turn["speaker"] == "主播" else
            args.cohost_instruction
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plan = {"source": str(args.docx.resolve()), "model": speech_model(purpose="ssml").model, "turns": turns,
            "characters": sum(len(turn["text"]) for turn in turns), "sample_only": True}
    (args.output_dir / "sample_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    registry.ensure_discovered()
    tool = registry.get("dashscope_tts")
    if tool is None:
        raise RuntimeError("dashscope_tts was not discovered")
    paths, results = [], []
    for index, turn in enumerate(turns, 1):
        identity = hashlib.sha256(json.dumps({**turn, "model": plan["model"]}, sort_keys=True).encode()).hexdigest()[:12]
        output = args.output_dir / f"{index:02d}_{identity}.wav"
        record = output.with_suffix(".json")
        if output.is_file() and record.is_file():
            result_data = json.loads(record.read_text(encoding="utf-8"))
            print(f"Reusing completed turn {index}", flush=True)
        else:
            result = tool.execute({"text": turn["text"], "voice_id": turn["voice_id"],
                                   "instruction": turn["instruction"], "model": plan["model"],
                                   "format": "wav", "sample_rate": 24000, "output_path": str(output)})
            if not result.success:
                print(f"Turn {index} failed: {result.error}", flush=True)
                return 1
            result_data = result.data
            record.write_text(json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Generated turn {index}: {result_data['audio_duration_seconds']} seconds", flush=True)
        paths.append(output)
        results.append(result_data)
    timings = join_wav(paths, args.output_dir / "dialogue_sample.wav")
    report = {**plan, "segments": [{**turn, **timing, "result": result}
                                   for turn, timing, result in zip(turns, timings, results)],
              "duration_seconds": timings[-1]["end_seconds"], "gap_seconds": 0.3,
              "timing_type": "turn_boundaries_not_word_alignment"}
    (args.output_dir / "sample_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Sample ready: {args.output_dir / 'dialogue_sample.wav'} ({report['duration_seconds']} seconds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
