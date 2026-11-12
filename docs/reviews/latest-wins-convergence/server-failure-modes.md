# Editor draft head: server failure modes

Rule: **the server never creates, restamps, retires or repoints a draft head because a generation
moved**, with one exception: at Save, a stranded pinned v2 saved draft is replaced. Everything
else is `main`: a head on an old generation is not current and readers ignore it. The phone
converges on the latest by itself and replays its own staged AI edit.

Earlier designs moved the head at approval execution and phone publish. Four reviews each found a
way for old content to resurface or for a flow to break, and the intent-review repair flow (it
needs the approved draft to remain the head) failed 73 cases. Moving heads is not done.

"Current" is `kria/editor_head.py`: same job, and the column and the payload stamp name the
variant's generation (or the generation a phone publish was made from). The chat planner reads the
payload stamp, the Draft PUT and the saved draft read the column, as on `main`; the snapshot's
`head_current` is all of it. Proof: `tests/kria/test_editor_head_postgres.py`,
`test_repair_editor_heads_postgres.py` (real Postgres, real writers).

## Failure modes and where each is handled

1. **Manual Save over an AI head.** The head stays on the old generation (as on `main`);
   `head_current` is false, no response serves the AI text as current, the next chat turn builds on
   the saved text.
2. **Approval execution, phone publish, speech cut, autoplace, guided re-render.** No head row
   changes. After a publish a head on the published source stays current; after anything else it is
   not current.
3. **Intent-review repair and render retry** still see the approved draft as the head (they are
   untouched).
4. **Variant with only `render_finished_at`.** `main` stamped its heads column None / payload "";
   new heads are stamped on the baseline. `main`'s old rows keep `main`'s acceptance at the Draft
   PUT and saved-draft attach (`is_unstamped_main_head`).
5. **Save 409/412.** `detail` is unchanged; a sibling `problem {code, recovery,
   current_base_generation}` says `reload` only for `baseline_conflict`, `GUIDED_REVISION_STALE`,
   `deletion_target_stale`.
6. **Stranded pinned v2 saved draft** (409 `editor_draft_unavailable` forever on `main`). Save
   replaces the pinned ROW with the committed document (not a head; the pin moves only from that
   row to its replacement, so a chat head carrying an unsaved AI title is never committed or
   pinned); the timeline GET serves the same document and writes nothing, for the requested
   variant only. An empty-state variant keeps `main`'s 409.
7. **Repair CLI.** Dry run is read-only. `--apply` needs `--variant` and `--operation`; a current
   head is reported and refused; `keep-ai-lanes` exists only there and is refused for a head of
   another job.
8. **Lock order** Plan, Item, Job, Session, Turn, Draft; the Save replacement runs with Item then
   Job locked and writes no trace event.

## Known gap (same as `main`)

An unsaved AI draft is not carried by the server across any generation move it did not author on
that generation. The head is left on its old generation and ignored; the phone's own staged copy is
what carries it forward.
