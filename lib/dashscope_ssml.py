"""Lossless source spans and the supported Aliyun SSML subset (no API calls)."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
import json
from xml.etree import ElementTree as ET

from lib.dashscope_config import speech_model

MODEL = speech_model(purpose="ssml").model
VOICE_LIBRARY = Path(__file__).resolve().parents[1] / "projects/voice-library/audio"
LABEL = re.compile(r"(?m)^[ \t]*([^：:\r\n]{1,80})[：:][ \t]*")


def native_synthesis_status(model: str, voice_ids: list[str]) -> dict:
    """Separate locally valid markup from a provider capability verified by listening."""
    if not speech_model(model).multi_voice_ssml and len(set(voice_ids)) > 1:
        return {
            "allowed": False, "status": "blocked_after_listening_failure",
            "reason": (
                f"Native multi-voice SSML is disabled for {model}: a single-request test "
                "was reported to use only the request-level voice for all roles. "
                "Per-speak voice switching is not verified. Local compilation and "
                "single-voice SSML remain available. No API request was sent."
            ),
        }
    return {"allowed": True, "status": "single_voice", "reason": None}


def resolve_voices(roles: dict, model: str, library: Path = VOICE_LIBRARY) -> dict:
    resolved, errors = {}, []
    for role, value in roles.items():
        try:
            if not isinstance(role, str) or not role.strip():
                raise ValueError("Role names must be nonempty strings")
            if isinstance(value, str):
                if not value or value in {".", ".."} or re.search(r"[\\/:]", value):
                    raise ValueError("Invalid library voice name")
                path = library / value / "voice.json"
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                if record.get("success") is not True or record.get("provider") != "dashscope":
                    raise ValueError("Library voice is not a successful DashScope enrollment")
                voice = {"name": value, "voice_id": record["data"]["voice_id"], "model": record["model"]}
            elif isinstance(value, dict):
                voice = dict(value)
            else:
                raise ValueError("Expected a library name or voice_id/model object")
            if voice.get("model") != model:
                raise ValueError(f"Voice model must be {model}")
            if not isinstance(voice.get("voice_id"), str) or not voice["voice_id"].strip():
                raise ValueError("Missing voice_id")
            if voice.get("provider", "dashscope") != "dashscope":
                raise ValueError("Voice provider must be dashscope")
            resolved[role] = voice
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(f"{role}: {exc}")
    if errors:
        raise ValueError("Invalid role mappings: " + "; ".join(errors))
    return resolved


def source_labels(source: str, roles: dict) -> list:
    matches = list(LABEL.finditer(source))
    unknown = sorted({m[1].strip() for m in matches if m[1].strip() not in roles})
    if unknown:
        raise ValueError("Unmapped or ambiguous role prefixes: " + ", ".join(unknown))
    return matches


def parse_labelled(source: str, roles: dict) -> list[dict]:
    matches = source_labels(source, roles)
    if not matches or source[:matches[0].start()].strip():
        raise ValueError("Unassigned text: provide AI-assigned segments with source spans, including narration")
    segments = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        # Preserve internal whitespace, including multiline dialogue.
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        segments.append({"role": match[1].strip(), "start": start, "end": end,
                         "text": source[start:end]})
    return segments


def number(value: Any, low: float, high: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def integer(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def validate_ssml(text: str, model: str, voice_models: dict | None = None) -> dict:
    """Validate sibling speak fragments; declarations/entities are never accepted."""
    if re.search(r"<!|<\?", text):
        raise ValueError("SSML declarations, entities, comments and processing instructions are unsupported")
    try:
        root = ET.fromstring("<fragments>" + text + "</fragments>")
    except ET.ParseError as exc:
        raise ValueError(f"Invalid SSML XML: {exc}") from None
    if not len(root) or (root.text or "").strip():
        raise ValueError("SSML must contain only speak fragments")
    voices, spoken = [], []
    for speak in root:
        if speak.tag != "speak" or (speak.tail or "").strip():
            raise ValueError("Only sibling speak fragments are supported")
        if set(speak.attrib) - {"voice", "rate", "pitch", "volume"}:
            raise ValueError("Unsupported speak attribute")
        voice = speak.get("voice", "")
        if not voice.strip():
            raise ValueError("Every speak must specify a voice ID")
        declared = (voice_models or {}).get(voice)
        if declared is None and voice_models is None and voice.startswith(model + "-"):
            declared = model
        if declared != model:
            raise ValueError(f"Voice {voice} must be mapped to model {model}")
        # Enrollment IDs encode their target model. Reject contradictory metadata.
        if voice.startswith(("qwen-", "cosyvoice-")) and not voice.startswith(model + "-"):
            raise ValueError(f"Voice {voice} belongs to a different model")
        for attr in ("rate", "pitch"):
            if attr in speak.attrib:
                number(speak.get(attr), 0.5, 2, attr)
        if "volume" in speak.attrib:
            val = speak.get("volume", "")
            if not re.fullmatch(r"\d+", val) or int(val) > 100:
                raise ValueError("volume must be an integer between 0 and 100")
        parts = [speak.text or ""]
        consecutive_ms = 0
        for child in speak:
            if child.tag != "break" or set(child.attrib) != {"time"} or len(child) or child.text:
                raise ValueError("Only empty break tags with a time attribute are supported inside speak")
            val = child.get("time", "")
            if re.fullmatch(r"\d+ms", val):
                ms = int(val[:-2])
                if not 50 <= ms <= 10000:
                    raise ValueError("break milliseconds must be between 50 and 10000")
            elif re.fullmatch(r"\d+s", val) and 1 <= int(val[:-1]) <= 10:
                ms = int(val[:-1]) * 1000
            else:
                raise ValueError("Invalid break time")
            consecutive_ms += ms
            if consecutive_ms > 10000:
                raise ValueError("Consecutive breaks exceed the platform's 10 second limit")
            parts.append(child.tail or "")
            if (child.tail or "").strip():
                consecutive_ms = 0
        if not "".join(parts).strip():
            raise ValueError("Each speak fragment must contain spoken text")
        spoken.extend(parts)
        if voice not in voices:
            voices.append(voice)
    return {"voice_ids": voices, "spoken_text": "".join(spoken)}


def compile_ssml(source: str, roles: dict, segments: list[dict] | None = None,
                 source_format: str | None = None, model: str | None = None,
                 library: Path = VOICE_LIBRARY) -> dict:
    profile = speech_model(model, purpose="ssml")
    if profile.protocol != "tts":
        raise ValueError("Use the dialogue compiler for audio-generation models")
    model = profile.model
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source_text must contain spoken content")
    if not roles:
        raise ValueError("At least one explicit role mapping is required")
    voices = resolve_voices(roles, model, library)
    source_format = source_format or ("labelled" if segments is None else "plain")
    if source_format not in {"plain", "labelled"}:
        raise ValueError("source_format must be plain or labelled")
    if segments is None:
        if source_format == "plain":
            raise ValueError("Plain manuscripts require AI-assigned source spans")
        segments = parse_labelled(source, voices)
    if not segments:
        raise ValueError("At least one segment is required")
    # Exact source slices are canonical; uncovered non-whitespace is always an error.
    covered = bytearray(len(source))
    if source_format == "labelled":
        for label in source_labels(source, voices):
            covered[label.start():label.end()] = b"\x01" * (label.end() - label.start())
    fragments, normalized, cursor = [], [], 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Each segment must be an object")
        role = segment.get("role")
        if role not in voices:
            raise ValueError(f"Unmapped or ambiguous role: {role}")
        start = integer(segment.get("start"), 0, len(source), "start")
        end = integer(segment.get("end"), start + 1, len(source), "end")
        if start < cursor or any(covered[start:end]):
            raise ValueError("Source spans overlap, are reordered, or include a role prefix")
        text = source[start:end]
        if not text.strip() or segment.get("text", text) != text:
            raise ValueError("Segment text must match the original source slice exactly")
        covered[start:end] = b"\x01" * (end - start)
        cursor = end
        attributes = {"voice": voices[role]["voice_id"]}
        for key in ("rate", "pitch"):
            if key in segment:
                attributes[key] = str(number(segment[key], 0.5, 2, key))
        if "volume" in segment:
            attributes["volume"] = str(integer(segment["volume"], 0, 100, "volume"))
        speak = ET.Element("speak", attributes)
        offset, last = 0, None
        for pause in segment.get("breaks", []):
            pos = integer(pause["offset"], offset, len(text), "break offset")
            duration = integer(pause["time_ms"], 50, 10000, "break time_ms")
            if last is None:
                speak.text = text[offset:pos]
            else:
                last.tail = text[offset:pos]
            last = ET.SubElement(speak, "break", {"time": f"{duration}ms"})
            offset = pos
        if last is None:
            speak.text = text
        else:
            last.tail = text[offset:]
        # XML normalizes literal CR/CRLF on parsing; references preserve source bytes.
        fragments.append(ET.tostring(speak, encoding="unicode", short_empty_elements=True).replace("\r", "&#13;"))
        normalized.append({**segment, "text": text, "voice_id": voices[role]["voice_id"]})
    missing = [i for i, char in enumerate(source) if not covered[i] and not char.isspace()]
    if missing:
        raise ValueError(f"Unassigned source text at offset {missing[0]}; no omissions allowed")
    ssml = "\n".join(fragments)
    voice_models = {v["voice_id"]: v["model"] for v in voices.values()}
    validation = validate_ssml(ssml, model, voice_models)
    if validation["spoken_text"] != "".join(s["text"] for s in normalized):
        raise ValueError("SSML round-trip changed the spoken text")
    return {"ssml": ssml, "model": model, "roles": voices, "segments": normalized,
            "validation": {"valid": True, "source_coverage": True,
                           "text_preserved": True, "voice_ids": validation["voice_ids"]},
            "synthesis_validation": native_synthesis_status(model, validation["voice_ids"]),
            "synthesis_inputs": {"text": ssml, "model": model,
                                 "voice_id": validation["voice_ids"][0],
                                 "enable_ssml": True, "ssml_voice_models": voice_models}}
