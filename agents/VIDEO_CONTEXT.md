# Video Domain Context — Nova

Read this before working on any Nova video processing task.

## ⚠️ Critical Anti-Pattern: Do NOT use MoviePy / VideoFileClip

There is an existing `~/src/vid-to-audio/` project using `moviepy.VideoFileClip`.
DO NOT use this pattern:
  video = VideoFileClip(path)   ← loads ENTIRE video into RAM

A 2GB source file = 2GB RAM consumed. This crashes on any real video.
Use subprocess FFmpeg directly (patterns below).

## Pipeline Stages
1. **Ingest** — accept upload (multipart or pre-signed URL), store raw file in cloud storage
2. **Probe** — `ffprobe` metadata: duration, resolution, fps, codec, audio channels
3. **Transcription** — Whisper (or equivalent) for spoken word detection + timing
4. **Scene detection** — detect cut points and scene changes (PySceneDetect or manual frame diff)
5. **Segment scoring** — score segments for hook potential (energy, speech density, motion)
6. **Hook selection** — pick best 2-3 second opener + best 45-55 second core
7. **Reframe/crop** — convert to 9:16 aspect ratio (scale + pad or smart crop to subject)
8. **Audio sync** — normalize loudness (`ffmpeg loudnorm`), sync to cuts
9. **Export** — render final output to platform specs

## Export Specs
| Platform | Resolution | Aspect | Codec | Max Duration | Target Bitrate |
|---|---|---|---|---|---|
| TikTok | 1080×1920 | 9:16 | H.264/AAC | 60s | 2-4 Mbps |
| Instagram Reels | 1080×1920 | 9:16 | H.264/AAC | 90s | 3.5 Mbps |
| YouTube Shorts | 1080×1920 | 9:16 | H.264/AAC | 60s | 8 Mbps |

## Key FFmpeg Patterns

**Reframe to 9:16 (with letterbox pad):**
```
ffmpeg -i input.mp4 \
  -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
  output.mp4
```

**Audio normalization (EBU R128):**
```
ffmpeg -i input.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 output.mp4
```

**Segment concat (concat demuxer):**
```
ffmpeg -f concat -safe 0 -i segments.txt -c copy output.mp4
```

**Stream a clip without loading full file:**
```python
# subprocess FFmpeg — do NOT use VideoFileClip
import subprocess
subprocess.run([
    'ffmpeg', '-ss', str(start), '-to', str(end),
    '-i', input_path, '-c', 'copy', output_path
], check=True)
```

## Template Mode: Interstitials and Transitions

Template mode inserts user clips into a template structure. Between slots, interstitials express transitions that can't be modeled as xfade.

**Interstitial types:**
- **curtain-close**: black bars grow from top/bottom edges, hold black, then next clip begins
- **fade-black-hold**: uniform fade to black, hold, then next clip
- **flash-white**: quick white flash between clips

**Detection** (`interstitials.py`): Sample 6 frames before each black segment detected by `blackdetect`. Compute luminance in 3 horizontal bands (top/middle/bottom) via `signalstats` YAVG. If top+bottom darken faster than middle (ratio > 1.5), classify as curtain-close. Uniform darkening = fade-black-hold.

**FFmpeg blackdetect thresholds** (lowered for template analysis):
```
blackdetect=d=0.15:pix_th=0.15
```

**ASS subtitle filter with bundled fonts:**
```
subtitles=overlay.ass:fontsdir=/path/to/assets/fonts
```

**Transition vocabulary** (`transitions.py`): Gemini outputs human-friendly names, translated to FFmpeg xfade types: hard-cut to none, whip-pan to wipe_left, zoom-in to crossfade, dissolve to crossfade, curtain-close to none (handled by interstitial clip instead).

## Virality Framework
- **Hook (0-3s):** Create a question/curiosity/emotion. Cut dead air. Start mid-action.
- **Retention (3-30s):** Remove pauses >1s. Add captions (Whisper timestamps). Keep energy high.
- **Peak (30-55s):** Land the emotional payoff or surprising reveal.

## Processing SLA
- MVP target: ≤5 minutes for a 10-minute source video on standard CPU
- Jobs are ASYNC via Celery + Redis — never block an HTTP request for processing
- Status: `GET /jobs/:id/status` → `{status: queued|processing|done|failed, progress: 0-100}`

## AI Models
- **Transcription:** `openai/whisper-large-v3` (API) or local `faster-whisper`
- **Scene detection:** PySceneDetect (no GPU required)
- **Smart crop / face tracking:** MediaPipe (optional at MVP)

## Rules
- Do NOT buffer entire video in memory (no VideoFileClip)
- Do NOT block on FFmpeg in the request thread (always Celery queue)
- Do NOT store uploads/outputs in git or the app container
- Do NOT generate synthetic video — Nova enhances real human footage only
