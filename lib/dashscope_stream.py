"""Durable, single-attempt DashScope SSE capture. No recognition or retries."""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path

import requests


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def sse_events(lines):
    """SSE framing, including comments, CRLF, multiline data and final EOF."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def capture_stream(endpoint, key, model, speech, output, timeout, metadata):
    """Keep complete raw JSON events (including audio) and unfinished audio on failure.

    An exclusive receipt blocks another paid submission even after process death.
    The caller receives paths in metadata even if the stream subsequently fails.
    """
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = output.with_suffix(".stream.receipt.json")
    events_path = output.with_suffix(".events.jsonl")
    timestamps = output.with_suffix(".timestamps.json")
    partial = output.with_suffix(output.suffix + ".partial")
    paths = [output, receipt, events_path, timestamps, partial]
    if any(path.exists() for path in paths):
        raise ValueError(f"Prior stream artifacts require review; no automatic resubmission: {receipt}")
    record = {"status": "request_started", "model": model, "input": speech,
              "automatic_retries": 0, "request_id": None, "usage": {}}
    metadata.update(streaming=True, stream_receipt=str(receipt), raw_events_path=str(events_path),
                    timestamps_path=str(timestamps), partial_audio_path=str(partial))
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    sentences, stopped, audio_bytes = [], False, 0
    try:
        with events_path.open("x", encoding="utf-8") as raw_events, partial.open("xb") as audio:
            with requests.post(endpoint, headers={"Authorization": f"Bearer {key}",
                               "X-DashScope-SSE": "enable", "Content-Type": "application/json"},
                               json={"model": model, "input": speech}, timeout=timeout,
                               stream=True, allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    payload = response.json()
                    record.update(request_id=payload.get("request_id"), provider_code=payload.get("code"))
                    metadata.update(request_id=record["request_id"])
                    raise ValueError(f"Aliyun HTTP {response.status_code}: {payload.get('code', 'HTTPError')}")
                if "application/json" in response.headers.get("Content-Type", "").lower():
                    payload = response.json()
                    raw_events.write(json.dumps(payload, ensure_ascii=False).replace(key, "[REDACTED]") + "\n")
                    record.update(request_id=payload.get("request_id"), provider_code=payload.get("code"))
                    metadata.update(request_id=record["request_id"])
                    raise ValueError("Aliyun returned JSON instead of the requested SSE stream")
                for data in sse_events(response.iter_lines()):
                    if data == "[DONE]":
                        break  # Not a substitute for output.finish_reason=stop.
                    payload = json.loads(data)
                    raw_events.write(json.dumps(payload, ensure_ascii=False).replace(key, "[REDACTED]") + "\n")
                    raw_events.flush()
                    if payload.get("request_id"):
                        record["request_id"] = payload["request_id"]
                    if payload.get("usage"):
                        record["usage"] = payload["usage"]
                    metadata.update(request_id=record["request_id"], usage=record["usage"])
                    save_json(receipt, record)
                    if payload.get("code"):
                        raise ValueError(f"Aliyun stream error: {payload['code']}")
                    event = payload.get("output", {})
                    encoded = (event.get("audio") or {}).get("data")
                    if encoded:
                        try:
                            content = base64.b64decode(encoded, validate=True)
                        except binascii.Error as exc:
                            raise ValueError("Invalid Base64 audio event") from exc
                        audio.write(content)
                        audio.flush()
                        audio_bytes += len(content)
                    if event.get("sentence") is not None:
                        sentences.append({"type": event.get("type"), "original_text": event.get("original_text"),
                                          "sentence": event["sentence"]})
                    if event.get("finish_reason") == "stop":
                        stopped = True
            if not stopped or not audio_bytes:
                raise ValueError("Incomplete SSE stream (missing stop or audio); review before retry")
        # A provider WAV can use a placeholder length; retain actual bytes intact.
        if speech["format"] == "wav":
            with partial.open("rb") as audio:
                header = audio.read(12)
            if not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
                raise ValueError("SSE did not return WAV data")
        audio_sha256 = hashlib.sha256(partial.read_bytes()).hexdigest()
        save_json(timestamps, {"schema_version": 1, "time_base": "request", "unit": "milliseconds",
                              "audio_sha256": audio_sha256,
                              "request_id": record["request_id"], "sentences": sentences})
        os.replace(partial, output)
        record.update(status="succeeded", audio_bytes=audio_bytes,
                      audio_sha256=audio_sha256,
                      timestamps_path=str(timestamps), raw_events_path=str(events_path))
        save_json(receipt, record)
        return metadata
    except Exception as exc:
        # Do not persist exception strings: requests may include signed URLs/credentials.
        record.update(status="failed_or_unknown", error_type=type(exc).__name__, audio_bytes=audio_bytes)
        save_json(receipt, record)
        raise
