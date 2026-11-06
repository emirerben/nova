# Split-screen acceptance record

The creator requested ordinary editable media in two or three horizontal panels. The operation examples were not a scope limit. This matrix separates existing capability restrictions from split-screen implementation and verification.

| Operation | Required behavior | Evidence or remaining check |
| --- | --- | --- |
| Two and three panels | Top/Bottom and Top/Middle/Bottom have stable identities and simultaneous playback | Engine native export tests pass for both counts. iPhone UI verification pending. |
| Mixed images and video | A still and each video keep independent duration and source clocks | Native preview and export tests pass with mixed media and three simultaneous videos. |
| Source identity | Repeated uses of one asset can show different source windows | Database Save/AI/Save/reopen test preserves source starts 2 and 7, offset 0 and 1, and rate 2 and 1. |
| Panel layout | Custom proportions, divider spacing and background survive Save | Typed layout, AI and UI controls implemented. Database AI layout update preserves weights and gutter. |
| Panel gaps | Empty time and empty panels show the section background | Native frame assertions and inspected three-panel gap image pass. |
| Section boundary | Full-screen footage resumes at the half-open section end | Native preview and export boundary assertions pass. |
| Trim and extend | Each member changes independently within source and section bounds | Native session tests and iPhone gestures pending. |
| Temporal cut | Both halves retain rate, source clock, crop and identity provenance | Native and AI operations implemented. Final session verification pending. |
| Duplicate and replace | Copies gain independent IDs; source replacement changes only the selected occurrence | Native controls implemented. Final session verification pending. |
| Panel swap and order | Media moves between named panels without borrowing another source clock | AI tests pass. Native session/UI verification pending. |
| Section order | A section moves as one serial unit and downstream clocks update once | Native group drag implementation and pure row tests added. Execution pending. |
| Selection | Timeline lane and preview panel select the same ordinary clip | Engine bounds account for viewport. iPhone interaction verification pending. |
| Multiple selection | Preserve any existing group or multi-selection behavior | The base native editor has one selection, not a general multi-selection set. Split sections move as units. |
| Undo and redo | Each gesture or compound operation restores the whole timeline value | Timeline snapshot includes sections and clips atomically. Native session verification pending. |
| Speed and source slip | Each member has an independent source-to-output map | Native marked-frame and PCM assertions pass. Existing format speed gates remain. |
| Crop and transforms | Source crop and local rotation/zoom never bleed into another panel | Marked-source crop, rotated panel and clipped backdrop native tests pass. |
| Holds and freezes | A held frame remains in its own panel without stretching audio | Native held-frame export tests pass. |
| Looks and backdrops | Existing portable looks and backdrops apply in panel coordinates | Golden-hour held panel matches the ordinary golden-hour reference. Colored backdrop test passes. Arbitrary looks were already unavailable for phone rendering. |
| Transitions | Same-panel clip overlaps and unrelated full-screen overlaps preserve their clocks | Native explicit-offset crossfade passes. Paired backend/native projection checks pending. Section boundaries use cuts. |
| Original audio | Per-panel gain and mute preserve independent source timing | Decoded preview/export PCM assertions pass for 0.5, 0.8 and muted sources. |
| Music, narration and SFX | Independent soundtrack lanes remain separate from panel clocks | Backend authored soundtrack receipt preservation implemented. Broader checks pending. |
| Captions and text | Canvas-wide placement remains global; source-bound timing follows the owning occurrence | Simultaneous source projection and explicit duration fixes implemented after independent review. Final paired checks pending. |
| Media overlays and drawings | Global layers remain above the composite and survive timeline edits | Existing lane compilers reused. Combined native fixture verification pending. |
| Camera and motion effects | Preserve applicable existing effects and refuse unsupported recipes explicitly | Backend phone admission already refuses unsupported camera/motion effects for ordinary timelines. Split screen does not open those capabilities. |
| Save and reopen | A saved draft reconstructs both section definitions and member fields | Real isolated Postgres test passes across three saved immutable drafts. |
| AI/manual/AI | Both editing paths use the same persistent timeline and keep unrelated edits | Real parser/compiler and database manual→AI→manual sequence passes. No live provider call made. |
| Initial creation prompt | A structured ordered panel request reaches phone dispatch | Typed creator schema, capability policy, prompt and bridge implemented. Dispatch/replay verification pending. |
| Older clients | They cannot discard a split layout on Save or silently render it full-screen | Protocol 42 and `splitScreen` admission added. Focused refusal tests pass. |
| Failure recovery | Missing sources, stale drafts and unavailable capabilities preserve the user's editable state | Existing recovery paths reused. Full regression and formal model checks pending. |

Native engine artifacts are local macOS AVFoundation output. They do not prove an iPhone UI journey, physical-device behavior or deployment. Test media is synthetic and remains outside git. No live project, production record or deployment has been modified.
