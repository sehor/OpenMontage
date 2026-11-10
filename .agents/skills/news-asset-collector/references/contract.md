# Candidate contract and execution

After media registration, a full collection run continues to the integrated reporting step in [editorial.md](editorial.md). The same news IDs and asset IDs connect sources, paragraphs and footage. Narrow research-only/download-only requests stop at their requested scope.

## News selection checkpoint

Before acquisition, the agent presents numbered news candidates and waits for the user's usable-story selection. Save research state in `news.json`: `window`, `candidates`, `selection_status` (`pending` or `confirmed`), `selected_story_ids`, and `selection` (user choice text and confirmed_at). Pending means an empty selected list. Preserve source metadata collected during search; only confirmed story IDs proceed to media acquisition and `candidates.json`. A request for another search keeps selection pending for the new candidates. A user selection of fewer/more than 4–5 is valid.

Example interaction: the agent lists stories 1–6 and recommends 1–5; the user replies “1、3、6 可用”. Record exactly those three IDs and collect only their media. A reply “再找几条” authorizes further research, not acquisition of the previous recommendations. This checkpoint is enforced by the agent workflow; the standalone copy helper does not read news.json or infer user approval.

## Media registration

Run from the skill directory. Use the repository's existing Python:

```powershell
$python = 'E:\projects\OpenMontage\.venv\Scripts\python.exe'
uv run --no-project --python $python scripts/collector.py plan --project 'E:\projects\OpenMontage\video_projects\ai-news-2026-11-10' --input candidates.json
uv run --no-project --python $python scripts/collector.py ingest --project 'E:\projects\OpenMontage\video_projects\ai-news-2026-11-10' --input candidates.json
uv run --no-project --python $python scripts/test_collector.py
```

`examples/candidates.json` is an offline, link-only fixture, not real news or a source verification. To test acquired media, duplicate the record with a new ID, supply an absolute `local_path` to an existing JPG/PNG/WEBP/video/audio file, and remove `link_only_reason`. Licensing metadata and separate authorization confirmation are not required. `plan` writes nothing. `ingest` copies files and adds records. It never renames originals. The test suite generates its own fixtures.

Each record requires: `id` (immutable provenance ID), `news_id`, `description`, `source`, `provenance`, `confidence`, `topics`, `keywords`, and `media_type` (`image`, `video`, `audio`). Optional `brand`, `product`, `event`, `scene`, `category`, `local_path`. Without a local file, `link_only_reason` records a concrete failed/unavailable download. Unknown content fields use null or `unknown`, not invented values. Omit licensing metadata by default.

- `source`: page_url, media_url, title, caption, alt, published_at, event_at, retrieved_at. Preserve dates as ISO timestamps with timezone where known; retain date-only precision otherwise.
- `licensing`: optional legacy-compatible object, included only when specifically requested. No license lookup, unknown-license label or permission confirmation is part of default acquisition. The helper does not add or infer license claims.
- `provenance`: method (`page_context`, `visual`, `frame_analysis`, `transcript`, `capture`), evidence (field, excerpt, url), model/tool, frame_timestamps_seconds, clip_start_seconds, clip_end_seconds, transcript_source and parent_asset_id where applicable. Null means unknown. Do not invent endpoints or quoted evidence.
- `confidence`: overall 0..1, per_field values, rationale. These are editorial assessments, not calibrated model probabilities. Low confidence needs objective naming or additional inspection, not a confident brand/event label.
- `topics`, `keywords`: source-grounded searchable strings; `category`: hero, screenshot, product-demo, chart, interview, logo, b-roll, narration, music or objective alternatives.

The generated `assets.json` has schema_version, updated_at and assets. Entries preserve candidate metadata and add status, original_filename, file_path (project-relative or null), sha256, backup_path and duplicate_of where applicable. Exact-byte SHA-256 deduplication shares a file but keeps separate source records. Visually similar recompressions are not automatically deleted. Names follow `brand_event_scene_001.ext`, retaining extension; unsupported/unsafe characters are normalized. Null brand/event become `unknown`. No filename is treated as evidence.

A project lock prevents concurrent helper writes. A stale lock after a crash requires confirming that no collector is active before removing it. Existing manifests with another schema are refused. Manifest replacement is atomic and recorded after each successful item. Interrupted copies may leave unreferenced copies/backups; retain and reconcile them by hash rather than deleting broadly. Atomic multi-file transactions and automatic repair of externally modified manifests are outside this helper's scope.

The helper trusts the agent to verify evidence and actual media content; extension checking is not media decoding. Before ingest, inspect/download with trusted tools and verify content type/decodability using Pillow or FFprobe as appropriate. For huge media check available space first. For derived clips keep the full original and parent/timestamp provenance. Avoid storing session tokens or signed URL secrets in a distributable manifest; redact them and retain a stable public source page.

Installation source: repository `.agents/skills/news-asset-collector`; user copy: `%USERPROFILE%\.codex\skills\news-asset-collector`. `agents/openai.yaml` supplies the display name NewsAssetCollector. Reload/new chat may be needed for skill discovery. Invocation: `$news-asset-collector 搜索最近两天 AI 资讯，筛 4–5 条并同步素材入库到 <project>`.
