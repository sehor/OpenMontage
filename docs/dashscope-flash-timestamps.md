# Flash 多角色合成字幕

Flash 分段渲染默认逐轮使用 HTTP SSE 和 `word_timestamp_enabled: true`。Next 的
audio-generation 调用保持原样；直接调用 `dashscope_tts` 时 SSE 是显式选项：
`streaming: true`、`word_timestamp_enabled: true`。时间戳选项不能单独用于非流式或 Next。

## 返回数据与时间轴

每轮保存 WAV、`.events.jsonl`（完整返回事件，含音频 Base64）、`.timestamps.json`
（原始 sentence 数据）和 `.stream.receipt.json`（请求、状态、request_id、usage、哈希）。
渲染器还保留原有按合成参数计算的 `.receipt.json`，避免升级传输方式后错失历史缓存。
这些回执不保存鉴权头。原始事件可能较大，属于应保留的恢复证据。

2026-11-05 在 `qwen-audio-3.1-tts-flash` 上用现有复刻音色 man-normal-cn、Geny
分别进行了一个短请求，两者都同时启用了 SSML、SSE 和字级时间戳：

- 请求 1：`AI需要睡眠。W3C有123项。重庆，你好。`，W3C 使用 sub alias，123 使用 cardinal，
  重庆使用 py phoneme，并插入 500ms break。音频 6.34 秒。
- 请求 2：`今天是2026年。API测试。休息后，再学习。`，2026 使用 digits，插入 300ms break。
  音频 4.30 秒。
- 保存的原始返回中有 sentence-begin、sentence-synthesis、sentence-end 和最终 stop。
  synthesis 事件可能重复累积词表，或暂时返回空 words；字幕只使用 sentence-end 最终词表。
- 请求 1 第二个 provider sentence 为重庆一句，时间 4660–6340ms；请求 2 第二句为
  3100–4220ms。时间从整次请求起点累计，不能再次叠加前句时长。字索引在这两次实测中也
  跨句累计；实现不依靠该索引猜测原稿字符位置。
- 服务端分句不一定等于标点分句：`AI需要睡眠。W3C有123项。` 在同一 provider sentence 中。
  original_text 保留 W3C/123，words 则返回 alias 展开后的网络标准/一百二十三；英文可能
  被拆成带前置空格的字母，标点可能附在后一词上。
- phoneme 的 words 返回了 `[ch]`、`[�ng]` 等音素文本。直接使用服务端的 original_text
  显示重庆一句，不再进行读音或原稿对应。
- 两段之间插入 7200 个静音采样（300ms），Geny 从采样 159360（6.64 秒）开始。
  合并为 262560 个采样、10.94 秒、4 条服务端分句字幕；未调用 ASR。

这是两个短样本的接口、音频文件、字幕和时间轴验证。未验证音色听感与音素实际发音，
未证明 break 的声学静音精确等于标签时长，也未覆盖全部 SSML 标签、say-as 格式、长文本、
其它音色/模型、rate/pitch/volume 组合。请求成功不等于这些能力全部验证。

## 原稿显示与限制

每个服务端分句直接生成一条字幕：显示返回的 original_text，开始和结束分别取该分句
第一个与最后一个词的时间。sentence-end 缺少 original_text 时，使用该分句之前事件
中返回的文本；整句都没有 original_text 时，拼接返回的 words.text，并标记 text_source=words。
这时 JSON 和 warnings 提示使用的是词文本，可能包含读音展开或音素符号。

不读取原稿或 reading_map 做字幕匹配，不转换数字或校验别名，也不按长度再次拆句。
检查仅针对返回结构、完整分句和时间戳是否缺失、非单调或超出音频范围；异常写 review.json
并停止发布。没有本地识别、估算时间或原稿对应步骤。

全局时间使用 PCM 实际字节数计算的 24kHz 整数采样位置，再加轮次间静音。保留角色、
原稿范围、每片段采样起止位置；句内 break 已包含在音频及时间戳内，不重复添加。

## 恢复与旧音频

每个付费请求前独占创建回执。超时、流缺少 stop、provider 错误、进程中断都保留回执与
已经收到的音频和事件；再次运行会停止，等待人工核对结果/账单。不会自动重试或换供应商。
未完成音频使用 `.wav.partial`，不会发布成完整音频。目录锁遗留时，应先确认任务已退出。

字幕失败独立于合成：成功音频与合成回执仍保留；核对并修正本地时间戳数据后可以复用。
修改轮次间隔只重新拼接，不重新合成。当前发布指针在音频、字幕和时间轴全部完成后更新。

旧非流式回执仅有音频 URL、request_id 和 usage，无法事后恢复字级时间戳。渲染器在任何
新请求前检查全计划缓存，默认对旧音频写 needs_review 并停止，保留文件。
Flash 的本地识别与旧转写缓存读取代码已移除，不再接受 `legacy_subtitles`、`model_size`、
`language` 渲染参数，也不提供 CLI 的 `--legacy-subtitles`、`--subtitle-model`、`--language`。
字幕只使用合成时间戳；旧音频不参与字幕生成，保留文件用于历史核对。

保持项目输出目录和合成参数，才能匹配已有流式缓存。历史 readings 字段不参与字幕生成；
但改变 SSML/音色/指令/模型会形成新的合成请求。重试或重新合成前核实旧请求结果，保留回执。

## 验证入口

`examples.dashscope_timestamp_probe` 默认仅编译两轮小样；加 `--synthesize` 才提交。
存在成功结果时复用捕获文件，以禁止网络访问的适配器重放实际渲染流程；
存在失败或未完成回执时拒绝重发。该入口不会处理全文。

实测产物位于本机 `projects/sleep-flash-voiceover/validation/stream-20261105/`，
包括两轮请求、原始事件、音频、audit.json 和 assembled/ 中的字幕与时间轴。
这些用户项目文件被 Git 忽略；回归测试使用独立合成事件，不依赖私有音色或密钥。

官方依据（核对于 2026-11-05）：

- [HTTP API](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api)：SSE 头、
  word_timestamp_enabled、复刻音色支持及事件字段。
- [SSML 使用限制](https://help.aliyun.com/zh/model-studio/ssml-latex-user-guide)：Flash 模型、
  复刻音色及 HTTP enable_ssml 的支持范围。文档支持范围不替代具体标签组合实测。
