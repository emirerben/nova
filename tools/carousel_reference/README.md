# Blossom Carousel browser reference harness

A deterministic browser reference for the four Blossom-carousel visual
effects (`scale-sweep`, `cover-flow`, `cards`, `flipbook`). Runs the REAL
`@blossom-carousel/web` component (vendored locally — see `VENDORED.md`)
through a scripted flick gesture, frame-stepped at exactly 30fps, and
records per-frame card poses (`trace.json`) plus a video (`reference.mp4`)
so the Python/Skia re-implementation in
`src/apps/api/app/pipeline/carousel/` can be compared against it pixel-
and-motion-wise.

## Layout

```
tools/carousel_reference/
  harness.js              # deterministic clock + gesture replay + trace capture
  gesture-trace.json       # the scripted flick (shared with the Python side)
  scale-sweep.html
  cover-flow.html
  cards.html
  flipbook.html
  capture.sh                # drives the browse daemon through one page
  lib/blossom-vendor/       # vendored @blossom-carousel/web (no CDN/network needed)
  VENDORED.md                # package provenance + which canonical CSS was reused
```

## Determinism contract

Reproducing the exact same frames/trace on every run depends on three
things all holding:

1. **Frozen clock.** `harness.js` overrides `performance.now`, `Date.now`,
   `requestAnimationFrame`, and `cancelAnimationFrame` *before* the Blossom
   module ever loads (it's a plain classic `<script src="./harness.js">`,
   which — unlike a `<script type="module">` — runs synchronously in
   document order, ahead of any deferred module script). Nothing in the
   page can observe real wall-clock time. Every `window.__step()` call
   advances the clock by exactly `1000/30` ms and flushes whatever rAF
   callbacks were queued at that instant — no more, no less.
2. **Scripted gesture, not real timing.** `window.__startGesture()` loads
   `gesture-trace.json`'s `drag_deltas_px` (finger-space deltas; negative =
   finger moves left). The *first* `window.__step()` call after
   `__startGesture()` dispatches `pointerdown` at `(540, 960)` on the
   carousel scroller element; each subsequent `__step()` dispatches one
   `pointermove` with `clientX` advanced by the next cumulative delta; once
   the deltas are exhausted, the next `__step()` dispatches `pointerup` and
   all `__step()` calls thereafter just keep flushing the rAF queue toward
   settle. Within one `__step()`, the pointer event is dispatched **before**
   the rAF queue is flushed — matching real-browser event ordering (input
   before rAF).
3. **DPR 1, fixed viewport.** Pages are laid out at a hardcoded 1080×1920
   (the target output canvas size) with no responsive breakpoints; capture
   uses `viewport 1080x1920 --scale 1` (deviceScaleFactor 1) so on-disk
   screenshot pixels equal CSS pixels 1:1, with no retina-scaling
   translation layer to keep in sync with the Python side.

Given the same `gesture-trace.json` and the same vendored Blossom build,
re-running `capture.sh` should produce byte-identical `trace.json` (modulo
floating-point rendering noise from the compositor, which the parity loop
should tolerate with a small epsilon, not expect literal byte-equality on
the PNGs).

## Capturing

**Prerequisites:**
- The gstack `browse` daemon CLI, resolved the same way `SKILL.md`'s
  preamble does: `./.claude/skills/gstack/browse/dist/browse` (project-
  local) or `~/.claude/skills/gstack/browse/dist/browse` (user-global).
  First invocation auto-starts the daemon (~3s); subsequent commands are
  ~100ms each.
- `ffmpeg` on `PATH` (used only for the final PNG-sequence → `reference.mp4`
  mux step; per this repo's `local-ffmpeg-no-libass` lesson, make sure it's
  a full build if you ever add subtitle/caption burning here — not needed
  for this harness, which is solid-color test cards only).

**Usage:**

```bash
cd tools/carousel_reference
./capture.sh scale-sweep out/scale-sweep
./capture.sh cover-flow  out/cover-flow
./capture.sh cards       out/cards
./capture.sh flipbook    out/flipbook
```

Each run: starts a throwaway `python3 -m http.server` bound to
`127.0.0.1` on an ephemeral port (torn down via `trap ... EXIT`), sets the
viewport, navigates to `http://127.0.0.1:<port>/<effect>.html`, waits for
`#ready` (appended by the page once `customElements.whenDefined
("blossom-carousel")` resolves), calls `window.__startGesture()`, then
loops `window.__step()` + a full-page screenshot per frame — checking
`window.__settled` after every step and stopping early once it flips
`true` (hard cap: 150 frames = 5s @ 30fps of post-gesture settle-polling,
generous headroom past the 12-delta scripted flick). It then dumps
`window.__getTrace()` to `trace.json` and muxes `frame_%04d.png` into
`reference.mp4` (`libx264`, `preset fast`, `crf 18`, matching this repo's
final-output encoder policy for anything that isn't a throwaway
intermediate — see root `CLAUDE.md` "Encoder policy").

**Why HTTP and not `file://`:** all four pages load the vendored Blossom
build via `<script type="module">import "./lib/.../blossom-carousel-web.es.js"`.
Chromium fetches module imports in CORS mode, and `file://` is an opaque
`null` origin that CORS mode rejects outright (classic scripts, like
`harness.js`, are unaffected — only `type="module"` enforces this).
Navigating straight to `file://.../scale-sweep.html` fails the import
silently (`net::ERR_FAILED`), the custom element never registers, and
`wait "#ready"` times out. `capture.sh` works around this by serving the
directory over a loopback `http.server` instead — see the comment above
the `HTTP_PORT=` line in `capture.sh` for the full mechanics.

## `trace.json` schema

```jsonc
[
  {
    "i": 0,                    // frame index, 0-based, matches capture.sh's frame_%04d.png numbering
    "scrollLeft": 0,           // carousel scroller's scrollLeft at this frame, px
    "cards": [
      {
        "left": 270,           // card's on-screen bounding-box left edge, viewport px (getBoundingClientRect)
        "top": 600,            // bounding-box top edge, viewport px
        "width": 540,          // bounding-box width, viewport px (reflects any transform scale)
        "height": 720,         // bounding-box height, viewport px
        "scale": 1.0,          // DOMMatrixReadOnly(computedStyle.transform).a — see caveat below
        "opacity": 1.0         // computed opacity, 0-1
      }
      // ... one entry per .card, in DOM order (card index 0-4)
    ]
  }
  // ... one entry per captured frame
]
```

**`scale` caveat:** `.a` is the matrix's `m11` component. For pure 2D
`scale()` transforms this *is* the scale factor. For the `cover-flow` and
`flipbook` pages, which combine `rotateY()` + `translateZ()` + `scale()`
into a single 3D `transform`, `.a` is the X-axis contribution of the full
3D matrix, not an isolated "scale" value — treat it as a rough proxy, and
prefer `width`/`height` (from `getBoundingClientRect()`, which already
reflects the full 3D projection) for anything that needs the true rendered
size.

Every keyframe across all four pages animates the `transform` *property*
(`transform: rotateY(...) translateZ(...) scale(...)`), never the
standalone `scale`/`rotate`/`translate` CSS properties — `getComputedStyle
().transform` does not reflect those standalone properties (they only
compose at render time), so using them would have made `trace.json`'s
`scale` field silently wrong. See `VENDORED.md` for the full note.
