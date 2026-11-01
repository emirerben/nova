---
name: generate-overlays
description: Design and refine supporting image and text overlays for Kria videos, including speech triggers, visual grounding, placement and taste feedback. Use when creating overlays or tuning overlay output quality.
---

# Generate overlays

Support the creator's footage with graphics. Keep the original video and audio. Render through Kria's native compositor. A generated still can use existing editor entrance and exit effects; describe that as animation applied to an image, never generated video.

Keep the prompt and preview inside the existing Visuals panel. Approval inserts an ordinary media image block, with the same timing, transforms, layering and animation controls as imported images. Do not create a separate generated-image editor or restrict later edits. Reopening an already-added proposal selects that block; it must not submit another generation request.

Read [taste.md](references/taste.md) when choosing an aesthetic or incorporating feedback. Read [examples.md](references/examples.md) when evaluating a changed prompt or planner.

## Plan from evidence

Honor an explicit request before choosing additional overlays. For automatic mode, use both speech and available pictured context. Silence is valid footage. Prefer no overlay when a graphic adds no meaning or the context cannot support it.

Tie each proposal to an exact quoted word, verified timed visual moment or explicit creator time. Resolve source times into the current edit before proposing placement. Do not treat a description of an entire clip as the time of an event inside it. Multiple occurrences require the requested occurrence or clarification. Do not infer the speaker's identity or emotion from their voice.

Use native editable typography for exact numbers and short labels. Use supplied or verified assets for logos. Preserve a name such as "Tanavachi" verbatim until its identity is clear. Do not substitute a similar brand or generate an imitation official logo. Ask for an upload when the exact mark is missing.

## Choose and place the asset

Prefer a transparent PNG cutout for an icon or illustration that should sit over footage. Avoid a new scene, rectangular stock-photo card or fake transparency checkerboard unless requested. Keep adequate source resolution for the displayed size and inspect actual alpha edges. Match the palette and visual weight to the footage and the creator's taste notes.

Place graphics away from faces, hands demonstrating something, screen content and captions throughout their whole interval. Evaluate moving subjects at more than one frame. Choose a smaller graphic or another interval when no clear area remains. Do not claim a safe zone is verified when only a text summary is available. The review must say which context was missing.

Keep each graphic long enough to read and short enough to leave. Avoid repeated decoration, spatial collisions and competing overlays. Review existing timeline content before adding more.

## Review and feedback

Inspect a composite preview at the intended size. Check meaning, exact timing, silhouette, palette, legibility, alpha and occlusion. Confirm audio is unchanged. Check undo and saved reopen. Keep the original project untouched during experimentation.

Record feedback as a concrete before/after preference in taste.md. For example, "Use one flat symbol instead of a glossy sticker" is actionable. A single rejected image does not establish a universal preference.

This skill guides developers and agents. The app does not load this file. Production behavior belongs in the reviewed overlay planner contract and prompt under src/apps/api/app/agents/overlay_generation.py. In-app Style preferences travel in taste_notes. When changing runtime behavior, update replay fixtures and the agent prompt version, then verify the real native preview before reporting completion. Do not enable production provider spending as part of taste calibration.
