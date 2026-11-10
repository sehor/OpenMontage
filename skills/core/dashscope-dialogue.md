# Multi-role manuscript → reference-conditioned dialogue

Use `dashscope_dialogue` for mixed-role manuscripts, natural multi-speaker
conversation, and requests to replace the old multi-voice SSML flow. Read the
`qwen-tts` provider skill first. This standalone audio workflow requires no video
pipeline; video production still follows its chosen pipeline.

## Understand and preserve the source

The current agent assigns speakers, narration and modest delivery annotations.
Resolve uncertain speakers and missing voice mappings in one question. Preserve
all spoken words and punctuation. With explicit `角色：台词` / `角色: dialogue`,
omit `segments` to parse labels locally. Multiline dialogue continues until the
next label. For unlabelled prose, supply ordered segments with `role`, `start`
and `end` (Python Unicode character offsets; end exclusive). Optional `text`
must equal the exact source slice. Set `source_format=plain` for prose or
`labelled` for reviewed labelled spans. Include narration explicitly.

Map roles to library names, for example `{"搭档":"Geny","主持人":"man-normal-cn"}`,
or to `{"audio_path":"absolute/path/to/reference.wav"}` objects. The compiler
reads the original `source_path` from the library's voice.json, not its Flash
enrollment ID. A missing original recording is a blocker, not permission to
select a different voice. Existing enrollment metadata stays unchanged.

## Compile locally

Discover `dashscope_dialogue` via the registry. Inputs are `source_text`, `roles`,
`project_dir`, and optional `segments`, `source_format`, `model`, `artifact_name`.
The compiler verifies source coverage and ordering, resolves reference slots,
checks reference duration/size and the configured model's prompt-length limit.
The documented default limit is 3000 characters for the full text_prompt,
including delivery guidance and role slots. Both compilation and paid-call
preflight enforce it; oversized inputs are rejected with a split-manuscript
message, never truncated. Next has no documented SSML switch: SSML tags are
rejected instead of forwarded as text. Natural-language pauses are approximate.
Shared recordings share one slot. The current configured profile permits up to
three distinct references, assigned in first-speaking order.

Optional segment `emotion`, `direction`, `rate`, and `breaks` become natural
language delivery instructions beside quoted original dialogue. Pause timing and
delivery are approximate model instructions, not sample-accurate SSML controls.
Use `direction` for pitch and volume rather than legacy numeric fields.

The outputs under `<project>/artifacts/` are original source, `.prompt.txt`, and
JSON with original spans, roles, reference slots and synthesis inputs. A request
only to prepare a manuscript ends here without billing. Do not emit multi-voice
SSML for the default reference-conditioned model.

## Synthesize when requested

Pass `data.synthesis_inputs` to `dashscope_tts` and save audio under
`<project>/assets/audio/`. The tool selects the request protocol from configuration
and encodes local references into Base64 only for transmission. Receipts save
paths, hashes and usage, not Base64 or temporary signed download URLs. Listen for
speaker switching, similarity and delivery; HTTP success alone does not verify
these perceptual properties. No automatic retries or provider substitutions.

The existing command now defaults to this dialogue workflow:

```powershell
uv --cache-dir .cache/uv run --no-project --python .venv/Scripts/python.exe python -m examples.dashscope_ssml --source video_projects/my-project/manuscript.txt --roles video_projects/my-project/roles.json --project-dir video_projects/my-project
```

Despite the historical module name, this emits a dialogue prompt by default.
Add `--segments <reviewed.json> --source-format plain` for unlabelled prose.
Add `--synthesize` only for requested audio. `--model <profile-or-full-ID>`
overrides the model for that run. `--ssml` selects the configured SSML model
and the segmented compiler. `--legacy-ssml` explicitly selects the old
single-request compiler. Existing synthesis receipts prevent accidental repeat billing.

## Configuration is the source of truth

`config.yaml` → `audio.dashscope` contains `default_model`, `ssml_model`,
`enrollment_model` and named `models` with full IDs, protocols and limits.
Resolution order is explicit `model`/`model_id`, then `DASHSCOPE_MODEL`, then
`default_model`. `OPENMONTAGE_CONFIG` optionally points to another YAML file.
Models must have a configured protocol profile. IDs are never inferred from a
suffix or replaced inside a cloned voice ID. Enrollment separately uses
`enrollment_model`; choosing Next for generation does not change old enrollment
records or imply that Next accepts enrollment IDs.

Official reference: https://help.aliyun.com/zh/model-studio/audio-generation-api

## Flash 分段对话（可选）

保持 Next 为默认模型。使用 `--ssml` 或 `--model flash` 选择按完整发言轮次合成：

```powershell
uv --cache-dir .cache/uv run --no-project --python .venv/Scripts/python.exe python -m examples.dashscope_ssml --source video_projects/my-project/manuscript.txt --roles video_projects/my-project/roles.json --project-dir video_projects/my-project --model flash
```

先检查编译产物，再加 `--synthesize` 生成音频、句级字幕与时间轴。此模式通过注册工具
`dashscope_dialogue_render` 执行，底层逐角色调用 `dashscope_tts` 流式合成，开启
`word_timestamp_enabled`，使用返回的文本与毫秒时间生成字幕，再由 `subtitle_gen` 输出。
新音频无需 Whisper。时间以单次请求音频起点为零；全局偏移来自已拼接音频的整数采样数。
读取 [Flash 时间戳说明](../../docs/dashscope-flash-timestamps.md) 了解实测返回结构和限制。

roles.json 中可以直接使用已有复刻库名（如 Geny），也可以使用对象：
`{"voice_id":"系统音色或已有复刻 ID","model":"qwen-audio-3.1-tts-flash","ssml_supported":true}`。
仅在官方音色列表确认支持 SSML 后标注 true；缺少标注的显式音色只能合成纯文本。
库内成功复刻的 Flash 音色默认可使用 SSML，不重新上传或登记。

segments 的 `ssml` 字段接受完整的单个 `<speak>...</speak>`，支持 break、sub、phoneme、
say-as。标签内显示文本必须与原文切片一致，且不能同时设置 breaks。旧片段的 `voice`
须与角色映射一致，转换时会移除。输出的 `.ssml` 按轮次保存标签，配套 `.json`
保存角色、复刻音色 ID 和分段请求。每次合成只从请求的 `input.voice` 选音色。
字幕直接使用返回的 original_text。

默认轮次间静音 300ms，`--gap-ms` 调整；segments 的 `gap_after_ms` 可覆盖某轮。
超长轮次沿句末拆分，内部不额外加静音。无法安全拆分时明确报错，不截断。

输出在 `assets/audio/<artifact_name>/versions/<指纹>/`，包含 dialogue.wav、subtitles.srt、
subtitles.json、timeline.json。只有全部成功后更新 current.json。timeline 的采样位置为
24kHz 整数采样数，角色和原文范围随片段保存。只改间隔不会重新付费合成。

chunks 目录保留请求回执、音频哈希、原始 SSE 事件、时间戳和字幕缓存。重复或累积词表只取
sentence-end 的最终快照。每个服务端分句用 original_text 和真实首末词时间生成一条字幕。
未返回 original_text 时使用该句 words.text 并在 JSON 标记 text_source=words。
字幕不与原稿或读音做匹配；缺失或无效时间戳生成 review.json，停止发布。

字幕只接受合成时返回的时间戳。缺失或无效时生成 needs_review 并停止。
旧非流式音频和识别缓存不用于本流程；保留旧文件只为核对历史请求，避免自动重复付费。
再次合成应使用明确的新输出目录并先确认范围；原目录遇到旧音频会在任何新请求前停止。
请求失败或 request_started 状态不会自动重试，需先人工核对账单和结果，再决定如何处理
对应回执；不要直接删除回执绕过重复付费保护。崩溃遗留的 .render.lock 需确认没有运行中的
同目录任务后人工移除。禁止同时操作同一输出目录。
