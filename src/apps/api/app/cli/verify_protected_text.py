"""E2E check that text stays off the creator's marked object (KRI-827).

    python -m app.cli.verify_protected_text [--out .protected-verify]

Builds synthetic talk-to-camera clips whose "plate" (a textured disc) follows a known path,
pins Merope T1's real request and brief ("... No text on her face or on the plate ...", as
the v76 brief section records it in `keep_text_off`) and a creator mark of the plate on an
approval binding, then runs the production decisions:
`resolve_protected_plan` (the real tracker subprocess), the caption look chooser, the
opening-title and spoken-text placement. Each placed row's REAL Skia ink (and each caption
line's ink box) is measured against the ground-truth plate every 0.1 s across the row's whole
visible interval, through the same cut and crop the recipe plays. Coverage is the ink on
the plate over the smaller of the two (ink, plate), so a small word on a big plate and a big
title over a small plate both count.

Writes ``report.json`` (per scenario, per row: interval, max coverage, what the receipt
claimed, what was measured, the mark's provenance) and ``montage.png`` (the worst frame per
row, plate outline in red). Exits 1 when any row is claimed clear while it covers the plate.

The face is supplied from ground truth (the face sampler's own detector is covered by its
tests; synthetic ellipses are not faces), so the run needs no model and no network.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

THREAD_ID = "00000000-0000-4000-8000-000000000827"
MEDIA_ID = "speaker-take"
TITLE = "Colombian dessert, Ecuadorian hands"
FPS = 30
DURATION_S = 6.0
SAMPLE_STEP_S = 0.1
MAX_COVERAGE = 0.05
FACE_SOURCE_BOX = (0.34, 0.16, 0.66, 0.40)
_CUES = [
    {"text": "Hi, I'm Karolina", "start_s": 0.0, "end_s": 1.6},
    {"text": "I'm from Ecuador", "start_s": 1.6, "end_s": 3.0},
    {"text": "this is Merope", "start_s": 3.0, "end_s": 4.5},
    {"text": "waffle cookie with blackberry jam", "start_s": 4.5, "end_s": 6.0},
]

Path = Callable[[float], tuple[float, float]]


@dataclass
class Scenario:
    name: str
    plate: Path  # source t -> plate centre (normalized source)
    radius: float = 0.2  # of the source width
    width: int = 360
    height: int = 640
    occluded: Callable[[float], bool] = lambda _t: False
    decoy: Path | None = None
    full_frame: bool = False
    keep_segments: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, DURATION_S)])
    position_x: float = 0.0
    timeout_s: float | None = None
    mark_generation: str = "gen-1"
    clip_generation: str = "gen-1"
    face_box: tuple[float, float, float, float] = FACE_SOURCE_BOX


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def scenarios() -> list[Scenario]:
    return [
        Scenario("static_plate_below_face", plate=lambda t: (0.5, 0.52)),
        Scenario(
            "plate_moves_into_band",
            plate=lambda t: (0.5, _lerp(0.70, 0.50, (t - 1.0) / 0.6)),
        ),
        Scenario(
            "fast_pass",
            plate=lambda t: (_lerp(0.22, 0.78, (t - 0.8) / 0.3), 0.52),
            radius=0.16,
        ),
        Scenario(
            "occluded_plate",
            plate=lambda t: (0.5, 0.52),
            occluded=lambda t: 0.6 <= t <= 1.4,
        ),
        Scenario(
            "decoy_plate",
            plate=lambda t: (_lerp(0.45, 0.55, t / DURATION_S), 0.52),
            decoy=lambda t: (_lerp(0.15, 0.4, t / DURATION_S), 0.80),
            radius=0.18,
        ),
        Scenario("full_frame_plate", plate=lambda t: (0.5, 0.5), full_frame=True),
        Scenario(
            "cleanup_cut",
            plate=lambda t: (0.5, 0.70 if t < 1.0 else _lerp(0.70, 0.50, (t - 1.0) / 1.0)),
            keep_segments=[(0.0, 1.0), (2.0, DURATION_S)],
        ),
        Scenario(
            "landscape_crop",
            plate=lambda t: (_lerp(0.50, 0.56, t / DURATION_S), 0.62),
            radius=0.05,
            width=640,
            height=360,
            position_x=-60.0,
            face_box=(0.42, 0.12, 0.54, 0.42),
        ),
        Scenario("tracker_timeout", plate=lambda t: (0.5, 0.52), timeout_s=0.05),
        Scenario(
            "source_swapped",
            plate=lambda t: (0.5, 0.52),
            mark_generation="gen-1",
            clip_generation="gen-2",
        ),
    ]


# --- synthetic footage -----------------------------------------------------------------------


def _plate_box(sc: Scenario, t: float) -> tuple[float, float, float, float]:
    if sc.full_frame:
        return (0.02, 0.03, 0.98, 0.97)
    cx, cy = sc.plate(t)
    rx, ry = sc.radius, sc.radius * sc.width / sc.height
    return (cx - rx, cy - ry, cx + rx, cy + ry)


def _write_clip(sc: Scenario, path: str) -> None:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    rng = np.random.default_rng(827)
    w, h = sc.width, sc.height
    background = (rng.integers(70, 110, size=(h, w, 3))).astype(np.uint8)
    background = cv2.GaussianBlur(background, (0, 0), 3)
    texture = rng.integers(0, 255, size=(h, w, 3)).astype(np.uint8)
    texture = cv2.GaussianBlur(texture, (0, 0), 1.2)
    texture[..., 2] = np.clip(texture[..., 2].astype(int) + 70, 0, 255)  # warm (red) plate
    decoy_texture = rng.integers(0, 255, size=(h, w, 3)).astype(np.uint8)
    decoy_texture[..., 0] = np.clip(decoy_texture[..., 0].astype(int) + 90, 0, 255)  # blue
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    fl, ft, fr, fb = sc.face_box
    for index in range(int(DURATION_S * FPS)):
        t = index / FPS
        frame = background.copy()
        cv2.ellipse(
            frame,
            (round((fl + fr) / 2 * w), round((ft + fb) / 2 * h)),
            (round((fr - fl) / 2 * w), round((fb - ft) / 2 * h)),
            0,
            0,
            360,
            (120, 160, 220),
            -1,
        )
        for centre, tex in (
            (sc.decoy(t) if sc.decoy else None, decoy_texture),
            (None if sc.occluded(t) else "plate", texture),
        ):
            if centre is None:
                continue
            if centre == "plate":
                left, top, right, bottom = _plate_box(sc, t)
            else:
                cx, cy = centre
                rx, ry = sc.radius, sc.radius * w / h
                left, top, right, bottom = cx - rx, cy - ry, cx + rx, cy + ry
            mask = np.zeros((h, w), np.uint8)
            if sc.full_frame and centre == "plate":
                cv2.rectangle(
                    mask, (round(left * w), round(top * h)), (round(right * w), round(bottom * h)),
                    255, -1,
                )  # fmt: skip
            else:
                cv2.ellipse(
                    mask,
                    (round((left + right) / 2 * w), round((top + bottom) / 2 * h)),
                    (round((right - left) / 2 * w), round((bottom - top) / 2 * h)),
                    0, 0, 360, 255, -1,
                )  # fmt: skip
            # The texture moves with the object (it is painted relative to its box).
            shift = np.roll(tex, (round(top * h), round(left * w)), axis=(0, 1))
            frame[mask > 0] = shift[mask > 0]
        writer.write(frame)
    writer.release()


# --- measurement ---------------------------------------------------------------------------


def _ink_mask(row: dict, canvas: Any, factor: int) -> Any:
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415
    from app.pipeline.overlay_verify import render_overlay_frame  # noqa: PLC0415

    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)],
        video_duration_s=max(float(row["end_s"]), 0.1),
        independent_box_alignment=True,
    )
    image = render_overlay_frame(overlay)
    if image.size != (canvas.width, canvas.height):
        image = image.resize((canvas.width, canvas.height))
    small = image.resize((canvas.width // factor, canvas.height // factor), Image.BILINEAR)
    return np.asarray(small)[..., 3] > 40, image


def _to_canvas(box: tuple[float, float, float, float], proj: dict, canvas: Any) -> tuple:
    """Ground truth: the engine's cover-fill + scale + shift, computed independently of the
    production helper and NOT clamped to the frame (the disc's true centre and radii)."""
    dw, dh = proj["display_width"], proj["display_height"]
    cover = max(canvas.width / dw, canvas.height / dh)
    sw, sh = dw * cover / canvas.width, dh * cover / canvas.height
    shift = proj["position_x"] / canvas.width

    def x(v: float) -> float:
        return 0.5 + (v - 0.5) * sw * proj["scale"] + shift

    def y(v: float) -> float:
        return 0.5 + (v - 0.5) * sh * proj["scale"]

    return x(box[0]), y(box[1]), x(box[2]), y(box[3])


def _plate_mask(sc: Scenario, t_out: float, proj: dict, canvas: Any, factor: int) -> Any:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from app.pipeline.object_track import output_to_source_s  # noqa: PLC0415

    w, h = canvas.width // factor, canvas.height // factor
    mask = np.zeros((h, w), np.uint8)
    t_src = output_to_source_s(t_out, sc.keep_segments)
    if t_src is None or sc.occluded(t_src):
        return mask > 0, None
    l, t, r, b = _to_canvas(_plate_box(sc, t_src), proj, canvas)  # noqa: E741
    if sc.full_frame:
        cv2.rectangle(mask, (round(l * w), round(t * h)), (round(r * w), round(b * h)), 255, -1)
    else:
        cv2.ellipse(
            mask,
            (round((l + r) / 2 * w), round((t + b) / 2 * h)),
            (max(1, round((r - l) / 2 * w)), max(1, round((b - t) / 2 * h))),
            0, 0, 360, 255, -1,
        )  # fmt: skip
    return mask > 0, (l, t, r, b)


def _times(start: float, end: float) -> list[float]:
    steps = max(1, int(math.ceil((end - start) / SAMPLE_STEP_S)))
    return [min(end - 1e-3, start + index * SAMPLE_STEP_S) for index in range(steps + 1)]


def _measure_row(sc: Scenario, row: dict, proj: dict, canvas: Any, factor: int) -> dict:
    ink, image = _ink_mask(row, canvas, factor)
    worst = (0.0, float(row["start_s"]))
    for t in _times(float(row["start_s"]), float(row["end_s"])):
        plate, _box = _plate_mask(sc, t, proj, canvas, factor)
        area = int(plate.sum())
        if not area:
            continue
        coverage = float((plate & ink).sum()) / max(1, min(area, int(ink.sum())))
        if coverage > worst[0]:
            worst = (coverage, t)
    return {"max_coverage": round(worst[0], 4), "worst_t": round(worst[1], 2), "image": image}


def _measure_captions(
    sc: Scenario, look: Any, proj: dict, canvas: Any, factor: int, style: str
) -> list[dict]:
    import numpy as np  # noqa: PLC0415

    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415
    from app.pipeline.phone_captions import (  # noqa: PLC0415
        CAPTION_FONT_FAMILY,
        caption_ink_box,
        compile_caption_layers,
    )

    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    layers = compile_caption_layers(
        [dict(c) for c in _CUES],
        canvas_width=canvas.width,
        canvas_height=canvas.height,
        style=style,
        look=look,
    )
    out = []
    for layer in layers:
        inks = [b for run in layer.runs if (b := caption_ink_box(run, typeface)) is not None]
        if not inks:
            continue
        w, h = canvas.width // factor, canvas.height // factor
        ink = np.zeros((h, w), bool)
        x0, y0 = min(b[0] for b in inks) / factor, min(b[1] for b in inks) / factor
        x1, y1 = max(b[2] for b in inks) / factor, max(b[3] for b in inks) / factor
        ink[max(0, int(y0)) : int(math.ceil(y1)), max(0, int(x0)) : int(math.ceil(x1))] = True
        worst = (0.0, float(layer.start))
        for t in _times(float(layer.start), float(layer.end)):
            plate, _box = _plate_mask(sc, t, proj, canvas, factor)
            area = int(plate.sum())
            if area:
                coverage = float((plate & ink).sum()) / max(1, min(area, int(ink.sum())))
                if coverage > worst[0]:
                    worst = (coverage, t)
        out.append(
            {
                "kind": "captions",
                "id": layer.id,
                "interval_s": [round(float(layer.start), 2), round(float(layer.end), 2)],
                "ink_box": [round(x0 * factor), round(y0 * factor), round(x1 * factor),
                            round(y1 * factor)],
                "max_coverage": round(worst[0], 4),
                "worst_t": round(worst[1], 2),
            }
        )  # fmt: skip
    return out


def _claimed(placement: dict | None) -> str:
    if not isinstance(placement, dict):
        return "clear"
    if placement.get("object_collision") is True or placement.get("status") == "error":
        return "collision"
    if placement.get("objects_verified") is False:
        return "unverified"
    return "clear"


# --- one scenario -----------------------------------------------------------------------------


def run_scenario(sc: Scenario, work_dir: str) -> dict:
    from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: PLC0415
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
    from app.kria.recipes import Canvas  # noqa: PLC0415
    from app.kria.spoken_text_ask import SpokenTextAsk  # noqa: PLC0415
    from app.pipeline.object_track import object_canvas_boxes  # noqa: PLC0415
    from app.pipeline.phone_protected_text import (  # noqa: PLC0415
        choose_caption_look,
        protected_text_receipt,
        resolve_protected_plan,
    )
    from app.pipeline.phone_recipe_shared import fit_transform  # noqa: PLC0415
    from app.pipeline.phone_spoken_text import caption_boxes, spoken_text_rows  # noqa: PLC0415
    from app.pipeline.phone_subtitled_title import (  # noqa: PLC0415
        _measure_title_box,
        choose_protected_y_frac,
        place_talking_title,
        source_box_to_canvas,
        talking_title_element,
    )
    from app.pipeline.render_geometry import (  # noqa: PLC0415
        NormalizedBox,
        ProtectedRegion,
        _translate_centered_box_to_y,
    )

    canvas = Canvas(width=1080, height=1920)
    factor = 4
    clip = os.path.join(work_dir, f"{sc.name}.mp4")
    _write_clip(sc, clip)
    fixture = os.path.join(
        os.path.dirname(__file__), "..", "..", "tests", "fixtures", "kri793_t1_caro",
        "t1_brief_and_speech.json",
    )  # fmt: skip
    with open(fixture) as handle:
        merope = json.load(handle)
    stored = CreativeBrief.model_validate(merope["brief"])
    # What the brief section (v76) records for "No text on her face or on the plate".
    keep_off = BriefRequirement(
        id="r8",
        kind="style",
        scope="global",
        description="No text on her face or on the plate",
        facts={"keep_text_off": [{"kind": "face"}, {"kind": "object", "label": "plate"}]},
    )
    brief = stored.model_copy(update={"requirements": [*stored.requirements, keep_off]})
    seed = _plate_box(sc, 0.0)
    mark = {
        "label": "plate",
        "media_id": MEDIA_ID,
        "source_t_s": 0.0,
        "box": [round(max(0.0, v), 4) if i < 2 else round(min(1.0, v), 4)
                for i, v in enumerate(seed)],
        "generation": sc.mark_generation,
    }  # fmt: skip
    binding = BriefBinding.create(
        THREAD_ID,
        brief,
        full_creator_request=merope["creator_request"],
        protected_regions=[mark],
    ).model_dump(mode="json")
    kept = sc.keep_segments
    timeline_s = sum(b - a for a, b in kept)
    plan = resolve_protected_plan(
        binding,
        media_id=MEDIA_ID,
        generation=sc.clip_generation,
        clip_path=clip,
        source_span=(kept[0][0], kept[-1][1]),
        timeout_s=sc.timeout_s,
    )
    assert plan is not None, "Merope's brief names targets"
    landscape_fit = "fill"
    scale = fit_transform(sc.width, sc.height, canvas, landscape_fit).scale
    proj = {
        "display_width": sc.width,
        "display_height": sc.height,
        "scale": scale,
        "position_x": sc.position_x,
    }
    face_canvas = source_box_to_canvas(NormalizedBox(*sc.face_box), canvas=canvas, **proj)
    faces = [face_canvas] if face_canvas is not None else []

    def face_sampler(_path: str, anchors: list[float], **_kw: Any) -> tuple[list, dict]:
        regions = [ProtectedRegion(a, a, NormalizedBox(*sc.face_box), "face") for a in anchors]
        return regions, {"decoded": len(anchors)}

    look, caption_receipt = choose_caption_look(
        _CUES,
        caption_style="sentence",
        canvas=canvas,
        objects=plan.objects,
        faces=faces,
        keep_segments=kept,
        **proj,
    )
    common = {
        "clip_path": clip,
        "keep_segments": kept,
        "display_width": sc.width,
        "display_height": sc.height,
        "canvas": canvas,
        "scale": scale,
        "position_x": sc.position_x,
        "sample": face_sampler,
    }
    protected_kwargs = (
        {"objects": plan.objects, "protect_all_faces": plan.protect_all_faces}
        if plan.objects
        else {}
    )
    title = talking_title_element(
        TITLE, duration_s=2.0, first_word_end_s=None, timeline_duration_s=timeline_s, canvas=canvas
    )
    assert title is not None
    title_window = (float(title["start_s"]), float(title["end_s"]))
    title_avoid = caption_boxes(
        _CUES, window=title_window, caption_style="sentence", canvas=canvas, look=look
    )
    placed_title, title_receipt = place_talking_title(
        title, avoid=title_avoid, **common, **protected_kwargs
    )

    def place(row: dict, avoid: Any) -> tuple[dict, dict]:
        return place_talking_title(row, avoid=avoid, **common, **protected_kwargs)

    ask = SpokenTextAsk(requirement_id="r7", text="MEROPE", triggers=("Merope",))
    spoken, spoken_receipt = spoken_text_rows(
        [ask],
        words=[{"text": "Merope", "start_s": 2.5, "end_s": 3.0}],
        cues=_CUES,
        caption_style="sentence",
        timeline_duration_s=timeline_s,
        canvas=canvas,
        place=place,
        shown_rows=[placed_title],
        caption_look=look,
    )
    spoken_placements = {
        f"spoken:{r['requirement_id']}": r.get("placement")
        for r in spoken_receipt["rows"]
        if r.get("status") == "placed"
    }
    receipt = protected_text_receipt(
        plan, {"title": title_receipt, **spoken_placements, "captions": caption_receipt}
    )

    rows: list[dict] = []
    images: list[tuple[str, Any, dict]] = []
    for kind, row, placement in (
        ("title", placed_title, title_receipt),
        *(("spoken_text", r, spoken_receipt["rows"][0].get("placement")) for r in spoken),
    ):
        measured = _measure_row(sc, row, proj, canvas, factor)
        images.append((f"{sc.name}:{kind}", measured.pop("image"), {**measured, "row": row}))
        rows.append(
            {
                "kind": kind,
                "text": row["text"],
                "interval_s": [row["start_s"], row["end_s"]],
                "y_frac": row["y_frac"],
                "size_px": row.get("size_px"),
                **measured,
                "measured": "clear" if measured["max_coverage"] <= MAX_COVERAGE else "collision",
                "claimed": _claimed(placement) if plan.objects else "unverified",
                "placement_status": (placement or {}).get("status"),
            }
        )
    for caption in _measure_captions(sc, look, proj, canvas, factor, "sentence"):
        caption["measured"] = "clear" if caption["max_coverage"] <= MAX_COVERAGE else "collision"
        caption["claimed"] = _claimed(caption_receipt) if plan.objects else "unverified"
        rows.append(caption)

    result: dict[str, Any] = {
        "name": sc.name,
        "verdict": receipt["verdict"],
        "receipt": receipt,
        "tracked": bool(plan.objects),
        "caption_look_y": getattr(look, "position_y_frac", None),
        "caption_receipt_status": caption_receipt.get("status"),
        "rows": rows,
    }
    if plan.objects:
        track = plan.objects[0]
        ious = []
        for anchor in track.anchors:
            if anchor.status != "tracked" or anchor.box is None:
                continue
            gl, gt, gr, gb = _plate_box(sc, anchor.at_s)
            ious.append(anchor.box.iou(NormalizedBox(gl, gt, gr, gb)))
        result["track_min_iou"] = round(min(ious), 3) if ious else 0.0
        result["track_status"] = track.status
    if sc.name == "plate_moves_into_band" and plan.objects:
        # F1 control: what a placement that looked only at the first frame would do.
        first, _ = object_canvas_boxes(
            plan.objects[0], window=(0.0, SAMPLE_STEP_S), keep_segments=kept, canvas=canvas, **proj
        )
        probe = _translate_centered_box_to_y(
            _measure_title_box(title, canvas=canvas), float(title["y_frac"])
        )
        chosen = choose_protected_y_frac(probe, float(title["y_frac"]), faces, first)
        control_row = {**title, "y_frac": chosen[0] if chosen else title["y_frac"]}
        measured = _measure_row(sc, control_row, proj, canvas, factor)
        measured.pop("image")
        result["first_frame_only_control"] = {
            "y_frac": control_row["y_frac"],
            **measured,
            "measured": "clear" if measured["max_coverage"] <= MAX_COVERAGE else "collision",
        }
    result["_images"] = images
    result["_clip"] = clip
    result["_proj"] = proj
    return result


# --- montage + report -------------------------------------------------------------------------


def _montage(results: list[dict], out_path: str) -> None:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from PIL import Image, ImageDraw  # noqa: PLC0415

    from app.kria.recipes import Canvas  # noqa: PLC0415
    from app.pipeline.object_track import output_to_source_s  # noqa: PLC0415

    canvas = Canvas(width=1080, height=1920)
    tile_w, tile_h = 270, 480
    tiles: list[Image.Image] = []
    sc_by_name = {sc.name: sc for sc in scenarios()}
    for result in results:
        sc = sc_by_name[result["name"]]
        proj = result["_proj"]
        for label, ink_image, info in result["_images"]:
            t_out = float(info["worst_t"])
            t_src = output_to_source_s(t_out, sc.keep_segments) or 0.0
            capture = cv2.VideoCapture(result["_clip"])
            capture.set(cv2.CAP_PROP_POS_FRAMES, round(t_src * FPS))
            ok, frame = capture.read()
            capture.release()
            base = Image.new("RGB", (canvas.width, canvas.height), (0, 0, 0))
            if ok:
                cover = max(canvas.width / sc.width, canvas.height / sc.height) * proj["scale"]
                w, h = round(sc.width * cover), round(sc.height * cover)
                src = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((w, h))
                base.paste(
                    src,
                    (
                        round((canvas.width - w) / 2 + proj["position_x"]),
                        round((canvas.height - h) / 2),
                    ),
                )
            base = Image.alpha_composite(base.convert("RGBA"), ink_image).convert("RGB")
            _mask, box = _plate_mask(sc, t_out, proj, canvas, 1)
            draw = ImageDraw.Draw(base)
            if box is not None:
                draw.ellipse(
                    [box[0] * canvas.width, box[1] * canvas.height,
                     box[2] * canvas.width, box[3] * canvas.height],
                    outline=(255, 0, 0), width=8,
                )  # fmt: skip
            tile = base.resize((tile_w, tile_h))
            ImageDraw.Draw(tile).text(
                (6, 6),
                f"{label}\nt={t_out:.1f}s cov={info['max_coverage']:.2f}",
                fill=(255, 255, 0),
            )
            tiles.append(tile)
    columns = 6
    rows = max(1, math.ceil(len(tiles) / columns))
    sheet = Image.new("RGB", (columns * tile_w, rows * tile_h), (20, 20, 20))
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * tile_w, (index // columns) * tile_h))
    sheet.save(out_path)
    _ = np


def run_scenarios(out_dir: str, names: list[str] | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="protected-verify-") as work_dir:
        for sc in scenarios():
            if names and sc.name not in names:
                continue
            results.append(run_scenario(sc, work_dir))
        _montage(results, os.path.join(out_dir, "montage.png"))
    false_success = [
        {"scenario": r["name"], "row": row.get("kind"), "id": row.get("id")}
        for r in results
        for row in r["rows"]
        if row["claimed"] == "clear" and row["measured"] == "collision"
    ]
    for r in results:
        if r["verdict"] == "clear" and any(row["measured"] == "collision" for row in r["rows"]):
            false_success.append({"scenario": r["name"], "row": "verdict"})
        for key in ("_images", "_clip", "_proj"):
            r.pop(key, None)
    report = {
        "version": 1,
        "sample_step_s": SAMPLE_STEP_S,
        "max_coverage": MAX_COVERAGE,
        "scenarios": results,
        "false_success": false_success,
    }
    with open(os.path.join(out_dir, "report.json"), "w") as handle:
        json.dump(report, handle, indent=2, default=str)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=".protected-verify")
    parser.add_argument("--scenario", action="append")
    args = parser.parse_args(argv)
    report = run_scenarios(args.out, args.scenario)
    for scenario in report["scenarios"]:
        print(
            f"{scenario['name']:<26} verdict={scenario['verdict']:<12} "
            + " ".join(f"{r['kind']}:{r['measured']}/{r['claimed']}" for r in scenario["rows"])
        )
    print(f"false success: {len(report['false_success'])}  -> {args.out}/report.json, montage.png")
    return 1 if report["false_success"] else 0


if __name__ == "__main__":
    sys.exit(main())
