"""Local role-to-SSML compiler. Understanding unlabelled prose stays with the agent."""
from pathlib import Path
import json

import jsonschema

from lib.dashscope_ssml import MODEL, compile_ssml
from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStatus, ToolTier


class DashScopeSSML(BaseTool):
    name = "dashscope_ssml"
    capability = "ssml_compile"
    provider = "dashscope"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.LOCAL
    agent_skills = ["qwen-tts"]
    dependencies = []
    capabilities = ["role_mapping", "source_preservation", "ssml_validation"]
    side_effects = ["writes manuscript, SSML, role mappings and validation artifacts"]
    input_schema = {
        "type": "object", "required": ["source_text", "roles", "project_dir"],
        "additionalProperties": False,
        "properties": {
            "source_text": {"type": "string", "minLength": 1},
            "roles": {"type": "object", "minProperties": 1,
                      "additionalProperties": {"oneOf": [
                          {"type": "string", "minLength": 1},
                          {"type": "object", "required": ["voice_id", "model"],
                           "properties": {"voice_id": {"type": "string", "minLength": 1},
                                          "model": {"type": "string"}}}]}},
            "segments": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["role", "start", "end"],
                "additionalProperties": False,
                "properties": {
                    "role": {"type": "string", "minLength": 1},
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 1},
                    "text": {"type": "string"},
                    "rate": {"type": "number", "minimum": 0.5, "maximum": 2},
                    "pitch": {"type": "number", "minimum": 0.5, "maximum": 2},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                    "emotion": {"type": "string"}, "direction": {"type": "string"},
                    "breaks": {"type": "array", "items": {
                        "type": "object", "required": ["offset", "time_ms"],
                        "additionalProperties": False,
                        "properties": {"offset": {"type": "integer", "minimum": 0},
                                       "time_ms": {"type": "integer", "minimum": 50, "maximum": 10000}}}}}}},
            "source_format": {"enum": ["plain", "labelled"]},
            "model": {"type": "string", "minLength": 1},
            "project_dir": {"type": "string", "minLength": 1},
            "artifact_name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
        },
    }

    def get_status(self):
        return ToolStatus.AVAILABLE

    def execute(self, inputs):
        try:
            jsonschema.validate(inputs, self.input_schema)
            result = compile_ssml(inputs["source_text"], inputs["roles"], inputs.get("segments"),
                                  inputs.get("source_format"), inputs.get("model"))
            folder = Path(inputs["project_dir"]).resolve() / "artifacts"
            folder.mkdir(parents=True, exist_ok=True)
            name = inputs.get("artifact_name", "dialogue")
            if (folder / f"{name}.synthesis.json").exists():
                raise ValueError("A synthesis receipt already references these artifacts; choose a new artifact_name")
            files = [folder / f"{name}.source.txt", folder / f"{name}.ssml",
                     folder / f"{name}.json"]
            files[0].write_text(inputs["source_text"], encoding="utf-8", newline="")
            files[1].write_text(result["ssml"], encoding="utf-8")
            files[2].write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return ToolResult(success=True, model=result["model"], artifacts=[str(f) for f in files],
                              data=result)
        except (ValueError, KeyError, TypeError, OSError, jsonschema.ValidationError) as exc:
            return ToolResult(success=False, error=f"SSML compilation failed: {exc}")
