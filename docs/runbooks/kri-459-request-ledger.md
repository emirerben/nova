# KRI-459 request ledger failure modes

The Creative Brief is an append-only request ledger.  A creator requirement may
leave active use only through an explicit, versioned removal.  The planning
context may be bounded, but the persisted ledger and its full serialization are
not silently bounded.

| Failure mode | Required behavior |
| --- | --- |
| A model emits more updates than a supported planning turn can accept | Reject the whole batch with a recovery error. Do not retain a prefix. |
| The ledger has more than 40 live requirements | Preserve every requirement and surface a context-budget recovery error only at a bounded planning stage. |
| The full ledger is larger than a planning-request budget | Preserve and serialize the full request. Batch whole requirements for a supported stage, or return a recovery error if that stage cannot cover them. Never slice a string. |
| Two requirements share kind and scope | Keep both unless a change or removal names one stable requirement ID. |
| A caption correction or removal is ambiguous, stale, or references a non-live ID | Reject the complete update batch. Ask for an explicit target and current version; do not guess or modify another caption. |
| Stored requirement data is malformed | Raise a recovery error. Do not load a smaller valid-looking ledger. |

`render_brief_request` is the unrestricted, persistence-safe representation.
`brief_context` and `batch_brief_requests` own planning-context limits and must
either cover every applicable requirement as whole entries or fail explicitly.

## Requirement-evidence failure modes

Receipts distinguish a factual conclusion from an unverified request. A bound
writer can request an unchecked receipt so every active requirement remains
visible, while legacy callers retain their existing omission behavior.

| Failure mode | Required behavior |
| --- | --- |
| A checker has no evidence for a live requirement | Emit `partial` with `verification: "unchecked"` only when the caller requests unchecked receipts; never say it is fulfilled. |
| A generic editor text element contains a clip-scoped caption | Do not credit the caption unless the exact target clip has evidence. |
| A reply has unchecked receipts | Omit a generic success summary and name the unverified requirement. |
| A checker makes a determinate factual comparison | Mark it `verification: "checked"` and include the furthest evidence stage (`understood`, `matched`, `applied`, or `checked`). |
| A checked receipt only describes what the chosen format does (a Talking edit's length follows the take; speech cleanup is chosen at approval and cuts pauses, never a named line) | Keep it an honest `partial`, but never turn the draft into the "simpler version?" question: `brief_checks.is_format_limit` / `needs_creator_choice` (reason prefixes in `_FORMAT_LIMIT_REASON_PREFIXES`). |
| The gate fires on a thread with no draft | Say "I haven't started a draft yet", never "Your current draft is unchanged". |

Draft-time checkers added for Talking / voiceover asks (thread 17f666cb, 2026-10-06):
`_check_speech_cleanup` (a removal verb plus pauses/filler/a named retake, never a
"keep my pauses" ask or a pop-in trigger; only on a speech-spined format, elsewhere
the sentence takes its kind's usual path; met once `speech_cleanup_enabled`, else
"choose Clean up speech when you approve" only when the cohort check says it will
be offered, hedged when unknown; a named line or retake is editor work; a length in
the same sentence follows the cut take) and `_check_captions` (on / off /
word-by-word against the strategy's `caption_style`; karaoke and kinetic are
word-by-word; `auto`, or an ask about a caption's look, place or language, stays
"can't check"). Patterns match `fold_text` output (ı folded to i). Both read `PlanFacts.caption_style` /
`speech_cleanup_offered`, filled by `plan_facts_from_strategy` and
`kria_runtime._speech_cleanup_offered`. Tests: `tests/kria/test_brief_talking_receipts.py`,
`tests/kria/test_runtime_talking_first_draft.py`.
