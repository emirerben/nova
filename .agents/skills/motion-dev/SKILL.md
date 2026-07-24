---
name: motion-dev
description: Motion.dev-inspired animation craft for Nova and frontend work. Use when designing, tuning, or reviewing UI/video-preview motion, Motion/Framer-style easing, springs, keyframes, timelines, staggered reveals, view transitions, or renderer-parity animation math where browser preview must match a non-browser renderer such as Skia.
---

# Motion Dev

## Overview

Use Motion.dev as a motion-design reference, not as a blind dependency. Prefer its primitives: named easings, `cubicBezier`, springs, keyframe `times`, sequences, and staggered delays. In Nova video work, browser preview and Skia burn must share deterministic math.

## Workflow

1. Identify the animated value contract: transform, opacity, clip/mask, path, or text reveal. Name every value that must match across preview and render.
2. Choose the primitive:
   - Use `cubicBezier`/named tween easing for timeline-locked video effects.
   - Use springs for interactive UI feedback where exact frame parity is not required.
   - Use keyframes with `times` when there is a hold, ramp, overshoot, or cleanup phase.
   - Use sequence/stagger patterns for multi-element entrances.
3. Preserve parity. If output is rendered outside the browser, port the easing function and constants into both implementations. Do not import Motion only in the web preview unless the production renderer samples the same curve.
4. Respect reduced motion for live UI. Video-render math can still animate, but editor chrome and nonessential loops need `prefers-reduced-motion` or `motion-safe` behavior.
5. Verify with focused tests and, for Nova Skia changes, `make verify-overlays`.

## Nova Rules

- Keep renderer constants adjacent and mirrored. TS preview comments should point to the Python source and vice versa when useful.
- Avoid physics springs for burned video unless the spring is sampled into deterministic keyframes or an equivalent closed-form helper.
- For transition-like text effects, prefer scale/opacity/clip transforms over positional sliding unless the user explicitly wants travel.
- If the effect is selectable in an editor, update all vocabularies, schema allowlists, and round-trip tests.

## Reference

Read `references/motion-primitives.md` when choosing a Motion primitive or porting Motion-style math.
