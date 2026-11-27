"""CLI: how often her photo cards fit an intro look on real Talking framings (KRI-1615 stage B).

Each fixture is one saved Talking edit's evidence, read from the admin job-debug view: the title,
its language, the canvas-space ``face_band`` the worker placed the title against, and where her
captions are (the receipt's measured ``caption_boxes``, and an estimate from the caption style's
anchor and size). The run feeds that evidence to the offer's OWN code (`intro_styles.evidence_for`,
`card_stage`, `fit_card_looks`: the same candidates, zones, layout and compile) with four
synthetic photos of one shape, and counts per arrangement how many edits get a card look and why
the rest decline. The Save dry run is not part of it (it needs the pinned recipe).

For every face it also writes the room stage A leaves (`room`): above the title, under the face
above the captions, and under the captions, against the smallest card a photo shape allows, so
each decline names the row that has no room.

    python -m app.cli.intro_cards_fit --debug-dumps <dir of /admin/jobs/{id}/debug JSON>
    python -m app.cli.intro_cards_fit --fixtures faces.json --out .intro-cards-fit

``--debug-dumps`` reads saved `scripts/admin.py --prod GET /admin/jobs/{id}/debug` answers (read
only) and keeps every portrait phone Talking variant with an intro title and a saved face band.
Dumps and fixtures hold creator titles: keep them out of git (the docs get the aggregate table
only). Fixture shape: ``[{"language", "title", "face_band": [l, t, r, b],
"receipt_caption_band": [top, bottom] | null, "style_caption_band": [top, bottom] | null}]``.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("STORAGE_BUCKET", "intro-cards-fit")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import intro_styles as st  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.services import intro_props_layout as props  # noqa: E402
from app.services.intro_card_pool import CardPhoto  # noqa: E402

PHOTO_SHAPES = {"portrait_3_4": 0.75, "square": 1.0, "landscape_4_3": 1.333}
CAPTION_SOURCES = ("receipt", "receipt_or_style")
ARRANGEMENTS = ("around_speaker", "grid_2x2")


def fixtures_from_debug_dumps(directory: str) -> list[dict[str, Any]]:
    """One fixture per portrait Talking variant with an intro title and a saved face band.
    The caption style estimate is two caption lines around the style's anchor."""
    out, seen = [], set()
    for path in sorted(Path(directory).glob("*.json")):
        try:
            job = json.loads(path.read_text())["job"]
        except (ValueError, KeyError, TypeError):
            continue
        if job.get("id") in seen:
            continue
        seen.add(job.get("id"))
        language = (job.get("all_candidates") or {}).get("language")
        for variant in (job.get("assembly_plan") or {}).get("variants") or []:
            title = [
                row
                for row in variant.get("text_elements") or []
                if row.get("role") == "generative_intro"
                and not (row.get("start_s") or 0)
                and not row.get("removed")
            ]
            receipt = variant.get("opening_title_placement") or {}
            face = receipt.get("face_band")
            if (
                variant.get("resolved_archetype") != "subtitled"
                or variant.get("orientation") == "landscape"
                or not title
                or not face
            ):
                continue
            boxes = receipt.get("caption_boxes") or []
            style = variant.get("caption_editor_style") or {}
            size = variant.get("caption_size_px")
            estimate = None
            if style.get("position_y_frac") is not None and size:
                half = 1.25 * float(size) / PORTRAIT.height
                y = float(style["position_y_frac"])
                estimate = [round(max(0.0, y - half), 4), round(min(1.0, y + half), 4)]
            out.append(
                {
                    "language": variant.get("caption_language") or language or "en",
                    "title": " ".join(" ".join(row["text"] for row in title).split()),
                    "face_band": [face[k] for k in ("left", "top", "right", "bottom")],
                    "receipt_caption_band": (
                        [min(b["top"] for b in boxes), max(b["bottom"] for b in boxes)]
                        if boxes
                        else None
                    ),
                    "style_caption_band": estimate,
                }
            )
    return out


def _variant(fixture: dict[str, Any], source: str) -> dict[str, Any]:
    band = fixture.get("receipt_caption_band")
    if band is None and source == "receipt_or_style":
        band = fixture.get("style_caption_band")
    left, top, right, bottom = fixture["face_band"]
    receipt: dict[str, Any] = {
        "face_band": {"left": left, "top": top, "right": right, "bottom": bottom}
    }
    if band is not None:
        receipt["caption_boxes"] = [{"left": 0.0, "top": band[0], "right": 1.0, "bottom": band[1]}]
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "opening_title_placement": receipt,
    }


def _title_rows(title: str, language: str) -> list[dict[str, Any]]:
    from app.pipeline.phone_subtitled_title import talking_title_element  # noqa: PLC0415

    row = talking_title_element(
        title, duration_s=2.5, first_word_end_s=1.0, timeline_duration_s=30.0, canvas=PORTRAIT
    )
    return [{**row, "text_language": language}]


def _layout_reason(
    title: st.Title, stage: Any, arrangement: str, aspect: float, seed: str, language: str
) -> str:
    """The stage A layout's own decline for the first candidate that compiles alone."""
    from app.pipeline import intro_scene_pick as pick  # noqa: PLC0415

    slots = scene_mod.ARRANGEMENTS[arrangement].slots
    for candidate in pick.pick_candidates(title.words, seed=seed, recent=()):
        for zone in [z for z in candidate["zones"] if z != "mid_frame"]:
            scene = scene_mod.IntroScene(
                decision="compose",
                voices=tuple(candidate["voices"]),
                font_pair=candidate["font_pair"],
                ink="white",
                zone=zone,
                entrance="fade",
                sound="none",
            )
            alone = scene_mod.compile_intro_scene(
                scene, title.words, canvas=PORTRAIT, timeline_duration_s=30.0, language=language
            )
            if alone.rows is None:
                continue
            layout = props.layout_intro_props(
                arrangement,
                [aspect] * slots,
                alone.rows,
                stage=stage,
                canvas=PORTRAIT,
                position=alone.position,
            )
            return getattr(layout, "reason", "layout_fits_but_compile_declined")
    return "no_title_compiles_alone"


def room(fixture: dict[str, Any], stage: Any, title: st.Title, language: str) -> dict[str, float]:
    """What stage A leaves for each card row on this framing, canvas-height fractions."""
    from app.pipeline import intro_scene_pick as pick  # noqa: PLC0415
    from app.pipeline.phone_title_look import _FACE_GAP, title_block_box  # noqa: PLC0415
    from app.services.phone_card_layout import EDGE_INSET, GAP  # noqa: PLC0415

    face = stage.faces[0]
    captions = stage.caption_box
    heights = []
    for candidate in pick.pick_candidates(title.words, seed="room", recent=())[:3]:
        scene = scene_mod.IntroScene(
            decision="compose",
            voices=tuple(candidate["voices"]),
            font_pair=candidate["font_pair"],
            ink="white",
            zone="above_head",
            entrance="fade",
            sound="none",
        )
        alone = scene_mod.compile_intro_scene(
            scene, title.words, canvas=PORTRAIT, timeline_duration_s=30.0, language=language
        )
        if alone.rows is not None:
            placed, _receipt = scene_mod.place_on_stage(
                alone.rows, alone.position, PORTRAIT, faces=stage.faces, avoid=[captions]
            )
            heights.append(title_block_box(placed, canvas=PORTRAIT).height)
    title_height = min(heights) if heights else float("nan")
    return {
        "face_top": round(face.top, 3),
        "face_bottom": round(face.bottom, 3),
        "caption_band": [round(captions.top, 3), round(captions.bottom, 3)],
        "smallest_title_height": round(title_height, 3),
        "above_title": round(face.top - _FACE_GAP - title_height - GAP - EDGE_INSET, 3),
        "under_face_above_captions": round(captions.top - GAP - (face.bottom + GAP), 3),
        "under_captions": round(1 - EDGE_INSET - (captions.bottom + GAP), 3),
    }


def min_card_height(aspect: float) -> float:
    """The shortest card a photo of ``aspect`` can be (its short side `MIN_WIDTH` of the width)."""
    from app.services.phone_card_layout import CANVAS_RATIO, MIN_WIDTH  # noqa: PLC0415

    return round(max(MIN_WIDTH / aspect, MIN_WIDTH) * CANVAS_RATIO, 3)


def run(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    seen: set[tuple] = set()
    cases: list[dict[str, Any]] = []
    for fixture in fixtures:
        face_key = tuple(round(x, 3) for x in fixture["face_band"])
        title = st.read_title_rows(_title_rows(fixture["title"], fixture["language"]), "talking")
        for source in CAPTION_SOURCES:
            variant = _variant(fixture, source)
            evidence = st.evidence_for("talking", variant, {}, title, PORTRAIT)
            stage = st.card_stage(variant, {}, title, evidence)
            case: dict[str, Any] = {
                "language": fixture["language"],
                "caption_source": source,
                "distinct_face": face_key not in seen,
                "captions_measured": stage is not None and stage.caption_band is not None,
                "room": room(fixture, stage, title, fixture["language"]) if stage else None,
                "fits": {},
                "declines": {},
            }
            for shape, aspect in PHOTO_SHAPES.items():
                photos = [
                    CardPhoto(str(uuid.UUID(int=n + 1)), f"pool/{n}.jpg", "1", aspect)
                    for n in range(4)
                ]
                seed = st.offer_seed("fit", "subtitled", title.words)
                declines: dict[str, int] = {}
                looks = st.fit_card_looks(
                    title,
                    route="talking",
                    variant=variant,
                    assembly={},
                    evidence=evidence,
                    photos=photos,
                    canvas=PORTRAIT,
                    seed=seed,
                    offer="fit",
                    language=fixture["language"],
                    excluded_fonts=(),
                    timeline_duration_s=30.0,
                    declines=declines,
                )
                fitted = {look.look.key.split("+")[1] for look in looks}
                case["fits"][shape] = sorted(fitted)
                case["declines"][shape] = {
                    arrangement: _layout_reason(
                        title, stage, arrangement, aspect, seed, fixture["language"]
                    )
                    for arrangement in ARRANGEMENTS
                    if arrangement not in fitted and stage is not None
                }
            cases.append(case)
        seen.add(face_key)
    return {"cases": cases, "summary": summarize(cases)}


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source in CAPTION_SOURCES:
        rows = [c for c in cases if c["caption_source"] == source]
        for shape in PHOTO_SHAPES:
            for arrangement in ARRANGEMENTS:
                reasons = collections.Counter(
                    c["declines"][shape].get(arrangement, "fits") for c in rows
                )
                out[f"{source}/{shape}/{arrangement}"] = {
                    "edits": len(rows),
                    "distinct_faces": sum(c["distinct_face"] for c in rows),
                    "captions_measured": sum(c["captions_measured"] for c in rows),
                    "fit": sum(arrangement in c["fits"][shape] for c in rows),
                    "declines": dict(reasons.most_common()),
                }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intro_cards_fit", description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixtures")
    source.add_argument("--debug-dumps")
    parser.add_argument("--out", default=".intro-cards-fit")
    args = parser.parse_args(argv)
    fixtures = (
        json.loads(Path(args.fixtures).read_text())
        if args.fixtures
        else fixtures_from_debug_dumps(args.debug_dumps)
    )
    report = run(fixtures)
    report["min_card_height"] = {s: min_card_height(a) for s, a in PHOTO_SHAPES.items()}
    os.makedirs(args.out, exist_ok=True)
    Path(args.out, "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("smallest card height by photo shape:", report["min_card_height"])
    for key, item in report["summary"].items():
        print(
            f"  {key}: {item['fit']}/{item['edits']} fit "
            f"({item['distinct_faces']} distinct faces, {item['captions_measured']} with "
            f"captions) declines {item['declines']}"
        )
    for case in report["cases"]:
        if case["distinct_face"] and case["room"]:
            print(f"  room ({case['caption_source']}): {case['room']}")
    print(f"  report: {os.path.join(args.out, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
