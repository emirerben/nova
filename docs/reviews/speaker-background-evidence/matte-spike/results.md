# KRI-1114 Phase 0 spike: person-matte edge quality for a green-screen background

Throwaway harness. Nothing inside the nova repos was edited. All paths below are under
<scratch>/matte-spike/

Harness: src/Lib.swift + src/main.swift, built to bin/spike (`bin/spike edge|rate|timing`). It mirrors the
production recipe in NativeSubjectMatte.swift (CIContext render to BGRA buffer, VNGeneratePersonSegmentationRequest,
OneComponent8, the same [Float] copy loop, treatmentMaxPixels downsample, 0.40 cut, tiny-fragment drop, sigma-1 feather,
bilinear upscale). Feather and upscale run on CPU arrays instead of CIGaussianBlur, so recipe A is faithful in
resolution and thresholds, not bit-exact.

## Footage (all real people; all LOW resolution, upscaled to a 1080x1920 canvas with lanczos)
- poster: nova-speaker-background/src/apps/web/public/plan/type-posters/talking.mp4 (416x740, 5 s, clean wall). Frames/poster/f075.png.
- feat: .../src/apps/ios/Kria/Resources/FeaturedEdits/featured-talking.mp4 (640x1138, 7 s used, busy staircase background,
  fast hand gestures). Burned-in captions and stickers appear from ~1 s; they sit over the body and slightly pollute segmentation.
- dl: ~/Downloads/3D5117EE-9309-483A-8D7A-023B65571FE3.MP4 (720x1280, t=8-10 s; dark hoodie, curly light hair, glasses). Kria captions burned in at the lower left.
No native 1080p footage of a person was available (the HEIC/JPG photos found were other people, screenshots, or low-res), so
the edge-refinement verdict is weaker than the rest.

## Q1 edge quality (montages, 2x nearest-neighbor zoom of 300x300 canvas crops over a magenta/cyan checker)
Tiles: ORIGINAL, A prod, B balanced soft, C accurate soft, F C+curve smoothstep(.3,.7), J curve(.4,.6), I accurate hard cut .5,
G1 accurate+gray guided filter (s4 r4 eps1e-3), H1 G1+curve.
- out/edge_dl_hair.png (curly hair vs wall: the hardest case)
- out/edge_poster_hair.png, out/edge_poster_shoulder.png
- out/edge_feat_head.png, out/edge_feat_rshoulder.png, out/edge_feat_larm.png (busy background, arm and sleeve)
- out/edge_dl_right.png
- full frames: out/full_<sample>_A.png (production) vs out/full_<sample>_F.png (recommended)
Facts measured/observed:
- Vision output size is fixed by quality, NOT by input size: balanced = 384x512, accurate = 1512x2016. Mean abs diff between
  270x480 and 720x1280 input masks is 0.0026 (balanced) and 0.0025 (accurate), under one 8-bit step. So option (d), higher input resolution, buys nothing.
- Production then downsamples the 384x512 balanced map to <=270x480 by integer stride: 384*512 > 129600, stride 2, so the cut and feather run on a 192x256 mask,
  upscaled 5.6x. That is the source of the blocky steps and haloes in A.
Visual verdict:
- A (prod) and B (balanced soft): wide pale halo of the original wall/wood leaking around hair, arm and sleeve; arm edge smears; stair-stepped hair top. Worst.
- C (accurate soft): tight edges, no wide halo. Still a thin translucent fringe and, on curly hair, a washed pink/cyan veil over the curls.
- F (accurate + steep curve .3/.7): cleanest overall. Crisp but anti-aliased silhouette, curls and hair strands kept, no halo. J (.4/.6) is a touch harder and more aliased.
- I (accurate hard cut, upscaled): stair-steps and chopped curls. Hard cut is worse than a steep soft curve.
- G1/H1 (gray guided filter): no visible gain over F, and G1 alone is worse than C on the arm (ghost translucency where skin and wood have similar luma). The refinement did not earn its cost on this footage.
Remaining flaw in every variant: light wall color stays in the hair fringe (no color decontamination / true alpha matting).

## Q2 mask rate (out/rate_report.md, out/rate_strip_feat_F.png)
Reference = a mask every frame (30 Hz). "step" = mean silhouette displacement per output frame in canvas px (sum|dM| / total variation).
feat (fast hands), accurate+curve pipeline: 30 Hz step mean 3.29, max 18.4. 15 Hz hold: even frames 5.69, odd frames 0.00 (the mask freezes then jumps 2x), max 25.8, min IoU 0.794.
15 Hz lerp offline: even/odd 2.85/2.87 (smooth), max 12.9, error vs 30 Hz mean 1.56 px. 15 Hz lerp causal (one tick late): error vs 30 Hz mean 5.4 px, max 26 px.
Same shape for the production balanced pipeline and for dl (head motion): hold alternates 5.2/0.0, lerp 2.6/2.6.
Strip look: hold shows frozen/offset hand silhouettes on alternate frames. Lerp is numerically smoothest but the linear mask blend is a cross-dissolve, so a fast hand leaves a translucent double image (ghosting). 30 Hz has neither stepping nor ghosting (it does show Vision noise on motion-blurred fingers).
Caveat: 30 Hz is the reference, not ground truth.

## Q3 cost on THIS Mac (not iPhone numbers; out/timing_report.md)
| quality | input | render CI->BGRA | Vision perform | [Float] copy | mask size |
|---|---|---|---|---|---|
| balanced | 270x480 | 1.9 | 10.4 | 0.1 | 384x512 |
| balanced | 540x960 | 1.7 | 10.7 | 0.1 | 384x512 |
| balanced | 1080x1920 | 2.0 | 11.2 | 0.1 | 384x512 |
| accurate | 270x480 | 2.1 | 34.9 | 1.4 | 1512x2016 |
| accurate | 540x960 | 1.9 | 35.1 | 1.5 | 1512x2016 |
| accurate | 1080x1920 | 2.3 | 35.6 | 1.5 | 1512x2016 |
Extras (Mac): CPU bilinear upscale of the accurate mask to 1080x1920 4.9 ms; CPU curve 1.4 ms; CPU gray guided filter 17.0 ms;
CoreImage wrap of the Vision buffer + scale to 1080x1920 + render 0.8 ms with no [Float] copy.
Input size does not change Vision time. Accurate costs ~3.4x balanced. The [Float] copy is negligible for balanced and ~1.5 ms for accurate.

## Recommendation
- Quality `.accurate`. Input stays 270x480 (anything larger is identical and slightly slower to render).
- Soft confidence plus a steep contrast curve (smoothstep ~0.3..0.7), no hard 0.40 cut, no 1 px feather. Apply at canvas resolution on the GPU (CIColorMatrix/CIToneCurve or a small kernel) from the CIImage-wrapped Vision buffer; keep coverage/fragment/stability gates on a small downsample only.
- Remove the 270x480 treatmentMaxPixels downsample for the render mask; it is what makes the current matte blocky.
- Rate: 30 Hz (a mask every frame) for export is the cleanest. If device cost forces 15 Hz, use hold for slow talking heads; do not use mask cross-fade (ghosts on fast hands) and do not use causal interpolation (66 ms lag).
- Edge refinement (guided filter): not recommended. No visible gain on this footage.

## Confidence and what a physical iPhone must confirm
- Medium-high on accurate > balanced and soft+curve > hard cut (consistent across 3 clips, 7 crops).
- Low-medium on "guided filter does not help": footage was upscaled from 416-720 px, so there was little true edge detail to guide with. Retest with native 1080p/4K phone clips and a color guide.
- Device must confirm: Vision accurate latency and thermal behavior per frame on A-series, whether it hits 30 Hz at export, whether accurate runs on the Neural Engine, memory of the 1512x2016 buffer, and the same quality on HDR/HEVC native footage.
- Not tested: multiple people, subject at frame edge, very dark/low-light, dark hair against a dark background, translucent hair color decontamination.
