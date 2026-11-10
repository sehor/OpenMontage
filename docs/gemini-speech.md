# Google Gemini 声音功能

已接入 `gemini_tts`（单人配音）和 `gemini_voice_clone`（录音克隆）。
原 `google_tts` 保留 Google Cloud TTS 路径，两者独立。

完整说明、录音要求、预算和错误处理见 [使用指南](../skills/core/gemini-tts.md)。

```python
from tools.tool_registry import registry
registry.discover("tools.audio")

# 官方音色；正式运行前按项目既有机制确认预算和样音。
result = registry.get("gemini_tts").execute({
    "text": "你好，欢迎收听。",
    "voice_id": "Kore",
    "speech_style": "温暖自然的普通话",
    "output_path": "video_projects/my-video/assets/audio/narration.wav",
})

# 用本人真实参考录音和本人授权录音创建音色。
voice = registry.get("gemini_voice_clone").execute({
    "reference_path": "video_projects/my-video/assets/audio/reference.wav",
    "consent_path": "video_projects/my-video/assets/audio/consent.wav",
    "display_name": "我的旁白音色",
    "output_path": "video_projects/my-video/artifacts/google-voice.json",
})
# 成功后，将 voice.data["voice_id"] 作为后续 gemini_tts 的 voice_id。
```

从仓库根目录探测模型访问（不合成）：

```powershell
uv run --no-project --python .venv/Scripts/python.exe python -m examples.gemini_speech
```

追加 `--sample` 会实际调用一次语音合成，默认输出到
`scratch/diagnostics/gemini-speech/official-sample.wav`，并记录保守预算预留。
真实克隆验证需要两份符合指南的录音；接口可用不代表相似度已通过试听。
