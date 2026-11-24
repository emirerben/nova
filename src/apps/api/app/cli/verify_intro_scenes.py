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

KRI-1611 adds the picks: the real dispatch pick (`intro_scene_pick`) for several job seeds, in
English and Turkish, on a light and a dark plate. Each pick is fitted with the real
`place_title_look`, takes its one ink from the plate (`intro_ink_pass`), and goes through the same
measurements; wiring controls then check that the same seed is stable, a recent look is excluded,
an unclean real receipt falls back to today's title, and the approval check accepts her slashless
title as a stack and refuses a changed word. A pick drawn in an ink that does not read on its
plate is a negative control and MUST fail.

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
from app.pipeline import intro_scene_pick as pick_mod  # noqa: E402
from app.pipeline import overlay_verify as ov  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.render_geometry import NormalizedBox  # noqa: E402

_TEARDOWNS = Path(__file__).resolve().parents[2] / "prompts" / "intro_teardowns.json"
LANGUAGES = ("en", "tr")
# The speaker on the synthetic plate IS the compiler's typical speaker, so a scene the compiler
# accepts is judged here against the same head and captions.
FACE_BOX = scene_mod.TYPICAL_FACE
CAPTION_BOX = scene_mod.TYPICAL_CAPTIONS
_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
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


# --- measuring ----------------------------------------------------------------------------


_overlay_for = scene_mod.overlay_for_row


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
    if not scene_mod.placement_is_clean(receipt):
        reasons.append(
            f"placement is not a clean spot (status {receipt.get('status')!r}, "
            f"face_collision {receipt.get('face_collision')}, text_overlap "
            f"{receipt.get('text_overlap')})"
        )
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
        reasons.extend(
            f"row {index}: {problem}"
            for problem in scene_mod.ink_box_problems(box, face=face, captions=CAPTION_BOX)
        )
    for a, b in zip(masks, masks[1:], strict=False):
        # Real ink on ink (loopy ascenders may share a box with the row above without touching).
        touching = ImageChops.darker(a, b).histogram()[255]
        smaller = min(a.histogram()[255], b.histogram()[255]) or 1
        if touching / smaller > _OVERLAP_MAX:
            reasons.append("two rows overlap each other")
    return ("FAIL" if reasons else "PASS"), reasons, frame


def _place(rows: list[dict[str, Any]], position: str | None) -> tuple[list[dict], dict]:
    return scene_mod.place_on_typical_face(rows, position, PORTRAIT)


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


# Titles that stress the faces: a long English one, and Turkish ones with dotted capitals, a
# long first line and a trailing one-word line.
SWEEP_TITLES = (
    ("en", "how am i / really loving this"),
    ("tr", "bu yılın en büyük tasarım trendi şu an"),
    ("tr", "bir tasarımcı olarak bir günde ne yiyorum / dürüstçe"),
)


def _sweep_voices(n: int, *, thorough: bool) -> list[tuple[str, ...]]:
    half = max(1, n // 2)
    splits = [
        ("solid",) * (n - 1) + ("accent",),
        ("solid",) * half + ("accent",) * (n - half),
    ]
    if thorough:
        splits += [
            ("accent",) + ("solid",) * (n - 1),
            ("accent",) * half + ("solid",) * (n - half),
        ]
    return splits


def sweep_issued_menu(*, thorough: bool = True) -> dict[str, Any]:
    """Every issued font pair x zone over the stress titles and four voice splits (two when not
    ``thorough``): whatever
    the compiler lets through must PASS the same render check; the rest must be declined with
    a reason. Nothing is hand-picked, so this is the claim "the issued menu is safe"."""
    compiled = 0
    declined: dict[str, int] = {}
    failed: list[dict[str, Any]] = []
    for language, title in SWEEP_TITLES:
        menu = scene_mod.issue_menu(title)
        words = len(scene_mod.title_words(title))
        for pair in menu.font_pairs:
            for zone in menu.zones:
                for voices in _sweep_voices(words, thorough=thorough):
                    scene = scene_mod.IntroScene(
                        decision="compose",
                        voices=voices,
                        font_pair=pair,
                        ink="oxblood",
                        zone=zone,
                        entrance="fade",
                        sound="none",
                    )
                    out = scene_mod.compile_intro_scene(
                        scene, title, canvas=PORTRAIT, timeline_duration_s=30.0, language=language
                    )
                    if out.rows is None:
                        declined[out.reason] = declined.get(out.reason, 0) + 1
                        continue
                    compiled += 1
                    placed, receipt = _place(out.rows, out.position)
                    verdict, reasons, _ = verify_rows(
                        placed, receipt, face=FACE_BOX, scene=scene, footage="light_wall"
                    )
                    if verdict != "PASS":
                        failed.append(
                            {
                                "title": title,
                                "font_pair": pair,
                                "zone": zone,
                                "voices": "".join(v[0] for v in voices),
                                "reasons": reasons,
                            }
                        )
    return {"compiled": compiled, "declined": declined, "failed": failed}


PICK_TITLES = (
    ("en", "the biggest design trend right now"),
    ("tr", "bu yılın en büyük tasarım trendi şu an"),
)
PICK_SEEDS = ("kri1611-a", "kri1611-b", "kri1611-c")
PICK_PLATES = ("light_wall", "dark_room")


def _take_frames(kind: str) -> list[Any]:
    from app.pipeline import caption_legibility as legibility  # noqa: PLC0415

    grey = plate(kind).convert("L").resize((270, 480))
    return [legibility.GreyFrame(grey.tobytes(), 270, 480, 0.0, 30.0)]


def _fit_pick(title: str, language: str, seed: str, kind: str, *, force_ink: str | None = None):
    """The dispatch pick for ``seed`` fitted to the plate, as the worker draws it."""
    ask = {
        "version": pick_mod.INTRO_SCENE_VERSION,
        "seed": seed,
        "inks": pick_mod.ink_order(seed),
        "candidates": pick_mod.pick_candidates(title, seed=seed),
    }
    if force_ink:
        ask["inks"] = [force_ink]
    rows, receipt, info = pick_mod.fit_intro_scene(
        ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=pick_mod.language_of(title, language),
        place=lambda r, position: _place(r, position),
    )
    if rows is not None and not force_ink:
        rows = pick_mod.intro_ink_pass(rows, _take_frames(kind), canvas=PORTRAIT)
    return ask, rows, receipt, info


def _pick_case(title: str, language: str, seed: str, kind: str, *, force_ink: str | None = None):
    base: dict[str, Any] = {
        "id": f"pick-{seed.split('-')[-1]}-{kind.split('_')[0]}",
        "name": "seeded pick",
        "language": language,
        "control": "pick_ink_unreadable" if force_ink else None,
        "expressible": True,
        "missing_moves": [],
        "title": title,
        "rendered": True,
    }
    ask, rows, receipt, info = _fit_pick(title, language, seed, kind, force_ink=force_ink)
    if rows is None or receipt is None:
        return _case(
            {**base, "verdict": "FAIL", "reasons": [f"no fit: {info.get('reason')}"]}, None
        )
    chosen = info["chosen"]
    ink_name = next(
        n for n, hex_ in scene_mod.INKS.items() if hex_ == str(rows[0]["color"]).upper()
    )
    scene = scene_mod.IntroScene(
        decision="compose",
        voices=tuple(ask["candidates"][chosen["candidate"]]["voices"]),
        font_pair=chosen["font_pair"],
        ink=ink_name,
        zone=chosen["zone"],
        entrance=ask["candidates"][chosen["candidate"]]["entrance"],
        sound="none",
    )
    verdict, reasons, frame = verify_rows(rows, receipt, face=FACE_BOX, scene=scene, footage=kind)
    return _case(
        {
            **base,
            "verdict": verdict,
            "reasons": reasons,
            "rows": _row_summary(rows),
            "pick": {
                "seed": seed,
                "font_pair": chosen["font_pair"],
                "zone": chosen["zone"],
                "ink": ink_name,
                "entrance": scene.entrance,
                "tried": len(info["tried"]),
            },
        },
        frame,
    )


def pick_cases() -> list[dict[str, Any]]:
    cases = [
        _pick_case(title, language, seed, kind)
        for language, title in PICK_TITLES
        for seed in PICK_SEEDS
        for kind in PICK_PLATES
    ]
    # Control: oxblood forced onto the dark room must be seen as unreadable.
    language, title = PICK_TITLES[0]
    cases.append(_pick_case(title, language, PICK_SEEDS[0], "dark_room", force_ink="oxblood"))
    return cases


def wiring_controls() -> list[dict[str, Any]]:
    """Checks on the pick's own contract. Each names what it expects and what it saw."""
    from app.pipeline.phone_title_look import stacked_title_matches  # noqa: PLC0415

    language, title = PICK_TITLES[0]
    out: list[dict[str, Any]] = []

    def check(name: str, expected: str, observed: str, ok: bool) -> None:
        out.append({"name": name, "expected": expected, "observed": observed, "ok": bool(ok)})

    a = pick_mod.pick_candidates(title, seed="s", recent=())
    check(
        "same_seed_is_stable",
        "equal",
        "equal" if a == pick_mod.pick_candidates(title, seed="s") else "differ",
        a == pick_mod.pick_candidates(title, seed="s"),
    )
    recent = (a[0]["font_pair"],)
    excluded = pick_mod.pick_candidates(title, seed="s", recent=recent)
    check(
        "recent_look_is_tried_last",
        f"first pair != {recent[0]}",
        f"first pair {excluded[0]['font_pair']}",
        excluded[0]["font_pair"] != recent[0],
    )

    def unclean(rows, _position):
        return rows, {"status": "best_effort", "face_collision": True}

    ask = {
        "version": 1,
        "seed": "s",
        "inks": pick_mod.ink_order("s"),
        "candidates": a,
    }
    fell_back, _receipt, info = pick_mod.fit_intro_scene(
        ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=None,
        place=unclean,
    )
    check("unclean_real_receipt_falls_back", "no rows", info["reason"], fell_back is None)

    _ask, rows, receipt, _info = _fit_pick(title, language, PICK_SEEDS[0], "light_wall")
    assert rows is not None
    evidence = [
        {
            "element_id": f"text-{i}",
            "text": row["text"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "anchor_y": row["y_frac"],
        }
        for i, row in enumerate(rows)
    ]
    accepted = stacked_title_matches(evidence, title, tolerance_s=0.05)
    check(
        "approval_accepts_her_title_as_a_stack",
        "1 match",
        f"{len(accepted)} match",
        len(accepted) == 1,
    )
    wrong = [{**evidence[0], "text": evidence[0]["text"] + "s"}, *evidence[1:]]
    refused = stacked_title_matches(wrong, title, tolerance_s=0.05)
    check("approval_refuses_a_changed_word", "no match", f"{len(refused)} match", not refused)
    return out


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
    cases.extend(pick_cases())
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


def run(*, out_dir: str, sweep: bool = True) -> dict[str, Any]:
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
    sweep_report = sweep_issued_menu() if sweep else {"compiled": 0, "declined": {}, "failed": []}
    wiring = wiring_controls()
    wiring_failed = [w["name"] for w in wiring if not w["ok"]]
    report = {
        "teardowns_version": data["version"],
        "environment": "prod-image" if os.environ.get("NOVA_IN_PROD_IMAGE") else "host",
        "skia": getattr(skia, "__version__", "unknown"),
        "overall": "PASS"
        if not failed and not blind and not sweep_report["failed"] and not wiring_failed
        else "FAIL",
        "failed": failed,
        "wiring": wiring,
        "sweep": sweep_report,
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
    for w in report["wiring"]:
        print(f"  [{'PASS' if w['ok'] else 'FAIL'}] wiring {w['name']}: {w['observed']}")
    sweep = report["sweep"]
    print(
        f"  sweep: {sweep['compiled']} issued scene(s) rendered, {len(sweep['failed'])} failed; "
        f"declined {sweep['declined']}"
    )
    for bad in sweep["failed"][:10]:
        print(f"    FAIL {bad['font_pair']}/{bad['zone']} {bad['voices']}: {bad['reasons']}")
    print(
        f"  rendered {sum(1 for c in cases if c['rendered'])} case(s); "
        f"{len(report['inexpressible'])} reference(s) name a missing move"
    )
    print(f"  report:  {os.path.join(args.out, report['report'])}")
    print(f"  montage: {os.path.join(args.out, report['montage'])}  <- read this")
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
