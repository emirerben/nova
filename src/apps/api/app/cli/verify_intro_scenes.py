"""CLI: compile the five intro teardowns (KRI-1610) and render them through the real Skia path.

The check that replaces the throwaway taste board with real renderer output, before any model
exists. For every teardown it can express, in English and Turkish, it compiles the scene to title
rows, places them with `place_title_look` against a stub face, draws every row with the same Skia
`_draw_frame` the burn uses, and measures the pixels:

* un-clipped: the ink stays inside the frame and the title safe zone (not the placement's word);
* off the face: the ink box covers none of the speaker's face box;
* one ink: every row is drawn in the scene's single ink;
* the right faces: the renderer resolved each row's own face, no tofu, no silent glyph swap;
* off the captions, and the ink reads on the footage (3:1 against the plate behind it);
* rows do not overlap each other; the placement receipt is not `error` or `best_effort`.

References today's engine cannot express are listed with the missing move and drawn as a labelled
blank tile. Four negative controls MUST fail (two inks, a title moved onto the face, a face
swapped for one that cannot draw Turkish, white ink on a light wall); a control that passes means
the check is blind and exits 1.

Run it font-accurately inside the prod image:

    make verify-intro-scenes

Outputs to ``.intro-verify/``: ``report.json`` and ``montage.png`` (read the montage: it is the
content check). The footage is a synthetic plate (a wall and a head-and-shoulders silhouette), so no
person's photo is committed. Exit code 0 when every case PASSes and every control FAILs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# The CLI reads no secrets; importing the app config just needs these to exist.
os.environ.setdefault("STORAGE_BUCKET", "intro-verify")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import overlay_verify as ov  # noqa: E402
from app.pipeline import phone_subtitled_title as title_mod  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.phone_title_look import place_title_look  # noqa: E402
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion  # noqa: E402

_TEARDOWNS = Path(__file__).resolve().parents[2] / "prompts" / "intro_teardowns.json"
LANGUAGES = ("en", "tr")
# The speaker on the synthetic plate: a head with clear wall above it (normalized 0-1).
FACE_BOX = NormalizedBox(0.27, 0.29, 0.74, 0.73)
# Where the speaker's own captions sit, so the title is also kept clear of them.
CAPTION_BOX = NormalizedBox(0.12, 0.81, 0.88, 0.87)
_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
# The title safe zone `phone_title_look` places inside (x 3-97%, y 6-95%).
SAFE = NormalizedBox(0.03, 0.06, 0.97, 0.95)
_OK_STATUS = {"preset", "moved", "moved_below_face", "shrunk", "squeezed"}
_OVERLAP_MAX = 0.02
_VERDICT_COLOR = {
    "PASS": (46, 160, 67),
    "FAIL": (218, 54, 51),
    "SKIPPED": (110, 118, 129),
    "CONTROL-FAILED": (46, 160, 67),  # a control that failed as it must
    "CONTROL-PASSED": (218, 54, 51),  # a control that passed: the check is blind
}


# --- the synthetic footage ----------------------------------------------------------------


PLATES = {
    # (wall, ceiling line, shoulders): the two footage looks the references sit on.
    "light_wall": ((226, 228, 222), (214, 216, 210), (226, 154, 56)),
    "dark_room": ((34, 36, 42), (46, 48, 55), (110, 76, 58)),
}
_MIN_CONTRAST = 3.0  # WCAG large text


def _luminance(rgb: tuple[int, ...]) -> float:
    def channel(c: int) -> float:
        c_ = c / 255
        return c_ / 12.92 if c_ <= 0.03928 else ((c_ + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def plate(kind: str = "light_wall") -> Image.Image:
    """A wall and a head-and-shoulders silhouette inside FACE_BOX, 1080x1920."""
    w, h = PORTRAIT.width, PORTRAIT.height
    wall, ceiling, shoulders = PLATES[kind]
    im = Image.new("RGBA", (w, h), (*wall, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, int(h * 0.05), w, int(h * 0.075)], fill=(*ceiling, 255))  # ceiling line
    draw.polygon(
        [
            (0, h),
            (0, int(h * 0.80)),
            (int(w * 0.28), int(h * 0.74)),
            (int(w * 0.72), int(h * 0.74)),
            (w, int(h * 0.80)),
            (w, h),
        ],
        fill=(*shoulders, 255),
    )  # shoulders
    box = [FACE_BOX.left * w, FACE_BOX.top * h, FACE_BOX.right * w, FACE_BOX.bottom * h]
    draw.ellipse(box, fill=(214, 168, 140, 255))
    caption = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 62)
    draw.text(
        (w / 2, (CAPTION_BOX.top + CAPTION_BOX.bottom) / 2 * h),
        "so here\u2019s the thing",
        font=caption,
        fill=(255, 255, 255, 255),
        anchor="mm",
        stroke_width=3,
        stroke_fill=(40, 40, 40, 255),
    )
    return im


def _stub_sampler(*faces: NormalizedBox):
    regions = [ProtectedRegion(0.0, 30.0, face, kind="face") for face in faces]

    def sample(_path, anchors, **_kwargs):
        return list(regions), {"attempted": len(anchors), "decoded": 6, "detected": len(regions)}

    return sample


# --- measuring ----------------------------------------------------------------------------


def _overlay_for(row: dict[str, Any]) -> dict[str, Any]:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415

    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)],
        video_duration_s=max(float(row["end_s"]), 0.1),
        independent_box_alignment=True,
    )
    return overlay


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _norm(bbox: tuple[int, int, int, int]) -> NormalizedBox:
    w, h = PORTRAIT.width, PORTRAIT.height
    return NormalizedBox(bbox[0] / w, bbox[1] / h, bbox[2] / w, bbox[3] / h)


def _expected_family(row: dict[str, Any], pair: scene_mod.FontPair) -> str:
    style = row["source_params"]["title_look"]["style"]
    return (pair.accent if style == "intro_accent" else pair.solid).family


def verify_rows(
    rows: list[dict[str, Any]],
    receipt: dict[str, Any],
    *,
    face: NormalizedBox,
    scene: scene_mod.IntroScene,
    footage: str = "light_wall",
) -> tuple[str, list[str], Image.Image]:
    """Draw ``rows`` over the plate and measure them. Returns (verdict, reasons, the frame)."""
    from app.pipeline import text_overlay_skia as tos  # noqa: PLC0415

    reasons: list[str] = []
    pair = scene_mod.FONT_PAIRS[scene.font_pair or ""]
    ink_hex = scene_mod.INKS[scene.ink or ""]
    status = str(receipt.get("status"))
    if status not in _OK_STATUS:
        reasons.append(f"placement status {status!r} is not a clean spot")
    if receipt.get("face_collision") or receipt.get("text_overlap"):
        reasons.append("placement reports a face or text collision")
    if len({str(r.get("color")).upper() for r in rows}) != 1:
        reasons.append(f"more than one ink: {sorted({str(r.get('color')) for r in rows})}")
    if any(str(r.get("color")).upper() != ink_hex for r in rows):
        reasons.append(f"a row is not in the scene's ink {ink_hex}")

    backdrop = plate(footage)
    frame = backdrop.copy()
    inks: list[NormalizedBox] = []
    masks: list[Image.Image] = []
    for index, row in enumerate(rows, start=1):
        overlay = _overlay_for(row)
        # Face and glyph checks first: a swapped face also clips, and the swap is the cause.
        missing = ov.missing_rendered_glyphs(overlay)
        if missing:
            reasons.append(f"row {index}: tofu for {''.join(missing)}")
        resolution = tos._resolved_typeface_for_render(overlay)
        expected = _expected_family(row, pair)
        if row.get("font_family") != expected:
            reasons.append(
                f"row {index}: font {row.get('font_family')!r}, the pair says {expected!r}"
            )
        if resolution.glyph_fallback or resolution.fallback:
            reasons.append(f"row {index}: renderer swapped the face (resolved {resolution.name!r})")
        frame = Image.alpha_composite(frame, ov.render_overlay_frame(overlay))
        # Measure the glyph ink, not its blurred shadow.
        bare = ov.render_overlay_frame({**overlay, "shadow_enabled": False})
        bbox = ov.text_bbox(bare)
        verdict, why = ov.check_clipping(bbox, bare.width, bare.height)
        if verdict != "PASS" or bbox is None:
            reasons.append(f"row {index}: {why}")
            continue
        box = _norm(bbox)
        inks.append(box)
        masks.append(bare.getchannel("A").point(lambda p: 255 if p > 64 else 0))
        behind = backdrop.crop(bbox).convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
        lum_a, lum_b = _luminance(behind), _luminance(_rgb(scene_mod.INKS[scene.ink or ""]))
        ratio = (max(lum_a, lum_b) + 0.05) / (min(lum_a, lum_b) + 0.05)
        if ratio < _MIN_CONTRAST:
            reasons.append(f"row {index}: ink does not read on the footage ({ratio:.1f}:1)")
        if (
            box.left < SAFE.left - 1e-6
            or box.right > SAFE.right + 1e-6
            or box.top < SAFE.top - 1e-6
            or box.bottom > SAFE.bottom + 1e-6
        ):
            reasons.append(f"row {index}: ink outside the title safe zone {box.as_dict()}")
        if box.coverage_by(face) > title_mod._MAX_FACE_COVERAGE:
            reasons.append(f"row {index}: ink covers the face ({box.coverage_by(face):.2f})")
        if box.coverage_by(CAPTION_BOX) > title_mod._MAX_FACE_COVERAGE:
            reasons.append(
                f"row {index}: ink covers the captions ({box.coverage_by(CAPTION_BOX):.2f})"
            )
    for a, b in zip(masks, masks[1:], strict=False):
        # Real ink on ink (loopy ascenders may share a box with the row above without touching).
        touching = ImageChops.darker(a, b).histogram()[255]
        smaller = min(a.histogram()[255], b.histogram()[255]) or 1
        if touching / smaller > _OVERLAP_MAX:
            reasons.append("two rows overlap each other")
    return ("FAIL" if reasons else "PASS"), reasons, frame


def _place(rows: list[dict[str, Any]], position: str | None) -> tuple[list[dict], dict]:
    return place_title_look(
        rows,
        clip_path="plate.mp4",
        keep_segments=[(0.0, 30.0)],
        display_width=PORTRAIT.width,
        display_height=PORTRAIT.height,
        canvas=PORTRAIT,
        sample=_stub_sampler(FACE_BOX),
        position=position,
        avoid=[CAPTION_BOX],
    )


# --- the cases ----------------------------------------------------------------------------


def _row_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("text", "font_family", "size_px", "color", "effect", "y_frac", "text_case")
    return [{k: row.get(k) for k in keys if row.get(k) is not None} for row in rows]


def _compile_and_place(example: dict[str, Any], language: str):
    scene = scene_mod.IntroScene.model_validate(example["scene"])
    out = scene_mod.compile_intro_scene(
        scene, example["title"], canvas=PORTRAIT, timeline_duration_s=30.0, language=language
    )
    if out.rows is None:
        return scene, out, None, None
    placed, receipt = _place(out.rows, out.position)
    return scene, out, placed, receipt


def _case(base: dict[str, Any], frame: Image.Image | None, **extra: Any) -> dict[str, Any]:
    return {**base, **extra, "_frame": frame}


def _controls(teardowns: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Cases the check MUST fail. Each takes a real compiled scene and breaks one thing."""

    def keep(rows: list[dict[str, Any]]) -> None:
        return None

    cases = []
    for name, ref, language, footage, mutate in (
        ("two_inks", "ref1", "en", None, lambda rows: rows[1].__setitem__("color", "#FFFFFF")),
        (
            "title_on_face",
            "ref1",
            "en",
            None,
            lambda rows: [
                row.__setitem__("y_frac", y) for row, y in zip(rows, (0.42, 0.52), strict=False)
            ],
        ),
        (
            "face_swapped_font",
            "ref1",
            "tr",
            None,
            lambda rows: rows[0].__setitem__("font_family", "Satisfy"),
        ),
        # white ink drawn on the light wall it was not made for
        ("ink_unreadable", "ref5", "en", "light_wall", keep),
    ):
        example = teardowns[ref]["examples"][language]
        scene, out, placed, receipt = _compile_and_place(example, language)
        assert placed is not None and receipt is not None, out.reason
        mutated = [json.loads(json.dumps(row)) for row in placed]
        mutate(mutated)
        verdict, reasons, frame = verify_rows(
            mutated,
            receipt,
            face=FACE_BOX,
            scene=scene,
            footage=footage or teardowns[ref]["footage_plate"],
        )
        cases.append(
            _case(
                {
                    "id": ref,
                    "language": language,
                    "title": example["title"],
                    "control": name,
                    "rendered": True,
                    "verdict": verdict,
                    "reasons": reasons,
                    "rows": _row_summary(mutated),
                },
                frame,
            )
        )
    return cases


def build_cases() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(_TEARDOWNS.read_text())
    teardowns = {t["id"]: t for t in data["teardowns"]}
    cases: list[dict[str, Any]] = []
    for teardown in data["teardowns"]:
        examples = teardown.get("examples") or {}
        for language in LANGUAGES:
            base: dict[str, Any] = {
                "id": teardown["id"],
                "name": teardown["name"],
                "language": language,
                "control": None,
                "expressible": teardown["expressible"],
                "missing_moves": teardown["missing_moves"],
            }
            if language not in examples:
                cases.append(
                    _case(
                        {
                            **base,
                            "rendered": False,
                            "verdict": "SKIPPED",
                            "title": None,
                            "reasons": [m["piece"] for m in teardown["missing_moves"]],
                        },
                        None,
                    )
                )
                continue
            example = examples[language]
            scene, out, placed, receipt = _compile_and_place(example, language)
            base.update(title=example["title"], scene=example["scene"], rendered=True)
            if placed is None or receipt is None:
                cases.append(
                    _case(
                        {**base, "verdict": "FAIL", "reasons": [f"compile declined: {out.reason}"]},
                        None,
                    )
                )
                continue
            verdict, reasons, frame = verify_rows(
                placed, receipt, face=FACE_BOX, scene=scene, footage=teardown["footage_plate"]
            )
            cases.append(
                _case(
                    {
                        **base,
                        "verdict": verdict,
                        "reasons": reasons,
                        "rows": _row_summary(placed),
                        "placement": {
                            k: receipt.get(k)
                            for k in ("status", "spot", "scale", "coverage", "lines")
                            if k in receipt
                        },
                        "sound": out.sound,
                        "typed_title": out.typed_title,
                    },
                    frame,
                )
            )
    cases.extend(_controls(teardowns))
    return cases, data


# --- montage ------------------------------------------------------------------------------


def _wrap(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return [*lines, line] if line else lines


def _status(case: dict[str, Any]) -> str:
    if case["control"]:
        return "CONTROL-FAILED" if case["verdict"] == "FAIL" else "CONTROL-PASSED"
    return case["verdict"]


def build_montage(cases: list[dict[str, Any]], out_path: str) -> None:
    thumb_w, pad, label_h = 300, 12, 74
    thumb_h = int(thumb_w * PORTRAIT.height / PORTRAIT.width)
    cols = 3
    ordered = [c for c in cases if not c["control"]] + [c for c in cases if c["control"]]
    # Pair EN / TR per teardown across two columns, controls fill the last row(s).
    cell_w, cell_h = thumb_w + pad * 2, thumb_h + label_h + pad * 2
    rows_n = (len(ordered) + cols - 1) // cols
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows_n), (22, 27, 34))
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 12)
    for i, case in enumerate(ordered):
        r, c = divmod(i, cols)
        x, y = c * cell_w + pad, r * cell_h + pad
        color = _VERDICT_COLOR[_status(case)]
        frame = case.get("_frame")
        if frame is not None:
            tile = frame.convert("RGB").resize((thumb_w, thumb_h))
            fb = [
                FACE_BOX.left * thumb_w,
                FACE_BOX.top * thumb_h,
                FACE_BOX.right * thumb_w,
                FACE_BOX.bottom * thumb_h,
            ]
            ImageDraw.Draw(tile).rectangle(fb, outline=(40, 120, 220), width=2)
        else:
            tile = Image.new("RGB", (thumb_w, thumb_h), (48, 54, 61))
            td = ImageDraw.Draw(tile)
            td.text((12, 16), "NOT EXPRESSIBLE TODAY", fill=(230, 230, 230), font=label_font)
            for n, move in enumerate(case["missing_moves"][:4]):
                for k, text in enumerate(_wrap(move["piece"], 38)[:2]):
                    td.text(
                        (12, 56 + n * 54 + k * 14),
                        ("- " if k == 0 else "  ") + text,
                        fill=(190, 198, 205),
                        font=label_font,
                    )
        sheet.paste(tile, (x, y))
        draw.rectangle([x - 3, y - 3, x + thumb_w + 2, y + thumb_h + 2], outline=color, width=3)
        head = f"[{_status(case)}] {case['id']} {case['language'].upper()}"
        if case["control"]:
            head += f"  control: {case['control']}"
        draw.text((x, y + thumb_h + 6), head, fill=color, font=label_font)
        sub = case.get("title") or case.get("name") or ""
        draw.text((x, y + thumb_h + 22), sub[:48], fill=(201, 209, 217), font=label_font)
        for k, text in enumerate(_wrap("; ".join(case["reasons"]), 52)[:2]):
            draw.text((x, y + thumb_h + 38 + k * 14), text, fill=(160, 168, 176), font=label_font)
    sheet.save(out_path, "PNG")


# --- run ----------------------------------------------------------------------------------


def run(*, out_dir: str) -> dict[str, Any]:
    import skia  # noqa: PLC0415

    os.makedirs(out_dir, exist_ok=True)
    cases, data = build_cases()
    montage = os.path.join(out_dir, "montage.png")
    report_path = os.path.join(out_dir, "report.json")
    build_montage(cases, montage)
    public = [{k: v for k, v in case.items() if k != "_frame"} for case in cases]
    checked = [c for c in public if not c["control"] and c["rendered"]]
    blind = [c["control"] for c in public if c["control"] and c["verdict"] != "FAIL"]
    failed = [f"{c['id']}/{c['language']}" for c in checked if c["verdict"] != "PASS"]
    report = {
        "teardowns_version": data["version"],
        "environment": "prod-image" if os.environ.get("NOVA_IN_PROD_IMAGE") else "host",
        "skia": getattr(skia, "__version__", "unknown"),
        "overall": "PASS" if not failed and not blind else "FAIL",
        "failed": failed,
        "blind_controls": blind,
        "inexpressible": [
            {"id": t["id"], "expressible": t["expressible"], "missing_moves": t["missing_moves"]}
            for t in data["teardowns"]
            if t["missing_moves"]
        ],
        "engine_limits": data["engine_limits"],
        "cases": public,
        "montage": "montage.png",
        "report": "report.json",
    }
    Path(report_path).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_intro_scenes", description=__doc__)
    parser.add_argument("--out", default=".intro-verify", help="output dir (default .intro-verify)")
    args = parser.parse_args(argv)
    report = run(out_dir=os.path.abspath(args.out))
    cases = [c for c in report["cases"] if not c["control"]]
    print(f"\nintro-verify: {report['overall']}  ({report['environment']}, skia {report['skia']})")
    for case in report["cases"]:
        label = f"control {case['control']}" if case["control"] else "case"
        print(
            f"  [{_status(case)}] {label} {case['id']}/{case['language']}: "
            f"{'; '.join(case['reasons']) or 'ok'}"
        )
    print(
        f"  rendered {sum(1 for c in cases if c['rendered'])} case(s); "
        f"{len(report['inexpressible'])} reference(s) name a missing move"
    )
    print(f"  report:  {os.path.join(args.out, report['report'])}")
    print(f"  montage: {os.path.join(args.out, report['montage'])}  <- read this")
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
