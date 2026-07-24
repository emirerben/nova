# Motion Primitives

Use this when translating Motion.dev ideas into production code.

## Core Docs Snapshot

- `animate()` supports HTML/SVG styles, independent transforms, CSS variables, SVG paths, objects, keyframe sequences, and per-value transition overrides.
- Tween transitions use `duration` plus `ease`. Motion accepts named easings, cubic-bezier arrays, and easing functions.
- Keyframes can use `times` to place hold/ramp/cleanup beats along the animation.
- Springs are excellent for gesture/UI feedback. Use duration/bounce springs for understandable timing, or stiffness/damping/mass for physics. For video-render parity, sample or port the spring rather than relying on browser-only runtime behavior.
- `stagger()` is the standard multi-element entrance pattern.

## Useful Defaults

- UI micro-interaction: `duration: 0.15-0.25`, `ease: "easeOut"` or a low-bounce spring.
- Panel/modal entrance: `duration: 0.22-0.35`, `ease: "easeOut"`; pair opacity with y/scale, not long travel.
- Video title transition: keyframes with a long hold, a short ramp, then opacity cleanup.
- Cinematic push-in: cubic-bezier `[0.76, 0, 0.24, 1]` gives a soft pickup, a decisive middle push, and a clean finish.

## Porting Rule

When Motion is not present in every renderer, name the curve after the Motion primitive but implement deterministic local math in each renderer:

```text
progress = cubicBezier(0.76, 0, 0.24, 1)(normalizedTime)
```

For Nova text overlays, mirror that function in `text_overlay_skia.py` and `overlay-animation.ts`, then pin behavior with both Python and Jest tests.
