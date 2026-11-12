# Editor draft head: server failure modes

Rule: **the server moves a draft head only when it KNOWS what the generation move did.** Otherwise
it behaves as `main` does: a head on an old generation is not current and readers ignore it. The
server never merges draft content and never guesses; the phone converges on the latest by itself
and replays its own staged AI edit.

Three independent reviews rejected earlier designs for one reason: each kept an old unsaved head
across a change to the committed video that the server did not understand, which brought old
content back over saved or rendered work. `main` never resurrects content, so the default is `main`.

"Current" is one predicate (`kria/editor_head.py`): same job, and the column and payload
generation both name the variant's render baseline (or the generation a phone publish was made
from). Draft GET/PUT, the timeline reads, the chat planner and `attach_saved_editor_drafts` use it.

Proof: `tests/kria/test_editor_head_deploy_safety_postgres.py`, `..._generation_postgres.py`,
`..._writers_postgres.py`, `test_repair_editor_heads_postgres.py` (real Postgres, real writers).

## The two moves

1. **Approval execution retires the head.** The render holds the approved edit; the new head is
   exactly what a fresh read creates (a v2 variant's sections come from the builder its Save
   uses, so `caption_meta`, `caption_text_color`, voiceover and music-treatment keys survive).
   Same transaction as the claim; redelivery inserts zero rows; the pinned `editor_draft` follows.
2. **Phone publish restamps the head**, only when column and payload base both equal
   `published_source_generation` and the head belongs to this job. Content is byte-identical.
   A head on any other generation is untouched.

## Excluded by construction

- **Manual Save over an AI head.** The head stays on the old generation (as on `main`); draft GET
  says `head_current: false`, no response serves the AI text as current, the next chat turn
  builds on the saved text. A v2 Save writes its own sections-only head.
- **Speech cut, autoplace, guided re-render, legacy dispatchers.** No restage; same as above.
- **Draft GET, PUT, undo and the chat turn never write a restage.** A PUT onto a head on an old
  generation is `draft_base_stale`, as on `main`.
- **An approval of an old draft after a manual Save.** `draft_still_current` follows only
  `approval_execution` and `phone_publish` rows and requires the head to still be current.
- **A discard survives a restamp.** A restage row carries `origin_revision`, so a phone state that
  names revision N as seen is still a discard when the server wrote N+1 with the same lanes.
- **Undo across an approval.** Committed work is not undone by draft undo:
  `draft_undo_unavailable`, `can_undo` false on the draft and the timeline read (one function).
  Across a phone-publish restamp, undo restores the earlier draft with the same restamp.
- **Stranded pinned v2 draft** (409 `editor_draft_unavailable` forever on `main`). Save retires
  it, then attaches; the timeline GET writes nothing and serves the committed state in the
  retired document's shape. An empty-state variant keeps `main`'s 409: its content exists only in
  its draft.
- **Save 409/412.** `detail` is unchanged; a sibling `problem {code, recovery}` says `reload` only
  for `baseline_conflict`, `GUIDED_REVISION_STALE`, `deletion_target_stale`.
- **Lock order** Plan, Item, Job, Session, Turn, Draft; the head is locked last and no trace event
  is written under the Job lock (the module imports no trace helper).
- **Repair CLI.** Dry-run is read-only. `--apply` needs `--variant` and `--operation`;
  `keep-ai-lanes` exists only there.

## Known gap (same as `main`)

An unsaved AI draft is not carried across a manual Save that did not include it, a speech cut,
autoplace or a guided re-render. The head is left on its old generation and ignored; the phone's
own staged copy is what carries it forward.
