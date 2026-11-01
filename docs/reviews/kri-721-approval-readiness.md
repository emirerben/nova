# KRI-721: executable requirements before approval

## Acceptance plan (written before implementation)

The effective CreativeBrief remains the request authority. Existing renderer
capabilities, strategy policy, receipt checkers and render-contract fields supply
evidence. Readiness is a server-owned assessment, not a second creative plan.

Failure cases to exercise through the real runtime and approval boundary:

- An unknown requirement or a missing receipt must block approval.
- A known unsupported instruction must block even when the receipt calls it a
  format limitation; displaying a limitation is not permission to omit it.
- Requirements deferred by the legacy montage/speech receipt filters must still
  have a disposition before approval.
- A compound supported request must proceed without an extra question.
- English, Turkish and paraphrases must use the same typed requirements and gate.
- An explicit, revision-bound simpler-choice waiver may omit exactly its named
  requirements. Generic replies and stale choices cannot waive an instruction.
- A concrete creative promise cannot be treated as an open delegated choice.
- Draft/source changes and withdrawn capabilities invalidate prior readiness.
- A direct or repeated approval request cannot bypass a missing or failed check.
- Rejection leaves the existing draft, media and accepted output unchanged, creates
  no render attempt, and gives one actionable next step.
- Historical unstamped drafts and flag-off cohorts retain their existing behavior;
  once stamped, an approval remains guarded even if enrollment is disabled.

Baseline incidents: KRI-705's seven Talking requests and KRI-700's typed exposure
request. Their existing focused fixes are regression inputs, not assumed broken.
Add negative controls for a requirement with no checker and an explicitly
unsupported change that previously left approval available.

## Scope and timeline contract

This change controls readiness and consent. It does not edit a timeline. Existing
EditRecipeV2/compiler duration ownership, base-time lanes, ripple, scrub bounds,
resize and undo semantics remain the canonical behavior. Native exported-output
verification remains independent of this pre-approval check. KRI-722/723 own
prepared-execution persistence and broader montage planning.

## Evidence to retain

Record the fresh base, selected capability/cohort, red/green DB/HTTP cases and
physical-iPhone result bundle. Distinguish replayed provider decisions, native
output and deployed production. No production rollout or paid evaluation is
authorized by this implementation record.

## Implementation and rollout

`approval_readiness.py` assesses every live CreativeBrief ID with the existing
receipt checkers, selected-format capability manifest, runtime strategy policy/compiler and
render contract. A missing or unsupported requirement blocks approval. An unchecked
one blocks too, unless no draft check can settle it (KRI-983 amendment below).
A missing extracted brief also blocks; it is not an empty successful checklist.
Duration evidence comes from the existing validated source-window plan, not the
model's requested duration. Original camera sound uses the existing render-contract
commitment. This assessment establishes executable commitments, not output quality.

Readiness is bound to the brief/version, exact strategy or compiled editor payload,
source identities, prepared window evidence, capabilities and adapter declarations.
The approval endpoint recomputes it before dispatch. A separate execution digest
prevents deleting the draft report from bypassing the reader. Stamped approvals
remain guarded after enrollment is disabled. Source/capability changes cancel
approval, preserve the saved edit and ask the user to prepare it again.

The existing named simpler-version choice can waive only the exact offered IDs at
that brief version. A generic reply cannot waive them. Existing-video scoped chat
edits now extract the full request before checking the real compiled editor payload.
Retry and Undo approval emitters share the same readiness reader; Retry keeps its
saved content rather than asking a model to rebuild it.

New enrollment defaults off. Enable `KRIA_APPROVAL_READINESS_ENABLED` or
`KRIA_APPROVAL_READINESS_USER_IDS` only for accounts already enrolled in
CreativeBrief extraction and BriefBinding. Deploy readers to API and workers before
enrolling the pilot account. No production settings changed in this work. Historical
unstamped, unbound drafts retain their existing behavior. This does not promote
unverified render adapters or replace KRI-722/723 execution preparation.

## Capability and evidence matrix

| Case | Expected behavior | Evidence level |
| --- | --- | --- |
| Exact title + six-second source-bounded cut | Approval without an extra question | EN/TR phone-source and cloud provider replay, real compiler/DB/HTTP approval |
| Keep original camera sound | Explicit render-contract evidence | Provider replay, real contract |
| Wind-only noise removal (English wording) | Hold approval; name the missing change | Provider replay, real runtime |
| Wind-only noise removal (Turkish wording, no reader) | KRI-983: approve as `unverified`; named under "Have a look at these in the video:" | TR typed requirement replay |
| Typed shadow brightening | Hold approval even when legacy receipt calls it a format limitation | EN/TR typed requirement replay |
| Exact visual moment with no executable window | KRI-983: approve as `unverified`; named for a look after render | EN/TR typed requirement replay |
| "Original camera audio" with no keep verb or typed intent | KRI-983: approve as `unverified` | EN/TR typed requirement replay |
| Typed `audio_intent: noise_removal` | KRI-983: approve as `unverified` (no draft checker reads that intent yet) | EN/TR typed requirement replay |
| "Make it feel dreamy" (no checker anywhere) | KRI-983: approve as `unverified`; HTTP approve re-check passes | EN/TR provider replay, real HTTP approval |
| An `unverified` ask beside a freeze frame the draft cannot hold | Hold approval on the freeze; the other ask stays `unverified` | Provider replay, real runtime |
| An `unverified` ask when the renderer is withdrawn before approval | Reject at the HTTP approval re-check | Real HTTP approval endpoint |
| Lip-sync to the uploaded song, strategy `user_song` + `lipsync` | KRI-983: `executable`; approval card; HTTP approve passes | Prod-thread replay (953a5265), real runtime/DB/HTTP |
| Lip-sync asked, draft plays the song in the background | Hold with "This draft plays your song in the background; your clips aren't matched to its words."; no card when it is the only ask | Prod-thread replay, real runtime |
| "Change my request" on a held draft (new and legacy key) | Deterministic reply naming the held asks; zero provider calls | Prod-thread replay, real runtime |
| Existing-video title color + unsupported glow | Hold the entire partial edit (an editor draft never gets `unverified`: its diff is the check) | Real Copilot and extractor parsers, editor compiler, runtime/DB |
| Source generation or capability withdrawn | Cancel direct/repeated approval; no dispatch | Real HTTP approval endpoint |
| Report/requirement evidence removed | Reject even after enrollment is disabled | Real DB + HTTP approval |
| Named omission vs generic agreement | Only exact offered choice supersedes that requirement | Real brief persistence and runtime |
| Missing extracted request | Hold approval | Red/green real runtime control |
| Historical flag-off draft | Existing behavior | Real runtime regression |

The EN/TR cases verify that equivalent typed requirements encounter the same gate.
They do not measure a live model's translation or requirement-recall accuracy.
Broader montage/speech/cloud/slide execution proof remains in KRI-722–725.

## Verification

Fresh implementation base: `b3a2a9edbd50a7d1d9a88791526dbf443ecf2e32`.
Scratch PostgreSQL: `nova_kri721_approval_readiness_test`.

- Baseline red: the wind-noise request received approval without executable evidence
  (`/private/tmp/kri721-baseline.log`).
- Scoped-edit red: only Copilot ran, so the remaining request could be omitted
  (`/private/tmp/kri721-scoped-red.log`).
- Phone-source red: the selected phone renderer materialized an original-audio
  default that the new strict gate compared with the pre-normalized proposal
  (`/private/tmp/kri721-phone-red.log`). The writer now canonicalizes against the
  proposed renderer before approval. The reader still refuses later changes.
- Missing-extraction red: no extracted request was accepted as a successful empty
  checklist (`/private/tmp/kri721-empty-brief-red.log`).
- Final focused command: `bash scripts/check-api.sh test tests/kria/test_approval_readiness_postgres.py tests/kria/test_runtime_simpler_version_loop.py tests/kria/test_render_retry_postgres.py tests/kria/test_plan_review.py tests/gates/test_reliability_gates.py -q -x` — **171 passed**.
- `bash scripts/preship-check.sh` — scoped lint/format and **622 selected incident
  regression tests passed**. Dedicated KRI-721 CI journey also selects the new tests.
- CI selector contracts: `python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v` — **66 passed**. The new journey is explicitly included in the shared-planner selection expectation.
- No prompt/model changes or paid model calls. Ordinary first-draft checking adds
  no model call. Previously unextracted scoped chat edits add one bounded call to
  the existing Brief Extractor. Live latency and spend for that extra call are not
  measured here.

Physical approval UI evidence is recorded below separately from exported-output
verification and production deployment.

## Final physical iPhone approval proof

**Passed 2026-10-19 08:00:23 UTC**, iPhone 13 Pro, isolated
`com.kria.app.qa.kri721`, with source code from `3bd820014` (branch HEAD
`15c98d0fa` also contains the CI expectation fix). The normal iOS application
source and its existing explicit QA launch mode were used. The production app,
its account and saved edit were not touched by this check.

1. Sent: `Make a six-second edit with only the title "Gameday." and remove only
   the wind noise.` The UI offered the named omission and no Create approval.
   Server proof: title/duration executable, only wind removal unresolved, no
   policy-level error, zero approvals and zero dispatches.
2. Tapped the exact `Make this simpler version` choice. Server proof: brief v2
   superseded only r3; r1/r2 remained executable; exactly one pending approval.
3. Tapped `Create this video` once. The real HTTP approval endpoint accepted and
   published exactly once. No additional model call, no worker error.

Three physical XCUITest phases passed, with mandatory API/DB assertions between
phases on the Mac. This retained and strengthened the negative control while
avoiding the test runner's separate local-network permission problem. Only the
provider transport and broker publisher were substituted. No internal policy,
compiler, receipt, source-window validator or approval logic was mocked.

Artifacts retained outside Git:

- `/private/tmp/kri721-qa/verified-summary.json`
- `/private/tmp/kri721-qa/final-phase1.xcresult`
- `/private/tmp/kri721-qa/final-phase2.xcresult`
- `/private/tmp/kri721-qa/final-phase3.xcresult`
- `/private/tmp/kri721-qa/final-flow.log`
- `/private/tmp/kri721-qa/run_checked_phases.py` and `ui/Probe.swift`

This verifies approval eligibility and exact consent on a physical phone using
replayed provider output and a real local API/database. Dispatch intentionally
stopped after the approval boundary: **no new native export, live-model result or
production deployment is claimed**. The phone was released after the QA app
terminated; production remains unenrolled.

## KRI-983 amendment: asks nothing can check before render

Prod thread 953a5265 (2026-10-27) held "Create a lipsync" because no draft checker read a
lip-sync ask. The same rule held every ask with no checker ("cinematic", "only the best
clips", "make it feel dreamy"). KRI-774 and KRI-945 each patched one instance.

- `brief_checks.wants_lipsync` checks a lip-sync ask against the draft. It is met when the
  creator's song is pinned to the draft's media and the strategy plays it as `user_song`
  with `song_sync: lipsync`. Otherwise it is a plain "Couldn't" (no song, song unused, song
  only in the background).
- A new disposition, `unverified`, covers an unchecked receipt on an ask no draft check
  can settle. That is an ask with no checker, or one whose only checker reads the finished
  video (`judged_at_render`). Approval goes ahead (`APPROVABLE`), the draft reply names it
  under "Have a look at these in the video:", and the render-time review judges it again.
- Still held: a checked miss, a policy problem, a prepared-story row, an editor draft, a
  researched-text ask and a keep-text-off mark. The last two are proven outside
  `_has_checker` (research preview, the mark route).
- A held draft reads "I can't make this yet:" with one plain reason per ask. The card
  offers "Make it without these" (its description lists what it leaves out) or "Change my
  request". "Change my request" and the legacy `different_approach` key are answered
  without a model call. Under readiness the card is not offered when leaving out the
  unmet asks would leave none of the creator's asks.

Known gaps, not fixed here: a typed `audio_intent: noise_removal` and the Turkish
"rüzgâr sesini kaldır" have no draft checker, so they are now approved as `unverified`
instead of held. A text ask whose look only the finished video shows still blocks,
because its words and its look share one receipt (closed by KRI-1113 below).

KRI-1113 (prod thread 83f2dcaa, 2026-10-31): the "Animate the title" chip was held as
"I can't confirm this draft does it yet." A text ask with look facts is now `unverified`
when its words alone are checked and met on the draft (`brief_checks.text_words_met`);
the render-time review still judges the look. Words the draft lacks still block. A
curved-text ask is unchanged (its shape is not a look fact). "Make it without these" now
also holds when the Main Creator restates exactly the offered asks on the answering turn;
any other model update on that turn still needs its own offer.

