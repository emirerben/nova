---
name: review-edit
description: Convert Emir's free-form critique of a Nova-rendered video into consent-safe, structured edit-feedback annotations. Use when Emir shares a normal usekria.com production edit link and wants to review, rate, critique, or give thoughts on the AI response, hook, pacing, cuts, clip choice/order, captions, text, transitions, music, audio, effects, or overlays without opening the admin interface.
---

# Review Edit

Turn a normal Kria production edit link plus one natural-language review into the structured labels used by Nova's edit-learning loop. Preserve Emir's meaning, require one compact confirmation, then save behind the scenes. Emir never needs to open the admin interface or provide an admin URL.

## Safety contract

- A `https://www.usekria.com/plan/items/...` link is an explicit production target. Use production for that artifact only, retain the CLI's production confirmation, and never pass `--yes` unless Emir explicitly approved that exact write.
- Default to localhost only when the request contains no production Kria link and does not otherwise explicitly request production.
- Never write feedback before showing the interpretation and receiving confirmation.
- Never invent a criticism, compliment, timestamp, or rationale.
- Treat annotations as append-only. Correct an existing dimension by superseding its current annotation; never update or delete history.
- Never expose admin tokens, signed playback URLs, storage paths, or raw uploads.
- Do not export data, start training, or promote a model as part of recording feedback.

## Workflow

### 1. Resolve the exact rendered artifact

Prefer, in order:

1. A normal product URL such as `https://www.usekria.com/plan/items/<plan-item-id>/edit?variant=<variant>` in the request or current conversation.
2. The normal product URL already established in the current task.
3. An artifact UUID already established in the current task.
4. A read-only local list query followed by one concise disambiguation question.

For a normal production product URL:

1. Parse the UUID from `/plan/items/<plan-item-id>` and the optional `variant` query parameter. Accept `www.usekria.com`, `usekria.com`, and Nova's production Vercel host. Reject lookalike domains.
2. Resolve the latest eligible retained render through the authenticated wrapper. URL-encode the query values:

```bash
python3 scripts/admin.py --prod GET '/admin/edit-feedback?plan_item_id=<plan-item-id>&variant_id=<variant>&sampling=chronological&limit=2'
```

3. If the URL has a variant, select only the newest exact plan-item + variant match. Older rows are prior rendered versions, not ambiguity.
4. If the URL has no variant and the result contains multiple variants, ask one concise question naming the available variants. Never guess.
5. If no retained artifact matches, say that this edit is not yet available for feedback. Never substitute a different item.

The authenticated admin API is an implementation detail. Do not ask Emir to visit the admin page or provide an admin URL.

Do not silently select an artifact when the plan item or variant is ambiguous. Fetch the resolved artifact detail before interpreting feedback:

```bash
python3 scripts/admin.py --prod GET /admin/edit-feedback/<artifact-id>
```

Omit `--prod` for a local artifact. Confirm the title, duration, rendered time, and variant in the proposed review. Keep the artifact UUID internal unless needed for troubleshooting. Use the returned current annotations to identify corrections.

### 2. Translate the review

Read [references/feedback-contract.md](references/feedback-contract.md). Map each distinct thought to the smallest relevant dimension. Keep the rationale close to Emir's own wording; make only light grammatical edits.

Use these rules:

- `bad`: Emir identifies a clear failure or unwanted choice.
- `mixed`: the same dimension contains meaningful strengths and weaknesses.
- `good`: Emir explicitly approves it, or confirms the grouped "everything else was good" interpretation.
- `not_applicable`: Emir says the factor was absent or irrelevant.
- Include a time range only when Emir supplies one or explicitly confirms a proposed range. Never derive a frame range from vague words such as "the middle."
- "The middle is too slow" maps to `pacing: bad` without a time range unless Emir supplied one.
- "The cuts feel random" maps to `clip_ordering: bad` when it describes an incoherent sequence; use `cuts` only for trim points, cut timing, or cut style, and `clip_selection` only when the complaint is about which moments were chosen.
- Approval of the opening title maps to `text: good`, not `captions`.
- If Emir says this is feedback about only one factor, leave all other dimensions unrated.
- If Emir is reviewing the whole edit, propose all unmentioned dimensions as one grouped `good` set in the confirmation. This is a proposal, not a silent default.
- If one statement legitimately covers multiple dimensions, reuse the rationale only where it independently explains each rating. Do not inflate one vague comment into many negative labels.

For an existing current annotation:

- Omit it when the requested rating and rationale are unchanged.
- Otherwise include its `id` as `supersedes_annotation_id`.

### 3. Ask for one confirmation

Show a compact interpretation, for example:

```text
Corfu · guided_story · 7.0s · rendered Aug 24

Save as:
- Pacing — bad — "The middle holds too long."
- Text — mixed — "Title works; lower text feels generic."
- Remaining 13 factors — good — "No other issue noted in this review pass."

Save this?
```

Group identical remaining-good ratings; do not print fifteen repetitive rows. If the user corrects the interpretation, revise it and ask for confirmation again.

### 4. Save atomically

After confirmation, construct one payload:

```json
{
  "annotations": [
    {
      "dimension": "pacing",
      "rating": "bad",
      "rationale": "The middle holds too long.",
      "frame_start_s": 3.0,
      "frame_end_s": 5.0,
      "supersedes_annotation_id": null
    }
  ]
}
```

Send one request through the token-safe wrapper:

```bash
python3 scripts/admin.py POST /admin/edit-feedback/<artifact-id>/annotations/bulk --json '<payload>'
```

For a production product URL, add `--prod` and allow the wrapper to prompt. Do not call the API with `curl` or print credentials. The user's confirmation of the interpreted review approves the exact annotation payload, but it does not waive the wrapper's production prompt.

On a `409`, refetch the detail, rebuild against the latest current annotations, show the changed interpretation, and reconfirm. After a connection failure with an unknown outcome, refetch before retrying so an ambiguous success does not create a correction unintentionally.

### 5. Verify and report

Refetch the artifact. Report:

- which dimensions were appended or corrected;
- whether the artifact is now fully reviewed or still partial;
- that the feedback enters evaluation/training preparation but does not immediately retrain or change the production model.

Keep the report brief. Never claim training occurred merely because feedback was saved.
