# Editor draft head: server failure modes

Invariant: for each (plan item, variant) the `CreatorEditDraft` head sits on the
variant's current generation. Current means `render_generation_id` (falling back to
`render_finished_at`), the same value `variant_render_baseline` returns. The head's
column `base_generation_id`, its payload `base_generation` and `base_job_id` all agree
with it.

Four predicates used to answer "is the head current" and disagree:
`fresh_editor_head_payload` (payload base only), `write_draft` (column base only),
`_same_saved_generation` (column base only), and the phone. Nothing moved the head when the
generation advanced. Each mode below is a way the invariant breaks. The head follows a moved
generation by exactly one named operation (`restamp`, `retire`, `drop_lanes`, `heal`; see
`docs/pipelines/kria-agent-runtime.md`); the server never merges draft content.

Covered at module level by `tests/kria/test_editor_head_generation_postgres.py` and
`tests/kria/test_editor_head_writers_postgres.py`; heads there are built the way production
builds them (an AI chat head through a real draft turn, a phone-written head through
`write_draft` with a full `sections` body).

1. **Approval advances the generation on a v2 variant.** An approved AI edit renders,
   the variant moves G1 to G2, and the head (an AI edit staged on G1) is left behind.
   The render already contains the edit, so the head is `retire`d: the committed document,
   no flat lane, empty diff. Test: approval advance.
2. **Phone publish swaps the id.** Publishing replaces `render_generation_id` with the
   upload attempt id; the head stays on the pre-publish generation, which is current
   only through `published_source_generation`. Old builds and the column/payload
   comparison treat it inconsistently, so the head is `restamp`ed: same content, the exact
   new id. Test: phone publish.
3. **A legacy dispatcher bumps the generation under a staged head.** About 35 code
   paths write a generation id and none restage. The head must be healed by whoever
   notices next (read, write, undo), by `heal`: committed `sections`, flat lanes kept. A chat
   turn and the timeline GET see the same heal in memory. Test: legacy bump.
4. **A retried Celery approval task restages twice.** The second call must insert zero
   rows: a no-op whenever column base and payload base already equal the baseline
   exactly. Test: retry idempotency.
5. **Concurrent `PUT /draft` races the restage.** A writer holding the head row lock
   inserts a newer revision. A restage that read the head before the lock would rebase
   the superseded revision and drop the creator's edit. The head must be read after the
   lock is granted, and the writer's revision is what gets rebased. Test: concurrent writer.
6. **The head belongs to an older job.** After a regenerate, the item points at a new
   job but the head still names the old one. It is never current, and old-job lane
   content cannot be replayed onto a different timeline, so the rebase takes the new
   job's committed state (`retire`, whichever operation the writer named). Test: older job.
7. **An empty or v2 variant's pinned `editor_draft` row is stale.** The variant keeps
   `editor_draft.draft_id`, and `attach_saved_editor_drafts` ignores a row whose base is
   not the current generation, so timeline GET and Save answer
   `409 editor_draft_unavailable`. A restage must repoint the reference to the new head
   and persist it (JSON column replaced, never mutated in place). Test: pinned reference.
8. **Undo after a restage.** `undo_draft` copies the parent snapshot into a new row
   stamped with the current column base but the old payload base, so column and payload
   disagree and the head is not current. Undo now lands its restored row on the baseline.
   Test: undo after restage.
9. **Column and payload bases disagree.** Any writer that stamps only one of them
   (`stage_editor_variant_draft` callers that pass a stale `target.generation_id`) leaves a head one predicate calls current and another stale.
   Current requires both. Test: payload-only mismatch.
10. **Variant has no `render_generation_id`.** The baseline falls back to
    `render_finished_at`; `_target` stamps `None`. The module uses the baseline, so a
    restage on such a variant writes the finished-at string into both fields. Test:
    baseline fallback.
11. **Non-editor head (initial or strategy draft).** There is no editor payload to move.
    Restage leaves it alone. Test: non-editor head.
12. **No head yet.** Restage is a no-op and must not create one (bootstrap owns that).
    Test: no head.
13. **Lock order.** Plan, Item, Job, Session, Turn, Draft. The caller holds Item and Job;
    restage takes only the head lock, last, and never writes a trace event while it
    holds the Job row (`record_pipeline_event` self-deadlocks under FOR UPDATE). Not
    observable from rows; enforced by the module having no import of it.
14. **Async and sync twins diverge.** The async entry runs the same code through
    `run_sync`, so there is one implementation. Test: async twin.
15. **WIRING: a path writes a generation and never calls restage.** Heal-on-read in
    `read_or_bootstrap_draft`, `write_draft` and `attach_saved_editor_drafts` covers
    this (units A2, A5).
16. **Chat turn with a stale phone state.** Unchanged: `EditorStateStaleError`, the phone
    converges and resends. Only a head that is off the baseline is no longer read as
    "no head" (mode 23).
17. **Speech cut changes the timeline.** The cut rewrites slots, so a chat head's flat
    `timeline_slots` / `split_screen_sections` would undo it: `drop_lanes` removes exactly
    those and keeps the other flat lanes (an unsaved text edit survives).

Found while wiring unit A2 (observable behaviour in the same test file):

18. **Draft writers stamp `render_generation_id` only.** `_target` and
    `editor_variant_target` stamped `None` on a variant that has only
    `render_finished_at`, so its head was never on the baseline and would be restaged on
    every read. Fixed at the source: both stamp `variant_render_baseline`. Test: finished-at only.
19. **Variant that never finished a render.** Baseline is `""`; heads carry the empty
    stamp and are current. A read must not insert a row. Test: never finished.
20. **Heal before the If-Match check turns every stranded write into `draft_stale`.** The
    precondition is checked against the head the client saw, then the head is moved, then
    the write applies. Test: write onto stranded head; stale revision still `draft_stale`.
21. **Undo restores a parent built on an older generation.** The restored document is the
    parent moved with `heal`, so the new head is exactly current instead of a second row
    restaging the undo row. Test: undo of a head on an old generation.
22. **Job locked from a stale ORM copy.** The job lock must refresh the loaded job and the
    baseline, or a generation committed between target load and lock is missed. Test:
    lock re-reads.
23. **Chat planner reads the head through the same predicate.** `fresh_editor_head_payload`
    is job-aware and requires column and payload to agree. A head of this job that is off the
    baseline is returned healed in memory (`latest_editor_head_payload`, operation `heal`,
    no write), so the AI draft on G1 is the base of the next turn after the generation moves.
    A head from another job is still never replayed. Test: chat turn after the generation
    moved builds on the AI's edit.

## Writers

Wiring of modes 1, 2, 3 and 17 into the three writers that advance a variant's
generation: approval execution (`tasks/kria_runtime.py`, `retire`), phone publish
(`routes/device_render.py`, `restamp`), speech-cut publication (`tasks/generative_build.py`,
`drop_lanes` of the timeline lanes). Proof is `tests/kria/test_editor_head_writers_postgres.py`,
driven through the real code paths on Postgres.

24. **Approval claim retried after its own restage.** The claim commits `accepted`, then
    a crash (or broker loss) before dispatch makes the reconciler call the claim again.
    The claim used to require the approved draft to be the head; the restage just demoted
    it, so the retry would cancel the approval as stale and the committed render would never
    be dispatched. The claim now asks `draft_still_current`. Test: claim twice, then dispatch.
25. **Approval restage adds rows on every retry.** Covered by 4 at module level; here the
    whole task runs twice (claim, dispatch, then the task again after `dispatched`) and
    the claim runs twice in the `accepted` window. Zero extra rows each time. Test: task
    twice.
26. **Approval that does not move the generation.** A title-only commit stages nothing
    and keeps the generation; a speech-cut approval only writes its control at claim time
    and moves the generation at publication. Restage must insert nothing in both.
    Test: unchanged generation.
27. **Phone publish with a head on the pre-publish generation.** Same document, new id:
    `restamp`, content byte-identical. A publish on a variant with no editor head must
    create nothing. Test: publish.
28. **Speech cut publication rewrites slots.** Laying the old head's flat `timeline_slots`
    back over the cut would undo it, so `drop_lanes` removes the timeline lanes and keeps the
    rest (an unsaved flat text edit survives). The publication transaction took only the Job
    row, so the Item row is locked first (Item before Job). Test: speech cut publication.
29. **Pending approval and a restage.** A pending approval names the head it was staged on
    and the generation (`target_generation_id`). Once the generation moves, the claim
    already refuses it (`approval_target_stale`), and `decide_approval` refuses a demoted
    head (`draft_stale`). A restage only demotes a head whose generation already moved,
    so it invalidates nothing that was still valid.
30. **Other readers of `draft.is_head` on an approved draft.**
    `kria/render_retry.py` (retry of a failed phone render) and
    `services/intent_review_post.py` (`_repair_is_head`) asked "is the approved draft still the
    head", and after the approval's restage the answer is no. One helper,
    `editor_head.draft_still_current`, answers for all three callers: the draft is the head, or
    the head descends from it through restage rows whose reason is `approval_execution` or
    `phone_publish`. A restage caused by a manual Save, a speech cut, a repair or a heal breaks
    the lineage, so an old approval never runs on top of the creator's Save. Test: approve,
    execute, publish (current), manual Save (not current).

## Save route and timeline

31. **Save on a stranded pinned draft.** A v2 / empty variant's `editor_draft` names a head whose
    generation moved. `attach_saved_editor_drafts` skips it, `saved_editor_draft` raises
    `409 editor_draft_unavailable`, and the creator's Save dead-ends. Save already holds Item then
    Job, so it `retire`s the head before attaching (the Save's own document follows). Test:
    stranded v2 Save.
32. **Manual Save bumps the generation under a chat head.** Legacy Save moves the variant to a new
    generation and writes no draft row, so a staged head lags and its flat lanes would be
    re-staged over the saved document. Save `retire`s the head in the same transaction. The v2
    Save writes a sections-only head of its own, so no flat lane survives either. Tests: legacy
    and v2 Save over an AI head, including a level the AI left unsaved.
33. **Save 409/412 with prose or object detail.** The phone reads every 409 as "newer version".
    `detail` stays byte-identical; a sibling `problem {code, recovery, current_base_generation}`
    names the cause. `recovery` is `reload` for `baseline_conflict`, `GUIDED_REVISION_STALE` and
    `deletion_target_stale`, so the other refusals (cancelled video, initial render in progress,
    source catalog invalid, draft requires supported client, draft unavailable) are never
    answered by reloading. Test: every 409 the route can produce carries a stable code.
34. **Timeline GET on a v2 variant whose pinned draft is off the baseline.** It used to raise
    `409 editor_draft_unavailable`. GET stays write-free, so it serves the pinned row moved in
    memory with `heal` (the same document GET /draft persists), with the `draft` key present.
    Test: timeline and draft agree, no row written.
35. **Timeline GET on an empty-state variant whose pinned draft is off the baseline.** The
    committed variant is the last non-empty render, so the draft-only content is what is served:
    the pinned row moved with `heal` keeps its own `sections`. The 409 remains only when no
    draft row backs the variant.
36. **Heal lock order.** The Save route already holds Item then Job; the restage takes the head
    last and fires no trace event. A Save that restaged and then fails validation rolls the
    restage back with the rest of the transaction.

## `PUT /draft` and Undo

37. **Stale `PUT /draft` after a generation moved.** The head is healed first, then If-Match is
    checked against the head as it is now. The heal's row is not the one the client read, so
    the answer is `draft_stale` (recovery refresh) and the heal is committed before the refusal.
    The phone re-reads and converges locally. Test: stale PUT writes only the heal.
38. **A body that names an old `base_generation`.** The head is current and the If-Match matches,
    but the body was authored on an older generation. It is `draft_stale`, never silently
    restamped onto the current one (`_on_target_generation` is gone). No server-internal caller
    relied on that restamp: `creator_comparison_persistence` and the tests write bodies that
    echo the head they just read. Test: stale body writes nothing.
39. **Undo after a restage.** Undo skips restage rows (the target is the parent of the nearest
    non-restage row) and restores that document moved with `heal`. When it equals the document
    the head already holds (an approval's render contains the edit) it answers
    `draft_undo_unavailable`, cancels nothing, and `can_undo` is false in the draft and the
    timeline reads, both computed by `drafts.can_undo`. Tests: undo after a heal restage;
    undo after an approval render.
40. **Chat turn after the generation moved under an AI head.** The turn builds on the AI's edit,
    not on "no head" (mode 23). A stale phone state is refused exactly as on `main`.

## Repair CLI (`app/cli/repair_editor_heads.py`)

One-off tool for items whose head was stranded before the server healed on its own. It is
built and tested locally only; a production run needs the owner's approval.

41. **Dry-run writes.** The default must change nothing: no row added, no head demoted, the
    variant's pinned `editor_draft` untouched. The session is read-only and never commits.
    Test: row count and head id before and after.
42. **The dry run hides what `--apply` would do.** It prints, per variant, the head's flat lane
    names, whether it has `sections`, and what `heal` (kept flat lanes; `sections` becomes the
    committed document) and `retire` (no flat lane kept) would do. Test: dry-run report.
43. **Which operation.** `--apply` heals by default; `--operation retire` is for a stranding
    where the committed video is known to contain the draft (approval rendered, then the phone
    published). Test: apply with each operation.
44. **Second `--apply` adds rows.** `restage_editor_head_sync` is a no-op once both stamps
    equal the baseline, so a second run reports nothing to do and adds zero rows. Test:
    apply twice.
45. **Lock order.** Item, then Job, then the head (taken inside the restage). One transaction
    per item.
46. **Item without a current job, variants, or head.** Reported as nothing to do, exit 0;
    an unknown item or variant key is an error, exit 2. Test: each.
47. **Head from another job.** It becomes the committed document of the current job under any
    operation (its slots do not exist on this timeline); the dry run names it "other job".
    Test: foreign head.
48. **Head without an editor payload** (strategy or initial draft). Never touched; the
    list query skips it too. Test: non-editor head.
49. **`--list-stranded` on a job whose `variants` is not an array.** Must not error; the
    query guards with `jsonb_typeof`. Test: scalar `variants`.

## No server-side merge

An earlier design merged an unsaved head with newer committed state on the server (first the
whole `sections` overlay, then a per-lane three-way over recorded `base_lanes` hashes). Two
independent reviews found it resurrected committed work or dropped edits, because the server
has no reliable lane model. The merge is gone; the phone owns it. These review sequences are
excluded by construction:

- **Legacy flat head, then a manual Save.** Save `retire`s the head: the saved document is the
  head and no flat lane is left to re-stage over it.
- **Lanes the accessor never reads (`mix`, `title`, `color_moments`, ...).** Nothing compares
  lanes, so an unseen committed change cannot look unchanged. An AI level left unsaved loses to
  the level the creator Saves (`retire`).
- **An iOS-shaped stale `PUT`.** A full-`sections` body can never be mistaken for a few dirty
  lanes, because it is never merged: a heal makes it `draft_stale`, and so does a body naming
  an old `base_generation`.
- **Sections-only creator edits.** A head with no flat lane is the committed document after a
  `heal`; it is never rebuilt from older sections.
- **Cleared keys.** Removing a key cannot be lost in a merge that does not exist.
- **Coarse lanes dropping unrelated edits.** `drop_lanes` names exactly the timeline lanes a
  speech cut rewrote; every other flat lane stays.
- **Rows written before this change.** They have no new field and need none: the operation is
  chosen by the writer, not by comparing against a recorded base.

Cost: a `heal` of a head with no flat lane discards that head's unsaved content in favour of
the committed document. A phone-written head that is off the baseline after an unnamed writer
is therefore replaced; the phone re-reads (`draft_stale`) and converges against the document it
still holds.
