"""Probe Gemini model access or create one explicitly requested smoke sample.

Run from the repository root: uv run --python .venv/Scripts/python.exe
python -m examples.gemini_speech [--sample]
"""
import argparse
import json
from pathlib import Path

from lib.gemini_speech import GeminiSpeechClient, GeminiSpeechError
from tools.cost_tracker import CostTracker
from tools.tool_registry import registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help="Generate one paid official-voice sample (user-authorized).")
    args = parser.parse_args()
    registry.discover("tools.audio")
    client = GeminiSpeechClient()
    try:
        model, _ = client.request("GET", f"models/{client.settings.model}")
        print(json.dumps({"model_access": True, "model": model.get("name"),
                          "replication_entitlement": "not verified without genuine consent recordings"}))
    except GeminiSpeechError as exc:
        print(json.dumps({"model_access": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if not args.sample:
        return 0
    output = Path("scratch/diagnostics/gemini-speech")
    output.mkdir(parents=True, exist_ok=True)
    tool = registry.get("gemini_tts")
    inputs = {"text": "你好，这是谷歌语音合成测试。我们正在检查中文发音、自然停顿，以及声音表达是否清晰流畅。",
              "speech_style": "自然、温暖，以正常语速朗读普通话。", "output_path": str(output / "official-sample.wav")}
    budget = CostTracker(cost_log_path=output / "cost-log.json")
    budget.approve_tool(tool.name)  # --sample represents explicit paid-smoke-test authorization.
    entry = budget.estimate(tool.name, "single official voice smoke sample", tool.estimate_cost(inputs))
    budget.reserve(entry)
    result = tool.execute(inputs)
    # No actual billing quote is returned. Keep the reservation, not a false $0 reconciliation.
    print(json.dumps({"success": result.success, "error": result.error, "data": result.data,
                      "artifacts": result.artifacts, "billing": "unknown; conservative reservation retained"}, ensure_ascii=False))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
