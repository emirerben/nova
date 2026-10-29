# API credit implementation workflow

## Acceptance

This increment prepares API experiments without spending credits. The offline command must reject unsupported provider capabilities, invalid allocations, empty output, truncated audio, and a provider transcript that differs from the approved script. It must preserve editable image titles outside the generated pixels. Model comparison must use the existing packet preflight, with provider totals reported separately. Gemini remains the production default. Final video rendering stays on the iPhone.

The creator sees no new controls in this increment. Native asset registration, authenticated ownership, approval, storage retention, and device export remain required before product exposure.

## Phases

- [x] Read poteto-mode Principles.
- [x] Phase A: Frame. Inspect existing comparison, ledger and native asset ownership.
- [x] Phase B: Design the workflow. Compare shared text-runtime extension against a separate generated-asset contract. Select the latter because binary output and streaming completion have different contracts.
- [x] Phase C: Run the loop. Verify comparison allocation preflight, then generated-image and narration contracts with offline transport fixtures.
- [x] Phase D: Keep the audit trail. Record decisions in decisions.tsv.
- [x] Phase E: Verify and hand back. Run contract checks and independent review. Report all unverified product boundaries.

## Throughput checkpoint

One bounded checkout owns three small modules and focused offline tests. No native build, media download, live provider call, deployment, or production configuration change is needed. Verify each unit before adding the next. Keep the existing experiment CLI and ledger as the live execution authority.

## Failure modes recorded before implementation

- A grant amount becomes permission to spend, or reserve dollars become an allocation.
- A packet exceeds one provider's allocation while staying under the combined total.
- An unknown model is assigned to the wrong provider.
- A synthetic control is represented as an independent real holdout.
- Image edits include a Gemini URI, or generated title text replaces editable text.
- A response contains a URL rather than inspected bytes, malformed base64, missing usage, or extra images.
- Narration returns partial audio, a failed terminal event, excessive bytes, a different transcript, or a different response identity.
- A model alias is sent to the deprecated Speech API.
- An SDK retry or a duplicated submission silently adds cost.
- A mock success is reported as a working phone asset or a live model result.

## Design sketch

`CreditAllocation` maps provider and capability to planning ceilings in USD. These ceilings never authorize execution. `comparison_credit_preflight(packet)` delegates packet validation and pricing to `prepare_packet`, then groups existing priced rows by provider. It does not create another runner.

`ImageRequest` owns prompt, image dimensions, quality and separately editable title. `image_payload` produces an Images API generation payload. `decode_image` accepts PNG bytes and token usage only. Image editing waits for an owned immutable input contract.

`NarrationRequest` owns the approved script and preset voice. `narration_events` creates an isolated Realtime response. `decode_narration` requires completed audio, completed transcript and a successful response with matching identity. It returns WAV bytes for later owned asset registration. No network client is constructed by these modules.

## Source checks on 29 October 2026

- [OpenAI image generation](https://developers.openai.com/api/docs/guides/image-generation) documents `gpt-image-2.5-sunburst` and direct Images API generation.
- [OpenAI speech migration](https://developers.openai.com/api/docs/guides/text-to-speech) directs new work to `gpt-realtime-2.1-mini` through Realtime, not `/audio/speech`.
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) documents audio delta events, response completion, and 24 kHz PCM output.
- [Claude models](https://platform.claude.com/docs/en/models/overview) lists `claude-sonnet-5-5` with text output and image input.

The existing text adapters already support current documented model IDs. No automatic provider promotion is justified by the earlier comparison. No paid usage or remaining credit balance was verified in this run.

## Remaining implementation

This is the first offline increment. It does not implement a paid provider transport, live spend caps, image editing, native asset insertion, or a narration control. Planning ceilings do not change the existing ledger budgets. The generated-asset request builders are not registered with any HTTP route, task, or agent capability. No feature flag can activate them in production.

The next unit needs a persistent operation identity, separate image and narration flags that default to false, ledger reservations, per-provider and per-capability atomic caps, zero SDK retries, and retained unknown liability. Verify authoritative worst-case request prices before that unit can contact a provider. A provider success followed by failed storage must settle billing separately and retry storage without regenerating.

Then admit owned image bytes through the existing `PlanItemAsset` pool and source registration contracts. Admit narration through the existing voiceover identity and duration checks. Keep image titles in editable text lanes. Image editing needs an owned immutable reference-image contract first. Native export must verify both paths before any exposure. Optional grounded script and localization work can reuse the existing text comparison after independent eligible sources are available. This run enrolled zero fresh real holdouts.

The audio tests use synthetic PCM. Matching the provider transcript establishes a text consistency check only. It does not establish what a human hears, timing, pronunciation, or whether a generated image obeys the request to omit lettering. These need independent inspection in the future live and native evidence.
