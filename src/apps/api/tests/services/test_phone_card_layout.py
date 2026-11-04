"""Qendresa item 15: card geometry (pure) and its grounding path.

Written BEFORE the code. Ways this goes wrong, each covered below:
- a big card partly over a face is accepted (IoU loophole): strict box test;
- a card on the caption band at 0.62 (Q1), 0.50 (middle captions), 0.76-0.82 (low box);
- no caption band known: today's 0.60-1.0 stays protected, top only;
- card off canvas; aspect not kept; a portrait screenshot (0.46) blown up; tie -> top;
- two cards that share time stacked;
- faces sampled for the first cards only (a face that moves later must still be protected);
- face sampling failed -> the fallback box is protected;
- face sampling ran but saw no face anywhere on a talking video -> the fallback box is protected
  (a full-width card over a missed face is worse than a small corner one);
- a card that falls back to today's corner path is accepted LOW (clear of the real band) and then
  pulled up to y 0.62 by the clamp, onto the captions;
- an alpha sticker kept small (today's corner path);
- a recording's trim start dropped; text windows shared with a card;
- ``card_layout=None`` changing today's output at all.
- (review) samples that cover only part of a card's window counted as the whole window;
- (review) a timed-out / partial sampling pass counted as clean;
- (review) the corner-card fallback path trusting "ok but no face seen".
"""

from __future__ import annotations

import uuid

import pytest

import app.services.phone_overlay_grounding as pg
from app.agents.overlay_placement import OverlayPlacementAgent, OverlayPlacementOutput, RawPlacement
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_card_layout import (
    CANVAS_RATIO,
    CardLayoutContext,
    blockers_for_card,
    boxes_intersect,
    place_card,
    shift_past_text_windows,
)

FACE = NormalizedBox(0.30, 0.22, 0.70, 0.50)


def caption(top: float, bottom: float) -> NormalizedBox:
    return NormalizedBox(0.0, top, 1.0, bottom)


# --- pure geometry -----------------------------------------------------------------------


@pytest.mark.parametrize("band", [(0.62, 0.72), (0.50, 0.60), (0.76, 0.82), (0.60, 1.0)])
def test_card_never_touches_a_caption_band_or_the_face(band):
    blockers = [FACE, caption(*band)]
    spot = place_card(16 / 9, blockers)
    assert spot is not None
    assert not any(boxes_intersect(spot.box, b) for b in blockers)
    assert spot.box.left >= 0 and spot.box.right <= 1 and spot.box.top >= 0 and spot.box.bottom <= 1


def test_big_card_partly_over_a_face_is_refused_where_iou_would_pass():
    # A card poking 0.01 into the face is far below the old 0.02 IoU, but must never be placed.
    spot = place_card(16 / 9, [FACE, caption(0.62, 0.72)])
    assert spot is not None and not boxes_intersect(spot.box, FACE)
    assert boxes_intersect(NormalizedBox(0.0, 0.0, 1.0, FACE.top + 0.01), FACE)


def test_top_band_is_full_width_for_a_wide_screenshot():
    spot = place_card(16 / 9, [NormalizedBox(0.3, 0.45, 0.7, 0.60), caption(0.62, 0.72)])
    assert spot is not None and spot.zone == "top"
    assert spot.scale == pytest.approx(0.96)
    # aspect kept: height fraction = w * (1080/1920) / aspect
    assert spot.height == pytest.approx(0.96 * CANVAS_RATIO / (16 / 9))


def test_bottom_band_is_used_when_it_is_bigger():
    spot = place_card(16 / 9, [NormalizedBox(0.3, 0.05, 0.7, 0.30), caption(0.30, 0.40)])
    assert spot is not None and spot.zone == "bottom"
    assert spot.box.top >= 0.40


def test_no_blockers_above_the_caption_when_unknown_band_protects_sixty_percent_down():
    spot = place_card(1.0, [caption(0.60, 1.0)])
    assert spot is not None and spot.zone == "top" and spot.box.bottom <= 0.60


def test_tall_portrait_screenshot_is_not_blown_up():
    spot = place_card(0.46, [FACE, caption(0.62, 0.72)])
    assert spot is not None
    assert spot.scale < 0.5
    assert spot.scale * CANVAS_RATIO / spot.height == pytest.approx(0.46)


def test_tie_goes_to_the_top():
    # symmetric free zones: same area -> top
    spot = place_card(16 / 9, [NormalizedBox(0.3, 0.4, 0.7, 0.6)])
    assert spot is not None and spot.zone == "top"


def test_nothing_fits_returns_none():
    assert place_card(16 / 9, [NormalizedBox(0.0, 0.05, 1.0, 0.97)]) is None


def test_stacked_cards_in_time_block_each_other():
    first = place_card(16 / 9, [FACE, caption(0.62, 0.72)])
    assert first is not None
    blockers = blockers_for_card(
        1.0,
        4.0,
        face_regions=[ProtectedRegion(0, 10, FACE)],
        face_sampling="ok",
        fallback_face_box=NormalizedBox(0.28, 0.02, 0.72, 0.52),
        caption_box=caption(0.62, 0.72),
        placed=[(0.0, 3.0, first.box), (5.0, 8.0, NormalizedBox(0, 0, 1, 1))],
    )
    second = place_card(16 / 9, blockers)
    assert second is not None and not boxes_intersect(second.box, first.box)
    assert second.zone != first.zone


def test_blockers_cover_a_face_that_is_only_seen_elsewhere():
    moved = NormalizedBox(0.3, 0.40, 0.7, 0.60)
    blockers = blockers_for_card(
        2.0,
        4.0,
        face_regions=[ProtectedRegion(40, 41, moved), ProtectedRegion(60, 61, FACE)],
        face_sampling="ok",
        fallback_face_box=NormalizedBox(0.28, 0.02, 0.72, 0.52),
        caption_box=caption(0.62, 0.72),
        placed=[],
    )
    union = NormalizedBox(0.3, 0.22, 0.7, 0.60)
    assert union in blockers


def test_blockers_use_only_faces_in_time_when_there_are_some():
    moved = NormalizedBox(0.3, 0.40, 0.7, 0.60)
    blockers = blockers_for_card(
        41.0,
        43.0,
        face_regions=[ProtectedRegion(40, 44, moved), ProtectedRegion(0, 1, FACE)],
        face_sampling="ok",
        fallback_face_box=NormalizedBox(0.28, 0.02, 0.72, 0.52),
        caption_box=caption(0.62, 0.72),
        placed=[],
    )
    assert moved in blockers and FACE not in blockers


@pytest.mark.parametrize("sampling", ["failed", "skipped"])
def test_failed_sampling_protects_the_fallback_box(sampling):
    fb = NormalizedBox(0.28, 0.02, 0.72, 0.52)
    blockers = blockers_for_card(
        0.0,
        2.0,
        face_regions=[],
        face_sampling=sampling,
        fallback_face_box=fb,
        caption_box=caption(0.6, 1.0),
        placed=[],
    )
    assert fb in blockers


def test_sampling_that_saw_no_face_anywhere_still_protects_the_fallback_box():
    fb = NormalizedBox(0.28, 0.02, 0.72, 0.52)
    blockers = blockers_for_card(
        0.0,
        2.0,
        face_regions=[],
        face_sampling="ok",
        fallback_face_box=fb,
        caption_box=caption(0.6, 0.64),
        placed=[],
    )
    assert fb in blockers


def test_text_window_moves_start_or_reports_overlap():
    assert shift_past_text_windows(0.0, 6.0, [(0.0, 2.0)], min_on_screen_s=1.5) == 2.0
    # chained windows
    assert shift_past_text_windows(0.0, 9.0, [(0.0, 2.0), (2.0, 4.0)], min_on_screen_s=1.5) == 4.0
    assert shift_past_text_windows(0.0, 3.0, [(0.0, 2.0)], min_on_screen_s=1.5) is None
    assert shift_past_text_windows(5.0, 8.0, [(0.0, 2.0)], min_on_screen_s=1.5) == 5.0


# --- grounding ----------------------------------------------------------------------------


def _open_session():
    raise AssertionError("the DB seam should be monkeypatched")


def _asset(id_, *, kind="image", aspect=16 / 9, analysis=None):
    return {
        "id": id_,
        "gcs_path": f"users/u1/pool/{id_}.png",
        "gcs_generation": "7",
        "kind": kind,
        "source_filename": f"{id_}.png",
        "duration_s": 12.0 if kind == "video" else None,
        "aspect": aspect,
        "user_context": "",
        "analysis": analysis or {},
    }


def _ground(monkeypatch, assets, placements, *, faces, card_layout, duration_s=80.0, clip="c.mp4"):
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        OverlayPlacementAgent,
        "run",
        lambda self, input, ctx=None: OverlayPlacementOutput(placements=placements, wishlist=[]),  # noqa: A002, ARG005
    )
    monkeypatch.setattr(pg, "sample_face_regions", faces)
    words = [
        {"text": f"w{i}", "start_s": i * 0.5, "end_s": i * 0.5 + 0.4, "confidence": 1.0}
        for i in range(int(duration_s * 2))
    ]
    return pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=words,
        duration_s=duration_s,
        clip_path=clip,
        card_layout=card_layout,
        video_supported=True,
    )


def _raw(asset_id, start, end, tier="likely"):
    return RawPlacement(
        asset_id=asset_id,
        slot="top",
        start_s=start,
        end_s=end,
        confidence_tier=tier,
        reason="r",
        transcript_anchor="",
    )


def _faces_at(boxes_by_time):
    def sample(_clip, anchors, **_kw):
        regions = [
            ProtectedRegion(max(0.0, t - 0.5), t + 0.5, boxes_by_time(t), kind="face")
            for t in anchors
        ]
        return regions, {"attempted": len(anchors), "detected": len(regions)}

    return sample


def _card_box(card, aspect):
    h = card.scale * CANVAS_RATIO / aspect
    return NormalizedBox(
        card.x_frac - card.scale / 2,
        card.y_frac - h / 2,
        card.x_frac + card.scale / 2,
        card.y_frac + h / 2,
    )


def test_cards_are_wide_off_the_face_the_caption_and_each_other(monkeypatch):
    assets = [_asset(f"a{i}") for i in range(8)]
    raw = [_raw(f"a{i}", 3.0 + 9 * i, 6.5 + 9 * i) for i in range(8)]
    band = (0.62, 0.72)
    result = _ground(
        monkeypatch,
        assets,
        raw,
        faces=_faces_at(lambda t: FACE if t < 40 else NormalizedBox(0.3, 0.34, 0.7, 0.58)),
        card_layout=CardLayoutContext(caption_band=band),
    )
    assert len(result.cards) == 8  # 8 > the old 4-per-card x3 face budget
    for card in result.cards:
        box = _card_box(card, 16 / 9)
        face = FACE if card.start_s < 40 else NormalizedBox(0.3, 0.34, 0.7, 0.58)
        assert card.caption_clear is True
        assert card.scale >= 0.7  # the widest the band under her captions allows
        assert not boxes_intersect(box, face), card
        assert not boxes_intersect(box, caption(*band)), card
        assert box.left >= 0 and box.right <= 1 and box.top >= 0 and box.bottom <= 1


def test_a_face_that_moves_later_is_protected_for_the_last_card(monkeypatch):
    assets = [_asset(f"a{i}") for i in range(10)]
    raw = [_raw(f"a{i}", 3.0 + 7 * i, 6.5 + 7 * i) for i in range(10)]
    result = _ground(
        monkeypatch,
        assets,
        raw,
        faces=_faces_at(lambda t: FACE if t < 40 else NormalizedBox(0.3, 0.34, 0.7, 0.58)),
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    late = [c for c in result.cards if c.start_s > 40]
    assert late
    for card in late:
        assert not boxes_intersect(_card_box(card, 16 / 9), NormalizedBox(0.3, 0.34, 0.7, 0.58))


def test_cards_sharing_time_never_stack(monkeypatch):
    assets = [_asset("a0"), _asset("a1")]
    raw = [_raw("a0", 0.0, 4.0, "confident"), _raw("a1", 1.0, 5.0, "confident")]
    result = _ground(
        monkeypatch,
        assets,
        raw,
        faces=_faces_at(lambda t: FACE),
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    assert len(result.cards) == 2  # the opening card (hook window) is kept at any tier
    a, b = (_card_box(c, 16 / 9) for c in result.cards)
    assert not boxes_intersect(a, b)


def test_failed_sampling_keeps_cards_off_the_fallback_box(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no opencv")

    result = _ground(
        monkeypatch,
        [_asset("a0")],
        [_raw("a0", 10.0, 14.0)],
        faces=boom,
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    assert result.receipt["face_sampling"] == "failed"
    assert len(result.cards) == 1
    assert not boxes_intersect(_card_box(result.cards[0], 16 / 9), pg._FALLBACK_FACE_BOX)


def test_alpha_sticker_stays_small_and_clamped(monkeypatch):
    result = _ground(
        monkeypatch,
        [_asset("s", aspect=1.0, analysis={"has_alpha": True})],
        [_raw("s", 10.0, 14.0)],
        faces=_faces_at(lambda t: FACE),
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.scale <= pg._DEFAULT_CARD_SCALE + 1e-9
    assert card.caption_clear is False
    assert card.y_frac <= CAPTION_BAND_TOP_FRAC
    assert not boxes_intersect(_card_box(card, 1.0), FACE)


def test_a_corner_card_is_never_pulled_onto_the_captions(monkeypatch):
    # The face fills the whole top half, so the sticker's only spots are low corners: clear of
    # the real caption row (0.60-0.64), but the clamp would pull any of them up to 0.62.
    big_face = NormalizedBox(0.05, 0.02, 0.95, 0.55)
    result = _ground(
        monkeypatch,
        [_asset("s", aspect=1.0, analysis={"has_alpha": True})],
        [_raw("s", 10.0, 14.0)],
        faces=_faces_at(lambda t: big_face),
        card_layout=CardLayoutContext(caption_band=(0.60, 0.64)),
    )
    for card in result.cards:
        box = _card_box(card, 1.0)
        assert not boxes_intersect(box, caption(0.60, 0.64)), box
        assert not boxes_intersect(box, big_face), box
        assert box.bottom <= 0.60 + 1e-9, box


def test_recording_plays_from_its_best_moment(monkeypatch):
    asset = _asset(
        "v",
        kind="video",
        analysis={"best_moments": [{"start_s": 3.0, "end_s": 9.0, "energy": 0.9}]},
    )
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)
    result = _ground(
        monkeypatch,
        [asset],
        [_raw("v", 10.0, 14.0)],
        faces=_faces_at(lambda t: FACE),
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    assert len(result.cards) == 1 and result.cards[0].kind == "video"
    assert result.cards[0].source_start_s > 0.0


def test_text_window_moves_a_card_or_reports_overlap(monkeypatch):
    assets = [_asset("a0"), _asset("a1")]
    raw = [_raw("a0", 2.0, 8.0), _raw("a1", 20.0, 22.0)]
    result = _ground(
        monkeypatch,
        assets,
        raw,
        faces=_faces_at(lambda t: FACE),
        card_layout=CardLayoutContext(
            caption_band=(0.62, 0.72), text_windows=((0.0, 4.0), (19.0, 21.0))
        ),
    )
    by_media = {c.media_id: c for c in result.cards}
    assert by_media["a0"].start_s >= 4.0
    assert "a1" not in by_media
    assert {"media_id": "a1", "reason": "overlap"}.items() <= next(
        u for u in result.receipt["unplaced"] if u["media_id"] == "a1"
    ).items()


def test_card_layout_none_is_todays_output(monkeypatch):
    assets = [_asset(f"a{i}", aspect=4 / 3) for i in range(4)]
    raw = [_raw(f"a{i}", 4.0 + 10 * i, 8.0 + 10 * i) for i in range(4)]
    kwargs = {"faces": _faces_at(lambda t: FACE)}
    without = _ground(monkeypatch, assets, raw, card_layout=None, **kwargs)
    assert all(c.caption_clear is False for c in without.cards)
    assert all(c.scale <= pg._DEFAULT_CARD_SCALE + 1e-9 for c in without.cards)
    assert all(c.y_frac <= CAPTION_BAND_TOP_FRAC for c in without.cards)
    assert all(c.source_start_s == 0.0 for c in without.cards)
    again = _ground(monkeypatch, assets, raw, card_layout=None, **kwargs)
    strip = lambda r: [  # noqa: E731
        (c.start_s, c.end_s, c.x_frac, c.y_frac, c.scale, c.kind) for c in r.cards
    ]
    assert strip(without) == strip(again)


# --- review follow-ups ---------------------------------------------------------------------

FB = NormalizedBox(0.28, 0.02, 0.72, 0.52)
LOW = NormalizedBox(0.3, 0.40, 0.7, 0.60)


def _blockers(regions, start=0.0, end=10.0, **kw):
    return blockers_for_card(
        start,
        end,
        face_regions=regions,
        face_sampling="ok",
        fallback_face_box=FB,
        caption_box=caption(0.62, 0.72),
        placed=[],
        **kw,
    )


def test_partial_coverage_adds_the_union_of_all_faces():
    # the in-time sample covers 2 s of a 10 s card; the head elsewhere sits lower
    regions = [ProtectedRegion(0, 2, FACE), ProtectedRegion(30, 31, LOW)]
    blockers = _blockers(regions)
    assert FACE in blockers
    assert NormalizedBox(0.3, 0.22, 0.7, 0.60) in blockers


def test_full_coverage_uses_only_the_in_time_faces():
    # Real samples are +-0.5 s around their moment; one every 1.5 s looks at the whole card.
    regions = [ProtectedRegion(t - 0.5, t + 0.5, FACE) for t in (0.5, 2.0, 3.5, 5.0, 6.5, 8.0, 9.5)]
    regions.append(ProtectedRegion(30, 31, LOW))
    blockers = _blockers(regions)
    assert LOW not in blockers and NormalizedBox(0.3, 0.22, 0.7, 0.60) not in blockers


def test_samples_two_seconds_apart_leave_gaps_and_add_the_union():
    regions = [ProtectedRegion(t - 0.5, t + 0.5, FACE) for t in (1.0, 3.0, 5.0, 7.0, 9.0)]
    regions.append(ProtectedRegion(30, 31, LOW))
    assert NormalizedBox(0.3, 0.22, 0.7, 0.60) in _blockers(regions)  # 7.5 s of 10 s < 90 %


def test_overlapping_samples_are_merged_not_double_counted():
    regions = [
        ProtectedRegion(0, 5, FACE),
        ProtectedRegion(1, 6, FACE),
        ProtectedRegion(30, 31, LOW),
    ]
    assert NormalizedBox(0.3, 0.22, 0.7, 0.60) in _blockers(regions)  # 6 s of 10 s < 90 %


def test_incomplete_sampling_with_no_in_time_face_adds_union_and_fallback():
    blockers = _blockers([ProtectedRegion(30, 31, LOW)], complete=False)
    assert LOW in blockers and FB in blockers


def test_complete_sampling_with_no_in_time_face_has_no_fallback_box():
    assert FB not in _blockers([ProtectedRegion(30, 31, LOW)])


def test_timed_out_sampling_is_reported_to_the_caller(monkeypatch):
    def partial(_clip, anchors, **_kw):
        return [ProtectedRegion(0, 1, FACE, kind="face")], {"timed_out": True, "partial": True}

    monkeypatch.setattr(pg, "sample_face_regions", partial)
    receipt: dict = {}
    pg.sample_card_faces(
        [{"start_s": 0.0, "end_s": 4.0}], clip_path="c.mp4", job_id="j", receipt_out=receipt
    )
    assert receipt["timed_out"] is True


def test_corner_fallback_protects_the_fallback_box_when_no_face_was_seen(monkeypatch):
    result = _ground(
        monkeypatch,
        [_asset("s", aspect=1.0, analysis={"has_alpha": True})],
        [_raw("s", 10.0, 14.0)],
        faces=lambda _c, anchors, **_k: ([], {"attempted": len(anchors), "detected": 0}),
        card_layout=CardLayoutContext(caption_band=(0.62, 0.72)),
    )
    assert result.receipt["face_sampling"] == "ok"
    # Strict and fail-closed: the sticker may find no safe spot, never one on the fallback box.
    assert len(result.cards) + len(result.receipt["unplaced"]) == 1
    for card in result.cards:
        assert not boxes_intersect(_card_box(card, 1.0), pg._FALLBACK_FACE_BOX)
