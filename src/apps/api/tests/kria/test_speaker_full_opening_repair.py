"""Qendresa item 16 (chat half): the strategy asks for a full-screen opening (S77-S79, S82).

Through the real schema, manifest and repair. Failure modes:
``docs/reviews/speaker-background-windows-failure-modes.md``.
"""

from __future__ import annotations

import json

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import BackgroundMedia
from app.config import settings
from app.services import creator_capabilities as capabilities
from app.services.client_protocol import bind_sending_build, unbind_sending_build
from tests.kria.test_speaker_background_chat import (
    FOREIGN,
    POOL_PHOTO,
    _manifest,
    _open_windows,
    _repair,
    _sequence,
    _strategy,
)


def _open_opening(monkeypatch, *, enabled=True, token=True):
    _open_windows(monkeypatch)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", enabled)
    if token:
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [*settings.phone_render_verified_features, "speakerFrameWindows"],
        )


@pytest.fixture
def build():
    tokens = []

    def _bind(value):
        tokens.append(bind_sending_build(value))

    yield _bind
    for token in reversed(tokens):
        unbind_sending_build(token)


# ------------------------------------------------------------------ schema


@pytest.mark.parametrize(
    ("raw", "want"),
    [(True, True), ("yes", True), ("true", True), (None, False), (2, False), ("junk", False)],
)
def test_full_screen_opening_is_lenient(raw, want):
    got = BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening=raw).full_screen_opening
    assert got is want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (7.2, 7.2),
        (1, 1.0),
        (15, 15.0),
        ("4", None),
        (None, None),
        (0.5, None),
        (20, None),
        (True, None),
        ("junk", None),
        (float("nan"), None),
    ],
)
def test_full_screen_opening_s_keeps_only_a_real_number_in_range(raw, want):
    assert (
        BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening_s=raw).full_screen_opening_s
        == want
    )


def test_a_valid_length_implies_the_opening():
    m = BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening_s=6.0)
    assert m.full_screen_opening is True
    assert (
        BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening_s=60).full_screen_opening is False
    )


def test_unused_fields_are_omitted_from_every_dump():
    plain = BackgroundMedia(media_id=POOL_PHOTO)
    assert plain.model_dump(mode="json") == {"media_id": POOL_PHOTO}
    on = BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening=True)
    assert on.model_dump(mode="json") == {"media_id": POOL_PHOTO, "full_screen_opening": True}
    timed = BackgroundMedia(media_id=POOL_PHOTO, full_screen_opening_s=5.5)
    assert timed.model_dump(mode="json") == {
        "media_id": POOL_PHOTO,
        "full_screen_opening": True,
        "full_screen_opening_s": 5.5,
    }
    s = _strategy(background_media={"media_id": POOL_PHOTO, "full_screen_opening": True})
    assert CreativeStrategy.model_validate(s.model_dump(mode="json", exclude_none=True)) == s
    assert "full_screen_opening" not in json.dumps(CreativeStrategy.model_json_schema())


# ------------------------------------------------------------------ manifest


def test_the_manifest_advertises_the_opening_only_for_a_bound_new_build(monkeypatch, build):
    _open_opening(monkeypatch, enabled=False)
    build(38)
    closed_flag = _manifest()
    assert capabilities.CAPABILITY_SPEAKER_FULL_OPENING not in closed_flag.capabilities
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    opened = _manifest()
    assert capabilities.speaker_full_opening_open(opened)
    assert opened.manifest_hash != closed_flag.manifest_hash
    build(35)
    old = _manifest()
    assert not capabilities.speaker_full_opening_open(old)
    assert capabilities.CAPABILITY_SPEAKER_FULL_OPENING not in old.capabilities
    assert old.manifest_hash == closed_flag.manifest_hash
    build(None)
    assert capabilities.CAPABILITY_SPEAKER_FULL_OPENING not in _manifest().capabilities


def test_no_opening_without_the_windows_capability(monkeypatch, build):
    from tests.kria.test_speaker_background_chat import _open_gate

    _open_gate(monkeypatch)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    build(38)
    assert not capabilities.speaker_full_opening_open(_manifest())


# ------------------------------------------------------------------ repair


def test_the_repair_keeps_the_opening_while_open_for_both_fills(monkeypatch, build):
    _open_opening(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackgroundColor"],
    )
    build(38)
    manifest = _manifest()
    asked = {"full_screen_opening": True, "full_screen_opening_s": 6.5}
    media, notices = _repair(
        manifest, _strategy(background_media={"media_id": POOL_PHOTO, **asked})
    )
    assert media.background_media.full_screen_opening is True
    assert media.background_media.full_screen_opening_s == 6.5
    assert notices == []
    color, _ = _repair(
        manifest, _strategy(background_media={"color": "#112233", "full_screen_opening": True})
    )
    assert color.background_media.full_screen_opening is True
    assert color.background_media.full_screen_opening_s is None
    seq, _ = _repair(manifest, _strategy(background_media=_sequence(POOL_PHOTO, **asked)))
    assert seq.background_media.full_screen_opening_s == 6.5


def test_flag_off_drops_it_silently_and_changes_nothing_else(monkeypatch, build):
    _open_opening(monkeypatch, enabled=False)
    build(38)
    manifest = _manifest()
    kept, notices = _repair(
        manifest,
        _strategy(background_media={"media_id": POOL_PHOTO, "full_screen_opening": True}),
    )
    plain, plain_notices = _repair(manifest, _strategy(background_media={"media_id": POOL_PHOTO}))
    assert kept == plain and notices == plain_notices == []


def test_an_old_build_drops_it_with_a_notice(monkeypatch, build):
    _open_opening(monkeypatch)
    build(35)
    manifest = _manifest()
    kept, notices = _repair(
        manifest,
        _strategy(
            background_media={
                "media_id": POOL_PHOTO,
                "full_screen_opening": True,
                "full_screen_opening_s": 6.0,
            }
        ),
    )
    assert kept.background_media.full_screen_opening is False
    assert kept.background_media.full_screen_opening_s is None
    assert len(notices) == 1 and notices[0].startswith(
        "Couldn't start you full screen before the background: "
    )
    assert "app" in notices[0]
    build(38)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "speakerFrameWindows"],
    )
    _kept, unavailable = _repair(
        _manifest(),
        _strategy(background_media={"media_id": POOL_PHOTO, "full_screen_opening": True}),
    )
    assert len(unavailable) == 1 and unavailable[0] != notices[0]


def test_no_background_means_no_opening(monkeypatch, build):
    _open_opening(monkeypatch)
    build(38)
    manifest = _manifest()
    gone, notices = _repair(
        manifest,
        _strategy(background_media={"media_id": FOREIGN, "full_screen_opening": True}),
    )
    assert gone.background_media is None
    assert not any("full screen before" in n for n in notices)
