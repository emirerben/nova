"""KRI-1117 (chat half): the creator strategy carries a Talking background, honestly.

Failure modes pinned here, each through the real manifest / repair / prompt / schema:

 1. a strategy that does not use the field serializes, hashes and schemas exactly as before;
 2. the manifest advertises the lane while its gate is closed (or on a montage / cloud edit), or
    changes its hash when closed, so the model is taught a field the worker would strip;
 3. an id outside THIS item's pool (another item's, the speaker's clip, made up) is kept (S19);
 4. a drop is silent: the creator approves a plan that quietly lost the background (S18);
 5. the creator's pick in the camera is forgotten when the agent leaves it out, or a stale pick
    is kept after its photo left the pool;
 6. the same photo is also a pop-in on a spoken word, or the closing shot;
 7. the creator prompt teaches backgrounds on an edit that cannot show one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.services import creator_capabilities as capabilities
from tests.kria.test_kri841_ending_clip_ask import (
    _LEGACY_STRATEGY,
    _LEGACY_STRATEGY_SHA256,
)

POOL_PHOTO = "asset-5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"
POOL_VIDEO = "asset-6c3a0e2f-9d4b-4a7c-8f32-8b1e5d4c3b21"
POOL_OTHER = "asset-7d4b1f30-0e5c-4b8d-9a43-9c2f6e5d4c32"
TAKE = "analysis-proxy-ios-A.mp4"
FOREIGN = "asset-00000000-0000-4000-8000-0000000000ff"


def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._prod_profile import apply_prod_profile

    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackground"],
    )


MEDIA: list[dict[str, Any]] = [
    {"media_id": TAKE, "kind": "video", "duration_s": 24.6},
    {"media_id": POOL_PHOTO, "kind": "image", "label": "beach.jpg"},
    {"media_id": POOL_VIDEO, "kind": "video", "duration_s": 8.0, "label": "waves.mp4"},
    {"media_id": POOL_OTHER, "kind": "image", "label": "desk.jpg"},
]


def _manifest(*, edit_format: str = "subtitled", pick: str | None = None, media=MEDIA):
    own = [m["media_id"] for m in media if not m["media_id"].startswith("asset-")]
    return capabilities.resolve_creator_manifest(
        item_id="item-1117",
        edit_format=edit_format,
        media=media,
        phone_source_media_ids=own,
        phone_rendering_allowed=True,
        speaker_background_pick=pick,
    )


def _strategy(**extra: Any) -> CreativeStrategy:
    return CreativeStrategy.model_validate(
        {
            "direction": "fast_montage",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 24,
            "selected_media_ids": [TAKE],
            "rationale": "Keep the take whole.",
            **extra,
        }
    )


# ------------------------------------------------------------------ 1. schema stays put


def test_a_strategy_without_a_background_serializes_and_hashes_exactly_as_before():
    strategy = CreativeStrategy.model_validate(_LEGACY_STRATEGY)
    for dump in (
        strategy.model_dump(mode="json"),
        strategy.model_dump(mode="json", exclude_none=True),
        json.loads(strategy.model_dump_json()),
    ):
        assert "background_media" not in dump
    digest = hashlib.sha256(
        json.dumps(
            strategy.model_dump(mode="json", exclude_none=True), sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert digest == _LEGACY_STRATEGY_SHA256


def test_the_strategy_round_trips_a_background_and_keeps_it_out_of_the_tool_schema():
    from app.agents._schemas.reaction_beats import BackgroundMedia
    from app.kria.registry import ApplyStrategyArguments

    strategy = _strategy(background_media={"media_id": POOL_PHOTO})
    assert isinstance(strategy.background_media, BackgroundMedia)
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert dumped["background_media"] == {"media_id": POOL_PHOTO}
    assert CreativeStrategy.model_validate(dumped) == strategy
    video = _strategy(background_media={"media_id": POOL_VIDEO, "source_start_s": 2.0})
    assert video.model_dump(mode="json")["background_media"]["source_start_s"] == 2.0
    with pytest.raises(ValueError):
        BackgroundMedia(media_id="")
    for schema in (
        CreativeStrategy.model_json_schema(),
        ApplyStrategyArguments.model_json_schema(),
    ):
        assert "background_media" not in json.dumps(schema)


def test_the_wire_schema_lets_the_main_creator_author_it():
    from app.agents._creator_wire import MODEL_HIDDEN, WireContract, main_creator_schema

    assert "background_media" in MODEL_HIDDEN
    contract = WireContract(main_creator_schema())
    assert "background_media" in contract.source["$defs"]["CreativeStrategy"]["properties"]


def test_the_render_contract_matrix_and_placed_media_know_the_field():
    from app.kria.clip_scope import placed_media
    from app.services.creator_render_contract import FIELD_MATRIX

    for path in (
        "background_media",
        "background_media.media_id",
        "background_media.source_start_s",
    ):
        assert path in FIELD_MATRIX, path
    assert POOL_PHOTO in placed_media(_strategy(background_media={"media_id": POOL_PHOTO}))


# ------------------------------------------------------------- 2. the manifest advertises it


def test_a_closed_gate_leaves_the_manifest_exactly_as_it_was(monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    closed = _manifest(pick=POOL_PHOTO)
    assert capabilities.CAPABILITY_SPEAKER_BACKGROUND not in closed.capabilities
    assert closed.speaker_background_pick is None
    assert "speaker_background" not in closed.model_dump_json()
    assert closed.context_hash == _manifest().context_hash
    assert closed.manifest_hash == _manifest().manifest_hash


def test_an_open_gate_advertises_it_on_a_phone_talking_edit_only(monkeypatch):
    _open_gate(monkeypatch)
    talking = _manifest(pick=POOL_PHOTO)
    assert talking.capabilities[capabilities.CAPABILITY_SPEAKER_BACKGROUND].available is True
    assert talking.speaker_background_pick == POOL_PHOTO
    assert talking.manifest_hash != _manifest(pick=POOL_OTHER).manifest_hash

    montage = _manifest(edit_format="montage", pick=POOL_PHOTO)
    assert capabilities.CAPABILITY_SPEAKER_BACKGROUND not in montage.capabilities
    assert montage.speaker_background_pick is None
    cloud = capabilities.resolve_creator_manifest(
        item_id="item-1117", edit_format="subtitled", media=MEDIA
    )
    assert capabilities.CAPABILITY_SPEAKER_BACKGROUND not in cloud.capabilities


def test_media_lanes_off_closes_it_even_when_the_phone_gate_is_open(monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", False)
    assert capabilities.CAPABILITY_SPEAKER_BACKGROUND not in _manifest().capabilities


# ------------------------------------------------------------------ 3-6. the server repair


def _repair(manifest, strategy):
    return capabilities.repair_creator_speaker_background(manifest, strategy)


def test_an_agents_background_is_kept_and_canonicalised(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest()
    for named in (POOL_PHOTO, "beach.jpg", POOL_PHOTO.removeprefix("asset-")):
        kept, notices = _repair(manifest, _strategy(background_media={"media_id": named}))
        assert kept.background_media is not None
        assert kept.background_media.media_id == POOL_PHOTO
        assert notices == []


def test_a_video_background_keeps_its_start_and_a_photo_drops_it(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest()
    video, _ = _repair(
        manifest, _strategy(background_media={"media_id": POOL_VIDEO, "source_start_s": 2.5})
    )
    assert video.background_media.source_start_s == 2.5
    photo, _ = _repair(
        manifest, _strategy(background_media={"media_id": POOL_PHOTO, "source_start_s": 2.5})
    )
    assert photo.background_media.source_start_s == 0.0


def test_the_camera_pick_fills_in_when_the_agent_left_it_out(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest(pick=POOL_PHOTO)
    kept, notices = _repair(manifest, _strategy())
    assert kept.background_media is not None and kept.background_media.media_id == POOL_PHOTO
    assert notices == []


def test_an_agents_own_valid_background_is_not_overwritten_by_the_pick(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest(pick=POOL_PHOTO)
    kept, _ = _repair(manifest, _strategy(background_media={"media_id": POOL_OTHER}))
    assert kept.background_media.media_id == POOL_OTHER


def test_a_pick_that_left_the_pool_is_dropped_with_a_receipt(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest(pick=FOREIGN)
    kept, notices = _repair(manifest, _strategy())
    assert kept.background_media is None
    assert notices == [capabilities.SPEAKER_BACKGROUND_NOTICES["pick_not_found"]]
    assert notices[0].startswith("Couldn't")


def test_an_id_outside_this_items_pool_is_dropped_with_a_receipt(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest()
    for foreign in (FOREIGN, TAKE, "someone-elses-photo.jpg"):
        kept, notices = _repair(manifest, _strategy(background_media={"media_id": foreign}))
        assert kept.background_media is None, foreign
        assert notices == [capabilities.SPEAKER_BACKGROUND_NOTICES["not_found"]], foreign


def test_a_foreign_agent_id_falls_back_to_a_valid_camera_pick(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest(pick=POOL_PHOTO)
    kept, notices = _repair(manifest, _strategy(background_media={"media_id": FOREIGN}))
    assert kept.background_media.media_id == POOL_PHOTO
    assert notices == [capabilities.SPEAKER_BACKGROUND_NOTICES["not_found"]]


def test_a_kind_the_phone_cannot_draw_is_dropped_with_a_receipt(monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "visualVideos"],
    )
    manifest = _manifest()
    kept, notices = _repair(manifest, _strategy(background_media={"media_id": POOL_VIDEO}))
    assert kept.background_media is None
    assert notices == [capabilities.SPEAKER_BACKGROUND_NOTICES["kind_off"]]
    photo, notices = _repair(manifest, _strategy(background_media={"media_id": POOL_PHOTO}))
    assert photo.background_media is not None and notices == []


def test_a_background_the_manifest_cannot_show_is_dropped_with_a_receipt(monkeypatch):
    _open_gate(monkeypatch)
    montage = _manifest(edit_format="montage")
    kept, notices = _repair(montage, _strategy(background_media={"media_id": POOL_PHOTO}))
    assert kept.background_media is None
    assert notices == [capabilities.SPEAKER_BACKGROUND_NOTICES["unavailable"]]
    assert notices[0].startswith("Couldn't")


def test_a_strategy_without_a_background_is_untouched_whatever_the_gate(monkeypatch):
    _open_gate(monkeypatch)
    strategy = _strategy()
    assert _repair(_manifest(), strategy) == (strategy, [])
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    assert _repair(_manifest(), strategy) == (strategy, [])


def test_the_background_photo_is_not_also_a_pop_in_or_the_closing_shot(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest()
    strategy = _strategy(
        background_media={"media_id": POOL_PHOTO},
        reaction_beats=[
            {"beat_id": "a", "trigger": "beach", "visual_id": POOL_PHOTO},
            {"beat_id": "b", "trigger": "desk", "visual_id": POOL_OTHER},
            {"beat_id": "c", "trigger": "wow", "visual_id": POOL_PHOTO, "sound": "buzzer"},
        ],
        closing_media={"visual_id": POOL_PHOTO},
    )
    kept, notices = _repair(manifest, strategy)
    assert [(b.beat_id, b.visual_id) for b in kept.reaction_beats] == [
        ("b", POOL_OTHER),
        ("c", None),
    ]
    assert kept.closing_media is None
    assert notices == [capabilities.SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE]


def test_compiling_the_plan_applies_the_repair_and_surfaces_the_notice(monkeypatch):
    _open_gate(monkeypatch)
    manifest = _manifest(pick=POOL_PHOTO)
    plan = capabilities.compile_strategy_to_plan(manifest, _strategy())
    assert plan.strategy.background_media.media_id == POOL_PHOTO

    gone = capabilities.compile_strategy_to_plan(
        _manifest(pick=FOREIGN), _strategy(background_media={"media_id": FOREIGN})
    )
    assert gone.strategy.background_media is None
    assert any(n.startswith("Couldn't") for n in gone.notices)


def test_every_background_notice_has_a_turkish_twin():
    from app.kria.strategy_policy import _KNOWN_TR

    for notice in (
        *capabilities.SPEAKER_BACKGROUND_NOTICES.values(),
        capabilities.SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE,
    ):
        assert notice in _KNOWN_TR


# ------------------------------------------------------ the camera pick lives on the thread


def test_the_pick_is_read_from_thread_state_in_either_id_spelling():
    bare = POOL_PHOTO.removeprefix("asset-")
    for media_id in (POOL_PHOTO, bare, f"  {bare} "):
        state = {"speaker_background": {"media_id": media_id}}
        assert capabilities.speaker_background_pick_from_state(state) == POOL_PHOTO
    for state in (
        {},
        None,
        {"speaker_background": None},
        {"speaker_background": {"media_id": "beach.jpg"}},
        {"speaker_background": {"media_id": 7}},
        {"speaker_background": "asset-x"},
    ):
        assert capabilities.speaker_background_pick_from_state(state) is None


# ------------------------------------------------------------------ 7. the creator prompt


def _render_prompt(manifest) -> str:
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    return MainCreatorAgent(None).render_prompt(
        MainCreatorInput(
            user_message="put the beach photo behind me",
            media_context=[{"media_id": POOL_PHOTO, "kind": "image", "filename": "beach.jpg"}],
            capability_manifest=manifest,
        )
    )


def test_the_prompt_teaches_backgrounds_only_where_the_manifest_advertises_them(monkeypatch):
    _open_gate(monkeypatch)
    open_prompt = _render_prompt(_manifest())
    assert "background_media" in open_prompt
    assert "speaker_background_pick" in open_prompt

    assert "background_media" not in _render_prompt(_manifest(edit_format="montage"))
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    assert "background_media" not in _render_prompt(_manifest())


def test_a_closed_gate_prompt_is_byte_identical_to_one_with_no_background_support(monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    closed = _render_prompt(_manifest(pick=POOL_PHOTO))
    assert closed == _render_prompt(_manifest())
    assert "PHOTO OR VIDEO BEHIND THE SPEAKER" not in closed


def test_the_prompt_version_is_bumped_for_the_new_field():
    from app.agents.main_creator import MAIN_CREATOR_PROMPT_VERSION

    assert MAIN_CREATOR_PROMPT_VERSION == "2026-10-31-v107"
