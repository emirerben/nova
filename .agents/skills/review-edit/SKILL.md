---
name: review-edit
description: Convert Emir's free-form critique of a Nova-rendered video into consent-safe, structured edit-feedback annotations. Use when Emir says he wants to review, rate, critique, or give thoughts on an edit, AI response, hook, pacing, cuts, clip choice/order, captions, text, transitions, music, audio, effects, or overlays without filling the admin form manually.
---

# Review Edit

Turn one natural-language review into the structured labels used by Nova's edit-learning loop. Preserve Emir's meaning, require one compact confirmation, then save through Nova's authenticated admin API.

## Safety contract

- Default to localhost. Use production only when Emir explicitly asks, retain the CLI's production confirmation, and never pass `--yes` unless Emir explicitly approved that exact write.
- Never write feedback before showing the interpretation and receiving confirmation.
- Never invent a criticism, compliment, timestamp, or rationale.
- Treat annotations as append-only. Correct an existing dimension by superseding its current annotation; never update or delete history.
- Never expose admin tokens, signed playback URLs, storage paths, or raw uploads.
- Do not export data, start training, or promote a model as part of recording feedback.

## Workflow

### 1. Resolve the exact rendered artifact

Prefer, in order:

1. An artifact UUID or `/admin/edit-feedback?...artifact=<uuid>` URL in the request or current conversation.
2. The artifact already established in the current task.
3. A read-only local list query followed by one concise disambiguation question.

Do not silently select the newest artifact when more than one is plausible. Fetch the exact detail before interpreting feedback:

```bash
python3 scripts/admin.py GET /admin/edit-feedback/<artifact-id>
```

Confirm the artifact title, duration, creation time, and ID in the proposed review. Use the returned current annotations to identify corrections.

### 2. Translate the review

Read [references/feedback-contract.md](references/feedback-contract.md). Map each distinct thought to the smallest relevant dimension. Keep the rationale close to Emir's own wording; make only light grammatical edits.

Use these rules:

- `bad`: Emir identifies a clear failure or unwanted choice.
- `mixed`: the same dimension contains meaningful strengths and weaknesses.
- `good`: Emir explicitly approves it, or confirms the grouped "everything else was good" interpretation.
- `not_applicable`: Emir says the factor was absent or irrelevant.
- Include a time range only when Emir supplies one or explicitly confirms a proposed range. Never derive a frame range from vague words such as "the middle."
- If Emir says this is feedback about only one factor, leave all other dimensions unrated.
- If Emir is reviewing the whole edit, propose all unmentioned dimensions as one grouped `good` set in the confirmation. This is a proposal, not a silent default.
- If one statement legitimately covers multiple dimensions, reuse the rationale only where it independently explains each rating. Do not inflate one vague comment into many negative labels.

For an existing current annotation:

- Omit it when the requested rating and rationale are unchanged.
- Otherwise include its `id` as `supersedes_annotation_id`.

### 3. Ask for one confirmation

Show a compact interpretation, for example:

```text
Corfu · 7.0s · artifact a641…

Save as:
- Pacing — bad — "The middle holds too long." — 3.0–5.0s
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

For production, add `--prod` and allow the wrapper to prompt. Do not call the API with `curl` or print credentials.

On a `409`, refetch the detail, rebuild against the latest current annotations, show the changed interpretation, and reconfirm. After a connection failure with an unknown outcome, refetch before retrying so an ambiguous success does not create a correction unintentionally.

### 5. Verify and report

Refetch the artifact. Report:

- which dimensions were appended or corrected;
- whether the artifact is now fully reviewed or still partial;
- that the feedback enters evaluation/training preparation but does not immediately retrain or change the production model.

Keep the report brief. Never claim training occurred merely because feedback was saved.
