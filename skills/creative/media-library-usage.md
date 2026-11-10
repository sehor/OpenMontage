# Local-first production media

Use `media_library` for all stock image, video and audio needs. This is the
default before direct provider tools or corpus acquisition. Existing semantic
video corpora remain available, but are not this inventory's source of truth.

1. Call `search_local` with query, kind and filters. It queries SQLite only.
2. Judge relevance using the brief, tags, description, dimensions and duration.
   Check shortlisted files with `get`; missing/invalid files cannot be used.
3. When local results are absent or unsuitable, briefly explain the gap and call
   `search_pixabay` automatically. Do not ask permission for this search.
4. Present candidates with stable IDs, source page (Pixabay attribution), tags,
   technical details and license. Do not download previews or original files
   just to present the results. No automatic pagination or keyword fan-out.
5. Wait for the user's chosen candidates, then call `download_selected` with
   only those IDs. A search result is not permission to download it.
6. Call `export` with media_id, project_dir and scene_id. Add the returned entry
   to the project's asset_manifest. The path is relative to the project.

Audio is local-only. Do not fall back to Pixabay Music web scraping. If local
audio is insufficient, report the gap; any other provider is a separate choice.
Do not bypass this workflow using legacy tools that auto-select the first hit.

## Operations

All calls use `operation`. Optional `root` overrides the configured resource
directory (primarily for testing); default is `F:\videomake\resources`, or
`MEDIA_LIBRARY_ROOT`. Missing roots fail rather than creating another library.

- `sync`: explicit initial import or incremental directory synchronization.
  Never run implicitly during search. Preserves files and manual metadata;
  reports unreadable media and marks missing/invalid records unavailable.
- `search_local`: query, optional kind, limit (default 20), filters.
- `search_pixabay`: query, kind (image/video), filters; one page, 20 hits maximum.
- `download_selected`: candidate_ids from a prior search; serial and restartable.
- `get`: media_id; verifies file existence.
- `update_metadata`: media_id, fields (title, tags as string list, description,
  license, license_url, usage_restrictions, source_url, creator).
- `export`: media_id, project_dir, scene_id.

Filters: min_width, min_height, min_duration, max_duration, orientation
(`horizontal`, `vertical`, `square`). Unknown dimensions cannot satisfy a filter.
Local keyword search requires every whitespace-separated term to occur in the
indexed text; Chinese substrings work without a tokenizer. No AI image analysis.

The database and shared request state live under `.library/media.sqlite3`.
All Pixabay image/video entrypoints share a cross-process lock, at least five
seconds between requests, and 24-hour query caching. A cooldown response means
stop and report retry_at; do not loop or switch entrypoints. Downloads are written
to temporary files, validated and renamed before inventory registration. Retry
the same candidate ID after a failure to reconcile an already completed file.
Content hashes mark duplicates while preserving individual provenance records.
Unknown licenses remain unknown; user restrictions must not be discarded.

## Offline verification only

Tests/acceptance must never contact Pixabay API, website, preview or CDN URLs.
Use generated local media, fake responses and clocks, with network connections
blocked. Report: “离线验证通过，未进行 Pixabay 实网验证”. Do not add a live smoke test.
