"""CLI: render a recipe's text overlays through the real Skia path and verify
the burned text is un-clipped + matches the recipe — BEFORE opening a PR.

This is the automation behind CLAUDE.md's rule: "an agentic/music overlay change
is NOT verified by the admin preview — verify the actual burned (Skia) video."

Run it font-accurately inside the prod Docker image:

    make verify-overlays ARGS="--fixtures"
    make verify-overlays ARGS="--recipe path/to/recipe.json"
    make verify-overlays ARGS="--template <uuid>"   # resolves the cached recipe

Outputs to ``.overlay-verify/``:
  - ``report.json`` — per-overlay clipping (and, if OCR ran, content) verdicts.
  - ``montage.png``  — contact sheet of every rendered overlay, border-coded by
    verdict. The agent VIEWS this as the content check (Claude reads PNGs), which
    beats tesseract on stylized fonts.

Stages
------
The clipping check is pure PIL and runs in-container (prod fonts). OCR needs
tesseract, which is not in the prod image, so content-OCR is opt-in:
  --stage render   render frames + clipping + montage (no OCR)   [in container]
  --stage ocr      OCR the already-rendered frames, finalize      [on host]
  --stage all      render + OCR in one process (default)          [host, or
                   container if tesseract is present]

Exit code: 0 when overall verdict is PASS/WARN/SKIPPED, 1 when any overlay FAILs
(so it composes in shells and the agent pre-PR gate).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from app.pipeline import overlay_verify as ov

_REPO_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "overlay_verify"
_VERDICT_COLOR = {
    "PASS": (46, 160, 67),
    "WARN": (210, 153, 34),
    "FAIL": (218, 54, 51),
    "SKIPPED": (110, 118, 129),
}


# -- Recipe resolution -------------------------------------------------------


def _load_recipe(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_fixtures() -> dict:
    """Merge every fixture under tests/fixtures/overlay_verify into one recipe,
    one fixture per slot so the montage groups them."""
    if not _REPO_FIXTURES.is_dir():
        raise SystemExit(f"no fixtures dir at {_REPO_FIXTURES}")
    slots = []
    for fp in sorted(_REPO_FIXTURES.glob("*.json")):
        data = json.loads(fp.read_text())
        overlays = data.get("overlays") if isinstance(data, dict) else data
        if isinstance(data, dict) and "slots" in data:
            for slot in data["slots"]:
                slots.append(slot)
            continue
        slots.append({"name": fp.stem, "text_overlays": overlays or []})
    if not slots:
        raise SystemExit(f"no *.json fixtures found in {_REPO_FIXTURES}")
    return {"slots": slots}


def _find_recipe_in(obj: object) -> dict | None:
    """Recursively locate a recipe-shaped object (a dict with a 'slots' list
    whose items carry 'text_overlays'). Shape-agnostic so it works whether the
    admin debug payload nests the recipe under template_recipe or template_text
    agent-run output."""
    if isinstance(obj, dict):
        slots = obj.get("slots")
        if isinstance(slots, list) and any(
            isinstance(s, dict) and "text_overlays" in s for s in slots
        ):
            return obj
        for v in obj.values():
            found = _find_recipe_in(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_recipe_in(v)
            if found:
                return found
    return None


def _find_admin_py() -> Path | None:
    """Locate scripts/admin.py by walking up from this file. Robust to layout:
    repo-root ``scripts/admin.py`` on the host, and ``/app/scripts/admin.py`` in
    the prod image (the Dockerfile copies scripts/ to /app/scripts)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "admin.py"
        if candidate.exists():
            return candidate
    return None


def _resolve_template(template_id: str) -> dict:
    """Fetch a template's cached recipe via scripts/admin.py. Needs the admin
    token + network, so this resolves the recipe wherever admin.py + creds live."""
    admin = _find_admin_py()
    if admin is None:
        raise SystemExit(
            "--template needs scripts/admin.py + an admin token. Resolve the recipe "
            "where those are available and pass --recipe instead."
        )
    proc = subprocess.run(
        [sys.executable, str(admin), "GET", f"templates/{template_id}/debug"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"admin.py fetch failed: {proc.stderr[:300]}")
    recipe = _find_recipe_in(json.loads(proc.stdout))
    if recipe is None:
        raise SystemExit(
            f"could not find a slots/text_overlays recipe in the debug payload for "
            f"template {template_id}. Has it been analyzed?"
        )
    return recipe


# -- Montage -----------------------------------------------------------------


def _build_montage(report: ov.VerifyReport, frames_dir: str, out_path: str) -> None:
    frames = []
    for o in report.overlays:
        fp = os.path.join(frames_dir, ov.frame_name(o.slot_index, o.overlay_index))
        if os.path.exists(fp):
            frames.append((o, fp))
    if not frames:
        return
    thumb_w, pad, label_h = 300, 12, 46
    scale = thumb_w / ov.tos.CANVAS_W
    thumb_h = int(ov.tos.CANVAS_H * scale)
    cols = min(4, len(frames))
    rows = (len(frames) + cols - 1) // cols
    cell_w, cell_h = thumb_w + pad * 2, thumb_h + label_h + pad * 2
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (22, 27, 34))
    draw = ImageDraw.Draw(sheet)
    for i, (res, fp) in enumerate(frames):
        r, c = divmod(i, cols)
        x, y = c * cell_w + pad, r * cell_h + pad
        thumb = Image.open(fp).convert("RGB").resize((thumb_w, thumb_h))
        color = _VERDICT_COLOR.get(res.verdict, (110, 118, 129))
        sheet.paste(thumb, (x, y))
        draw.rectangle([x - 3, y - 3, x + thumb_w + 2, y + thumb_h + 2], outline=color, width=3)
        label = f"[{res.verdict}] s{res.slot_index}/o{res.overlay_index} {res.text_anchor}"
        sub = (res.text[:34] + "…") if len(res.text) > 35 else res.text
        draw.text((x, y + thumb_h + 6), label, fill=color)
        draw.text((x, y + thumb_h + 24), sub, fill=(201, 209, 217))
    sheet.save(out_path, "PNG")


# -- Report I/O for the two-stage flow ---------------------------------------


def _report_from_dict(d: dict) -> ov.VerifyReport:
    rep = ov.VerifyReport(render_mode=d.get("render_mode", ov.RENDER_DRAW_FRAME))
    for od in d.get("overlays", []):
        rep.overlays.append(ov.OverlayResult(**od))
    return rep


def _print_summary(report: ov.VerifyReport, out_dir: str) -> None:
    c = report.counts
    print(
        f"\noverlay-verify: {report.overall}  "
        f"(PASS={c['PASS']} WARN={c['WARN']} FAIL={c['FAIL']} SKIPPED={c['SKIPPED']})"
    )
    for o in report.overlays:
        if o.verdict in ("FAIL", "WARN"):
            print(
                f"  [{o.verdict}] slot {o.slot_index}/ov {o.overlay_index} "
                f"{o.text!r}: {'; '.join(o.reasons)}"
            )
    print(f"  report:  {os.path.join(out_dir, 'report.json')}")
    print(f"  montage: {os.path.join(out_dir, 'montage.png')}  ← view this for the content check")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_overlays", description=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--recipe", help="path to a recipe JSON ({'slots':[{'text_overlays':[...]}]})")
    src.add_argument("--template", help="template UUID (fetches recipe via scripts/admin.py)")
    src.add_argument("--fixtures", action="store_true", help="verify the committed fixtures")
    p.add_argument("--stage", choices=["render", "ocr", "all"], default="all")
    p.add_argument(
        "--render-mode",
        choices=["draw_frame", "burn"],
        default="draw_frame",
        help="draw_frame: fast Skia canvas (default). burn: full ffmpeg burn + extract.",
    )
    p.add_argument("--out", default=".overlay-verify", help="output dir (default .overlay-verify)")
    p.add_argument("--no-ocr", action="store_true", help="skip OCR even if tesseract is available")
    args = p.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    frames_dir = os.path.join(out_dir, "frames")
    report_path = os.path.join(out_dir, "report.json")
    os.makedirs(out_dir, exist_ok=True)

    # --- stage: ocr (host) — enrich an existing render report -------------
    if args.stage == "ocr":
        if not os.path.exists(report_path):
            raise SystemExit(f"--stage ocr needs a prior render report at {report_path}")
        if not ov.ocr_available():
            # Explicit, not a silent pass: the clipping verdicts in the report
            # still stand; we just couldn't add the OCR content signal.
            raise SystemExit("--stage ocr requested but tesseract/pytesseract unavailable on host")
        report = _report_from_dict(json.loads(Path(report_path).read_text()))
        for o in report.overlays:
            ov.ocr_result_from_frame(o, frames_dir)
        report.ocr_available = True
        Path(report_path).write_text(json.dumps(report.to_dict(), indent=2))
        _build_montage(report, frames_dir, os.path.join(out_dir, "montage.png"))
        _print_summary(report, out_dir)
        return 1 if report.overall == "FAIL" else 0

    # --- resolve the recipe -----------------------------------------------
    if args.recipe:
        recipe = _load_recipe(args.recipe)
    elif args.template:
        recipe = _resolve_template(args.template)
        if not os.environ.get("NOVA_IN_PROD_IMAGE"):
            print(
                "WARNING: rendering on the host — fonts may differ from prod. "
                "For font-accurate results run `make verify-overlays`.",
                file=sys.stderr,
            )
    elif args.fixtures:
        recipe = _load_fixtures()
    else:
        raise SystemExit("one of --recipe / --template / --fixtures is required")

    run_ocr = (args.stage == "all") and not args.no_ocr
    report = ov.verify_recipe(
        recipe,
        render_mode=args.render_mode,
        run_ocr=run_ocr,
        frame_out_dir=frames_dir,
    )
    if args.stage == "render":
        # Make the deferred content check explicit so a render-only report is
        # never mistaken for a clean content pass.
        for o in report.overlays:
            if o.content == "SKIPPED" and "OCR" not in " ".join(o.reasons):
                o.reasons.append("content OCR deferred to host (--stage ocr)")

    Path(report_path).write_text(json.dumps(report.to_dict(), indent=2))
    _build_montage(report, frames_dir, os.path.join(out_dir, "montage.png"))
    _print_summary(report, out_dir)
    return 1 if report.overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
