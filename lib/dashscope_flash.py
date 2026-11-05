"""Flash dialogue compilation: one voice per request, lossless source spans."""
from copy import deepcopy
from xml.etree import ElementTree as ET
import re

from lib.dashscope_config import speech_model
from lib.dashscope_ssml import compile_ssml, validate_ssml


def units(speak):
    """Atomic markup units; never split pronunciation elements."""
    result = [(c, c, c) for c in speak.text or ""]
    for child in speak:
        node = deepcopy(child)
        node.tail = None
        display = child.text or ""
        reading = child.get("alias", display)
        result.append((ET.tostring(node, encoding="unicode"), display, reading))
        result.extend((c, c, c) for c in child.tail or "")
    return result


def render(items, attrs):
    from xml.sax.saxutils import escape, quoteattr
    body = "".join(markup if markup.startswith("<") and len(markup) > 1 else escape(markup)
                   for markup, _, _ in items)
    return ("<speak " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + ">" + body + "</speak>").replace("\r", "&#13;")


def compile_flash(source, roles, segments, source_format, model, library):
    profile = speech_model(model)
    roles = deepcopy(roles)
    for value in roles.values():
        if isinstance(value, dict) and "model" in value:
            value["model"] = speech_model(value["model"]).model
    compiled = compile_ssml(source, roles, segments, source_format, model, library)
    fragments = ET.fromstring("<root>" + compiled["ssml"] + "</root>")
    chunks = []
    for turn, (segment, speak) in enumerate(zip(compiled["segments"], fragments)):
        gap = segment.get("gap_after_ms")
        if gap is not None:
            from lib.dashscope_ssml import number
            number(gap, 0, 10000, "gap_after_ms")
        reading_end = 0
        for override in segment.get("readings", []):
            if override["start"] < reading_end:
                raise ValueError("Reading overrides must be ordered and nonoverlapping")
            reading_end = override["end"]
        voice = compiled["roles"][segment["role"]]
        annotated = any(k in segment for k in ("ssml", "breaks", "rate", "pitch", "volume"))
        use_ssml = voice.get("ssml_supported") is True
        if annotated and not use_ssml:
            raise ValueError(f"SSML support must be confirmed for {segment['role']}")
        remaining = units(speak)
        offset = segment["start"]
        while remaining:
            # Count serialized markup, conservatively including the wrapper.
            size, cuts = 0, []
            for i, item in enumerate(remaining):
                size += len(render([item], speak.attrib)) - len(render([], speak.attrib)) if use_ssml else len(item[1])
                if size + (len(render([], speak.attrib)) if use_ssml else 0) > profile.max_prompt_length:
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
                         "text": render(batch, speak.attrib) if use_ssml else text}
            if use_ssml:
                synthesis.update(enable_ssml=True, ssml_voice_models={voice["voice_id"]: model})
                validate_ssml(synthesis["text"], model, synthesis["ssml_voice_models"])
            instruction = "；".join(segment[k] for k in ("direction", "emotion") if segment.get(k))
            if instruction:
                synthesis["instruction"] = instruction
            mapping, local = [], 0
            for _, display, reading in batch:
                if display:
                    mapping.append({"start": local, "end": local + len(display), "display": display,
                                    "reading": reading})
                    local += len(display)
            # Reviewed pronunciation overrides handle expanded numeric/date readings.
            for override in segment.get("readings", []):
                a, b = override["start"], override["end"]
                if not isinstance(a, int) or not isinstance(b, int) or not 0 <= a < b <= len(segment["text"]):
                    raise ValueError("Invalid reading override span")
                a, b = a + segment["start"] - offset, b + segment["start"] - offset
                if b <= 0 or a >= len(text):
                    continue
                if a < 0 or b > len(text) or not override["reading"].strip():
                    raise ValueError("Reading override crosses a chunk boundary")
                if any(x["start"] < a < x["end"] or x["start"] < b < x["end"] for x in mapping):
                    raise ValueError("Reading override splits an atomic pronunciation tag")
                mapping = [x for x in mapping if x["end"] <= a or x["start"] >= b] + [
                    {"start": a, "end": b, "display": text[a:b], "reading": override["reading"]}]
                mapping.sort(key=lambda x: x["start"])
            chunks.append({"turn": turn, "role": segment["role"], "start": offset, "end": offset + len(text),
                           "text": text, "reading_map": mapping, "synthesis_inputs": synthesis,
                           "gap_after_ms": segment.get("gap_after_ms")})
            offset += len(text)
    return {**compiled, "protocol": "tts", "mode": "segmented", "chunks": chunks,
            "synthesis_validation": {"allowed": True, "status": "single_voice_per_chunk"},
            "synthesis_inputs": None}
