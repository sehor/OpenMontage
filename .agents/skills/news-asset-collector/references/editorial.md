# Editorial source and asset contract

Project files:

| File | Ownership and purpose |
|---|---|
| news.json | Verified candidates, actual user selection, source pages |
| assets.json | Stable asset IDs, observed content, files and provenance |
| report.json | Agent-authored canonical draft; stable paragraph IDs, text, sources, shots, gaps |
| report.md | Readable generated narration with separate editorial notes; synchronize edits back into report.json |
| storyboard.json | Generated paragraph-to-footage mapping and freshness hashes; no duplicate narration |

The writing step is part of a full NewsAssetCollector run. A request to improve the skill alone does not authorize generating a real episode draft; use offline fixtures to test it. A request to continue an existing episode can reuse all research/materials and selection state.

## Input

Prepare report-plan.json in a scratch directory using the current project's actual IDs. Required shape:

```json
{
  "title": "报道标题",
  "status": "draft",
  "paragraphs": [
    {
      "id": "p001",
      "news_ids": ["actual-selected-story-id"],
      "text": "这段实际要读出的中文报道正文。",
      "sources": [{"url": "https://official.example/announcement", "support": "这条来源实际支持的事实、限定条件或相关段落说明。"}],
      "shots": [{"asset_id": "actual-asset-id", "role": "illustration", "note": "品牌标志只用于交代主体，不代表本次事件的现场画面。"}],
      "gaps": []
    }
  ]
}
```

An opening/transition with no factual claims may use empty news_ids/sources. The agent must judge whether it truly contains no claims. Factual paragraphs reference selected stories and need sources. A source added beyond the original announcement is allowed after opening/verifying it; record what it supports.

shots reference acquired files only. A video shot needs numeric `in_seconds` and `out_seconds`, with `0 <= in < out <= technical.duration_seconds` in assets.json. Add evidence of interval inspection in note; the script checks range, not visual correspondence. Image shots omit timecodes. role is `evidence` or `illustration`; note explains the relationship and relevant limitations. Cross-story footage can only be illustration, explicitly explained. A missing shot requires an explicit gaps entry, e.g. “缺少该新功能的界面画面；当前仅有公司标志”. A planned chart/animation not yet generated is a gap, not a fabricated asset ID.

## Commands

From the installed skill directory, with an existing Python:

```powershell
$python = 'E:\projects\OpenMontage\.venv\Scripts\python.exe'
uv run --no-project --python $python scripts/editorial.py --project <project> --input <report-plan.json>
uv run --no-project --python $python scripts/editorial.py --project <project> --input <report-plan.json> --apply
uv run --no-project --python $python scripts/editorial.py --project <project> --check
uv run --no-project --python $python scripts/test_editorial.py
```

Preview performs validation without writing to the project. Apply validates selected news, source notes, asset files/hashes, timecodes and paths before saving a draft. Replacements are backed up under `artifacts/editorial-<timestamp>/`. A project lock coordinates with collection. Writes are atomic per file, not a multi-file transaction; --check detects incomplete/stale output. File freshness is strict: even unrelated news/assets edits require regeneration; human review determines whether the changed content affects a paragraph.

## Human review

Present report.md as the actual draft, not just a completion notice. Summarize visual/source gaps separately; do not read source/asset notes as narration. Preserve the user's chosen framing and factual limits. Have the user review the draft before voiceover or final production. Record approval in the production checkpoint with the actual user response and time, never inferred from passing validation. Revision keeps IDs stable and refreshes sources/mappings; new facts require new evidence. This exporter always outputs draft status and cannot approve a report.

Once the actual user approves the narration, the agent may preserve that response in an optional `text_approval` object: status `approved`, user_response, recorded_at, approved_text_sha256. Use `editorial.narration_digest(plan)` for the digest. This records an actual decision; the exporter never infers approval. Its structured document status stays draft, but the readable header reflects confirmed narration. Updating shots preserves approval; changing title, paragraph IDs or narration invalidates the digest and requires review of the changed text. Keep a separate report-approval.json checkpoint when handing off to production.

For a finished production handoff, resolve gaps or explicitly agree on their treatment, then align the approved narration to the actual recorded/generated audio. Source video in/out points are not final edit timestamps. Do not install new TTS/vision models for this workflow.
