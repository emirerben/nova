# KRI-651: approved copy survives preparation

The October 13 marathon incident crossed three independent boundaries: typography
removed an approved emoji, creation composition substituted historical context for
approved wording, and the worker reported a preparation error as a device limitation.
The fixture retains the Unicode hook and failure shapes, but substitutes media ids,
storage paths and descriptive context. No production transcript, token or signed URL
is committed.

Before implementation, the failure matrix is: changed/removed opening or closing
copy; added contextual caption; missing emoji/accent/combining mark; shortened literal;
valid word or phrase segmentation; invalid sequence; saved-program tampering; first
generation versus retry; English versus Turkish failure; phone publication versus
server-only recipe success. Provider transport alone may be substituted in journey
tests. Final contract checks must remain enforced.

Timeline decisions (verify-editor-timeline): `compile_proposal_execution_plan` owns
ordered cuts and duration. No media/audio lane ripples, stretches or trims in this
change. Text operations remain bounded by the existing output windows; scrubbing and
resize policies continue to consume that same timeline. Composition is one immutable
snapshot/program, not an interactive gesture or extra undo step. The incident's
paired server recipe/native export fixture verifies wording, Unicode and timing.

Acceptance requires the new incident test to fail on the pre-fix base, pass on the
fixed tree, and run automatically for every affected path. Native output and the
exact TestFlight re-send are separate evidence levels; the ticket stays open until
production artifact and receipt are confirmed.

## Verification

- Base: `6636cae71`. `python3 scripts/reliability-gates.py red-control
  kri-546-montage-receipts --base 6636cae710d88496343b4609d55d1621e390aa27
  --verbose` fails at the original missing-emoji assertion, with provider transport
  inputs present. The extended gate runs automatically for the affected source paths.
- Fixed tree: 933 selected reliability checks passed with the worktree's isolated
  PostgreSQL on port 55451; 1,185 free replay evals passed (15 optional cases skipped).
  The 119 draft-building checks and EN/TR real-Postgres observer/receipt journey pass.
- Native export: `kri-651-approved-hook-e2e.py` followed by
  `DeviceMontageRenderE2ETests/testApprovedUnicodeHookExportsWithoutContextCaptionOnTheIPhone`.
  The exported MP4/frame preserves the complete approved Unicode hook, visibly draws
  the color emoji, and omits the background explanation. This is a synthetic-media
  native simulator export, not a production re-send of the user's original clip.
- Editor timeline: 209 Jest checks passed. The default parallel desktop run timed
  out loading cold dev-server pages; the identical browser suite passed with one
  worker (28 desktop and 42 mobile scenarios), without changing timeouts/assertions.
- Overlay verification: the actual production Docker image rendered all 28 fixture
  slots (62 PASS, zero WARN/FAIL/SKIPPED); report and montage inspected. Colima's
  `/private/tmp` bind mount was empty and its VM disk was full, so the same recipe
  ran in read-only image + temporary memory storage. No shared Docker data was deleted.
- Resource envelope: plain confirmed hook preparation makes zero text model calls;
  formatting retains the existing single-pass model timeout/budget. No paid eval ran.

Merge/deploy approval and the exact TestFlight re-send remain required. Confirm all
API/worker machines use the fixed revision, then read the finished artifact and its
checked receipt before closing KRI-651.
