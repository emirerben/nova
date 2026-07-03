# Plan 008 — Live preview edit mode (delta on 005/006/007)

**Status:** IMPLEMENTED 2026-07-03 (backend signing + Hero live-edit mode + card-video sync + applied-SFX audio wiring fix; 12 new tests, full suite 1023 green)
**Fix 2026-07-03:** live mode only existed in `Hero` — instant-edit-eligible variants
(`agent_text` + base_video_url, i.e. most generative variants) render through
`LiveEditPreview`, which had no card layer, so timeline edits never reached the
preview (the known LiveEditPreview/Hero split gap, third occurrence). Mirrored the
full wiring into `LiveEditPreview`: same live-mode latch, pre-overlay source
selection + `live:` identity, `LiveOverlayCardsLayer` (above the text layers to
match bake order) and `HeroOverlayEditor`. Verified headless on a real job: hero
plays the pre-overlay base and a persisted 59%/Center popover edit shows live.
**Ask (verbatim):** "make the edits on visuals in the timeline real time change on
the preview in the left and render when user downloads the final output."

## Mechanism

Baked pixels can't be edited client-side — so while overlay cards exist AND the
overlay-clean base survives (`pre_media_overlay_video_path`, captured at first
burn), the hero plays the CLEAN BASE and renders every card as a live CSS layer:

```
timeline edit ──▶ overlayCards state (already lifted, render:false autosave)
                        │  instant re-render
                        ▼
hero: <video src=pre_overlay_video_url>  ◄── NEW: signed in _variants_for_response
      └─ CSS layer: overlayCardStyle(card) per card, time-gated,
         image → <img preview_url>; video → muted <video> seeked to
         clip_trim_start + (heroTime − start_s), lockstep play/pause
SFX  : useSfxPreview (already wired on the hero — placements re-arm on change)
Download ──▶ existing bake flow (render:false persisted metadata → burn)
```

- Backend: one addition — `pre_overlay_video_url` signed on read (same graceful
  pattern as `base_video_url`). DONE.
- Frontend: live-edit mode in the Hero + card-video time sync + verification
  that every lane edit path updates the lifted state. Gestures stay where they
  are (lanes for applied cards, HeroOverlayEditor for suggestions).
- No new burn paths, no new flags — Download semantics unchanged.

## Tests
Live-mode on/off source selection, style reflection on state change, card-video
trim-offset sync, rendering-in-flight guard, no-network layer, existing
render:false + download flows regression-green.
