"""KRI-374 lane C: the creator's uploaded song in the Main Creator policy.

Covers the manifest capability gating, the byte-identical no-song contract, the
strategy repairs/refusals (compile + runtime-v2), render-program routing,
approval's audio-mode mapping and the draft-time defer set.

The PlanItem ``song_*`` columns belong to another lane, so the item is a
``SimpleNamespace`` read through ``getattr`` exactly like production does.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    ResolvedCreatorManifest,
    canonical_manifest_hash,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_USER_SONG,
    UserSongUnavailableError,
    effective_render_program,
    repair_creator_user_song,
)
from app.kria.brief_checks import _NON_VOICEOVER_AUDIO
from app.kria.strategy_policy import (
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.services import creator_capabilities as capabilities
from app.services.creator_errors import CreatorCapabilityError
from app.services.speech_cleanup_decision import resolve_next_audio_mode

CLIPS = ["phone-a", "phone-b", "phone-c"]


def _song(**update) -> SimpleNamespace:
    base = {
        "song_gcs_path": "users/u/creation-threads/t/song.m4a",
        "song_duration_s": 187.4,
        "song_analysis": {"words": [{"text": "la", "start_s": 1.0, "end_s": 1.4}]},
    }
    return SimpleNamespace(**{**base, **update})


@pytest.fixture
def golden_settings(monkeypatch):
    """The exact settings the pre-change golden hashes below were captured under."""

    settings = capabilities.settings
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    for name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(settings, name, True, raising=False)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed", "audioMix"])
    return settings


@pytest.fixture
def song_on(golden_settings, monkeypatch):
    monkeypatch.setattr(golden_settings, "edit_format_day_vlog_enabled", True)
    monkeypatch.setattr(golden_settings, "edit_format_single_hero_enabled", True)
    monkeypatch.setattr(golden_settings, "user_song_montage_enabled", True)
    return golden_settings


def _manifest(*, song=None, phone=True, edit_format="montage", has_voiceover=False):
    return capabilities.resolve_creator_manifest(
        item_id="item-song",
        edit_format=edit_format,
        media=[{"media_id": media_id, "kind": "video", "duration_s": 20.0} for media_id in CLIPS],
        phone_source_media_ids=CLIPS if phone else None,
        phone_rendering_allowed=phone,
        has_voiceover=has_voiceover,
        user_song_item=song,
    )


def _song_strategy(**update) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="montage", audio_strategy="user_song", render_program="guided", **update
    )


# ── manifest gating ────────────────────────────────────────────────────────


def test_phone_montage_with_song_advertises_the_capability(song_on) -> None:
    manifest = _manifest(song=_song())

    assert manifest.has_user_song is True
    assert manifest.user_song is not None
    assert manifest.user_song.duration_s == pytest.approx(187.4)
    assert manifest.user_song.has_lyrics is True
    assert manifest.capabilities[CAPABILITY_USER_SONG].available is True
    # A song changes the confirmation identity.
    assert manifest.manifest_hash == canonical_manifest_hash(manifest)
    assert manifest.manifest_hash != _manifest().manifest_hash


def test_song_without_lyrics_says_so(song_on) -> None:
    manifest = _manifest(song=_song(song_analysis=None))
    assert manifest.user_song is not None and manifest.user_song.has_lyrics is False
    pending = _manifest(song=_song(song_analysis={"status": "pending", "words": []}))
    assert pending.user_song is not None and pending.user_song.has_lyrics is False


def test_song_objects_with_has_lyrics_property_are_read_too(song_on) -> None:
    analysis = SimpleNamespace(has_lyrics=True)
    manifest = _manifest(song=_song(song_analysis=analysis))
    assert manifest.user_song is not None and manifest.user_song.has_lyrics is True


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"phone": False}, "phone_only"),
        ({"edit_format": "talking_head"}, "unsupported_format"),
        ({"edit_format": "subtitled"}, "unsupported_format"),
        # No narration rollout in this profile: the phone itself can't carry a voiceover.
        ({"has_voiceover": True}, "phone_only"),
    ],
)
def test_attached_song_is_unavailable_where_it_cannot_render(song_on, kwargs, reason) -> None:
    manifest = _manifest(song=_song(), **kwargs)

    capability = manifest.capabilities[CAPABILITY_USER_SONG]
    assert capability.available is False
    assert capability.reason_code == reason
    assert manifest.has_user_song is False
    assert manifest.user_song is None


def test_song_and_recorded_voiceover_are_exclusive_even_when_the_phone_can_narrate(
    song_on, monkeypatch
) -> None:
    monkeypatch.setattr(song_on, "phone_narration_rendering_enabled", True, raising=False)
    monkeypatch.setattr(
        song_on, "phone_render_verified_features", ["musicBed", "audioMix", "narrationAudio"]
    )
    manifest = _manifest(song=_song(), has_voiceover=True)

    assert manifest.capabilities[CAPABILITY_USER_SONG].reason_code == "voiceover_present"
    assert manifest.has_user_song is False


def test_kill_switch_hides_the_song(song_on, monkeypatch) -> None:
    monkeypatch.setattr(song_on, "user_song_montage_enabled", False)
    manifest = _manifest(song=_song())

    assert manifest.capabilities[CAPABILITY_USER_SONG].reason_code == "disabled_by_setting"
    assert manifest.has_user_song is False


def test_missing_device_capability_hides_the_song(song_on, monkeypatch) -> None:
    monkeypatch.setattr(song_on, "phone_render_verified_features", ["musicBed"])
    manifest = _manifest(song=_song())

    assert manifest.capabilities[CAPABILITY_USER_SONG].available is False
    assert manifest.has_user_song is False


@pytest.mark.parametrize("item", [None, SimpleNamespace(), _song(song_gcs_path=None)])
def test_no_song_adds_nothing_to_the_manifest(song_on, item) -> None:
    manifest = _manifest(song=item)

    assert CAPABILITY_USER_SONG not in manifest.capabilities
    assert manifest.has_user_song is False
    assert manifest.user_song is None
    dumped = manifest.model_dump(mode="json")
    assert "has_user_song" not in dumped and "user_song" not in dumped
    assert "user_song" not in manifest.model_dump_json(exclude_none=True)


# Golden values captured BEFORE this change (git stash of lane C), under the
# `golden_settings` fixture. If these move, a song-less manifest
# or strategy is no longer byte-identical and every in-flight confirmation
# fence would trip.
GOLDEN_CLOUD = (
    "83931d1da37b134ab005804651f59502e6817fb933d47023a9d225e1bf36c3e8",
    "85a04645bac15aebcb789c716227654ca5cd2dadc61772c81e5b4eb063f16484",
)
GOLDEN_PHONE = (
    "3a6d40e0a01a9edd331d79a4c835c525976fd3600316458eb24e03a4ba1da011",
    "cf77d6c27f7917a530c29407c71040276a364a79c5102e294635ae80455d47e3",
)
GOLDEN_DEFAULT_STRATEGY_SHA = "abc7800b5f46b2489aca8962acbab3e894a77c3f7d6dc99e18737a6310213352"
GOLDEN_ORIGINAL_AUDIO_STRATEGY_SHA = (
    "9eaac745cd13ae319f789910e0a3093548c58392982d4381a6ccb27017a7b20c"
)


def test_no_song_manifest_hashes_are_byte_identical_to_before_the_feature(golden_settings) -> None:
    cloud = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video", "duration_s": 3.0}],
        catalog=[{"catalog_id": "song-1", "kind": "music"}],
    )
    phone = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )

    assert (cloud.context_hash, cloud.manifest_hash) == GOLDEN_CLOUD
    assert (phone.context_hash, phone.manifest_hash) == GOLDEN_PHONE


def test_no_song_strategy_serialization_is_byte_identical() -> None:
    plain = CreativeStrategy()
    assert (
        hashlib.sha256(plain.model_dump_json().encode()).hexdigest() == GOLDEN_DEFAULT_STRATEGY_SHA
    )
    audio = CreativeStrategy(audio_strategy="original_audio", selected_media_ids=["a"])
    assert (
        hashlib.sha256(audio.model_dump_json(exclude_none=True).encode()).hexdigest()
        == GOLDEN_ORIGINAL_AUDIO_STRATEGY_SHA
    )
    for key in ("song_sync", "resolved_song_takes"):
        assert key not in plain.model_dump()
        assert key not in plain.model_dump(mode="json")


def test_song_fields_serialize_only_when_set() -> None:
    strategy = _song_strategy(song_sync="lipsync", resolved_song_takes=[{"media_id": "a"}])
    dumped = strategy.model_dump(mode="json")
    assert dumped["song_sync"] == "lipsync"
    assert dumped["resolved_song_takes"] == [{"media_id": "a"}]
    assert CreativeStrategy.model_validate(dumped) == strategy


def test_song_fields_stay_out_of_every_json_schema() -> None:
    properties = CreativeStrategy.model_json_schema()["properties"]
    assert "song_sync" not in properties and "resolved_song_takes" not in properties
    assert "user_song" in properties["audio_strategy"]["enum"]
    manifest_props = ResolvedCreatorManifest.model_json_schema()["properties"]
    assert "has_user_song" in manifest_props  # a plain field; only its VALUE is omitted


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lipsync", "lipsync"),
        ("lip-sync", "lipsync"),
        ("Lip Sync", "lipsync"),
        ("lip_sync", "lipsync"),
        ("background", "background"),
        ("Background", "background"),
        ("karaoke", None),
        ("", None),
        (None, None),
    ],
)
def test_song_sync_reads_spellings_and_never_rejects(raw, expected) -> None:
    assert CreativeStrategy(audio_strategy="user_song", song_sync=raw).song_sync == expected


# ── compile-time policy ────────────────────────────────────────────────────


def test_missing_song_sync_is_repaired_to_background_with_a_notice(song_on) -> None:
    manifest = _manifest(song=_song())
    plan = capabilities.compile_strategy_to_plan(manifest, _song_strategy())

    assert plan.strategy.song_sync == "background"
    assert any("background music" in notice for notice in plan.notices)
    assert plan.strategy.audio_strategy == "user_song"
    assert plan.strategy.render_program == "guided"


def test_explicit_sync_is_kept_without_a_notice(song_on) -> None:
    manifest = _manifest(song=_song())
    for sync in ("background", "lipsync"):
        plan = capabilities.compile_strategy_to_plan(manifest, _song_strategy(song_sync=sync))
        assert plan.strategy.song_sync == sync
        assert not any("background music" in notice for notice in plan.notices)


def test_lipsync_clears_the_story_shape_and_source_audio(song_on) -> None:
    manifest = _manifest(song=_song())
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        _song_strategy(
            song_sync="lipsync",
            archetype="day_vlog",
            montage_audio={"preserve_source_audio": True},
        ),
    )

    assert plan.strategy.archetype is None
    assert plan.strategy.hero_media_id is None
    assert plan.strategy.montage_audio is None
    assert any("lip-sync" in notice for notice in plan.notices)


def test_lipsync_refuses_the_guided_voiceover_contract(song_on) -> None:
    manifest = _manifest(song=_song())
    with pytest.raises(CreatorCapabilityError) as raised:
        capabilities.compile_strategy_to_plan(
            manifest,
            _song_strategy(song_sync="lipsync", execution_contract="guided_voiceover_v1"),
        )
    assert raised.value.code == "user_song_voiceover_contract"
    with pytest.raises(UserSongUnavailableError):
        effective_render_program(
            manifest, _song_strategy(song_sync="lipsync", execution_contract="guided_voiceover_v1")
        )


def test_song_strategy_without_a_song_asks_to_upload_it_first(song_on) -> None:
    manifest = _manifest(song=None)
    with pytest.raises(CreatorCapabilityError) as raised:
        capabilities.compile_strategy_to_plan(manifest, _song_strategy(song_sync="background"))
    assert raised.value.code == "user_song_missing"
    assert "Upload the song first" in str(raised.value)


@pytest.mark.parametrize(
    "case",
    ["non_phone", "flag_off", "voiceover", "talking_head"],
)
def test_song_strategy_is_refused_with_the_stable_phone_only_code(
    song_on, monkeypatch, case
) -> None:
    if case == "flag_off":
        monkeypatch.setattr(song_on, "user_song_montage_enabled", False)
    kwargs = {
        "non_phone": {"phone": False},
        "flag_off": {},
        "voiceover": {"has_voiceover": True},
        "talking_head": {"edit_format": "talking_head"},
    }[case]
    manifest = _manifest(song=_song(), **kwargs)

    with pytest.raises(CreatorCapabilityError) as raised:
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format=kwargs.get("edit_format", "montage"),
                audio_strategy="user_song",
                song_sync="lipsync",
            ),
        )
    assert raised.value.code == "user_song_phone_only"


def test_stray_song_fields_on_other_audio_strategies_are_dropped(song_on) -> None:
    manifest = _manifest(song=_song())
    strategy, notices = repair_creator_user_song(
        manifest,
        CreativeStrategy(
            audio_strategy="licensed_music",
            song_sync="lipsync",
            resolved_song_takes=[{"media_id": "a"}],
        ),
    )
    assert strategy.song_sync is None and strategy.resolved_song_takes is None
    assert notices == []
    # Nothing to drop: the very same object comes back.
    untouched = CreativeStrategy(audio_strategy="original_audio")
    assert repair_creator_user_song(manifest, untouched) == (untouched, [])


def test_non_song_strategies_compile_exactly_as_before(song_on) -> None:
    manifest = _manifest(song=None)
    plan = capabilities.compile_strategy_to_plan(
        manifest, CreativeStrategy(edit_format="montage", audio_strategy="original_audio")
    )
    assert plan.strategy.song_sync is None
    assert plan.strategy.render_program == "guided"
    assert not any("song" in notice for notice in plan.notices)


# ── render-program routing ─────────────────────────────────────────────────


@pytest.mark.parametrize("edit_format", ["montage", "day_vlog", "single_hero"])
@pytest.mark.parametrize("sync", ["background", "lipsync"])
def test_phone_plus_user_song_routes_to_guided(song_on, edit_format, sync) -> None:
    manifest = _manifest(song=_song(), edit_format=edit_format)
    strategy = CreativeStrategy(
        edit_format=edit_format,
        audio_strategy="user_song",
        song_sync=sync,
        render_program="native",
    )
    assert effective_render_program(manifest, strategy) == "guided"
    plan = capabilities.compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.render_program == "guided"


def test_guided_proposal_capability_is_required_for_the_song_lane(song_on) -> None:
    manifest = _manifest(song=_song())
    capabilities_map = dict(manifest.capabilities)
    capabilities_map["draft_guided_proposal"] = capabilities._unavailable("x", "x")
    gated = manifest.model_copy(update={"capabilities": capabilities_map})
    with pytest.raises(Exception, match="guided proposal capability"):
        effective_render_program(gated, _song_strategy(song_sync="background"))


# ── runtime-v2 boundary ────────────────────────────────────────────────────


def test_runtime_v2_checks_a_song_strategy(song_on) -> None:
    manifest = _manifest(song=_song())
    checked = check_strategy_for_runtime_v2(manifest, _song_strategy())

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.song_sync == "background"
    assert checked.strategy.render_program == "guided"
    assert any("background music" in notice for notice in checked.notices)


def test_runtime_v2_refuses_with_stable_codes_and_plain_questions(song_on) -> None:
    no_song = check_strategy_for_runtime_v2(
        _manifest(song=None), _song_strategy(song_sync="lipsync")
    )
    assert isinstance(no_song, RefusedStrategy)
    assert no_song.code == "user_song_missing"
    assert "Upload the song first" in no_song.question

    cloud = check_strategy_for_runtime_v2(
        _manifest(song=_song(), phone=False), _song_strategy(song_sync="lipsync")
    )
    assert isinstance(cloud, RefusedStrategy)
    assert cloud.code == "user_song_phone_only"
    assert cloud.question.endswith("?")


def test_runtime_v2_refuses_lipsync_with_a_voiceover_contract_before_downgrading(song_on) -> None:
    refused = check_strategy_for_runtime_v2(
        _manifest(song=_song()),
        _song_strategy(song_sync="lipsync", execution_contract="guided_voiceover_v1"),
    )
    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "user_song_voiceover_contract"


# ── approval: audio mode + draft-time defer ────────────────────────────────


def test_resolve_next_audio_mode_maps_user_song_to_song() -> None:
    with_song = SimpleNamespace(song_gcs_path="users/u/song.m4a", voiceover_gcs_path=None)
    without = SimpleNamespace(song_gcs_path=None, voiceover_gcs_path=None)
    voiced = SimpleNamespace(song_gcs_path=None, voiceover_gcs_path="v.webm")
    strategy = SimpleNamespace(audio_strategy="user_song")

    assert resolve_next_audio_mode(strategy, with_song) == "song"
    assert resolve_next_audio_mode(strategy, without) is None
    # A voiceover never turns a song strategy into the voiceover lane.
    assert resolve_next_audio_mode(strategy, voiced) is None
    # Stub items from before the column existed are fine too.
    assert resolve_next_audio_mode(strategy, SimpleNamespace(voiceover_gcs_path=None)) is None


def test_resolve_next_audio_mode_is_unchanged_for_other_strategies() -> None:
    item = SimpleNamespace(song_gcs_path="x", voiceover_gcs_path="v.webm")
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="original_audio"), item) == (
        "original"
    )
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="licensed_music"), item) == "kria"
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="voiceover"), item) == "voiceover"


def test_user_song_leaves_the_voiceover_lane_for_the_unified_montage_defer() -> None:
    assert "user_song" in _NON_VOICEOVER_AUDIO
    assert {"original_audio", "licensed_music"} <= _NON_VOICEOVER_AUDIO
    assert "voiceover" not in _NON_VOICEOVER_AUDIO
