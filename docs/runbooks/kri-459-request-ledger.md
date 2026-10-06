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
