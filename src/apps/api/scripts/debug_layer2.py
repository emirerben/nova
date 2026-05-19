"""Run the Layer-2 text-overlay pipeline on a local video and dump every stage.

Usage:
    cd src/apps/api
    .venv/bin/python scripts/debug_layer2.py \\
        --video /tmp/fdaf3bbc.mp4 \\
        --out /tmp/fdaf3bbc-stages \\
        --slot-boundaries-s 0:5.5,5.5:22.37

Produces in --out:
    stage_a.json   frames: [{path, t_s}, ...]
    stage_b.json   OCR detections: [{frame_t_s, text, polygon, confidence}, ...]
    stage_c.json   temporal text events
    stage_d.json   reconstructed phrases
    stage_e.json   transcript-aligned phrases
    stage_e_dropped.txt   number of phrases dropped at alignment
    stage_f.json   classified phrases (effect, role, size_class, font_color_hex)
    stage_g.json   final TemplateTextOutput

Then prints a one-line-per-stage summary so the operator can pinpoint where
overlay loss / corruption happened:

    A frames=21
    B detections=58
    C events=14
    D phrases=12     ← if user expected ~15, the loss happened in B or D
    E aligned=12 dropped=0
    F classified=12
    G overlays=10    ← if 12→10, two overlays failed schema validation; see logs

When a ground-truth JSON file is provided via --ground-truth, also prints a
per-overlay diff against it (text + bbox IoU + color match) so the operator can
attribute each failure to a specific stage.

Requires GEMINI_API_KEY for stages E and F. Stage A needs ffmpeg on PATH. Stage
B needs Apple Vision (macOS) or google-cloud-vision + GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_boundaries(s: str | None) -> list[tuple[float, float]] | None:
    if not s:
        return None
    out: list[tuple[float, float]] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        a, b = chunk.split(":")
        out.append((float(a), float(b)))
    return out


def _load_transcript(path: str | None) -> list[dict] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "words" in data:
        data = data["words"]
    if not isinstance(data, list):
        raise SystemExit(
            f"--transcript-json {path}: expected list or {{'words': [...]}}, got {type(data)}"
        )
    return data


def _summarize(stages_dir: Path) -> None:
    def _len(name: str) -> str:
        f = stages_dir / f"{name}.json"
        if not f.exists():
            return "MISSING"
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            return "INVALID_JSON"
        if isinstance(data, list):
            return str(len(data))
        if isinstance(data, dict) and "overlays" in data:
            return str(len(data["overlays"]))
        return "1"

    dropped = "?"
    f = stages_dir / "stage_e_dropped.txt"
    if f.exists():
        dropped = f.read_text().strip()

    print()
    print(f"  A frames        = {_len('stage_a')}")
    print(f"  B detections    = {_len('stage_b')}")
    print(f"  C events        = {_len('stage_c')}")
    print(f"  D phrases       = {_len('stage_d')}")
    print(f"  E aligned       = {_len('stage_e')}  ({dropped})")
    print(f"  F classified    = {_len('stage_f')}")
    print(f"  G overlays_out  = {_len('stage_g')}")
    print()
    print("Read each stage_<x>.json to see what dropped where.")


def _diff_against_ground_truth(stages_dir: Path, gt_path: str) -> None:
    # tests/ is not part of the installed package (uv sync only ships app/),
    # so add src/apps/api onto sys.path before importing the scoring module.
    api_root = Path(__file__).resolve().parent.parent
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))
    try:
        from tests.evals.runners.text_overlay_scoring import score_overlays  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            f"--ground-truth requires the test scoring module; could not import "
            f"tests.evals.runners.text_overlay_scoring ({exc}). Run from src/apps/api/."
        ) from exc

    truth = json.loads(Path(gt_path).read_text())["overlays"]
    predicted = json.loads((stages_dir / "stage_g.json").read_text())["overlays"]
    scoring = score_overlays(predicted, truth)
    print()
    print("Ground-truth scoring:")
    print(f"  completeness       = {scoring.completeness:.2f}")
    print(f"  precision          = {scoring.precision:.2f}")
    print(f"  mean_temporal_iou  = {scoring.mean_temporal_iou:.2f}")
    print(f"  mean_spatial_iou   = {scoring.mean_spatial_iou:.2f}")
    print(f"  color_match        = {scoring.color_match_fraction:.2f}")
    print(f"  effect_accuracy    = {scoring.effect_label_accuracy:.2f}")
    print(
        f"  matched {scoring.matched_count}/{scoring.truth_count} (pred={scoring.predicted_count})"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--video", required=True, help="Local path to the template video")
    ap.add_argument("--out", required=True, help="Directory to write stage dumps into")
    ap.add_argument(
        "--slot-boundaries-s", help='Comma-separated "start:end" pairs, e.g. "0:5.5,5.5:22"'
    )
    ap.add_argument("--transcript-json", help="Path to a transcript words JSON file")
    ap.add_argument("--fps", type=float, default=2.0, help="Frame extraction fps (default 2.0)")
    ap.add_argument("--template-id", help="Optional template_id label for logging")
    ap.add_argument("--ground-truth", help="Path to ground-truth JSON for scoring diff")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        print(f"error: --video {video} does not exist", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    from app.pipeline.text_overlay_v2.pipeline import run_full_pipeline

    boundaries = _parse_boundaries(args.slot_boundaries_s)
    transcript = _load_transcript(args.transcript_json)

    print(f"Running Layer-2 pipeline on {video}")
    print(
        f"  out_dir={out_dir}  fps={args.fps}  n_slot_boundaries={len(boundaries or [])}  "
        f"n_transcript_words={len(transcript or [])}"
    )

    output = run_full_pipeline(
        video,
        transcript_words=transcript,
        slot_boundaries_s=boundaries,
        fps=args.fps,
        template_id=args.template_id,
        dump_stages_dir=out_dir,
    )

    print(f"Done. {len(output.overlays)} overlays in final output.")
    _summarize(out_dir)

    if args.ground_truth:
        _diff_against_ground_truth(out_dir, args.ground_truth)

    return 0


if __name__ == "__main__":
    sys.exit(main())
