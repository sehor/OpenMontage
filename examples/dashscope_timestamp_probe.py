"""Bounded two-request integration probe; never submits the full manuscript."""
import argparse
from array import array
import json
from dataclasses import asdict
from pathlib import Path
import hashlib
import shutil
import wave

from lib.dashscope_dialogue import compile_dialogue
from lib.dashscope_flash_render import render_flash
from lib.dashscope_stream import save_json
from tools.base_tool import ToolResult
from tools.subtitle.subtitle_gen import SubtitleGen
from tools.tool_registry import registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--synthesize", action="store_true")
    args = parser.parse_args()
    registry.discover("tools.audio")
    roles = {"主播": "man-normal-cn", "副播": "Geny"}
    texts = ["AI需要睡眠。W3C有123项。重庆，你好。", "今天是2026年。API测试。休息后，再学习。"]
    bodies = ['AI需要睡眠。<sub alias="网络标准">W3C</sub>有<say-as interpret-as="cardinal">123</say-as>项。'
              '<break time="500ms"/><phoneme alphabet="py" ph="chong2 qing4">重庆</phoneme>，你好。',
              '今天是<say-as interpret-as="digits">2026</say-as>年。API测试。<break time="300ms"/>休息后，再学习。']
    source = "".join(texts)
    plain = compile_dialogue(source, roles, [
        {"role": role, "start": sum(map(len, texts[:i])), "end": sum(map(len, texts[:i+1]))}
        for i, role in enumerate(roles)], "plain", "flash")
    segments = []
    for i, role in enumerate(roles):
        voice = plain["roles"][role]["voice_id"]
        segments.append({"role": role, "start": sum(map(len, texts[:i])), "end": sum(map(len, texts[:i+1])),
                         "ssml": f'<speak voice="{voice}">{bodies[i]}</speak>'})
    plan = compile_dialogue(source, roles, segments, "plain", "flash")
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_json(args.output_dir / "plan.json", plan)
    if not args.synthesize:
        print("Dry run: two short requests. Add --synthesize to submit.")
        return
    for i, chunk in enumerate(plan["chunks"]):
        output = args.output_dir / f"probe-{i+1}.wav"
        result_path = args.output_dir / f"probe-{i+1}.result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not result["success"]:
                raise RuntimeError("Prior failed probe requires review; no retries")
            print(f"Reusing {result_path}")
            continue
        result = registry.get("dashscope_tts").execute({**chunk["synthesis_inputs"], "streaming": True,
                   "word_timestamp_enabled": True, "output_path": str(output)})
        save_json(result_path, asdict(result))
        print(json.dumps({"success": result.success, "error": result.error, "data": result.data}, ensure_ascii=False), flush=True)
        if not result.success:
            return
    # Replay captured responses through the real renderer. This adapter cannot
    # call the network, so rechecking assembly/captions never incurs another fee.
    class CapturedAudio:
        def execute(self, inputs):
            for i, chunk in enumerate(plan["chunks"]):
                if all(inputs.get(k) == v for k, v in chunk["synthesis_inputs"].items()):
                    audio = args.output_dir / f"probe-{i+1}.wav"
                    receipt = json.loads(audio.with_suffix(".stream.receipt.json").read_text(encoding="utf-8"))
                    if receipt["status"] != "succeeded" or hashlib.sha256(audio.read_bytes()).hexdigest() != receipt["audio_sha256"]:
                        raise ValueError("Captured probe audio failed integrity check")
                    speech = receipt["input"]
                    if (receipt["model"] != plan["model"] or speech["text"] != inputs["text"]
                            or speech["voice"] != inputs["voice_id"]):
                        raise ValueError("Captured request differs from probe plan; review without resynthesis")
                    result = json.loads((args.output_dir / f"probe-{i+1}.result.json").read_text(encoding="utf-8"))
                    shutil.copyfile(audio, inputs["output_path"])
                    data = result["data"]
                    for field in ("timestamps_path", "raw_events_path", "stream_receipt"):
                        data[field] = str(Path(data[field]).resolve())
                    return ToolResult(success=True, data=data)
            raise ValueError("Unexpected paid request; probe replay refuses it")
    assembled = render_flash(plan, args.output_dir / "assembled", CapturedAudio(), SubtitleGen())
    directory = Path(assembled["directory"])
    with wave.open(str(directory / "dialogue.wav"), "rb") as audio:
        data = audio.readframes(audio.getnframes())
        frames = len(data) // 2
        peak = max(abs(sample) for sample in array("h", data))
    timeline = json.loads((directory / "timeline.json").read_text(encoding="utf-8"))
    captions = json.loads((directory / "subtitles.json").read_text(encoding="utf-8"))["cues"]
    assert frames == timeline["duration_samples"]
    assert peak > 0
    assert all(0 <= c["start"] < c["end"] <= frames/24000 for c in captions)
    audit = {"assembly": assembled, "duration_samples": frames, "duration_seconds": frames/24000,
             "roles": list(roles), "caption_count": len(captions),
             "text_sources": sorted({c["text_source"] for c in captions}),
             "peak_pcm16": peak,
             "automatic_retries": 0, "asr_calls": 0}
    save_json(args.output_dir / "audit.json", audit)
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == "__main__":
    main()
