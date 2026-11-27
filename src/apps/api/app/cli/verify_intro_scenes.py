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
* rows do not overlap each other, unless they declare the script-over-serif tuck, which must then
  be drawn (KRI-1618); each row enters when it declares (a late accent at its issued delay);
* the placement receipt is not `error` or `best_effort`.

References today's engine cannot express are listed with the missing move and drawn as a labelled
blank tile. Four negative controls MUST fail (two inks, a title moved onto the face, a face
swapped for one that cannot draw Turkish, white ink on a light wall); a control that passes means
the check is blind and exits 1.

KRI-1618 adds the two moves (`move_cases`): a late accent and a script tucked onto the serif, in
English and Turkish, the late accent also as a still before it enters, tucks in the sweep, and four
more controls that MUST fail (a tuck without its declaration, a declared tuck drawn as a gap, an
unissued delay, a solid line waiting instead of the accent).

KRI-1612 adds the model arm (`verify_intro_scene_model`): the real agent, parser and fit with the
provider call replayed, drawn and measured the same way (see that module).

KRI-1615 adds her photo cards (`props_cases`): ref 3's title with ``around_speaker`` and ref 5's
with ``grid_2x2``, in English and Turkish, compiled with the cards on a speaker framed with room
above and below (`ROOMY_FACE`; on the typical close speaker cards decline, a wiring check). Each
card is drawn as a neutral plate at its planned box under the real Skia title and measured
(`card_reasons`): inside the frame, off the face, the caption row, the Kria wordmark and the other
cards, clear of the title's drawn ink, the title between the card rows. Four controls MUST fail
(a card moved onto the title, onto the face, past the frame edge, onto the caption row), and
`sweep_props` renders every arrangement x photo shape x issued pair x top zone over the stress
titles.

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
import itertools
import json
import os
import sys
from collections.abc import Sequence
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
from app.services import intro_props_layout as props_mod  # noqa: E402
from app.services.phone_card_layout import EDGE_INSET, WATERMARK_BOX, boxes_intersect  # noqa: E402

_TEARDOWNS = Path(__file__).resolve().parents[2] / "prompts" / "intro_teardowns.json"
LANGUAGES = ("en", "tr")
# The speaker on the synthetic plate IS the compiler's typical speaker, so a scene the compiler
# accepts is judged here against the same head and captions.
FACE_BOX = scene_mod.TYPICAL_FACE
CAPTION_BOX = scene_mod.TYPICAL_CAPTIONS
# KRI-1615: the footage refs 3 and 5 suit, a speaker centred with room above and below. On the
# typical (close) speaker the title alone fills the room above the head, so cards decline there.
ROOMY_FACE = NormalizedBox(0.32, 0.44, 0.68, 0.66)
ROOMY_STAGE = props_mod.Stage((ROOMY_FACE,), (CAPTION_BOX.top, CAPTION_BOX.bottom))
_CARD_FILL = (150, 154, 160)  # a neutral plate where her photo would be
_CARD_EDGE = (236, 238, 240)
_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_OVERLAP_MAX = 0.02
# KRI-1618: no pixel measure tells a tuck from a plain stack. A tuck's crossing ink touches under
# 0.2% of a row, less than a plain grotesk_pen stack (0.64%), and a loopy face's plain stack
# shares up to 0.22 of its ink height with the row above (ref 3 in Turkish). So the rows' line
# boxes (the renderer's own measure) are checked against the policy: a declared tuck sits at its
# gap, any other pair no closer than the tightest plain stack (ink `_TIGHT_GAP_PX` apart).
_LINE_GAP_SLACK_PX = 2.0
_FRAME_S = 1 / 30
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


def _px(fraction: float, size: int) -> int:
    return int(round(fraction * size, 6))


def plate(
    kind: str = "light_wall",
    face: NormalizedBox = FACE_BOX,
    cards: Sequence[NormalizedBox] = (),
) -> Image.Image:
    """A wall and a head-and-shoulders silhouette inside ``face``, 1080x1920, and a neutral
    plate where each of her photo cards would be (KRI-1615)."""
    w, h = PORTRAIT.width, PORTRAIT.height
    wall, ceiling, shoulders = PLATES[kind]
    im = Image.new("RGBA", (w, h), (*wall, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, int(h * 0.05), w, int(h * 0.075)], fill=(*ceiling, 255))  # ceiling line
    draw.polygon(
        [
            (0, h),
            (0, _px(face.bottom + 0.07, h)),
            (_px(face.left + 0.01, w), _px(face.bottom + 0.01, h)),
            (_px(face.right - 0.02, w), _px(face.bottom + 0.01, h)),
            (w, _px(face.bottom + 0.07, h)),
            (w, h),
        ],
        fill=(*shoulders, 255),
    )  # shoulders
    box = [face.left * w, face.top * h, face.right * w, face.bottom * h]
    draw.ellipse(box, fill=(214, 168, 140, 255))
    label = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 34)
    for slot, card in enumerate(cards, start=1):
        rect = [card.left * w, card.top * h, card.right * w, card.bottom * h]
        draw.rectangle(rect, fill=(*_CARD_FILL, 255), outline=(*_CARD_EDGE, 255), width=4)
        centre = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        draw.text(centre, f"prop_{slot}", font=label, fill=(*_CARD_EDGE, 255), anchor="mm")
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
    cards: Sequence[NormalizedBox] = (),
) -> tuple[str, list[str], Image.Image]:
    """Draw ``rows`` over the plate (her ``cards`` drawn under them) and measure them. Returns
    (verdict, reasons, the frame)."""
    from app.pipeline import phone_subtitled_title as title_mod  # noqa: PLC0415
    from app.pipeline import text_overlay_skia as tos  # noqa: PLC0415
    from app.pipeline.phone_title_look import (  # noqa: PLC0415
        _TIGHT_GAP_PX,
        _ink_insets,
        _tuck_gaps,
    )
    from app.pipeline.title_family import retime_title_rows, title_window  # noqa: PLC0415

    reasons: list[str] = []
    for index, (row, declared) in enumerate(
        zip(rows, retime_title_rows(rows, *title_window(rows)), strict=True), start=1
    ):
        if abs(float(row["start_s"]) - declared["start_s"]) > _FRAME_S:
            reasons.append(
                f"row {index}: enters at {row['start_s']} s, it declares {declared['start_s']} s"
            )
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

    backdrop = plate(footage, face, cards)
    frame = backdrop.copy()
    inks: list[NormalizedBox] = []
    drawn: list[tuple[Image.Image, tuple[int, int, int, int]] | None] = []
    for index, row in enumerate(rows, start=1):
        drawn.append(None)
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
        drawn[-1] = (bare.getchannel("A").point(lambda p: 255 if p > 64 else 0), bbox)
        behind = backdrop.crop(bbox).convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
        lum_a, lum_b = _luminance(behind), _luminance(_rgb(scene_mod.INKS[scene.ink or ""]))
        ratio = (max(lum_a, lum_b) + 0.05) / (min(lum_a, lum_b) + 0.05)
        if ratio < _MIN_CONTRAST:
            reasons.append(f"row {index}: ink does not read on the footage ({ratio:.1f}:1)")
        reasons.extend(
            f"row {index}: {problem}"
            for problem in scene_mod.ink_box_problems(box, face=face, captions=CAPTION_BOX)
        )
    for index, (upper, lower) in enumerate(zip(drawn, drawn[1:], strict=False)):
        if upper is None or lower is None:
            continue
        (a, _top_box), (b, _bottom_box) = upper, lower
        # Real ink on ink (loopy ascenders may share a box with the row above without touching).
        touching = ImageChops.darker(a, b).histogram()[255]
        smaller = min(a.histogram()[255], b.histogram()[255]) or 1
        if touching / smaller > _OVERLAP_MAX:
            reasons.append("two rows overlap each other")
        pair = rows[index : index + 2]
        above, below = (title_mod._measure_title_box(row, canvas=PORTRAIT) for row in pair)
        height = PORTRAIT.height
        line_gap = (below.top - above.bottom) * height
        [tuck] = _tuck_gaps(pair, PORTRAIT)
        lines = f"rows {index + 1}-{index + 2} (line gap {line_gap:.0f} px"
        if tuck is not None:
            if abs(line_gap - tuck * height) > _LINE_GAP_SLACK_PX:
                reasons.append(f"{lines}, the tuck is {tuck * height:.0f}): tuck not drawn")
            continue
        insets = _ink_insets(pair[0], PORTRAIT)[1] + _ink_insets(pair[1], PORTRAIT)[0]
        tightest = _TIGHT_GAP_PX - insets * height
        if line_gap < tightest - _LINE_GAP_SLACK_PX:
            reasons.append(f"{lines}, plain stacks keep {tightest:.0f}): rows overlap undeclared")
    if cards:
        reasons.extend(card_reasons(cards, face=face, inks=inks))
    return ("FAIL" if reasons else "PASS"), reasons, frame


def card_reasons(
    cards: Sequence[NormalizedBox], *, face: NormalizedBox, inks: Sequence[NormalizedBox]
) -> list[str]:
    """KRI-1615: why her cards may not be drawn with this title, measured against the plate's
    own face, the caption row across the frame, the Kria wordmark and the title's drawn ink."""
    captions = NormalizedBox(0.0, CAPTION_BOX.top, 1.0, CAPTION_BOX.bottom)
    reasons = []
    for n, card in enumerate(cards, start=1):
        if (
            card.left < EDGE_INSET - 1e-6
            or card.top < EDGE_INSET - 1e-6
            or card.right > 1 - EDGE_INSET + 1e-6
            or card.bottom > 1 - EDGE_INSET + 1e-6
        ):
            reasons.append(f"card {n} leaves the frame {card.as_dict()}")
        for what, box in (("face", face), ("caption row", captions), ("watermark", WATERMARK_BOX)):
            if boxes_intersect(card, box):
                reasons.append(f"card {n} is on the {what}")
        if any(boxes_intersect(card, ink) for ink in inks):
            reasons.append(f"card {n} touches the title's ink")
    for (a, first), (b, second) in itertools.combinations(enumerate(cards, start=1), 2):
        if boxes_intersect(first, second):
            reasons.append(f"cards {a} and {b} overlap")
    if inks:
        title_top, title_bottom = min(i.top for i in inks), max(i.bottom for i in inks)
        middle = (face.top + face.bottom) / 2
        above = [c for c in cards if (c.top + c.bottom) / 2 < middle]
        below = [c for c in cards if (c.top + c.bottom) / 2 >= middle]
        if any(c.bottom > title_top for c in above) or any(c.top < title_bottom for c in below):
            reasons.append("the title is not between the cards")
    return reasons


def _place(rows: list[dict[str, Any]], position: str | None) -> tuple[list[dict], dict]:
    return scene_mod.place_on_typical_face(rows, position, PORTRAIT)


def approval_matches(rows: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """The approval check on what the phone is told to draw: the rows compiled to text layers the
    way the Talking recipe does (`_compile_text_lane`: capitals applied), read back as
    `verify_phone_recipe` reads layers (NFC text per baseline, `text-N` ids, `anchor_y`), then
    matched like its opening-title requirement. Reading the rows' own text instead hid the
    all-caps pairs (review round 2); tests/tasks/test_intro_scene_worker.py runs the real call."""
    import unicodedata  # noqa: PLC0415

    from app.pipeline.cloud_render_evidence import normalize_text  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import _compile_text_lane  # noqa: PLC0415
    from app.pipeline.phone_title_look import stacked_title_matches  # noqa: PLC0415
    from app.pipeline.sequence_text_evidence import text_matches  # noqa: PLC0415

    layers = _compile_text_lane(rows, user_edited=False, timeline_duration_s=30.0)
    evidence = []
    for layer in layers:
        lines: list[list[str]] = []
        baseline = None
        for run in layer.runs:
            text = unicodedata.normalize("NFC", run.text)
            if baseline is None or abs(run.baseline_y - baseline) > 0.001:
                lines.append([text])
                baseline = run.baseline_y
            else:
                lines[-1].append(text)
        evidence.append(
            {
                "element_id": layer.id,
                "text": normalize_text("\n".join("".join(line) for line in lines)),
                "start_s": layer.start,
                "end_s": layer.end,
                "anchor_y": layer.anchor_y,
            }
        )
    frame = 1 / 30
    return text_matches(evidence, title, role="opening", tolerance_s=frame) or (
        stacked_title_matches(evidence, title, tolerance_s=frame)
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
                for voices, moves in itertools.product(
                    _sweep_voices(words, thorough=thorough), ({}, {"stack": "script_over_serif"})
                ):
                    scene = scene_mod.IntroScene(
                        decision="compose",
                        voices=voices,
                        font_pair=pair,
                        ink="oxblood",
                        zone=zone,
                        entrance="fade",
                        sound="none",
                        **moves,
                    )
                    if moves and scene_mod.check_scene(scene, title, menu):
                        continue  # KRI-1618: the tuck is not issued for this pair or split
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
                                **moves,
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
    if not approval_matches(rows, title):
        verdict = "FAIL"
        reasons = [*reasons, "the approval check does not find her title in the drawn layers"]
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
    accepted = approval_matches(rows, title)
    check(
        "approval_accepts_her_title_as_a_stack",
        "1 match",
        f"{len(accepted)} match",
        len(accepted) == 1,
    )
    changed = approval_matches(rows, title + "s")
    check("approval_refuses_a_changed_word", "no match", f"{len(changed)} match", not changed)

    # Every issued pair, both languages, as drawn: the caps pairs once hid behind row text.
    refused: list[str] = []
    for lang, pair_title in PICK_TITLES:
        menu = scene_mod.issue_menu(pair_title)
        for pair in menu.font_pairs:
            pair_ask = {
                "version": pick_mod.INTRO_SCENE_VERSION,
                "seed": "pairs",
                "inks": list(scene_mod.INKS),
                "candidates": [
                    {
                        "voices": list(pick_mod.accent_splits(pair_title)[0]),
                        "font_pair": pair,
                        "entrance": menu.entrances_for(pair)[0],
                        "zones": list(scene_mod.ZONES),
                    }
                ],
            }
            pair_rows, _r, _i = pick_mod.fit_intro_scene(
                pair_ask,
                pair_title,
                canvas=PORTRAIT,
                timeline_duration_s=30.0,
                duration_s=None,
                language=pick_mod.language_of(pair_title, lang),
                place=_place,
            )
            if pair_rows is None:
                continue  # the face-fit declined this pair on the typical speaker: today's rows
            if not approval_matches(pair_rows, pair_title):
                refused.append(f"{lang}:{pair}")
    check(
        "approval_accepts_every_issued_pair_as_drawn",
        "no pair refused",
        ", ".join(refused) or "no pair refused",
        not refused,
    )

    # Nothing measured on the take: the scene takes a light ink, never a pinned dark one.
    from app.pipeline import caption_legibility as legibility  # noqa: PLC0415

    dark_first = {
        "version": pick_mod.INTRO_SCENE_VERSION,
        "seed": "u",
        "inks": ["oxblood", "white", "cream", "butter", "chartreuse"],
        "candidates": pick_mod.pick_candidates(title, seed="u"),
    }
    unmeasured, _r, _i = pick_mod.fit_intro_scene(
        dark_first,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=None,
        place=_place,
    )
    assert unmeasured is not None
    unmeasured = pick_mod.intro_ink_pass(unmeasured, [], canvas=PORTRAIT)
    luminance = legibility.relative_luminance(unmeasured[0]["color"])
    check(
        "unmeasured_take_never_keeps_a_dark_ink",
        "light ink (luminance >= 0.5)",
        f"{unmeasured[0]['color']} ({luminance:.2f})",
        luminance >= 0.5,
    )
    return out


# --- KRI-1618: the late accent and the script tucked onto the serif ---------------------------

_LATE = {"stagger": "accent_after"}
_TUCK = {
    "font_pair": "script_serif",
    "ink": "white",
    "entrance": "fade",
    "stack": "script_over_serif",
}
MOVE_CASES = (
    # (id, language, title, voices, choices, plate)
    ("late-accent", "en", PICK_TITLES[0][1], ("solid",) * 4 + ("accent",) * 2, _LATE, "light_wall"),
    ("late-accent", "tr", PICK_TITLES[1][1], ("solid",) * 6 + ("accent",) * 2, _LATE, "light_wall"),
    (
        "script-tuck",
        "en",
        "city guide: lisbon, day one",
        ("accent",) * 2 + ("solid",) * 3,
        {**_TUCK, **_LATE},
        "dark_room",
    ),
    (
        "script-tuck",
        "tr",
        "şehir rehberi: lizbon, ilk gün",
        ("accent",) * 2 + ("solid",) * 3,
        {**_TUCK, **_LATE},
        "dark_room",
    ),
)


def _move_scene(voices: tuple[str, ...], choices: dict[str, str]) -> scene_mod.IntroScene:
    base = {"font_pair": "grotesk_hand", "ink": "oxblood", "zone": "above_head", "entrance": "ink"}
    return scene_mod.IntroScene(
        decision="compose", voices=voices, sound="none", **{**base, **choices}
    )


def _layer_starts(rows: list[dict[str, Any]]) -> list[float]:
    """When the phone is told to draw each row (the Talking text lane)."""
    from app.pipeline.phone_subtitled_plan import _compile_text_lane  # noqa: PLC0415

    return [
        layer.start
        for layer in _compile_text_lane(rows, user_edited=False, timeline_duration_s=30.0)
    ]


def _still_at(rows: list[dict[str, Any]], at_s: float, footage: str) -> Image.Image:
    frame = plate(footage)
    for row in rows:
        if float(row["start_s"]) <= at_s < float(row["end_s"]):
            frame = Image.alpha_composite(frame, ov.render_overlay_frame(_overlay_for(row)))
    return frame


def _move_case(
    case_id: str,
    language: str,
    title: str,
    scene: scene_mod.IntroScene,
    rows: list[dict[str, Any]],
    receipt: dict[str, Any],
    footage: str,
    *,
    control: str | None = None,
) -> dict[str, Any]:
    verdict, reasons, frame = verify_rows(
        rows, receipt, face=FACE_BOX, scene=scene, footage=footage
    )
    if not approval_matches(rows, title):
        reasons = [*reasons, "the approval check does not find her title in the drawn layers"]
    starts = _layer_starts(rows)
    if [round(s, 3) for s in starts] != [round(float(r["start_s"]), 3) for r in rows]:
        reasons = [*reasons, f"the phone is told other entrances: {starts}"]
    base = {
        "id": case_id,
        "name": "KRI-1618 move",
        "language": language,
        "control": control,
        "expressible": "yes",
        "missing_moves": [],
        "title": title,
        "rendered": True,
        "scene": scene.model_dump(mode="json", exclude_none=True),
        "rows": _row_summary(rows),
        "entrances": starts,
    }
    return _case({**base, "verdict": "FAIL" if reasons else "PASS", "reasons": reasons}, frame)


def move_cases() -> list[dict[str, Any]]:
    """Each move in English and Turkish, compiled, placed, drawn and approved; the late accent
    also as a still before it enters (only the solid rows are up). Then controls that MUST fail:
    a tuck drawn without its declaration, a declared tuck drawn as a gap, a late line at a delay
    nobody issued, and a solid line waiting instead of the accent."""
    cases: list[dict[str, Any]] = []
    placed_by: dict[tuple[str, str], tuple] = {}
    for case_id, language, title, voices, choices, footage in MOVE_CASES:
        scene = _move_scene(voices, choices)
        out = scene_mod.compile_intro_scene(
            scene, title, canvas=PORTRAIT, timeline_duration_s=30.0, language=language
        )
        if out.rows is None:
            cases.append(
                _case(
                    {
                        "id": case_id,
                        "name": "KRI-1618 move",
                        "language": language,
                        "control": None,
                        "expressible": "yes",
                        "missing_moves": [],
                        "title": title,
                        "rendered": True,
                        "verdict": "FAIL",
                        "reasons": [f"compile declined: {out.reason}"],
                    },
                    None,
                )
            )
            continue
        rows, receipt = _place(out.rows, out.position)
        placed_by[(case_id, language)] = (title, scene, rows, receipt, footage, out)
        cases.append(_move_case(case_id, language, title, scene, rows, receipt, footage))
        early = scene_mod.STAGGERS[scene.stagger] / 2
        if early:
            up = [r for r in rows if float(r["start_s"]) <= early]
            styles = {r["source_params"]["title_look"]["style"] for r in up}
            reasons = [] if styles == {"intro_solid"} else [f"up at {early} s: {sorted(styles)}"]
            cases.append(
                _case(
                    {
                        "id": f"{case_id}@{early:.1f}s",
                        "name": "KRI-1618 move, before the accent enters",
                        "language": language,
                        "control": None,
                        "expressible": "yes",
                        "missing_moves": [],
                        "title": title,
                        "rendered": True,
                        "verdict": "FAIL" if reasons else "PASS",
                        "reasons": reasons,
                    },
                    _still_at(rows, early, footage),
                )
            )

    def stripped(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        out = json.loads(json.dumps(rows))
        for row in out:
            row["source_params"]["title_look"].pop(key, None)
        return out

    title, scene, rows, receipt, footage, out = placed_by[("script-tuck", "en")]
    undeclared = stripped(rows, "stack")
    cases.append(
        _move_case(
            "script-tuck",
            "en",
            title,
            scene,
            undeclared,
            receipt,
            footage,
            control="undeclared_overlap",
        )
    )
    gap_rows, gap_receipt = _place(stripped(out.rows, "stack"), out.position)
    redeclared = [
        {**row, "source_params": src["source_params"]}
        for row, src in zip(gap_rows, rows, strict=True)
    ]
    cases.append(
        _move_case(
            "script-tuck",
            "en",
            title,
            scene,
            redeclared,
            gap_receipt,
            footage,
            control="declared_tuck_not_drawn",
        )
    )
    title, scene, rows, receipt, footage, _out = placed_by[("late-accent", "en")]
    late = scene_mod.STAGGERS["accent_after"]
    shifted = [
        {**r, "start_s": float(r["start_s"]) + 0.3} if float(r["start_s"]) >= late else r
        for r in rows
    ]
    cases.append(
        _move_case(
            "late-accent", "en", title, scene, shifted, receipt, footage, control="unissued_delay"
        )
    )
    swapped = [{**r, "start_s": late - float(r["start_s"])} for r in rows]
    cases.append(
        _move_case(
            "late-accent", "en", title, scene, swapped, receipt, footage, control="solid_line_waits"
        )
    )
    return cases


# --- KRI-1615: her photo cards laid out with the title ----------------------------------------

# (teardown, arrangement, her photos' aspects in slot order): ref 3's collage, ref 5's 2 + 2 grid.
PROPS_CASES = (
    ("ref3", "around_speaker", (4 / 3, 3 / 4)),
    ("ref5", "grid_2x2", (3 / 4, 3 / 4, 3 / 4, 3 / 4)),
)
PROPS_PHOTO_SETS = {
    "portrait": (3 / 4,) * 4,
    "landscape": (4 / 3,) * 4,
    "square": (1.0,) * 4,
    "mixed": (4 / 3, 3 / 4, 1.0, 3 / 4),
}


def _with_props(
    example: dict[str, Any],
    language: str,
    arrangement: str,
    photos: Sequence[float],
    stage: props_mod.Stage | None = ROOMY_STAGE,
) -> tuple[scene_mod.IntroScene, scene_mod.IntroCompile]:
    scene = scene_mod.IntroScene.model_validate({**example["scene"], "arrangement": arrangement})
    out = scene_mod.compile_intro_scene(
        scene,
        example["title"],
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        language=language,
        photo_aspects=photos,
        stage=stage,
    )
    return scene, out


def _props_case(
    case_id: str,
    language: str,
    title: str,
    scene: scene_mod.IntroScene,
    out: scene_mod.IntroCompile,
    footage: str,
    *,
    cards: Sequence[NormalizedBox] | None = None,
    control: str | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": case_id,
        "name": "KRI-1615 cards",
        "language": language,
        "control": control,
        "expressible": "yes",
        "missing_moves": [],
        "title": title,
        "rendered": True,
        "scene": scene.model_dump(mode="json", exclude_none=True),
    }
    if out.props is None or out.rows is None:
        return _case(
            {**base, "verdict": "FAIL", "reasons": [f"compile declined: {out.reason}"]}, None
        )
    boxes = list(cards if cards is not None else (card.box for card in out.props.cards))
    verdict, reasons, frame = verify_rows(
        out.rows, out.props.receipt, face=ROOMY_FACE, scene=scene, footage=footage, cards=boxes
    )
    if not approval_matches(out.rows, title):
        reasons = [*reasons, "the approval check does not find her title in the drawn layers"]
    return _case(
        {
            **base,
            "verdict": "FAIL" if reasons else "PASS",
            "reasons": reasons,
            "rows": _row_summary(out.rows),
            "cards": [
                {"slot": f"prop_{n}", **box.as_dict()} for n, box in enumerate(boxes, start=1)
            ],
        },
        frame,
        _face=ROOMY_FACE,
    )


def _moved(box: NormalizedBox, *, x: float | None = None, y: float | None = None) -> NormalizedBox:
    """``box`` with its centre moved to ``x`` / ``y``, its size kept."""
    dx = 0.0 if x is None else x - (box.left + box.right) / 2
    dy = 0.0 if y is None else y - (box.top + box.bottom) / 2
    return NormalizedBox(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy)


def props_cases(teardowns: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each arrangement in English and Turkish on the roomy speaker: compiled, cards drawn as
    neutral plates under the real Skia title, measured and approved. Then controls that MUST
    fail, each one card of the English grid moved: onto the title, onto the face, past the frame
    edge, onto the caption row."""
    from app.pipeline.phone_title_look import title_block_box  # noqa: PLC0415

    cases: list[dict[str, Any]] = []
    for ref, arrangement, photos in PROPS_CASES:
        footage = teardowns[ref]["footage_plate"]
        for language in LANGUAGES:
            example = teardowns[ref]["examples"][language]
            scene, out = _with_props(example, language, arrangement, photos)
            case_id = f"{ref}+{arrangement}"
            cases.append(_props_case(case_id, language, example["title"], scene, out, footage))
    example = teardowns["ref5"]["examples"]["en"]
    scene, out = _with_props(example, "en", "grid_2x2", PROPS_CASES[1][2])
    assert out.props is not None and out.rows is not None, out.reason
    boxes = [card.box for card in out.props.cards]
    title = title_block_box(out.rows, canvas=PORTRAIT)
    face = ROOMY_FACE
    for control, index, moved in (
        ("card_over_title", 0, lambda b: _moved(b, y=(title.top + title.bottom) / 2)),
        ("card_over_face", 2, lambda b: _moved(b, y=(face.top + face.bottom) / 2)),
        ("card_bleeds_off_frame", 1, lambda b: _moved(b, x=1.0 - b.width / 4)),
        ("card_on_captions", 3, lambda b: _moved(b, y=(CAPTION_BOX.top + CAPTION_BOX.bottom) / 2)),
    ):
        mutated = [moved(box) if n == index else box for n, box in enumerate(boxes)]
        cases.append(
            _props_case(
                "ref5+grid_2x2",
                "en",
                example["title"],
                scene,
                out,
                "dark_room",
                cards=mutated,
                control=control,
            )
        )
    return cases


def props_wiring(teardowns: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The arrangement's own contract: what declines, what is not issued."""
    example = teardowns["ref5"]["examples"]["en"]
    photos = PROPS_CASES[1][2]
    _scene, close = _with_props(example, "en", "grid_2x2", photos, stage=None)
    _scene, three = _with_props(example, "en", "grid_2x2", photos[:3])
    issued = scene_mod.issue_menu(example["title"]).arrangements
    return [
        {
            "name": "cards_decline_on_a_close_speaker",
            "expected": "props_do_not_fit, no rows",
            "observed": f"{close.reason}, {'rows' if close.rows else 'no rows'}",
            "ok": close.reason == "props_do_not_fit" and close.rows is None,
        },
        {
            "name": "no_grid_from_three_photos",
            "expected": "option_not_issued",
            "observed": three.reason,
            "ok": three.reason == "option_not_issued",
        },
        {
            "name": "no_arrangement_is_issued_without_her_photos",
            "expected": "('none',)",
            "observed": str(issued),
            "ok": issued == ("none",),
        },
    ]


def sweep_props(*, thorough: bool = True) -> dict[str, Any]:
    """Every arrangement x photo shapes x issued font pair x top zone over the stress titles on
    the roomy speaker: whatever compiles must PASS the same render check, cards included; the
    rest must be declined with a reason. (The approval check runs on the cases above.)"""
    compiled = 0
    declined: dict[str, int] = {}
    failed: list[dict[str, Any]] = []
    photo_sets = list(PROPS_PHOTO_SETS.items())[: None if thorough else 2]
    zones = ("above_head", "top_left") if thorough else ("above_head",)
    for language, title in (*SWEEP_TITLES, *PICK_TITLES):
        menu = scene_mod.issue_menu(title, photo_count=4)
        words = len(scene_mod.title_words(title))
        for pair, zone, voices, arrangement, (shape, photos) in itertools.product(
            menu.font_pairs,
            zones,
            _sweep_voices(words, thorough=False),
            ("around_speaker", "grid_2x2"),
            photo_sets,
        ):
            scene = scene_mod.IntroScene(
                decision="compose",
                voices=voices,
                font_pair=pair,
                ink="cream",
                zone=zone,
                entrance="fade",
                sound="none",
                arrangement=arrangement,
            )
            out = scene_mod.compile_intro_scene(
                scene,
                title,
                canvas=PORTRAIT,
                timeline_duration_s=30.0,
                language=language,
                photo_aspects=photos,
                stage=ROOMY_STAGE,
                menu=menu,
            )
            if out.props is None or out.rows is None:
                declined[out.reason] = declined.get(out.reason, 0) + 1
                continue
            compiled += 1
            verdict, reasons, _ = verify_rows(
                out.rows,
                out.props.receipt,
                face=ROOMY_FACE,
                scene=scene,
                footage="dark_room",
                cards=[card.box for card in out.props.cards],
            )
            if reasons:
                failed.append(
                    {
                        "title": title,
                        "font_pair": pair,
                        "zone": zone,
                        "voices": "".join(v[0] for v in voices),
                        "arrangement": arrangement,
                        "photos": shape,
                        "reasons": reasons,
                    }
                )
    return {"compiled": compiled, "declined": declined, "failed": failed}


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
    cases.extend(move_cases())
    cases.extend(props_cases(teardowns))
    cases.extend(pick_cases())
    from app.cli.verify_intro_scene_model import model_cases  # noqa: PLC0415

    cases.extend(model_cases())  # KRI-1612: the replayed model arm, drawn the same way
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
            face = case.get("_face", FACE_BOX)
            fb = [
                face.left * thumb_w,
                face.top * thumb_h,
                face.right * thumb_w,
                face.bottom * thumb_h,
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
    public = [{k: v for k, v in case.items() if not k.startswith("_")} for case in cases]
    checked = [c for c in public if not c["control"] and c["rendered"]]
    blind = [c["control"] for c in public if c["control"] and c["verdict"] != "FAIL"]
    failed = [f"{c['id']}/{c['language']}" for c in checked if c["verdict"] != "PASS"]
    nothing: dict[str, Any] = {"compiled": 0, "declined": {}, "failed": []}
    sweep_report = sweep_issued_menu() if sweep else nothing
    props_sweep = sweep_props() if sweep else nothing
    from app.cli.verify_intro_scene_model import model_wiring  # noqa: PLC0415

    teardowns = {t["id"]: t for t in data["teardowns"]}
    wiring = [*wiring_controls(), *model_wiring(), *props_wiring(teardowns)]
    wiring_failed = [w["name"] for w in wiring if not w["ok"]]
    report = {
        "teardowns_version": data["version"],
        "environment": "prod-image" if os.environ.get("NOVA_IN_PROD_IMAGE") else "host",
        "skia": getattr(skia, "__version__", "unknown"),
        "overall": "PASS"
        if not failed
        and not blind
        and not sweep_report["failed"]
        and not props_sweep["failed"]
        and not wiring_failed
        else "FAIL",
        "failed": failed,
        "wiring": wiring,
        "sweep": sweep_report,
        "props_sweep": props_sweep,
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
    props = report["props_sweep"]
    print(
        f"  props sweep: {props['compiled']} scene(s) with cards rendered, "
        f"{len(props['failed'])} failed; declined {props['declined']}"
    )
    for bad in props["failed"][:10]:
        print(
            f"    FAIL {bad['arrangement']} {bad['photos']} {bad['font_pair']}/{bad['zone']} "
            f"{bad['voices']}: {bad['reasons']}"
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
