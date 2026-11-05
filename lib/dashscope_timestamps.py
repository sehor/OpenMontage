"""Build captions directly from finalized provider sentences and timestamps."""
import math


def provider_cues(capture, duration):
    if capture.get("time_base") != "request" or capture.get("unit") != "milliseconds":
        raise ValueError("needs_review: unsupported provider timestamp time base")
    # Synthesis events repeat/extend word lists. Use only finalized snapshots.
    texts, sentences, seen = {}, {}, set()
    for event in capture.get("sentences", []):
        sentence = event["sentence"]
        index = sentence["index"]
        if type(index) is not int or index < 0:
            raise ValueError("needs_review: invalid provider sentence index")
        seen.add(index)
        text = event.get("original_text")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("needs_review: invalid provider sentence text")
            if text.strip():
                texts[index] = text
        if event["type"] == "sentence-end":
            sentences[index] = sentence
    if not sentences or sorted(sentences) != list(range(len(sentences))) or seen != set(sentences):
        raise ValueError("needs_review: missing finalized provider sentences")
    last, cues = 0, []
    for index, sentence in sorted(sentences.items()):
        words = sentence.get("words", [])
        if not words:
            raise ValueError("needs_review: provider returned no word timestamps")
        for word in words:
            a, b = word["begin_time"], word["end_time"]
            if (any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (a, b))
                    or not 0 <= a < b <= duration * 1000 + 1 or a < last):
                raise ValueError("needs_review: invalid or sentence-relative provider timestamps")
            if not isinstance(word["text"], str):
                raise ValueError("needs_review: invalid provider word text")
            last = b
        text = texts.get(index)
        source = "original_text" if text is not None else "words"
        if text is None:
            text = "".join(word["text"] for word in words)
        if not text.strip():
            raise ValueError("needs_review: provider returned no caption text")
        cue = {"text": text.strip(), "start": words[0]["begin_time"] / 1000,
               "end": words[-1]["end_time"] / 1000, "text_source": source,
               "timing_source": "dashscope_sse", "provider_sentence": index}
        if source == "words":
            cue["limitation"] = "Provider original_text missing; caption uses returned word text"
        cues.append(cue)
    return cues
