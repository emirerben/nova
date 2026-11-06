# Remaining actions in the touched routes

This is a source-review inventory, not executed UI evidence. The current working tree is not yet frozen/rebased. Labels below distinguish a real user decision from a reload disguised as repair.

| Affordance | Classification and behavior |
| --- | --- |
| Generic chat failure “Refresh project” | Removed. It only reloaded project/delta state and could not repair a terminal render. The existing live poll continues; fallback copy directs a new chat instruction. |
| Approved-render retry with no execution ID | No dispatch and no instruction to refresh manually. The authoritative projection may already have moved on; the existing chat live poll follows it. A specific failed execution is required before a real retry. |
| “Retry approved edit” | Explicit action against an exact failed execution and thread revision. It is not automatically repeated after an uncertain outcome. |
| “Retry message” in general chat | Existing original-submission reconciliation; the copy says it checks the same submission. It is distinct from a new user message. |
| “Retry preparing clips” / failed upload retry | Explicit failed preparation/upload action when the server marks it retryable. Processing/status reads converge automatically; no blind repeat of an uncertain upload is introduced. |
| Native/SlidePost conflict choices | Genuine overlapping authored changes or connected timing/source dependencies. Independent changes merge automatically. Choices preserve the other side's independent changes. |
| Native “Switch / Keep editing” | Retained only when a matching authoritative editable base is unavailable for a dirty different-job target. Clean/newer results and mergeable dirty content follow automatically. Switch explicitly discards the old local version; it is not a routine refresh path. |
| Native unsafe song choice | A missing/unproven source authority may require “Use latest”, then a deliberate edit to the new song. Old relative edits cannot be silently applied to a different source. Saved-song metadata/acknowledgement handles the legitimate old-render-plan projection case. |
| Strict AI “Check original result” | Read-only observation of the original UUID after automatic read-budget exhaustion. Cached completed output can still deliver without another provider call. Ordinary transient retrieval follows automatically within the budget and persisted server deadline. |
| Strict AI new instruction after rejection/unknown | A new explicit user intent, not Retry of the original paid request. Known rejection and unverifiable provider outcome remain distinct. No new UUID/provider execution is minted by a recovery GET. |
| Render Create after confirmed terminal failure | New explicit render intent only after a fresh exact-job failure receipt/state. New admission also compares current Job and draft version under lock. Pending/unknown work is observed automatically using its original identity. |
| Export “Try again” after render/poll budget | Starts a bounded new export/check flow; it must observe existing render intent rather than issue duplicate generation. Photos/share transaction evidence is a separate gate. |
| Retry/remove failed media | A genuine source preparation failure. Invalid/unavailable input cannot be made ready by reloading; the user may retry the failed preparation or replace/remove that input. |
| Workspace/editor opening “Try again” | Client read-budget exhaustion or an explicit unsupported/missing prerequisite; automatic quiet retries run first. This action does not imply the underlying generation failed. |
| Review “reopen Review to see it” after six read failures | Remaining bounded-read exhaustion behavior, **not a terminal generation failure**. Same-job/turn convergence is automatic before exhaustion; reopening re-reads rather than resubmits. The existing ten-minute follow bound and six-failure cap do not prove indefinite-outage recovery. |
| Gallery/device read-budget exhaustion | Existing lifecycle/network/foreground signals can resume checks. No blanket claim is made that an indefinitely unavailable service will converge without another signal after its budget is exhausted. |

No general “click to show the new version” action remains necessary for a clean state with a verified newer result in the implemented paths. Genuine conflict, unavailable identity/base, unsupported input, device permission, terminal execution failure and exhausted read budgets remain explicit boundaries. Their presence must not be presented as proof that the background operation was restarted, failed, or delivered.
