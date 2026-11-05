"""Verify output timing/streams and preserved audio against the imported source."""
import argparse
import array
import math
import subprocess
from pathlib import Path

from assemble import load_snapshot, probe, read_json, write_json


def pcm(path, start, duration):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
                             "-t", str(duration), "-vn", "-ac", "1", "-ar", "8000",
                             "-f", "s16le", "-"], capture_output=True, timeout=60, check=True)
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


def correlation(left, right):
    best = (-1, 0)
    # Resolve sub-frame AAC/container delay, inspecting up to +/- 30 ms.
    for lag in range(-240, 241):
        pairs = [(left[i], right[i + lag]) for i in range(800, min(len(left), len(right)) - 800, 41)]
        xy = sum(a * b for a, b in pairs)
        denom = math.sqrt(sum(a * a for a, _ in pairs) * sum(b * b for _, b in pairs))
        score = xy / denom if denom else 0
        if score > best[0]:
            best = score, lag
    return {"correlation": round(best[0], 6), "delayMs": best[1] / 8}


def verify(output):
    output = Path(output).resolve()
    snapshot = load_snapshot(output)
    ep, clips = snapshot["episode"], snapshot["clips"]
    request = read_json(output / "artifacts/compose-request.json")
    target = Path(request["output_path"])
    media = probe(target)
    timeline = read_json(output / "artifacts/timeline.json")
    if (media["width"], media["height"], media["fps"]) != (ep["width"], ep["height"], ep["fps"]):
        raise ValueError("Output dimensions/frame rate differ from episode")
    if abs(media["duration"] - timeline["duration"]) > max(0.05, 2 / ep["fps"]):
        raise ValueError("Output duration differs from timeline")
    lookup = {a["id"]: a for a in snapshot["assets"]}
    evidence = []
    for clip, chapter in zip(clips, timeline["chapters"], strict=True):
        if clip["audio"] == "silent":
            continue
        duration = clip["out"] - clip["in"]
        original = pcm(output / lookup[clip["assetId"]]["path"], clip["in"], duration)
        rendered = pcm(target, chapter["start"], duration)
        match = correlation(original, rendered)
        if match["correlation"] < 0.97 or abs(match["delayMs"]) > 1000 / ep["fps"]:
            raise ValueError(f"Audio mismatch: {clip['id']}: {match}")
        evidence.append({"clipId": clip["id"], **match})
    report = {"passed": True, "modelCount": len(clips), "media": media,
              "audioPreservation": evidence, "sourceHashesVerified": True,
              "limitations": ["Audio comparison measures waveform preservation, not subjective listening.",
                              "Historical preview only; this does not certify benchmark facts."]}
    write_json(output / "artifacts/media-verification.json", report)
    return report


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    print(json.dumps(verify(parser.parse_args().output), ensure_ascii=False))
