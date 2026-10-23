# Cabin N2: full-screen photo followed by its original video

## Request and observable result

Source: the creator's supplied `kria-test-prompts.md` and N2 kit, confirmed in
this implementation task. Original request:

> When I say "the loft", show the loft bed. When I say "bathtub", show the bathtub photo. When I say "the river", show the river photo first, then the river clip. End on Deniz at his laptop. Title: "3 things we loved". Clean captions.

The creator clarified that photo then video means full-screen sequential pictures.
A corner/PiP is used only when explicitly requested. Captions mean spoken subtitles;
the requested title is separate and no extra labels/decorative copy are implied.

This change covers native recorded-voiceover edits. The river photo occupies its
existing narration interval, then the selected original river video starts at its
original in-point. It must not consume the video's opening frames behind the photo.
The voiceover, subtitles, title, total duration and pinned laptop ending retain their
timing. A sequence that cannot fit must recover explicitly, not silently become PiP
or claim success after dropping the photo/video. An ordinary bathtub photo is also
full-screen. The default sequence photo hold is 1.5 seconds; an explicit hold must
fit unchanged, with at least 0.3 seconds of the following video visible.

## Boundaries and root causes

1. Planner/schema: the old reaction-beat contract had no display mode or typed
   photo-to-original relationship. Add backward-compatible optional fields, resolve
   canonical owned/selected source IDs, and preserve all-scope selection semantics.
2. Grounding: the old path always used face-safe corner geometry. Full-screen photos
   now have their own overlap lane and opaque frame-filling placement.
3. Narrated compiler: it sent every card to the PiP compiler. Reuse the existing
   full-screen track compiler and preserve capabilities during lane saves/removal.
4. Source timing: hiding a running original under the photo loses its opening.
   Split only the selected clip at the photo end, restart its visible suffix at
   the original in-point, and preserve all other timeline boundaries.
5. Save/receipt: accept full-screen lanes on caption saves; verify the final compiled
   sequence before recording success and invalidate evidence if a later edit removes
   or replaces either picture.

There is no new native rendering protocol, app UI or feature flag. Existing
voiceover/reaction-beat, editor-lane, device capability and full-screen gates apply.
The N1 caption/text PR remains separate.

## Timeline decisions

1. Duration source: existing narrated video track tiled to authoritative voiceover
   duration. Sequence coverage replaces pictures within that duration.
2. Ripple: none. No narration, subtitle, title, SFX or music timestamp shifts.
3. Scrub bounds: unchanged recipe duration; output/base mapping stays identity.
4. Resize: existing lane editor. Reject shortened/overlapping invalid sequence
   windows; don't steal time from the next shot or ending.
5. Undo: existing atomic editor document/save snapshots; no new gestures.
6. Preview/export parity: final recipe drives editor projection and native export;
   save/reload and native simulator evidence are separate from model understanding.

## Failure matrix and deliberate limits

Covered: image then selected original; default/explicit photo holds; full-screen
image/video lanes with PiP above; same-duration wrong-photo and wrong-video controls;
unselected/foreign/Visuals-video follow targets; empty all-scope selection; missing
photo/lost lane; missing cue; duplicate/ambiguous following source; photo overlap;
no visible room; full-screen flag off; caption save; lane removal and stale success;
retained video capabilities; unchanged legacy Talking/PiP serialization; title/audio/
caption/duration/final-shot preservation. Multiple independent sequences may use
different originals. A later intentional cutaway may cover the video after the
minimum visible window; evidence records only the actually visible interval.

The compiler handles only a first-occurrence photo followed by a uniquely aligned
selected original. Repeated-target sequences and ambiguous placements recover.
No independent insertion of time, crossfade design, arbitrary reordering, synthetic
media generation or new client interaction is part of this fix. Native replay uses
an explicitly authored schedule and local source media, not a live planner or
measured speech alignment. EN/TR grounding neighbors run in the existing gates;
novel multilingual model interpretation remains unverified.

## Completion boundary

Claim: **local fix**, with offline contract and native renderer evidence. Related
parent voiceover-planner incident: **KRI-598**; keep it open. A new N2-specific ticket
was not created because external disclosure approval is pending. Remaining gaps
are tracked in this PR: live interpretation of the exact request; measured speech
alignment on the affected account; the final TestFlight request/export/receipt replay;
and effective rollout checks after merge. Do not call the entire N2 request fulfilled
based on authored replay or the production health check alone. Raw footage, audio,
private transcript, credentials and signed URLs are excluded from git.
