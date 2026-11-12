# Editor draft head: server failure modes

Invariant: for each (plan item, variant) the `CreatorEditDraft` head sits on the
variant's current generation. Current means `render_generation_id` (falling back to
`render_finished_at`), the same value `variant_render_baseline` returns. The head's
column `base_generation_id`, its payload `base_generation` and `base_job_id` all agree
with it.

Today four predicates answer "is the head current" and disagree:
`fresh_editor_head_payload` (payload base only), `write_draft` (column base only),
`_same_saved_generation` (column base only), and the phone. Nothing moves the head
when the generation advances. Each mode below is a way the invariant breaks.

Covered at module level by `tests/kria/test_editor_head_generation_postgres.py`
(`restage_editor_head*`, `is_head_current`). Wiring-level modes are marked WIRING and
get their end-to-end proof in the call-site units.

1. **Approval advances the generation on a v2 variant.** An approved AI edit renders,
   the variant moves G1 to G2, and the head (an AI edit staged on G1) is left behind.
   The render already contains the edit, so the right result is the committed state
   with an empty diff (`contained=True`). Test: approval advance.
2. **Phone publish swaps the id.** Publishing replaces `render_generation_id` with the
   upload attempt id; the head stays on the pre-publish generation, which is current
   only through `published_source_generation`. Old builds and the column/payload
   comparison treat it inconsistently, so the head must be moved to the exact new id
   with its edits kept (`contained=False`). Test: phone publish.
3. **A legacy dispatcher bumps the generation under a staged head.** About 35 code
   paths write a generation id and none restage. The head must be healed by whoever
   notices next (read, write, or chat turn), not only by an eager hook. Test: legacy bump.
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
   job's committed state. Test: older job.
7. **An empty or v2 variant's pinned `editor_draft` row is stale.** The variant keeps
   `editor_draft.draft_id`, and `attach_saved_editor_drafts` ignores a row whose base is
   not the current generation, so timeline GET and Save answer
   `409 editor_draft_unavailable`. A restage must repoint the reference to the new head
   and persist it (JSON column replaced, never mutated in place). Test: pinned reference.
8. **Undo after a restage.** `undo_draft` copies the parent snapshot into a new row
   stamped with the current column base but the old payload base, so column and payload
   disagree and the head is not current. The predicate must see that, and a restage must
   repair it. Test: undo after restage. WIRING: undo itself should restage (unit A2).
9. **Column and payload bases disagree.** Any writer that stamps only one of them
   (undo above, `stage_editor_variant_draft` callers that pass a stale
   `target.generation_id`) leaves a head one predicate calls current and another stale.
   Current requires both. Test: payload-only mismatch.
10. **Variant has no `render_generation_id`.** The baseline falls back to
    `render_finished_at`; `_target` stamps `None`. The module uses the baseline, so a
    restage on such a variant writes the finished-at string into both fields. Test:
    baseline fallback. WIRING note for A2: `write_draft` compares against
    `target.generation_id`, which is `None` here.
11. **Non-editor head (initial or strategy draft).** There is no payload base to
    rebase. Restage leaves it alone. Test: non-editor head.
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
16. **WIRING: chat turn with a stale phone state.** `EditorStateStaleError` today;
    unit A7.
17. **WIRING: speech cut changes the timeline.** The cut rewrites slots, so overlaying
    a head's old `timeline_slots` would undo it; unit A4 must restage with the lanes
    the cut did not touch.

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
21. **Undo restores a parent built on an older generation.** The restored body is rebased
    (same-job lanes kept) so the new head is exactly current, instead of restaging a
    second row after the undo row. Test: undo of a head on an old generation.
22. **Job locked from a stale ORM copy.** The job lock must refresh the loaded job and the
    baseline, or a generation committed between target load and lock is missed. Test:
    lock re-reads.
23. **Chat planner reads the head through the same predicate.** `fresh_editor_head_payload`
    is now job-aware and requires column and payload to agree, so a head that GET /draft has
    not yet healed is treated as no head by a chat turn (unit A7 closes this).

## Writers

Wiring of modes 1, 2, 3 and 17 into the three writers that advance a variant's
generation: approval execution (`tasks/kria_runtime.py`), phone publish
(`routes/device_render.py`), speech-cut publication (`tasks/generative_build.py`).
Proof is `tests/kria/test_editor_head_writers_postgres.py`, driven through the real code
paths on Postgres.

24. **Approval claim retried after its own restage.** The claim commits `accepted`, then
    a crash (or broker loss) before dispatch makes the reconciler call the claim again.
    The claim requires the approved draft to be the head (`draft.is_head`). The restage
    just demoted it, so the retry would cancel the approval as stale and the committed
    render would never be dispatched. The retry must recognise a head that is the
    approved draft's direct restaged child. Test: claim twice, then dispatch.
25. **Approval restage adds rows on every retry.** Covered by 4 at module level; here the
    whole task runs twice (claim, dispatch, then the task again after `dispatched`) and
    the claim runs twice in the `accepted` window. Zero extra rows each time. Test: task
    twice.
26. **Approval that does not move the generation.** A title-only commit stages nothing
    and keeps the generation; a speech-cut approval only writes its control at claim time
    and moves the generation at publication. Restage must insert nothing in both.
    Test: unchanged generation.
27. **Phone publish with a head on the pre-publish generation.** The head must move to the
    exact attempt id and keep its lanes (`contained=False`: same document, new id). A
    publish on a variant with no editor head must create nothing. Test: publish.
28. **Speech cut publication rewrites slots.** Laying the old head's `timeline_slots` back
    over the cut would undo it; the head becomes the committed state (`contained=True`).
    The publication transaction took only the Job row, so the Item row must be locked
    first (Item before Job). Test: speech cut publication.
29. **Pending approval and a restage.** A pending approval names the head it was staged on
    and the generation (`target_generation_id`). Once the generation moves, the claim
    already refuses it (`approval_target_stale`), and `decide_approval` refuses a demoted
    head (`draft_stale`). A restage only demotes a head whose generation already moved,
    so it invalidates nothing that was still valid.
30. **NOT HANDLED HERE: other readers of `draft.is_head` on an approved draft.**
    `kria/render_retry.py:138` (retry of a failed phone render) and
    `services/intent_review_post.py:135` (`_repair_is_head`) require the approved draft to
    still be the head. After the approval's restage they stop being true. Retry would
    answer `render_retry_target_stale`. Needs a lineage-aware check in a follow-up unit.
