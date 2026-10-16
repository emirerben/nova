# KRI-698: approved simpler edits retain their own requirements

Before: a creator could accept the offered simpler edit, approve its draft, and
then receive “missing confirmed on-screen text.” The KRI-610 loop breaker only
remembered that an alternative had been offered. It neither checked the answer
nor updated the brief the render contract subsequently pinned.

After: the offer has concrete choice buttons and an exact text fallback. Only
acceptance of the named simpler option, for the same missing requirements at the
same brief version, supersedes those requirements. Refusal, an unrelated reply,
a clip number, a new request and an old choice cannot do so. A later unresolved
choice preserves an already accepted omission through rollback/recovery.

The next brief revision, draft binding, receipts, approval summary and render
contract share the effective request. The original requirement history remains
available. Chronological chat in the worker request explicitly identifies
superseded requirements as history. Existing approvals/jobs retain their immutable
authority; a previously failed job needs a new approval rather than a silent edit
to its old contract.

## Verification

The regression failed before the fix: the durable brief stayed at version 1,
and refusal/unrelated/stale replies reached approval. The focused checks exercise
real Postgres, turn completion, approval, dispatch claiming, the phone narration
compiler, immutable recipe pinning, idempotent pin redelivery and the publication
authority guard. Planner output is supplied by the replay; these checks do not
measure fresh model behavior.

The export proof uses the exact recipe pinned by that journey. Three identifiable
synthetic clips and a six-second narration tone go through the production macOS
AVFoundation exporter. Frame/audio checks verify the exact opening title in its
approved two-second window, all three clips in order, continuous audio, six-second
duration and H.264/AAC output. A title-free twin export proves the pixel comparison
detects a dropped title. Both unwaived rule text and a missing retained title still
fail the real contract verifier.

Repeat from the task worktree with a migrated scratch database:

```sh
bash scripts/dev/test-db.sh
src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py prepare /private/tmp/kri698-proof
KRIA_AUDIO_PARITY_DIR=/private/tmp/kri698-proof swift test --package-path src/apps/ios/Packages/KriaMediaEngine --filter AudioParityFixtureTests
src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py compare /private/tmp/kri698-proof
```

Inspect `approval.json`, `report.json`, `montage.png` and
`cases/approved/phone.mp4`. Keep generated media outside git. CI runs the API
regression through the existing approval/contract journey
`kri-492-length-ownership`; KRI-577's ledger records the evidence limits.

The original N2 boat source files were unavailable on this host. This is a local
synthetic-media export proof. Original-footage replay on a physical iPhone after
deployment remains pending; no paid model run or production mutation was made.
Duration ownership and editor timeline behavior are unchanged.
