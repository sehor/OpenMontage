"""Conservative source/ASR matching. Unmatched words require review, never guessed timing."""
import re
from difflib import SequenceMatcher
from functools import lru_cache


@lru_cache(maxsize=1)
def chinese_converter():
    try:
        from opencc import OpenCC
        return OpenCC("t2s")
    except ImportError:
        return None


def clean(text):
    return "".join(c.lower() for c in text if c.isalnum())


def normalized(text, spans):
    """Match Arabic and Chinese numeric spellings, retaining real observed spans."""
    converter = chinese_converter()
    if converter:
        converted, converted_spans = "", []
        for character, span in zip(text, spans):
            value = converter.convert(character)
            converted += value
            converted_spans.extend([span] * len(value))
        text, spans = converted, converted_spans
    digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
    result, owners = "", []
    pattern = r"[0-9零〇一二两三四五六七八九十百千万亿]+|."
    for match in re.finditer(pattern, text, re.DOTALL):
        token = match.group()
        if all(c in digits or c in units for c in token):
            if not any(c in units for c in token):
                token = "".join(str(digits[c]) for c in token)
            else:
                total = section = number = 0
                for c in token:
                    if c in digits:
                        number = digits[c]
                    elif units[c] < 10000:
                        section += (number or 1) * units[c]
                        number = 0
                    else:
                        section = (section + number) * units[c]
                        total += section
                        section = number = 0
                token = str(total + section + number)
        else:
            token = clean(token)
        if token:
            a, b = match.span()
            span = (spans[a][0], spans[b-1][1])
            result += token.lower()
            owners.extend([span] * len(token))
    return result, owners


def align_chunk(chunk, transcript, duration):
    observed, times = "", []
    last = 0
    for segment in transcript:
        for word in segment.get("words", []):
            a, b = word["start"], word["end"]
            if not 0 <= a <= b <= duration + 0.001 or a < last - 0.001:
                raise ValueError("Invalid ASR word timestamps")
            token = word["word"]
            observed += token
            times.extend([(a, b)] * len(token))
            last = b
    expected, owners = "", []
    for mapping in chunk["reading_map"]:
        token = mapping["reading"]
        expected += token
        owners.extend([(mapping["start"], mapping["end"])] * len(token))
    observed, times = normalized(observed, times)
    expected, owners = normalized(expected, owners)
    matcher = SequenceMatcher(None, expected, observed, autojunk=False)
    matched = {}
    for block in matcher.get_matching_blocks():
        for j in range(block.size):
            matched[block.a + j] = times[block.b + j]
    # Require every spoken character: a missing sentence must not silently pass.
    if not expected or len(matched) != len(expected) or matcher.ratio() < 0.95:
        raise ValueError("needs_review: ASR does not reliably match the manuscript; inspect transcript and reading_map")
    text = chunk["text"]
    cues, cursor = [], 0
    while cursor < len(text):
        end = min(cursor + 48, len(text))
        boundaries = [m.end() + cursor for m in re.finditer(r"[。！？!?；;\n]", text[cursor:end])]
        if boundaries:
            end = boundaries[0]
        elif end < len(text):
            minor = [m.end() + cursor for m in re.finditer(r"[，,、：:]", text[cursor:end])]
            if minor:
                end = minor[-1]
        # Keep a pronunciation alias atomic.
        for item in chunk["reading_map"]:
            if item["start"] < end < item["end"]:
                end = item["start"] if item["start"] > cursor else item["end"]
                break
        indexes = [i for i, (a, b) in enumerate(owners) if a < end and b > cursor]
        if indexes:
            a, b = matched[indexes[0]][0], matched[indexes[-1]][1]
            if cues and a < cues[-1]["end"]:
                raise ValueError("needs_review: sentence boundary falls inside an ASR word")
            display = text[cursor:end].strip()
            if len(display) > 48:
                raise ValueError("needs_review: pronunciation element exceeds caption length")
            cues.append({"text": display[:24] + ("\n" + display[24:] if len(display) > 24 else ""),
                         "start": a, "end": b, "role": chunk["role"]})
        cursor = end
    if not cues:
        raise ValueError("needs_review: no aligned captions")
    return cues
