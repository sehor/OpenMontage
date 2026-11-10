# Gemini speech 接入验证（2026-11-10）

- 新增 `gemini_tts`、`gemini_voice_clone`，保留 Google Cloud `google_tts`。
- 81 项测试通过：Gemini 离线行为、Google Cloud 兼容、指定服务不降级、阿里云相关回归。
- 6 项既有配置和预算测试通过；共 87 项。
- 当前 `.env` 密钥可访问 `models/gemini-3.8-flash-tts`。
- 真实官方音色合成成功：Kore，中文正文与风格指令分开传递。
- FFprobe 验证：9.32 秒、24 kHz、单声道、16 位 PCM WAV。
- 本地样音：`scratch/diagnostics/gemini-speech/official-sample.wav`。
- 服务用量：输入 27 tokens、输出 233 tokens、合计 260 tokens。
- 计费实际金额未知；保留保守预算预留，不以框架默认 0 声称免费。
- 录音克隆的请求和错误恢复已通过模拟测试；没有上传任何本人参考／授权录音，真实创建、账号克隆权限和相似度仍待验证。
- 样音已通过容器与音频规格检查；发音、遗漏及指令是否被朗读仍需试听确认。

测试命令从项目根目录执行，使用项目现有 Python 环境：

```powershell
uv run --no-project --python .venv/Scripts/python.exe python -m pytest tests/tools/test_gemini_speech.py tests/tools/test_dashscope_tts.py tests/tools/test_dashscope_flash.py tests/tools/test_dashscope_dialogue.py -q -p no:cacheprovider --basetemp scratch/diagnostics/gemini-regression-final
uv run --no-project --python .venv/Scripts/python.exe python -m pytest tests/contracts/test_phase0_contracts.py::TestConfig tests/contracts/test_phase0_contracts.py::TestCostTracker -q -p no:cacheprovider --basetemp scratch/diagnostics/gemini-config-check
```
