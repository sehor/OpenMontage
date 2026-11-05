"""Local manuscript-to-reference-dialogue tool; model selection is configuration-driven."""
from copy import deepcopy
import json
from pathlib import Path

import jsonschema

from lib.dashscope_dialogue import compile_dialogue
from tools.audio.dashscope_ssml import DashScopeSSML
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier


class DashScopeDialogue(BaseTool):
    name = "dashscope_dialogue"
    capability = "dialogue_compile"
    provider = "dashscope"
    runtime = ToolRuntime.LOCAL
    tier = ToolTier.VOICE
    agent_skills = ["qwen-tts"]
    capabilities = ["role_mapping", "source_preservation", "reference_audio", "dialogue_prompt"]
    input_schema = deepcopy(DashScopeSSML.input_schema)
    input_schema["properties"]["roles"]["additionalProperties"] = {
        "oneOf": [{"type": "string", "minLength": 1}, {"type": "object"}]}
    side_effects = ["writes source, dialogue prompt, role mappings and validation artifacts"]

    def get_status(self):
        return ToolStatus.AVAILABLE

    def execute(self, inputs):
        try:
            jsonschema.validate(inputs, self.input_schema)
            result = compile_dialogue(inputs["source_text"], inputs["roles"], inputs.get("segments"),
                                      inputs.get("source_format"), inputs.get("model"))
            folder = Path(inputs["project_dir"]).resolve()/"artifacts"
            name = inputs.get("artifact_name", "dialogue")
            if (folder/f"{name}.synthesis.json").exists():
                raise ValueError("Synthesis receipt exists; use a new artifact_name")
            folder.mkdir(parents=True, exist_ok=True)
            files = [folder/f"{name}.source.txt", folder/f"{name}.prompt.txt", folder/f"{name}.json"]
            files[0].write_text(inputs["source_text"], encoding="utf-8", newline="")
            files[1].write_text(result.get("text_prompt", json.dumps(result.get("chunks", []), ensure_ascii=False, indent=2)), encoding="utf-8")
            files[2].write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return ToolResult(success=True, model=result["model"], data=result, artifacts=[str(p) for p in files])
        except (ValueError, KeyError, TypeError, OSError, jsonschema.ValidationError) as exc:
            return ToolResult(success=False, error=f"Dialogue compilation failed: {exc}")
