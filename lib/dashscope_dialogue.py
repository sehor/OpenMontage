"""Compile reviewed source spans into reference-conditioned dialogue prompts."""
import json
from pathlib import Path

from lib.dashscope_config import speech_model
from lib.dashscope_ssml import VOICE_LIBRARY, integer, parse_labelled, source_labels
from lib.dashscope_references import reference_info, reject_ssml


def compile_dialogue(source, roles, segments=None, source_format=None, model=None, library=VOICE_LIBRARY):
    profile = speech_model(model)
    if profile.protocol != "audio_generation":
        from lib.dashscope_flash import compile_flash
        return compile_flash(source, roles, segments, source_format, profile.model, library)
    if not source.strip() or not roles:
        raise ValueError("source_text and explicit role mappings are required")
    reject_ssml(source)
    source_format = source_format or ("labelled" if segments is None else "plain")
    if source_format not in {"plain", "labelled"}:
        raise ValueError("source_format must be plain or labelled")
    if segments is None:
        if source_format != "labelled":
            raise ValueError("Unlabelled manuscripts require AI-assigned source spans")
        segments = parse_labelled(source, roles)
    covered = bytearray(len(source))
    if source_format == "labelled":
        for match in source_labels(source, roles):
            covered[match.start():match.end()] = b"\x01" * (match.end()-match.start())
    normalized, cursor = [], 0
    for segment in segments:
        role = segment.get("role")
        if role not in roles:
            raise ValueError(f"Unmapped role: {role}")
        start = integer(segment.get("start"), 0, len(source), "start")
        end = integer(segment.get("end"), start+1, len(source), "end")
        if start < cursor or any(covered[start:end]):
            raise ValueError("Reordered/overlapping source spans or role prefix in spoken text")
        text = source[start:end]
        if not text.strip() or segment.get("text", text) != text:
            raise ValueError("Spoken text must match the original source slice exactly")
        covered[start:end] = b"\x01"*(end-start)
        normalized.append({**segment, "text": text})
        cursor = end
    if not normalized or any(not covered[i] and not ch.isspace() for i,ch in enumerate(source)):
        raise ValueError("Unassigned source text; no omissions allowed")
    resolved, paths = {}, []
    for segment in normalized:
        role = segment["role"]
        if role in resolved:
            continue
        value = roles[role]
        if isinstance(value, str):
            if value in {".", ".."} or any(c in value for c in '/\\:'):
                raise ValueError("Invalid library voice name")
            record = json.loads((Path(library)/value/"voice.json").read_text(encoding="utf-8-sig"))
            path = record.get("source_path")
        elif isinstance(value, dict):
            record = value
            path = record.get("audio_path", record.get("source_path"))
        else:
            raise ValueError(f"Invalid reference mapping for {role}")
        if not path:
            raise ValueError(f"{role} needs original reference audio; enrolled voice IDs cannot be used by this protocol")
        info = reference_info(path, profile)
        path = info["audio_path"]
        if path not in paths:
            paths.append(path)
        if len(paths) > profile.max_references:
            raise ValueError("Too many distinct reference recordings for the configured model")
        resolved[role] = {**info, "slot": f"@voice{paths.index(path)+1}",
                          "origin_enrollment_model": record.get("model")}
    lines = ["普通话自然对话，只使用参考录音对应的说话人声音，保持每位说话人的音色一致。"
             "只朗读每段台词字段的原文，不读角色名或表演提示。只有干净人声，没有音乐和环境音。"]
    for segment in normalized:
        notes = [segment[k] for k in ("emotion", "direction") if segment.get(k)]
        if "rate" in segment:
            notes.append(f"语速约为正常的{segment['rate']}倍")
        if "pitch" in segment or "volume" in segment:
            raise ValueError("Use natural-language direction for pitch/volume with audio generation")
        for pause in segment.get("breaks", []):
            position = integer(pause["offset"], 0, len(segment["text"]), "break offset")
            duration = integer(pause["time_ms"], 50, 10000, "break time_ms")
            notes.append(f"在台词第{position}个字符后自然停顿约{duration}毫秒")
        slot = resolved[segment["role"]]["slot"]
        lines.append(f"{slot} 表演提示：{json.dumps('；'.join(notes) or '自然说话', ensure_ascii=False)}；台词：{json.dumps(segment['text'], ensure_ascii=False)}")
        segment["reference_slot"] = slot
    prompt = "\n".join(lines)
    if len(prompt) > profile.max_prompt_length:
        raise ValueError(f"Compiled prompt has {len(prompt)} characters, exceeding the configured limit of {profile.max_prompt_length}; split the manuscript explicitly")
    return {"model": profile.model, "protocol": profile.protocol, "text_prompt": prompt,
            "roles": resolved, "segments": normalized,
            "validation": {"valid": True, "source_coverage": True, "text_preserved": True,
                           "prompt_characters": len(prompt), "max_prompt_characters": profile.max_prompt_length},
            "synthesis_validation": {"allowed": True, "status": "reference_conditioned_dialogue"},
            "synthesis_inputs": {"model": profile.model, "text_prompt": prompt, "reference_paths": paths}}
