# KRI-29: phone rendering for creator chat and re-edits

Status: implementation in progress; no verified rollout capabilities.
Branch: `emirerben/kri-29-rendering-videos-on-the-phone-in-our-app-rather-than-cloud`.

The user approved cloud AI analysis of low-resolution proxies, device-only
processing of originals/effect generation/final rendering, and automatic upload
of finished exports for Gallery/cross-device playback. Original uploads always
require explicit cloud-fallback consent. All creator-accessible styles and
effects, including re-edits and valid combinations, are in scope. Admin-only and
disabled experimental features are excluded. Parity is visual/timing parity,
not pixel-identical encoding. Do not introduce new generated-media features.

## Task ledger

- [x] Durable local originals and fingerprint-bound media IDs.
- [x] Clip-proxy reservation/attachment provenance and old-cloud rejection.
- [x] Shared basic preview/export compositor and explicit H.264/AAC writer.
- [x] Durable revision/attempt-fenced device coordinator and upload retries.
- [x] Owner/current-revision upload reservation and verified completion APIs.
- [ ] Complete portable, negotiated rendering contract for every style/effect.
- [ ] Shared analysis/planning split that stops before cloud effect generation.
- [ ] Visual-pool/narration local sources and licensed asset downloads.
- [ ] Chat creation and editor-save coordinator integration and local playback.
- [ ] Explicit cloud consent, relinking, retry, and interruption user flows.
- [ ] Full native effect/typography/audio port and combination coverage matrix.
- [ ] Poster/finalization, retention/account deletion, and publication audit.
- [ ] Cloud-reference comparisons for layout, typography, timing, audio, layers.
- [ ] Physical iPhone 13/current-iPhone performance and thermal gates.
- [ ] Final review, generated contracts, relevant tests, ios-verify, preship.
- [ ] Autoship PR, explicit landing approval, deployment, production checks.

## Acceptance and rollout

Each enabled capability must pass correctness/parity and physical-device gates:
sustained 30-fps preview, seek p95 <=250ms, and a 60-second export <=120 seconds,
with no crashes or critical thermal state. Record peak memory and thermal
behavior per fixture. The user will help with physical iPhone testing.
Capabilities remain disabled until measured. Rollback blocks new local attempts,
preserves finished exports, and never uploads originals automatically. All
agreed matrix rows must pass before KRI-29 is complete.

See [current implementation boundaries](../docs/runbooks/phone-rendering.md) and
[production capability baseline](../docs/reviews/kri-29/capability-baseline.json).
