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
uv --cache-dir .cache/uv run --no-project --python .venv/Scripts/python.exe python -m examples.dashscope_ssml --source projects/my-project/manuscript.txt --roles projects/my-project/roles.json --project-dir projects/my-project
```

Despite the historical module name, this emits a dialogue prompt by default.
Add `--segments <reviewed.json> --source-format plain` for unlabelled prose.
Add `--synthesize` only for requested audio. `--model <profile-or-full-ID>`
overrides the model for that run. `--ssml` explicitly selects the legacy SSML
compiler. Existing synthesis receipts prevent accidental repeat billing.

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
