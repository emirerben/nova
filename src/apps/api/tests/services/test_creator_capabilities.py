"""Tests for server-derived Main Creator Agent capabilities."""

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, CreatorEditSnapshot
from app.agents._schemas.creator_policy import (
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    PhoneFormatUnavailableError,
    PhoneMediaUnavailableError,
)
from app.schemas.edit_proposal import MixedMediaTimingProfile, MontageCadenceConstraint
from app.services import creator_capabilities as capabilities
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError
from app.services.creator_sessions import compile_active_plan
from app.services.phone_rollout import (
    PHONE_SUBTITLED_OVERLAY_FEATURES,
    PHONE_SUBTITLED_SFX_FEATURES,
    PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES,
)


def _enable_guided(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", True)
    for name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(capabilities.settings, name, True, raising=False)


def test_guided_policy_is_used_and_manifest_is_deterministic(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    kwargs = {
        "item_id": "item-1",
        "edit_format": "montage",
        "media": [{"media_id": "clip-1", "kind": "video", "duration_s": 3.0}],
        "catalog": [{"catalog_id": "song-1", "kind": "music"}],
    }
    first = capabilities.resolve_creator_manifest(**kwargs)
    second = capabilities.resolve_creator_manifest(**kwargs)

    assert first.render_program == "guided"
    assert first.capabilities[capabilities.CAPABILITY_GUIDED_STORY].available is True
    assert first.capabilities[capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL].available is True
    assert first.context_hash == second.context_hash
    assert first.manifest_hash == second.manifest_hash


def test_async_clip_duration_does_not_change_confirmation_identity(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    pending = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video", "duration_s": None}],
    )
    analyzed = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video", "duration_s": 26.433}],
    )

    assert pending.context_hash == analyzed.context_hash
    assert pending.manifest_hash == analyzed.manifest_hash


def test_audio_led_and_voiceover_items_are_native(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="talking_head",
        has_voiceover=False,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    assert manifest.render_program == "native"
    guided = manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY]
    assert guided.available is False
    assert guided.reason_code == "native_render_required"


@pytest.mark.parametrize("edit_format", ["montage", "day_vlog", "single_hero"])
def test_verified_phone_original_audio_uses_guided_and_preserves_audio(
    monkeypatch, edit_format
) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", True)
    monkeypatch.setattr(capabilities.settings, "edit_format_single_hero_enabled", True)
    phone_ids = [f"phone-{index}" for index in range(17)]
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=[{"media_id": media_id, "kind": "video"} for media_id in phone_ids],
        phone_source_media_ids=phone_ids,
        phone_rendering_allowed=True,
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format=edit_format, audio_strategy="original_audio", render_program="native"
        ),
    )
    assert plan.strategy.render_program == "guided"
    assert plan.strategy.montage_audio is not None
    assert plan.strategy.montage_audio.preserve_source_audio is True
    # Empty selection preserves every approved moment, without applying the
    # twelve-source limit intended for individually selected audio beds.
    assert plan.strategy.montage_audio.source_media_ids == []


@pytest.mark.parametrize("media_scope", [None, "all"])
@pytest.mark.parametrize("case", ["disabled", "invalid", "mixed", "voiceover"])
def test_unavailable_phone_sources_cannot_fall_back_to_cloud(
    monkeypatch, case, media_scope
) -> None:
    _enable_guided(monkeypatch)
    media = [{"media_id": "phone-a", "kind": "video"}]
    if case == "mixed":
        media.append({"media_id": "cloud-b", "kind": "video"})
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=media,
        phone_source_media_ids=[] if case == "invalid" else ["phone-a"],
        phone_rendering_allowed=case != "disabled",
        has_voiceover=case == "voiceover",
    )
    assert capabilities.CAPABILITY_PHONE_SOURCE_AUDIO in manifest.capabilities
    assert not manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available
    assert not manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].available
    with pytest.raises(MixedMediaTimingUnavailableError):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage", audio_strategy="original_audio", media_scope=media_scope
            ),
        )


def test_cloud_manifest_and_original_audio_route_stay_unchanged(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert capabilities.CAPABILITY_PHONE_SOURCE_AUDIO not in manifest.capabilities
    phone = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}],
        phone_source_media_ids=["clip-a"],
        phone_rendering_allowed=True,
    )
    assert phone.manifest_hash != manifest.manifest_hash
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage", audio_strategy="original_audio", selected_media_ids=["clip-a"]
        ),
    )
    assert plan.strategy.render_program == "native"
    assert plan.strategy.montage_audio is None


@pytest.mark.parametrize("audio", ["licensed_music", "original_audio"])
def test_phone_provenance_overrides_model_native_choice(monkeypatch, audio) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest, CreativeStrategy(audio_strategy=audio, render_program="native")
    )
    assert plan.strategy.render_program == "guided"


def test_phone_only_montage_rejects_voiceover_strategy(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    # The phone subtype keeps every mixed-media handler working unchanged.
    with pytest.raises(MixedMediaTimingUnavailableError, match="does not support voiceover") as exc:
        capabilities.compile_strategy_to_plan(
            manifest, CreativeStrategy(audio_strategy="voiceover")
        )
    assert isinstance(exc.value, PhoneFormatUnavailableError)
    assert exc.value.voiceover is True


def test_phone_item_with_recorded_voiceover_names_the_voiceover(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        has_voiceover=True,
    )
    with pytest.raises(PhoneFormatUnavailableError, match="recorded voiceover") as exc:
        capabilities.compile_strategy_to_plan(manifest, CreativeStrategy())
    assert exc.value.voiceover is True


def test_phone_item_with_recorded_voiceover_compiles_native_when_verified(
    monkeypatch,
) -> None:
    """KRI-132: flag on + narrationAudio verified lifts BOTH the
    manifest-level CAPABILITY_PHONE_SOURCE_AUDIO gate (creator_capabilities.py's
    `resolve_creator_manifest`) and `effective_render_program`'s
    (creator_policy.py) phone branch, which now resolves a voiceover-carrying
    phone manifest to "native" instead of unconditionally demanding the
    guided-proposal capability -- mirroring `_dispatch_item_render`'s own
    `guided_applicable = guided_edit_applicable(strategy_format,
    has_voiceover=True)` (always False) and `routes/creator_agent.py`'s
    `bypass_guided_edit_gate = render_program == "native"`.

    KRI-132 phone-voiceover-gate follow-up: CAPABILITY_GUIDED_VOICEOVER is no
    longer unconditionally `unsupported_phone_audio` on every phone manifest
    -- `phone_guided_narration_rendering_enabled` (default True) also holds
    here (this test never turns it off), so the phone gate now carries
    through the CLOUD-computed reason instead. This project has no narration
    identity and `creator_prompt_fidelity_enabled` is off (this test's
    default), so the cloud rule's own `guided_voiceover_executable` is False
    for a reason that has nothing to do with the phone -- `disabled_by_setting`,
    same as it would be for a cloud project in this exact shape. That
    capability's value is still asserted unavailable below: this test's real
    point (native render doesn't need the guided-story lane) is unaffected
    either way.
    """
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_render_verified_features", ["narrationAudio"])
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        has_voiceover=True,
    )
    phone_audio = manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO]
    assert phone_audio.available is True
    guided_voiceover = manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER]
    assert (guided_voiceover.available, guided_voiceover.reason_code) == (
        False,
        "disabled_by_setting",
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest, CreativeStrategy(selected_media_ids=["phone-a"])
    )
    assert plan.strategy.render_program == "native"
    assert [c.command for c in plan.commands] == ["set_item_intent", "dispatch_render"]


def test_phone_voiceover_with_selected_pool_media_fails_closed(monkeypatch) -> None:
    """The montage-family phone compiler only binds clip-lane sources -- an
    explicit Visuals-pool ("asset-*") selection alongside a voiceover must
    fail closed with a typed phone error instead of silently resolving
    "native" and dropping the selected pool media."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        ["narrationAudio", "stillImages", "visualVideos"],
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[
            {"media_id": "phone-a", "kind": "video"},
            {"media_id": "asset-photo1", "kind": "image"},
        ],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        has_voiceover=True,
    )
    with pytest.raises(PhoneMediaUnavailableError, match="not Visuals"):
        capabilities.compile_strategy_to_plan(
            manifest, CreativeStrategy(selected_media_ids=["phone-a", "asset-photo1"])
        )


def test_phone_voiceover_all_media_scope_with_pool_media_fails_closed(monkeypatch) -> None:
    """`media_scope == "all"` with a voiceover is intercepted by a pre-existing,
    EARLIER, voiceover-specific guard (has_voiceover + all-media scope requires
    the guided_voiceover_v1 execution contract) before the new native/voiceover
    branch is ever reached -- confirm it still fails closed (never silently
    resolves native, dropping the pool media) when the project has pool media
    attached.

    KRI-132 phone-voiceover-gate follow-up: this guard has an exemption in
    `effective_render_program` for `guided_voiceover.reason_code ==
    "disabled_by_setting"` (so a fleet-wide disabled guided-voiceover lane
    doesn't mask the plainer "requires the guided proposal capability"
    message a few lines below it). `CAPABILITY_GUIDED_VOICEOVER` now carries
    through the cloud-computed reason on a phone manifest once this lane's
    own rollout is on (`phone_guided_narration_supported()`, true here via
    `phone_narration_rendering_enabled` + verified narrationAudio) instead of
    an unconditional `unsupported_phone_audio` -- without
    `creator_prompt_fidelity_enabled`, that cloud reason IS
    "disabled_by_setting" (the whole guided-voiceover lane is off), which
    would trip the exemption and route this test into the OTHER guard's
    message instead. Set it on so this test keeps exercising the guard it
    names: the reason becomes "narration_identity_missing" (no narration
    identity supplied here), which is not exempted.
    """
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(capabilities.settings, "creator_prompt_fidelity_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        ["narrationAudio", "stillImages"],
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[
            {"media_id": "phone-a", "kind": "video"},
            {"media_id": "asset-photo1", "kind": "image"},
        ],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        has_voiceover=True,
    )
    with pytest.raises(MixedMediaTimingUnavailableError, match="guided_voiceover_v1"):
        capabilities.compile_strategy_to_plan(manifest, CreativeStrategy(media_scope="all"))


def test_phone_voiceover_stays_blocked_when_flag_or_capability_missing(monkeypatch) -> None:
    """(c) Flag off, or narrationAudio unverified: byte-identical to
    pre-KRI-132 -- still PhoneFormatUnavailableError(voiceover=True), never
    "native"."""
    _enable_guided(monkeypatch)

    def manifest_for(flag: bool, features: list[str]) -> object:
        monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", flag)
        monkeypatch.setattr(capabilities.settings, "phone_render_verified_features", features)
        return capabilities.resolve_creator_manifest(
            item_id="item-phone",
            edit_format="montage",
            media=[{"media_id": "phone-a", "kind": "video"}],
            phone_source_media_ids=["phone-a"],
            phone_rendering_allowed=True,
            has_voiceover=True,
        )

    for flag, features in [(False, ["narrationAudio"]), (True, [])]:
        manifest = manifest_for(flag, features)
        assert manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available is False
        with pytest.raises(PhoneFormatUnavailableError, match="recorded voiceover") as exc:
            capabilities.compile_strategy_to_plan(
                manifest, CreativeStrategy(selected_media_ids=["phone-a"])
            )
        assert exc.value.voiceover is True


def test_phone_only_strategy_rejects_audio_led_format_even_when_enabled(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "edit_format_talking_head_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    # KRI-132: talking_head has no phone compiler at all (unlike subtitled/
    # narrated*, which can now be phone-eligible) -- still fails closed, just
    # with `phone_format:{format}`'s own reason text instead of the old
    # hardcoded "guided edit format" message.
    with pytest.raises(
        MixedMediaTimingUnavailableError, match="does not render on this iPhone"
    ) as exc:
        capabilities.compile_strategy_to_plan(
            manifest, CreativeStrategy(edit_format="talking_head")
        )
    assert isinstance(exc.value, PhoneFormatUnavailableError)
    assert exc.value.voiceover is False


def _enable_narrated_and_subtitled_flags(monkeypatch, *, phone_flags_on: bool = True) -> None:
    monkeypatch.setattr(capabilities.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(capabilities.settings, "narrated_self_narration_enabled", True)
    monkeypatch.setattr(capabilities.settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(capabilities.settings, "edit_format_talking_head_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_rendering_enabled", phone_flags_on)
    monkeypatch.setattr(capabilities.settings, "phone_narrated_rendering_enabled", phone_flags_on)
    monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", phone_flags_on)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        ["narrationAudio"] if phone_flags_on else [],
    )


def _phone_manifest(monkeypatch, edit_format, media, *, has_voiceover=False):
    return capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media],
        phone_rendering_allowed=True,
        has_voiceover=has_voiceover,
    )


@pytest.mark.parametrize("edit_format", ["narrated", "narrated_planned", "narrated_ready"])
def test_phone_self_narration_two_clips_refused_with_typed_reason(monkeypatch, edit_format) -> None:
    """KRI-118 L1 item 3: self-narration (no recorded voiceover) across 2+
    clips has no phone compiler (`talking_head`, which 2+ self-narrated
    clips would resolve to, is never phone-supported). Refused at planning
    time with its own reason/copy, distinct from `phone_format_unavailable`."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch,
        edit_format,
        [
            {"media_id": "phone-a", "kind": "video"},
            {"media_id": "phone-b", "kind": "video"},
        ],
        has_voiceover=False,
    )
    entry = manifest.capabilities[f"phone_format:{edit_format}"]
    assert entry.available is False
    assert entry.reason_code == "self_narration_multi_clip"
    assert "Narrating across several clips isn't on iPhone yet" in entry.reason


def test_phone_self_narration_one_clip_still_available(monkeypatch) -> None:
    """The single-clip self-narration shape (unaffected by the new refusal)."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated",
        [{"media_id": "phone-a", "kind": "video"}],
        has_voiceover=False,
    )
    entry = manifest.capabilities["phone_format:narrated"]
    assert entry.available is True


def test_phone_narrated_voiceover_two_clips_is_unaffected(monkeypatch) -> None:
    """The multi-clip refusal is specific to self-narration (no recorded
    voiceover) -- a narrated item WITH a recorded voiceover across 2+ clips
    is a totally different (and phone-supported, once rolled out) lane."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated",
        [
            {"media_id": "phone-a", "kind": "video"},
            {"media_id": "phone-b", "kind": "video"},
        ],
        has_voiceover=True,
    )
    entry = manifest.capabilities["phone_format:narrated"]
    assert entry.available is True


def test_phone_subtitled_one_clip_no_voiceover_compiles_native(monkeypatch) -> None:
    """KRI-132 follow-up: subtitled ("Talking to camera") is audio-led and has
    no guided-story lane -- exactly one clip, no voiceover, resolves "native"
    directly once the rollout flags are on."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(monkeypatch, "subtitled", [{"media_id": "phone-a", "kind": "video"}])
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="subtitled", audio_strategy="original_audio", selected_media_ids=["phone-a"]
        ),
    )
    assert plan.strategy.render_program == "native"


def _without_phone_format_capabilities(manifest):
    """A manifest as it was resolved before `phone_format:*` existed."""
    return manifest.model_copy(
        update={
            "capabilities": {
                key: value
                for key, value in manifest.capabilities.items()
                if not key.startswith("phone_format")
            }
        }
    )


def test_legacy_phone_manifest_without_phone_format_keys_keeps_montage_working(
    monkeypatch,
) -> None:
    """An in-flight session (or replay fixture) persisted before KRI-132 has no
    `phone_format:*` capability. Absence falls back to the old rule -- montage
    still compiles "guided" -- rather than refusing every phone project the
    moment this deploys (caught by the `phone_original_audio_17_clips` eval)."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _without_phone_format_capabilities(
        _phone_manifest(monkeypatch, "montage", [{"media_id": "phone-a", "kind": "video"}])
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage", audio_strategy="original_audio", selected_media_ids=["phone-a"]
        ),
    )
    assert plan.strategy.render_program == "guided"


def test_legacy_phone_manifest_without_phone_format_keys_still_refuses_subtitled(
    monkeypatch,
) -> None:
    """The legacy fallback is the OLD rule, which never admitted subtitled --
    the new formats need a freshly resolved manifest."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _without_phone_format_capabilities(
        _phone_manifest(monkeypatch, "subtitled", [{"media_id": "phone-a", "kind": "video"}])
    )
    with pytest.raises(PhoneFormatUnavailableError, match="guided edit format"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                selected_media_ids=["phone-a"],
            ),
        )


def test_phone_subtitled_two_clips_fails_closed(monkeypatch) -> None:
    """subtitled requires exactly one clip -- two clips raises
    PhoneFormatUnavailableError, not a silent single-clip truncation."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    media = [{"media_id": "phone-a", "kind": "video"}, {"media_id": "phone-b", "kind": "video"}]
    manifest = _phone_manifest(monkeypatch, "subtitled", media)
    with pytest.raises(PhoneFormatUnavailableError, match="exactly one clip"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                selected_media_ids=["phone-a", "phone-b"],
            ),
        )


@pytest.mark.parametrize("edit_format", ["narrated_planned", "narrated_ready", "montage"])
def test_phone_voiceover_renders_with_prod_narration_identity_and_prompt_fidelity(
    monkeypatch, edit_format
) -> None:
    """Prod shape (found on a real iPhone 2026-09-22): `creator_prompt_fidelity
    _enabled` + guided editing are ON and an uploaded voiceover has a resolved
    narration identity, so `guided_voiceover_executable` is True for EVERY
    voiceover project. That used to mark `CAPABILITY_PHONE_SOURCE_AUDIO`
    unavailable ("A voiceover can't render on your iPhone yet... Remove the
    voiceover") -- blocking narrated AND #1116's montage voiceover in prod while
    every unit test (no narration identity) stayed green. The native voiceover
    render (this test's real point: a PLAIN strategy with no
    `guided_voiceover_v1` execution contract) must not be vetoed by
    `guided_voiceover_executable`'s mere truth either way.

    KRI-132 phone-voiceover-gate follow-up: unlike when that fix landed,
    CAPABILITY_GUIDED_VOICEOVER is no longer unconditionally blocked on the
    phone -- `_enable_narrated_and_subtitled_flags` (phone_flags_on=True, the
    default) turns on `phone_narration_rendering_enabled` + verified
    narrationAudio, and `phone_guided_narration_rendering_enabled` defaults
    True, so `phone_guided_narration_supported()` holds and the capability
    now carries through the cloud-computed value. With prompt fidelity +
    narration identity + guided editing all present, that value IS available
    (`guided_voiceover_executable` is True) -- asserted below. It still does
    not veto this native voiceover render: this strategy never sets
    `execution_contract="guided_voiceover_v1"` or `media_scope="all"`, so
    `effective_render_program` never even consults the capability.
    """
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "creator_prompt_fidelity_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.m4a",
            "generation": "voice-generation-1",
            "duration_s": 48.0,
        },
    )
    assert manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available is True
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].available is True
    # The guided-story narration lane is ALSO now available (this project has
    # everything `guided_voiceover_executable` requires); it simply isn't
    # requested by this plain strategy, so it cannot veto the native render.
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is True
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format=edit_format, audio_strategy="voiceover", selected_media_ids=["phone-a"]
        ),
    )
    assert plan.strategy.render_program == "native"


def test_phone_narrated_with_recorded_voiceover_compiles_native_when_supported(
    monkeypatch,
) -> None:
    """A narrated_planned item that ALREADY has its voiceover recorded, on a
    phone account with the narrated rollout flags on, resolves "native" --
    this is the manifest-level `CAPABILITY_PHONE_SOURCE_AUDIO` gate
    (`resolve_creator_manifest`) recognizing the narrated family, not just
    montage/day_vlog/single_hero."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated_planned",
        [{"media_id": "phone-a", "kind": "video"}],
        has_voiceover=True,
    )
    assert manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available is True
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="narrated_planned",
            audio_strategy="voiceover",
            selected_media_ids=["phone-a"],
        ),
    )
    assert plan.strategy.render_program == "native"


def test_phone_narrated_with_recorded_voiceover_blocked_when_flag_off(monkeypatch) -> None:
    """Flag/capability off: byte-identical refusal, still
    PhoneFormatUnavailableError(voiceover=True) -- the montage-family
    voiceover gate's own existing behavior, now also proven for narrated."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch, phone_flags_on=False)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated_planned",
        [{"media_id": "phone-a", "kind": "video"}],
        has_voiceover=True,
    )
    assert manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available is False
    with pytest.raises(PhoneFormatUnavailableError, match="recorded voiceover") as exc:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="narrated_planned",
                audio_strategy="voiceover",
                selected_media_ids=["phone-a"],
            ),
        )
    assert exc.value.voiceover is True


def test_phone_narrated_pending_voiceover_compiles_native_before_recording(monkeypatch) -> None:
    """KRI-132 follow-up (the coordinator's traced dead end): the normal
    Narrated journey is pick format -> chat/write script -> record voiceover
    -> Generate. A narrated_planned strategy that intends to record a
    voiceover (`audio_strategy="voiceover"`) but has none attached YET must
    still be plannable on a phone account -- otherwise the chat turn
    dead-ends before the creator ever gets a chance to record anything.
    Generate itself still gates on the real recording via the dispatch gate
    (`content_plan_build.py`), unaffected by this test."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated_planned",
        [{"media_id": "phone-a", "kind": "video"}],
        has_voiceover=False,
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="narrated_planned",
            audio_strategy="voiceover",
            selected_media_ids=["phone-a"],
        ),
    )
    assert plan.strategy.render_program == "native"


def test_phone_narrated_pending_voiceover_blocked_when_flag_off(monkeypatch) -> None:
    """Same pending-voiceover planning turn, but the narrated phone rollout
    is off -- must still fail closed with the typed voiceover reason, not
    silently let planning through for a format that can never render."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch, phone_flags_on=False)
    manifest = _phone_manifest(
        monkeypatch,
        "narrated_planned",
        [{"media_id": "phone-a", "kind": "video"}],
        has_voiceover=False,
    )
    with pytest.raises(PhoneFormatUnavailableError, match="does not render on this iPhone") as exc:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="narrated_planned",
                audio_strategy="voiceover",
                selected_media_ids=["phone-a"],
            ),
        )
    # The `voiceover=True` subtype is what routes the creator-facing copy to
    # "ask for this edit without a voiceover" rather than a generic format
    # rejection -- the exact message text matters less than that flag.
    assert exc.value.voiceover is True


def test_phone_montage_voiceover_switch_with_none_attached_is_unchanged(monkeypatch) -> None:
    """The montage-family behaviour this fix must NOT loosen: a montage
    strategy newly switching `audio_strategy` to "voiceover" with none
    attached yet stays unconditionally blocked, byte-identical to before this
    turn's fix -- montage is not itself the voiceover format the way narrated
    is, so it never gets the pending-voiceover leniency."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = _phone_manifest(
        monkeypatch, "montage", [{"media_id": "phone-a", "kind": "video"}], has_voiceover=False
    )
    with pytest.raises(
        PhoneFormatUnavailableError, match="does not support voiceover audio"
    ) as exc:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage", audio_strategy="voiceover", selected_media_ids=["phone-a"]
            ),
        )
    assert exc.value.voiceover is True


def test_phone_format_capabilities_all_unavailable_when_phone_itself_unavailable(
    monkeypatch,
) -> None:
    """When the top-level phone capability is unavailable (e.g. unverified
    phone sources), every derived `phone_format:*` /
    `phone_format_pending_voiceover:*` entry must be unavailable too --
    never a stale/available leftover from a different code path."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        # No `phone_source_media_ids` passed -> unverified phone sources.
        phone_source_media_ids=[],
        phone_rendering_allowed=True,
    )
    phone_capability = manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO]
    assert phone_capability.available is False
    for edit_format in capabilities.EDIT_FORMATS:
        entry = manifest.capabilities.get(f"phone_format:{edit_format}")
        assert entry is not None
        assert entry.available is False
    for narrated_format in ("narrated", "narrated_planned", "narrated_ready"):
        entry = manifest.capabilities.get(f"phone_format_pending_voiceover:{narrated_format}")
        assert entry is not None
        assert entry.available is False


def _phone_manifest_with_pool(monkeypatch, pool, *, verified_features=(), allowed=True):
    _enable_guided(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "phone_render_verified_features", list(verified_features)
    )
    return capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}, *pool],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=allowed,
    )


def _pool_strategies(pool_id: str) -> dict[str, CreativeStrategy]:
    return {
        "all": CreativeStrategy(media_scope="all"),
        "selected": CreativeStrategy(selected_media_ids=[pool_id]),
        "audio": CreativeStrategy(montage_audio={"source_media_ids": [pool_id]}),
        "cadence": CreativeStrategy(
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["phone-a", pool_id],
                cut_duration_s=1,
            )
        ),
    }


@pytest.mark.parametrize(
    ("pool_kind", "verified_features"),
    [
        ("image", []),
        ("video", []),
        ("video", ["stillImages"]),
        ("image", ["visualVideos"]),
    ],
)
def test_unused_pool_media_does_not_disable_phone_clips_but_cannot_be_selected(
    monkeypatch, pool_kind, verified_features
) -> None:
    manifest = _phone_manifest_with_pool(
        monkeypatch,
        [{"media_id": "asset-photo", "kind": pool_kind}],
        verified_features=verified_features,
    )
    assert manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO].available
    for strategy in _pool_strategies("asset-photo").values():
        # The phone subtype keeps every mixed-media handler working unchanged.
        with pytest.raises(MixedMediaTimingUnavailableError, match="bound video sources") as exc:
            capabilities.compile_strategy_to_plan(manifest, strategy)
        assert isinstance(exc.value, PhoneMediaUnavailableError)
        assert exc.value.still_images_available is ("stillImages" in verified_features)
        assert exc.value.visual_videos_available is ("visualVideos" in verified_features)


def test_phone_still_images_capability_requires_verified_device_stills(monkeypatch) -> None:
    pool = [{"media_id": "asset-photo", "kind": "image"}]
    unverified = _phone_manifest_with_pool(monkeypatch, pool, verified_features=["looks"])
    # Absent, not merely unavailable, so flag-off manifests and their
    # confirmation hashes are exactly what they were before stills existed.
    assert capabilities.CAPABILITY_PHONE_STILL_IMAGES not in unverified.capabilities
    assert unverified.manifest_hash == _phone_manifest_with_pool(monkeypatch, pool).manifest_hash

    verified = _phone_manifest_with_pool(
        monkeypatch, pool, verified_features=["looks", "stillImages"]
    )
    assert verified.capabilities[capabilities.CAPABILITY_PHONE_STILL_IMAGES].available
    assert verified.manifest_hash != unverified.manifest_hash

    disabled = _phone_manifest_with_pool(
        monkeypatch, pool, verified_features=["stillImages"], allowed=False
    )
    stills = disabled.capabilities[capabilities.CAPABILITY_PHONE_STILL_IMAGES]
    assert not stills.available
    assert stills.reason_code == "disabled_by_setting"

    cloud = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}, *pool],
    )
    assert capabilities.CAPABILITY_PHONE_STILL_IMAGES not in cloud.capabilities


def test_phone_visual_videos_capability_requires_verified_device_videos(monkeypatch) -> None:
    pool = [{"media_id": "asset-video", "kind": "video"}]
    unverified = _phone_manifest_with_pool(monkeypatch, pool, verified_features=["stillImages"])
    # Absent with the flag off, so the stills-only manifest hash is unchanged.
    assert capabilities.CAPABILITY_PHONE_VISUAL_VIDEOS not in unverified.capabilities

    verified = _phone_manifest_with_pool(
        monkeypatch, pool, verified_features=["stillImages", "visualVideos"]
    )
    assert verified.capabilities[capabilities.CAPABILITY_PHONE_VISUAL_VIDEOS].available
    assert verified.manifest_hash != unverified.manifest_hash

    disabled = _phone_manifest_with_pool(
        monkeypatch, pool, verified_features=["visualVideos"], allowed=False
    )
    videos = disabled.capabilities[capabilities.CAPABILITY_PHONE_VISUAL_VIDEOS]
    assert not videos.available
    assert videos.reason_code == "disabled_by_setting"

    cloud = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}, *pool],
    )
    assert capabilities.CAPABILITY_PHONE_VISUAL_VIDEOS not in cloud.capabilities


@pytest.mark.parametrize("scope", ["all", "selected", "audio", "cadence"])
def test_verified_phone_visual_videos_allow_pool_videos_even_as_sources(monkeypatch, scope) -> None:
    manifest = _phone_manifest_with_pool(
        monkeypatch,
        [{"media_id": "asset-video", "kind": "video", "duration_s": 12}],
        verified_features=["visualVideos"],
    )
    # A Visuals video compiles like bound footage, sound and cuts included.
    plan = capabilities.compile_strategy_to_plan(manifest, _pool_strategies("asset-video")[scope])
    assert plan.strategy.render_program == "guided"
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "draft_guided_proposal",
        "dispatch_render",
    ]


def test_verified_phone_visuals_leave_only_the_photo_source_limit(monkeypatch) -> None:
    manifest = _phone_manifest_with_pool(
        monkeypatch,
        [
            {"media_id": "asset-photo", "kind": "image"},
            {"media_id": "asset-video", "kind": "video"},
        ],
        verified_features=["stillImages", "visualVideos"],
    )
    plan = capabilities.compile_strategy_to_plan(manifest, CreativeStrategy(media_scope="all"))
    assert plan.strategy.selected_media_ids == ["phone-a", "asset-photo", "asset-video"]

    photo_strategies = _pool_strategies("asset-photo")
    for strategy in (photo_strategies["audio"], photo_strategies["cadence"]):
        with pytest.raises(PhoneMediaUnavailableError, match="bound video sources") as exc:
            capabilities.compile_strategy_to_plan(manifest, strategy)
        assert exc.value.still_images_available is True
        assert exc.value.visual_videos_available is True


_PHONE_UNSUPPORTED = [
    "sound_effects",
    "media_overlays",
    "visual_blocks",
    "motion_scenes",
    "wide_looks",
]


def test_phone_manifest_never_advertises_lanes_the_phone_compiler_rejects(monkeypatch) -> None:
    manifest = _phone_manifest_with_pool(monkeypatch, [])
    for name in _PHONE_UNSUPPORTED:
        assert not manifest.capabilities[name].available
        assert manifest.capabilities[name].reason_code == "unsupported_on_phone"
    # The iPhone draws crossfades and dips itself.
    assert manifest.capabilities["transitions"].available

    cloud = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
    )
    assert all(cloud.capabilities[name].available for name in _PHONE_UNSUPPORTED)

    # An ordinary phone montage still plans; only the unsupported extras drop out.
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(optional_treatments=["sfx", "overlays", "looks", "transitions"]),
    )
    assert plan.strategy.render_program == "guided"
    assert plan.strategy.optional_treatments == ["transitions"]
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "draft_guided_proposal",
        "dispatch_render",
    ]


def test_visual_blocks_stays_unsupported_without_editor_media_flag(monkeypatch) -> None:
    """KRI-118 L1 item 5: `visualBlocks` verified alone is not enough --
    the editor-media rollout flag must also be on, mirroring
    `phone_rollout.validate_phone_pilot_recipe`'s exact rule."""
    manifest = _phone_manifest_with_pool(monkeypatch, [], verified_features=["visualBlocks"])
    assert manifest.capabilities["visual_blocks"].available is False
    assert manifest.capabilities["visual_blocks"].reason_code == "unsupported_on_phone"


def test_visual_blocks_stays_unsupported_without_verified_feature(monkeypatch) -> None:
    """The rollout flag alone is not enough either -- the device must have
    verified `visualBlocks`."""
    monkeypatch.setattr(capabilities.settings, "phone_editor_media_enabled", True)
    manifest = _phone_manifest_with_pool(monkeypatch, [], verified_features=[])
    assert manifest.capabilities["visual_blocks"].available is False
    assert manifest.capabilities["visual_blocks"].reason_code == "unsupported_on_phone"


def test_visual_blocks_available_once_flag_and_capability_both_hold(monkeypatch) -> None:
    """Both conditions met: `visual_blocks` becomes available, unlike its
    permanently-unsupported siblings (sound_effects/media_overlays/etc)."""
    monkeypatch.setattr(capabilities.settings, "phone_editor_media_enabled", True)
    manifest = _phone_manifest_with_pool(monkeypatch, [], verified_features=["visualBlocks"])
    assert manifest.capabilities["visual_blocks"].available is True
    for name in _PHONE_UNSUPPORTED:
        if name == "visual_blocks":
            continue
        assert manifest.capabilities[name].available is False


def test_visual_blocks_generic_setting_off_still_wins_on_phone(monkeypatch) -> None:
    """The generic `visual_blocks_enabled` feature gate (cloud and phone
    alike) is still respected even when the phone-specific conditions hold.
    Sets state directly (rather than through `_phone_manifest_with_pool`,
    which calls `_enable_guided` and would re-flip the generic setting back
    on) so the off-setting sticks for the single manifest resolution."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_render_verified_features", ["visualBlocks"])
    monkeypatch.setattr(capabilities.settings, "visual_blocks_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    assert manifest.capabilities["visual_blocks"].available is False


def test_phone_named_sfx_request_fails_up_front_with_phone_copy(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        catalog=[{"catalog_id": "sfx-fah", "kind": "sound_effect", "label": "Fah"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    with pytest.raises(capabilities.CreatorSfxUnavailableError, match="on your iPhone yet"):
        capabilities.compile_strategy_to_plan(
            manifest, CreativeStrategy(licensed_sfx={"effect_id": "sfx-fah"})
        )


@pytest.mark.parametrize("scope", ["all", "selected"])
def test_verified_phone_stills_allow_pool_photos(monkeypatch, scope) -> None:
    manifest = _phone_manifest_with_pool(
        monkeypatch,
        [{"media_id": "asset-photo", "kind": "image"}],
        verified_features=["stillImages"],
    )
    plan = capabilities.compile_strategy_to_plan(manifest, _pool_strategies("asset-photo")[scope])
    assert plan.strategy.render_program == "guided"
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "draft_guided_proposal",
        "dispatch_render",
    ]
    if scope == "all":
        assert plan.strategy.selected_media_ids == ["phone-a", "asset-photo"]


def test_verified_phone_stills_still_reject_pool_video_and_photo_sources(monkeypatch) -> None:
    manifest = _phone_manifest_with_pool(
        monkeypatch,
        [
            {"media_id": "asset-photo", "kind": "image"},
            {"media_id": "asset-video", "kind": "video"},
        ],
        verified_features=["stillImages"],
    )
    # An unused pool video does not block a selected photo.
    plan = capabilities.compile_strategy_to_plan(
        manifest, CreativeStrategy(selected_media_ids=["asset-photo"])
    )
    assert plan.strategy.render_program == "guided"

    photo_strategies = _pool_strategies("asset-photo")
    for strategy in (
        # Scope "all" pulls in the pool video.
        photo_strategies["all"],
        CreativeStrategy(selected_media_ids=["asset-photo", "asset-video"]),
        # A still has no sound or source cuts to drive the montage.
        photo_strategies["audio"],
        photo_strategies["cadence"],
        CreativeStrategy(
            selected_media_ids=["asset-photo"],
            montage_audio={"source_media_ids": ["phone-a", "asset-photo"]},
        ),
    ):
        with pytest.raises(PhoneMediaUnavailableError, match="bound video sources") as exc:
            capabilities.compile_strategy_to_plan(manifest, strategy)
        assert exc.value.still_images_available is True
        assert exc.value.visual_videos_available is False


def test_phone_original_audio_preserves_explicit_audio_policy(monkeypatch) -> None:
    from app.schemas.edit_proposal import MontageAudioPlan

    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )
    explicit = MontageAudioPlan(preserve_source_audio=False)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(audio_strategy="original_audio", montage_audio=explicit),
    )
    assert plan.strategy.render_program == "guided"
    assert plan.strategy.montage_audio == explicit


def test_guided_voiceover_opt_in_preserves_all_media_without_native_cap(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "creator_prompt_fidelity_enabled", True, raising=False
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 44.7,
        },
        media=[
            *[{"media_id": f"clip-{index}", "kind": "video"} for index in range(19)],
            *[{"media_id": f"photo-{index}", "kind": "image"} for index in range(20)],
        ],
    )
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is True

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="voiceover",
            execution_contract="guided_voiceover_v1",
            media_scope="all",
            render_program="native",
        ),
    )

    assert plan.strategy.render_program == "guided"
    assert len(plan.strategy.selected_media_ids) == 39
    assert plan.strategy.selected_media_ids == [media.media_id for media in manifest.media]


def test_guided_voiceover_is_unavailable_without_opt_in_flag(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "creator_prompt_fidelity_enabled", False, raising=False
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 12,
        },
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is False
    with pytest.raises(MixedMediaTimingUnavailableError, match="disabled"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                audio_strategy="voiceover",
                execution_contract="guided_voiceover_v1",
                media_scope="all",
            ),
        )


def test_guided_voiceover_requires_explicit_execution_contract(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "creator_prompt_fidelity_enabled", True, raising=False
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 12,
        },
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    with pytest.raises(MixedMediaTimingUnavailableError, match="execution contract"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                audio_strategy="voiceover",
                media_scope="all",
            ),
        )


def test_manifest_reports_setting_and_state_reasons(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = capabilities.resolve_creator_manifest(item_id="item-1", edit_format="montage")
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY].reason_code == (
        "disabled_by_setting"
    )
    assert manifest.capabilities[capabilities.CAPABILITY_NATIVE_RENDER].reason_code == (
        "no_native_clip"
    )
    assert manifest.capabilities[capabilities.CAPABILITY_SELECT_READY_VARIANT].reason_code == (
        "no_ready_variant"
    )

    ready = capabilities.resolve_creator_manifest(
        item_id="item-1",
        current_edit=CreatorEditSnapshot(status="ready", variant_id="variant-1"),
    )
    assert ready.capabilities[capabilities.CAPABILITY_SELECT_READY_VARIANT].available is True


def test_trusted_creator_can_use_guided_renderer_while_public_plan_api_is_dark(
    monkeypatch,
) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)

    public = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    creator = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
        guided_capability_enabled=True,
    )

    assert public.capabilities[capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL].available is False
    assert creator.capabilities[capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL].available is True


def test_manifest_reports_caption_style_from_live_creator_execution_flag(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "main_creator_agent_execution_enabled", False)
    disabled = capabilities.resolve_creator_manifest(item_id="item-1")
    assert disabled.capabilities[capabilities.CAPABILITY_CAPTION_STYLE].reason_code == (
        "disabled_by_setting"
    )

    monkeypatch.setattr(capabilities.settings, "main_creator_agent_execution_enabled", True)
    enabled = capabilities.resolve_creator_manifest(item_id="item-1")
    assert enabled.capabilities[capabilities.CAPABILITY_CAPTION_STYLE].available is True


def test_manifest_reports_automatic_cut_when_either_detector_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "silence_cut_enabled", False)
    monkeypatch.setattr(capabilities.settings, "retake_cut_enabled", False)
    disabled = capabilities.resolve_creator_manifest(item_id="item-1")
    assert disabled.capabilities[capabilities.CAPABILITY_AUTOMATIC_CUT].reason_code == (
        "disabled_by_setting"
    )

    monkeypatch.setattr(capabilities.settings, "retake_cut_enabled", True)
    enabled = capabilities.resolve_creator_manifest(item_id="item-1")
    assert enabled.capabilities[capabilities.CAPABILITY_AUTOMATIC_CUT].available is True


def test_day_vlog_manifest_is_explicitly_unavailable_while_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="day_vlog",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    capability = manifest.capabilities["edit_format:day_vlog"]
    assert capability.available is False
    assert capability.reason_code == "disabled_by_setting"
    assert "EDIT_FORMAT_DAY_VLOG_ENABLED" in (capability.reason or "")


def test_day_vlog_manifest_can_advertise_guided_renderer_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", True)
    monkeypatch.setattr(capabilities.settings, "NARRATIVE_CLIP_ORDER_ENABLED", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="day_vlog",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    assert manifest.capabilities["edit_format:day_vlog"].available is True


def test_day_vlog_manifest_fails_closed_when_chronology_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", True)
    monkeypatch.setattr(capabilities.settings, "NARRATIVE_CLIP_ORDER_ENABLED", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="day_vlog",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    capability = manifest.capabilities["edit_format:day_vlog"]
    assert capability.available is False
    assert capability.reason_code == "chronology_disabled"


def test_single_hero_manifest_explains_flag_and_advertises_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_single_hero_enabled", False)
    unavailable = capabilities.resolve_creator_manifest(
        item_id="item-1", edit_format="single_hero", media=[{"media_id": "clip-1", "kind": "video"}]
    )
    capability = unavailable.capabilities["edit_format:single_hero"]
    assert capability.reason_code == "disabled_by_setting"
    assert "EDIT_FORMAT_SINGLE_HERO_ENABLED" in (capability.reason or "")

    monkeypatch.setattr(capabilities.settings, "edit_format_single_hero_enabled", True)
    available = capabilities.resolve_creator_manifest(
        item_id="item-1", edit_format="single_hero", media=[{"media_id": "clip-1", "kind": "video"}]
    )
    assert available.capabilities["edit_format:single_hero"].available is True


def test_compile_strategy_uses_only_available_commands_and_never_guided_for_voiceover(
    monkeypatch,
) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="voiceover",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "dispatch_render",
    ]
    assert plan.strategy.render_program == "native"


def test_compile_drops_treatments_not_advertised_by_manifest(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            optional_treatments=["sfx", "transitions"],
        ),
    )
    assert plan.strategy.optional_treatments == ["transitions"]


def test_compile_rejects_named_sfx_when_capability_is_unavailable(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
        catalog=[{"catalog_id": "sfx-fah", "kind": "sound_effect", "label": "Fah"}],
    )

    with pytest.raises(capabilities.CreatorSfxUnavailableError, match="unavailable"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(licensed_sfx={"effect_id": "sfx-fah"}),
        )


def test_resolver_requires_one_exact_effect_id_or_label(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        catalog=[
            {"catalog_id": "sfx-a", "kind": "sound_effect", "label": "Fah"},
            {"catalog_id": "sfx-b", "kind": "sound_effect", "label": "Fah"},
        ],
    )

    assert capabilities.resolve_creator_sfx_catalog_ref(manifest, "") is None
    assert capabilities.resolve_creator_sfx_catalog_ref(manifest, "missing") is None
    assert capabilities.resolve_creator_sfx_catalog_ref(manifest, "Fah") is None


def test_compile_rejects_media_ids_outside_manifest(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    with pytest.raises(ValueError, match="must reference manifest media"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["clip-from-another-item"],
            ),
        )


def test_compile_preserves_explicit_native_program(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )

    assert plan.strategy.render_program == "native"
    assert "draft_guided_proposal" not in [command.command for command in plan.commands]


def test_mixed_media_timing_forces_guided_specialist_when_available(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
    )

    assert plan.strategy.render_program == "guided"
    assert plan.strategy.selected_media_ids == ["clip-1", "asset-photo-1"]
    assert "draft_guided_proposal" in [command.command for command in plan.commands]


@pytest.mark.parametrize(
    ("audio_strategy", "has_voiceover", "include_image"),
    [
        pytest.param("original_audio", False, True, id="original-audio"),
        pytest.param("licensed_music", True, True, id="voiceover-present"),
        pytest.param("licensed_music", False, False, id="video-only"),
    ],
)
def test_mixed_media_timing_preserves_native_required_paths(
    monkeypatch,
    audio_strategy: str,
    has_voiceover: bool,
    include_image: bool,
) -> None:
    _enable_guided(monkeypatch)
    media = [{"media_id": "clip-1", "kind": "video"}]
    if include_image:
        media.append({"media_id": "asset-photo-1", "kind": "image"})
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=has_voiceover,
        media=media,
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy=audio_strategy,
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
    )

    assert plan.strategy.render_program == "native"
    assert plan.strategy.selected_media_ids == ["clip-1"]
    assert "draft_guided_proposal" not in [command.command for command in plan.commands]


def test_mixed_media_timing_fails_closed_when_guided_specialist_is_unavailable(
    monkeypatch,
) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )

    with pytest.raises(
        MixedMediaTimingUnavailableError,
        match="mixed-media timing requires the guided proposal capability",
    ):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["clip-1"],
                mixed_media_timing=MixedMediaTimingProfile(
                    image_hold="very_fast",
                    video_hold="longer",
                    boundary_style="cut",
                ),
            ),
        )


def test_mixed_media_timing_fails_closed_when_guided_capability_is_not_advertised(
    monkeypatch,
) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    manifest = manifest.model_copy(
        update={
            "capabilities": {
                name: availability
                for name, availability in manifest.capabilities.items()
                if name != capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL
            }
        }
    )

    with pytest.raises(MixedMediaTimingUnavailableError):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["clip-1"],
                mixed_media_timing=MixedMediaTimingProfile(
                    image_hold="very_fast",
                    video_hold="longer",
                    boundary_style="cut",
                ),
            ),
        )


def test_guided_compile_leaves_exact_media_choice_to_the_specialist(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "clip-2", "kind": "video"},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )

    assert plan.strategy.render_program == "guided"
    assert plan.strategy.selected_media_ids == ["clip-1", "clip-2"]


def test_native_compile_excludes_pool_assets_it_cannot_render(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "image"},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=[
                "clip-1",
                "asset-11111111-1111-1111-1111-111111111111",
            ],
        ),
    )
    assert plan.strategy.selected_media_ids == ["clip-1"]

    with pytest.raises(ValueError, match="requires at least one attached clip"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["asset-11111111-1111-1111-1111-111111111111"],
            ),
        )


def test_native_manifest_requires_an_attached_clip_not_only_pool_assets(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="talking_head",
        media=[{"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "video"}],
    )

    assert manifest.render_program == "native"
    assert manifest.capabilities[capabilities.CAPABILITY_NATIVE_RENDER].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].reason_code == (
        "no_native_clip"
    )


def test_assets_only_manifest_cannot_dispatch_when_guided_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "image"}],
    )

    assert manifest.render_program == "guided"
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].reason_code == (
        "no_native_clip"
    )


def test_compile_rejects_format_whose_renderer_flag_is_off(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "subtitled_archetype_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    assert manifest.capabilities["edit_format:subtitled"].available is False
    with pytest.raises(CreatorCapabilityError, match="unavailable") as exc_info:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                render_program="native",
                selected_media_ids=["clip-1"],
            ),
        )
    assert exc_info.value.code == "edit_format_unavailable"


def test_compile_rejects_exact_opening_title_on_caption_owned_formats(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "subtitled_archetype_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="subtitled",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    with pytest.raises(CreatorStrategyError, match="opening_title is not supported") as exc_info:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="subtitled",
                opening_title="Emir Olympics",
                render_program="native",
                selected_media_ids=["clip-1"],
            ),
        )
    assert exc_info.value.code == "unsupported_treatment"


def test_compile_preserves_exact_opening_title_on_narrated_voiceover(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "narrated_archetype_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="narrated_planned",
        has_voiceover=True,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="narrated_planned",
            audio_strategy="voiceover",
            opening_title="Match Day",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )

    assert plan.strategy.opening_title == "Match Day"


def test_cadence_forces_guided_renderer_even_with_original_audio(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video", "duration_s": 10},
            {"media_id": "clip-2", "kind": "video", "duration_s": 10},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="original_audio",
            selected_media_ids=["clip-1", "clip-2"],
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["clip-1", "clip-2"], cut_duration_s=1
            ),
        ),
    )

    assert plan.strategy.render_program == "guided"
    assert plan.strategy.montage_audio is not None
    assert plan.strategy.montage_audio.preserve_source_audio is True
    assert plan.strategy.montage_audio.source_media_ids == ["clip-1", "clip-2"]


def test_cadence_with_recorded_voiceover_fails_closed(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        media=[
            {"media_id": "clip-1", "kind": "video", "duration_s": 10},
            {"media_id": "clip-2", "kind": "video", "duration_s": 10},
        ],
    )

    with pytest.raises(
        MontageCadenceUnavailableError,
        match="unavailable with a recorded voiceover",
    ):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                audio_strategy="voiceover",
                selected_media_ids=["clip-1", "clip-2"],
                montage_cadence=MontageCadenceConstraint(
                    source_media_ids=["clip-1", "clip-2"], cut_duration_s=1
                ),
            ),
        )


def test_session_compiles_agent_strategy_through_capability_service(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    receipt = compile_active_plan(
        SimpleNamespace(active_plan=None),
        manifest=manifest,
        strategy=CreativeStrategy(
            edit_format="montage",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
        ),
        summary="A fast, personal montage.",
        creator_request="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert receipt["version"] == 1
    assert receipt["creator_request"].startswith("Photos should")
    assert receipt["mixed_media_timing"] == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }
    assert receipt["edit_plan"]["commands"][-1]["command"] == "dispatch_render"
    assert receipt["original_current_edit_present"] is True
    assert receipt["original_current_edit"] is None


def test_session_receipt_pins_original_current_edit_snapshot(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
        current_edit={
            "status": "failed",
            "variant_id": "original_text",
            "edit_hash": "a" * 64,
        },
    )

    receipt = compile_active_plan(
        SimpleNamespace(active_plan=None),
        manifest=manifest,
        strategy=CreativeStrategy(edit_format="montage", selected_media_ids=["clip-1"]),
        summary="Retry this cut.",
    )

    assert receipt["original_current_edit_present"] is True
    assert receipt["original_current_edit"] == {
        "revision": 0,
        "status": "failed",
        "variant_id": "original_text",
        "edit_hash": "a" * 64,
    }


# --- KRI-121 round 2: a project holding only Visuals plans for the iPhone ----


def _visuals_only_manifest(monkeypatch, media, *, verified_features, **changes):
    _enable_guided(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "phone_render_verified_features", list(verified_features)
    )
    return capabilities.resolve_creator_manifest(
        **(
            {
                "item_id": "item-visuals",
                "edit_format": "montage",
                "media": media,
                "phone_source_media_ids": [],
                "phone_rendering_allowed": True,
                "phone_visuals_only": True,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    ("media", "verified_features", "scope"),
    [
        ([{"media_id": "asset-photo", "kind": "image"}], ["stillImages"], "all"),
        ([{"media_id": "asset-video", "kind": "video"}], ["visualVideos"], "all"),
        (
            [
                {"media_id": "asset-photo", "kind": "image"},
                {"media_id": "asset-video", "kind": "video"},
            ],
            ["stillImages", "visualVideos"],
            "all",
        ),
        (
            # The undrawable pool video is simply not selected.
            [
                {"media_id": "asset-photo", "kind": "image"},
                {"media_id": "asset-video", "kind": "video"},
            ],
            ["stillImages"],
            "selected",
        ),
    ],
    ids=["photos", "pool_videos", "both", "photo_beside_an_undrawable_video"],
)
def test_visuals_only_phone_manifest_plans_a_guided_device_render(
    monkeypatch, media, verified_features, scope
) -> None:
    manifest = _visuals_only_manifest(monkeypatch, media, verified_features=verified_features)
    for name in (
        capabilities.CAPABILITY_PHONE_SOURCE_AUDIO,
        capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL,
        capabilities.CAPABILITY_DISPATCH_RENDER,
    ):
        assert manifest.capabilities[name].available, name
    strategy = (
        CreativeStrategy(media_scope="all")
        if scope == "all"
        else CreativeStrategy(selected_media_ids=["asset-photo"])
    )
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.render_program == "guided"
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "draft_guided_proposal",
        "dispatch_render",
    ]
    # The destination is part of what a confirmation pins.
    cloud = capabilities.resolve_creator_manifest(
        item_id="item-visuals", edit_format="montage", media=media
    )
    assert cloud.manifest_hash != manifest.manifest_hash


@pytest.mark.parametrize(
    "case", ["flag_absent", "footage_attached", "nothing_drawable", "no_visuals", "disabled"]
)
def test_empty_phone_sources_stay_invalid_receipts_outside_the_visuals_only_case(
    monkeypatch, case
) -> None:
    media = [{"media_id": "asset-photo", "kind": "image"}]
    changes: dict = {}
    verified = ["stillImages"]
    if case == "flag_absent":
        changes["phone_visuals_only"] = False
    elif case == "footage_attached":
        media = [{"media_id": "clip-a", "kind": "video"}, *media]
    elif case == "nothing_drawable":
        verified = ["visualVideos"]
    elif case == "no_visuals":
        media = []
    else:
        changes["phone_rendering_allowed"] = False
    manifest = _visuals_only_manifest(monkeypatch, media, verified_features=verified, **changes)
    phone = manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO]
    assert not phone.available
    assert phone.reason_code == (
        "disabled_by_setting" if case == "disabled" else "unverified_phone_sources"
    )
    assert not manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].available


def test_visuals_only_phone_manifest_still_refuses_voiceover(monkeypatch) -> None:
    manifest = _visuals_only_manifest(
        monkeypatch,
        [{"media_id": "asset-photo", "kind": "image"}],
        verified_features=["stillImages"],
        has_voiceover=True,
    )
    phone = manifest.capabilities[capabilities.CAPABILITY_PHONE_SOURCE_AUDIO]
    assert (phone.available, phone.reason_code) == (False, "unsupported_phone_audio")


# ── KRI-118 item 1/2: story shapes (day_vlog/single_hero) under Montage ──────


def _shape_manifest(monkeypatch, *, has_voiceover: bool = False):
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "creator_montage_shapes_enabled", True)
    return capabilities.resolve_creator_manifest(
        item_id="item-shape",
        edit_format="montage",
        has_voiceover=has_voiceover,
        media=[
            {"media_id": "clip-1", "kind": "video", "duration_s": 5.0},
            {"media_id": "clip-2", "kind": "video", "duration_s": 5.0},
        ],
    )


def test_available_shape_round_trips_through_compile(monkeypatch) -> None:
    manifest = _shape_manifest(monkeypatch)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            archetype="single_hero",
            hero_media_id="clip-1",
            render_program="guided",
            media_scope="all",
        ),
    )
    assert plan.strategy.archetype == "single_hero"
    assert plan.strategy.hero_media_id == "clip-1"
    assert plan.notices == []


def test_shape_with_voiceover_is_dropped_with_a_notice(monkeypatch) -> None:
    manifest = _shape_manifest(monkeypatch, has_voiceover=True)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            archetype="day_vlog",
            render_program="native",
            audio_strategy="voiceover",
            selected_media_ids=["clip-1"],
        ),
    )
    assert plan.strategy.render_program == "native"
    assert plan.strategy.archetype is None
    assert plan.strategy.hero_media_id is None
    assert plan.notices == ["Day vlog shape needs music; kept a regular montage."]


def test_shapes_disabled_by_flag_drop_with_a_notice(monkeypatch) -> None:
    manifest = _shape_manifest(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "creator_montage_shapes_enabled", False)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            archetype="single_hero",
            hero_media_id="clip-1",
            render_program="guided",
            media_scope="all",
        ),
    )
    assert plan.strategy.archetype is None
    assert any("needs music" in notice for notice in plan.notices)


def test_stale_edit_format_day_vlog_is_rewritten_to_montage_with_a_notice(monkeypatch) -> None:
    manifest = _shape_manifest(monkeypatch)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="day_vlog",
            render_program="guided",
            media_scope="all",
        ),
    )
    assert plan.strategy.edit_format == "montage"
    assert plan.strategy.archetype == "day_vlog"
    assert plan.notices == ["Reading this as a montage in the day-vlog style."]


def test_stale_edit_format_single_hero_is_rewritten_to_montage_with_a_notice(
    monkeypatch,
) -> None:
    manifest = _shape_manifest(monkeypatch)
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="single_hero",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )
    assert plan.strategy.edit_format == "montage"
    assert plan.strategy.archetype == "single_hero"
    assert plan.notices == ["Reading this as a montage in the single-hero style."]


# --- KRI-176: subtitled (Talking) phone picture-in-picture Visuals overlays -

_MEDIA_OVERLAYS_VERIFIED_VARIANTS: dict[str, tuple[str, ...]] = {
    "full": tuple(PHONE_SUBTITLED_OVERLAY_FEATURES),
    **{
        f"missing_{feature}": tuple(f for f in PHONE_SUBTITLED_OVERLAY_FEATURES if f != feature)
        for feature in PHONE_SUBTITLED_OVERLAY_FEATURES
    },
}


@pytest.mark.parametrize("edit_format", ["subtitled", "montage", "narrated"])
@pytest.mark.parametrize("lanes_flag_on", [True, False])
@pytest.mark.parametrize("overlays_enabled", [True, False])
@pytest.mark.parametrize("verified_variant", list(_MEDIA_OVERLAYS_VERIFIED_VARIANTS))
def test_media_overlays_phone_capability_matrix(
    monkeypatch, edit_format, lanes_flag_on, overlays_enabled, verified_variant
) -> None:
    """KRI-176: `media_overlays` is available on a phone manifest iff the
    edit format is `subtitled` (Talking) AND the KRI-174 Phase 1 lane flag is
    on AND the generic `media_overlays_enabled` gate is on AND every device
    feature `PHONE_SUBTITLED_OVERLAY_FEATURES` lists is verified. Every other
    cell of this matrix must stay the blanket `unsupported_on_phone` refusal
    -- in particular montage and narrated NEVER become available regardless
    of how the flags/features resolve, since only the Talking phone compiler
    has a lane for this today."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", lanes_flag_on)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", overlays_enabled)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        list(_MEDIA_OVERLAYS_VERIFIED_VARIANTS[verified_variant]),
    )
    manifest = _phone_manifest(monkeypatch, edit_format, [{"media_id": "phone-a", "kind": "video"}])
    entry = manifest.capabilities["media_overlays"]
    should_be_available = (
        edit_format == "subtitled"
        and lanes_flag_on
        and overlays_enabled
        and verified_variant == "full"
    )
    if should_be_available:
        assert entry.available is True
    else:
        assert entry.available is False
        assert entry.reason_code == "unsupported_on_phone"


def test_media_overlays_subtitled_manifest_hash_unchanged_when_lanes_flag_off(
    monkeypatch,
) -> None:
    """A flag-off deploy must keep the exact pre-KRI-176 manifest: the
    `subtitled` phone manifest's `media_overlays` entry is byte-identical to
    a montage phone manifest's, and turning on the unrelated generic
    `media_overlays_enabled` gate (while the KRI-174 Phase 1 lane flag stays
    off) contributes nothing to the resolved manifest hash."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", False)
    media = [{"media_id": "phone-a", "kind": "video"}]

    subtitled_manifest = _phone_manifest(monkeypatch, "subtitled", media)
    montage_manifest = _phone_manifest(monkeypatch, "montage", media)
    assert (
        subtitled_manifest.capabilities["media_overlays"]
        == montage_manifest.capabilities["media_overlays"]
    )
    assert subtitled_manifest.capabilities["media_overlays"].available is False
    assert subtitled_manifest.capabilities["media_overlays"].reason_code == "unsupported_on_phone"

    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    subtitled_with_generic_gate_on = _phone_manifest(monkeypatch, "subtitled", media)
    assert subtitled_with_generic_gate_on.manifest_hash == subtitled_manifest.manifest_hash


def test_media_overlays_cloud_manifest_unaffected(monkeypatch) -> None:
    """The KRI-176 override only touches the phone branch (guarded by
    `phone_source_media_ids is not None`) -- a cloud subtitled manifest keeps
    advertising `media_overlays` purely off the generic `media_overlays_enabled`
    gate, same as before this change."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        list(PHONE_SUBTITLED_OVERLAY_FEATURES),
    )
    cloud_manifest = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="subtitled",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert cloud_manifest.capabilities["media_overlays"].available is True

    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", False)
    cloud_manifest_disabled = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="subtitled",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert cloud_manifest_disabled.capabilities["media_overlays"].available is False
    assert cloud_manifest_disabled.capabilities["media_overlays"].reason_code == (
        "disabled_by_setting"
    )


def test_media_overlays_available_phone_subtitled_manifest_compiles_overlays_treatment(
    monkeypatch,
) -> None:
    """`compile_strategy_to_plan` on an available phone `subtitled` manifest
    keeps `"overlays"` in `optional_treatments` -- mirrors
    `test_phone_manifest_never_advertises_lanes_the_phone_compiler_rejects`'s
    pattern, but for the one lane KRI-176 turns on instead of one that stays
    refused."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        list(PHONE_SUBTITLED_OVERLAY_FEATURES) + ["narrationAudio"],
    )
    manifest = _phone_manifest(monkeypatch, "subtitled", [{"media_id": "phone-a", "kind": "video"}])
    assert manifest.capabilities["media_overlays"].available is True

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="subtitled",
            audio_strategy="original_audio",
            selected_media_ids=["phone-a"],
            optional_treatments=["overlays"],
        ),
    )
    assert plan.strategy.render_program == "native"
    assert plan.strategy.optional_treatments == ["overlays"]


# --- KRI-183: phone `subtitled` VIDEO Visuals as picture-in-picture cards ---


def _kri183_verified_features(
    *, overlay_lane_supported: bool, visual_videos_verified: bool
) -> list[str]:
    verified: set[str] = {"narrationAudio"}
    if overlay_lane_supported:
        verified |= set(PHONE_SUBTITLED_OVERLAY_FEATURES)
    if visual_videos_verified:
        verified |= set(PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES)
    return list(verified)


@pytest.mark.parametrize("video_flag_on", [True, False])
@pytest.mark.parametrize("visual_videos_verified", [True, False])
@pytest.mark.parametrize("overlay_lane_supported", [True, False])
def test_media_overlay_video_cards_capability_matrix(
    monkeypatch, video_flag_on, visual_videos_verified, overlay_lane_supported
) -> None:
    """KRI-183: `media_overlays:video_cards` is present (and available) on a
    phone `subtitled` manifest iff `phone_rollout.phone_subtitled_video_
    overlays_supported()` holds -- the KRI-183 lane flag AND the base
    KRI-176 overlay lane (`phone_subtitled_media_lanes_enabled` + every
    `PHONE_SUBTITLED_OVERLAY_FEATURES` device feature, folded into
    `overlay_lane_supported` here) AND the device's verified
    `visualVideos` feature. It must never appear at all -- not even as an
    unavailable entry -- when any leg fails (mirrors the `phone_still_
    images`/`phone_visual_videos` "omit, don't mark unavailable" pattern),
    and it must never appear when the `media_overlays` capability itself
    isn't available."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "phone_subtitled_video_overlays_enabled", video_flag_on
    )
    monkeypatch.setattr(
        capabilities.settings, "phone_subtitled_media_lanes_enabled", overlay_lane_supported
    )
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        _kri183_verified_features(
            overlay_lane_supported=overlay_lane_supported,
            visual_videos_verified=visual_videos_verified,
        ),
    )

    manifest = _phone_manifest(monkeypatch, "subtitled", [{"media_id": "phone-a", "kind": "video"}])

    # Sanity: the base KRI-176 lane matches how this case was set up, so a
    # failure below is attributable to the KRI-183 gate, not a broken test.
    assert manifest.capabilities["media_overlays"].available is overlay_lane_supported

    should_be_available = overlay_lane_supported and video_flag_on and visual_videos_verified
    if should_be_available:
        entry = manifest.capabilities[capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS]
        assert entry.available is True
    else:
        assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS not in manifest.capabilities


def test_media_overlay_video_cards_absent_and_hash_unchanged_when_flag_off(monkeypatch) -> None:
    """A flag-off deploy must keep the exact pre-KRI-183 manifest: on an
    otherwise fully-eligible phone `subtitled` manifest (base overlay lane
    available, `visualVideos` verified), leaving `phone_subtitled_video_
    overlays_enabled` at its code default (`False`) is byte-identical --
    same `capabilities` dict, same `context_hash`/`manifest_hash` -- to
    explicitly setting it `False`. Turning it on is the only thing that
    changes the hash."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        _kri183_verified_features(overlay_lane_supported=True, visual_videos_verified=True),
    )
    media = [{"media_id": "phone-a", "kind": "video"}]

    baseline = _phone_manifest(monkeypatch, "subtitled", media)
    assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS not in baseline.capabilities
    assert baseline.capabilities["media_overlays"].available is True

    monkeypatch.setattr(capabilities.settings, "phone_subtitled_video_overlays_enabled", False)
    explicit_off = _phone_manifest(monkeypatch, "subtitled", media)
    assert explicit_off.model_dump(exclude={"manifest_hash"}) == baseline.model_dump(
        exclude={"manifest_hash"}
    )
    assert explicit_off.manifest_hash == baseline.manifest_hash

    monkeypatch.setattr(capabilities.settings, "phone_subtitled_video_overlays_enabled", True)
    flag_on = _phone_manifest(monkeypatch, "subtitled", media)
    assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS in flag_on.capabilities
    assert flag_on.manifest_hash != baseline.manifest_hash


@pytest.mark.parametrize("edit_format", ["montage", "narrated"])
def test_media_overlay_video_cards_absent_on_non_subtitled_phone_formats(
    monkeypatch, edit_format
) -> None:
    """Even with every KRI-183 gate satisfied, only a phone `subtitled`
    manifest can carry `media_overlays:video_cards` -- montage/narrated keep
    the blanket phone `media_overlays` refusal (KRI-176), so the video
    signal built on top of it never appears either."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        _kri183_verified_features(overlay_lane_supported=True, visual_videos_verified=True),
    )
    manifest = _phone_manifest(monkeypatch, edit_format, [{"media_id": "phone-a", "kind": "video"}])
    assert manifest.capabilities["media_overlays"].available is False
    assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS not in manifest.capabilities


def test_media_overlay_video_cards_absent_on_cloud_manifest(monkeypatch) -> None:
    """A cloud manifest never carries the phone-only video-cards signal,
    even with every gate satisfied and `edit_format="subtitled"` -- the
    KRI-183 block, like KRI-176's, only runs inside the phone branch
    (guarded by `phone_source_media_ids is not None`)."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        _kri183_verified_features(overlay_lane_supported=True, visual_videos_verified=True),
    )
    cloud_manifest = capabilities.resolve_creator_manifest(
        item_id="item-cloud",
        edit_format="subtitled",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert cloud_manifest.capabilities["media_overlays"].available is True
    assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS not in cloud_manifest.capabilities


# --- KRI-178: phone `subtitled` reaction beats (name-triggered photo/sticker
# and sound-effect pop-ins, plus a held closing shot) ------------------------


def _enable_phone_subtitled_reaction_beats(monkeypatch) -> None:
    """Every condition `phone_rollout.phone_subtitled_reaction_beats_supported()`
    checks, plus the overlay lane it builds on."""
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        list(set(PHONE_SUBTITLED_OVERLAY_FEATURES) | set(PHONE_SUBTITLED_SFX_FEATURES)),
    )


def _reaction_beats_manifest(monkeypatch, edit_format, **overrides):
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    _enable_phone_subtitled_reaction_beats(monkeypatch)
    # Visuals-pool images use the "asset-*" id convention (same as every
    # other pool-media test in this file) -- only NON-"asset-" media counts
    # as an attached phone source, so real pool images must not collide with
    # the verified phone-only video-source check.
    media = overrides.pop(
        "media",
        [
            {"media_id": "phone-a", "kind": "video"},
            {"media_id": "asset-greenwood.png", "kind": "image", "label": "Greenwood"},
            {"media_id": "asset-reject-x.png", "kind": "image", "label": "Reject X"},
            {"media_id": "asset-salah.png", "kind": "image", "label": "Salah"},
            {"media_id": "asset-goat-badge.png", "kind": "image", "label": "GOAT badge"},
        ],
    )
    catalog = overrides.pop(
        "catalog", [{"catalog_id": "sfx-buzzer", "kind": "sound_effect", "label": "Wrong buzzer"}]
    )
    return capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=media,
        catalog=catalog,
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
        **overrides,
    )


@pytest.mark.parametrize("beats_flag_on", [True, False])
@pytest.mark.parametrize("is_phone", [True, False])
@pytest.mark.parametrize("edit_format", ["subtitled", "montage"])
def test_reaction_beats_capability_matrix(monkeypatch, beats_flag_on, is_phone, edit_format):
    """`reaction_beats` is available iff phone + subtitled + media_overlays
    available + `phone_subtitled_reaction_beats_supported()`. Every other
    cell reports unavailable, with `unsupported_on_phone` when the item IS a
    phone subtitled edit but the gate fails, and `phone_talking_only`
    otherwise (cloud, or phone-but-not-subtitled)."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    _enable_phone_subtitled_reaction_beats(monkeypatch)
    monkeypatch.setattr(
        capabilities.settings, "phone_subtitled_reaction_beats_enabled", beats_flag_on
    )

    media = [{"media_id": "phone-a" if is_phone else "clip-a", "kind": "video"}]
    kwargs = dict(item_id="item-1", edit_format=edit_format, media=media)
    if is_phone:
        kwargs.update(phone_source_media_ids=["phone-a"], phone_rendering_allowed=True)
    manifest = capabilities.resolve_creator_manifest(**kwargs)

    entry = manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS]
    should_be_available = beats_flag_on and is_phone and edit_format == "subtitled"
    if should_be_available:
        assert entry.available is True
    else:
        assert entry.available is False
        if is_phone and edit_format == "subtitled":
            assert entry.reason_code == "unsupported_on_phone"
        else:
            assert entry.reason_code == "phone_talking_only"


def test_reaction_beats_capability_present_and_unavailable_by_default():
    """Flag-off default settings add exactly one new entry to any manifest --
    `reaction_beats`, unavailable -- and nothing else changes."""
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1", edit_format="montage", media=[{"media_id": "clip-a", "kind": "video"}]
    )
    entry = manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS]
    assert entry.available is False
    assert entry.reason_code == "phone_talking_only"


def test_repair_a_strips_beats_when_capability_unavailable(monkeypatch):
    """Repair (a): a strategy carrying reaction_beats/closing_media on a
    manifest where the capability is unavailable is repaired (fields
    dropped + notice), never rejected."""
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS].available is False
    strategy = CreativeStrategy(
        edit_format="montage",
        reaction_beats=[{"beat_id": "b1", "trigger": "hello", "sound": "buzzer"}],
        closing_media={"visual_id": "clip-a"},
    )
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.reaction_beats is None
    assert plan.strategy.closing_media is None
    assert any("aren't available" in notice for notice in plan.notices)


def test_repairs_b_and_c_resolve_visual_and_sound_refs(monkeypatch):
    """Repairs (b)/(c): visual_id/badge_visual_id resolve to the canonical
    manifest media_id (label or id match, case-insensitive); sound resolves
    to a canonical catalog_id when it matches, else is left as the
    creator's own words. Unresolved visuals drop just that beat/closing
    with a notice."""
    manifest = _reaction_beats_manifest(monkeypatch, "subtitled")
    assert manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS].available is True

    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        selected_media_ids=["phone-a"],
        reaction_beats=[
            {
                "beat_id": "greenwood",
                "trigger": "Mason Greenwood",
                "visual_id": "greenwood",  # matches by label, case-insensitive
                "sound": "wrong buzzer",  # matches catalog by label
            },
            {
                "beat_id": "unknown",
                "trigger": "some unknown name",
                "visual_id": "does-not-exist",
            },
            {
                "beat_id": "own-words",
                "trigger": "ding moment",
                "sound": "a triumphant ding",  # no catalog match -- kept verbatim
            },
        ],
        closing_media={
            "visual_id": "asset-salah.png",  # matches by exact media_id
            "badge_visual_id": "goat badge",  # matches by label, case-insensitive
            "from_trigger": "Salah",
        },
    )
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    beats = {beat.beat_id: beat for beat in plan.strategy.reaction_beats}
    assert set(beats) == {"greenwood", "own-words"}
    assert beats["greenwood"].visual_id == "asset-greenwood.png"
    assert beats["greenwood"].sound == "sfx-buzzer"
    assert beats["own-words"].sound == "a triumphant ding"
    assert any("Couldn't find" in notice for notice in plan.notices)

    assert plan.strategy.closing_media.visual_id == "asset-salah.png"
    assert plan.strategy.closing_media.badge_visual_id == "asset-goat-badge.png"


def test_repair_b_drops_closing_media_when_visual_unresolved(monkeypatch):
    manifest = _reaction_beats_manifest(monkeypatch, "subtitled")
    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        selected_media_ids=["phone-a"],
        closing_media={"visual_id": "no-such-photo"},
    )
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.closing_media is None
    assert any("closing photo" in notice for notice in plan.notices)


def test_repair_d_drops_licensed_sfx_when_beats_available_instead_of_raising(monkeypatch):
    """Repair (d): on a phone manifest where `sound_effects` is unavailable
    but `reaction_beats` IS available, a `licensed_sfx` request is dropped
    with a notice instead of raising CreatorSfxUnavailableError."""
    _enable_guided(monkeypatch)
    _enable_narrated_and_subtitled_flags(monkeypatch)
    _enable_phone_subtitled_reaction_beats(monkeypatch)
    manifest = _reaction_beats_manifest(monkeypatch, "subtitled")
    assert manifest.capabilities["sound_effects"].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS].available is True

    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        selected_media_ids=["phone-a"],
        licensed_sfx={"effect_id": "funny-1", "semantics": "funny_moments"},
    )
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx is None
    assert any("Sound effects on iPhone are placed" in notice for notice in plan.notices)


def test_repair_d_still_raises_when_reaction_beats_also_unavailable(monkeypatch):
    """Unchanged pre-KRI-178 behavior: when sound_effects is unavailable AND
    reaction_beats is ALSO unavailable (e.g. cloud, or montage), an explicit
    licensed_sfx request still raises rather than being silently dropped."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-a", "kind": "video"}],
    )
    assert manifest.capabilities["sound_effects"].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS].available is False
    strategy = CreativeStrategy(
        edit_format="montage",
        licensed_sfx={"effect_id": "funny-1", "semantics": "funny_moments"},
    )
    with pytest.raises(capabilities.CreatorSfxUnavailableError):
        capabilities.compile_strategy_to_plan(manifest, strategy)


def test_resolve_creator_image_media_ref_matches_id_or_label(monkeypatch):
    manifest = _reaction_beats_manifest(monkeypatch, "subtitled")
    assert (
        capabilities.resolve_creator_image_media_ref(manifest, "asset-salah.png").media_id
        == "asset-salah.png"
    )
    assert (
        capabilities.resolve_creator_image_media_ref(manifest, "SALAH").media_id
        == "asset-salah.png"
    )
    assert capabilities.resolve_creator_image_media_ref(manifest, "no-such-thing") is None
    assert capabilities.resolve_creator_image_media_ref(manifest, None) is None
    # A video source is never resolved as an image, even by id.
    assert capabilities.resolve_creator_image_media_ref(manifest, "phone-a") is None
