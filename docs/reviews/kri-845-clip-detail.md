# KRI-845: clip records name props, actions and blur

Evidence note. No media is stored here; the footage stayed in the two test kits. No AI of any kind produced
anything below: frames were looked at, sharpness and camera speed were measured with OpenCV / ffmpeg.

## The bug

`prompts/analyze_clip.txt` never asked for objects, movements or how the footage looks, and the exclude / order
resolver sees only `ClipUnderstanding.prompt_view()`. So in prod:

- Café M1 (2026-10-19, thread A9100235): "skip the short clip where Caro bends and puts the plate on the table"
  could not tell M1_05 (the skip) from M1_08 (the clip the creator pinned FIRST, "the one where Caro came with the
  black tray"). Prod recorded them almost inverted: M1_05 "hands over a plate of food to an off-camera customer",
  M1_08 "approaches a table, places a plate of food".
- Don Julio M1 (2026-10-21, thread F4083F1D): the menus clip M1_09 was recorded as "waiter carrying a rectangular
  tray"; the blurry table M1_04 as "the camera pans across a restaurant table" with no blur note, so "Skip the blurry
  shot of the empty table" ended in the KRI-807 picker.

## Ground truth from the footage

All 21 clips: 1080x1920, H.264, 30 fps. The READMEs of the kits were NOT used where they differ from the frames.

| Clip | Length | What the frames show |
|---|---|---|
| Café M1_05 | 2.2 s | NO tray. Caro carries a beige plate with the oblea in both hands toward the camera (0-0.9 s), bends forward deeply (0.99-2.09 s) and lowers the plate out of the bottom of the frame (onto a table never visible). Sharp. |
| Café M1_08 | 5.0 s | The ONLY clip with a flat black round tray. She already stands in frame (does not walk in) holding it (0-1.2 s), leans forward (1.9-3.1 s) to pick up a plate, holds it out to the camera (3.3-3.6 s), walks off right with the plate and the tray (3.9-5.0 s). Never sets the plate down on camera. Sharp. |
| Café M1_03 | 4.77 s | Laughs, waves both hands at about 3 s, walks off right; holds nothing. Sharp. |
| Café, all others | | Sharp. No café clip is blurry or shaky. |
| Don Julio M1_09 | 2.5 s | The waiter lifts a thick dark wooden slab (two stacked menu boards) flat on his palms to face height, carries it sideways. Nothing on it. Held like a tray, which is why prod says "tray". |
| Don Julio M1_04 | 2.6 s | Fast handheld pan (about 32 px/frame) across a white-tablecloth table. First ~0.5 s crisp, then directional motion smear (fork tines doubled, glass rims ghosted). Nothing is out of focus, and at 360 px wide it reads as a fast pan, not as blur. |
| Don Julio M1_06 | 2.0 s | A similar-speed tilt-down (about 28 px/frame) from a diner holding a menu board to bread, sauce bowl, salad. Subjects stay crisp. |

She bends in BOTH café clips, so "bends" alone separates nothing; the tray and the direction (lowers a plate down vs
picks one up and walks off) do. The full per-clip record with times is `tests/fixtures/kri845_m1_clip_detail.json`.

### Sharpness (lower Laplacian = less fine detail)

| Don Julio | Laplacian var @540 | @360 | blurdetect (higher = blurrier) | camera px/frame |
|---|---|---|---|---|
| M1_04 (blurry pan) | 33.6 | 70.4 | 6.82 | 31.7 |
| M1_06 (sharp look-alike) | 59.9 | 138.4 | 7.02 | 28.2 |
| M1_08 | 125.7 | 262 | 6.55 | 9.7 |
| M1_07 | 142.6 | 318 | 6.37 | 20.5 |
| M1_02 | 247.2 | 525 | 6.20 | 6.3 |
| M1_05 | 404.0 | 843 | 5.16 | 3.7 |
| M1_09 (menus) | 419.9 | 790.9 | 5.49 | 5.8 |
| M1_03 | 527.5 | 952 | 5.31 | 4.6 |
| M1_01 | 562.8 | 1032.8 | 5.61 | 9.1 |

Café (every clip is sharp): M1_07 186.3, M1_02 205.1, M1_09 271.1, M1_01 280.5, M1_11 308.8, M1_10 321.5,
M1_05 350.9, M1_06 365.7, M1_04 380.2, M1_08 397.3, M1_12 418.8, M1_03 643.6; camera at most 6.6 px/frame in all.

No single number marks M1_04 as blurry: it is the lowest on Laplacian, but ffmpeg `blurdetect` ranks M1_06 above it,
and brightness-normalised (lap / luma^2) they are equal (0.0026 vs 0.0028). Both are low-texture white tablecloth
shots at about 30 px/frame. So blur has to come from the vision model's judgment of the frames, which is why the
analysis prompt now defines blur by the frames (smeared, doubled or ghosted edges for much of the clip), not by a
camera-speed rule.

Method (the same on every clip):

```
# 6 frames at the middle of 6 equal slices, 360 px wide, 3x2 contact sheet
ffmpeg -v error -ss "$t" -i clip.mp4 -frames:v 1 -vf "scale=360:-2" -q:v 3 frame_$i.jpg   # t = dur*(i+0.5)/6
ffmpeg -v error -framerate 1 -i frame_%d.jpg -vf tile=3x2 -frames:v 1 sheet.jpg
# ffmpeg blur score (mean over all frames, 540 px wide)
ffmpeg -v error -i clip.mp4 -vf "scale=540:-2,blurdetect,metadata=print:key=lavfi.blur:file=-" -an -f null -
# Laplacian sharpness: 8 evenly spaced frames, gray, resized to 540 (or 360) px wide, median of the variance
python3 -I -c 'import cv2,statistics,sys; c=cv2.VideoCapture(sys.argv[1]); n=int(c.get(7)); v=[]
for i in range(8):
    c.set(1,min(n-1,int(n*(i+.5)/8))); ok,f=c.read(); g=cv2.cvtColor(f,6); g=cv2.resize(g,(540,round(g.shape[0]*540/g.shape[1])),interpolation=cv2.INTER_AREA)
    v.append(cv2.Laplacian(g,cv2.CV_64F).var())
print(statistics.median(v))' clip.mp4
# camera speed: cv2.phaseCorrelate between consecutive 135 px wide gray frames, x8 to 1080 px, median
```

## What changed

- `analyze_clip.txt` asks for `props` (0-6 distinctive objects with colour / material / size), `actions` (0-4 verb
  phrases in order, with the prop and the direction of each movement) and `visual_quality` (the camera move AND
  whether the detail stays crisp; smear is blurry even when the subject is recognisable; "fast pan, stays sharp" for a
  fast move that stays crisp; "sharp and steady"; never brightness).
- The fields go through `ClipMetadataAgent.parse` -> `ClipMeta.clip_*` -> `understanding_payload` ->
  `ClipUnderstanding` (`exclude_if` empty: records written before the change are byte-identical) -> `prompt_view()`
  -> the resolver prompt, which says a "blurry / shaky / out of focus" ask matches reported blur, not "stays sharp",
  and that a clip without the fields was not judged.
- The Main Creator's chat evidence pops the three keys (its input is unchanged). Prompt versions
  `2026-10-23.1` for `clip_metadata` and `clip_request_resolver`; `CACHE_SCHEMA_VERSION` s4. `ANALYSIS_VERSION` is NOT
  bumped (that would re-run every stored video analysis, paid and prod-wide), so only clips analysed after the deploy
  carry the fields: re-attach the footage in a NEW chat.

## What is proven offline (no paid calls)

`tests/kria/test_kri845_clip_detail_evidence.py` (33 cases; on main 21 fail and 12 pass, the 12 being the
unchanged-behaviour sentinels, the pin check and the two negative controls) with the authored outputs in
`tests/fixtures/kri845_m1_clip_detail.json` and the Don Julio prod records of `kri807_don_julio_m1_clips.json`:

- the three fields survive `parse()`, the `analyze_clip` shim and the payload writer; junk values, comma strings,
  role markers, fences and runaway lists are cleaned or capped; records without the fields and the Main Creator chat
  evidence are unchanged;
- the REAL rendered resolver prompt carries the evidence and tells the two café clips apart (tray only on M1_08, the
  put-down only on M1_05) and the Don Julio ones (menu boards on M1_09, "blurry" on M1_04 and not on M1_06);
- through the real planner, resolution and exclusion code, with a STAND-IN resolver (a few string rules over the
  records in the prompt it is handed): the café skip drops M1_05 only and M1_08 stays first with no picker; the Don
  Julio menus resolve to M1_09 and M1_04 is excluded with no picker and no vision call;
- negative controls on the records as main wrote them: café asks both pickers with nothing dropped; Don Julio's skip
  is unmatched and the KRI-807 picker asks;
- wrong-evidence controls: a skip that matches both look-alikes, or evidence that puts the put-down or the tray on the
  wrong clip so the pin and the skip meet, asks and never offers or drops M1_08. KNOWN LIMIT: if the tray AND the
  put-down are both read onto the wrong clips, the answer is self-consistent and the code drops M1_08 without asking
  (pinned by `test_known_limit_fully_swapped_evidence_is_followed_not_second_guessed`); the only guard is correct
  evidence.
- four hand-authored replay goldens (2 `clip_request_resolver`, 2 `clip_metadata`) pass the free replay evals and
  satisfy the eval-input CI gate.

## What is NOT proven (needs a paid live check)

- That Gemini writes "black round tray" for M1_08 and no tray for M1_05, "thick wooden menu boards" for M1_09, and
  "blurry, fast pan" for M1_04 but "fast pan, stays sharp" for M1_06. The stand-in resolver does not show that
  Gemini reads the evidence the same way either.
- Bounded live check (development ledger, `--usage-purpose=live_eval`, hard cap well under $2): the 21 clips
  (about 91 s of footage, on the analysis proxy) through `clip_metadata`, then the two resolver calls (café request,
  Don Julio request) on the resulting records. Estimate from the AgentSpec / `ai_cost_control` prices
  (gemini-2.5-flash: $0.30/M input, $2.50/M output incl. thinking, $1.00/M audio input): clips about 84k input tokens
  (23k video + 60k of prompt text at about 2.9k per call) + 3k audio + about 25k output = about $0.09; resolver
  2 calls x (6.5k in, about 1k out) = about $0.01; total about $0.10. If each clip cost the $0.03 that
  `kria_clip_understanding` documents as its ceiling, 21 clips would be about $0.63. Run it with
  `--max-cost-usd=1` and look at the written `props` / `actions` / `visual_quality` of M1_05, M1_08, Don Julio M1_04,
  M1_06, M1_09 before judging the resolver.
- Then the iPhone check, in a fresh chat after the deploy: attach the café M1 clips and send the 2026-10-19 request
  ("Caro'nun elinde siyah tepsiyle geldiği klip ilk olsun ... eğilip tabağı masaya bıraktığı kısa klibi kullanma"):
  M1_08 first, M1_05 gone, no picker. Attach the Don Julio M1 clips and send "... the big wooden menus ... Skip the
  blurry shot of the empty table": the menus are M1_09, the table pan is left out without a picker. Old chats keep
  their old records: that is expected.
