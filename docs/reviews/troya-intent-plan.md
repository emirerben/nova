# Troya visitor-guide intent and plan handoff

The original request asked to highlight what travelers could see in the area and
museum. The creator confirmed that the untouched AI export was a partial miss:
visual coverage was present, explanatory text was absent, and car footage closed
the edit. No historical count or era has been independently verified.

## Changes

- Creation text receives the actual compiled source/output windows and bounded
  footage descriptions through the shared clip-understanding projection. Storage
  locations and arbitrary raw metadata are excluded. Evidence is context, not
  literal copy; unknown historical details must not be invented.
- Music-led phone montages consisting of video clips can carry an `editorial_plan`:
  viewer purpose, ordered beats with owned clip IDs, and optional text purposes.
  The complete clip set is checked before approval and again at compilation.
  Creator order and sequence constraints take precedence. Old snapshots omit the
  new field and retain their existing serialization.
- Approval summaries use the executable window duration and bound story beats.
  A model-selected duration remains distinct from a creator-requested duration.
- The worker retains the editorial plan, and the render contract checks its
  picture order. Each explanatory beat must have readable non-title text within
  its planned time range. That check verifies presence and timing, not meaning.
- The length planner reserves three seconds per explanatory beat within the existing
  duration budget before approval. Impossible explanation timing is returned as a
  question instead of an approvable draft. Exact beat timestamps also reach the writer.
- Both creation entry points use the shared writer. Other formats retain their
  existing timeline planner; no web research was added.

## Verification

Run from the API directory:

```sh
.venv/bin/pytest tests/evals/request_following/test_troya_context_journey.py -q
.venv/bin/pytest tests/evals/ -q
```

The incident journey was written before the corresponding implementation. It
first failed on missing footage context, then missing executable editorial order,
then the silent title-only result. It covers source identity/coverage, immutable
replay, creator sequence conflicts, and explanatory text omission.

A private local replay uses all 28 saved Troya clip records. It rejects the
recorded title-only response, preserves an authored test sequence with the car
second and museum last, and retains the corresponding explanatory text through
snapshot serialization and canonical compilation. Its positive model responses
are authored fixtures, **not evidence of live model behavior**.

The user authorized a live replay under a $2 cap. The saved original request and
28 clip descriptions produced a live editorial plan and five footage-grounded
captions. The first replay exposed missing per-beat timing and decimal rounding;
the writer now receives exact beat boundaries and a reading-time rule. Replaying
the saved 18-second music window also exposed insufficient time for short beats;
the length planner now reserves explanation time before approval.

The final 18-second native preview passes the production AVFoundation exporter on
macOS. Midpoint samples from every one of the 28 source windows match the planned
footage, and all five captions were visually inspected. Snapshot, compiled plan,
recipe, output hashes, contact sheet and pixel report are retained as private local
verification artifacts. They contain user footage and are not committed.

Validation: 1,227 replay/length checks passed (15 skipped), 1,044 database-backed
incident checks passed (2 expected failures, no environment skips), 159 focused
main-planner/render-contract checks passed, and native export, scoped lint and
pre-ship checks passed. An upstream planner change must be integrated before landing.

Limits: the original-request live planner starts with the drive. Three attempts
to verify the creator's later strong-hook/car-second correction disconnected, so
that creative correction is not live-verified. The offline car-second fixture is
still authored evidence only. The native test uses analysis copies, omits the catalog song while retaining
camera audio, and runs on macOS rather than a physical phone. It proves text/order fidelity to
a frozen test snapshot, not full-resolution or soundtrack delivery, nor a new
creator approval in production. No web research or production changes were made.

Confirmed model usage is $0.609444; unknown calls retain $1.341940 in reservations.
Total counted against the approved $2 cap is $1.951384. Further paid calls stopped.

## Explicit opening correction and audio verification

The creator subsequently required a strong visual hook followed immediately by
the car footage. A direct revision of the local replay plan now opens on the
Trojan Horse replica (0–3s), puts the car second (3–6s), preserves all 28 clips,
and ends inside the museum. Five captions were retimed to their pictured subjects.
This is a user-directed revision, not a claim that a fresh model chose the order.

The corrected canonical plan now includes the saved song reference: Daft Punk's
Instant Crush, 130–147.989s. Compiler-v6+ catalog songs are reference-only by design;
the posting export retains camera audio and the selected song is added on the
publishing platform. The earlier description of the export as silent was inaccurate.
A separate local listening-preview recipe adds that exact pinned music section
at half gain for headroom, without changing the production recipe's audio policy.

Both outputs rendered through AVFoundation. All 28 midpoint source-window checks
passed on both. Five captions were visually inspected. The listening-preview audio
correlates 0.9994 with the pinned song section at zero measured offset and without
clipping. Native camera audio correlates 0.9854 with the approved concatenated
source windows at zero offset. All 22 focused Troya/song-reference checks passed.
Private local artifacts retain hashes, recipes, source/audio receipts, screenshots,
and verification reports. No new paid calls, production writes, deployment, or web
research were performed. This remains a proxy-resolution macOS verification, not
a physical-iPhone export from full-resolution originals.

## Source-backed local revision — 2026-10-19

Applied the user's requested web research to a separate local artifact at
`/private/tmp/nova-session-review/fresh/troya-researched/`. The corrected horse-first
opening, car second, and all 28 clip positions are retained. The first 26 source
and output windows are unchanged; the last two artifact shots use 2.88s and 2.4s
of the existing sources to support readable captions (22.043s total).

Official Turkish Museums site chronology supports successive settlement layers
and the Bronze Age, Greek and Roman periods. The Çanakkale culture ministry's
museum catalogue identifies the filmed Polyxena Sarcophagus and dates it to
520–500 BCE. Its catalogue photograph matches the distinctive broken lid and
figural relief in the footage. Gallery-wide copy describes regional finds; the
last sculptures remain unnamed because their precise identities are unverified.

Sources:
- https://www.turkishmuseums.com/museum/detail/2020-canakkale-troya-orenyeri/2020/1
- https://canakkale.ktb.gov.tr/TR-70510/troya-muzesi.html

Evidence: `verification-report.md`, `verification-report.json`, `research/claims.json`,
`contact-sheet.jpg`, two native recipes/MP4s and audio/frame comparison reports in
that private artifact directory. Both native AVFoundation exports passed, with all
28 midpoint frame comparisons matching per output and all seven caption frames
inspected. Song correlation 0.9994; camera-audio correlation 0.9857; zero measured
audio offset. Same track/start (130s), extended end (152.043s) for reading time.
Posting export preserves camera audio and external song reference; the separate
listening preview mixes the song with 6dB headroom.

This is a deterministic, source-reviewed local edit, not evidence that an autonomous
research agent generated it. No new model calls, production writes, or deployment.
Native verification uses analysis copies on macOS, not full-resolution originals
on a physical iPhone. Raw media and private claim/source records remain outside Git.
