"""Check that supplied radar clips are tied to complete 0–100 score data.

This validates upstream data; it never calculates or fills in scores. A passing
data audit still requires frame inspection before visualEvidenceConfirmed is set.
"""
import argparse
import json
import math
from pathlib import Path

from assemble import digest, read_json, write_json


def findings(data, clips, data_hash):
    issues = []
    axes = [axis["key"] for axis in data.get("axes", [])]
    rows = data.get("models", [])
    models = {model["id"]: model for model in rows}
    if len(models) != len(rows):
        issues.append("Duplicate model IDs in score dataset")
    if len(axes) != 6 or len(set(axes)) != 6:
        issues.append("Score dataset must define six distinct axes")
    for clip in clips:
        model_id = clip["modelId"]
        model = models.get(model_id)
        if model is None:
            issues.append(f"{model_id}: missing from score dataset")
            continue
        scores = model.get("scores", [])
        if len(scores) != 6 or any(value is None for value in scores):
            issues.append(f"{model_id}: six complete scores unavailable; do not fill missing values")
        elif any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                 not math.isfinite(value) or not 0 <= value <= 100 for value in scores):
            issues.append(f"{model_id}: scores must be finite numbers in 0–100")
        if clip.get("axes") != axes:
            issues.append(f"{model_id}: video axes do not match the score dataset")
        if clip.get("dataVersion") != data.get("scaleVersion"):
            issues.append(f"{model_id}: video scale version does not match the score dataset")
        if clip.get("scoreDataSha256") != data_hash:
            issues.append(f"{model_id}: no matching upstream score-file fingerprint for this video")
    return issues


def audit(data_path, output):
    data_path, output = Path(data_path).resolve(), Path(output).resolve()
    snapshot_path = output / "input/snapshot.json"
    snapshot, data = read_json(snapshot_path), read_json(data_path)
    checksum = digest(data_path)
    issues = findings(data, snapshot["clips"], checksum)
    report = {"status": "blocked" if issues else "data_validated", "issues": issues,
              "scoreDataPath": str(data_path), "scoreDataSha256": checksum,
              "snapshotSha256": digest(snapshot_path), "visualEvidenceConfirmed": False,
              "selectedModels": [clip["modelId"] for clip in snapshot["clips"]],
              "scores": {m["id"]: m.get("scores") for m in data.get("models", [])
                         if m["id"] in {c["modelId"] for c in snapshot["clips"]}},
              "nextStep": "Obtain matching upstream clips and inspect actual frame labels/numbers against scores. Do not use raw speed or price as radar scores."}
    write_json(output / "artifacts/score-handoff-audit.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit(args.data, args.output)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result["issues"] else 0)
