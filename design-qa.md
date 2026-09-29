# Glass wordmark design QA

## Evidence

- Source visual truth: the upper panel in `.design-qa/glass-comparison.png`.
- Browser-rendered implementation: `.design-qa/glass-final-desktop.png`.
- Browser-rendered build midpoint: `.design-qa/glass-build-midpoint.png`.
- Combined focused comparison: `.design-qa/glass-comparison.png`.
- Scroll-state captures: `.design-qa/scroll-{35,50,70,100}.png`
- Viewport: 919 × 791 CSS px, desktop, device scale factor 1.
- Source pixels: 1103 × 1426. Implementation pixels: 919 × 791.
- Density normalization: the focused comparison crops each logo region and normalizes both to 640 × 330 before stacking them vertically.
- State: completed 2.9-second wordmark reveal plus a separately captured 2.25-second build midpoint, hero video playing, no scroll morph applied.
- Full-view evidence: the implementation remains centered over the full-bleed reel with the CTA and playback control unobstructed.
- Focused evidence: the combined comparison shows the source above and the implementation below at equal logo-region dimensions. A focused crop is required because material fidelity depends on the edge, highlight, and refraction detail.

## Required fidelity surfaces

- Fonts and typography: the exact production Kria vector paths remain the source for the pale glass-edge write-on, glass mask, and lighting pass. No font substitution or altered letter geometry is present.
- Spacing and layout rhythm: the lockup retains the existing centered size and aspect ratio; the new material layers do not change hero composition or CTA spacing.
- Colors and visual tokens: the live result now matches the source's cool clear body, white upper-left specular light, pale-blue rim, and navy lower edge. The source uses stronger synthetic warping; the live treatment keeps distortion restrained to avoid shimmer while footage moves.
- Image quality and asset fidelity: the hero's synchronized duplicate is magnified inside the exact production SVG mask, producing real moving refraction instead of a flat translucent fill. The SVG lighting pass remains sharp at browser scale and shows no duplicate geometry or transparency halo.
- Copy and content: unchanged.

## Comparison history

1. P1 — The original implementation read as a flat frosted cutout. It used a pale translucent fill and blur but lacked the concept's clear center, molded rim, and internal refraction.
   - Fix: introduced a synchronized magnified video core, separated depth/glint layers, and reduced the face tint.
   - Post-fix evidence: the final implementation visibly carries the moving scene through each letter rather than covering it.
2. P1 — Early depth tuning darkened the entire silhouette and made it read as navy plastic.
   - Fix: restricted dark material to the lower edge and replaced the broad depth fill with a transparent SVG lighting pass.
   - Post-fix evidence: the final comparison retains transparency while preserving volume over both bright sky and detailed scenery.
3. P2 — A non-scaling external SVG stroke produced faint duplicate geometry below the wordmark.
   - Fix: removed the external stroke treatment and generated bevel, edge, and specular light from the source image alpha in one local SVG filter.
   - Post-fix evidence: two browser frames were inspected; both show a single clean lockup with no ghost outline.
4. P1 — The first motion pass completed a solid white wordmark before swapping materials, so the animation described a color change instead of glass construction.
   - Fix: the refracted video core, pale edge trace, bevel, glints, and lower rim now build together from 0.8s to 2.9s. The solid-white fill phase was removed.
   - Post-fix evidence: `.design-qa/glass-build-midpoint.png` shows the wordmark already reading as transparent glass while it is still forming.
5. P1 — The scroll scrub ran a JavaScript scroll listener on every frame, mixed layout reads with several style writes, and repainted a large clipped video layer.
   - Fix: supported browsers now use a native root scroll timeline with compositor-friendly opacity and transform animation. The JavaScript path remains only as a compatibility fallback, with geometry cached outside its animation frame. The large video no longer animates `clip-path` on the native path.
   - Post-fix evidence: the same 1.8-second automated scroll profile reduced TaskDuration from 62.70 ms to 36.82 ms, ScriptDuration from 14.09 ms to 7.96 ms, and RecalcStyleDuration from 17.87 ms to 5.84 ms. No long tasks were recorded. The four scroll-state captures confirm the visual choreography is preserved.
6. P1 — The completed glass shell faded away as the masked reel entered, leaving the final video-filled wordmark flatter than the constructed logo.
   - Fix: the construction trace still dissolves, but the glass contour, depth, glints, and lighting now remain fully visible over the moving reel for the entire scroll.
   - Post-fix evidence: refreshed 70% and 100% scroll captures retain the same molded rim and translucent surface while the footage continues moving inside the letters.
7. P2 — The completed wordmark was materially rich but perfectly rigid, which worked against the inflated glass-balloon character.
   - Fix: K, R, I, and A now use separate 8-second transform-only drift paths (1–2 px, under 0.4 degrees) with staggered phases. The video masks and matching glass surfaces share each letter's motion, pause with playback, and disable movement under reduced motion.
   - Post-fix evidence: live computed transforms changed independently for all four letters across a 1.8-second sample. A fresh scroll profile recorded no long tasks, 6.84 ms script time, and 1.25 ms layout time.

## Findings

No actionable P0, P1, or P2 mismatches remain.

The implementation is intentionally less warped than the still concept because aggressive displacement on continuously moving footage would introduce unstable shimmer. The retained magnification, bright bevel, dark lower edge, and localized glints preserve the same material read without compromising motion quality.

## Interaction and implementation checks

- Hero reveal completed and remained stable across multiple video scenes.
- Mid-build capture confirms there is no solid-white intermediate wordmark.
- CTA and pause control remained visible and unobstructed.
- Native scroll-timeline mode registers no JavaScript `scroll` listener; the compatibility fallback retains the same progress mapping.
- The finished glass shell remains at full opacity from scroll start through the final masked-reel state.
- Each finished letter drifts independently without adding another video element; the page still uses two synchronized video nodes total.
- Focused Jest suite: 14/14 passing, including the no-scroll-listener native-path regression test.
- TypeScript: passing.
- Frontend lint: passing with only pre-existing warnings outside this component.

## Follow-up polish

- P3: consider a production-video-specific displacement map only if future browser support allows stable per-frame refraction without shimmer.

final result: passed
