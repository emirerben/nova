"""KRI-1561 PR 5: the `sfx_ask` capability and its repair.

Failure modes, written before the tests:

1. The capability opens on a format whose writer cannot place effects (a montage with no voiceover,
   the cloud renderer), so the planner promises effects nothing plays.
2. It stays shut where the writer can (Talking, Voiceover, montage with a recorded voiceover), so
   the ask the creator made is dropped as "not available".
3. A closed gate changes the manifest: a new key on every manifest moves every context hash.
4. The model sets `sfx_ask` where it is closed and the plan keeps it, or drops it silently.
5. A dropped optional "sfx" treatment says "not available" next to an ask the writer will honour.
6. The "no moment was named" notice contradicts an ask the writer will place.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services import creator_capabilities as capabilities
from app.services.creator_sessions import _generic_sound_effect_notice
from tests.services.test_creator_capabilities import (
    PHONE_SUBTITLED_OVERLAY_FEATURES,
    PHONE_SUBTITLED_SFX_FEATURES,
    PHONE_VOICEOVER_EDITOR_FEATURES,
    _enable_guided,
    _enable_narrated_and_subtitled_flags,
    _enable_phone_subtitled_reaction_beats,
)

ASK = {"request_text": "Uygun yerlere ses efekti ekle", "max_placements": 4}
VOICE = {"gcs_path": "users/u/creation-threads/t/voice.m4a", "generation": "1", "duration_s": 47.4}


def _gates(monkeypatch, **overrides) -> None:
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    _enable_phone_subtitled_reaction_beats(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        [
            *PHONE_SUBTITLED_OVERLAY_FEATURES,
            *PHONE_SUBTITLED_SFX_FEATURES,
            *PHONE_VOICEOVER_EDITOR_FEATURES,
            "narrationAudio",
        ],
    )
    for name, value in overrides.items():
        monkeypatch.setattr(capabilities.settings, name, value)


def _manifest(monkeypatch, edit_format, *, voiceover=False, phone=True, clips=2, **overrides):
    _gates(monkeypatch, **overrides)
    ids = ["phone-a", "phone-b"][:clips]
    return capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=[{"media_id": media_id, "kind": "video"} for media_id in ids],
        has_voiceover=voiceover,
        narration=VOICE if voiceover else None,
        **({"phone_source_media_ids": ids, "phone_rendering_allowed": True} if phone else {}),
    )


def _open(manifest) -> bool:
    entry = manifest.capabilities.get(capabilities.CAPABILITY_SFX_ASK)
    return bool(entry is not None and entry.available)


@pytest.mark.parametrize(
    ("edit_format", "voiceover"),
    [("subtitled", False), ("narrated_planned", True), ("montage", True)],
    ids=["talking", "voiceover", "montage-with-voiceover"],
)
def test_open_where_a_phone_writer_can_place_effects(monkeypatch, edit_format, voiceover):
    assert _open(_manifest(monkeypatch, edit_format, voiceover=voiceover))


@pytest.mark.parametrize(
    ("edit_format", "voiceover", "phone"),
    [
        ("montage", False, True),
        ("day_vlog", False, True),
        ("subtitled", False, False),
        ("montage", True, False),
    ],
    ids=["montage-no-voice", "day-vlog-no-voice", "talking-cloud", "montage-voice-cloud"],
)
def test_shut_where_no_writer_can(monkeypatch, edit_format, voiceover, phone):
    manifest = _manifest(monkeypatch, edit_format, voiceover=voiceover, phone=phone)
    assert capabilities.CAPABILITY_SFX_ASK not in manifest.capabilities


@pytest.mark.parametrize(
    ("edit_format", "voiceover", "gate"),
    [
        ("subtitled", False, "phone_subtitled_editor_lanes_enabled"),
        ("subtitled", False, "sound_effects_enabled"),
        ("narrated_planned", True, "phone_voiceover_editor_lanes_enabled"),
        ("montage", True, "phone_voiceover_editor_lanes_enabled"),
        ("montage", True, "sound_effects_enabled"),
    ],
)
def test_each_writer_gate_closes_it(monkeypatch, edit_format, voiceover, gate):
    manifest = _manifest(monkeypatch, edit_format, voiceover=voiceover, **{gate: False})
    assert capabilities.CAPABILITY_SFX_ASK not in manifest.capabilities


def test_a_closed_gate_leaves_the_manifest_byte_identical(monkeypatch):
    """The key is omitted, not marked unavailable, so no closed-gate manifest or hash moves."""
    shut = _manifest(monkeypatch, "subtitled", phone_subtitled_editor_lanes_enabled=False)
    baseline = {k: v for k, v in shut.capabilities.items() if k != capabilities.CAPABILITY_SFX_ASK}
    assert shut.capabilities == baseline
    opened = _manifest(monkeypatch, "subtitled")
    assert set(opened.capabilities) - set(shut.capabilities) == {capabilities.CAPABILITY_SFX_ASK}


def _talking_strategy(**fields) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=["phone-a"],
        **fields,
    )


def test_an_ask_where_the_capability_is_open_is_kept_untouched(monkeypatch):
    manifest = _manifest(monkeypatch, "subtitled", clips=1)
    plan = capabilities.compile_strategy_to_plan(manifest, _talking_strategy(sfx_ask=ASK))

    assert plan.strategy.sfx_ask is not None
    assert plan.strategy.sfx_ask.request_text == ASK["request_text"]
    assert plan.strategy.sfx_ask.max_placements == 4
    assert capabilities.SFX_ASK_UNAVAILABLE_NOTICE not in plan.notices


def test_an_ask_where_it_is_shut_is_dropped_and_said(monkeypatch):
    manifest = _manifest(
        monkeypatch, "subtitled", clips=1, phone_subtitled_editor_lanes_enabled=False
    )
    plan = capabilities.compile_strategy_to_plan(manifest, _talking_strategy(sfx_ask=ASK))

    assert plan.strategy.sfx_ask is None
    assert capabilities.SFX_ASK_UNAVAILABLE_NOTICE in plan.notices


def test_no_ask_adds_no_notice(monkeypatch):
    manifest = _manifest(
        monkeypatch, "subtitled", clips=1, phone_subtitled_editor_lanes_enabled=False
    )
    plan = capabilities.compile_strategy_to_plan(manifest, _talking_strategy())
    assert capabilities.SFX_ASK_UNAVAILABLE_NOTICE not in plan.notices


def test_an_ask_survives_to_the_persisted_job_strategy(monkeypatch):
    """The workers read `all_candidates["creator_strategy"]`, a `model_dump(exclude_none=True)`."""
    manifest = _manifest(monkeypatch, "subtitled", clips=1)
    plan = capabilities.compile_strategy_to_plan(manifest, _talking_strategy(sfx_ask=ASK))
    persisted = plan.strategy.model_dump(mode="json", exclude_none=True)
    assert persisted["sfx_ask"] == ASK


def test_an_optional_sfx_treatment_says_nothing_next_to_an_open_ask(monkeypatch):
    open_manifest = _manifest(monkeypatch, "narrated_planned", voiceover=True)
    strategy = SimpleNamespace(optional_treatments=["sfx"])
    assert capabilities._dropped_treatment_notices(strategy, open_manifest) == []

    shut_manifest = _manifest(
        monkeypatch, "narrated_planned", voiceover=True, phone_voiceover_editor_lanes_enabled=False
    )
    assert capabilities._dropped_treatment_notices(strategy, shut_manifest) in (
        [capabilities.DROPPED_TREATMENT_NOTICES["sfx"]],
        [],  # the named-moment lane still covers sound on this edit
    )


def test_the_no_moment_notice_never_contradicts_an_ask(monkeypatch):
    manifest = _manifest(monkeypatch, "subtitled", clips=1)
    with_ask = capabilities.compile_strategy_to_plan(manifest, _talking_strategy(sfx_ask=ASK))
    without = capabilities.compile_strategy_to_plan(manifest, _talking_strategy())

    request = "add sound effects where they fit"
    assert _generic_sound_effect_notice(manifest, request, with_ask) is None
    assert (
        _generic_sound_effect_notice(manifest, request, without)
        == capabilities.GENERIC_SOUND_EFFECT_NOTICE
    )
