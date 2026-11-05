"""Resumable Flash synthesis and sample-accurate assembly."""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import wave

from lib.dashscope_timestamps import provider_cues


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def pcm(path):
    with wave.open(str(path), "rb") as audio:
        signature = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
        data = audio.readframes(audio.getnframes())
    if signature != (1, 2, 24000, "NONE") or not data or len(data) % 2:
        raise ValueError("Expected nonempty 24kHz mono PCM16 WAV")
    return data


def normalize(path):
    try:
        pcm(path)
    except (ValueError, wave.Error, EOFError):
        converted = path.with_suffix(".normalized.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ar", "24000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(converted)], check=True, capture_output=True)
        pcm(converted)
        os.replace(converted, path)


def render_flash(plan, folder, tts, subtitles, gap_ms=300):
    if not math.isfinite(gap_ms) or not 0 <= gap_ms <= 10000:
        raise ValueError("gap_ms must be between 0 and 10000")
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".render.lock"
    with lock.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        return _render(plan, folder, tts, subtitles, gap_ms)
    finally:
        lock.unlink()


def _render(plan, folder, tts, subtitles, gap_ms):
    cache = folder / "chunks"
    cache.mkdir(exist_ok=True)
    # Audit ALL existing requests before any new paid call. Keep historical
    # non-streaming identities unchanged, so switching transport cannot rebill them.
    for chunk in plan["chunks"]:
        identity = digest(chunk["synthesis_inputs"])
        receipt = cache / f"{identity}.receipt.json"
        if not receipt.exists() and (cache / f"{identity}.wav").exists():
            raise ValueError(f"Audio without receipt requires review: {cache / f'{identity}.wav'}")
        if receipt.exists():
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record.get("status") != "succeeded":
                raise ValueError(f"Prior request failed or has unknown outcome; review before retry: {receipt}")
            audio = cache / f"{identity}.wav"
            if not audio.is_file() or hashlib.sha256(audio.read_bytes()).hexdigest() != record["audio_sha256"]:
                raise ValueError(f"Cached audio missing or changed; review {receipt}")
            timestamp_path = record.get("result", {}).get("data", {}).get("timestamps_path")
            if timestamp_path and not Path(timestamp_path).is_file():
                raise ValueError(f"needs_review: timestamp file missing; no resynthesis: {timestamp_path}")
            if not timestamp_path:
                review = {"status": "needs_review", "reason": "legacy_audio_without_provider_timestamps",
                          "audio": str(cache / f"{identity}.wav"), "receipt": str(receipt),
                          "action": "Provider timestamps are required. Old audio is unsupported; no automatic resynthesis."}
                save(cache / f"{identity}.legacy.review.json", review)
                raise ValueError(f"needs_review: legacy audio has no provider timestamps; preserved without resynthesis: {receipt}")
    completed = []
    for chunk in plan["chunks"]:
        request = chunk["synthesis_inputs"]
        identity = digest(request)
        audio = cache / f"{identity}.wav"
        receipt = cache / f"{identity}.receipt.json"
        if receipt.exists():
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record["status"] != "succeeded":
                raise ValueError(f"Prior request failed or has unknown outcome; review before retry: {receipt}")
            if not audio.is_file() or hashlib.sha256(audio.read_bytes()).hexdigest() != record["audio_sha256"]:
                raise ValueError(f"Cached audio missing or changed; review {receipt}")
        else:
            if audio.exists():
                raise ValueError(f"Audio without receipt requires review: {audio}")
            # Exclusive creation before the paid request survives process interruption.
            with receipt.open("x", encoding="utf-8") as stream:
                json.dump({"status": "request_started", "inputs": request}, stream, ensure_ascii=False)
            result = tts.execute({**request, "streaming": True, "word_timestamp_enabled": True,
                                  "output_path": str(audio)})
            record = {"status": "failed", "inputs": request, "result": asdict(result)}
            if result.success:
                normalize(audio)
                record.update(status="succeeded", audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest())
            save(receipt, record)
            if not result.success:
                raise ValueError(f"TTS failed; no automatic retry: {receipt}: {result.error}")
        data = pcm(audio)
        duration = len(data) / 48000
        timestamp_path = record.get("result", {}).get("data", {}).get("timestamps_path")
        if not timestamp_path:
            save(cache / f"{identity}.review.json", {"status": "needs_review", "reason": "missing_provider_timestamps"})
            raise ValueError("needs_review: audio saved without provider timestamps; subtitle generation stopped")
        alignment_key = digest({"audio": record["audio_sha256"], "caption_version": 5,
                                "timestamp_sha256": hashlib.sha256(Path(timestamp_path).read_bytes()).hexdigest()})
        captions = cache / f"{alignment_key}.captions.json"
        if captions.exists():
            cues = json.loads(captions.read_text(encoding="utf-8"))
        else:
            try:
                cues = provider_cues(json.loads(Path(timestamp_path).read_text(encoding="utf-8")), duration)
            except (ValueError, KeyError, TypeError) as exc:
                save(cache / f"{alignment_key}.review.json", {"status": "needs_review", "error": str(exc),
                     "chunk": chunk, "timestamps": timestamp_path, "audio": str(audio)})
                raise ValueError(f"needs_review: audio saved; invalid provider caption data: {exc}") from exc
            save(captions, cues)
        completed.append((chunk, audio, data, cues))
    if not completed:
        raise ValueError("No speech chunks")
    version = digest({"plan": plan, "gap_ms": gap_ms,
                      "caption_version": 5, "cues": [item[3] for item in completed]})
    publish = folder / "versions" / version
    publish.mkdir(parents=True, exist_ok=True)
    staging = publish / "dialogue.pending.wav"
    timeline, global_cues, cursor = [], [], 0
    with wave.open(str(staging), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        for i, (chunk, audio, data, cues) in enumerate(completed):
            if i and chunk["turn"] != completed[i-1][0]["turn"]:
                previous = completed[i-1][0]
                # Explicit per-turn gaps override the run default.
                gap = previous.get("gap_after_ms")
                gap = gap_ms if gap is None else gap
                if not isinstance(gap, (int, float)) or not math.isfinite(gap) or not 0 <= gap <= 10000:
                    raise ValueError("Invalid turn gap")
                frames = round(gap * 24)
                output.writeframes(b"\x00\x00" * frames)
                cursor += frames
            start, end = cursor, cursor + len(data)//2
            timeline.append({"role": chunk["role"], "turn": chunk["turn"], "audio": str(audio),
                             "timing_source": "dashscope_sse",
                             "source_start": chunk["start"], "source_end": chunk["end"],
                             "start_sample": start, "end_sample": end,
                             "start": start / 24000, "end": end / 24000})
            for cue in cues:
                item = {**cue, "role": chunk["role"], "start": (round(cue["start"]*24000) + start)/24000,
                        "end": (round(cue["end"]*24000) + start)/24000}
                if not start/24000 <= item["start"] < item["end"] <= end/24000 + 0.001:
                    raise ValueError("Subtitle outside its audio chunk")
                if global_cues and item["start"] < global_cues[-1]["end"]:
                    raise ValueError("Overlapping subtitles")
                global_cues.append(item)
            output.writeframes(data)
            cursor = end
    for fmt in ("srt", "json"):
        result = subtitles.execute({"cues": global_cues, "segments": [], "format": fmt,
                                    "output_path": str(publish / f"subtitles.{fmt}")})
        if not result.success:
            raise ValueError(result.error)
    save(publish / "timeline.json", {"sample_rate": 24000, "duration_samples": cursor,
         "duration_seconds": cursor/24000, "segments": timeline})
    os.replace(staging, publish / "dialogue.wav")
    result = {"status": "complete", "version": version, "directory": str(publish),
              "subtitle_text_sources": sorted({c["text_source"] for c in global_cues}),
              "warnings": sorted({c["limitation"] for c in global_cues if c.get("limitation")}),
              "artifacts": [str(publish / name) for name in
                            ("dialogue.wav", "subtitles.srt", "subtitles.json", "timeline.json")]}
    save(folder / "current.json", result)
    return result
