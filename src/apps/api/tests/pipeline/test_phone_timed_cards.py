"""Exact-timed small cards on phone Montage / Voiceover edits.

Written BEFORE the code against the failure-mode list in
``docs/reviews/timed-cards-montage-voiceover-failure-modes.md`` (the number in each test's
docstring is the row it pins). Every test runs the real grounding
(`app.pipeline.phone_timed_cards.ground_timed_cards`) and the real compilers, asserting the
compiled recipe's overlay track -- no network, no LLM, no database.
"""

from __future__ import annotations

import pytest

from app.kria.device_render import recipe_digest
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_narrated_plan import (
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    replace_editor_lanes,
)
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC, PhoneSubtitledLanes
from app.pipeline.phone_timed_cards import (
    CARD_SCALE,
    FaceWindow,
    clip_windows_from_recipe,
    ground_timed_cards,
    timed_card_rows,
    timed_cards_from_rows,
)
from app.pipeline.phone_voiceover_montage_plan import (
    compile_phone_voiceover_montage_plan as _vo_compile,
)
from app.pipeline.render_geometry import NormalizedBox
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from tests.pipeline.test_phone_guided_plan import fixture as guided_fixture
from tests.pipeline.test_phone_guided_plan import transition_fixture
from tests.pipeline.test_phone_voiceover_montage_plan import _narration
from tests.pipeline.test_phone_voiceover_montage_plan import fixture as voiceover_fixture

PHOTO_ID = "11111111-1111-4111-8111-111111111111"
VIDEO_ID = "22222222-2222-4222-8222-222222222222"


def _visual(media_id: str, *, kind: str = "image", duration_s: float | None = None):
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/owner/{media_id}.{'mp4' if kind == 'video' else 'jpg'}",
        generation="9",
        sha256=("b" if kind == "image" else "c") * 64,
        byte_count=2048,
        kind=kind,
        duration_s=duration_s,
        width=1080 if kind == "video" else None,
        height=1920 if kind == "video" else None,
    )


def _card(card_id: str = "menu", **overrides):
    row = {"card_id": card_id, "visual_id": f"asset-{PHOTO_ID}", "label": "menu photo"}
    row.update(overrides)
    return row


def _ground(cards, *, timeline_end=10.0, clip_windows=None, visuals=None, **kwargs):
    return ground_timed_cards(
        cards,
        clip_windows=clip_windows or {},
        timeline_end=timeline_end,
        visuals=visuals if visuals is not None else (_visual(PHOTO_ID),),
        videos_allowed=kwargs.pop("videos_allowed", False),
        **kwargs,
    )


def compile_phone_voiceover_montage_plan(decision, bindings, **kwargs):
    return _vo_compile(decision, bindings, narration=_narration(), **kwargs)


def _overlay_track(recipe):
    return next(track for track in recipe.tracks if track.id == "subtitled-overlays")


# -- 4: at_s beyond the edit ---------------------------------------------------------------


def test_card_past_the_end_is_unplaced_and_a_late_card_is_shortened() -> None:
    grounded = _ground(
        [
            _card("late", at_s=9.9, hold_s=2.0),
            _card("tail", at_s=9.0, hold_s=3.0),
        ],
        timeline_end=10.0,
    )
    assert [c.id for c in grounded.cards] == ["timed-tail"]
    (tail,) = grounded.cards
    assert (tail.start_s, tail.end_s) == (9.0, 10.0), "a card never outlasts the video"
    (unplaced,) = grounded.receipt["unplaced"]
    assert unplaced["card_id"] == "late" and unplaced["reason"] == "past_end"
    assert unplaced["timeline_end_s"] == 10.0
    (placed,) = grounded.receipt["placed"]
    assert placed == {
        "card_id": "tail",
        "media_id": PHOTO_ID,
        "visual_id": f"asset-{PHOTO_ID}",
        "label": "menu photo",
        "anchor": "at_s",
        "at_s": 9.0,
        "end_s": 10.0,
        "hold_s": 1.0,
        "kind": "image",
        "x_frac": placed["x_frac"],
        "y_frac": placed["y_frac"],
    }


# -- 2: the caption band -------------------------------------------------------------------


def test_card_stays_above_the_caption_band() -> None:
    grounded = _ground([_card(at_s=1.0, hold_s=2.0)])
    (card,) = grounded.cards
    assert card.y_frac + (CARD_SCALE * 9 / 16) / 2 <= CAPTION_BAND_TOP_FRAC
    assert card.scale == CARD_SCALE and card.display_mode == "pip" and card.kind == "image"


# -- 3: faces --------------------------------------------------------------------------------


def test_card_moves_off_a_face_then_drops_when_every_spot_collides() -> None:
    right_corner = FaceWindow(0.0, 10.0, NormalizedBox(0.55, 0.05, 0.95, 0.35))
    grounded = _ground([_card(at_s=1.0, hold_s=2.0)], faces=(right_corner,))
    (card,) = grounded.cards
    assert card.x_frac < 0.5, "the card moved to the other corner instead of covering the face"
    (placed,) = grounded.receipt["placed"]
    assert placed["x_frac"] == card.x_frac

    everywhere = FaceWindow(0.0, 10.0, NormalizedBox(0.0, 0.0, 1.0, CAPTION_BAND_TOP_FRAC))
    grounded = _ground([_card(at_s=1.0, hold_s=2.0)], faces=(everywhere,))
    assert grounded.cards == ()
    (unplaced,) = grounded.receipt["unplaced"]
    assert unplaced["reason"] == "face_collision"

    # A face shown OUTSIDE the card's window never moves it.
    later = FaceWindow(5.0, 10.0, NormalizedBox(0.0, 0.0, 1.0, CAPTION_BAND_TOP_FRAC))
    grounded = _ground([_card(at_s=1.0, hold_s=2.0)], faces=(later,))
    (card,) = grounded.cards
    assert card.x_frac > 0.5


# -- 7: two cards overlapping ----------------------------------------------------------------


def test_overlapping_cards_keep_the_first_and_report_the_second() -> None:
    grounded = _ground(
        [
            _card("second", at_s=2.0, hold_s=2.0),
            _card("first", at_s=1.0, hold_s=2.0),
            _card("third", at_s=3.0, hold_s=1.0),
        ]
    )
    assert [c.id for c in grounded.cards] == ["timed-first", "timed-third"]
    (unplaced,) = grounded.receipt["unplaced"]
    assert unplaced == {
        "card_id": "second",
        "media_id": PHOTO_ID,
        "visual_id": f"asset-{PHOTO_ID}",
        "label": "menu photo",
        "at_s": 2.0,
        "reason": "overlaps_card",
        "overlaps": "first",
    }


# -- 5: video cards ---------------------------------------------------------------------------


def test_video_card_needs_visual_videos_verified() -> None:
    video = _visual(VIDEO_ID, kind="video", duration_s=1.0)
    card = _card("clip", visual_id=f"asset-{VIDEO_ID}", at_s=1.0, hold_s=2.0)
    refused = _ground([card], visuals=(video,), videos_allowed=False)
    assert refused.cards == ()
    assert refused.receipt["unplaced"][0]["reason"] == "video_not_supported"

    allowed = _ground([card], visuals=(video,), videos_allowed=True)
    (compiled,) = allowed.cards
    assert compiled.kind == "video" and compiled.source_start_s == 0.0
    assert allowed.receipt["placed"][0]["kind"] == "video"


# -- 13: unknown visual ------------------------------------------------------------------------


def test_unbound_visual_is_reported_not_guessed() -> None:
    grounded = _ground([_card(visual_id="asset-nope", at_s=1.0)])
    assert grounded.cards == ()
    assert grounded.receipt["unplaced"][0]["reason"] == "visual_not_bound"


# -- 6 / 1: clip anchors ---------------------------------------------------------------------


def test_clip_anchor_must_name_an_edit_clip() -> None:
    grounded = _ground(
        [_card(clip_id="missing", offset_s=0.5, hold_s=1.0)],
        clip_windows={"source": (0.0, 3.0)},
    )
    assert grounded.cards == ()
    assert grounded.receipt["unplaced"][0]["reason"] == "clip_not_in_edit"


def test_clip_anchor_follows_the_compiled_clip_start_through_a_crossfade() -> None:
    """1: the second moment starts 0.3 s early on the output clock (the crossfade overlap),
    and that compiled start -- not the plan's nominal slot -- is the card's clock."""
    plan, bindings = transition_fixture("crossfade", 0.3)
    base = compile_phone_guided_plan(plan, bindings)
    windows = clip_windows_from_recipe(base, video_track_id="story", bindings=bindings)
    second = base.tracks[0].clips[1]
    assert windows == {"source": (0.0, 3.0)}
    # One binding plays both moments; anchor by media id lands on its FIRST appearance.
    # A per-moment anchor uses the moment id.
    windows_by_clip = clip_windows_from_recipe(
        base, video_track_id="story", bindings=bindings, by_clip_id=True
    )
    assert windows_by_clip[second.id] == (2.7, 5.7)
    grounded = ground_timed_cards(
        [_card(clip_id=second.id, offset_s=0.5, hold_s=1.0)],
        clip_windows=windows_by_clip,
        timeline_end=base.duration,
        visuals=(_visual(PHOTO_ID),),
        videos_allowed=False,
    )
    (card,) = grounded.cards
    assert (card.start_s, card.end_s) == pytest.approx((3.2, 4.2))
    assert grounded.receipt["placed"][0]["anchor"] == "clip"

    recipe = compile_phone_guided_plan(
        plan, bindings, visuals=(_visual(PHOTO_ID),), timed_cards=grounded.cards
    )
    (clip,) = _overlay_track(recipe).clips
    assert clip.timeline_start == pytest.approx(3.2)
    assert clip.visual_placement.window_end == pytest.approx(4.2)


# -- 9: flag off / no cards => byte-identical --------------------------------------------------


def test_compile_without_cards_is_byte_identical() -> None:
    plan, bindings = guided_fixture()
    assert recipe_digest(compile_phone_guided_plan(plan, bindings)) == recipe_digest(
        compile_phone_guided_plan(plan, bindings, timed_cards=())
    )
    decision, vo_bindings = voiceover_fixture()
    assert recipe_digest(compile_phone_voiceover_montage_plan(decision, vo_bindings)) == (
        recipe_digest(
            compile_phone_voiceover_montage_plan(decision, vo_bindings, timed_cards=(), visuals=())
        )
    )


# -- 10: each runner's own main track ----------------------------------------------------------


def _narrated_recipe():
    def binding(media_id: str) -> PhoneSourceBinding:
        return PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"user/analysis-proxy-{media_id}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=4.0,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )

    steps = [
        NarratedPhoneStep(step_id="s0", media_id="main-0", start_s=0, end_s=4),
        NarratedPhoneStep(step_id="s1", media_id="main-1", start_s=4, end_s=8),
    ]
    bindings = (binding("main-0"), binding("main-1"))
    recipe = compile_phone_narrated_plan(
        steps,
        bindings,
        PhoneNarrationBed(
            plan_item_id="item-1",
            generation="1",
            fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=99),
            duration_s=8,
        ),
        voiceover_duration_s=8,
        caption_cues=[{"text": "Hello there", "start_s": 0, "end_s": 2}],
    )
    return recipe, bindings


def test_each_runner_lands_the_card_on_its_own_main_track() -> None:
    photo = _visual(PHOTO_ID)

    # Montage (guided / unified): `story` track, the card at 1.0 s for 1.5 s.
    plan, bindings = guided_fixture()
    windows = clip_windows_from_recipe(
        compile_phone_guided_plan(plan, bindings), video_track_id="story", bindings=bindings
    )
    grounded = ground_timed_cards(
        [_card(at_s=1.0, hold_s=1.5)],
        clip_windows=windows,
        timeline_end=3.0,
        visuals=(photo,),
        videos_allowed=False,
    )
    guided = compile_phone_guided_plan(plan, bindings, visuals=(photo,), timed_cards=grounded.cards)
    (clip,) = _overlay_track(guided).clips
    assert (clip.timeline_start, clip.visual_placement.window_end) == (1.0, 2.5)
    assert clip.volume == 0 and clip.visual_placement.width_fraction == CARD_SCALE
    assert {"visualBlocks", "alphaOverlay", "audioMix", "stillImages"} <= set(
        guided.required_capabilities
    )
    assert any(a.id == f"visual-{PHOTO_ID}" for a in guided.asset_manifest.assets)

    # Montage-family voiceover: `montage` track.
    decision, vo_bindings = voiceover_fixture()
    base = compile_phone_voiceover_montage_plan(decision, vo_bindings)
    windows = clip_windows_from_recipe(base, video_track_id="montage", bindings=vo_bindings)
    assert set(windows) == {"c0", "c1", "c2"}
    grounded = ground_timed_cards(
        [_card(clip_id="c1", offset_s=0.5, hold_s=2.0)],
        clip_windows=windows,
        timeline_end=base.duration,
        visuals=(photo,),
        videos_allowed=False,
    )
    voiceover = compile_phone_voiceover_montage_plan(
        decision, vo_bindings, visuals=(photo,), timed_cards=grounded.cards
    )
    (clip,) = _overlay_track(voiceover).clips
    assert clip.timeline_start == pytest.approx(windows["c1"][0] + 0.5)
    # Narration and the montage clips are untouched.
    assert [t.id for t in voiceover.tracks] == ["montage", "narration", "subtitled-overlays"]
    assert voiceover.audio == base.audio

    # Narrated voiceover: the KRI-281 lanes on the `narrated` track.
    narrated, _ = _narrated_recipe()
    windows = clip_windows_from_recipe(narrated, video_track_id="narrated", bindings=())
    assert windows == {}
    grounded = ground_timed_cards(
        [_card(at_s=5.0, hold_s=2.0)],
        clip_windows=windows,
        timeline_end=narrated.duration,
        visuals=(photo,),
        videos_allowed=False,
    )
    with_lane = replace_editor_lanes(
        narrated, lanes=PhoneSubtitledLanes(overlays=list(grounded.cards)), visuals=(photo,)
    )
    (clip,) = _overlay_track(with_lane).clips
    assert (clip.timeline_start, clip.visual_placement.window_end) == (5.0, 7.0)


# -- 11: the grounded cards round-trip through the variant row ---------------------------------


def test_grounded_cards_round_trip_through_persisted_rows() -> None:
    grounded = _ground([_card(at_s=1.0, hold_s=2.0)])
    rows = timed_card_rows(grounded.cards)
    assert rows[0]["media_id"] == PHOTO_ID and "display_mode" not in rows[0]
    assert timed_cards_from_rows(rows) == grounded.cards
    assert timed_cards_from_rows(None) == () and timed_cards_from_rows([{"bad": 1}]) == ()
