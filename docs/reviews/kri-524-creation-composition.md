# KRI-524 creation composition follow-up

## Failure and scope

A new creation request combined word-by-word title animation, chronological clips,
and bottom-left location placeholders. The saved brief retained the title and
`typewriter` effect but lost sequencing. The resulting timeline contained a
whole-title entrance and an unintended static duplicate on the opening clip.
The production model output was redacted, so extraction versus normalization
cannot be distinguished from that capture. Raw incident data remains private.

The creation planner previously populated fixed title/label fields without the
editor's composable text-operation path. PR #1499 now addresses both semantic
retry routing (described in [the earlier report](kri-524-retry-routing.md)) and
this initial-creation gap. It does not claim to fix every AI capability.

## Change

- Reuse existing editor text operation descriptions, parser and compiler after
  the actual base timeline is assembled. Word and phrase sequences, placement,
  styling and entrance/exit phases use general operations, not wording branches.
- Persist a typed versioned text program bound to base text and source/output
  windows. Replay is deterministic through validation and phone export planning.
  New text identities are stable. No model runs during immutable compilation.
- Preserve the full original request at approval, even after short clarification
  replies. Reject overflow instead of truncating. Keep complete long/currency
  title text in the model's component context.
- Strengthen Main Creator's brief preservation instructions. Receipts inspect
  actual composed wording and do not equate literal presence with verified
  animation or placement.
- Preserve current generation authorization, ownership, media/attempt/revision
  fences, and existing Save/export contracts. No public endpoint, migration,
  product UI or renderer capability was added. The stored program stays internal;
  manual proposal saves preserve it and refuse stale bases with a replan message.

A composed montage keeps its source/output windows if catalog music is selected
later. This intentionally disables later beat-snapping of those windows; moving
them would invalidate the text program. Unchanged/no-op creation keeps the old
behavior. The extra composition pass has a 90-second background timeout and adds
model cost. Explicit incomplete/unsupported responses fail creation instead of
silently claiming the requested effect was completed.

## Evidence

Failures were captured before implementation: missing creation composition,
false completion receipts, absent worker handoff, superseded label receipts,
and truncated long/currency text. Offline cases cover real parser/compiler replay,
serialize/reload, non-text operation rejection, base drift, invalid timing,
independent labels, arbitrary phrase chunks, no-op, faster follow-up, and phone
export text layers. Real PostgreSQL transaction regressions verify original
instructions survive a choice reply, later messages are excluded, and raw or
combined raw-plus-brief overflow rolls back draft/brief changes into recovery. Separate coverage checks pinned windows against catalog beats.

Three authored synthetic live Gemini Pro cases passed:

| Request | Observed compiled result |
| --- | --- |
| Three successive phrases with typewriter entrance | Three ordered, non-overlapping phrase bars |
| Each word fades in and out, then the next; location placeholders | Twelve sequential word bars, both fade phases, bottom-left labels, no duplicate hook |
| Word-by-word typewriter entrance; location placeholders | Twelve sequential word bars with typewriter entrances, bottom-left labels, no duplicate hook |

Inputs and raw model responses are committed in
`tests/fixtures/prompt_coverage/creation_composition.json`, with independent
request expectations. They are synthetic and contain no private conversation or
media. Offline replay of those captures is not additional live evidence.

The first broad-prompt live attempt timed out; its provider outcome remains
unknown with **$0.254644** reserved. The smaller shared text prompt succeeded in
three calls costing **$0.148832**. Cumulative KRI-524 spend is **$2.861438 settled**,
or **$3.116082 including the unknown reservation**, below the $5 cap. No unmetered
judges were used. A subsequent valid live follow-up, “Make those words twice as
fast; keep everything else,” produced twelve timing operations that shortened the
sequence from 10 to 5 seconds while retaining identities, text, styles, fades,
labels and source windows. This cost $0.106154. A preceding $0.069040 probe was
correctly rejected because its harness omitted editable capability families; it
is a harness failure, not a successful product test. Both are included above.

A local 10-second Skia/FFmpeg clip was rendered from the live fading-word program,
with white Inter text and a bottom-left placeholder. Phone export recipe tests
also pass. This is synthetic local rendering, not the user's video and not a
physical iPhone Save/export. Production behavior remains unverified until this
PR is approved, merged and deployed, then the affected creation flow is checked.

## Simulator verification

A dedicated iPhone 17 Pro simulator on iOS 26.5 ran the real native editor and
exporter. The generated draft is tied by an API equality regression to the
committed creation and faster-follow-up captures; Swift does not hand-author the
word sequence. Separate tests establish:

- Twelve individual word bars open, the playback clock advances, and all bars
  reopen. The UI exports an actual movie to the simulator Photos library.
- A later color edit uses the real editor Save payload and acknowledgment path,
  preserving word identities, timing, fades and placement through reopening.
  Only HTTP transport is mocked in this test.
- The actual server-compiled phone recipe exports through the native renderer
  using synthetic red/blue video with audio. Pixel checks observe the word fade
  in, fade out before its successor, and disappear after the five-second run;
  existing source-frame and audio assertions also pass.

The focused simulator run passed all four checks (two native, two UI). The
preceding native suite passed 1,425 tests with 19 skipped. The clean offline API
suite passed 23,776 tests with 31 skipped and 4 expected failures; the expanded
creation replay/fixture suite then passed 18 checks. Final integration counts
are recorded in PR evidence because main can advance independently.

These are synthetic simulator checks, not the user's private video, a physical
phone, or a deployed-production rerun. The UI uses synthetic color stills for
its preview/export; the separate compiler export uses video and tone. Captured
model responses make simulator runs repeatable offline; the fresh live model
probe above is separate evidence. The opt-in native compiler export requires
`KRIA_E2E_DIR`; the UI test is registered in the normal editor CI group.

Reproduce from the repository root:

```sh
src/apps/api/.venv/bin/python scripts/ios/kri-524-creation-e2e.py
```

The generator prints its output directory. Supply that directory as
`TEST_RUNNER_KRIA_E2E_DIR` to Xcode and run
`CreationWordSaveTests`,
`DeviceMontageRenderE2ETests/testCapturedCreationWordsRetimedThenExportOnTheIPhone`,
and `EditorUITests/testCapturedCreationWordsPlayReopenAndExport` using the
simulator workflow in `docs/runbooks/ios-development.md`.

## Oct 9 correction and verification update

Post-deployment testing exposed a validation step missing from the earlier
simulator harness. After PR #1499, retries with the same and new videos produced
`phone_plan_unsupported`; all three captured production jobs failed at the
same explicit gate, `missing confirmed on-screen text`. Those jobs were then
replayed offline through the corrected validation path: 3/3 passed the
guided and phone checks. The approved captures supplied `has_audio` metadata;
no media was downloaded, so this is captured-job replay evidence, not a claim
that the user's actual video rendered or that a new live Gemini call ran.

The shared sequence-evidence matcher now requires the full ordered,
contiguous wording, visible rows, and matching role/media. Guided receipts
retain IDs and the root role, while the phone compiler preserves validated
sequence IDs in its existing ID field; static IDs remain unchanged.
Nested sequences remain fail-closed, and this verification covers the single
creation composition pass only. No public schema, renderer, prompt, or
database change was made.

The affected API/replay checks passed (892 tests). Separately,
`make verify-kria` passed with 2,311 tests and 6 skips. Two simulator checks passed using a newly
generated corrected recipe against the already-built native binary: pixel
checked native export, plus UI block/play/reopen and Photos export coverage.
The strengthened `scripts/ios/kri-524-creation-e2e.py` now exercises the real
text/audio/order contract gates that the earlier harness omitted. An
independent review caught and removed a role-borrow bypass; final review had
no blockers. No new model spend was incurred. The follow-up PR is pending and
has not been deployed.

Current verification counts, exact revision and check results are recorded in
the PR evidence. Merge and deployment remain approval-gated.
