#!/usr/bin/env python3
"""Scene-by-scene glitch detector for rendered template outputs.

Investigates a final-cut MP4 produced by the template orchestrator and
finds every visible defect that a viewer might call a "lag" or "glitch":

  - frozen frames (full-frame MAD ≈ 0 between consecutive frames)
  - frozen background under animated text overlays (region-masked MAD)
  - regular periodic stutter from source-fps / output-fps mismatch
    (e.g. a 23.976fps source rendered at 30fps duplicates one frame
    every ~4 input frames → a 6 Hz visible stutter)
  - slot-boundary anomalies (cut shows no motion delta, or two
    consecutive deltas = ghost frame at the join)
  - audio drops, clipping, or amplitude cliffs at slot cuts

Combine with the job's assembly_plan (passed via --job-id) to label
every reported glitch with its source clip + native fps, so the user
sees both *where* it is and *why* it's there before any fix is shipped.

Outputs:
  - stdout summary table (per-slot rows)
  - --report path: full markdown report
  - --json path: structured findings for downstream tooling
  - --strip path: visual strip of suspect frames

Usage:

    python analyze_scene_glitches.py /path/to/output.mp4 \\
        --job-id <uuid> \\
        --report /tmp/glitch_report.md \\
        --json   /tmp/glitch_report.json \\
        --strip  /tmp/glitch_strips.png

If --job-id is omitted, the script still runs full-frame MAD,
region-MAD, stutter detection, and audio sweep — but findings are
reported by timestamp only (no slot/source-clip labels).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Thresholds tuned for 1080×1920 output at 30fps. Lower MAD = less motion.
FREEZE_MAD_THRESHOLD = 1.0          # below this between consecutive frames = frozen
LOW_MOTION_MAD_THRESHOLD = 2.0      # below this = suspicious, worth flagging
SLOT_BOUNDARY_SPIKE_MIN = 15.0      # full-frame MAD spike that proves a real cut
AUDIO_DROP_DB = 30.0                # drop > this many dB vs prior 500ms = glitch
AUDIO_CLIP_RMS = 0.95               # RMS this close to 1.0 = clipping

# Geometry: BRAZIL/Welcome region defaults (match seed_dimples_passport_brazil.py)
TITLE_Y_FRAC = 0.45                  # PERU_Y_FRAC
TITLE_Y_BAND_PX = 220                # half-height of band where title can sit
WELCOME_Y_FRAC = 0.4779
WELCOME_Y_BAND_PX = 50

FRAME_H = 1920
FRAME_W = 1080


# ---------------- slot mapping ----------------

@dataclass
class SlotInfo:
    position: int
    t_start_s: float
    t_end_s: float
    source_gcs: str
    source_fps: float | None = None
    source_codec: str | None = None
    source_probe_ok: bool = False


async def _load_slot_map(job_id: str) -> list[SlotInfo]:
    """Read the assembly_plan from the DB and build a slot map with
    cumulative absolute timestamps + each source clip's path."""
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import Job  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        if not job:
            raise SystemExit(f"job {job_id} not found")
        plan = job.assembly_plan or {}

    steps = plan.get("steps") or []
    slots: list[SlotInfo] = []
    t = 0.0
    for step in steps:
        slot = step.get("slot") or {}
        pos = slot.get("position")
        dur = float(slot.get("target_duration_s") or 0.0)
        src = step.get("clip_gcs_path") or ""
        slots.append(SlotInfo(
            position=pos,
            t_start_s=t,
            t_end_s=t + dur,
            source_gcs=src,
        ))
        t += dur
    return slots


def _probe_source_fps(slots: list[SlotInfo]) -> None:
    """Mutates: fills source_fps / source_codec / source_probe_ok on each
    slot by ffprobing the GCS source via a signed URL. 404s / network
    failures are surfaced but don't crash the run."""
    try:
        from app.config import settings  # noqa: PLC0415
        from app.storage import _get_client  # noqa: PLC0415
    except Exception as exc:
        print(f"warning: cannot import storage module ({exc}); "
              f"source-fps probe skipped", file=sys.stderr)
        return

    bucket = _get_client().bucket(settings.storage_bucket)
    for s in slots:
        if not s.source_gcs:
            continue
        try:
            blob = bucket.blob(s.source_gcs)
            url = blob.generate_signed_url(
                expiration=timedelta(minutes=5), version="v4", method="GET",
            )
            out = subprocess.check_output(
                ["ffprobe", "-v", "error",
                 "-show_entries", "stream=codec_name,r_frame_rate",
                 "-of", "csv=p=0",
                 "-select_streams", "v:0", url],
                text=True, timeout=20,
            ).strip()
            parts = out.split(",")
            codec = parts[0] if parts else "?"
            rfps = parts[1] if len(parts) > 1 else ""
            num, den = (int(x) for x in rfps.split("/"))
            fps = num / den if den else 0.0
            s.source_fps = fps
            s.source_codec = codec
            s.source_probe_ok = True
        except Exception:
            s.source_fps = None
            s.source_codec = None
            s.source_probe_ok = False


def _slot_at(slots: list[SlotInfo], t: float) -> SlotInfo | None:
    """O(N) is fine; slot count is ≤ 20 in any reasonable template."""
    for s in slots:
        if s.t_start_s <= t < s.t_end_s:
            return s
    if slots and t >= slots[-1].t_end_s:
        return slots[-1]
    return None


# ---------------- frame analysis ----------------

@dataclass
class FrameMAD:
    frame_idx: int
    t_s: float
    full: float
    bg_below_text: float
    text_band: float


def _extract_frames(video: Path, out_dir: Path,
                    scale_w: int = 540, scale_h: int = 960) -> int:
    """Native-rate frame extraction via `-vsync passthrough` so the decoder
    emits exactly the frames in the stream (no virtual-frame interpolation
    from `fps=N` filter)."""
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video),
        "-vsync", "passthrough",
        "-vf", f"scale={scale_w}:{scale_h}",
        "-y", str(out_dir / "f_%04d.png"),
    ], check=True, timeout=180)
    return len(list(out_dir.glob("f_*.png")))


def _region_y_bounds(scaled_h: int) -> dict[str, tuple[int, int]]:
    """Return (y_lo, y_hi) bounds for each region on the SCALED frame."""
    # Title region center is at frame_h * TITLE_Y_FRAC. Band half-height
    # scales with the same ratio.
    title_y_center = int(scaled_h * TITLE_Y_FRAC)
    title_band_half = int(scaled_h * (TITLE_Y_BAND_PX / FRAME_H))
    title_lo = max(0, title_y_center - title_band_half)
    title_hi = min(scaled_h, title_y_center + title_band_half)
    return {
        "full": (0, scaled_h),
        "text_band": (title_lo, title_hi),
        # bg_below_text starts ONE PIXEL after the title region ends —
        # so even the lowest descender from the title doesn't bleed.
        "bg_below_text": (min(scaled_h, title_hi + 1), scaled_h),
    }


def _region_mad_pair(prev: np.ndarray, curr: np.ndarray,
                     regions: dict[str, tuple[int, int]]) -> dict[str, float]:
    """Return MAD per region for one consecutive frame pair."""
    diff = np.abs(curr.astype(np.int16) - prev.astype(np.int16))
    out = {}
    for name, (lo, hi) in regions.items():
        out[name] = float(diff[lo:hi].mean()) if hi > lo else 0.0
    return out


def _scan_frames(frames_dir: Path, fps_out: float) -> list[FrameMAD]:
    paths = sorted(frames_dir.glob("f_*.png"))
    if not paths:
        return []
    first = np.array(Image.open(paths[0]).convert("RGB"))
    regions = _region_y_bounds(first.shape[0])
    obs: list[FrameMAD] = []
    prev = first
    for i, fp in enumerate(paths[1:], start=1):
        curr = np.array(Image.open(fp).convert("RGB"))
        mads = _region_mad_pair(prev, curr, regions)
        obs.append(FrameMAD(
            frame_idx=i,
            t_s=i / fps_out,
            full=mads["full"],
            bg_below_text=mads["bg_below_text"],
            text_band=mads["text_band"],
        ))
        prev = curr
    return obs


# ---------------- stutter pattern detection ----------------

@dataclass
class StutterCluster:
    t_start_s: float
    t_end_s: float
    period_s: float | None
    implied_source_fps: float | None
    count: int
    frame_indices: list[int]


def detect_stutter_clusters(obs: list[FrameMAD],
                            field_name: str = "bg_below_text",
                            mad_threshold: float = FREEZE_MAD_THRESHOLD,
                            ) -> list[StutterCluster]:
    """Find regular periodic freezes that indicate fps mismatch."""
    low_frames = [(o.frame_idx, o.t_s) for o in obs
                  if getattr(o, field_name) < mad_threshold]
    if len(low_frames) < 3:
        return []
    # Group into clusters where successive low-frames are evenly spaced.
    clusters: list[StutterCluster] = []
    i = 0
    while i < len(low_frames) - 2:
        # Try to grow a cluster starting at i. Look at the gap between
        # the next two low frames; if subsequent gaps match within 1
        # frame, that's a periodic stutter.
        gaps = []
        j = i
        idxs = [low_frames[i][0]]
        ts = [low_frames[i][1]]
        while j + 1 < len(low_frames):
            g = low_frames[j + 1][0] - low_frames[j][0]
            if gaps and abs(g - gaps[0]) > 1:
                break
            gaps.append(g)
            idxs.append(low_frames[j + 1][0])
            ts.append(low_frames[j + 1][1])
            j += 1
        if len(gaps) >= 2 and len(set(gaps)) <= 2:
            period_frames = median(gaps)
            # period in seconds = period_frames / fps_out (we don't have
            # fps_out here; convert via t deltas — average of t-gaps)
            t_gaps = [ts[k + 1] - ts[k] for k in range(len(ts) - 1)]
            period_s = median(t_gaps)
            # source_fps = output_fps * (period - 1) / period:
            # 30→24: period=5, ratio 4/5=0.8, 30*0.8=24 ✓
            # 30→25: period=6, ratio 5/6=0.833, 30*0.833=25 ✓
            implied_fps = 30.0 * (period_frames - 1) / period_frames
            clusters.append(StutterCluster(
                t_start_s=ts[0],
                t_end_s=ts[-1],
                period_s=period_s,
                implied_source_fps=implied_fps,
                count=len(ts),
                frame_indices=idxs,
            ))
        i = j + 1 if j > i else i + 1
    return clusters


# ---------------- audio sweep ----------------

@dataclass
class AudioGlitch:
    t_s: float
    kind: str   # "drop", "clip", "silence"
    detail: str


def detect_audio_glitches(video: Path) -> list[AudioGlitch]:
    """Sweep per-50ms windows for amplitude cliffs and clipping.

    Extracts raw PCM from the video and computes RMS per window — simpler
    and more reliable than parsing ffmpeg's `astats` filter output.
    """
    sr = 44100
    win = int(sr * 0.05)  # 50 ms
    raw = subprocess.check_output([
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video),
        "-ac", "1", "-ar", str(sr),
        "-f", "s16le", "pipe:1",
    ], timeout=60)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n_wins = len(samples) // win
    rms = np.array([
        np.sqrt(np.mean(samples[i * win:(i + 1) * win] ** 2))
        for i in range(n_wins)
    ])
    if rms.size < 11:
        return []

    glitches: list[AudioGlitch] = []
    # Clipping
    for i, r in enumerate(rms):
        if r > AUDIO_CLIP_RMS:
            glitches.append(AudioGlitch(
                t_s=i * 0.05, kind="clip",
                detail=f"RMS={r:.3f} (≥{AUDIO_CLIP_RMS})",
            ))
    # Amplitude drop: compare each window's dB vs the mean over the
    # prior 0.5s (10 windows).
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    for i in range(10, n_wins):
        prev_mean = db[i - 10:i].mean()
        if db[i] < prev_mean - AUDIO_DROP_DB:
            glitches.append(AudioGlitch(
                t_s=i * 0.05, kind="drop",
                detail=f"{db[i]:.1f}dB vs prior 500ms {prev_mean:.1f}dB",
            ))
    return glitches


# ---------------- slot-boundary check ----------------

@dataclass
class BoundaryIssue:
    slot_in: int | None
    slot_out: int | None
    t_s: float
    kind: str   # "no_spike" or "double_spike"
    detail: str


def detect_boundary_issues(obs: list[FrameMAD],
                           slots: list[SlotInfo]) -> list[BoundaryIssue]:
    if not slots:
        return []
    issues: list[BoundaryIssue] = []
    for i in range(len(slots) - 1):
        boundary_t = slots[i].t_end_s
        # Find the closest frame to the boundary
        candidates = [o for o in obs if abs(o.t_s - boundary_t) < 0.1]
        if not candidates:
            continue
        peak = max(candidates, key=lambda o: o.full)
        # Check spike magnitude
        if peak.full < SLOT_BOUNDARY_SPIKE_MIN and slots[i].position != slots[i + 1].position - 0:
            # Cut should be visible — no spike here = clips might be from
            # the same source or concat silently joined identical content.
            issues.append(BoundaryIssue(
                slot_in=slots[i].position,
                slot_out=slots[i + 1].position,
                t_s=boundary_t,
                kind="no_spike",
                detail=f"peak full-MAD={peak.full:.2f} (expected ≥{SLOT_BOUNDARY_SPIKE_MIN})",
            ))
        # Double spike: two consecutive frames both above the threshold —
        # suggests a ghost frame at the join.
        nearby = [o for o in obs if boundary_t - 0.05 <= o.t_s <= boundary_t + 0.05]
        spikes = [o for o in nearby if o.full > SLOT_BOUNDARY_SPIKE_MIN]
        if len(spikes) >= 2:
            issues.append(BoundaryIssue(
                slot_in=slots[i].position,
                slot_out=slots[i + 1].position,
                t_s=boundary_t,
                kind="double_spike",
                detail=f"{len(spikes)} consecutive spikes at the cut "
                       f"(MADs: {[f'{s.full:.1f}' for s in spikes]})",
            ))
    return issues


# ---------------- report writer ----------------

def _fmt_fps(fps: float | None) -> str:
    if fps is None:
        return "?"
    if abs(fps - 23.976) < 0.05:
        return "23.976 ★"
    if abs(fps - 29.97) < 0.05:
        return "29.97"
    if abs(fps - 30.0) < 0.05:
        return "30"
    if abs(fps - 25.0) < 0.05:
        return "25 ★"
    return f"{fps:.2f}"


def write_report(
    video_path: Path,
    slots: list[SlotInfo],
    obs: list[FrameMAD],
    stutters: list[StutterCluster],
    boundaries: list[BoundaryIssue],
    audio: list[AudioGlitch],
    report_path: Path | None,
    json_path: Path | None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Scene Glitch Report — {video_path.name}")
    lines.append("")
    lines.append(f"- frames analyzed: {len(obs)}")
    lines.append(f"- total stutter clusters: {len(stutters)}")
    lines.append(f"- slot-boundary issues: {len(boundaries)}")
    lines.append(f"- audio glitches: {len(audio)}")
    lines.append("")

    # Per-slot table
    lines.append("## Per-slot summary")
    lines.append("")
    lines.append(
        "| slot | t_range | source_fps | freezes (full) | freezes (bg) | stutter | verdict |"
    )
    lines.append(
        "|------|---------|------------|----------------|--------------|---------|---------|"
    )
    for s in slots:
        # Find observations inside this slot
        in_slot = [o for o in obs if s.t_start_s <= o.t_s < s.t_end_s]
        full_freezes = [o for o in in_slot if o.full < FREEZE_MAD_THRESHOLD]
        bg_freezes = [o for o in in_slot if o.bg_below_text < FREEZE_MAD_THRESHOLD]
        # Stutter clusters that overlap this slot
        slot_stutters = [c for c in stutters
                         if s.t_start_s <= c.t_start_s < s.t_end_s
                         or s.t_start_s <= c.t_end_s < s.t_end_s]
        # Verdict
        verdict_parts = []
        if s.source_probe_ok and s.source_fps and not (29.5 < s.source_fps < 30.5):
            verdict_parts.append("★ FPS MISMATCH")
        if full_freezes:
            verdict_parts.append(f"FREEZE ×{len(full_freezes)}")
        if bg_freezes and not full_freezes:
            verdict_parts.append(f"BG FREEZE ×{len(bg_freezes)} (masked by text)")
        if slot_stutters:
            verdict_parts.append(f"STUTTER ×{sum(c.count for c in slot_stutters)}")
        verdict = " / ".join(verdict_parts) if verdict_parts else "OK"
        stutter_brief = ", ".join(
            f"{c.count}@{c.period_s:.3f}s≈{c.implied_source_fps:.1f}fps"
            for c in slot_stutters
        ) or "—"
        lines.append(
            f"| {s.position} | {s.t_start_s:.2f}-{s.t_end_s:.2f} | "
            f"{_fmt_fps(s.source_fps)} | {len(full_freezes)} | "
            f"{len(bg_freezes)} | {stutter_brief} | {verdict} |"
        )
    lines.append("")

    # Stutter cluster details
    if stutters:
        lines.append("## Stutter clusters (regular periodic freezes)")
        lines.append("")
        for i, c in enumerate(stutters, 1):
            slot_in = _slot_at(slots, c.t_start_s)
            slot_label = f"slot {slot_in.position}" if slot_in else "—"
            lines.append(
                f"{i}. {slot_label}: t={c.t_start_s:.3f}-{c.t_end_s:.3f}s, "
                f"{c.count} freezes at period {c.period_s:.3f}s. "
                f"Implied source fps: **{c.implied_source_fps:.2f}**. "
                f"Frame indices: {c.frame_indices[:10]}"
                f"{'…' if len(c.frame_indices) > 10 else ''}"
            )
        lines.append("")

    # Boundary issues
    if boundaries:
        lines.append("## Slot-boundary issues")
        lines.append("")
        for b in boundaries:
            lines.append(
                f"- slot {b.slot_in} → slot {b.slot_out} at t={b.t_s:.3f}s: "
                f"**{b.kind}** — {b.detail}"
            )
        lines.append("")

    # Audio glitches
    if audio:
        lines.append("## Audio glitches")
        lines.append("")
        for a in audio[:30]:
            slot = _slot_at(slots, a.t_s)
            label = f"slot {slot.position}" if slot else "—"
            lines.append(
                f"- t={a.t_s:.3f}s ({label}): **{a.kind}** — {a.detail}"
            )
        if len(audio) > 30:
            lines.append(f"- … {len(audio) - 30} more")
        lines.append("")

    text = "\n".join(lines)

    if report_path:
        report_path.write_text(text)
        print(f"report written: {report_path}", file=sys.stderr)

    if json_path:
        payload = {
            "video": str(video_path),
            "n_frames": len(obs),
            "slots": [
                {
                    "position": s.position,
                    "t_start_s": s.t_start_s,
                    "t_end_s": s.t_end_s,
                    "source_gcs": s.source_gcs,
                    "source_fps": s.source_fps,
                    "source_codec": s.source_codec,
                }
                for s in slots
            ],
            "stutters": [
                {
                    "t_start_s": c.t_start_s,
                    "t_end_s": c.t_end_s,
                    "period_s": c.period_s,
                    "implied_source_fps": c.implied_source_fps,
                    "count": c.count,
                }
                for c in stutters
            ],
            "boundaries": [
                {
                    "slot_in": b.slot_in, "slot_out": b.slot_out,
                    "t_s": b.t_s, "kind": b.kind, "detail": b.detail,
                }
                for b in boundaries
            ],
            "audio_glitches": [
                {"t_s": a.t_s, "kind": a.kind, "detail": a.detail}
                for a in audio
            ],
        }
        json_path.write_text(json.dumps(payload, indent=2))
        print(f"json written: {json_path}", file=sys.stderr)

    return text


# ---------------- visual strip ----------------

def write_strip(frames_dir: Path, obs: list[FrameMAD],
                stutters: list[StutterCluster],
                out_path: Path) -> None:
    """Render a strip showing the lowest-MAD suspect frames."""
    # Top 12 lowest-MAD on bg_below_text — those are the strongest stutter
    # candidates. Pad with a cell explaining the bug-class.
    candidates = sorted(obs, key=lambda o: o.bg_below_text)[:12]
    if not candidates:
        return
    paths = sorted(frames_dir.glob("f_*.png"))
    cell_w, cell_h = 200, 360
    img = Image.new("RGB", (cell_w * len(candidates), cell_h + 40),
                    (24, 24, 24))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
    except Exception:
        font = ImageFont.load_default()
    for i, fobs in enumerate(candidates):
        if fobs.frame_idx < len(paths):
            cell = Image.open(paths[fobs.frame_idx]).convert("RGB")
            cell.thumbnail((cell_w - 4, cell_h - 4))
            img.paste(cell, (i * cell_w + 2, 2))
        d.text((i * cell_w + 4, cell_h + 4),
               f"t={fobs.t_s:.3f}s", fill=(220, 220, 220), font=font)
        d.text((i * cell_w + 4, cell_h + 20),
               f"bg={fobs.bg_below_text:.2f}", fill=(255, 180, 80), font=font)
    img.save(out_path)
    print(f"strip written: {out_path}", file=sys.stderr)


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="rendered MP4 to analyze")
    ap.add_argument("--job-id", help="template job UUID (enables slot/source labels)")
    ap.add_argument("--report", type=Path, help="markdown report output")
    ap.add_argument("--json", type=Path, help="structured JSON output")
    ap.add_argument("--strip", type=Path, help="suspect-frame strip PNG output")
    ap.add_argument("--fps-out", type=float, default=30.0,
                    help="output container fps (default 30)")
    args = ap.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        sys.exit(f"video not found: {video_path}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe required on PATH")

    # Slot map (optional)
    slots: list[SlotInfo] = []
    if args.job_id:
        print(f"loading slot map for job {args.job_id} …", file=sys.stderr)
        slots = asyncio.run(_load_slot_map(args.job_id))
        print(f"  {len(slots)} slots loaded", file=sys.stderr)
        print(f"probing source fps for {len(slots)} clips …", file=sys.stderr)
        _probe_source_fps(slots)

    # Extract frames
    with tempfile.TemporaryDirectory(prefix="scene_glitch_") as td:
        td_path = Path(td)
        print("extracting frames …", file=sys.stderr)
        n = _extract_frames(video_path, td_path)
        print(f"  {n} frames extracted", file=sys.stderr)

        # Per-frame MAD
        print("computing per-frame MAD (full + region-masked) …", file=sys.stderr)
        obs = _scan_frames(td_path, args.fps_out)
        print(f"  {len(obs)} consecutive-frame deltas", file=sys.stderr)

        # Stutter clusters on bg_below_text (the clean signal)
        bg_stutters = detect_stutter_clusters(obs, field_name="bg_below_text")
        # Also on full frame, for slots without text overlays
        full_stutters = detect_stutter_clusters(obs, field_name="full")
        # Merge by t_start
        all_stutters = sorted(set(
            (c.t_start_s, c.t_end_s, c.period_s, c.implied_source_fps,
             c.count, tuple(c.frame_indices))
            for c in (bg_stutters + full_stutters)
        ))
        stutters: list[StutterCluster] = [
            StutterCluster(t_start_s=t0, t_end_s=t1, period_s=p,
                           implied_source_fps=fps, count=n,
                           frame_indices=list(idxs))
            for (t0, t1, p, fps, n, idxs) in all_stutters
        ]
        print(f"  {len(stutters)} stutter cluster(s) found", file=sys.stderr)

        # Slot boundary issues
        boundaries = detect_boundary_issues(obs, slots) if slots else []
        print(f"  {len(boundaries)} slot-boundary issue(s)", file=sys.stderr)

        # Audio glitches
        print("sweeping audio for glitches …", file=sys.stderr)
        audio = detect_audio_glitches(video_path)
        print(f"  {len(audio)} audio glitch(es) detected", file=sys.stderr)

        # Write outputs
        text = write_report(
            video_path, slots, obs, stutters, boundaries, audio,
            args.report, args.json,
        )
        print()
        print(text)

        if args.strip:
            write_strip(td_path, obs, stutters, args.strip)

    return 0


if __name__ == "__main__":
    sys.exit(main())
