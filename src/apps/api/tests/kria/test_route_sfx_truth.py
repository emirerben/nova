"""KRI-1561: what Kria says about sound effects comes from what the edit can play.

Ways the line could be wrong, written down before the tests:

1. The unified montage runs through the guided worker, which plays a licensed effect while
   `phone_guided_sfx_supported` holds, yet its card said "I can't add sound effects ... none will
   be played" with every gate open.
2. The reason it names is a copy of the gate list that drifted from the predicate the worker reads
   (a gate closed with the line saying nothing, or open with a limit named).
3. A closed gate gets the generic "on this video yet" line instead of the gate that is closed.
4. A route that never plays the effect (voice-over montage, lip-sync, slides, spoken excerpts)
   starts answering as if it did.
5. The Turkish catch-all reads as "because the video has speech" again.
6. A limit has no Turkish sentence, or the same sentence as another limit.
7. An optional treatment the manifest cannot render vanishes from the plan with no line, or a
   named-moment sound lane says "not available" next to its own notice.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.reply_language import reply_language_for
from app.kria.route_capabilities import RouteId
from app.kria.route_disclosure import strategy_notices
from app.services.sfx_capability import guided_sfx_limit, limit_line
from app.services.sfx_chooser import SfxLimit

LICENSED = {"effect_id": "fah", "semantics": "funny_moments", "max_placements": 3}
FEATURES = ("soundEffects", "audioMix")


@pytest.fixture(autouse=True)
def _gates_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_route_disclosure_enabled", True, raising=False)
    monkeypatch.setattr(settings, "route_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kria_honest_reply_lines_enabled", True, raising=False)
    monkeypatch.setattr(settings, "sound_effects_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_guided_sfx_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(FEATURES), raising=False)


def _sfx_line(route: RouteId) -> str | None:
    notices = strategy_notices({"licensed_sfx": LICENSED}, route=str(route))
    lines = [n.text for n in notices if n.subject == "licensed_sfx"]
    return lines[0] if lines else None


def test_open_gates_leave_no_not_played_line_on_the_guided_montage() -> None:
    assert _sfx_line(RouteId.UNIFIED_MONTAGE) is None


def test_the_named_limit_follows_the_predicate_for_every_gate_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.phone_rollout import phone_guided_sfx_supported

    for effects, lane, verified in itertools.product((True, False), (True, False), (True, False)):
        monkeypatch.setattr(settings, "sound_effects_enabled", effects, raising=False)
        monkeypatch.setattr(settings, "phone_guided_sfx_enabled", lane, raising=False)
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            list(FEATURES) if verified else [],
            raising=False,
        )
        limit = guided_sfx_limit()
        assert (limit is None) == phone_guided_sfx_supported(), (effects, lane, verified)
        if limit is not None:
            assert _sfx_line(RouteId.UNIFIED_MONTAGE) == limit_line(limit)


def test_each_closed_gate_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_guided_sfx_enabled", False, raising=False)
    assert guided_sfx_limit() is SfxLimit.flag_off
    monkeypatch.setattr(settings, "phone_guided_sfx_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["audioMix"], raising=False)
    assert guided_sfx_limit() is SfxLimit.feature_unverified
    assert limit_line(SfxLimit.flag_off) != limit_line(SfxLimit.feature_unverified)
    assert limit_line(SfxLimit.feature_unverified) != limit_line(SfxLimit.format_unsupported)


def test_with_the_honest_lines_switch_off_the_old_line_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_honest_reply_lines_enabled", False, raising=False)
    assert _sfx_line(RouteId.UNIFIED_MONTAGE) == limit_line(SfxLimit.format_unsupported)


@pytest.mark.parametrize(
    "route",
    [
        RouteId.VOICEOVER_MONTAGE,
        RouteId.TALKING_HEAD,
        RouteId.NARRATED,
        RouteId.LIPSYNC_SONG,
        RouteId.SPEECH_EXCERPT,
        RouteId.SLIDE_POST,
    ],
)
def test_routes_whose_writer_does_not_read_the_effect_still_say_so(route: RouteId) -> None:
    """These writers never read `licensed_sfx` (Talking and narrated place named cues from
    `reaction_beats`; the others have no effects track): their line must not go quiet."""
    assert _sfx_line(route)


def test_every_limit_has_its_own_english_and_turkish_sentence() -> None:
    english = []
    for limit in SfxLimit:
        with reply_language_for("en"):
            en = limit_line(limit)
        with reply_language_for("tr"):
            tr = limit_line(limit)
        assert en and tr and tr != en, limit
        english.append(en)
    assert len(set(english)) == len(english), "two limits share one sentence"


def test_the_turkish_catch_all_no_longer_reads_as_because_of_speech() -> None:
    from app.kria import brief_checks as bc

    with reply_language_for("tr"):
        assert "Konuşmalı" not in bc._loc(bc._TALKING_NO_SUCH_EDIT)
    with reply_language_for("en"):
        assert bc._loc(bc._TALKING_NO_SUCH_EDIT) == bc._TALKING_NO_SUCH_EDIT


def _manifest(**available: bool) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities={name: SimpleNamespace(available=value) for name, value in available.items()}
    )


def test_a_dropped_optional_treatment_is_said_once() -> None:
    from app.services.creator_capabilities import (
        DROPPED_TREATMENT_NOTICES,
        _dropped_treatment_notices,
    )

    strategy = SimpleNamespace(optional_treatments=["sfx", "sfx", "transitions", "overlays"])
    notices = _dropped_treatment_notices(
        strategy, _manifest(sound_effects=False, transitions=True, media_overlays=False)
    )
    assert notices == [DROPPED_TREATMENT_NOTICES["sfx"], DROPPED_TREATMENT_NOTICES["overlays"]]


def test_a_named_moment_sound_lane_adds_no_second_sound_line() -> None:
    from app.services.creator_capabilities import _dropped_treatment_notices

    strategy = SimpleNamespace(optional_treatments=["sfx"])
    manifest = _manifest(sound_effects=False, reaction_beats=True)
    assert _dropped_treatment_notices(strategy, manifest) == []


def test_every_new_notice_has_turkish() -> None:
    from app.kria.strategy_policy import localize_known_message
    from app.services.creator_capabilities import (
        DROPPED_TREATMENT_NOTICES,
        GENERIC_SOUND_EFFECT_NOTICE,
    )

    for text in (GENERIC_SOUND_EFFECT_NOTICE, *DROPPED_TREATMENT_NOTICES.values()):
        with reply_language_for("en"):
            assert localize_known_message(text) == text
        with reply_language_for("tr"):
            assert localize_known_message(text) != text, text
