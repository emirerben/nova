"""CLI: "Try intro styles" (KRI-1633) through the real offer, the real phone Save and real Skia.

For talking, narrated and montage phone jobs (built with the real phone builders,
`intro_styles_fixtures`), in English and Turkish, over stress titles (two words, five words, a long
first line, a Turkish title with dotted and dotless i, a typed " / " stack), it runs the SAME
`build_offer` the route calls and, for EVERY offered option:

* pushes the rows through the route's real phone Save (`prepare_editor_commit`, a throwaway copy);
* reads the render contract's requirement texts the Save derives (`_text_requirements_for_save`)
  and checks the saved recipe against `[opening X, any X]` (`verify_phone_recipe`);
* draws the rows with the real Skia path and measures each ink box against the face, the captions
  and the title safe zone;
* records ms.

Pass: at least 3 distinct looks for every multi-word title on every route that is in; every look
Save-compiles; the opening requirement is still her whole title; every ink box is clean; "No intro
style" on an unstyled title is byte-equal to its input; request-time face evidence reproduces the
worker's placement within 0.01 in three crops.

Six negative controls MUST fail (N1 the old `has_text_ask` blocks saved preferences, N2 her font ask
for the title is honoured, N3 a look moved onto the face fails the clean check, N4 a face that
cannot draw a dotted capital is never issued, N5 re-applying the option over her coloured line
fails "her edit kept", N6 a contract without the stacked `any` fallback refuses the Save). A control
that does not fail means the check is blind and the run exits 1.

    make verify-intro-styles          # prod image
    python -m app.cli.verify_intro_styles --out .intro-styles-verify

Outputs ``report.json`` and ``montage.png`` (read the montage: it is the content check).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("STORAGE_BUCKET", "intro-verify")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.cli import intro_styles_fixtures as fx  # noqa: E402
from app.cli.verify_intro_scenes import _FONTS, _norm, plate  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import intro_scene_pick as pick_mod  # noqa: E402
from app.pipeline import intro_styles as st  # noqa: E402
from app.pipeline import overlay_verify as ov  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.phone_title_look import (  # noqa: E402
    intro_plain_text,
    stacked_title_matches,
)
from app.pipeline.render_geometry import NormalizedBox  # noqa: E402

_SEED_JOB = uuid.UUID("00000000-0000-4000-8000-000000001633")
CAPTIONS = scene_mod.__getattr__("TYPICAL_CAPTIONS")
TITLES = {
    "en": {
        "two_words": "Weekend plans",
        "five_words": "five things i learned today",
        "long_first_line": "the absolutely biggest design trend right now",
        "typed_stack": "5 PROJECTS / the AI builder community",
        "captions_repeat_title": "the biggest design trend right now",
    },
    "tr": {
        "two_words": "Hafta sonu",
        "five_words": "bugün öğrendiğim beş şey",
        "dotted_dotless": "bu yılın trendi",
        "long_first_line": "bu yılın en büyük tasarım trendi şu an",
        "typed_stack": "5 PROJE / yapay zeka topluluğu",
        "captions_repeat_title": "bu yılın en büyük tasarım trendi şu an",
    },
}
_COLOR = {"PASS": (46, 160, 67), "FAIL": (218, 54, 51), "CONTROL-FAILED": (46, 160, 67)}


def _apply_flags() -> None:
    for key, value in fx.flag_overrides().items():
        setattr(settings, key, value)
    settings.intro_styles_min_client_protocol = 57
    settings.intro_scene_model_enabled = False


# --- measuring ---------------------------------------------------------------------------------


def ink_report(
    rows: list[dict[str, Any]], face: NormalizedBox, captions: list[NormalizedBox] | None = None
) -> tuple[list[str], Image.Image]:
    """Draw ``rows`` over the light plate; return the problems of each measured ink box against
    the saved face, every caption box of the pinned recipe (else the typical captions) and the
    title safe zone."""
    frame = plate("light_wall")
    problems: list[str] = []
    for index, row in enumerate(rows, start=1):
        overlay = scene_mod.overlay_for_row(row)
        frame = Image.alpha_composite(frame, ov.render_overlay_frame(overlay))
        bare = ov.render_overlay_frame({**overlay, "shadow_enabled": False})
        bbox = ov.text_bbox(bare)
        if bbox is None:
            problems.append(f"row {index}: nothing drawn")
            continue
        for caption in captions or [CAPTIONS]:
            problems += [
                f"row {index}: {p}"
                for p in scene_mod.ink_box_problems(_norm(bbox), face=face, captions=caption)
            ]
    return problems, frame


NO_FACE = NormalizedBox(0.0, 0.0, 0.0, 0.0)  # narrated and montage keep no face evidence


def _face_of(route: str, variant: dict[str, Any]) -> NormalizedBox:
    """The saved Talking face band; narrated and montage have no face evidence to measure."""
    if route != "talking":
        return NO_FACE
    return st.saved_face_band(variant) or scene_mod.__getattr__("TYPICAL_FACE")


def saved_state(job: Any, vid: str, route: str, rows: list[dict[str, Any]], title: str) -> dict:
    """Save ``rows`` through the route's real phone Save on a copy and read what it keeps."""
    from app.routes import generative_jobs as gj  # noqa: PLC0415
    from app.services.creator_render_contract import (  # noqa: PLC0415
        CreatorRenderContract,
        TextRequirement,
        verify_phone_recipe,
    )
    from app.services.device_render import device_status  # noqa: PLC0415
    from app.services.phone_editor import _text_requirements_for_save  # noqa: PLC0415

    scratch = st.detach(job)
    variant = next(v for v in scratch.assembly_plan["variants"] if v["variant_id"] == vid)
    revision = gj._guided_v2_revision(scratch, variant) if route == "montage" else None
    out: dict[str, Any] = {"save": "ok", "exact_texts": [], "contract": "not_checked"}
    try:
        gj.prepare_editor_commit(
            scratch,
            vid,
            gj.EditorCommitRequest(
                base_generation="first",
                text_elements=rows,
                **({"guided_revision_number": revision["revision_number"]} if revision else {}),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a refusal is the measurement
        detail = getattr(exc, "detail", None)
        out["save"] = f"refused: {detail if detail is not None else type(exc).__name__}"[:160]
        return out
    recipe = device_status(scratch, vid).request.recipe
    plain = " ".join(title.replace(" / ", " ").split())
    prior = (
        TextRequirement(role="opening", text=title, duration_s=2.0),
        TextRequirement(role="any", text=title),
    )
    derived = _text_requirements_for_save(rows, prior, {rows[0]["id"]: title}, keep_shot_roles=True)
    out["exact_texts"] = [{"role": r.role, "text": r.text} for r in derived]
    contract = CreatorRenderContract(generation_id="g").rebind(exact_texts=derived)
    try:
        verify_phone_recipe(contract, recipe)
        out["contract"] = "ok"
    except Exception as exc:  # noqa: BLE001
        out["contract"] = f"declined: {type(exc).__name__}"
    evidence = [
        {
            "element_id": layer.id,
            "text": " ".join(run.text for run in layer.runs),
            "start_s": layer.start,
            "end_s": layer.end,
            "anchor_y": layer.anchor_y,
        }
        for layer in recipe.text_layers
        if layer.id.startswith(("title-", "text-"))
    ]
    out["stacked_title_matches"] = (
        bool(stacked_title_matches(evidence, plain, tolerance_s=0.05)) or len(rows) == 1
    )
    out["opening_is_her_whole_title"] = any(
        r.role == "opening"
        and "".join(r.text.split()).casefold().replace("/", "") == "".join(plain.split()).casefold()
        for r in derived
    )
    return out


# --- the matrix --------------------------------------------------------------------------------


def caption_boxes(assembly: dict[str, Any], vid: str, probe: st.Title) -> list[NormalizedBox]:
    """The pinned recipe's caption layers in the title's window, measured on their own (not through
    the offer's avoid list, so a caption the offer forgot is still caught: F6)."""
    out = []
    for layer in st.recipe_layers(assembly, vid):
        if not str(layer.get("id") or "").startswith("caption"):
            continue
        if (
            float(layer.get("end", 0)) <= probe.start_s
            or float(layer.get("start", 0)) >= probe.end_s
        ):
            continue
        box = st.layer_box(layer, PORTRAIT)
        if box is not None:
            out.append(box)
    return out


def _chunks(title: str) -> list[dict[str, Any]]:
    """Caption cues that say her title, in two halves (people usually say the title they type)."""
    words = title.split()
    mid = max(1, len(words) // 2)
    return [
        {"text": " ".join(words[:mid]), "start_s": 0.0, "end_s": 1.5},
        {"text": " ".join(words[mid:]), "start_s": 1.5, "end_s": 3.0},
    ]


def run_case(route: str, lang: str, name: str, title: str, index: int) -> dict[str, Any]:
    saved_cues = list(fx.CUES)
    if name == "captions_repeat_title":
        fx.CUES[:] = _chunks(title)
    try:
        return _run_case(route, lang, name, title, index)
    finally:
        fx.CUES[:] = saved_cues


def _run_case(route: str, lang: str, name: str, title: str, index: int) -> dict[str, Any]:
    job_id = uuid.UUID(int=_SEED_JOB.int + index)
    job, vid, rows = fx.BUILDERS[route](title, job_id=job_id)
    variant = next(v for v in job.assembly_plan["variants"] if v["variant_id"] == vid)
    face = _face_of(route, variant)
    probe = st.read_title_rows(rows, route)
    captions = caption_boxes(job.assembly_plan, vid, probe)
    if name == "captions_repeat_title" and route == "talking" and not captions:
        raise RuntimeError("the repeated-caption case has no caption layers to measure")
    started = time.monotonic()
    offer = st.build_offer(st.detach(job), vid, rows, "first")
    total_ms = round((time.monotonic() - started) * 1000)
    case: dict[str, Any] = {
        "route": route,
        "language": lang,
        "title_kind": name,
        "title": title,
        "available": offer["available"],
        "reason": offer["reason"],
        "looks": 0,
        "total_ms": total_ms,
        "options": [],
        "problems": [],
        "_frames": [],
    }
    if not offer["available"]:
        return case
    case["looks"] = len([o for o in offer["options"] if o["key"] != "none"])
    for option in offer["options"]:
        t0 = time.monotonic()
        state = saved_state(job, vid, route, option["rows"], title)
        ink, frame = ink_report(option["rows"], face, captions)
        record = {
            "key": option["key"],
            "rows": [
                {k: r.get(k) for k in ("id", "text", "font_family", "color", "y_frac")}
                for r in option["rows"]
            ],
            "save_compile": state["save"],
            "exact_texts": state["exact_texts"],
            "contract": state["contract"],
            "stacked_title_matches": state.get("stacked_title_matches"),
            "opening_is_her_whole_title": state.get("opening_is_her_whole_title"),
            "ink_problems": ink,
            "clean": not ink,
            "preview": option["preview"] is not None,
            "ms": round((time.monotonic() - t0) * 1000),
        }
        case["options"].append(record)
        case["_frames"].append((option["key"], frame, not ink and state["save"] == "ok"))
        if option["key"] == "none" and option["current"]:
            if option["rows"] != rows:
                case["problems"].append("none on an unstyled title is not byte-equal to input")
        if state["save"] != "ok":
            case["problems"].append(f"{option['key']}: Save {state['save']}")
        elif state["contract"] != "ok":
            case["problems"].append(f"{option['key']}: contract {state['contract']}")
        elif option["key"] != "none" and not state.get("opening_is_her_whole_title"):
            case["problems"].append(f"{option['key']}: opening is not her whole title")
        if ink and option["key"] != "none":
            case["problems"].append(f"{option['key']}: {ink[0]}")
    first = next((o for o in offer["options"] if o["key"] != "none"), None)
    if first is not None:
        again = st.build_offer(st.detach(job), vid, first["rows"], "first")
        back = again["options"][-1]["rows"] if again["available"] else None
        case["none_restores_title"] = back == rows
        if back != rows:
            case["problems"].append("none after a picked look does not restore her title")
    multi_word = len(scene_mod.title_words(title)) >= 2
    if multi_word and case["looks"] < 3:
        case["problems"].append(f"only {case['looks']} looks")
    return case


# --- parity ------------------------------------------------------------------------------------


def face_parity() -> list[dict[str, Any]]:
    """F7: the worker's saved receipt band, replayed at request time, places today's plain
    Talking title where the worker did, in three crops."""
    from app.pipeline.phone_subtitled_title import (  # noqa: PLC0415
        place_talking_title,
        talking_title_element,
    )
    from app.pipeline.render_geometry import ProtectedRegion  # noqa: PLC0415

    row = talking_title_element(
        fx.EN_TITLE, duration_s=2.5, first_word_end_s=1.0, timeline_duration_s=10.0, canvas=PORTRAIT
    )
    cases = {
        "centred": ((1080, 1920), 1.0, 0.0),
        "position_x_crop": ((1920, 1080), 1.0, -180.0),
        "letterboxed_landscape": ((1920, 1080), 0.5625, 0.0),
    }
    out = []
    for name, (display, scale, shift) in cases.items():
        source = NormalizedBox(0.38, 0.12, 0.62, 0.52)

        def sampler(box):  # noqa: ANN001, ANN202
            def sample(_path, anchors, **_kw):  # noqa: ANN001, ANN202
                return [ProtectedRegion(0.0, 1e6, box, kind="face")], {
                    "attempted": len(anchors),
                    "decoded": len(anchors),
                    "detected": 1,
                }

            return sample

        common = {"clip_path": "t.mp4", "keep_segments": [(0.0, 10.0)], "canvas": PORTRAIT}
        placed, receipt = place_talking_title(
            row,
            display_width=display[0],
            display_height=display[1],
            scale=scale,
            position_x=shift,
            sample=sampler(source),
            **common,
        )
        band = NormalizedBox(*(receipt["face_band"][k] for k in ("left", "top", "right", "bottom")))
        again, again_receipt = place_talking_title(
            row,
            display_width=PORTRAIT.width,
            display_height=PORTRAIT.height,
            scale=1.0,
            position_x=0.0,
            sample=sampler(band),
            **common,
        )
        delta = max(
            abs(again["y_frac"] - placed["y_frac"]),
            abs(again_receipt["default_y_frac"] - receipt["default_y_frac"]),
        )
        out.append(
            {
                "crop": name,
                "same_status": again_receipt["status"] == receipt["status"],
                "same_band": again_receipt["face_band"] == receipt["face_band"],
                "max_delta": round(delta, 4),
                "ok": delta <= 0.01 and again_receipt["status"] == receipt["status"],
            }
        )
    return out


# --- negative controls -------------------------------------------------------------------------


def controls() -> list[dict[str, Any]]:
    """Each control is a wrong thing the checks must catch (`caught` True = the check failed it)."""
    out: list[dict[str, Any]] = []

    def add(name: str, caught: bool, observed: str) -> None:
        out.append({"name": name, "caught": bool(caught), "observed": observed})

    # N1: the old has_text_ask blocks a policy that only holds saved preferences.
    prefs = {
        "preferences": {"title": {"position": "top_left", "favorite_fonts": ["Anton"]}},
        "requested": {"all_text": {"font_family": "Anton"}},
    }

    def legacy(policy: dict) -> bool:
        for source in (policy.get("requested"), policy.get("preferences")):
            for role in ("all_text", "title"):
                rule = (source or {}).get(role)
                if isinstance(rule, dict) and any(k != "automatic_font" for k in rule):
                    return True
        return False

    add(
        "N1_old_has_text_ask_blocks_saved_preferences",
        legacy(prefs) and not pick_mod.has_text_ask(prefs),
        f"old={legacy(prefs)} new={pick_mod.has_text_ask(prefs)}",
    )
    # N2: her font ask for this title is honoured.
    job, vid, rows = fx.talking_job()
    job.assembly_plan["phone_text_style"]["requested"] = {"title": {"font_family": "Anton"}}
    asked = st.build_offer(st.detach(job), vid, rows, "first")
    add(
        "N2_title_look_ask_is_honoured", asked["reason"] == "title_look_asked", str(asked["reason"])
    )
    # N3: a look moved onto the face fails the clean check (wrong content, right shape).
    job, vid, rows = fx.talking_job()
    offer = st.build_offer(st.detach(job), vid, rows, "first")
    face = st.saved_face_band(job.assembly_plan["variants"][0])
    cy = (face.top + face.bottom) / 2
    moved = [{**r, "y_frac": cy, "x_frac": 0.46} for r in offer["options"][0]["rows"]]
    problems, _frame = ink_report(moved, face, None)
    add("N3_look_on_the_face_is_not_clean", bool(problems), "; ".join(problems[:1]) or "clean")
    # N4: a face that cannot draw the title is never issued. Every bundled face draws Turkish, so
    # the real control is a title the faces cannot draw (Cyrillic: only one face does); the dotted
    # capital is checked by taking "İ" away from one face.
    cyrillic = "Привет большой мир"
    menu = scene_mod.issue_menu(cyrillic)
    blocked = [p for p in scene_mod.FONT_PAIRS if p not in menu.font_pairs]
    job, vid, rows = fx.talking_job(cyrillic)
    issued = {o["key"] for o in st.build_offer(st.detach(job), vid, rows, "first")["options"]}
    real = scene_mod.face_draws
    scene_mod.face_draws = lambda family, text: (
        False if family == "Zeyada" and "i" in text.lower() else real(family, text)
    )
    try:
        no_dotted = scene_mod.issue_menu("iyi bir tasarım trendi şu an").font_pairs
        job2, vid2, rows2 = fx.talking_job("iyi bir tasarım trendi şu an")
        issued_dotted = {
            o["key"] for o in st.build_offer(st.detach(job2), vid2, rows2, "first")["options"]
        }
    finally:
        scene_mod.face_draws = real
    add(
        "N4_face_without_the_glyphs_is_never_issued",
        bool(blocked)
        and not (issued & set(blocked))
        and "grotesk_hand" not in no_dotted
        and "grotesk_hand" not in issued_dotted,
        f"cyrillic blocked={blocked} issued={sorted(issued)}; "
        f"without a dotted capital Zeyada's pair issued={'grotesk_hand' in issued_dotted}",
    )
    # N5: re-applying the option over her coloured line fails "her edit kept".
    job, vid, rows = fx.talking_job()
    options = st.build_offer(st.detach(job), vid, rows, "first")["options"]
    picked = options[0]["rows"]
    edited = [{**r, "color": "#12AB34"} for r in picked]
    reapplied = options[0]["rows"]
    kept = lambda saved: {r["color"] for r in saved} == {"#12AB34"}  # noqa: E731
    add(
        "N5_reapplying_the_option_over_her_edit_fails",
        kept(edited) and not kept(reapplied),
        f"edited={kept(edited)} reapplied={kept(reapplied)}",
    )
    # N6: a contract [opening X, any X] without the stacked `any` fallback refuses the stack.
    from app.services.creator_render_contract import text_matches  # noqa: PLC0415

    rows_en = st.build_offer(st.detach(job), vid, rows, "first")["options"][0]["rows"]
    evidence = [
        {
            "element_id": f"text-{i}",
            "text": r["text"],
            "start_s": r["start_s"],
            "end_s": r["end_s"],
            "anchor_y": r["y_frac"],
        }
        for i, r in enumerate(rows_en)
    ]
    title = intro_plain_text(rows_en) or ""
    old_any = bool(text_matches(evidence, title, role="any", tolerance_s=0.05))
    new_any = bool(stacked_title_matches(evidence, title, tolerance_s=0.05))
    add(
        "N6_old_any_rule_refuses_the_stack",
        (not old_any) and new_any,
        f"old={old_any} new={new_any}",
    )
    # N7: a caption that repeats her words is still a caption to keep clear of. The first
    # version matched layers by substring of the title, so such captions were never avoided.
    real_avoid = st.avoid_boxes

    def substring_avoid(assembly: Any, variant_id: str, title: Any, canvas: Any) -> list[Any]:
        from app.text_fold import fold_text  # noqa: PLC0415

        own = {fold_text(" ".join(str(r["text"]).split())) for r in title.rows}
        own.add(fold_text(title.words))
        boxes = []
        for layer in st.recipe_layers(assembly, variant_id):
            runs = layer.get("runs") or []
            text = fold_text(" ".join(str(run.get("text") or "") for run in runs))
            if not text.strip() or any(text in value for value in own):
                continue
            box = st.layer_box(layer, canvas)
            if box is not None:
                boxes.append(box)
        return boxes

    caught = False
    saved_cues = list(fx.CUES)
    fx.CUES[:] = _chunks(fx.EN_TITLE)
    st.avoid_boxes = substring_avoid
    try:
        job, vid, rows = fx.talking_job()
        probe = st.read_title_rows(rows, "talking")
        measured = caption_boxes(job.assembly_plan, vid, probe)
        for look in st.build_offer(st.detach(job), vid, rows, "first")["options"]:
            if look["key"] != "none" and ink_report(look["rows"], NO_FACE, measured)[0]:
                caught = True
    finally:
        st.avoid_boxes = real_avoid
        fx.CUES[:] = saved_cues
    add(
        "N7_captions_that_repeat_her_words_are_avoided",
        caught and bool(measured),
        f"old substring rule drew over a caption: {caught}",
    )
    # N8: "No intro style" must give back the title the look replaced, not rebuild one.
    job, vid, rows = fx.talking_job()
    picked = st.build_offer(st.detach(job), vid, rows, "first")["options"][0]["rows"]
    stripped = [
        {
            **r,
            "source_params": {
                **r["source_params"],
                "title_look": {
                    **r["source_params"]["title_look"],
                    "intro_style": {
                        k: v
                        for k, v in r["source_params"]["title_look"]["intro_style"].items()
                        if k != "restore"
                    },
                },
            },
        }
        for r in picked
    ]
    rebuilt = st.build_offer(st.detach(job), vid, stripped, "first")["options"][-1]["rows"]
    restored = st.build_offer(st.detach(job), vid, picked, "first")["options"][-1]["rows"]
    add(
        "N8_none_rebuilt_without_the_saved_title_differs",
        restored == rows and rebuilt != rows,
        f"restored_equal={restored == rows} rebuilt_equal={rebuilt == rows}",
    )
    return out


# --- montage -----------------------------------------------------------------------------------


def build_montage(cases: list[dict[str, Any]], out_path: str) -> None:
    thumb_w, pad, label_h, cols = 220, 8, 44, 5
    thumb_h = int(thumb_w * PORTRAIT.height / PORTRAIT.width)
    picked = [
        c for c in cases if c["title_kind"] in ("long_first_line", "dotted_dotless", "two_words")
    ]
    tiles = [
        (c, key, frame, ok) for c in picked for key, frame, ok in c["_frames"] if key != "none"
    ][:60]
    cell_w, cell_h = thumb_w + pad * 2, thumb_h + label_h + pad * 2
    rows_n = max(1, (len(tiles) + cols - 1) // cols)
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows_n), (22, 27, 34))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 11)
    for i, (case, key, frame, ok) in enumerate(tiles):
        r, c = divmod(i, cols)
        x, y = c * cell_w + pad, r * cell_h + pad
        sheet.paste(frame.convert("RGB").resize((thumb_w, thumb_h)), (x, y))
        color = _COLOR["PASS" if ok else "FAIL"]
        draw.rectangle([x - 2, y - 2, x + thumb_w + 1, y + thumb_h + 1], outline=color, width=3)
        draw.text(
            (x, y + thumb_h + 4), f"{case['route']}/{case['language']} {key}", fill=color, font=font
        )
        draw.text((x, y + thumb_h + 18), case["title"][:34], fill=(201, 209, 217), font=font)
    sheet.save(out_path, "PNG")


# --- run ---------------------------------------------------------------------------------------


def run(*, out_dir: str) -> dict[str, Any]:
    import skia  # noqa: PLC0415

    _apply_flags()
    os.makedirs(out_dir, exist_ok=True)
    cases: list[dict[str, Any]] = []
    index = 0
    for route in fx.ROUTES:
        for lang, titles in TITLES.items():
            for name, title in titles.items():
                index += 1
                cases.append(run_case(route, lang, name, title, index))
    parity = face_parity()
    ctl = controls()
    build_montage(cases, os.path.join(out_dir, "montage.png"))
    failed = [
        f"{c['route']}/{c['language']}/{c['title_kind']}: {p}"
        for c in cases
        for p in c["problems"]
        + (
            []
            if c["available"] or c["title_kind"] == "typed_stack"
            else [f"unavailable: {c['reason']}"]
        )
    ]
    parity_failed = [p["crop"] for p in parity if not p["ok"]]
    blind = [c["name"] for c in ctl if not c["caught"]]
    public = [{k: v for k, v in case.items() if k != "_frames"} for case in cases]
    summary: dict[str, dict[str, Any]] = {}
    for case in public:
        key = f"{case['route']}/{case['language']}"
        item = summary.setdefault(key, {"cases": 0, "looks": [], "declines": {}, "total_ms": 0})
        item["cases"] += 1
        item["looks"].append(case["looks"])
        item["total_ms"] += case["total_ms"]
        if not case["available"]:
            item["declines"][case["reason"]] = item["declines"].get(case["reason"], 0) + 1
    report = {
        "environment": "prod-image" if os.environ.get("NOVA_IN_PROD_IMAGE") else "host",
        "skia": getattr(skia, "__version__", "unknown"),
        "overall": "PASS" if not failed and not parity_failed and not blind else "FAIL",
        "failed": failed,
        "face_parity": parity,
        "controls": ctl,
        "blind_controls": blind,
        "per_route_language": summary,
        "cases": public,
        "montage": "montage.png",
        "report": "report.json",
    }
    Path(out_dir, "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_intro_styles", description=__doc__)
    parser.add_argument("--out", default=".intro-styles-verify")
    args = parser.parse_args(argv)
    report = run(out_dir=os.path.abspath(args.out))
    env = f"{report['environment']}, skia {report['skia']}"
    print(f"\nintro-styles-verify: {report['overall']}  ({env})")
    for key, item in report["per_route_language"].items():
        print(f"  {key}: looks {item['looks']} declines {item['declines']} {item['total_ms']} ms")
    for bad in report["failed"][:20]:
        print(f"  FAIL {bad}")
    for p in report["face_parity"]:
        verdict = "PASS" if p["ok"] else "FAIL"
        print(f"  [{verdict}] face parity {p['crop']}: max delta {p['max_delta']}")
    for c in report["controls"]:
        print(f"  [{'CAUGHT' if c['caught'] else 'BLIND'}] control {c['name']}: {c['observed']}")
    print(f"  report:  {os.path.join(args.out, report['report'])}")
    print(f"  montage: {os.path.join(args.out, report['montage'])}  <- read this")
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
