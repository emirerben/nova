# Split-screen acceptance record

The creator requested ordinary editable media in two or three horizontal panels. The operation examples were not a scope limit. This matrix separates existing capability restrictions from split-screen implementation and verification.

| Operation | Required behavior | Evidence or remaining check |
| --- | --- | --- |
| Two and three panels | Top/Bottom and Top/Middle/Bottom have stable identities and simultaneous playback | Engine native exports and native model tests pass for both counts. UI creates both layouts and named rows, but the full journey remains failing. |
| Mixed images and video | A still and each video keep independent duration and source clocks | Native preview and export tests pass with mixed media and three simultaneous videos. |
| Source identity | Repeated uses of one asset can show different source windows | Database Save/AI/Save/reopen test preserves source starts 2 and 7, offset 0 and 1, and rate 2 and 1. |
| Panel layout | Custom proportions, divider spacing and background survive Save | Typed layout, AI and UI controls implemented. Database AI layout update preserves weights and gutter; current protocol is 43. |
| Panel gaps | Empty time and empty panels show the section background | Native frame assertions and inspected three-panel gap image pass. |
| Section boundary | Full-screen footage resumes at the half-open section end | Native preview and export boundary assertions pass. |
| Trim and extend | Each member changes independently within source and section bounds | Native session regression passes independent trim, section extension and unchanged neighboring media. iPhone gestures remain unrun. |
| Temporal cut | Both halves retain rate, source clock, crop and identity provenance | Native session cut/source-offset regression and AI operation tests pass. |
| Duplicate and replace | Copies gain independent IDs; source replacement changes only the selected occurrence | Ordinary duplication UI passed. Native controls include unused admitted sources and captured panel-targeted imports. Replacement UI still fails to open source choices. |
| Panel swap and order | Media moves between named panels without borrowing another source clock | AI tests pass. Native controls implemented. The three-panel UI journey fails its Middle selection assertion; saved recording shows the tap was covered by the clip toolbar. |
| Section order | A section moves as one serial unit and downstream clocks update once | Five native row tests pass, including section reorder and empty-tail duration. Group drag UI remains unrun. |
| Selection | Timeline lane and preview panel select the same ordinary clip | Repeated-speaker panel selection/drag UI and two local-geometry tests pass. Three-panel lane tap clearance remains under diagnosis. |
| Multiple selection | Preserve any existing group or multi-selection behavior | The base native editor has one selection, not a general multi-selection set. Split sections move as units. |
| Undo and redo | Each gesture or compound operation restores the whole timeline value | Native session regression passes atomic section/clip snapshot undo, redo and reopen. |
| Speed and source slip | Each member has an independent source-to-output map | Native marked-frame and PCM assertions pass. Existing format speed gates remain. |
| Crop and transforms | Source crop and local rotation/zoom never bleed into another panel | Marked-source crop, rotated panel and clipped backdrop native tests pass. |
| Holds and freezes | A held frame remains in its own panel without stretching audio | Native held-frame export tests pass. AI trim, cut, speed and whole-output retarget preserve moving and held spans. |
| Looks and backdrops | Existing portable looks and backdrops apply in panel coordinates | Golden-hour held panel matches the ordinary golden-hour reference. Colored backdrop test passes. Arbitrary looks were already unavailable for phone rendering. |
| Transitions | Same-panel clip overlaps and unrelated full-screen overlaps preserve their clocks | Native explicit-offset crossfade and backend panel predecessor/serial neighbor checks pass. Section boundaries and gaps use cuts. |
| Original audio | Per-panel gain and mute preserve independent source timing | Decoded preview/export PCM assertions pass for 0.5, 0.8 and muted sources. |
| Music, narration and SFX | Independent soundtrack lanes remain separate from panel clocks | Backend Save regressions preserve narration, processed original audio and creator-song receipts. Song volume/window/removal checks pass. |
| Captions and text | Canvas-wide placement remains global; source-bound timing follows the owning occurrence | Backend Save and native owner-aware text, speech, global gap and frozen-label regressions pass. Full historical native label suite remains unrun. |
| Media overlays and drawings | Global layers remain above the composite and survive timeline edits | Global canvas layers retain existing placement controls; they do not gain panel-local spatial anchors. Real Save gap, trim, weights-only and repeated-Save checks pass without duplicate drawings. |
| Camera and motion effects | Preserve applicable existing effects and refuse unsupported recipes explicitly | Backend phone admission already refuses unsupported camera/motion effects for ordinary timelines. Split screen does not open those capabilities. |
| Save and reopen | A saved draft reconstructs both section definitions and member fields | Real isolated Postgres test passes across three saved immutable drafts. Native API compatibility, variant fallback and atomic commit/reopen tests pass. |
| AI/manual/AI | Both editing paths use the same persistent timeline and keep unrelated edits | Real parser/compiler, database manual→AI→manual and native chat create/layout/unstack staging tests pass. No live provider call made. |
| Initial creation prompt | A structured ordered panel request reaches phone dispatch | Typed creator schema, capability policy, prompt and dispatch bridge tested with recorded EN/TR structured transport. Source properties and previous presentation lanes survive lowering. No live provider call. |
| Older clients | They cannot discard a split layout on Save or silently render it full-screen | Protocol 43 and `splitScreen` admission added. Focused refusal tests pass. |
| Failure recovery | Missing sources, stale drafts and unavailable capabilities preserve the user's editable state | Existing recovery paths reused. API regression checks pass. Formal checker is blocked by a downloaded binary checksum mismatch; native lifecycle checks remain pending. |

Native engine artifacts are local macOS AVFoundation output. They do not prove an iPhone UI journey, physical-device behavior or deployment. Test media is synthetic and remains outside git. No live project, production record or deployment has been modified.

Speaker backgrounds retain exact subject occurrence ownership across repeated panels and unstacking. Native exports cover panel-local frames, masks, timed fills, full-screen windows and background focus. Shortening a section removes windows that become too short to validate. Backend omitted-lane Save moves later background windows with the section boundary.

An entirely media-free retained section is not representable in this slot-ordered schema. A trim that would leave only its background is refused atomically with an explanation; deleting the final member removes the section. Existing arbitrary camera/motion/custom-effect admission restrictions remain. General multi-selection was absent in the base editor.
