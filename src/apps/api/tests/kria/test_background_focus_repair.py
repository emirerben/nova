"""Qendresa item 18 (chat half): the strategy names a side or a zoom for the recordings behind
the speaker (F2, F12-F14). Through the real schema, manifest and repair.
Failure modes: ``docs/reviews/background-focus-failure-modes.md``.
"""

from __future__ import annotations

import json
import math

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import BackgroundMedia
from app.config import settings
from app.services import creator_capabilities as capabilities
from app.services.client_protocol import bind_sending_build, unbind_sending_build
from tests.kria.test_speaker_background_chat import (
    POOL_PHOTO,
    POOL_VIDEO,
    _manifest,
    _open_windows,
    _repair,
    _sequence,
    _strategy,
)


def _open_focus(monkeypatch, *, enabled=True, token=True, colour=False):
    _open_windows(monkeypatch)
    monkeypatch.setattr(settings, "background_focus_enabled", enabled)
    extra = []
    if token:
        extra.append("backgroundFocus")
    if colour:
        extra.append("speakerBackgroundColor")
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *extra],
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
    [
        ("left", "left"),
        ("LEFT", "left"),
        (" Right ", "right"),
        ("center", "center"),
        ("centre", "center"),
        ("middle", "center"),
        ("top", None),
        ("", None),
        (None, None),
        (3, None),
        (True, None),
    ],
)
def test_focus_is_lenient(raw, want):
    assert BackgroundMedia(media_id=POOL_PHOTO, focus=raw).focus == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (1, 1.0),
        (1.5, 1.5),
        (0.1, 0.3),
        (9, 3.0),
        ("2", None),
        (None, None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        ("junk", None),
    ],
)
def test_focus_zoom_is_a_clamped_finite_number(raw, want):
    got = BackgroundMedia(media_id=POOL_PHOTO, focus_zoom=raw).focus_zoom
    assert got == want
    assert got is None or math.isfinite(got)


def test_unused_fields_are_omitted_from_every_dump():
    assert BackgroundMedia(media_id=POOL_PHOTO).model_dump(mode="json") == {"media_id": POOL_PHOTO}
    on = BackgroundMedia(media_id=POOL_PHOTO, focus="left", focus_zoom=1.4)
    assert on.model_dump(mode="json") == {
        "media_id": POOL_PHOTO,
        "focus": "left",
        "focus_zoom": 1.4,
    }
    s = _strategy(background_media={"media_id": POOL_PHOTO, "focus": "right"})
    assert CreativeStrategy.model_validate(s.model_dump(mode="json", exclude_none=True)) == s
    assert "focus" not in json.dumps(
        _strategy(background_media={"media_id": POOL_PHOTO}).model_dump(mode="json")
    )


# ------------------------------------------------------------------ capability


def test_capability_is_advertised_per_protocol(monkeypatch, build):
    _open_focus(monkeypatch)
    before = _manifest()
    assert capabilities.CAPABILITY_BACKGROUND_FOCUS == "background_focus"
    build(38)
    old = _manifest()
    assert not capabilities.background_focus_open(old)
    assert capabilities.CAPABILITY_BACKGROUND_FOCUS not in old.capabilities
    assert old.manifest_hash == before.manifest_hash
    build(39)
    new = _manifest()
    assert capabilities.background_focus_open(new)
    assert new.manifest_hash != old.manifest_hash


def test_capability_needs_no_windows(monkeypatch, build):
    from tests.kria.test_speaker_background_chat import _open_gate

    _open_gate(monkeypatch)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "backgroundFocus"],
    )
    build(39)
    assert capabilities.background_focus_open(_manifest())


def test_flag_off_or_token_missing_advertises_nothing(monkeypatch, build):
    build(39)
    _open_focus(monkeypatch, enabled=False)
    assert capabilities.CAPABILITY_BACKGROUND_FOCUS not in _manifest().capabilities
    _open_focus(monkeypatch, token=False)
    assert not capabilities.background_focus_open(_manifest())


# ------------------------------------------------------------------ repair


def test_the_repair_keeps_focus_in_the_media_branch(monkeypatch, build):
    _open_focus(monkeypatch)
    build(39)
    manifest = _manifest()
    asked = {"focus": "left", "focus_zoom": 1.6}
    kept, notices = _repair(manifest, _strategy(background_media={"media_id": POOL_PHOTO, **asked}))
    assert (kept.background_media.focus, kept.background_media.focus_zoom) == ("left", 1.6)
    assert notices == []
    seq, notices = _repair(
        manifest, _strategy(background_media=_sequence(POOL_PHOTO, POOL_VIDEO, **asked))
    )
    assert (seq.background_media.focus, seq.background_media.focus_zoom) == ("left", 1.6)
    assert notices == []


def test_the_repair_keeps_focus_on_a_colour_fill_with_windows_only(monkeypatch, build):
    _open_focus(monkeypatch, colour=True)
    build(39)
    manifest = _manifest()
    with_windows, notices = _repair(
        manifest,
        _strategy(
            background_media={
                "color": "#112233",
                "sequence": [{"media_id": POOL_PHOTO}],
                "focus": "right",
                "focus_zoom": 2.0,
            }
        ),
    )
    assert with_windows.background_media.color == "#112233"
    assert with_windows.background_media.focus == "right"
    assert with_windows.background_media.focus_zoom == 2.0
    assert notices == []
    bare, bare_notices = _repair(
        manifest, _strategy(background_media={"color": "#112233", "focus": "right"})
    )
    assert bare.background_media.focus is None and bare.background_media.focus_zoom is None
    assert bare_notices == []


def test_flag_off_drops_it_silently_and_changes_nothing_else(monkeypatch, build):
    _open_focus(monkeypatch, enabled=False)
    build(39)
    manifest = _manifest()
    kept, notices = _repair(
        manifest, _strategy(background_media={"media_id": POOL_PHOTO, "focus": "left"})
    )
    plain, plain_notices = _repair(manifest, _strategy(background_media={"media_id": POOL_PHOTO}))
    assert kept == plain and notices == plain_notices == []


def test_an_old_build_drops_it_with_a_notice(monkeypatch, build):
    _open_focus(monkeypatch)
    build(38)
    manifest = _manifest()
    kept, notices = _repair(
        manifest,
        _strategy(background_media={"media_id": POOL_PHOTO, "focus": "left", "focus_zoom": 1.5}),
    )
    assert kept.background_media.focus is None and kept.background_media.focus_zoom is None
    assert notices == [capabilities.BACKGROUND_FOCUS_NOTICES["app_too_old"]]
    assert "latest Kria app" in notices[0]
    build(39)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "backgroundFocus"],
    )
    _kept, unavailable = _repair(
        _manifest(), _strategy(background_media={"media_id": POOL_PHOTO, "focus_zoom": 1.5})
    )
    assert unavailable == [capabilities.BACKGROUND_FOCUS_NOTICES["unavailable"]]
    assert unavailable != notices


def test_nothing_asked_means_no_notice(monkeypatch, build):
    _open_focus(monkeypatch)
    build(38)
    _kept, notices = _repair(_manifest(), _strategy(background_media={"media_id": POOL_PHOTO}))
    assert notices == []


def test_turkish_twins_exist():
    from app.kria.strategy_policy import _KNOWN_TR

    for notice in capabilities.BACKGROUND_FOCUS_NOTICES.values():
        assert notice in _KNOWN_TR
