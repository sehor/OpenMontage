# Gemini 声音克隆与单人配音

Use `gemini_tts` for official or replicated single-speaker voices and
`gemini_voice_clone` to create a persistent voice. Discover both through the
registry. Existing `google_tts` uses a different Google Cloud API.

## Setup and preflight

The registry loads `.env`. Gemini tools prefer `GEMINI_API_KEY`, falling back
to `GOOGLE_API_KEY`. Never print credentials. A configured key means locally
configured, not verified API entitlement. The default model is configured at
`audio.gemini.model`; the default official voice is `Kore`.

Read tool `dry_run()` before paid production. Use the existing `CostTracker`
approval/reservation workflow. `gemini_tts.estimate_cost()` is a conservative
full-request reservation ($0.303104), not an expected short-sample cost.
Receipts retain service usage; `actual_cost_usd=null` means unknown billing.
The framework's default `cost_usd=0` does not mean free synthesis. Clone costs
are unquoted; report that rather than treating a zero estimate as free.

## Clone a voice

Require two authentic recordings of the same adult speaker:
- `reference_path`: 10–30 seconds of clean natural speech.
- `consent_path`: the speaker recites Google's exact consent phrase.
- Both files: 24 kHz mono 16-bit PCM WAV; maximum configured size is 10 MiB each.

Chinese consent phrase:
“我是此声音的拥有者并授权谷歌使用此声音创建语音合成模型”

Call `gemini_voice_clone.execute` with those paths, `display_name`, and a new
JSON `output_path`. Google verifies the consent; local WAV validation does not
verify the identity, age or spoken consent. Replication requires Paid Tier 1+.
Never manufacture a consent clip or substitute synthesized speech.

The tool uploads both clips and creates `store=true` state. Preserve the
returned `voice_id` and provider `expire_time`; reuse the ID for later requests.
Stored voices expire one year after last use; the tool does not calculate or
renew expiry. Creation is not retried automatically. If receipt saving fails,
preserve `result.data.voice_id` rather than repeating creation.

## Synthesize

Call `gemini_tts.execute` with `text`, optional `voice_id`, `speech_style`,
`model_id`, and a WAV `output_path`. Or use `tts_selector.execute` with the
same fields and `preferred_provider="gemini"`. Model overrides must match
the explicit configuration. Official voices use names such as `Kore`;
replicated voices use the returned `voice_…` ID.

Keep performance instructions in `speech_style`, not in spoken text. Existing
numeric `style` belongs to ElevenLabs and is not a Gemini style prompt.
The response is a complete WAV; never prepend another header. Each successful
call writes WAV plus `.receipt.json` containing format, duration and usage.
No streaming, multi-speaker, SSML, native word timestamps or automatic long
text splitting is provided in this version. Existing subtitle tools remain
available; label their timing source honestly.

## Review and failures

Listen to a short sample before a full production run. Check intelligibility,
missing/truncated words, whether directions were spoken and cloned similarity.
Audio validation proves container integrity, not pronunciation or similarity.
Permission errors require checking key/project/tier/region; quota errors require
checking quota; consent errors require matching genuine recordings; expired IDs
require checking the stored voice. No automatic retries or provider substitutions.

Production media belongs under `video_projects/<name>/`; smoke-test audio belongs
under `scratch/diagnostics/` or `tests/qa/output/`. Never commit recordings,
secrets or raw request bodies.

References:
- https://ai.google.dev/gemini-api/docs/speech-generation
- https://ai.google.dev/gemini-api/docs/voice-replication
- https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash-tts
