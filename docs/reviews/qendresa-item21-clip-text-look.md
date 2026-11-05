# Qendresa item 21: her short-clip text look (written before the code, 2026-11-05)

Flag: `CLIP_TEXT_TIKTOK_LOOK_ENABLED` (default off). Related: KRI-523 (whole-video pins), KRI-648
(phone text style pass), KRI-706 / KRI-116 (text never covers a face). No ticket (Linear full).

## Her look, measured (V2 @2.5 s, V3 @5 s, 1080x1920 frames + Apple Vision OCR)

| | V2 (5.5 s) | V3 (11 s) |
|---|---|---|
| Lines | 2, centred (x centre 0.50) | 2, centred (x centre 0.50) |
| Block centre y | 0.343 (across her eyes) | 0.553 (over her body) |
| Cap height / x-height | ~50 / ~37 px | 43 / 32 px (TikTok Sans proportions) |
| Widest line | 0.69 of the frame | 0.67 of the frame |
| Colour | white | white |
| Shadow | faint: glyph surroundings 3-6 luminance units darker out to ~12 px, no clear offset | same |
| On screen | 100% | 100% |

Weight sits between TikTok Sans Regular and Bold (stem/cap 0.17-0.18 vs 0.14 / 0.23); Bold reads closer
by eye. Chosen look: TikTok Sans Bold, 56 px, white, max line width 0.72 (her V3 line breaks exactly:
"The prompt I run at the end / of every Claude session"), the existing authored shadow paint at
opacity 0.2 (sigma 12, dy 6): darkening -6 at 8 px, -4.5 at 12 px on a skin-tone patch, matching hers.
Everything is drawn by existing iPhone code (bundled font, portable blur layers, `shadow_opacity`), so
no protocol or verified token is needed.

## Ways it could fail

1. Flag off changes anything (prompt bytes, snapshot hash, rows, receipts).
2. Stored strategies / snapshots without the new fields serialise differently.
3. A model writes the new values while the flag is off: crash, 426, or a new look leaks out.
4. "In the middle" lands on her face (V2: face y 0.29-0.67): the line must move to the nearest clear
   spot, and the receipt must say why it is not in the middle.
5. Face sampling unavailable: the line stays centred; nothing may claim it was checked against a face.
6. "No captions" sends "middle"/"white" to captions (item 7 trap): her line's look must not depend on
   the parsed `requested` style.
7. The style pass resets font, shadow (default off), size or colour: the look must survive it.
8. The paid typography agent picks another font: her look's font must win.
9. The same words also written as `opening_title`: drawn twice for 2.2 s (breaks one_text/text_short).
10. A long line (up to 120 chars) wraps to many lines: must stay in frame and readable.
11. Emoji (V2 🤯): must still draw (colour emoji via the phone).
12. Two middle lines: must stack without overlapping.
13. A middle line scoped to seconds or a clip: only in that window.
14. Another text (title, label) on screen at the same time in the middle: must not overlap.
15. Landscape canvas: still centred, still in frame.
16. Editor round trip (iPhone Save) drops the shadow opacity or the look's fields.
17. Receipt says "Partly ... position" while the line is in the middle; or says "Done" after moving it.
18. Talking edits: a single top pin becomes a title (KRI-1009); a middle line must not be converted.
19. Old iPhone builds: must draw it (server-compiled layers only) or keep today's output.
20. Moving off a face lands on the Kria wordmark or out of the safe band.
21. White on a white background: accepted (her own look; she asked for white). A named colour wins.
22. A size she names ("big TikTok text") must win over the look's 56 px.
23. Turkish asks ("ortada", "TikTok'taki gibi") must be taught too.
