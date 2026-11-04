"""Qendresa item 15: her Q1 screenshot and recording cards through the real phone Talking writer.

Qendresa (@qbuilder) talks at her desk while screenshots and screen recordings pop in (kit
`_src/measure/sheets/V4_*`, `V7_*`; answer_key Q1 rules `screen` + `hook`):

- Cards are 0.92-1.0 of the frame wide, in the TOP band (from the top edge down to just above
  her face) or the BOTTOM band (just under her captions at ~0.61, down to the bottom edge).
  Never on the caption row, never on her face.
- 12-14 cards per ~80 s video, on screen 44-54 % of the time; V4 opens on a card at frame 0.
- A recording starts at the part worth seeing, not at its first frame.

On item 12's branch (88bd0aca9, = origin/main 37f470c7f + her caption shape) the writer draws
every card at <= 0.36 of the width (`phone_overlay_grounding._DEFAULT_CARD_SCALE`), keeps every
card centre above y 0.62 (`_card_placement` / `CAPTION_BAND_TOP_FRAC`) whatever the captions do,
stops at 10 cards (`overlay_autoplace._CEILING`), drops a free-path ("likely") match that starts
in the first 2.5 s (`_HOOK_WINDOW_S`), and plays a video card from frame 0 although
`build_suggestions` already picked its best moment (`clip_trim_start_s`).

Behind TALKING_CARD_LAYOUT_ENABLED + `cardLayout` verified + an approving build >= protocol 40.

The REAL chain runs: `_run_generative_job` -> `_run_phone_subtitled_job` -> heuristic matcher
(no Gemini key: the paid `OverlayPlacementAgent` never runs) -> grounding -> compiler ->
recipe validation. Only transport is replaced (Whisper words, probe, DB, the Visuals pool,
Visuals binding, face sampling). Face sampling returns the stand-in face's padded protection
box at every sampled moment: high in the first half of the talk, lower in the second half.

Ways this goes wrong (item-15 failure list F1-F32 in the PR) and the check that catches each:
- F1 a big card covers part of a face (IoU <= 0.02 lets it): strict box test vs the TRUE face.
- F2/F3/F31 a card on the caption row: strict box test vs the drawn caption boxes.
- F5 faces sampled only for the first cards: the face moves at 40 s; later cards are checked
  against where it really is.
- F8 two cards stacked: no two card boxes intersect while both are on screen.
- F9 a card off the canvas, or pulled back to y 0.62: box inside the canvas; bottom cards exist.
- F10 aspect: width from the asset's own aspect (portrait screenshot stays narrow).
- F16 no opening card; F18 ceiling 10; F19 recording from frame 0.
- F12/F17 a card under the opening title.
- F21 flag off byte-identical (frozen literal of today's cards); F22 old build / token off.
- F23/F24/F25 editor: projection marks the card, a Save keeps its spot, a recipe-derived lane
  keeps it too.
"""

from __future__ import annotations

import uuid

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_visuals as phone_visuals_mod
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_captions import caption_look_from_variant
from app.pipeline.phone_spoken_text import caption_boxes
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS, compile_phone_subtitled_plan
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import phone_text_styling as styling
from app.services.client_protocol import clear_request_context
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_subtitled_editor import (
    lanes_from_editor_sections,
    lanes_from_recipe,
    sections_from_lanes,
)
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
    _words_from,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled
from tests.tasks.test_qendresa_caption_shape_worker import Q1_CAPTIONS, Q1_PROMPT, Q2_CAPTIONS

CANVAS_RATIO = 1080.0 / 1920.0
TALK_S = 80.0
PROTOCOL = 40
OVERLAY_TRACK = "subtitled-overlays"

# The stand-in face's padded protection box (what `_face_protection_box` returns for a
# talk-to-camera face). She sits up in the first half and leans back in the second.
FACE_HIGH = NormalizedBox(0.30, 0.22, 0.70, 0.50)
FACE_LOW = NormalizedBox(0.30, 0.34, 0.70, 0.58)
FACE_SWITCH_S = 40.0


def _face_at(t: float) -> NormalizedBox:
    return FACE_HIGH if t < FACE_SWITCH_S else FACE_LOW


# Her Q1 kit footage list (V7), stretched to 15 Visuals so the 10-card ceiling bites. Each line
# names its Visual with two words nobody else says, at word 6 and 7 of a 10-word line, so the
# free matcher's 8-word window starts exactly on the line.
_NS = uuid.UUID("00000000-0000-4000-8000-000000000015")
VISUALS = [
    # (key, kind, aspect w/h, two naming words, line)
    (
        "signup",
        "image",
        0.46,
        "superapp signup",
        "This app wants everything before you Superapp signup even starts",
    ),
    (
        "devtools",
        "video",
        16 / 9,
        "devtools javascript",
        "So I opened it up and read devtools javascript code underneath",
    ),
    (
        "github",
        "image",
        16 / 9,
        "github repository",
        "Then I found this tool on a github repository online today",
    ),
    (
        "terminal",
        "video",
        16 / 9,
        "terminal npm",
        "Installing it takes exactly one line in terminal npm install command",
    ),
    (
        "diagram",
        "image",
        4 / 3,
        "architecture diagram",
        "Here is how it works inside the architecture diagram stages explained",
    ),
    (
        "prompt",
        "image",
        16 / 9,
        "exact prompt",
        "This is what I typed to it the exact prompt itself",
    ),
    (
        "pixel",
        "image",
        16 / 9,
        "tracking pixel",
        "The very first thing it found was tracking pixel hidden there",
    ),
    (
        "socket",
        "image",
        16 / 9,
        "websocket keystrokes",
        "Second thing it caught was a live websocket keystrokes leaving quietly",
    ),
    (
        "canvas",
        "image",
        16 / 9,
        "fingerprints canvas",
        "Third it quietly reads your whole browser fingerprints canvas every visit",
    ),
    (
        "cookie",
        "image",
        4 / 3,
        "cookie banner",
        "And then it checked whether rejecting the cookie banner does anything",
    ),
    (
        "privacy",
        "image",
        16 / 9,
        "privacy policy",
        "Next it compared what they promise in privacy policy to reality",
    ),
    (
        "analytics",
        "image",
        16 / 9,
        "analytics dashboard",
        "They even count every scroll inside their analytics dashboard all day",
    ),
    (
        "competitor",
        "image",
        16 / 9,
        "competitor checklist",
        "Then I lined it up next to my competitor checklist for comparison",
    ),
    (
        "scorecard",
        "image",
        16 / 9,
        "final scorecard",
        "So here is how it scored on my final scorecard overall rating",
    ),
    (
        "benchmark",
        "image",
        16 / 9,
        "benchmark table",
        "And honestly it beat my manual review in benchmark table results",
    ),
]
RECORDING = "devtools"
RECORDING_S = 12.0
RECORDING_BEST = (3.0, 9.0)  # the part worth watching (Gemini `best_moments`, read at upload)
LINE_S = 5.3


def _id(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


SPEC = {_id(key): (key, kind, aspect, naming) for key, kind, aspect, naming, _line in VISUALS}
ASPECT = {f"visual-{media_id}": spec[2] for media_id, spec in SPEC.items()}
WORDS = _words_from(
    [(0.2 + LINE_S * i, 0.2 + LINE_S * i + 5.0, line) for i, (*_, line) in enumerate(VISUALS)]
)


def _pool_rows():
    rows = []
    for media_id, (key, kind, aspect, naming) in SPEC.items():
        analysis: dict = {"subject": naming}
        ext = "mp4" if kind == "video" else "png"
        if key == RECORDING:
            analysis["best_moments"] = [
                {"start_s": 0.0, "end_s": 2.0, "energy": 0.1, "description": "idle"},
                {"start_s": RECORDING_BEST[0], "end_s": RECORDING_BEST[1], "energy": 0.9},
            ]
        rows.append(
            {
                "id": media_id,
                "gcs_path": f"users/u1/plan/item1/pool/{media_id}.{ext}",
                "gcs_generation": "7",
                "kind": kind,
                "source_filename": f"{key}.{ext}",
                "duration_s": RECORDING_S if kind == "video" else None,
                "aspect": aspect,
                "user_context": "",
                "analysis": analysis,
                "capture": None,
            }
        )
    return rows


def _visual(media_id: str) -> PhoneVisualBinding:
    _key, kind, aspect, _naming = SPEC[media_id]
    row = next(r for r in _pool_rows() if r["id"] == media_id)
    extra = (
        {
            "duration_s": RECORDING_S,
            "width": 1920,
            "height": round(1920 / aspect),
            "orientation_degrees": 0,
        }
        if kind == "video"
        else {}
    )
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=row["gcs_path"],
        generation="7",
        sha256="b" * 64,
        byte_count=100,
        kind=kind,
        **extra,
    )


def _bind(_open, *, job_id, pins):  # noqa: ARG001
    return tuple(_visual(media_id) for media_id in pins)


def _faces(_clip_path, anchors, **_kwargs):
    regions = [
        ProtectedRegion(max(0.0, at - 0.5), at + 0.5, _face_at(at), kind="face") for at in anchors
    ]
    return regions, {"attempted": len(anchors), "detected": len(regions)}


def _cards_job(
    monkeypatch,
    *,
    layout: bool = True,
    verified: bool = True,
    protocol: int | None = PROTOCOL,
    title: str | None = None,
    captions=None,
    shape: bool = True,
    alpha: frozenset[str] = frozenset(),
):
    """Her Q1 job: Q1 captions (item 12 shape on), 15 Visuals, the card layout gates as given."""
    clear_request_context()
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=WORDS, duration_s=TALK_S
    )
    features = ["visualVideos", "authoredText", "captionShape"] + (
        ["cardLayout"] if verified else []
    )
    _enable_beats(monkeypatch, extra_features=tuple(features))
    monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
    if layout:  # default off; the flag-off jobs never touch it
        monkeypatch.setattr(settings, "talking_card_layout_enabled", True)
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", shape)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)

    def pool():
        rows = _pool_rows()
        for row in rows:
            if SPEC[row["id"]][0] in alpha:  # a transparent sticker / logo
                row["analysis"] = {**row["analysis"], "has_alpha": True}
        return rows

    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: pool())
    monkeypatch.setattr(ogm, "sample_face_regions", _faces)

    def environment(canvas):  # no proxy on disk: captions sit at her asked spot
        styling._POLICY.get()["_face_boxes"] = []

    monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
    job.all_candidates["creator_request"] = Q1_PROMPT
    job.all_candidates["voiceover_caption_style"] = "word"
    strategy: dict = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    if title is not None:
        monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
        strategy["opening_title"] = title
        strategy["opening_title_duration_s"] = 3.0
    job.all_candidates["creator_strategy"] = strategy
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    brief = CreativeBrief(version=1, requirements=[captions or Q1_CAPTIONS])
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    return job


def _run(job):
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.status
    return job.assembly_plan["variants"][0]


def _card_clips(recipe):
    track = next((t for t in recipe.tracks if t.id == OVERLAY_TRACK), None)
    return list(track.clips) if track is not None else []


def _box(clip) -> NormalizedBox:
    place = clip.visual_placement
    width = place.width_fraction
    height = width * CANVAS_RATIO / ASPECT[clip.source_asset_id]
    return NormalizedBox(
        place.x_fraction - width / 2,
        place.y_fraction - height / 2,
        place.x_fraction + width / 2,
        place.y_fraction + height / 2,
    )


def _hits(a: NormalizedBox, b: NormalizedBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _geometry(recipe) -> list[tuple]:
    """Every card as the device draws it: asset, window, spot, width, source start."""
    return [
        (
            clip.source_asset_id.removeprefix("visual-")[:8],
            round(clip.visual_placement.window_start, 3),
            round(clip.visual_placement.window_end, 3),
            round(clip.visual_placement.x_fraction, 4),
            round(clip.visual_placement.y_fraction, 4),
            round(clip.visual_placement.width_fraction, 4),
            round(clip.source_start, 3),
        )
        for clip in sorted(_card_clips(recipe), key=lambda c: c.visual_placement.window_start)
    ]


# ------------------------------------------------------------------------------ her layout


@pytest.mark.parametrize(
    "captions",
    ["q1_shape", "todays_middle", "q2_box_low"],
    ids=["her_q1_captions", "item12_off_captions", "low_grey_box_captions"],
)
def test_her_cards_are_wide_top_or_bottom_off_her_face_and_off_her_captions(monkeypatch, captions):
    # The caption row is READ from the plan, never assumed: her Q1 shape (0.60-0.64, item 12),
    # today's 8-word middle captions (item 12 off), and a low grey box (0.76-0.82).
    job = _cards_job(
        monkeypatch,
        shape=captions != "todays_middle",
        captions=Q2_CAPTIONS if captions == "q2_box_low" else Q1_CAPTIONS,
    )
    variant = _run(job)
    recipe = _recipe(job)
    clips = _card_clips(recipe)
    look = caption_look_from_variant(variant)
    style = variant.get("voiceover_caption_style") or "word"

    # F18: her 12-14 cards (15 named, the 14-card ceiling keeps the first 14).
    assert len(clips) == 14, _geometry(recipe)
    receipt = variant["phone_overlay_receipt"]
    assert [row["reason"] for row in receipt["unplaced"]] == ["density_cap"], receipt["unplaced"]

    tops = bottoms = 0
    for clip in clips:
        box = _box(clip)
        place = clip.visual_placement
        aspect = ASPECT[clip.source_asset_id]
        # F9: drawn whole, inside the frame.
        assert box.left >= -1e-6 and box.right <= 1 + 1e-6, box
        assert box.top >= -1e-6 and box.bottom <= 1 + 1e-6, box
        # Her width: a wide screenshot fills the frame; aspect is kept (F10). Always bigger
        # than today's 0.36 for a wide one.
        if aspect >= 1.3:
            assert place.width_fraction > 0.36, (clip.id, place.width_fraction)
        if captions == "q1_shape" and aspect >= 1.7:
            assert place.width_fraction >= 0.9, (clip.id, place.width_fraction)
        elif captions == "q1_shape" and aspect >= 1.3:
            assert place.width_fraction >= 0.7, (clip.id, place.width_fraction)
        # F1/F5: never on her face, wherever she sits during the card (no tolerance).
        for t in (
            place.window_start,
            (place.window_start + place.window_end) / 2,
            place.window_end,
        ):
            assert not _hits(box, _face_at(min(t, TALK_S - 1e-3))), (clip.id, t, box)
        # F2/F3: never on a drawn caption while both are on screen.
        for caption in caption_boxes(
            variant["caption_cues"],
            window=(place.window_start, place.window_end),
            caption_style=style,
            canvas=_STORY_CANVAS,
            look=look,
        ):
            assert not _hits(box, caption), (clip.id, box, caption)
        centre = (box.top + box.bottom) / 2
        tops += centre < 1 / 3
        bottoms += centre > 2 / 3
    if captions == "q1_shape":
        # Top AND bottom thirds, as she does (the face moves at 40 s, so both bands open up).
        assert tops >= 2 and bottoms >= 2, _geometry(recipe)
    else:
        assert tops + bottoms == len(clips), _geometry(recipe)

    # F8: two cards never stack.
    for i, a in enumerate(clips):
        for b in clips[i + 1 :]:
            pa, pb = a.visual_placement, b.visual_placement
            if pa.window_start < pb.window_end and pb.window_start < pa.window_end:
                assert not _hits(_box(a), _box(b)), (a.id, b.id)

    # F16: a screenshot opens the video (her first sentence names it).
    assert min(c.visual_placement.window_start for c in clips) < 1.0, _geometry(recipe)
    # On screen for a good share of the talk (hers: 44-54 %).
    shown = sum(c.visual_placement.window_end - c.visual_placement.window_start for c in clips)
    assert shown / TALK_S >= 0.44, shown


def test_a_transparent_sticker_keeps_todays_corner_card(monkeypatch):
    # F11: a logo / sticker with see-through edges is not a screenshot: never blown up to the
    # full width. It keeps today's small corner card, above the caption row.
    job = _cards_job(monkeypatch, alpha=frozenset({"scorecard"}))
    _run(job)
    (clip,) = [
        c for c in _card_clips(_recipe(job)) if c.source_asset_id == f"visual-{_id('scorecard')}"
    ]
    assert clip.visual_placement.width_fraction <= 0.36 + 1e-6
    assert clip.visual_placement.y_fraction <= 0.62 + 1e-6


def test_a_recording_starts_at_the_part_worth_seeing(monkeypatch):
    job = _cards_job(monkeypatch)
    _run(job)
    (clip,) = [
        c for c in _card_clips(_recipe(job)) if c.source_asset_id == f"visual-{_id(RECORDING)}"
    ]
    # F19: from its best moment (3.0-9.0 s of the 12 s recording), not from frame 0.
    assert clip.source_start == pytest.approx(RECORDING_BEST[0], abs=1e-3)
    assert clip.source_start + clip.source_duration <= RECORDING_S + 1e-6


def test_no_card_sits_under_her_opening_title(monkeypatch):
    job = _cards_job(monkeypatch, title="lets build an agent that reads an APP before I trust it!!")
    _run(job)
    recipe = _recipe(job)
    titles = [layer for layer in recipe.text_layers if not layer.id.startswith("caption-")]
    assert titles, "the harness must draw her opening title"
    title_end = max(layer.end for layer in titles if layer.start < 1.0)
    clips = _card_clips(recipe)
    assert clips
    # F12/F17: a card may open the video only when no title is there; with her title on screen
    # for the first 3 s, the first card waits for it.
    assert all(c.visual_placement.window_start >= title_end - 1e-3 for c in clips), (
        title_end,
        _geometry(recipe),
    )


# ------------------------------------------------------------------------------ gates


# Today's cards for this exact job, frozen from the pre-change code (item 12's branch 88bd0aca9):
# 10 cards (ceiling), all top-right at <= 0.36 width, the opening screenshot dropped
# (`hook_window`), the recording from frame 0. The flag-off output may never move.
TODAY = [
    ("60cc5977", 5.955, 9.955, 0.74, 0.22, 0.198, 0.0),
    ("3521123e", 11.255, 15.255, 0.74, 0.22, 0.198, 0.0),
    ("317b9551", 16.555, 20.555, 0.74, 0.22, 0.198, 0.0),
    ("6b1e980d", 21.855, 25.855, 0.74, 0.22, 0.36, 0.0),
    ("8f8843c5", 27.609, 31.609, 0.74, 0.22, 0.36, 0.0),
    ("9db52386", 32.455, 36.455, 0.74, 0.22, 0.36, 0.0),
    ("40c56dc2", 37.755, 41.755, 0.74, 0.22, 0.36, 0.0),
    ("4f373294", 43.055, 47.055, 0.74, 0.22, 0.36, 0.0),
    ("4610c1a5", 48.355, 52.355, 0.74, 0.22, 0.36, 0.0),
    ("3a8355ed", 53.655, 57.655, 0.74, 0.22, 0.36, 0.0),
]
TODAY_UNPLACED = ["hook_window", "density_cap", "density_cap", "density_cap", "density_cap"]


def test_flag_off_cards_are_exactly_todays(monkeypatch):
    job = _cards_job(monkeypatch, layout=False)
    variant = _run(job)
    # F21: the switch off draws exactly today's cards and says exactly today's reasons.
    assert _geometry(_recipe(job)) == TODAY
    receipt = variant["phone_overlay_receipt"]
    assert [row["reason"] for row in receipt["unplaced"]] == TODAY_UNPLACED
    assert all("caption_clear" not in item for item in variant.get("media_overlays") or [])


@pytest.mark.parametrize(
    "gate",
    [
        {"verified": False},
        {"protocol": PROTOCOL - 1},
        {"protocol": None},
    ],
    ids=["token_off", "old_build", "no_build_stamp"],
)
def test_a_closed_gate_renders_todays_cards(monkeypatch, gate):
    off = _cards_job(monkeypatch, layout=False, **gate)
    _run(off)
    closed = _cards_job(monkeypatch, **gate)
    _run(closed)
    # F22: an old build, an unverified token or an unknown build gets exactly today's recipe.
    assert _recipe(closed).model_dump_json() == _recipe(off).model_dump_json()
    assert (
        closed.assembly_plan["variants"][0]["phone_overlay_receipt"]
        == off.assembly_plan["variants"][0]["phone_overlay_receipt"]
    )


# ------------------------------------------------------------------------------ editor


def test_an_editor_save_keeps_every_card_where_it_was(monkeypatch):
    job = _cards_job(monkeypatch)
    _run(job)
    recipe = _recipe(job)
    before = _geometry(recipe)
    assert any(row[4] > 0.62 for row in before), "the harness must place a bottom card"

    visuals = tuple(_visual(media_id) for media_id in SPEC)
    # F25: the first editor open derives the lanes from the recipe (nothing persisted yet).
    derived = lanes_from_recipe(recipe, visuals=visuals)
    low = [card for card in derived.overlays if card.y_frac > 0.62]
    assert low and all(card.caption_clear for card in low)
    # F23: the projection tells a new editor not to pull the card up to 0.62.
    sections = sections_from_lanes(derived, labels={})
    marked = {item["id"] for item in sections["media_overlays"] if item.get("caption_clear")}
    assert marked == {card.id for card in low}
    # F24: a Save (the editor sends back what it was shown) recompiles to the same spots.
    saved = lanes_from_editor_sections(
        previous=derived,
        sound_effects=sections["sound_effects"],
        media_overlays=sections["media_overlays"],
        visuals=visuals,
    )
    assert isinstance(saved, PhoneSubtitledLanes)
    variant = job.assembly_plan["variants"][0]
    resaved = compile_phone_subtitled_plan(
        recipe_bindings(job),
        caption_cues=variant["caption_cues"],
        caption_style=variant.get("voiceover_caption_style") or "word",
        visuals=visuals,
        lanes=saved,
        caption_look=caption_look_from_variant(variant),
    )
    assert _geometry(resaved) == before


def recipe_bindings(job):
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

    return tuple(
        PhoneSourceBinding.model_validate(row) for row in job.assembly_plan[PHONE_SOURCES_FIELD]
    )
