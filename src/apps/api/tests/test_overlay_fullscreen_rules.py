"""Plan 009 T2: fullscreen takeover enforcement — rules (a)–(h), kill switch,
manual-path contract (E4/E9), apply receipt (ARCH-4), stale-bake skip (E5).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.overlay_autoplace import (
    _FS_MAX_IMAGE_S,
    _FS_MAX_PER_VIDEO,
    _FS_MAX_VIDEO_S,
    _FS_MIN_S,
    _shape_fullscreen_window,
    build_suggestions,
    resolve_slot,
)


def _asset(
    *,
    kind="image",
    aspect=0.5625,
    width=1080,
    height=1920,
    duration_s=None,
    with_dims=True,
) -> dict:
    analysis = {"subject": "demo", "analysis_version": 3}
    if with_dims:
        analysis["width"], analysis["height"] = width, height
    return {
        "id": "a1",
        "gcs_path": "users/u/plan/p/pool/x.png",
        "kind": kind,
        "source_filename": "x.png",
        "duration_s": duration_s,
        "aspect": aspect,
        "analysis": analysis,
    }


def _shape(**kw):
    defaults = dict(
        start=10.0,
        end=20.0,
        asset=_asset(),
        duration_s=60.0,
        intro_windows=[],
        caption_cues=[],
        fullscreen_count=0,
        taken=[],
    )
    defaults.update(kw)
    return _shape_fullscreen_window(**defaults)


class TestShapeFullscreenWindow:
    def test_eligible_portrait_image_clamps_to_image_cap(self):
        start, end = _shape()
        assert start == 10.0
        assert end - start == pytest.approx(_FS_MAX_IMAGE_S)

    def test_video_clamps_to_video_cap(self):
        start, end = _shape(asset=_asset(kind="video", duration_s=30.0))
        assert end - start == pytest.approx(_FS_MAX_VIDEO_S)

    def test_zero_freeze_video_window_capped_to_footage(self):
        start, end = _shape(asset=_asset(kind="video", duration_s=2.0))
        assert end - start == pytest.approx(2.0)

    def test_cap_demotes(self):
        assert _shape(fullscreen_count=_FS_MAX_PER_VIDEO) == "cap"

    def test_missing_aspect_demotes(self):
        assert _shape(asset=_asset(aspect=None)) == "no_metadata"

    def test_panorama_demotes(self):
        assert _shape(asset=_asset(aspect=2.5)) == "panorama"

    def test_missing_dims_fails_closed(self):
        # Rule (g): no persisted dims (legacy / stub assets) → demote, never guess.
        assert _shape(asset=_asset(with_dims=False)) == "no_dims"

    def test_low_res_demotes(self):
        assert _shape(asset=_asset(width=640, height=360, aspect=1.7778)) == "low_res"

    def test_hook_window_shifts_start(self):
        start, _end = _shape(start=0.5, end=4.0)
        assert start == pytest.approx(2.5)

    def test_intro_window_shifts_start(self):
        start, _end = _shape(start=1.0, end=8.0, intro_windows=[(0.0, 3.4)])
        assert start == pytest.approx(3.4)

    def test_intro_starting_mid_window_shortens_tail(self):
        start, end = _shape(start=5.0, end=9.0, intro_windows=[(7.0, 10.0)])
        assert start == pytest.approx(5.0)
        assert end == pytest.approx(7.0)

    def test_cue_overlap_shortens_to_cue_start(self):
        # Rule (h): cue at 12.5s — window trims to it (≥ floor away from start).
        start, end = _shape(
            start=10.0,
            end=14.0,
            caption_cues=[{"start_s": 12.5, "end_s": 13.5}],
        )
        assert (start, end) == (pytest.approx(10.0), pytest.approx(12.5))

    def test_cue_overlap_inside_floor_shrinks_to_floor(self):
        # Cue starts 0.5s in — can't trim to it; shrink toward the 1.5s floor.
        start, end = _shape(
            start=10.0,
            end=14.0,
            caption_cues=[{"start_s": 10.5, "end_s": 13.0}],
        )
        assert end - start == pytest.approx(_FS_MIN_S)

    def test_window_too_short_demotes(self):
        assert _shape(start=59.4, end=60.0, duration_s=60.0) == "window_too_short"

    def test_reanchor_gap_demotes(self):
        # Existing card ends 0.5s before our start — inside the 1.0s gap.
        assert _shape(taken=[(8.0, 9.6)]) == "gap"


def _raw(slot="full", start=10.0, end=14.0, sfx="pop_in"):
    p = MagicMock()
    p.asset_id = "a1"
    p.slot = slot
    p.start_s = start
    p.end_s = end
    p.confidence_tier = "confident"
    p.reason = "You say it."
    p.transcript_anchor = "it"
    p.sfx_intent = sfx
    return p


_WORDS = [{"word": "it", "start_s": 10.0, "end_s": 10.3}]


def _build(placements, *, assets=None, fullscreen_enabled=True, stats=None, **kw):
    return build_suggestions(
        placements,
        assets_by_id={"a1": assets or _asset()},
        words=_WORDS,
        duration_s=60.0,
        occupied=kw.pop("occupied", []),
        glossary=kw.pop("glossary", []),
        fullscreen_enabled=fullscreen_enabled,
        stats=stats,
        **kw,
    )


class TestBuildSuggestionsFullscreen:
    def test_eligible_full_slot_emits_fullscreen(self):
        stats = {}
        out = _build([_raw()], stats=stats)
        assert len(out) == 1
        ov = out[0]["overlay"]
        assert ov["display_mode"] == "fullscreen"
        assert (ov["x_frac"], ov["y_frac"], ov["scale"]) == (0.5, 0.5, 1.0)
        assert stats["fullscreen_emitted"] == 1

    def test_kill_switch_off_is_legacy_byte_identical(self):
        # Flag off: slot "full" goes through resolve_slot's legacy band-fit.
        out = _build([_raw()], fullscreen_enabled=False)
        assert len(out) == 1
        ov = out[0]["overlay"]
        assert ov["display_mode"] == "pip"
        legacy = resolve_slot("full", 0.5625)
        assert ov["scale"] == pytest.approx(legacy.scale)
        assert ov["y_frac"] == pytest.approx(legacy.y_frac)

    def test_default_is_off(self):
        out = build_suggestions(
            [_raw()],
            assets_by_id={"a1": _asset()},
            words=_WORDS,
            duration_s=60.0,
            occupied=[],
            glossary=[],
        )
        assert out[0]["overlay"]["display_mode"] == "pip"

    def test_ineligible_demotes_to_centered_pip(self):
        stats = {}
        out = _build([_raw()], assets=_asset(with_dims=False), stats=stats)
        assert len(out) == 1
        ov = out[0]["overlay"]
        assert ov["display_mode"] == "pip"
        centered = resolve_slot("center", 0.5625)
        assert ov["y_frac"] == pytest.approx(centered.y_frac)
        assert stats["fullscreen_demoted"] == 1
        assert stats["demote_reasons"] == ["no_dims"]

    def test_max_two_takeovers_third_demotes(self):
        stats = {}
        out = _build(
            [
                _raw(start=10.0, end=13.0),
                _raw(start=20.0, end=23.0),
                _raw(start=30.0, end=33.0),
            ],
            stats=stats,
        )
        modes = [s["overlay"]["display_mode"] for s in out]
        assert modes.count("fullscreen") == 2
        assert modes.count("pip") == 1
        assert stats["fullscreen_demoted"] == 1

    def test_fullscreen_default_sfx_is_whoosh(self):
        glossary = [
            {"id": "g1", "name": "Pop", "audio_gcs_path": "sound-effects/pop.mp3"},
            {"id": "g2", "name": "Whoosh", "audio_gcs_path": "sound-effects/whoosh.mp3"},
        ]
        out = _build([_raw(sfx="pop_in")], glossary=glossary)
        assert out[0]["sfx"]["sound_effect_id"] == "g2"

    def test_reanchor_gap_reserved_in_taken(self):
        # Second card 0.5s after the takeover ends → inside the reserved gap.
        out = _build(
            [
                _raw(start=10.0, end=13.0),
                _raw(slot="top", start=13.0, end=16.0),
            ]
        )
        # First becomes fullscreen (10-12.5 image cap), second overlaps the
        # padded interval and is dropped by the plain overlap check.
        assert len(out) == 1
        assert out[0]["overlay"]["display_mode"] == "fullscreen"


# ── E4 + E9: the manual-path contract helper ─────────────────────────────────

from app.agents._schemas.media_overlay import MediaOverlay  # noqa: E402
from app.services.overlay_apply import (  # noqa: E402
    apply_suggestions_to_variant,
    validate_fullscreen_constraints,
)


def _card(display_mode="pip", start=0.0, end=3.0, id_=None) -> MediaOverlay:
    return MediaOverlay.model_validate(
        {
            "id": id_ or uuid.uuid4().hex,
            "kind": "image",
            "src_gcs_path": "users/u/plan/p/overlays/x.png",
            "display_mode": display_mode,
            "start_s": start,
            "end_s": end,
        }
    )


class TestValidateFullscreenConstraints:
    def test_pip_overlap_stays_legal(self):
        # Regression pin (E4): z-ordered pip stacking is a supported state.
        validate_fullscreen_constraints(
            [_card(start=0, end=5), _card(start=2, end=7)], {"variant_id": "original_text"}
        )

    def test_card_overlapping_fullscreen_rejected(self):
        with pytest.raises(ValueError, match="overlap a full-screen moment"):
            validate_fullscreen_constraints(
                [_card("fullscreen", 5, 8), _card("pip", 7, 10)],
                {"variant_id": "original_text"},
            )

    def test_fullscreen_overlapping_card_rejected_other_direction(self):
        with pytest.raises(ValueError, match="overlap a full-screen moment"):
            validate_fullscreen_constraints(
                [_card("pip", 3, 6), _card("fullscreen", 5, 8)],
                {"variant_id": "original_text"},
            )

    def test_lyrics_variant_rejects_fullscreen(self):
        with pytest.raises(ValueError, match="lyric edits"):
            validate_fullscreen_constraints(
                [_card("fullscreen", 5, 8)], {"variant_id": "song_lyrics"}
            )

    def test_lyrics_text_mode_rejects_fullscreen(self):
        with pytest.raises(ValueError, match="lyric edits"):
            validate_fullscreen_constraints(
                [_card("fullscreen", 5, 8)],
                {"variant_id": "v1", "text_mode": "lyrics"},
            )

    def test_lyrics_variant_allows_pip(self):
        validate_fullscreen_constraints([_card()], {"variant_id": "song_lyrics"})

    def test_non_overlapping_fullscreen_ok(self):
        validate_fullscreen_constraints(
            [_card("fullscreen", 5, 8), _card("pip", 9, 12)],
            {"variant_id": "original_text"},
        )


# ── ARCH-4: the apply receipt ─────────────────────────────────────────────────


def _envelope(start: float, end: float) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "asset_id": "a1",
        "confidence_tier": "confident",
        "reason": "r",
        "overlay": {
            "id": uuid.uuid4().hex,
            "kind": "image",
            "src_gcs_path": "users/u/plan/i/pool/x.png",
            "start_s": start,
            "end_s": end,
        },
        "sfx": None,
    }


def _job(variant: dict) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.assembly_plan = {"variants": [variant]}
    return job


class TestApplyReceipt:
    def test_dropped_cards_write_receipt(self):
        variant = {
            "variant_id": "original_text",
            "media_overlays": [{"start_s": 2.0, "end_s": 7.0}],
            "sound_effects": None,
        }
        job = _job(variant)
        with patch("app.routes.generative_jobs.dispatch_set_media_overlays"):
            apply_suggestions_to_variant(
                job, "original_text", [_envelope(3.0, 6.0), _envelope(10.0, 13.0)], user_id="u"
            )
        receipt = variant["overlay_apply_receipt"]
        assert receipt["dropped"] == 1
        assert receipt["reason"] == "overlap"
        assert receipt["at"]

    def test_clean_apply_clears_receipt(self):
        variant = {
            "variant_id": "original_text",
            "media_overlays": None,
            "sound_effects": None,
            "overlay_apply_receipt": {"dropped": 2},
        }
        job = _job(variant)
        with patch("app.routes.generative_jobs.dispatch_set_media_overlays"):
            apply_suggestions_to_variant(job, "original_text", [_envelope(3.0, 6.0)], user_id="u")
        assert variant["overlay_apply_receipt"] is None
