# KRI-506: resolve creative uncertainty before rendering

## Acceptance contract

A creator asking for a hook without giving the words is asked whether they have an idea
or want Kria to generate one. Generated wording is discussed and approved separately
from the complete edit plan. Asking for generation is not approval of its output.
Exact creator wording remains exact; generated wording retains its actual provenance.

The initial inspection used `b2b656942`. Implementation starts from `0eae045b1`, which
includes PR #1462's missing-title gate and failed-first-render recovery. Those fixes
are reused. There is no incident/job reference on KRI-506, so this is a code-backed
failure map, not a claim to have reproduced a particular production failure.

## Decision and failure map

| Situation | Existing evidence and handling | Required behavior / regression |
| --- | --- | --- |
| Hook requested without words | Main Creator has an advisory `intro_hook`; `opening_title` needs grounded literal copy. `title_text_choice` now asks for words or omission. | Ask whether the creator wants to supply an idea or delegate writing. No approvable plan with unresolved requested copy. |
| Creator delegates writing | `_apply_explicit_render_intent` deliberately strips copy without a creator quotation. | Store a generated candidate separately; approve that exact candidate before using it. Never fabricate a creator quotation to pass the check. |
| Discussion, revisions, or short answers | Existing choice cards validate question IDs and allow free text. Normal model history is bounded. | Reconstruct pending decisions from durable events, expose compact state to the agent, and route replies through the conversation instead of an editor shortcut. A revised candidate requires fresh approval. |
| Stale question or changed media | Runtime already fences thread revisions, choice IDs, media snapshots, and render approvals. | Bind decisions to stable dependencies and preserve those fences. Never apply approval to replacement wording or a changed media set. |
| Ambiguous clip / repeated label | Clip-intent resolution and `collect_conflicts` already handle clip identity and duplicate shot-label placement. | Reuse those questions. Do not replace known footage facts with new questions. |
| Missing chronology / requested order | Existing `order_basis` and order/group checks ask supported alternatives. | Keep scoped answers, avoid asking again after resolution, and never guess unavailable capture evidence. |
| Duration versus required coverage | Existing duration/count checks compare requested length with required sources. | Preserve current choices and the format-specific must-not-ask corpus. Do not ask simply because raw footage is longer than the edit. |
| Audio source / narration requirements | Song-order, clip-intent, and capability checks own those decisions. | Ask only for a real supported choice or missing creator input. Do not promise unavailable voice generation or audio treatment. |
| Unsupported capability | Server capability manifest and typed render declines are authoritative. | Explain the limitation and offer supported alternatives; no question can manufacture a capability. |
| Planner omitted a stated requirement | Brief ledger and receipt checks compare requested output against the proposed/rendered edit. | Preserve the requirement and repair or stop the plan; never ask the creator to repeat a known decision merely to hide an internal omission. |
| Renderer / recipe / infrastructure failure | Render contract verification and failure observers report execution failures. | Keep system-owned recovery. A technical failure is not evidence the creator failed to specify their intent. |

## Verification contract

```text
request -> identify known / delegated / unresolved decisions [agent replay]
        -> durable pending question or generated candidate [event integration]
        -> exact wording approval [stale/forged/revised/retry tests]
        -> executable strategy with real provenance [compiler + contract]
        -> separate render approval [runtime integration]
```

Exercise creator-authored and generated text, discussion, cancellation, multilingual
answers, old conversation history, reload, media changes, and duplicate/concurrent
turns. Keep title/order/duration/clip/audio and must-not-ask regressions green. Verify
exact approved words survive strategy serialization and recipe/contract generation.
Replay cassettes verify structured contracts, not live model quality.

No new endpoint or database table is required. Existing clients retain question-card
and plain-text fallback support. Production deployment and verification remain behind
the autoship merge/deploy approval gate.
