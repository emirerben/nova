"""KRI-1511: a pause cut inside a sticker's window no longer shortens its time on screen.

Cusco kit T3 "Sticker 1" (job a4f7b702, phone Talking, Clean up speech): every sticker got a
2.5 s window on the source take, then `remap_lanes_for_cut` moved it through the cut plan with
`remap_range`, which shrinks a window by the removed time inside it. The puma was up for 1.27 s
(1.2 s in the frame strip), the snake 1.87 s, the condor 1.75 s. A sticker (or corner photo)
held from its spoken word keeps that hold in OUTPUT time now, and stops early only where the
next card starts or the speaker's take ends.

Ways this can go wrong (written before the code):
 H1 a held sticker still shrinks when a cut falls inside it;
 H2 the longer sticker runs into the next card (two pictures stacked in one corner);
 H3 it runs past the end of the cut take;
 H4 any card ends EARLIER than before (one already overlapping a later card, a short window);
 H5 a card that is not held changes: a phrase-sized photo (its phrase got shorter, so should
    it), a full-screen cutaway, a video card, the closing photo or badge, a manual card;
 H6 no cut, or nothing held, is not byte-identical;
 H7 a held sticker whose whole window was cut comes back;
 H8 the sticker's start moves off its word;
 H9 grounding marks the wrong cards as held;
 H10 the real T3 job does not come out as snake 11.96-14.01, puma 14.01-15.50, condor
     15.50-18.00 (puma cannot reach 2.5 s: "kondor" is said 1.49 s after "puma" once the
     pause between them is cut, and both stickers share the corner).

Real data: `tests/fixtures/kri1511_cusco_t3/sticker1.json` (the variant's own timed words, cut
and beat receipt). The phrase matcher, geometry arbitration, lane remap and recipe compiler are
the real ones; only the pool/SFX database seams and the face sampler are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app.services.phone_overlay_grounding as pg
import app.services.phone_reaction_grounding as rg
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    SubtitledOverlayCard,
    remap_lanes_for_cut,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_visuals import PhoneVisualBinding
from tests.pipeline.test_phone_subtitled_plan import _binding, _cut_plan

T3 = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1511_cusco_t3" / "sticker1.json").read_text(
        encoding="utf-8"
    )
)
T3_PLAN = _cut_plan([tuple(span) for span in T3["removed"]], T3["duration_s"])
T3_CUT_END_S = sum(end - start for start, end in T3_PLAN.keep_segments)
_IDS = {
    "snake": "asset-yilan",
    "puma": "asset-puma",
    "condor": "asset-kondor",
    "llama": "asset-lama",
}


def _asset(media_id: str, *, kind: str = "image") -> dict[str, Any]:
    ext = "mp4" if kind == "video" else "png"
    return {
        "id": media_id,
        "gcs_path": f"users/u1/plan/item1/pool/{media_id}.{ext}",
        "gcs_generation": "9",
        "kind": kind,
        "source_filename": f"{media_id}.{ext}",
        "duration_s": 4.0 if kind == "video" else None,
        "aspect": 1.0,
        "user_context": "",
        "analysis": {},
    }


def _ground(monkeypatch, beats, words, *, assets, closing=None, **extra):  # noqa: ANN001, ANN003, ANN202
    """The real grounding, called the way the phone Talking worker calls it."""
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(pg, "sample_face_regions", lambda *a, **k: ([], {}))
    return rg.ground_phone_reaction_beats(
        lambda: None,
        job_id="job-kri1511",
        beats=beats,
        closing=closing,
        words=words,
        duration_s=T3["duration_s"],
        clip_path="/tmp/clip.mp4",
        allow_fullscreen=True,
        fullscreen_hold_s_default=rg.TALKING_FULLSCREEN_HOLD_S_DEFAULT,
        **extra,
    )


def _t3_grounded(monkeypatch):  # noqa: ANN001, ANN202
    beats = [{**beat, "visual_id": _IDS[beat["beat_id"]]} for beat in T3["beats"]]
    assets = [_asset(media_id) for media_id in _IDS.values()]
    return _ground(monkeypatch, beats, T3["words"], assets=assets)


def _windows(lanes: PhoneSubtitledLanes) -> dict[str, tuple[float, float]]:
    return {
        card.id.removeprefix("beat-").rsplit("-", 1)[0]: (
            round(card.start_s, 2),
            round(card.end_s, 2),
        )
        for card in lanes.overlays
    }


def _card(card_id: str, start_s: float, end_s: float, **extra: Any) -> SubtitledOverlayCard:
    return SubtitledOverlayCard(
        id=card_id,
        media_id=card_id,
        gcs_path=f"users/u1/plan/item1/pool/{card_id}.png",
        generation="1",
        start_s=start_s,
        end_s=end_s,
        **extra,
    )


# --- H10 / H1 / H8: the real T3 job -------------------------------------------------------


def test_h10_t3_grounding_reproduces_the_receipt_and_marks_the_three_stickers_held(monkeypatch):
    grounded = _t3_grounded(monkeypatch)
    placed = {p["beat_id"]: (p["at_s"], p["end_s"]) for p in T3["placed"]}
    assert {
        b: (round(s, 2), round(e, 2))
        for b, (s, e) in _windows(PhoneSubtitledLanes(overlays=grounded.cards)).items()
    } == placed
    assert grounded.receipt["unplaced"] == T3["unplaced"]  # the llama: never_heard
    assert grounded.held_card_ids == frozenset({"beat-snake-1", "beat-puma-1", "beat-condor-1"})


def test_h10_before_the_fix_the_replay_shrinks_the_stickers_as_the_phone_showed(monkeypatch):
    """The control: without held ids, the real remap gives the windows the frame strip saw."""
    grounded = _t3_grounded(monkeypatch)
    lanes, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(overlays=grounded.cards), T3_PLAN)
    assert dropped == frozenset()
    windows = _windows(lanes)
    assert windows == {"snake": (11.96, 13.83), "puma": (14.01, 15.28), "condor": (15.5, 17.25)}
    for beat_id, (seen_start, seen_end) in T3["seen_on_screen_s"].items():
        start, end = windows[beat_id]
        assert start == pytest.approx(seen_start, abs=0.2)
        assert end == pytest.approx(seen_end, abs=0.2)


def test_h1_h2_h8_h10_t3_stickers_keep_their_hold_until_the_next_sticker(monkeypatch):
    grounded = _t3_grounded(monkeypatch)
    lanes, dropped = remap_lanes_for_cut(
        PhoneSubtitledLanes(overlays=grounded.cards),
        T3_PLAN,
        held_card_ids=grounded.held_card_ids,
    )
    assert dropped == frozenset()
    assert _windows(lanes) == {
        "snake": (11.96, 14.01),  # 2.05 s: up to the puma
        "puma": (14.01, 15.5),  # 1.49 s: "kondor" follows that soon in the cleaned take
        "condor": (15.5, 18.0),  # the full 2.5 s
    }
    # The recipe the phone renders carries the same windows (no stacked stickers).
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=T3["duration_s"]),),
        caption_cues=[],
        visuals=tuple(_visual(card.media_id) for card in lanes.overlays),
        lanes=lanes,
        cut_plan=T3_PLAN,
    )
    clips = sorted(
        (
            clip
            for track in recipe.tracks
            for clip in track.clips
            if clip.id.startswith("subtitled-overlay-beat-")
        ),
        key=lambda clip: clip.timeline_start,
    )
    spans = [
        (round(clip.timeline_start, 2), round(clip.timeline_start + clip.source_duration, 2))
        for clip in clips
    ]
    assert all(clip.rate == 1 for clip in clips)
    assert spans == [(11.96, 14.01), (14.01, 15.5), (15.5, 18.0)]


def _visual(media_id: str) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/u1/plan/item1/pool/{media_id}.png",
        generation="9",
        sha256="b" * 64,
        byte_count=2048,
    )


# --- H2 / H3 / H4 / H7: the cap and the never-shorter rule --------------------------------


def test_h2_any_later_card_stops_the_hold_even_in_another_corner():
    plan = _cut_plan([(2.0, 3.0)], 20.0)
    lanes = PhoneSubtitledLanes(
        overlays=[_card("held", 1.0, 3.5), _card("photo", 4.0, 6.0, x_frac=0.8)]
    )
    out, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"})
    assert _windows(out)["held"] == (1.0, 3.0)  # wants 1.0-3.5; the photo starts at 3.0


def test_h2_a_marked_objects_window_stops_the_hold():
    """Grounding kept the sticker clear of a marked object only for its original window."""
    plan = _cut_plan([(2.0, 3.0)], 20.0)
    lanes = PhoneSubtitledLanes(overlays=[_card("held", 1.0, 3.5)])
    out, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"}, hold_stops_s=[3.8])
    assert _windows(out)["held"] == (1.0, 2.8)  # the object's window starts at 3.8 - 1.0
    # A stop inside the old window never shortens it.
    out, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"}, hold_stops_s=[1.5])
    assert _windows(out)["held"] == (1.0, 2.5)


def test_h3_the_hold_stops_at_the_end_of_the_cut_take():
    plan = _cut_plan([(7.5, 8.5)], 9.5)  # the cut take ends at 8.5 s
    lanes = PhoneSubtitledLanes(overlays=[_card("held", 7.0, 9.0)])
    before, _ = remap_lanes_for_cut(lanes, plan)
    after, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"})
    assert _windows(before)["held"] == (7.0, 8.0)
    assert _windows(after)["held"] == (7.0, 8.5)  # wants 7.0-9.0


def test_h4_a_card_already_running_into_a_later_one_keeps_todays_end():
    plan = _cut_plan([(2.0, 2.5)], 20.0)
    lanes = PhoneSubtitledLanes(overlays=[_card("held", 1.0, 5.0), _card("other", 3.0, 4.0)])
    before, _ = remap_lanes_for_cut(lanes, plan)
    after, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"})
    assert _windows(after) == _windows(before) == {"held": (1.0, 4.5), "other": (2.5, 3.5)}


def test_h2_a_card_starting_at_the_same_moment_is_already_on_screen_so_no_cap():
    """A photo and a sticker on one word were on screen together before the cut too."""
    plan = _cut_plan([(2.0, 2.5)], 20.0)
    lanes = PhoneSubtitledLanes(
        overlays=[_card("held", 1.0, 3.5), _card("photo", 1.0, 3.5, x_frac=0.8)]
    )
    out, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held", "photo"})
    assert _windows(out) == {"held": (1.0, 3.5), "photo": (1.0, 3.5)}


def test_h7_a_held_card_inside_a_removal_is_still_dropped():
    plan = _cut_plan([(2.0, 5.0)], 20.0)
    lanes = PhoneSubtitledLanes(overlays=[_card("held", 2.5, 4.5), _card("next", 6.0, 8.0)])
    out, dropped = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"})
    assert dropped == frozenset({"held"})
    assert [card.id for card in out.overlays] == ["next"]


# --- H5 / H6: everything else is byte-identical --------------------------------------------


def test_h5_h6_cards_not_held_and_plans_without_cuts_are_unchanged():
    plan = _cut_plan([(2.0, 3.0)], 20.0)
    lanes = PhoneSubtitledLanes(
        overlays=[
            _card("phrase-photo", 1.0, 4.0),
            _card("cutaway", 5.0, 7.0, display_mode="fullscreen"),
            _card("held", 9.0, 11.5),
        ]
    )
    before, _ = remap_lanes_for_cut(lanes, plan)
    after, _ = remap_lanes_for_cut(lanes, plan, held_card_ids={"held"})
    assert after.overlays[:2] == before.overlays[:2]
    # No removal inside the held window: it keeps exactly today's window.
    assert after == before
    no_cut = _cut_plan([], 20.0)
    assert remap_lanes_for_cut(lanes, no_cut, held_card_ids={"held"}) == (lanes, frozenset())


# --- H9: which cards grounding holds ---------------------------------------------------------


def test_h9_only_word_timed_corner_pictures_are_held(monkeypatch):
    words = [
        {"text": text, "start_s": float(i), "end_s": i + 0.4}
        for i, text in enumerate(
            "the snake then my photo then the cutaway then my clip and that is all folks "
            "goodbye now".split()
        )
    ]
    assets = [
        _asset("asset-yilan"),
        _asset("asset-photo"),
        _asset("asset-full"),
        _asset("asset-clip", kind="video"),
        _asset("asset-end"),
    ]
    beats = [
        {
            "beat_id": "snake",
            "trigger": "snake",
            "visual_id": "asset-yilan",
            "visual_role": "sticker",
        },
        {
            "beat_id": "photo",
            "trigger": "photo",
            "visual_id": "asset-photo",
            "visual_role": "photo",
        },
        {
            "beat_id": "full",
            "trigger": "cutaway",
            "visual_id": "asset-full",
            "visual_role": "photo",
            "display_mode": "fullscreen",
        },
        {"beat_id": "clip", "trigger": "clip", "visual_id": "asset-clip", "visual_role": "photo"},
    ]
    grounded = _ground(
        monkeypatch,
        beats,
        words,
        assets=assets,
        closing={"visual_id": "asset-end"},
        video_supported=True,
    )
    ids = {card.id for card in grounded.cards}
    assert {"beat-snake-1", "beat-photo-1", "beat-full-1", "beat-clip-1", "closing-photo"} <= ids
    assert grounded.held_card_ids == frozenset({"beat-snake-1", "beat-photo-1"})


def test_h9_a_phrase_sized_corner_photo_is_not_held(monkeypatch):
    """KRI-1342: a corner photo cued by what the talk is about follows its phrase; when the
    phrase loses a pause, the photo should end with it."""
    words = [
        {"text": text, "start_s": float(i), "end_s": i + 0.4}
        for i, text in enumerate("we went to the old market and the bread there was great".split())
    ]
    beats = [
        {
            "beat_id": "market",
            "trigger": "old market",
            "visual_id": "asset-photo",
            "visual_role": "photo",
        },
    ]
    grounded = _ground(
        monkeypatch,
        beats,
        words,
        assets=[_asset("asset-photo")],
        phrase_timing=rg.TALKING_PHRASE_TIMING,
    )
    assert grounded.receipt["placed"][0].get("timing") == "phrase"
    assert grounded.held_card_ids == frozenset()
