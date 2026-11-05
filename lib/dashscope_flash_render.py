"""Resumable Flash synthesis and sample-accurate assembly."""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import wave

from lib.dashscope_alignment import align_chunk


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


def render_flash(plan, folder, tts, transcriber, subtitles, gap_ms=300, model_size="small", language="zh"):
    if not math.isfinite(gap_ms) or not 0 <= gap_ms <= 10000:
        raise ValueError("gap_ms must be between 0 and 10000")
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".render.lock"
    with lock.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        return _render(plan, folder, tts, transcriber, subtitles, gap_ms, model_size, language)
    finally:
        lock.unlink()


def _render(plan, folder, tts, transcriber, subtitles, gap_ms, model_size, language):
    cache = folder / "chunks"
    cache.mkdir(exist_ok=True)
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
            if hasattr(transcriber, "get_status") and transcriber.get_status().value != "available":
                raise ValueError("Local transcriber unavailable; install faster-whisper before paid synthesis")
            # Exclusive creation before the paid request survives process interruption.
            with receipt.open("x", encoding="utf-8") as stream:
                json.dump({"status": "request_started", "inputs": request}, stream, ensure_ascii=False)
            result = tts.execute({**request, "output_path": str(audio)})
            record = {"status": "failed", "inputs": request, "result": asdict(result)}
            if result.success:
                normalize(audio)
                record.update(status="succeeded", audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest())
            save(receipt, record)
            if not result.success:
                raise ValueError(f"TTS failed; no automatic retry: {receipt}: {result.error}")
        data = pcm(audio)
        duration = len(data) / 48000
        alignment_key = digest({"audio": record["audio_sha256"], "text": chunk["text"],
                                "reading_map": chunk["reading_map"], "model_size": model_size,
                                "language": language, "alignment_version": 2})
        captions = cache / f"{alignment_key}.captions.json"
        if captions.exists():
            cues = json.loads(captions.read_text(encoding="utf-8"))
        else:
            transcript_path = cache / f"{identity}.{model_size}.{language}.transcript.json"
            if transcript_path.exists():
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            else:
                result = transcriber.execute({"input_path": str(audio), "language": language,
                                              "model_size": model_size, "diarize": False,
                                              "output_dir": str(cache / f"{identity}.asr")})
                if not result.success:
                    raise ValueError(f"Audio saved; subtitle transcription failed: {result.error}")
                transcript = result.data["segments"]
                save(transcript_path, transcript)
            try:
                cues = align_chunk(chunk, transcript, duration)
            except ValueError as exc:
                save(cache / f"{alignment_key}.review.json", {"status": "needs_review", "error": str(exc),
                     "chunk": chunk, "transcript": str(transcript_path)})
                raise
            save(captions, cues)
        completed.append((chunk, audio, data, cues))
    if not completed:
        raise ValueError("No speech chunks")
    version = digest({"plan": plan, "gap_ms": gap_ms, "model_size": model_size, "language": language})
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
              "artifacts": [str(publish / name) for name in
                            ("dialogue.wav", "subtitles.srt", "subtitles.json", "timeline.json")]}
    save(folder / "current.json", result)
    return result
