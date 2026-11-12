# Latest-wins convergence: failure modes

Rule: the iPhone editor always shows the single latest version. AI chat edits and manual edits both apply on top of it. The creator is never asked to resolve a conflict.

"Converge" means: when a newer version exists, adopt it and replay only the creator's own edits that apply cleanly. It is total: it always returns a document, never a pending decision.

Terms: `base` = the clean document the phone last loaded. `local` = the creator's own unsaved edits (nil when the editor is clean or holds only edits staged from an AI draft). `latest` = the server's current version.

## Ways converge can fail

1. **Chat-only local vs a render of the same edit.** The AI draft head is staged on the phone as unsaved edits. The server then renders that same edit, quantized to a 0.5 s grid, so `latest` holds slightly different numbers. A three-way merge sees two changes to the same atomic timeline group and reports a conflict with itself. Converge must treat a chat-only local as no creator edits and return `latest`.
2. **Draft base is `render_finished_at`, not `render_generation_id`.** Variants that never went through the editor have no generation id; the baseline is the finish timestamp. Comparing only generation ids says "stale" or "current" wrongly. One shared baseline helper must be used for the draft, the variant and the Save request.
3. **Song changed in latest while the creator edited the song.** The merge cannot reconcile two song authorities. Converge returns `latest` (the creator's song edit is replaced) and reports that creator edits were replaced.
4. **Replayed result fails validation.** The merged document can fail split-screen or interval checks (`end < start`, non-finite, overlapping panels). Converge must fall back to `latest` alone, never adopt an invalid replay.
5. **Converge arrives mid-gesture, mid-text-entry or mid-save.** It must defer and run after the gesture, text commit or save settles. It must never drop the request and never apply over a gesture baseline.
6. **Converge fires twice for the same generation.** Replays must be idempotent: the second run must not push a second Undo step or toast, and must not replay creator edits onto an already converged document.
7. **Undo after converge, then Save.** Undo must restore the creator's edits rebuilt on the latest base, carrying the latest generation. Restoring the old document verbatim carries the old generation and Save returns `baseline_conflict` again.
8. **Job or variant switch with creator edits.** Converging onto another job or variant must not replay lanes authored for the previous one, and must not leave state from the old target (staged chat draft, Undo stack, applied draft revision) alive.
9. **A non-baseline HTTP 409.** Other 409s (`editor_draft_unavailable`, `timeline_stale`, plan unavailable) are not "a newer version exists". Reload-and-converge on them loops forever or hides the real error. Only `baseline_conflict` may trigger a reload.
10. **Latest read fails.** Offline or 5xx while fetching latest: keep the creator's local edits, show no decision card, retry quietly. Never block Save, preview or export on an unreadable latest.
11. **Stale converge result.** A response for an older load, job or account arrives after the editor moved on. It must be fenced by the editor operation identity and discarded.
12. **Creator edits made while the latest read is in flight.** The document changed after `local` was captured. The result must either include those edits or be recomputed; it must not silently overwrite them.
13. **Clean editor with a pending render retry or saved-but-unrendered lanes.** Clean must mean "no creator edits", not "no dirty flag". Edits already acknowledged by the server must not be replayed as local edits.
14. **Unprovable AI base.** A chat draft that echoes no submitted state cannot be diffed against a trusted base. It is the AI's edit, never the creator's, so it must never be treated as `local`.
15. **Capability or protocol difference between local and latest.** `latest` may drop a capability the local document used (for example a lane the renderer no longer opens). Replaying that lane must not produce an unrenderable document; fall back to `latest`.
16. **Deletion intents.** A creator deletion of an entity that `latest` rewrote must not resurrect the entity, nor keep a delete command for an entity `latest` kept. Mismatch means fall back to `latest`.

## Added for the generation rule and the 409 split (B4, B5)

17. **Variant has only `render_finished_at`.** The server's baseline is `render_generation_id`, else `render_finished_at`. A phone site that compares only `render_generation_id` sees "no generation", so the AI's draft head (stamped with the finish time) is judged stale and dropped even though the server says the edit is done. Every site that reads a generation off a variant dictionary must use the one shared helper.
18. **Server marks the head current but the ids differ in shape.** After a phone publish `render_generation_id` becomes the upload attempt id while the head names the generation the Save was made from. Re-deriving freshness on the phone drops a head the server just certified. When `head_current == true` is present, the phone accepts the head; it never drops one the server marked current.
19. **Older server omits `head_current`, or sends `false`.** Absent: fall back to the helper comparison. `false`: do not trust the head, use the same helper comparison (it fails for a genuinely stale head).
20. **A non-baseline 409 on Save treated as "a newer version exists".** `TIMELINE_STALE`, `editor_draft_unavailable`, `variant_initial_render_in_progress`, `deletion_target_stale`, `editor_source_catalog_invalid` carry no information about a newer version. Reloading on them replaces nothing, resends the same Save, and shows "changed elsewhere" copy for a different problem. They must surface as an ordinary rejected save, keep the creator's edits, make no reload call, send the commit once, and leave Save enabled.
21. **Client-detected generation mismatch loses its classification.** The preview and source-pool paths throw a detail-free `.conflict` when the variant's generation differs from the document's. If reload is gated on a code, these must carry the baseline code explicitly, or the preview stops following the latest version.
22. **A 409 or 412 with no body.** An empty or unparseable body proves nothing about versions. It is not a baseline conflict; it is a normal failure, not a reload.
