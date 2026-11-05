"""Resolve model capabilities from configuration; no model-ID decisions in code."""
import os
from pathlib import Path

from lib.config_model import OpenMontageConfig, SpeechModelConfig


def speech_model(selection: str | None = None, purpose: str = "synthesis") -> SpeechModelConfig:
    config_path = os.environ.get("OPENMONTAGE_CONFIG")
    settings = OpenMontageConfig.load(Path(config_path) if config_path else None).audio.dashscope
    if selection is None:
        if purpose == "synthesis":
            selection = os.environ.get("DASHSCOPE_MODEL") or settings.default_model
        elif purpose == "ssml":
            selection = settings.ssml_model
        elif purpose == "enrollment":
            selection = settings.enrollment_model
        else:
            raise ValueError(f"Unknown speech purpose: {purpose}")
    if selection in settings.models:
        return settings.models[selection]
    for profile in settings.models.values():
        if profile.model == selection:
            return profile
    raise ValueError(f"Speech model {selection!r} is not configured under audio.dashscope.models")
