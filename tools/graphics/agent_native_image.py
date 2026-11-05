"""Agent Native Image Provider - Virtual tool to route generation to the executing Agent."""

from __future__ import annotations

from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class AgentNativeImage(BaseTool):
    name = "agent_native_image"
    version = "1.0.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "agent_native"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = "No installation required. Native to the executing Agent."
    agent_skills = []

    capabilities = ["generate_image", "generate_illustration", "text_to_image"]
    supports = {
        "negative_prompt": True,
        "seed": False,
        "custom_size": True,
    }
    best_for = [
        "free local generation",
        "zero-setup image generation",
        "agent-native capabilities",
        "default image provider",
    ]
    not_good_for = ["strict seed reproduction"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "negative_prompt": {"type": "string", "default": ""},
            "width": {"type": "integer", "default": 1024},
            "height": {"type": "integer", "default": 1024},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=0, ram_mb=0, vram_mb=0, disk_mb=0, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["prompt", "width", "height"]
    side_effects = []
    user_visible_verification = ["Inspect generated image"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.00

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        # Instead of executing an API request, we instruct the Agent to do it itself.
        prompt = inputs.get("prompt", "")
        output_path = inputs.get("output_path", "assets/images/generated.png")

        instruction = (
            f"VIRTUAL TOOL INTERCEPT: Do not use this Python script to generate images. "
            f"Instead, use your Agent's native `generate_image` tool directly with the prompt: '{prompt}'. "
            f"Save the generated image to '{output_path}'."
        )

        return ToolResult(
            success=True,
            data={
                "provider": "agent_native",
                "instruction": instruction,
                "output": str(output_path),
            },
            cost_usd=0.00,
            duration_seconds=0.0,
        )
