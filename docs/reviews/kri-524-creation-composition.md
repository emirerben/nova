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
three calls costing **$0.148832**. Cumulative KRI-524 spend is **$2.686244 settled**,
or **$2.940888 including the unknown reservation**, below the $5 cap. No unmetered
judges were used.

A local 10-second Skia/FFmpeg clip was rendered from the live fading-word program,
with white Inter text and a bottom-left placeholder. Phone export recipe tests
also pass. This is synthetic local rendering, not the user's video and not a
physical iPhone Save/export. Production behavior remains unverified until this
PR is approved, merged and deployed, then the affected creation flow is checked.

Current verification counts, exact revision and check results are recorded in
the PR evidence. Merge and deployment remain approval-gated.
