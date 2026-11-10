# 制作素材库

入口：`media_library`，操作说明见
[media-library-usage.md](../skills/creative/media-library-usage.md)。

- 默认资源目录：`F:\videomake\resources`。
- 数据库：资源目录下 `.library/media.sqlite3`。
- 首次整理旧素材：显式调用 `{"operation": "sync"}`。
- 日常检索：`{"operation": "search_local", "query": "科技", "kind": "video"}`。
- 制作代理判断本地不合适时，自动调用 `search_pixabay`；音频仅查本地。
- 用户选择后，以返回的 ID 调用 `download_selected`；下载成功立即入库。
- 外部手动添加、移动素材后，再显式调用 `sync`。检索不会自动扫描磁盘。
- 项目使用素材时调用 `export`，返回可加入 `asset_manifest` 的条目。

图片 API 的原图尺寸可能与可下载版本不同，不能当成下载尺寸使用。
要求尺寸/方向筛选时，优先使用带准确尺寸的 web 版本；无准确尺寸的
候选不能通过硬尺寸筛选。下载后以本地探测结果为准。
音视频技术信息需要本地 `ffprobe`；图片使用 Pillow。没有内容识别模型。
重复内容记录 `duplicate_of`，保留每份来源记录，不自动删除已有文件。

旧图片、视频工具和视频源适配器共用同一限速客户端，但旧工具仍保留
原有“取首条并下载”的兼容行为。新素材流程必须使用 `media_library`，
不得以旧工具绕过用户选择。既有音乐网页抓取工具不用于此流程。

## 离线验收记录

2026-11-06：**离线验证通过，未进行 Pixabay 实网验证。**

执行：`uv run --offline --no-project --python .venv/Scripts/python.exe python -m pytest tests/media_library -q -p no:cacheprovider`

结果：16 项通过。测试目录内自动阻断外部 socket 连接；仅用模拟响应、
模拟时间和本地生成的图片、音视频样本。跨进程锁测试的子进程也禁止联网。
覆盖数据库检索、增量同步、元数据保留、项目清单验证、缓存和请求间隔、
跨进程互斥、冷却持久化、认证停止、有限重试、密钥脱敏、重复素材、
下载中断、入库失败恢复、损坏素材、缺失目录及旧接口兼容。

本次实现没有扫描真实资源目录，没有建立实际素材索引，没有下载素材，
也没有验证真实 API 密钥或 Pixabay 服务可用性。
