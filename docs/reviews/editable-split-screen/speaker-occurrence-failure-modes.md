# Speaker occurrence framing

The user requires every applicable existing edit to remain available in split-screen. At tested head `117a4f7b5`, selecting a repeated speaker resolves the right occurrence and panel viewport, but drag, pinch, slider, preset and Full write `document.speakerBackground.speakerFrame`. All other occurrences therefore change too. The existing repeated-speaker UI test observes selected identity and movement, not unchanged peers. This is a product defect beyond wording; the 436 passing tests do not cover it.

## Domain and compatibility

- An optional occurrence-owned `speaker_frame_override` on the authored slot uses the same recipe wire key and Swift property `speakerFrameOverride`. The wrapper distinguishes `inherit`, `full` and `custom`. Missing/inherit resolves the existing global default; full explicitly removes the inset frame while preserving matting/background; custom uses existing validated frame geometry.
- A bare nullable frame is insufficient because omitted/inherited and explicitly full geometry must survive `exclude_none` serialization distinctly.
- Existing global controls continue to change the global default. Selecting a split speaker, or an occurrence retaining an override after unstacking, changes that slot in one timeline transaction. Capture the selected occurrence at gesture start.
- Explicit inherit resets remain stored and inert, including without a background; omission alone cannot erase an older custom/full policy.
- Overrides survive Save/GET, AI snapshots, trim, cut, duplicate, source replacement where eligible, and unstacking. Ordinary crop/rotation/zoom are independent. Refuse invalid subject eligibility or malformed geometry atomically.
- AI operations without a selector retain global behavior. Targeted preset/custom edits and explicit full/inherit changes affect only resolved occurrences and report timeline changes.
- Any override requires the unreleased splitScreen capability/protocol43, including after unstacking. Unsupported clients must refuse, not silently substitute global geometry. Full-screen speaker windows retain their stronger bypass semantics.
- Resolve each occurrence before rendering. Different same-panel transition contributors must each be masked/framed before blending. Preview fast paths must preserve the same resolution as export.

- Existing background/window/color/focus capability flags remain authoritative. Authored admission requires validated current split sections or a server-derived durable origin marker plus an owned eligible video subject; an arbitrary override cannot open unrelated authored edits. Global removal resets occurrence policies in the same transaction and cannot resurrect the prior recipe background.

## Failure modes and required evidence

| Failure mode | Required observation |
| --- | --- |
| Selected lower speaker moves/resizes upper peer | Exact peer slot/global-frame equality plus stable peer rendered bounds/pixels at a fixed paused time after lower drag, pinch, corner resize, slider and presets. |
| Full collapses into missing/inherited during serialization | Full affects only the selected occurrence with a global inset present; Save/reopen and AI/manual round trips retain it. Undo restores its prior override in one step. |
| Selection synchronization writes a frame | Switching between different overrides updates slider/preset state without changing the document or adding undo entries. |
| Gesture changes target after selection moves | Capture the occurrence at gesture begin; updates cannot leak to a newly selected peer. |
| Local override freezes global controls or global edits overwrite it | Global default edits update inheriting clips while explicit custom/full overrides stay unchanged. |
| Geometry is stored in media_transform | Crop/rotation/zoom survive speaker edits byte-for-byte, and ordinary media edits preserve the frame override. |
| Cut/duplicate/unstack lose ownership | Copies retain the override value and independent IDs; cut halves retain source timing; unstack preserves the retained occurrence's frame and capability requirement. |
| Replacement silently drops or misapplies an override | Preserve eligible occurrence geometry; explicitly handle invalid speaker eligibility without partial mutation. |
| AI selector modifies another panel or global default | Actual parser/operation path targets stable occurrence IDs, preserves unrelated slots and global frame, and survives subsequent manual edits/AI snapshot. |
| Overlap blends frames before applying one participant's geometry | Two differently framed same-panel transition participants render their own geometry before blending; preview and exported frames agree. |
| Preview fast path ignores per-occurrence changes | Live preview reflects the same effective frames as a rebuilt composition/export; fallback rebuild is preferable to stale/shared geometry. |
| Old client silently renders inherited frame | Required capability is detected from overrides even without split sections; protocol/admission refusal is exercised. |

Tests must precede implementation and preserve existing assertions. New tests remain explicitly unrun while native/package/formal execution is held. No shared model, verifier or coverage-hash change is authorized here. Record the exact later revision for every executed result.
