# Independent draft review and repair

A timing-only editor plan can contain a valid sequence retime plus an unrequested text rewrite. The current compiler correctly rejects that combination. This slice lets an independent reviewer select a server-built correction, so the same turn can produce a reversible draft with the existing words intact.

The implementation uses runtime-v2's existing draft, compiler, approval and Undo path. It does not extend legacy Creator-session auto-iteration. The initial legacy prototype in PR #1953 was discarded after verifying that legacy mutation routes reject runtime-v2 projects.

## Behavior

The sole candidate removes `rewrite_text`, `remove_texts` and `add_text` from a current timing-only plan. It retains exactly one existing `retime_text_sequence` and any existing duration operation, including their exact parameters. It never invents a retime, changes a span or supplies new copy. Every current extracted requirement must be timing-only. Mixed requests, duration-only edits, timing patches, explicit preservation, surviving absolute timing requirements and missing Undo history are ineligible.

The reviewer receives the current request, proposed operations, fixed candidate and explicitly labelled compiler evidence. It has not watched the video. It returns a candidate ID, keeps the plan, requests confirmation or reports unavailability. It cannot return executable operations or replacement wording.

Before inference, a short read transaction verifies ownership and copies the current baseline. Inference runs outside write locks under the turn heartbeat. The completion transaction rechecks ownership, generation, draft head, client state, brief and operation fingerprints, then uses the existing compiler. A successful draft event and execution receipt retain candidate identity, evidence and provider provenance. The localized explanation describes the applied draft correction; the existing preview and Undo controls remain authoritative. Rendering still requires its existing approval.

## Bounds

The feature defaults off. Even with flags on, runtime does not manufacture a paid review capability. A separately supplied, server-only RunContext must authorize the exact turn, job and session through existing credit gates and the reservation ledger. No production capability producer or activation is included. One call, 48 KB context, 1,600 output tokens, at most 60 seconds and a completion-time reserve apply. Unknown provider outcomes retain liability and are not retried.

Unavailable or rejected review leaves the original plan subject to its existing compiler guards. A stale candidate, unreadable duration, missing media or unsupported operation cannot become a claim of success. No original project is mutated by development tests, and no server final rendering is introduced.

## Follow-up boundary

This is a preapproval draft correction, not a new post-render automatic-edit grant. Existing source-normalization recovery remains limited to that purpose. Post-render quality suggestions, live provider qualification, phone acceptance and any scoped production activation require separate evidence and authority. The feature does not promise output from missing or invalid media.
