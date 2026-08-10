---
name: verify-editor-timeline
description: Verify Nova editor features that insert, remove, move, resize, trim, retime, or project timeline content. Use before implementing or shipping changes to timeline duration, clips, timed lanes, playback/scrubbing, preview choreography, or render compilation.
---

# Verify Editor Timeline

Apply this contract to every editor timing feature, not only Carousel. The goal is one timeline model shared by editing, preview, and rendering.

## Before implementation

Write down these six decisions in the plan or PR description. Stop and resolve any missing answer:

1. **Duration source:** Name the canonical object/function that owns ordered entries, overlaps, and total output duration. UI components may consume it but must not recompute it.
2. **Ripple policy:** For every lane, declare whether insertion shifts points at/after the boundary and extends crossing intervals. Explicitly name excluded lanes such as a continuous music bed.
3. **Scrub bounds:** State which duration clamps pointer scrubbing, playback, keyboard seeking, ruler ticks, playhead geometry, and inverse output-to-base mapping. They must agree.
4. **Resize behavior:** Define left/right edge semantics, minimum/maximum values, rounding, whether the content stretches or trims, and what happens at adjacent overlaps.
5. **Undo behavior:** Define the snapshot boundary. A pointer gesture is one undo step; pointermove previews must not add snapshots or destructively rewrite stored timestamps.
6. **Preview/render parity:** Name the shared fixture or paired tests proving order, phase duration/frame rounding, transitions, and legacy behavior.

## Implementation invariants

- Store user-authored timed lanes in stable base time. Project into output time at preview and render boundaries; never repeatedly mutate persisted values when inserts move or disappear.
- Exact insertion-boundary timestamps are right-biased. Crossing intervals keep their start and shift their end.
- Use the canonical projection for ruler, clips, playhead, pointer scrubbing, playback, and transport duration.
- A manual timing model is deterministic: preserve authored order, honor phase values, and disable automatic jitter. Keep the legacy path byte-identical until the model is explicitly upgraded.
- Round render phases to the renderer frame rate and editor inputs to the documented UI step.
- Cap transition overlap against both adjacent blocks after all duration changes.

## Required regression matrix

Add the smallest pure tests covering:

- intro, middle, and outro insertion;
- removed entries and transition overlaps;
- exact-boundary point mapping and crossing intervals;
- downstream text, visual, motion/camera, SFX, and media-overlay ripple;
- continuous music exclusion;
- inverse scrub mapping;
- final ruler tick and post-insert clip geometry;
- both resize handles and one undo snapshot;
- preview/render order and exact frame counts;
- removal plus byte-identical legacy behavior.

For a new interaction surface, add a deterministic desktop fixture and desktop Playwright test. Do not rely on mobile-only projects for timeline geometry.

## Gate

Run from the repository root:

```bash
make verify-editor-timeline
```

Read failures as contract failures, not snapshots to update blindly. Then run the normal TypeScript, Ruff, and preship gates required by the changed files.
