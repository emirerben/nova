from __future__ import annotations

import app.services.phone_reaction_grounding as grounding


def _open_session():
    raise AssertionError("database seams must be mocked")


def _asset(asset_id: str) -> dict:
    return {
        "id": asset_id,
        "gcs_path": f"users/owner/pool/{asset_id}.jpg",
        "gcs_generation": "7",
        "kind": "image",
        "source_filename": f"{asset_id}.jpg",
        "duration_s": None,
        "aspect": 1.0,
        "user_context": "",
        "analysis": {},
    }


def _ground(monkeypatch, beats: list[dict], words: list[dict], *, allow_fullscreen: bool):
    assets = [_asset(beat["visual_id"]) for beat in beats if beat.get("visual_id")]
    monkeypatch.setattr(grounding, "_load_ready_pool_assets", lambda *args, **kwargs: assets)
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *args, **kwargs: [])

    def geometry(overlays, **kwargs):
        return {overlay["id"]: overlay for overlay in overlays}, {}, "stubbed"

    monkeypatch.setattr(grounding, "resolve_phone_card_geometry", geometry)
    return grounding.ground_phone_reaction_beats(
        _open_session,
        job_id="job-1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=10,
        clip_path=None,
        allow_fullscreen=allow_fullscreen,
    )


def test_fullscreen_following_clip_uses_cue_timing_full_frame_and_receipt(monkeypatch):
    result = _ground(
        monkeypatch,
        [
            {
                "beat_id": "photo",
                "trigger": "show this",
                "visual_id": "photo-1",
                "display_mode": "fullscreen",
                "following_clip_id": "selected-video-1",
            }
        ],
        [
            {"text": "show", "start_s": 2.25, "end_s": 2.5},
            {"text": "this", "start_s": 2.5, "end_s": 2.75},
        ],
        allow_fullscreen=True,
    )

    card = result.cards[0]
    assert (card.start_s, card.end_s) == (2.25, 3.75)
    assert (card.x_frac, card.y_frac, card.scale, card.fade, card.display_mode) == (
        0.5,
        0.5,
        1.0,
        False,
        "fullscreen",
    )
    assert result.receipt["placed"] == [
        {
            "beat_id": "photo",
            "trigger": "show this",
            "at_s": 2.25,
            "end_s": 3.75,
            "visual_label": "photo-1.jpg",
            "card_id": "beat-photo-1",
            "display_mode": "fullscreen",
            "following_clip_id": "selected-video-1",
        }
    ]


def test_fullscreen_plain_photo_keeps_three_second_default_and_explicit_hold(monkeypatch):
    result = _ground(
        monkeypatch,
        [
            {
                "beat_id": "plain",
                "trigger": "plain",
                "visual_id": "photo-1",
                "display_mode": "fullscreen",
            },
            {
                "beat_id": "held",
                "trigger": "held",
                "visual_id": "photo-2",
                "display_mode": "fullscreen",
                "hold_s": 2.0,
            },
        ],
        [
            {"text": "plain", "start_s": 0, "end_s": 0.2},
            {"text": "held", "start_s": 4, "end_s": 4.2},
        ],
        allow_fullscreen=True,
    )

    assert [(card.start_s, card.end_s) for card in result.cards] == [(0.0, 3.0), (4.0, 6.0)]


def test_fullscreen_overlap_truncates_only_fullscreen_sequence_not_pip(monkeypatch):
    result = _ground(
        monkeypatch,
        [
            {
                "beat_id": "first",
                "trigger": "first",
                "visual_id": "one",
                "display_mode": "fullscreen",
            },
            {
                "beat_id": "second",
                "trigger": "second",
                "visual_id": "two",
                "display_mode": "fullscreen",
            },
            {"beat_id": "pip", "trigger": "first", "visual_id": "corner"},
        ],
        [
            {"text": "first", "start_s": 1, "end_s": 1.2},
            {"text": "second", "start_s": 2, "end_s": 2.2},
        ],
        allow_fullscreen=True,
    )

    cards = {card.id: card for card in result.cards}
    assert (cards["beat-first-1"].start_s, cards["beat-first-1"].end_s) == (1.0, 2.0)
    assert (cards["beat-second-1"].start_s, cards["beat-second-1"].end_s) == (2.0, 5.0)
    assert (cards["beat-pip-1"].start_s, cards["beat-pip-1"].end_s) == (1.0, 3.5)
    assert cards["beat-pip-1"].display_mode == "pip"


def test_fullscreen_is_explicitly_unplaced_when_not_enabled(monkeypatch):
    result = _ground(
        monkeypatch,
        [
            {
                "beat_id": "blocked",
                "trigger": "blocked",
                "visual_id": "photo-1",
                "display_mode": "fullscreen",
            }
        ],
        [{"text": "blocked", "start_s": 1, "end_s": 1.2}],
        allow_fullscreen=False,
    )

    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "blocked", "trigger": "blocked", "reason": "fullscreen_not_enabled"}
    ]


def test_default_pip_shape_is_unchanged(monkeypatch):
    result = _ground(
        monkeypatch,
        [{"beat_id": "pip", "trigger": "pip", "visual_id": "photo-1"}],
        [{"text": "pip", "start_s": 1, "end_s": 1.2}],
        allow_fullscreen=False,
    )

    card = result.cards[0]
    assert card.display_mode == "pip"
    assert card.fade is True
    assert card.model_dump(mode="json").get("display_mode") is None
    assert "display_mode" not in result.receipt["placed"][0]
