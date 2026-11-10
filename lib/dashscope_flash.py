"""Flash dialogue compilation: one voice per request, lossless source spans."""
from copy import deepcopy
from xml.etree import ElementTree as ET
import re

from lib.dashscope_config import speech_model
from lib.dashscope_ssml import compile_ssml, validate_ssml


def units(speak):
    """Atomic markup units; never split pronunciation elements."""
    result = [(c, c) for c in speak.text or ""]
    for child in speak:
        node = deepcopy(child)
        node.tail = None
        display = child.text or ""
        result.append((ET.tostring(node, encoding="unicode"), display))
        result.extend((c, c) for c in child.tail or "")
    return result


def render(items, attrs):
    from xml.sax.saxutils import escape, quoteattr
    body = "".join(markup if markup.startswith("<") and len(markup) > 1 else escape(markup)
                   for markup, _ in items)
    attributes = "".join(f" {key}={quoteattr(value)}" for key, value in attrs.items())
    return (f"<speak{attributes}>{body}</speak>").replace("\r", "&#13;")


def compile_flash(source, roles, segments, source_format, model, library):
    profile = speech_model(model)
    roles = deepcopy(roles)
    for value in roles.values():
        if isinstance(value, dict) and "model" in value:
            value["model"] = speech_model(value["model"]).model
    compiled = compile_ssml(source, roles, segments, source_format, model, library, segmented=True)
    chunks = []
    for turn, segment in enumerate(compiled["segments"]):
        speak = ET.fromstring(segment["ssml"])
        gap = segment.get("gap_after_ms")
        if gap is not None:
            from lib.dashscope_ssml import number
            number(gap, 0, 10000, "gap_after_ms")
        voice = compiled["roles"][segment["role"]]
        request_attrs = speak.attrib
        original = segments[turn] if segments is not None else {}
        annotated = any(k in original for k in ("ssml", "breaks", "rate", "pitch", "volume"))
        use_ssml = voice.get("ssml_supported") is True
        if annotated and not use_ssml:
            raise ValueError(f"SSML support must be confirmed for {segment['role']}")
        remaining = units(speak)
        offset = segment["start"]
        while remaining:
            # Count serialized markup, conservatively including the wrapper.
            size, cuts = 0, []
            for i, item in enumerate(remaining):
                size += len(render([item], request_attrs)) - len(render([], request_attrs)) if use_ssml else len(item[1])
                if size + (len(render([], request_attrs)) if use_ssml else 0) > profile.max_prompt_length:
                    break
                if re.search(r"[。！？!?；;\n]$", item[1]):
                    cuts.append(i + 1)
            else:
                i = len(remaining)
            if i == len(remaining):
                count = i
            else:
                if not cuts:
                    raise ValueError("Oversized sentence or SSML element; split at a reviewed sentence boundary")
                count = cuts[-1]
            batch, remaining = remaining[:count], remaining[count:]
            text = "".join(x[1] for x in batch)
            if not text.strip():
                raise ValueError("Split would create an empty speech chunk")
            synthesis = {"model": model, "voice_id": voice["voice_id"], "format": "wav", "sample_rate": 24000,
                         "text": render(batch, request_attrs) if use_ssml else text}
            if use_ssml:
                synthesis.update(enable_ssml=True, ssml_voice_models={voice["voice_id"]: model})
                validate_ssml(synthesis["text"], model, synthesis["ssml_voice_models"],
                              request_voice=synthesis["voice_id"])
            instruction = "；".join(segment[k] for k in ("direction", "emotion") if segment.get(k))
            if instruction:
                synthesis["instruction"] = instruction
            chunks.append({"turn": turn, "role": segment["role"], "start": offset, "end": offset + len(text),
                           "text": text, "synthesis_inputs": synthesis,
                           "gap_after_ms": segment.get("gap_after_ms")})
            offset += len(text)
    return {**compiled, "protocol": "tts", "mode": "segmented", "chunks": chunks,
            "synthesis_validation": {"allowed": True, "status": "single_voice_per_chunk"},
            "synthesis_inputs": None}
