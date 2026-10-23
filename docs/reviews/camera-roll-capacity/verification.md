# Camera-roll capacity and coverage verification

This is the initial bounded capacity fix for KRI-731/KRI-730. The multiple-idea
continuation and current prompt are recorded in [the current receipt](multiple-ideas-verification.json). The original request and
pre-implementation expectations are in [acceptance.md](acceptance.md). Safe
aggregate results and retained-log hashes are in [verification.json](verification.json).
Private uploads, descriptions, account IDs and generated copy remain outside Git.

## What this establishes

The real collection HTTP/index/search/correction path accepts 500 members,
reaches the last indexing page with the existing 32-item batch limit, and can
retrieve a relevant interval from the last source. Story input remains bounded
to 100 moments; this is not a 500-source model call. The original 50-source
case remains covered. A story still selects at most eight intervals/60 seconds.

Visual analysis may overestimate a short clip's end. The index now clips that
end to the real duration and consumes the analyzer's existing `best_moments`
as well as notable moments. It never moves a start, extends a clip or trims a
spoken word. The index version changes so old intervals are rebuilt from facts.
The private cached-data replay recovered usable windows for 19 videos versus
11 before; this establishes bounds and availability, not what the pictures show.

A default full-collection request waits before acquiring a provider call or
returning partial cached results. It rechecks after planning. An explicit native
partial choice remains possible, with omitted-video counts visible. That choice
has its own cache identity. Untimed photos do not block this video-only adapter.

The writer receives non-overlapping source-time coverage, rather than a count
that double-counts labels. It is directed toward developed, concrete ideas when
supported, with short atmosphere posts still allowed and no duration padding.
Prompt version is `2026-10-22.2`; model, provider, one-attempt/20-second envelope,
512 thinking budget and 4096 output-token limit are unchanged.

## Meaning and limits

The two private live writer previews cost $0.006656 total. The first selected
22.107 seconds but sounded like an advertisement. The final prompt selected
18.333 seconds and less promotional copy, but still missed the frozen developed
idea bar. Neither is a successful creative-quality evaluation or a creator
approval. Neither was offered, saved as a production idea, rendered or published.
No held-out generalization claim is made.

The broader KRI-731 incident remains open: useful ideas judged against the actual
footage, followed by the approved full-quality iPhone export/reopen flow and
rollout verification. Full-quality uploads of 500 actual originals have not been
benchmarked. API/native metadata capacity, replayed execution and a longer AI
sequence do not establish those outcomes.

## Earlier checks and environment failures

A 500-item test initially exceeded the default 30-second timeout because its
fixture flushed twice per item. Bulk fixture inserts now preserve every assertion
without increasing the timeout. A sandboxed run could not connect to localhost;
rerunning with local-database access passed. The first native UI run used a stale
expectation that partial analysis automatically offers stories; it was corrected
to assert waiting and explicit opt-in. The resume test checks the corrected group directly on the collection after
relaunch; reopening its sheet would duplicate the accessible field. A subsequent shared-simulator run was interrupted
by app installation/termination events; its failed results are retained separately.
The dedicated-simulator results below replace those interrupted attempts.

The final free suites passed 2,166 API/eval checks, 61 adjacent export/taste
journey guards and 117 iOS orchestration checks. Fifteen skips and seven expected
failures in the broad replay suite are retained in the receipt rather than
counted as passes. Contract generation matches all three frozen outputs.
A final 23-case collection run covers the lint-only message adjustment.

The dedicated simulator passed the six collection persistence/privacy checks,
including 500 **bound** saved-video metadata records through the actual protected
save/load methods. Four UI cases passed: partial opt-in/omitted-video disclosure,
research disclosure, taste management and replace/reject without rendering. The
resume field query uses `.firstMatch` plus a hittability assertion directly on
the resumed collection; its focused rerun is recorded separately. This is simulator
UI/data evidence, never native export evidence.

Disk exhaustion stopped an initial fresh-simulator launch. Only disposable
compiler/npm caches and interrupted result bundles from this camera-roll work
were removed; logs, final results, uploads and private media evidence were kept.
The renamed UI test is reflected in the iOS group inventory and the required
camera-roll journey mapping. No existing quality gate is removed.

Final focused collection UI rerun: **two passed, zero failures**. It checks
waiting, explicit partial selection and omission disclosure, then group correction,
removal and relaunch without upload. Alongside the six persistence tests and three
story UI cases, all eleven unique selected native checks have passing final
results. The earlier complete native run retains its failed selector result;
it is not relabelled as an all-green run. These are fixture/simulator checks,
with no physical-device export or production rollout claim.
