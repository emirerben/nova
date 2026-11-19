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
- (simulator) a bottom-band card under the Kria watermark the phone draws over every export
  (KRI-548: the mark's corner is brand-approved, things make room for it), so the mark is
  printed over the screenshot's text.
"""

from __future__ import annotations

import uuid

import pytest

import app.services.phone_overlay_grounding as pg
from app.agents.overlay_placement import OverlayPlacementAgent, OverlayPlacementOutput, RawPlacement
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC, SubtitledOverlayCard
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.background_text_focus import TextColumn
from app.services.phone_card_layout import (
    CANVAS_RATIO,
    EDGE_INSET,
    GAP,
    MIN_ZONE_HEIGHT,
    STRIP_WIDTH,
    WATERMARK_BOX,
    CardLayoutContext,
    CardPlacement,
    blockers_for_card,
    boxes_intersect,
    place_card,
    shift_past_text_windows,
    strip_crop,
    strip_for,
)
from app.services.phone_overlay_grounding import share_beat_windows

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


def test_watermark_box_is_the_phone_marks_keep_out():
    # Mirrors KriaBranding (x 60-193, y 1416-1475 on 1080x1920) plus the caption keep-out gap.
    assert WATERMARK_BOX.left < 60 / 1080 < 193 / 1080 < WATERMARK_BOX.right
    assert WATERMARK_BOX.top < 1416 / 1920 < 1475 / 1920 < WATERMARK_BOX.bottom


def test_a_bottom_card_makes_room_for_the_watermark_and_stays_big():
    spot = place_card(16 / 9, [FACE, caption(0.60, 0.64)], side_blockers=[WATERMARK_BOX])
    assert spot is not None and spot.zone == "bottom"
    assert not boxes_intersect(spot.box, WATERMARK_BOX), spot.box
    assert spot.scale >= 0.75, spot  # beside the mark, not shrunk under it
    assert spot.box.right <= 1.0 and spot.box.bottom <= 1.0


def test_the_watermark_changes_nothing_for_a_card_that_misses_it():
    blockers = [NormalizedBox(0.30, 0.40, 0.70, 0.66), caption(0.76, 0.82)]
    assert place_card(16 / 9, blockers, side_blockers=[WATERMARK_BOX]) == place_card(
        16 / 9, blockers
    )


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
        assert not boxes_intersect(box, WATERMARK_BOX), card
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


# --- KRI-1478: a wide card becomes a full-width strip cut to its text column ---------------
#
# Written BEFORE the code (her Q1 run 3, job 3ef3cbe5: five 1920x1080 README screenshots drawn
# whole, 0.51-0.60 wide above a face whose top is at 0.21, text 5 px tall, 0.15 s fades; all five
# screen recordings unplaced as "overlap"). Ways this goes wrong, each covered below:
# - S1 the strip is taller or lower than the card it replaces, so it reaches her hair or face;
# - S2 a portrait screenshot, a card moved beside the watermark, or one already full width is
#   stretched into a strip;
# - S3 no measurable text column (a photo, a diagram, no line pitch) still gets cropped;
# - S4 a narrow column is blown up past 2.5x, or a crop keeps fewer than three text lines;
# - S5 the crop leaves the picture (x + width > 1) or the drawn strip is not the band's height;
# - S6 the strip keeps the 0.15 s fade (no visual change for the kit scorer);
# - S7 a second visual for a mention is dropped as "overlap" although the first card can share;
# - S8 sharing leaves a part under 1.5 s, shares a window that is not a strip card's, shares one
#   window twice, or stacks two cards in time;
# - S9 the first card is cut short although the second card was never placed;
# - S10 with the switch off anything at all changes (cards, receipts, recipe).

FACE_RUN3 = NormalizedBox(0.22, 0.21, 0.84, 0.60)
CAPTIONS_RUN3 = (0.83, 0.89)
COLUMN = TextColumn(
    left=0.197, right=0.613, line_pitch=37 / 1080, source_width=1920, source_height=1080
)


def _run3_spot():
    blockers = [FACE_RUN3, caption(*CAPTIONS_RUN3)]
    return place_card(16 / 9, blockers, side_blockers=(WATERMARK_BOX,)), blockers


def test_strip_is_the_cards_own_band_made_full_width():
    spot, blockers = _run3_spot()
    assert spot.scale == pytest.approx(0.175 * (16 / 9) / CANVAS_RATIO)  # today: 0.553 wide
    strip = strip_for(spot, 16 / 9, blockers, side_blockers=(WATERMARK_BOX,))
    assert strip is not None and strip.scale == STRIP_WIDTH == pytest.approx(0.94)
    # S1: same top, same height, centred: only wider.
    assert (strip.x_frac, strip.y_frac, strip.height) == (0.5, spot.y_frac, spot.height)
    assert strip.box.bottom <= FACE_RUN3.top - 0.015 + 1e-9
    assert not any(boxes_intersect(strip.box, b) for b in [*blockers, WATERMARK_BOX])


def test_no_strip_for_a_portrait_and_no_wider_box_for_a_moved_or_full_width_card():
    spot, blockers = _run3_spot()
    assert strip_for(spot, 0.46, blockers) is None  # S2 portrait
    assert strip_for(spot, 1.0, blockers) is None  # square is not a page
    # S2: these keep their own box (only the picture is cut to its column, never the box moved)
    wide = place_card(6.0, blockers)  # a banner already 0.96 wide in the same band
    assert wide.scale == pytest.approx(0.96) and strip_for(wide, 6.0, blockers) is wide
    beside = CardPlacement(0.7, spot.y_frac, 0.4, "top", spot.height)  # beside the watermark
    assert strip_for(beside, 16 / 9, blockers) is beside
    mark = NormalizedBox(0.0, 0.03, 0.1, 0.08)  # a mark the wider strip would cover
    assert strip_for(spot, 16 / 9, blockers, side_blockers=(mark,)) is spot


def test_a_bottom_band_card_beside_the_watermark_becomes_a_centred_strip_past_it():
    # Her own layout (captions in the middle): the bottom band is the big one and the card
    # sits beside the Kria mark. The strip is centred below the mark instead of right-shifted.
    blockers = [NormalizedBox(0.30, 0.22, 0.70, 0.50), caption(0.58, 0.64)]
    spot = place_card(16 / 9, blockers, side_blockers=(WATERMARK_BOX,))
    assert spot.zone == "bottom" and spot.x_frac > 0.5
    strip = strip_for(spot, 16 / 9, blockers, side_blockers=(WATERMARK_BOX,))
    assert strip.x_frac == 0.5 and strip.scale == STRIP_WIDTH
    assert strip.box.top >= WATERMARK_BOX.bottom + GAP - 1e-9
    assert strip_crop(strip.scale, strip.height, COLUMN) is not None


# --- KRI-1478 follow-up (Q1 run 4): a strip beside the wordmark is centred, never right-shifted.
# Ways this goes wrong, each asserted below:
# - the strip is still right-shifted / narrower than STRIP_WIDTH (centre != 0.5);
# - it covers the wordmark, the caption box, a face, or another placed card;
# - it leaves the frame inset (bottom past 1 - EDGE_INSET) or is shorter than MIN_ZONE_HEIGHT;
# - a portrait / square picture gets a strip, or a card with nothing in the way is moved;
# - no room past the wordmark: today's beside-wordmark box must be kept unchanged;
# - the crop does not hold the measured text column or leaves the picture;
# - the lane row's dump changes for a strip that was not moved (extra ``y_frac`` key);
# - the compile draws the moved strip at the card's old height.
RUN4_CAPTION_BOTTOM = 0.725
RUN4_FACE = NormalizedBox(0.22, 0.22, 0.84, 0.60)


def _run4():
    blockers = [RUN4_FACE, caption(0.60, RUN4_CAPTION_BOTTOM)]
    spot = place_card(16 / 9, blockers, side_blockers=(WATERMARK_BOX,))
    return spot, blockers


def test_run4_today_the_card_sits_right_shifted_beside_the_wordmark():
    spot, _ = _run4()
    assert spot.zone == "bottom" and spot.x_frac == pytest.approx(0.592, abs=0.005)
    assert spot.scale == pytest.approx(0.759, abs=0.005)


def test_run4_strip_is_centred_and_clear_of_the_wordmark():
    spot, blockers = _run4()
    strip = strip_for(spot, 16 / 9, blockers, side_blockers=(WATERMARK_BOX,))
    assert strip is not spot
    assert (strip.x_frac, strip.scale) == (0.5, STRIP_WIDTH)
    assert not any(boxes_intersect(strip.box, b) for b in [*blockers, WATERMARK_BOX])
    box = strip.box
    assert box.top >= WATERMARK_BOX.bottom + GAP - 1e-9
    assert box.bottom <= 1 - EDGE_INSET + 1e-9 and strip.height >= MIN_ZONE_HEIGHT
    assert box.left >= 0 and box.right <= 1
    # same zone edge as before: it still reaches the bottom inset, item 15 is unchanged
    assert box.bottom == pytest.approx(spot.box.bottom)
    crop = strip_crop(strip.scale, strip.height, COLUMN)
    assert crop is not None
    x, y, w, h = crop
    assert x <= COLUMN.left and x + w >= COLUMN.right and y + h <= 1.0


def test_strip_keeps_the_beside_wordmark_box_when_no_room_is_left_past_it():
    # A tall mark leaves under MIN_ZONE_HEIGHT between it and the bottom inset.
    spot = CardPlacement(0.6, 0.86, 0.76, "bottom", 0.24)
    tall_mark = NormalizedBox(0.04, 0.74, 0.19, 0.93)
    assert strip_for(spot, 16 / 9, [], side_blockers=(tall_mark,)) is spot


def test_strip_never_moves_a_picture_that_is_not_wide_or_a_card_with_nothing_in_the_way():
    spot, blockers = _run4()
    assert strip_for(spot, 0.46, blockers, side_blockers=(WATERMARK_BOX,)) is None
    assert strip_for(spot, 1.0, blockers, side_blockers=(WATERMARK_BOX,)) is None
    assert strip_for(spot, 16 / 9, blockers) is spot  # no mark hit: unchanged


def test_a_moved_strip_row_carries_its_height_and_an_unmoved_one_dumps_as_before():
    from app.pipeline.phone_subtitled_lanes import SubtitledCardStrip

    base = {
        "id": "c1",
        "media_id": "m1",
        "gcs_path": "p",
        "generation": "1",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    plain = SubtitledOverlayCard(**base, strip=SubtitledCardStrip(scale=0.94, height=0.2))
    assert "y_frac" not in plain.model_dump()["strip"]
    moved = SubtitledOverlayCard(
        **base, strip=SubtitledCardStrip(scale=0.94, height=0.19, y_frac=0.88)
    )
    assert moved.model_dump()["strip"]["y_frac"] == 0.88


def test_strip_crop_is_the_text_column_at_the_bands_shape():
    spot, blockers = _run3_spot()
    crop = strip_crop(STRIP_WIDTH, spot.height, COLUMN)
    assert crop is not None
    x, y, width, height = crop
    assert (x, y) == (pytest.approx(0.177), 0.0)  # the column, 2 % of the page either side
    assert width == pytest.approx(0.456)
    assert x + width <= 1.0 and height <= 1.0  # S5
    # S5: drawn 0.94 wide, the crop is exactly as tall as the band it replaces.
    drawn_h = STRIP_WIDTH * CANVAS_RATIO * (height * 1080) / (width * 1920)
    assert drawn_h == pytest.approx(spot.height, abs=1e-4)
    # readable: a 37 px line pitch lands at ~43 px on the canvas (her own: 39-44 px)
    assert 37 * (STRIP_WIDTH * 1080) / (width * 1920) == pytest.approx(42.9, abs=0.5)


@pytest.mark.parametrize(
    "column",
    [
        None,
        COLUMN.model_copy(update={"line_pitch": None}),  # S3: not text
        COLUMN.model_copy(update={"source_width": None}),
        COLUMN.model_copy(update={"source_width": 1080, "source_height": 1920}),  # portrait
        COLUMN.model_copy(update={"line_pitch": 0.2}),  # S4: under three lines in the band
    ],
)
def test_no_crop_without_a_readable_landscape_column(column):
    assert strip_crop(STRIP_WIDTH, 0.175, column) is None


def test_a_narrow_column_is_not_zoomed_past_two_and_a_half_times():
    narrow = COLUMN.model_copy(update={"left": 0.45, "right": 0.55})
    x, _y, width, _h = strip_crop(STRIP_WIDTH, 0.175, narrow)
    assert width == pytest.approx(1 / 2.5)
    assert x == pytest.approx(0.5 - 0.2)  # still centred on the column
    edge = COLUMN.model_copy(update={"left": 0.0, "right": 0.1})
    assert strip_crop(STRIP_WIDTH, 0.175, edge)[0] == 0.0  # clamped inside the picture


def test_strip_switch_marks_wide_layout_cards_and_changes_nothing_else(monkeypatch):
    # A square picture is the tallest that still fits this 0.175 band as a layout card.
    assets = [_asset("wide"), _asset("square", aspect=1.0)]
    raw = [_raw("wide", 4.0, 8.0), _raw("square", 14.0, 18.0)]
    kwargs = {"faces": _faces_at(lambda t: FACE_RUN3)}
    off = _ground(
        monkeypatch,
        assets,
        raw,
        card_layout=CardLayoutContext(caption_band=CAPTIONS_RUN3),
        **kwargs,
    )
    on = _ground(
        monkeypatch,
        assets,
        raw,
        card_layout=CardLayoutContext(caption_band=CAPTIONS_RUN3, strip=True),
        **kwargs,
    )
    assert [c.strip for c in off.cards] == [None, None]  # S10
    assert all("strip" not in c.model_dump() for c in off.cards)
    by_media = {c.media_id: c for c in on.cards}
    assert by_media["square"].caption_clear and by_media["square"].strip is None  # S2
    strip = by_media["wide"].strip
    assert strip is not None and strip.scale == pytest.approx(0.94)
    assert strip.height == pytest.approx(by_media["wide"].scale * CANVAS_RATIO / (16 / 9))
    # Today's own geometry, window and fade stay on the card: the strip is extra.
    plain = lambda r: [  # noqa: E731
        (c.media_id, c.start_s, c.end_s, c.x_frac, c.y_frac, c.scale, c.fade) for c in r.cards
    ]
    assert plain(on) == plain(off)
    assert on.receipt == off.receipt and on.shared_windows == ()


def _run3_overlays(monkeypatch, *, shareable, occupied, raw=None):
    """The run-3 recordings pass: five 18 s 1920x1080 recordings the agent put on the same
    mentions the screenshots already hold."""
    assets = [_asset(f"r{i}", kind="video") for i in range(1, 6)]
    for asset in assets:
        asset["duration_s"] = 18.0
    raw = raw or [
        _raw("r1", 8.48, 12.84, "confident"),
        _raw("r2", 15.32, 20.98, "confident"),
        _raw("r3", 22.82, 29.4, "confident"),
        _raw("r4", 31.06, 35.98, "confident"),
        _raw("r5", 39.32, 44.92, "confident"),
    ]
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        OverlayPlacementAgent,
        "run",
        lambda self, input, ctx=None: OverlayPlacementOutput(placements=raw, wishlist=[]),  # noqa: A002, ARG005
    )
    monkeypatch.setattr(pg, "sample_face_regions", _faces_at(lambda t: FACE_RUN3))
    words = [
        {"text": f"w{i}", "start_s": i * 0.5, "end_s": i * 0.5 + 0.4, "confidence": 1.0}
        for i in range(97)
    ]
    return pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=words,
        duration_s=48.8,
        clip_path="c.mp4",
        card_layout=CardLayoutContext(
            caption_band=CAPTIONS_RUN3, text_windows=((0.0, 3.0),), strip=bool(shareable)
        ),
        video_supported=True,
        occupied=occupied,
        **({"shareable": shareable} if shareable else {}),
    )


# `phone_beat_receipt.placed` of job 3ef3cbe5 (source seconds) + the closing-text window.
BEAT_WINDOWS = [(8.38, 13.04), (15.22, 18.88), (22.72, 26.5), (30.96, 36.18), (39.22, 41.94)]
OCCUPIED = [*BEAT_WINDOWS, (45.8, 48.8)]


def test_today_every_recording_on_a_taken_mention_is_unplaced_as_overlap(monkeypatch):
    result = _run3_overlays(monkeypatch, shareable=[], occupied=OCCUPIED)
    assert result.cards == [] and result.shared_windows == ()
    assert [u["reason"] for u in result.receipt["unplaced"]] == ["overlap"] * 5


def test_a_second_visual_takes_the_second_half_of_a_strip_cards_window(monkeypatch):
    result = _run3_overlays(monkeypatch, shareable=BEAT_WINDOWS, occupied=OCCUPIED)
    windows = {c.media_id: (c.start_s, c.end_s) for c in result.cards}
    assert windows == {  # S7: each recording from the midpoint to the first card's end
        "r1": (10.71, 13.04),
        "r2": (17.05, 18.88),
        "r3": (24.61, 26.5),
        "r4": (33.57, 36.18),
        # S8 (Q1 run 4): the fifth mention is 2.72 s; two parts of 1.36 s are too short, so the
        # screenshot keeps all of it and the recording follows it to the end of its own window
        # (prod dropped it as "overlap" and the video never showed it).
        "r5": (41.94, 44.92),
    }
    assert all(c.kind == "video" and c.strip is not None for c in result.cards)
    assert result.receipt["unplaced"] == []
    assert [p["media_id"] for p in result.receipt["placed"]] == ["r1", "r2", "r3", "r4", "r5"]
    assert result.shared_windows == (
        (8.38, 13.04, 10.71),
        (15.22, 18.88, 17.05),
        (22.72, 26.5, 24.61),
        (30.96, 36.18, 33.57),
        (39.22, 41.94, 41.94),  # "midpoint" = its end: the screenshot keeps its whole window
    )


def test_only_a_strip_cards_window_is_shared_and_only_once(monkeypatch):
    # S8: the closing-text window and a non-strip beat stay whole.
    result = _run3_overlays(monkeypatch, shareable=BEAT_WINDOWS[:1], occupied=OCCUPIED)
    assert [c.media_id for c in result.cards] == ["r1"]
    assert len(result.receipt["unplaced"]) == 4
    # two recordings on one mention: the first takes the half, the second is still "overlap"
    twice = _run3_overlays(
        monkeypatch,
        shareable=BEAT_WINDOWS[:1],
        occupied=OCCUPIED,
        raw=[_raw("r1", 8.48, 12.84, "confident"), _raw("r2", 8.9, 12.0, "confident")],
    )
    assert [c.media_id for c in twice.cards] == ["r1"]
    assert twice.shared_windows == ((8.38, 13.04, 10.71),)
    # a visual that only starts in the second half belongs to what she says next
    late = _run3_overlays(
        monkeypatch,
        shareable=BEAT_WINDOWS[:1],
        occupied=OCCUPIED,
        raw=[_raw("r1", 11.5, 14.5, "confident")],
    )
    assert late.cards == [] and late.shared_windows == ()


def test_the_first_card_is_only_cut_short_for_a_card_that_was_placed(monkeypatch):
    # S9: the face fills the frame for the second half, so the recording finds no spot.
    monkeypatch.setattr(pg, "_FALLBACK_FACE_BOX", NormalizedBox(0.0, 0.0, 1.0, 1.0))
    assets = [_asset("r1", kind="video")]
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)
    raw = [_raw("r1", 8.48, 12.84, "confident")]
    monkeypatch.setattr(
        OverlayPlacementAgent,
        "run",
        lambda self, input, ctx=None: OverlayPlacementOutput(placements=raw, wishlist=[]),  # noqa: A002, ARG005
    )
    monkeypatch.setattr(
        pg, "sample_face_regions", lambda *a, **k: ([], {"attempted": 4, "detected": 0})
    )
    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=[{"text": "w", "start_s": 8.4, "end_s": 8.8, "confidence": 1.0}],
        duration_s=48.8,
        clip_path="c.mp4",
        card_layout=CardLayoutContext(caption_band=CAPTIONS_RUN3, strip=True),
        video_supported=True,
        occupied=OCCUPIED,
        shareable=BEAT_WINDOWS,
    )
    assert result.cards == [] and result.shared_windows == ()


def _strip_card(card_id, start, end):
    return SubtitledOverlayCard(
        id=card_id,
        media_id=f"m-{card_id}",
        gcs_path=f"users/u1/pool/{card_id}.png",
        generation="7",
        start_s=start,
        end_s=end,
        x_frac=0.5,
        y_frac=0.1075,
        scale=0.553,
        fade=True,
        caption_clear=True,
        strip={"scale": 0.94, "height": 0.175},
    )


def test_sharing_ends_the_first_card_and_its_receipt_row_at_the_midpoint():
    cards = [_strip_card("beat-a-1", 8.38, 13.04), _strip_card("beat-b-1", 15.22, 18.88)]
    receipt = {
        "version": 1,
        "placed": [
            {"beat_id": "a", "at_s": 8.38, "end_s": 13.04},
            {"beat_id": "b", "at_s": 15.22, "end_s": 18.88},
        ],
    }
    new_cards, new_receipt = share_beat_windows(cards, receipt, ((8.38, 13.04, 10.71),))
    assert [(c.start_s, c.end_s) for c in new_cards] == [(8.38, 10.71), (15.22, 18.88)]
    assert [p["end_s"] for p in new_receipt["placed"]] == [10.71, 18.88]
    assert receipt["placed"][0]["end_s"] == 13.04, "the caller's receipt is not mutated"
    # nothing shared: the very same objects come back
    assert share_beat_windows(cards, receipt, ()) == (cards, receipt)
    assert share_beat_windows(cards, None, ((8.38, 13.04, 10.71),))[1] is None


# --- KRI-1478 acceptance: the run-3 job, beats -> recordings -> compiled recipe -------------


def _run3_job(monkeypatch, *, strip: bool):
    import app.services.phone_reaction_grounding as rg
    from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.phone_sources import PhoneVisualBinding
    from tests.pipeline.test_phone_subtitled_plan import _binding

    names = ["speckit", "impeccable", "tradingagents", "marketplace", "gamestudios"]
    shots = [_asset(f"s{i}") for i in range(1, 6)]
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: shots)
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(pg, "sample_face_regions", _faces_at(lambda t: FACE_RUN3))
    layout = CardLayoutContext(
        caption_band=CAPTIONS_RUN3, text_windows=((0.0, 3.0),), **({"strip": True} if strip else {})
    )
    beats = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[
            {
                "id": f"b{i}",
                "trigger": name,
                "visual_id": f"s{i}",
                "visual_role": "photo",
                "hold_s": round(end - start, 2),
            }
            for i, (name, (start, end)) in enumerate(zip(names, BEAT_WINDOWS, strict=True), 1)
        ],
        closing=None,
        words=[
            {"text": name, "start_s": start, "end_s": start + 0.4, "confidence": 1.0}
            for name, (start, _end) in zip(names, BEAT_WINDOWS, strict=True)
        ],
        duration_s=48.8,
        clip_path="c.mp4",
        card_layout=layout,
    )
    beat_cards, beat_receipt = list(beats.cards), beats.receipt
    shareable = [(c.start_s, c.end_s) for c in beat_cards if c.strip is not None]
    recordings = _run3_overlays(
        monkeypatch,
        shareable=shareable,
        occupied=[*[(c.start_s, c.end_s) for c in beat_cards], (45.8, 48.8)],
    )
    beat_cards, beat_receipt = share_beat_windows(
        beat_cards, beat_receipt, recordings.shared_windows
    )
    cards = [*recordings.cards, *beat_cards]
    visuals = tuple(
        PhoneVisualBinding(
            media_id=card.media_id,
            gcs_path=card.gcs_path,
            generation=card.generation,
            sha256="b" * 64,
            byte_count=2048,
            text_column=COLUMN,
            **(
                {"kind": "video", "duration_s": 18.0, "width": 1920, "height": 1080}
                if card.kind == "video"
                else {}
            ),
        )
        for card in cards
    )
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=48.8),),
        caption_cues=[],
        visuals=visuals,
        lanes=PhoneSubtitledLanes(overlays=cards),
    )
    clips = next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips
    return cards, beat_receipt, recordings.receipt, recipe, clips


def test_run3_job_with_the_strip_on(monkeypatch):
    from app.kria.recipes_v2 import EditRecipeV2

    cards, beat_receipt, overlay_receipt, recipe, clips = _run3_job(monkeypatch, strip=True)

    assert len(clips) == 10, "five screenshots and her five recordings"
    for clip in clips:
        placement, crop = clip.visual_placement, clip.source_crop
        assert placement.width_fraction == pytest.approx(0.94) and placement.x_fraction == 0.5
        assert crop is not None and (crop.x, crop.y) == (pytest.approx(0.177), 0.0)
        assert (placement.fade_in, placement.fade_out) == (False, False)  # S6: hard cut
        height = 0.94 * CANVAS_RATIO * (crop.height * 1080) / (crop.width * 1920)
        box = NormalizedBox(
            0.03, placement.y_fraction - height / 2, 0.97, placement.y_fraction + height / 2
        )
        assert box.top >= 0.02 - 1e-6 and box.bottom <= FACE_RUN3.top - 0.015 + 1e-4  # her face
        assert box.bottom <= CAPTIONS_RUN3[0]  # her captions
        assert placement.window_start >= 3.0  # her title
        assert placement.window_end <= 45.8  # her closing text
    spans = sorted((c.visual_placement.window_start, c.visual_placement.window_end) for c in clips)
    assert all(a_end <= b_start + 1e-9 for (_, a_end), (b_start, _) in zip(spans, spans[1:]))
    assert spans == [
        (8.38, 10.71),
        (10.71, 13.04),
        (15.22, 17.05),
        (17.05, 18.88),
        (22.72, 24.61),
        (24.61, 26.5),
        (30.96, 33.57),
        (33.57, 36.18),
        (39.22, 41.94),
        (41.94, 44.92),  # Q1 run 4: the fifth recording follows its too-short mention
    ]
    assert [p["end_s"] for p in beat_receipt["placed"]] == [10.71, 17.05, 24.61, 33.57, 41.94]
    assert len(overlay_receipt["placed"]) == 5
    assert overlay_receipt["unplaced"] == []
    assert "sourceCrop" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_run3_job_with_the_strip_off_is_todays_job(monkeypatch):
    cards, beat_receipt, overlay_receipt, recipe, clips = _run3_job(monkeypatch, strip=False)

    assert len(clips) == 5 and all(c.kind == "image" and c.strip is None for c in cards)
    assert "strip" not in str([c.model_dump() for c in cards])
    for clip in clips:
        placement = clip.visual_placement
        assert clip.source_crop is None
        assert placement.width_fraction == pytest.approx(0.175 * (16 / 9) / CANVAS_RATIO)
        assert (placement.fade_in, placement.fade_out) == (True, True)
    assert [(p["at_s"], p["end_s"]) for p in beat_receipt["placed"]] == BEAT_WINDOWS
    assert overlay_receipt["placed"] == []
    assert [u["reason"] for u in overlay_receipt["unplaced"]] == ["overlap"] * 5
    assert "sourceCrop" not in recipe.required_capabilities
    assert "source_crop" not in recipe.model_dump_json()


def test_a_strip_window_is_shared_when_the_two_lists_differ_past_the_millisecond(monkeypatch):
    """KRI-1478 review: `occupied` (the beat windows) and `shareable` (the cards' own start/end)
    are two float lists; a difference in the 4th decimal must not turn the share into "overlap"."""
    drifted = [(start + 0.0004, end - 0.0004) for start, end in BEAT_WINDOWS[:1]]
    result = _run3_overlays(monkeypatch, shareable=drifted, occupied=OCCUPIED)
    assert [c.media_id for c in result.cards] == ["r1"]
    # ... and still only once: a second recording on that mention is "overlap".
    twice = _run3_overlays(
        monkeypatch,
        shareable=drifted,
        occupied=OCCUPIED,
        raw=[_raw("r1", 8.48, 12.84, "confident"), _raw("r2", 8.6, 12.84, "confident")],
    )
    assert [c.media_id for c in twice.cards] == ["r1"]
