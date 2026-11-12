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
30. **Other readers of `draft.is_head` on an approved draft** (handled in "Lineage and heal" below).
    `kria/render_retry.py:138` (retry of a failed phone render) and
    `services/intent_review_post.py:135` (`_repair_is_head`) require the approved draft to
    still be the head. After the approval's restage they stop being true. Retry would
    answer `render_retry_target_stale`. Needs a lineage-aware check in a follow-up unit.

## Lineage and heal

30 (continued). `kria/render_retry.py` and `services/intent_review_post.py` asked "is the
approved draft still the head" and the approval claim asked it with a one-off accepted-only
exception. After the approval's restage the answer is no, so a retry of a failed phone render
answered `render_retry_target_stale`. One helper, `editor_head.draft_still_current`, now answers
for all three: the draft is the head, or the head descends from it through restages only (each
link moved the generation, which no creator write does, because a write heals first) and adds no
pending change the approved draft did not carry. Test: approve, execute, restage, retry accepted.

## Save route and timeline

31. **Save on a stranded pinned draft.** A v2 / empty variant's `editor_draft` names a head whose
    generation moved. `attach_saved_editor_drafts` skips it, `saved_editor_draft` raises
    `409 editor_draft_unavailable`, and the creator's Save dead-ends. Save already holds Item then
    Job, so it restages (same-job lanes kept) before attaching. Test: stranded v2 Save.
32. **v1 manual Save bumps the generation under a chat head.** `prepare_editor_commit` moves the
    variant to a new generation and no draft row is written, so a staged head lags. Save restages
    in the same transaction (`contained=True`: the Save is the committed state). Test: v1 Save.
33. **Save 409/412 with prose or object detail.** The phone reads every 409 as "newer version".
    `detail` stays byte-identical; a sibling `problem {code, recovery, current_base_generation}`
    names the cause. `recovery` is `reload` only for `baseline_conflict`, so the other refusals
    (cancelled video, initial render in progress, source catalog invalid, draft requires supported
    client, deletion target stale, draft unavailable) are never answered by reloading. Test: every
    409 the route can produce carries a stable code.
34. **Timeline GET on a v2 variant whose pinned draft is not current.** It used to raise
    `409 editor_draft_unavailable`. GET stays write-free, so it serves the committed variant state
    (no `draft` key, `base_generation` = baseline). Test: timeline before any heal.
35. **Timeline GET on an empty-state variant whose pinned draft is not current.** The committed
    variant is the last non-empty render, not the saved empty state, so serving it would show the
    deleted clips back. No write-free answer is correct; the 409 stays and the next
    `GET /draft` or Save heals it. Not fixed here.
36. **Heal lock order.** The Save route already holds Item then Job; the restage takes the head
    last and fires no trace event. A Save that restaged and then fails validation rolls the
    restage back with the rest of the transaction.
## Chat turn with a stale phone state

A chat turn may carry the phone's unsaved editor state. The phone encodes only the lanes
the creator changed (`changedSections`), so a lane present in `state.lanes` is a dirty
lane and an absent lane is "same as saved". Product rule: AI and manual edits both apply
on top of the latest version, and a turn is never refused because the phone's copy is old.

37. **State built on an older generation.** `resolve_editor_base` raised
    `EditorStateStaleError` and the planner turned it into the "Your video changed" miss, so
    a turn after an approval render or a phone publish was refused. The turn must proceed
    on the latest. Test: stale state with a head, with no head, and clean.
38. **Stale dirty lane overwrites a newer edit.** Applying the whole stale state would
    revert what the latest head already changed. A dirty lane applies only where the latest
    did not change that lane: a lane present in the current head's own payload wins. Test:
    overlapping text lane keeps the head's content, a disjoint lane keeps the creator's.
39. **Stale timeline lane replayed onto a new timeline.** Slot ids and cut times belong to
    the generation the phone saw. A dirty `timeline_slots` or `split_screen_sections` lane
    from an older generation is never applied (latest wins). Test: stale timeline lane is
    dropped.
40. **Clean stale state.** No dirty lane means nothing of the creator's to carry; the turn
    builds on the latest head, else the committed variant. Test: clean stale state.
41. **Unhealed head seen as no head.** A head on an older generation of the same job (or
    with disagreeing column and payload stamps) used to read as "no head", so the turn
    silently dropped the chat's earlier unsaved edits. The turn builds on the head rebased
    onto the current generation, in memory, by the same function restage persists, so the
    planner snapshot and the draft compile agree and the planner stays read-only. A head
    from another job is still never replayed. Test: head one generation behind; head from
    another job.
42. **Rebase fails.** A head whose rebase raises must not crash the turn; the turn builds
    on the committed variant and logs. Test: unusable head.
43. **Planner and compile disagree.** Both call the one resolver, so the stale-state
    answer cannot differ between plan time and the locked compile. Test: existing
    one-resolver guard plus a real-Postgres turn.

## Repair CLI (`app/cli/repair_editor_heads.py`)

One-off tool for items whose head was stranded before the server healed on its own. It is
built and tested locally only; a production run needs the owner's approval.

44. **Dry-run writes.** The default must change nothing: no row added, no head demoted, the
    variant's pinned `editor_draft` untouched. The session is read-only and never commits.
    Test: row count and head id before and after.
45. **Second `--apply` adds rows.** `restage_editor_head_sync` is a no-op once both stamps
    equal the baseline, so a second run reports nothing to do and adds zero rows. Test:
    apply twice.
46. **Lock order.** Item, then Job, then the head (taken inside the restage). Test: apply
    path locks in that order, one transaction per item.
47. **Item without a current job, variants, or head.** Reported as nothing to do, exit 0;
    an unknown item or variant key is an error, exit 2. Test: each.
48. **Head from another job.** The restage re-bases it onto the current job as the
    committed state (its slots do not exist on this timeline); the dry-run names it as
    "other job" so the owner sees what will happen. Test: foreign head.
49. **Head without an editor payload** (strategy or initial draft). Never touched; the
    list query skips it too. Test: non-editor head.
50. **`--list-stranded` on a job whose `variants` is not an array.** Must not error; the
    query guards with `jsonb_typeof`. Test: scalar `variants`.

## Lane-level rebase

Found by an independent review of the whole-content rebase (`editor_head.rebased_document`
overlaid the head's content on the committed state, so the head's older copy won on every
lane it carried). A head records no base, so nothing tells "the creator changed this lane"
from "this lane was copied along". Fix: every head records `base_lanes` (a hash of each
committed lane at the generation it was authored on) and a rebase is a three-way merge per
lane (head, committed now, recorded base). The rows below are the review's sequences; each is
a red-then-green test in `tests/kria/test_editor_head_lanes_postgres.py`.

51. **Legacy dispatcher under a full-`sections` head.** A bootstrapped or phone-written head
    stores every editor lane under `sections`. A dispatcher (swap song, retext, ...; about 35
    of them, none restage) commits G1 with new music, `GET /draft` heals, and the old
    `{**committed, **head sections}` stamps G0's music on G1 with `head_current=true`; the
    next Save commits it and the swapped song is gone. Same in the stranded pinned-draft Save
    heal and the repair CLI `--apply`. Rule: a lane the head did not change takes the
    committed value. Test: music swap then heal.
52. **Speech cut under a flat-lane chat head.** A chat draft stores its lanes at the top level
    of the payload and `project_editor_draft` reads only those. The `contained=True` restage
    kept every non-`sections` key, so the head kept its pre-cut `timeline_slots` over the cut.
    Rule: a lane both sides changed takes the committed value; a lane only the head changed
    (an untouched AI text edit) survives. Test: cut under a flat-lane head.
53. **Legacy Save over an AI text lane.** After a manual Save, the AI's older text still
    sat over the creator's saved text. Latest wins on a lane both changed. Test: Save over
    AI text.
54. **Stale `PUT /draft`.** A client read head H on G0 and PUTs after a dispatcher moved to G1;
    the etag still matches, and the whole G0 body was stamped G1. The body is a head authored
    against H: its dirty lanes are the ones that differ from what H recorded, and they are
    three-way rebased onto the current committed state; a body that only re-asserts old
    committed content changes nothing. `draft_stale` stays for a revision/etag mismatch.
    Test: stale PUT cannot resurrect G0.
55. **A restage is inferred from `changes == []`.** A manual Save or a speech-cut restage also
    produces changed stamps and an empty diff, so an old pending approval / render retry /
    intent-review post could run on top of a creator's Save (`prepare_editor_commit` of the
    approved draft overwrites the Save). A restage now records `restage: {of, reason}`;
    `draft_still_current` follows the lineage and accepts only the approval's own render and
    a phone publish, and only when no link replaced a lane of the draft. Test: approval across
    a manual Save refused; across its own render plus phone publish accepted.
56. **Undo after a restage.** Undo rebuilt `rebased(parent)`, which equals the current head (a
    visible no-op) or marks already-rendered changes as pending again while cancelling the
    approvals. Undo skips restage rows; when the result equals the head's lanes it answers
    `draft_undo_unavailable`, cancels nothing, and `can_undo` is false. Test: undo after a
    restage.
57. **Stale phone state against a `sections` head.** `_rebase_stale_state` looked only at
    top-level keys, so for any head with `sections` the phone's old lanes always won, and the
    creator's unsaved `timeline_slots` / `split_screen_sections` were dropped with no word.
    Same accessor and rule; a dropped creator lane is named to the creator. Test: stale state
    against a `sections` head.
58. **Timeline GET and draft GET disagree.** A v2 variant whose pinned draft is not current
    answered 200 with the committed state and no `draft`, while `GET /draft` served the healed
    head. GET stays write-free and serves the pinned draft rebased in memory by the same
    function. Test: timeline and draft agree, no row written.
59. **`problem.recovery` is `none` for a real version mismatch.** Guided `GUIDED_REVISION_STALE`
    is answered by reloading (the phone read every 409 that way before `problem` existed).
    Test: recovery per code.
60. **Repair CLI hides what `--apply` would do.** The dry run names the head and its stamps
    only. It prints, per lane, kept from head / taken from committed / replaced, from the same
    function. Test: dry-run lane report.
61. **Rows written before this change record no base.** Their flat lanes are dirty by
    construction (kept), lanes that exist only in a full `sections` head are taken from
    committed, and only the approval-execution restage (the render contains the draft) takes
    committed on every lane. A speech cut also names the timeline as rewritten by the writer.
    Test: unrecorded head.
