# KRI-1042: picture frame profile for soft analysis proxies

`phone-simple-cut-v4` rejected the correct Lisbon export (job
`3daccc8c-b263-430a-a62f-5821f7b3b647`) on its picture check. The verifier compares each
sampled export frame with the analysis proxy. That proxy is 568x320 at about 0.7 Mbps. On
detailed handheld footage it is soft, so 9 of the 16 samples scored 0.89-0.95 local SSIM,
under the fixed 0.95 bar. Colour, crop and timing were exact. The displayed source frame was
the best match in all 16 samples, and its neighbours scored 0.34-0.94 (repeated proxy frames
left out).

## The rule

v5 keeps the exact bar and adds a frame profile for samples below it. The profile reads
the previous and the next source frame that v4 already decodes.

- A window qualifies when its mean displayed SSIM is at least 0.93 and the displayed frame
  leads both neighbours by at least 0.02 on average. The lead is displayed SSIM minus
  neighbour SSIM, per sample, then averaged over the window.
- A sample below the bar then passes when its error is at most 0.1, its SSIM is at least
  0.85, and no neighbour scores higher.
- A neighbour within 1.5 codes mean difference of the displayed frame repeats it. A 30 fps
  proxy of a 25 fps original does this every sixth frame. A repeat adds no lead and is left
  out of the per-sample comparison.
- The exact bar, the next-frame tolerance and the whole-window shift rule are unchanged, so
  every window v4 passes, v5 passes.

A shift moves the best match to a neighbour, so the lead toward that neighbour goes
negative. Still footage has no best frame and leads near 0, so it keeps the exact bar.

## Why a window lead, not a per-sample margin

The first proposal was a margin of about 0.02 per sample. The slow crowd shot in Lisbon
(`db9af674`) fails that. At 2.4 s the displayed frame beats the previous one by 0.007, and the
next one is a repeat. Across the window the same clip leads by 0.042 and 0.049. Over the
replay corpus, the per-sample margin rescues 6 of 25 correct windows that v4 rejects, and the
window lead rescues 17. Neither accepts a shift.

## Calibration corpus

- The Lisbon scores, 16 samples in 3 clips, measured by the real verifier code during the
  investigation. The media was not retained, so this is a replay of stored scores.
- Real iOS exports. The phone's proxy generator and exporter ran on 9 kinds of footage
  (stripes, Mandelbrot zoom, noisy and plain test patterns in SDR, HLG and full range, a
  sparse line) at 25 and 30 fps, on 9:16 and 16:9 canvases. Negative controls shift the
  recipe by 1 or 2 frames either way, swap clip windows, or re-encode the correct export
  with a zoom, an offset, a tone change, a blur or a low bitrate.
- Generated footage. FFmpeg Mandelbrot footage (handheld shake with motion blur, calm pan,
  slow zoom, still, still with grain), a 568x320 proxy at 700 kbps, and compositor-style
  exports. The regression tests build this footage.

## Separation

The table is an offline replay at the shipped parameters. It covers the windows v4 rejects,
25 correct and 241 shifted or wrong-clip.

| Condition | Correct, lowest that v5 passes | Shifted or wrong clip, highest | Separates shifts |
| --- | --- | --- | --- |
| Window lead (lower of the two) | +0.027 | +0.004 | Yes |
| Per-sample peak (worst sample below the bar) | +0.0068 | -0.0005 | Yes, by a thin gap |
| Window mean SSIM | 0.934 | 0.969 | No |
| Lowest SSIM below the bar | 0.892 | 0.930 | No |

The four swapped windows of periodic stripes lead by +0.25 to +0.45. The stripe pattern
repeats in other colours, so luma SSIM stays high. The unchanged error cap (0.18 against 0.1)
rejects all four, as it does in v4, and two of them also fail the window mean.

Every other shifted or wrong-clip window fails the lead. All but one also fail another
condition, and that one fails the unchanged whole-window shift rule.

The parameter sweep (floor 0.80-0.90, lead 0.01-0.06, window mean 0.90-0.95) accepts no
shifted or wrong-clip window in any cell. Lead 0.01 and 0.02 rescue the same 17 correct
windows. The midpoint of the lead gap is 0.016. 0.02 sits closer to the correct side, so
an ambiguous window is rejected. The window mean decides only tone errors. With it at 0.90,
0.93 and 0.95, 41, 27 and 12 wrong-picture windows pass, and at 0.95 only 5 correct windows
pass. The full tables are in
[kri-1042-picture-profile-replay.json](kri-1042-picture-profile-replay.json).

The real v4 and v5 verifiers on the same 564 picture windows agree with the replay. 241
shifted or wrong-clip windows v4 rejects stay rejected. 30 correct windows flip to checked.
27 wrong-picture windows flip, all brightness (about 8 codes), saturation, blur or
low-quality re-encodes. No zoom or offset window flips.

## Limits

- One real export with soft-proxy footage. The rest is generated footage or test patterns
  through the real iOS proxy generator and exporter.
- A tone change that keeps SSIM at 0.93 or more now passes on soft-proxy footage. v4 already
  passed the same change on sharp-proxy footage. The check reads luma structure, not colour.
- A one-frame-late export on calm footage can pass through the next-frame tolerance, in v4
  and v5 alike. The whole-window rule catches it only when every sample needed the next
  frame.
- Still footage below the bar is still rejected. A noisy still shot of a correct export
  scores about 0.86 and has no lead.
