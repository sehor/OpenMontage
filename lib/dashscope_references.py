"""Validate and encode local reference recordings for the audio-generation protocol."""
import base64
import hashlib
import re
from pathlib import Path

from tools.analysis.audio_probe import probe_duration


def reject_ssml(text):
    if re.search(r"<\s*/?\s*(?:speak|break|prosody|voice|phoneme|say-as|sub)\b", text, re.IGNORECASE):
        raise ValueError("Audio-generation protocol requires a dialogue prompt, not SSML")


def reference_info(path, profile):
    path = Path(path).resolve()
    mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".opus": "audio/ogg"}.get(path.suffix.lower())
    if not mime or not path.is_file():
        raise ValueError(f"Missing or unsupported reference audio: {path}")
    if not 0 < path.stat().st_size <= profile.max_reference_bytes:
        raise ValueError("Reference file exceeds the configured size limit or is empty")
    duration = probe_duration(path)
    if duration is None or not 0 < duration <= profile.max_reference_seconds:
        raise ValueError(f"Reference duration must be within {profile.max_reference_seconds} seconds: {path}")
    return {"audio_path": str(path), "mime_type": mime, "duration_seconds": duration}


def next_speech(inputs, profile):
    legacy = {"voice_id", "enable_ssml", "ssml_voice_models", "rate", "pitch", "volume", "instruction"} & inputs.keys()
    if legacy:
        raise ValueError("Audio-generation protocol uses text_prompt and reference_paths; incompatible fields: " + ", ".join(sorted(legacy)))
    if "text" in inputs and "text_prompt" in inputs:
        raise ValueError("Provide text or text_prompt, not both")
    prompt = inputs.get("text_prompt", inputs.get("text", ""))
    if not prompt.strip() or len(prompt) > profile.max_prompt_length:
        raise ValueError("Empty prompt or configured prompt-length limit exceeded")
    reject_ssml(prompt)
    paths = inputs.get("reference_paths", [])
    if len(paths) > profile.max_references:
        raise ValueError("Too many reference recordings for the selected model")
    used = {int(x) for x in re.findall(r"@voice(\d+)\b", prompt)}
    if any(x < 1 or x > len(paths) for x in used):
        raise ValueError("Prompt refers to a missing reference slot")
    speech = {"text_prompt": prompt, "format": inputs.get("format", "wav"),
              "sample_rate": inputs.get("sample_rate", profile.sample_rate),
              "channels": inputs.get("channels", profile.channels)}
    if speech["format"] not in {"wav", "mp3", "pcm"}:
        raise ValueError("Audio-generation output must be wav, mp3 or pcm")
    metadata, references = [], []
    for index, path in enumerate(paths, 1):
        info = reference_info(path, profile)
        data = Path(info["audio_path"]).read_bytes()
        metadata.append({**info, "slot": f"@voice{index}", "sha256": hashlib.sha256(data).hexdigest()})
        references.append({"audio_data": f"data:{info['mime_type']};base64," + base64.b64encode(data).decode("ascii")})
    if references:
        speech["references"] = references
    return speech, metadata
