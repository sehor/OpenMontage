"""Registered Flash dialogue synthesis/assembly tool."""
import jsonschema
import subprocess
import wave

from lib.dashscope_flash_render import render_flash
from tools.base_tool import BaseTool, ToolResult, ToolTier, ToolStatus, ToolRuntime, RetryPolicy, ResumeSupport


class DashScopeDialogueRender(BaseTool):
    name = "dashscope_dialogue_render"
    capability = "dialogue_render"
    provider = "dashscope"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.API
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    agent_skills = ["qwen-tts"]
    dependencies = ["python:faster_whisper"]
    capabilities = ["segmented_synthesis", "sentence_subtitles", "audio_assembly", "resume"]
    side_effects = ["calls paid TTS API", "writes audio, captions, timeline and receipts"]
    input_schema = {"type": "object", "required": ["plan", "output_dir"], "additionalProperties": False,
                    "properties": {"plan": {"type": "object"}, "output_dir": {"type": "string"},
                                   "gap_ms": {"type": "number", "minimum": 0, "maximum": 10000},
                                   "model_size": {"enum": ["tiny", "base", "small", "medium", "large-v2", "large-v3"]},
                                   "language": {"type": "string", "pattern": "^[a-z]{2,3}$"}}}

    def get_status(self):
        return ToolStatus.AVAILABLE

    def execute(self, inputs):
        try:
            jsonschema.validate(inputs, self.input_schema)
            plan = inputs["plan"]
            if plan.get("mode") != "segmented" or not plan.get("chunks"):
                raise ValueError("Expected a compiled Flash segmented plan")
            from tools.tool_registry import registry
            registry.discover("tools.audio")
            registry.discover("tools.analysis")
            registry.discover("tools.subtitle")
            result = render_flash(plan, inputs["output_dir"], registry.get("dashscope_tts"),
                                  registry.get("transcriber"), registry.get("subtitle_gen"),
                                  inputs.get("gap_ms", 300), inputs.get("model_size", "small"),
                                  inputs.get("language", "zh"))
            return ToolResult(success=True, data=result, artifacts=result["artifacts"], model=plan["model"])
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, wave.Error, subprocess.SubprocessError,
                jsonschema.ValidationError) as exc:
            return ToolResult(success=False, error=f"Flash dialogue render stopped: {exc}")
