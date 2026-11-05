# LLM Radar 成片组装

> 当前三模型样片已撤销内容验收：误用了旧“速度／性价比”视频，出现 227.8 tokens/s 等原始值，未对应百分制数据。技术测试通过不代表内容正确。旧片仅保留用于追溯，不是可交付成果。

本项目只接收 **LLM Radar 已经导出的媒体**。雷达动画代码、数据计算和动画渲染均保留在上游。这里提供素材交接校验、片头片尾、时间线组装、测试标识和最终 MP4 渲染。

## 三模型测试

仅使用 GPT-6 Astra、Claude Fable 5.1、DeepSeek V4.1 Flash，按上游原顺序播放。

- 来源：上游 `out/three-model-six-axis-final.mp4`，一个 18 秒文件中三个连续六秒段落。
- 包装：3.5 秒片头 + 18 秒原片 + 5.5 秒片尾，共 27 秒；1920×1080、60 fps。
- 保留原片已混合的音乐、音效与署名，片头片尾留白，不再重复叠加音乐。
- 这是历史素材组装测试。原片含“速度／延迟、性价比”旧轴，不等于当前数据定义或最新评测。
- 输出：`episodes/three-model-preview/renders/llm-radar-three-model-preview.mp4`。
- 阶段记录、媒体检查和时间线：同一期的 `artifacts/`。

## 文件

| 文件 | 用途 |
| --- | --- |
| `config/handoff.example.json` | 已检查的三模型素材清单，包括哈希、时间范围、模型前驱、版本、内嵌音轨和署名 |
| `config/episode.example.json` | 三模型片序、标题、片头片尾时长、输出规格 |
| `config/source.local.json` | 本机上游目录，不进入版本控制 |
| `schemas/assembly.schema.json` | 清单和每期配置 schema |
| `scripts/assemble.py` | 导入、复查、包装准备、受检查点约束的合成适配器 |
| `tests/test_assembly.py` | 顺序、草稿门禁、路径、哈希和快照测试 |

## 使用

在 OpenMontage 根目录的 PowerShell 中执行。需要 FFmpeg/ffprobe、现有 Python 环境和 jsonschema；包装使用本机 Microsoft YaHei 字体。

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) 'scratch/uv-cache'
$radarSource = (Get-Content projects/llm-radar/config/source.local.json -Raw | ConvertFrom-Json).sourceRoot
```

首次导入（已存在的快照会拒绝覆盖）：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python projects/llm-radar/scripts/assemble.py import --source $radarSource --manifest projects/llm-radar/config/handoff.example.json --episode projects/llm-radar/config/episode.example.json --output projects/llm-radar/episodes/three-model-preview
```

验证已导入素材：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python projects/llm-radar/scripts/assemble.py validate --output projects/llm-radar/episodes/three-model-preview
```

由 agent 按 `pipeline_defs/hybrid.yaml` 完成媒体审阅、idea、script、scene_plan，并保存提案和检查点后，准备片头片尾与合成参数：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python projects/llm-radar/scripts/assemble.py prepare --output projects/llm-radar/episodes/three-model-preview
```

`prepare` 需要 `artifacts/proposal_packet.json`。它只执行确定性包装工作，不创建审批结论或代替 agent 编排管线。重新 prepare 后，必须重新审阅并保存 assets/edit 检查点。

完成 assets、edit 阶段审阅及检查点后：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python projects/llm-radar/scripts/assemble.py render --output projects/llm-radar/episodes/three-model-preview
```

`render` 核验已有检查点和合成参数，通过 registry 调用 `video_compose` 的显式 FFmpeg 路径，并保存结果。渲染后 agent 仍需核对关键帧、音轨及正式/草稿状态，写入 compose 检查点。不会自动发布。

测试：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python -m pytest projects/llm-radar/tests tests/tools/test_video_compose_frame_rate.py -q
```

## 下一期怎么复用

1. 让 LLM Radar 导出所需动画片段；可以是独立片段，也可以提供长片中的明确入出点。
2. 更新交付清单、文件 SHA-256、模型前驱、烧录编号、轴顺序、版本、署名及就绪状态。不能仅重命名文件或翻转 `renderReady` 来宣称数据已冻结。
3. 复制每期配置，设置新的 `id` 与 `clipIds`；导入新的 episode 目录，旧快照保持不变。
4. 由 OpenMontage agent 复用 hybrid 组装流程。新增一期不需要雷达源码或上游 node_modules。

入出点不改变速度。上游已烧录的残影与编号必须与本期顺序匹配，否则导入会拒绝。连续片段会合并为一次连续剪切，避免音乐在模型切换处被重新编码成不连续片段。

## 当前边界

- 新增强制内容检查：实际六轴、0–100 分数、尺度版本、上游视频的数据指纹必须对应，并逐帧核对画面。执行 `scripts/audit_score_handoff.py --data <百分制数据文件> --output <本期目录>` 后，仅数据校验通过还不够，agent 必须完成真实画面核对；不能直接把 `visualEvidenceConfirmed` 改为 true 来绕过检查。
- 当前指定上游 `data/radar/models.json` 中三个选中模型的 `scores` 均为六个 null。正确数据与对应上游动画尚未就绪，渲染门禁已阻止旧片再次作为修正版输出。

- 第一版使用带背景的成片素材、硬切衔接，音轨策略为 `preserve-embedded`。音乐/音效已经混合时不尝试拆分。
- 独立音乐/音效再混音、独立图片叠加、透明通道及复杂转场应在后续素材交付中明确配置并通过现有管线工具处理；当前命令不会默默支持或忽略这些未知参数。
- 正式模式要求全部片段为 `frozen` 且 `renderReady=true`；当前历史样例会被拒绝。
- 当前输入需与指定 1080p 帧率一致；不匹配时给出错误，让编排阶段决定规范化方案。
- 项目源码与配置示例进入版本控制；上游素材副本、个人路径、检查点及视频保持忽略。

公共工具补充帧率控制：`edit_decisions.metadata.compose_target.fps` 控制 FFmpeg 中间剪切帧率，默认仍为 30；指定媒体 profile 时 profile 优先。这避免将 60 fps 素材先降帧再升回 60 fps。

同时修复中间片段各自编码 AAC 时累积音频起始延迟的问题：中间使用 PCM 音轨，最终只编码一次 AAC。三模型样片逐段波形比对延迟为 0 ms，相似度均超过 0.998。

可复查成片规格和原音轨保留情况（不替代主观试听）：

```powershell
uv run --offline --no-project --python .venv/Scripts/python.exe python projects/llm-radar/scripts/verify_media.py --output projects/llm-radar/episodes/three-model-preview
```
