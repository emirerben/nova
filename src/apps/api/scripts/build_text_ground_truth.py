"""Build OCR-derived ground truth for template-text evals.

Two modes:

1. Single-video (legacy, interactive)::

    python scripts/build_text_ground_truth.py \\
        --video /path/to/template.mp4 \\
        --slot-boundaries 0.0:3.0,3.0:7.5,7.5:12.0 \\
        --out tests/fixtures/agent_evals/template_text/ground_truth/<slug>.json

2. Autobuilder (Lane B / T2): cross-verified ground truth for every
   published template::

    python scripts/build_text_ground_truth.py --all-published [--limit N] [--dry-run]

   The autobuilder:
     - queries ``video_templates`` for every ``published_at IS NOT NULL
       AND archived_at IS NULL`` row,
     - downloads each template video from GCS,
     - samples frames at ``--frame-interval`` seconds,
     - runs two independent OCR engines (pytesseract + Cloud Vision),
     - keeps only the frames where the engines agree above a Levenshtein
       similarity threshold (default 0.85 — see
       :mod:`app.services.ocr.cross_check`),
     - writes the agreed-on tokens into ``ground_truth/<slug>.json``
       in the same shape the eval already consumes,
     - dumps the rest to ``disagreements/<slug>.json`` for human review,
     - skips templates whose fixture is already up-to-date for the
       same GCS blob generation (idempotent re-runs).

   Whisper transcript: optional, runs the project's
   ``nova.audio.transcript`` agent against the template's Gemini
   file_uri when ``GEMINI_API_KEY`` is set. Best-effort — fixtures
   still write without transcript words when the agent is unavailable.

Pipeline (single-video mode):
    1. Sample frames every N seconds (default 0.25s — fine enough to catch
       brief overlays without exploding OCR cost)
    2. pytesseract OCR each frame → (text, bbox) detections
    3. Group consecutive frames with the same normalized text + overlapping
       bbox into intervals → one overlay per group
    4. Filter by min visibility duration (default 0.25s — single-frame
       glitches are noise)
    5. Write JSON in the shape the eval expects:
         {"overlays": [{slot_index, sample_text, start_s, end_s, bbox,
                       font_color_hex, effect, role, size_class}, ...]}
    6. Operator review: opens each overlay's sample frame for visual
       confirmation; prompts for effect (which OCR can't infer) and
       role label.

Outputs are hand-validated artifacts, not raw OCR — commit them to git.

Why tesseract: already a project dep (pyproject.toml), no auth required, no
network round-trip per frame, runs on dev machines without setup. For
production-grade OCR (Google Cloud Vision, AWS Textract, PaddleOCR), swap
``_run_tesseract`` for the alternative implementation — or use
``--all-published`` which cross-checks both engines automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_FRAME_INTERVAL_S = 0.25
DEFAULT_MIN_VISIBILITY_S = 0.25
# IoU threshold for considering two frame-level detections "the same overlay
# continuing." Generous because the same text can shift a few pixels frame to
# frame due to encoder noise / re-render of the overlay.
DEFAULT_GROUPING_IOU = 0.30
# Canonical TikTok aspect ratio — frames may be other ratios but we
# normalize bbox coords to this for consistency with the eval.
CANVAS_W = 1080
CANVAS_H = 1920


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class FrameDetection:
    """One OCR detection on one frame."""

    frame_t: float
    text: str
    x_norm: float
    y_norm: float
    w_norm: float
    h_norm: float
    font_color_hex: str = "#FFFFFF"


@dataclass
class GroundTruthOverlay:
    slot_index: int
    sample_text: str
    start_s: float
    end_s: float
    bbox: dict = field(default_factory=dict)
    font_color_hex: str = "#FFFFFF"
    effect: str = "none"
    role: str = "label"
    size_class: str = "medium"


# ── Frame sampling ───────────────────────────────────────────────────────────


def _probe_duration(video: str) -> float:
    """Get the video's duration in seconds via ffprobe."""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video,
        ],
        text=True,
    )
    return float(out.strip())


def _sample_frames(video: str, interval_s: float, outdir: str) -> list[tuple[float, str]]:
    """Sample one PNG every `interval_s` seconds into `outdir`.

    Returns list of (timestamp_s, png_path). Uses ffmpeg `fps` filter with
    `select` to pick frames at exact times.

    Frames are sampled at the source's native dimensions — NOT letterboxed to
    1080x1920. Reason: the bbox coords this script writes are normalized
    fractions of the frame, and the template-text agent sees the ORIGINAL
    (un-padded) video via the Gemini File API. Padding here would produce
    coords relative to a padded frame, shifting them away from what the agent
    reports. The OCR coordinate fractions are written relative to whatever
    frame shape ffmpeg emits, so the eval's bbox IoU is measured in the same
    coordinate space the agent uses.
    """
    duration = _probe_duration(video)
    n_frames = max(1, int(duration / interval_s))
    fps = 1.0 / interval_s
    out_pattern = os.path.join(outdir, "frame_%05d.png")
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            video,
            "-vf",
            f"fps={fps}",
            "-vsync",
            "vfr",
            out_pattern,
        ]
    )
    frames: list[tuple[float, str]] = []
    for i in range(1, n_frames + 1):
        path = os.path.join(outdir, f"frame_{i:05d}.png")
        if not os.path.exists(path):
            break
        t = (i - 1) * interval_s
        frames.append((t, path))
    return frames


# ── OCR ──────────────────────────────────────────────────────────────────────


def _run_tesseract(frame_path: str) -> list[FrameDetection]:
    """OCR one frame, return per-text-block detections in normalized coords.

    Uses pytesseract's image_to_data output for per-block bboxes + confidence.
    Low-confidence (< 30) blocks are filtered as junk.
    """
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(
            f"pytesseract/Pillow not installed: {exc}. Install with: pip install pytesseract Pillow"
        ) from exc

    img = Image.open(frame_path)
    w, h = img.size
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    detections: list[FrameDetection] = []
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = int(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < 30:
            continue
        # image_to_data returns top-left + width/height in pixels. Convert to
        # center-normalized coords.
        x, y, ww, hh = (
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
        )
        cx_norm = (x + ww / 2.0) / w
        cy_norm = (y + hh / 2.0) / h
        w_norm = ww / w
        h_norm = hh / h
        font_color = _sample_text_color(img, (x, y, x + ww, y + hh))
        detections.append(
            FrameDetection(
                frame_t=0.0,  # filled in by caller
                text=text,
                x_norm=cx_norm,
                y_norm=cy_norm,
                w_norm=w_norm,
                h_norm=h_norm,
                font_color_hex=font_color,
            )
        )
    return detections


def _sample_text_color(img, box: tuple[int, int, int, int]) -> str:
    """Sample the dominant text-pixel color inside `box`.

    Detects polarity (dark text on bright background vs bright text on dark
    background) by checking mean brightness, then averages the FAR side of the
    brightness distribution. The previous version unconditionally averaged
    brighter-than-median pixels — which sampled the white background instead
    of the black text for typical lower-third / caption colors. That bug made
    every dark-text overlay get tagged as light in ground truth, then the
    eval would penalize the agent for correctly identifying the text color.

    Picks the FAR quartile (top 25% or bottom 25%) instead of "above/below
    median" because text typically occupies ~20-40% of a tight bbox; the
    median is dominated by background pixels.
    """
    try:
        cropped = img.crop(box).convert("RGB")
        pixels = list(cropped.getdata())
        if not pixels:
            return "#FFFFFF"
        brightnesses = [sum(p) / 3 for p in pixels]
        mean_brightness = sum(brightnesses) / len(brightnesses)
        # Mean > 128 => mostly-bright bbox => text is the DARK minority.
        # Sample the darkest quartile and call that the text color.
        # Mean <= 128 => mostly-dark bbox => text is the BRIGHT minority.
        pairs = sorted(zip(brightnesses, pixels, strict=True), key=lambda x: x[0])
        q = max(1, len(pairs) // 4)
        text_pixels = (
            [p for _, p in pairs[:q]] if mean_brightness > 128 else [p for _, p in pairs[-q:]]
        )
        r = sum(p[0] for p in text_pixels) // len(text_pixels)
        g = sum(p[1] for p in text_pixels) // len(text_pixels)
        b = sum(p[2] for p in text_pixels) // len(text_pixels)
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return "#FFFFFF"


# ── Grouping ─────────────────────────────────────────────────────────────────


def _bbox_iou(a: FrameDetection, b: FrameDetection) -> float:
    a_x1, a_x2 = a.x_norm - a.w_norm / 2, a.x_norm + a.w_norm / 2
    a_y1, a_y2 = a.y_norm - a.h_norm / 2, a.y_norm + a.h_norm / 2
    b_x1, b_x2 = b.x_norm - b.w_norm / 2, b.x_norm + b.w_norm / 2
    b_y1, b_y2 = b.y_norm - b.h_norm / 2, b.y_norm + b.h_norm / 2
    iw = max(0.0, min(a_x2, b_x2) - max(a_x1, b_x1))
    ih = max(0.0, min(a_y2, b_y2) - max(a_y1, b_y1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = (a_x2 - a_x1) * (a_y2 - a_y1)
    b_area = (b_x2 - b_x1) * (b_y2 - b_y1)
    return inter / (a_area + b_area - inter)


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def _slot_index_for(t: float, boundaries: list[tuple[float, float]]) -> int:
    """1-indexed slot containing global time t. Falls back to 1 / last slot
    when t is outside any window."""
    for i, (s, e) in enumerate(boundaries, start=1):
        if s <= t < e:
            return i
    return len(boundaries) if boundaries else 1


def _group_detections(
    per_frame: list[list[FrameDetection]],
    *,
    iou_threshold: float = DEFAULT_GROUPING_IOU,
) -> list[list[FrameDetection]]:
    """Group consecutive-frame detections that look like one continuing overlay.

    A detection joins an existing track when it has the same normalized text
    AND its bbox overlaps the last frame's bbox in that track by IoU ≥ threshold.
    Otherwise it starts a new track. Tracks not extended this frame stay
    "open" for one frame of slack before closing — handles brief OCR misses
    mid-overlay.
    """
    open_tracks: list[list[FrameDetection]] = []
    closed_tracks: list[list[FrameDetection]] = []
    SLACK_FRAMES = 1
    track_misses: list[int] = []

    for frame in per_frame:
        unmatched = list(range(len(open_tracks)))
        for det in frame:
            best_i, best_iou = -1, 0.0
            for i in unmatched:
                last = open_tracks[i][-1]
                if _norm(last.text) != _norm(det.text):
                    continue
                iou = _bbox_iou(last, det)
                if iou > best_iou and iou >= iou_threshold:
                    best_i, best_iou = i, iou
            if best_i >= 0:
                open_tracks[best_i].append(det)
                track_misses[best_i] = 0
                unmatched.remove(best_i)
            else:
                open_tracks.append([det])
                track_misses.append(0)
        # Increment miss count for tracks that didn't extend this frame.
        for i in unmatched:
            track_misses[i] += 1
        # Close tracks that have been quiet for > SLACK_FRAMES frames.
        new_open, new_misses = [], []
        for track, misses in zip(open_tracks, track_misses, strict=False):
            if misses > SLACK_FRAMES:
                closed_tracks.append(track)
            else:
                new_open.append(track)
                new_misses.append(misses)
        open_tracks = new_open
        track_misses = new_misses

    closed_tracks.extend(open_tracks)
    return closed_tracks


def _track_to_overlay(
    track: list[FrameDetection],
    boundaries: list[tuple[float, float]],
) -> GroundTruthOverlay:
    """Collapse a track into one GroundTruthOverlay."""
    times = [d.frame_t for d in track]
    start_s = min(times)
    end_s = max(times)
    # Take the median-frame detection as the canonical bbox (least likely to
    # be a mid-animation artifact).
    mid = track[len(track) // 2]
    sample_frame_t = mid.frame_t
    h_norm = mid.h_norm
    if h_norm < 0.04:
        size_class = "small"
    elif h_norm < 0.10:
        size_class = "medium"
    elif h_norm < 0.18:
        size_class = "large"
    else:
        size_class = "jumbo"
    return GroundTruthOverlay(
        slot_index=_slot_index_for(start_s, boundaries),
        sample_text=mid.text,
        start_s=round(start_s, 3),
        end_s=round(end_s + DEFAULT_FRAME_INTERVAL_S, 3),  # extend by sample interval
        bbox={
            "x_norm": round(mid.x_norm, 4),
            "y_norm": round(mid.y_norm, 4),
            "w_norm": round(mid.w_norm, 4),
            "h_norm": round(mid.h_norm, 4),
            "sample_frame_t": round(sample_frame_t, 3),
        },
        font_color_hex=mid.font_color_hex,
        effect="none",  # operator override
        role="label",  # operator override
        size_class=size_class,
    )


# ── Operator review ──────────────────────────────────────────────────────────


def _interactive_review(overlays: list[GroundTruthOverlay]) -> list[GroundTruthOverlay]:
    """Prompt the operator to confirm effect/role per overlay.

    Skip with empty input. Press 's' to skip the rest of the prompts and
    accept defaults. Press 'd' to delete the overlay from ground truth (it
    was an OCR false positive).
    """
    print(f"\nFound {len(overlays)} candidate overlays. Review interactively:")
    skip_all = False
    kept: list[GroundTruthOverlay] = []
    for i, ov in enumerate(overlays, start=1):
        print(
            f"\n[{i}/{len(overlays)}] slot={ov.slot_index} "
            f"text={ov.sample_text!r} {ov.start_s:.2f}-{ov.end_s:.2f}s "
            f"@ ({ov.bbox['x_norm']:.2f},{ov.bbox['y_norm']:.2f}) "
            f"size={ov.size_class} color={ov.font_color_hex}"
        )
        if skip_all:
            kept.append(ov)
            continue
        action = input(
            "  [Enter]=accept | d=drop (OCR false pos) | s=accept rest "
            "| role <hook|reaction|cta|label> | effect <pop-in|fade-in|...> > "
        ).strip()
        if not action:
            kept.append(ov)
            continue
        if action == "s":
            kept.append(ov)
            skip_all = True
            continue
        if action == "d":
            print("  dropped.")
            continue
        # Allow chained tokens: "role hook effect pop-in"
        toks = action.split()
        ii = 0
        while ii < len(toks):
            key = toks[ii]
            val = toks[ii + 1] if ii + 1 < len(toks) else None
            if key == "role" and val:
                ov.role = val
                ii += 2
            elif key == "effect" and val:
                ov.effect = val
                ii += 2
            else:
                print(f"  ignored: {key}")
                ii += 1
        kept.append(ov)
    return kept


# ── Autobuilder (Lane B / T2) ────────────────────────────────────────────────


def _slugify(name: str, template_id: str) -> str:
    """Produce a stable, filename-safe slug for a template.

    Falls back to the template UUID's first segment when the name has no
    alphanumeric content (e.g. emoji-only template names exist in prod).
    Keeping the UUID prefix as suffix-of-last-resort means two templates
    with identical names don't collide in the fixtures directory.
    """
    import re  # noqa: PLC0415 — only used in this helper

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
    if not cleaned:
        return template_id.split("-", 1)[0]
    # Always suffix the short id so renames don't orphan a fixture and so
    # name-collisions can't silently overwrite each other.
    short = template_id.split("-", 1)[0]
    return f"{cleaned}_{short}"


def _fetch_blob_generation(gcs_path: str) -> str | None:
    """Return the GCS object generation number for ``gcs_path``.

    Used to skip re-OCRing templates whose video hasn't changed. Returns
    None on any failure — the caller treats that as "unknown, do the
    work" so a transient GCS error never silently leaves stale fixtures.
    """
    try:
        from app.config import settings  # noqa: PLC0415
        from app.storage import _get_client  # type: ignore[attr-defined]  # noqa: PLC0415

        bucket = _get_client().bucket(settings.storage_bucket)
        blob = bucket.blob(gcs_path)
        blob.reload()
        return str(blob.generation) if blob.generation else None
    except Exception as exc:  # pragma: no cover — env-dependent
        print(f"  [warn] could not read GCS generation for {gcs_path!r}: {exc}")
        return None


def _download_template_video(gcs_path: str, dest: str) -> None:
    """Download a GCS object to ``dest`` using the project's storage helper."""
    from app.storage import download_to_file  # noqa: PLC0415

    download_to_file(gcs_path, dest)


def _fetch_published_templates() -> list[dict]:
    """Read all published, non-archived templates straight from the DB.

    Returns a list of plain dicts (id, name, gcs_path) so the autobuilder
    doesn't depend on the SQLAlchemy session lifecycle for its main loop.
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    # The async DATABASE_URL ships with the asyncpg driver; for this
    # one-shot script we want sync. Strip the driver suffix so the
    # default psycopg2 driver kicks in.
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url.removeprefix("postgresql+asyncpg://")
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name, gcs_path
                FROM video_templates
                WHERE published_at IS NOT NULL
                  AND archived_at IS NULL
                  AND gcs_path IS NOT NULL
                ORDER BY created_at DESC
                """
            )
        ).fetchall()
    return [{"id": r[0], "name": r[1], "gcs_path": r[2]} for r in rows]


def _build_engines() -> tuple[object, object]:
    """Instantiate (pytesseract, cloud_vision) — fail fast with a clear
    error message if either runtime prereq is missing. Returning a tuple
    instead of a dict makes the cross-check call site one line."""
    from app.services.ocr.engines import CloudVisionEngine, PytesseractEngine  # noqa: PLC0415

    return PytesseractEngine(), CloudVisionEngine()


def _fetch_transcript_for_template(
    template: dict,
) -> list[dict] | None:
    """Best-effort: run the project's nova.audio.transcript agent against
    the template's Gemini file_uri. Returns a list of {text, start_s,
    end_s, confidence} dicts, or None if the agent can't run (no key,
    SDK missing, etc.). Failures are non-fatal — the fixture still
    writes without transcript data.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    try:
        # Imports deferred so the script's --help works even when the
        # agent runtime isn't fully wired (e.g. missing optional deps
        # in a fresh dev checkout).
        import asyncio  # noqa: PLC0415

        from app.agents.transcript import TranscriptAgent, TranscriptInput  # noqa: PLC0415

        gcs_path = template["gcs_path"]
        if not gcs_path:
            return None
        agent_input = TranscriptInput(file_uri=gcs_path, file_mime="video/mp4")
        agent = TranscriptAgent()
        result = asyncio.run(agent.run(agent_input))
        return [
            {
                "text": w.text,
                "start_s": w.start_s,
                "end_s": w.end_s,
                "confidence": w.confidence,
            }
            for w in result.words
        ]
    except Exception as exc:  # pragma: no cover — best-effort
        print(f"  [warn] transcript agent failed for {template['id']}: {exc}")
        return None


def _autobuild_one_template(
    template: dict,
    *,
    frame_interval_s: float,
    threshold: float,
    ground_truth_dir: Path,
    disagreements_dir: Path,
    dry_run: bool,
) -> str:
    """Cross-OCR one template; write to fixture or disagreements.

    Returns a one-word status: 'skipped', 'agreed', 'disagreed', 'failed'.
    The autobuilder loop aggregates these for the final summary.
    """
    from app.services.ocr.cross_check import _cross_check_from_words  # noqa: PLC0415

    slug = _slugify(template["name"] or "", template["id"])
    fixture_path = ground_truth_dir / f"{slug}.json"
    disagreement_path = disagreements_dir / f"{slug}.json"

    # Cache key: GCS object generation. When a template video is replaced
    # at the same path, GCS bumps the generation; we re-OCR. When it
    # hasn't changed, we skip.
    generation = _fetch_blob_generation(template["gcs_path"])
    if fixture_path.exists():
        try:
            existing = json.loads(fixture_path.read_text())
        except json.JSONDecodeError:
            existing = {}
        prior_gen = (existing.get("_meta") or {}).get("gcs_generation")
        if generation and prior_gen == generation:
            print(f"  skip {slug}: same GCS generation ({generation})")
            return "skipped"

    if dry_run:
        print(f"  [dry-run] would OCR {slug} (gcs_path={template['gcs_path']})")
        return "skipped"

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "template.mp4")
        try:
            _download_template_video(template["gcs_path"], video_path)
        except Exception as exc:
            print(f"  [error] download failed for {slug}: {exc}")
            return "failed"

        try:
            engine_a, engine_b = _build_engines()
        except Exception as exc:
            print(f"  [error] engine init failed: {exc}")
            return "failed"

        frames = _sample_frames(video_path, frame_interval_s, tmpdir)
        print(f"  {slug}: sampled {len(frames)} frames")

        per_frame_results: list[dict] = []
        agreed_frames = 0
        for t, png in frames:
            try:
                a_words = engine_a.recognize(png)
                b_words = engine_b.recognize(png)
            except Exception as exc:
                print(f"    [warn] OCR failure on t={t:.2f}: {exc}")
                continue
            result = _cross_check_from_words(
                engine_a.name, a_words, engine_b.name, b_words, threshold=threshold
            )
            per_frame_results.append(
                {
                    "frame_t": round(t, 3),
                    "status": result.status,
                    "agreement": round(result.agreement, 4),
                    "tokens": result.tokens,
                    "engine_a": {
                        "name": result.engine_a_name,
                        "tokens": result.engine_a_tokens,
                    },
                    "engine_b": {
                        "name": result.engine_b_name,
                        "tokens": result.engine_b_tokens,
                    },
                }
            )
            if result.status == "agreed":
                agreed_frames += 1

        if not per_frame_results:
            print(f"  [warn] no frames produced any OCR for {slug}; skipping")
            return "failed"

        agreement_ratio = agreed_frames / len(per_frame_results)
        print(
            f"  {slug}: {agreed_frames}/{len(per_frame_results)} frames agreed "
            f"({agreement_ratio:.0%})"
        )

    transcript_words = _fetch_transcript_for_template(template)

    meta = {
        "template_id": template["id"],
        "template_name": template["name"],
        "gcs_path": template["gcs_path"],
        "gcs_generation": generation,
        "frame_interval_s": frame_interval_s,
        "agreement_threshold": threshold,
        "agreed_frames": agreed_frames,
        "total_frames": len(per_frame_results),
        "agreement_ratio": round(agreement_ratio, 4),
    }

    if agreement_ratio >= threshold:
        # Convert agreed-frame tokens into an overlay-shaped ground truth.
        # The agreed-tokens-per-frame are token-set granular; downstream
        # human review (operator opens the fixture) can refine into
        # per-overlay records with the same shape the eval expects.
        # We emit one "overlay" per agreed frame as a candidate so the
        # eval has something concrete to score against immediately —
        # the operator collapses adjacent frames into one overlay if
        # needed.
        overlays: list[dict] = []
        for entry in per_frame_results:
            if entry["status"] != "agreed" or not entry["tokens"]:
                continue
            overlays.append(
                {
                    "slot_index": 1,  # operator override; no per-slot
                    # boundaries in autobuilder mode
                    "sample_text": " ".join(entry["tokens"]),
                    "start_s": entry["frame_t"],
                    "end_s": round(entry["frame_t"] + frame_interval_s, 3),
                    "bbox": {
                        "x_norm": 0.5,
                        "y_norm": 0.5,
                        "w_norm": 0.0,
                        "h_norm": 0.0,
                        "sample_frame_t": entry["frame_t"],
                    },
                    "font_color_hex": "#FFFFFF",
                    "effect": "none",
                    "role": "label",
                    "size_class": "medium",
                }
            )
        payload: dict = {"_meta": meta, "overlays": overlays}
        if transcript_words is not None:
            payload["transcript"] = {"words": transcript_words}
        ground_truth_dir.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(json.dumps(payload, indent=2))
        print(f"  wrote {fixture_path}")
        return "agreed"

    # Below threshold — dump both engines' raw output for human review.
    payload = {
        "_meta": meta,
        "frames": per_frame_results,
    }
    if transcript_words is not None:
        payload["transcript"] = {"words": transcript_words}
    disagreements_dir.mkdir(parents=True, exist_ok=True)
    disagreement_path.write_text(json.dumps(payload, indent=2))
    print(f"  disagreement → {disagreement_path}")
    return "disagreed"


def _run_autobuilder(args: argparse.Namespace) -> int:
    """Top-level driver for ``--all-published`` mode."""
    if not 0.0 <= args.agreement_threshold <= 1.0:
        print(
            f"--agreement-threshold must be in [0, 1]; got {args.agreement_threshold}",
            file=sys.stderr,
        )
        return 2

    print("Autobuilder: querying published templates …")
    try:
        templates = _fetch_published_templates()
    except Exception as exc:
        print(f"DB query failed: {exc}", file=sys.stderr)
        return 1
    print(f"  → {len(templates)} candidate templates")
    if args.limit is not None:
        templates = templates[: args.limit]
        print(f"  --limit {args.limit}: capping to {len(templates)} templates")

    gt_dir = Path(args.ground_truth_dir)
    dis_dir = Path(args.disagreements_dir)
    summary: dict[str, int] = {
        "agreed": 0,
        "disagreed": 0,
        "skipped": 0,
        "failed": 0,
    }
    for i, t in enumerate(templates, start=1):
        print(f"\n[{i}/{len(templates)}] {t['id']} — {t['name']!r}")
        try:
            status = _autobuild_one_template(
                t,
                frame_interval_s=args.frame_interval,
                threshold=args.agreement_threshold,
                ground_truth_dir=gt_dir,
                disagreements_dir=dis_dir,
                dry_run=args.dry_run,
            )
        except KeyboardInterrupt:
            print("interrupted by operator", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            print(f"  [error] {exc}")
            status = "failed"
        summary[status] = summary.get(status, 0) + 1

    print("\n── Autobuilder summary ──")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────


def _parse_boundaries(spec: str) -> list[tuple[float, float]]:
    """Parse '0.0:3.0,3.0:7.5,7.5:12.0' → [(0.0,3.0),(3.0,7.5),(7.5,12.0)].

    Raises SystemExit with a clear message on malformed input so the operator
    doesn't get a bare ValueError stack trace when they fat-finger the spec.
    """
    out: list[tuple[float, float]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise SystemExit(
                f"--slot-boundaries chunk {chunk!r} missing ':' separator. "
                "Expected format: 'start:end,start:end,...'"
            )
        parts = chunk.split(":")
        if len(parts) != 2:
            raise SystemExit(
                f"--slot-boundaries chunk {chunk!r} has {len(parts)} colon-separated "
                "fields, expected exactly 2 (start:end)."
            )
        try:
            s, e = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise SystemExit(f"--slot-boundaries chunk {chunk!r}: {exc}") from exc
        if s < 0 or e <= s:
            raise SystemExit(
                f"--slot-boundaries chunk {chunk!r}: start must be >= 0 and end > start "
                f"(got start={s}, end={e})."
            )
        out.append((s, e))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--video",
        help="Local path to the template video (single-video mode).",
    )
    p.add_argument(
        "--slot-boundaries",
        help=("Comma-separated 'start:end' pairs for single-video mode, e.g. '0:3,3:7.5,7.5:12'"),
    )
    p.add_argument("--out", help="Output JSON path (single-video mode).")
    p.add_argument(
        "--frame-interval",
        type=float,
        default=DEFAULT_FRAME_INTERVAL_S,
        help=f"Seconds between sampled frames (default {DEFAULT_FRAME_INTERVAL_S})",
    )
    p.add_argument(
        "--min-visibility",
        type=float,
        default=DEFAULT_MIN_VISIBILITY_S,
        help=f"Drop overlays shorter than this (default {DEFAULT_MIN_VISIBILITY_S}s)",
    )
    p.add_argument(
        "--no-review",
        action="store_true",
        help="Skip interactive operator review (use OCR output as-is — for batch mode).",
    )
    # ── Autobuilder (Lane B / T2) flags ──
    p.add_argument(
        "--all-published",
        action="store_true",
        help=(
            "Run the cross-engine autobuilder over every published, non-archived "
            "video_templates row. Writes agreed fixtures to "
            "tests/fixtures/agent_evals/template_text/ground_truth/<slug>.json "
            "and disagreements to disagreements/<slug>.json."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Autobuilder: stop after N templates (testing aid).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Autobuilder: print what would be written without touching disk "
            "or running OCR. Useful for sanity-checking the candidate set."
        ),
    )
    p.add_argument(
        "--agreement-threshold",
        type=float,
        default=0.85,
        help=(
            "Autobuilder: minimum Levenshtein-similarity ratio (0..1) between "
            "the two OCR engines for a frame to count as agreed. Default 0.85."
        ),
    )
    p.add_argument(
        "--ground-truth-dir",
        default="tests/fixtures/agent_evals/template_text/ground_truth",
        help="Autobuilder: output dir for agreed fixtures (default matches eval).",
    )
    p.add_argument(
        "--disagreements-dir",
        default="tests/fixtures/agent_evals/template_text/disagreements",
        help="Autobuilder: output dir for disagreements (human-review queue).",
    )
    args = p.parse_args(argv)

    if args.all_published:
        return _run_autobuilder(args)

    # Single-video mode requires the legacy positional-ish flags.
    if not args.video or not args.slot_boundaries or not args.out:
        p.error(
            "Single-video mode requires --video, --slot-boundaries, and --out. "
            "For batch mode use --all-published."
        )

    boundaries = _parse_boundaries(args.slot_boundaries)
    print(f"Slot boundaries: {boundaries}")

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"Sampling frames every {args.frame_interval}s …")
        frames = _sample_frames(args.video, args.frame_interval, tmpdir)
        print(f"  → {len(frames)} frames")

        per_frame: list[list[FrameDetection]] = []
        for t, path in frames:
            try:
                dets = _run_tesseract(path)
            except Exception as exc:
                print(f"  OCR failed on frame_t={t:.2f}: {exc}", file=sys.stderr)
                dets = []
            for d in dets:
                d.frame_t = t
            per_frame.append(dets)
        print(f"OCR done. {sum(len(f) for f in per_frame)} raw detections.")

        tracks = _group_detections(per_frame)
        overlays = [_track_to_overlay(t, boundaries) for t in tracks]
        # Filter by min visibility
        overlays = [ov for ov in overlays if (ov.end_s - ov.start_s) >= args.min_visibility]
        print(f"Grouped into {len(overlays)} overlays.")

        if not args.no_review:
            overlays = _interactive_review(overlays)

    out_data = {
        "overlays": [_overlay_to_json(ov) for ov in overlays],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out_data, indent=2))
    print(f"\nWrote {len(overlays)} overlays to {args.out}")
    return 0


def _overlay_to_json(ov: GroundTruthOverlay) -> dict:
    return asdict(ov)


if __name__ == "__main__":
    raise SystemExit(main())
