import json

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    CreatorNarrationIdentity,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput


def _manifest() -> ResolvedCreatorManifest:
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(
                media_id=f"clip-{index:02d}-11111111-1111-1111-1111-111111111111",
                kind="video",
            )
            for index in range(45)
        ]
        + [
            CreatorMediaRef(
                media_id=f"asset-{index:02d}-22222222-2222-2222-2222-222222222222",
                kind="image",
            )
            for index in range(5)
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _input() -> MainCreatorInput:
    manifest = _manifest()
    return MainCreatorInput(
        user_message="Make this feel alive.",
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
    )


def _raw(*, audio_strategy: str, selected: list[str], montage_audio: dict | None = None) -> str:
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": audio_strategy,
                    "montage_audio": montage_audio,
                    "render_program": "guided",
                    "selected_media_ids": selected,
                    "target_duration_s": 24,
                    "rationale": "Build a concise visual arc.",
                },
                "summary": "A concise visual story.",
            }
        }
    )


def test_guided_main_creator_output_drops_opaque_media_list() -> None:
    agent_input = _input()
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.render_program == "guided"
    assert output.action.strategy.selected_media_ids == []
    assert len(output.model_dump_json()) < 1800


def test_main_creator_prompt_explains_guided_media_and_music_contracts() -> None:
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert "guided `all` or omitted scope, return `selected_media_ids: []`;" in prompt
    assert "only when the manifest catalog contains a usable music entry" in prompt
    assert "Recorded voiceover uses native" in prompt
    assert "explicit advertised `guided_voiceover_v1` contract applies" in prompt
    assert "voiceover, and audio-led formats force native" not in prompt


def test_main_creator_prompt_receives_pinned_account_direction() -> None:
    agent_input = _input().model_copy(update={"creator_direction": "- Never use drop shadows"})

    prompt = MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "Never use drop shadows" in prompt


def test_main_creator_prompt_carries_full_request_but_redacts_narration_identity() -> None:
    narration = CreatorNarrationIdentity(
        gcs_path="voiceover-uploads/user/item/voice.webm",
        generation="voice-generation-1",
        duration_s=44.7,
    )
    manifest = _manifest().model_copy(update={"has_voiceover": True, "narration": narration})
    request = "Use all uploaded media. " + ("preserve this instruction " * 200)
    agent_input = MainCreatorInput(
        user_message="Make it work.",
        creator_request=request,
        capability_manifest=manifest,
    )

    prompt = MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert request in prompt
    assert narration.gcs_path not in prompt
    assert narration.generation not in prompt


def test_main_creator_recognizes_mixed_media_timing_request() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Photos should have a very fast transition, videos can be a bit longer"
        }
    )
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None
    assert output.action.strategy.mixed_media_timing.model_dump() == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


@pytest.mark.parametrize(
    ("request_text", "model_scope", "expected_scope"),
    [
        # KRI-129: the regex is evidence FOR an explicit scope, never a veto.
        # When it finds nothing, the model's own read of the full
        # conversation now survives instead of being overwritten with None.
        ("Create an edit of the best moments.", "all", "all"),
        ("Create an edit of the best moments.", "selected", "selected"),
        ("Create an edit of the best moments.", None, None),
        # When the regex DOES fire, it still wins over whatever the model
        # said -- these three are unchanged from before KRI-129.
        ("Use all uploaded media.", "selected", "all"),
        ("Use these 17 clips, keep their original sound, and prepare a fresh edit.", None, "all"),
        ("Don't use all media; use only the selected clips.", "all", "selected"),
    ],
)
def test_main_creator_normalizes_media_scope_against_actual_request(
    request_text: str, model_scope: str | None, expected_scope: str | None
) -> None:
    agent_input = _input().model_copy(update={"user_message": request_text})
    raw = json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "native",
                    "edit_format": "montage",
                    "audio_strategy": "licensed_music",
                    "media_scope": model_scope,
                    "render_program": "native",
                    "selected_media_ids": [agent_input.capability_manifest.media[0].media_id],
                    "target_duration_s": 24,
                    "rationale": "Use the strongest moments.",
                },
                "summary": "A focused edit.",
            }
        }
    )

    output = MainCreatorAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.media_scope == expected_scope


def test_main_creator_recovers_explicit_one_second_alternation() -> None:
    base = _input()
    videos = [
        CreatorMediaRef(media_id="match-a", kind="video", duration_s=6.633),
        CreatorMediaRef(media_id="match-b", kind="video", duration_s=26.433),
    ]
    agent_input = base.model_copy(
        update={
            "user_message": (
                "Show one second from one, switch to the other one, and back and forth."
            ),
            "capability_manifest": base.capability_manifest.model_copy(update={"media": videos}),
        }
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.montage_cadence is not None
    assert output.action.strategy.montage_cadence.model_dump() == {
        "mode": "round_robin",
        "source_media_ids": ["match-a", "match-b"],
        "cut_duration_s": 1.0,
        "reuse_policy": "no_repeat",
    }
    assert output.action.strategy.render_program == "guided"


def test_main_creator_prefers_latest_cadence_revision() -> None:
    base = _input()
    videos = [
        CreatorMediaRef(media_id="match-a", kind="video", duration_s=20),
        CreatorMediaRef(media_id="match-b", kind="video", duration_s=20),
    ]
    agent_input = base.model_copy(
        update={
            "conversation": [
                {"role": "user", "content": "Alternate every 1 second without repeating."}
            ],
            "user_message": (
                "Actually, alternate every 2 seconds and allow the moments to repeat."
            ),
            "capability_manifest": base.capability_manifest.model_copy(update={"media": videos}),
        }
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.montage_cadence is not None
    assert output.action.strategy.montage_cadence.cut_duration_s == 2
    assert output.action.strategy.montage_cadence.reuse_policy == "allow_repeat"


def test_main_creator_keeps_generic_montage_audio_intent_for_guided_original_audio() -> None:
    agent_input = _input()
    source_ids = [media.media_id for media in agent_input.capability_manifest.media[:2]]
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="original_audio",
            selected=[],
            montage_audio={
                "preserve_source_audio": True,
                "preview_source_beds": True,
                "source_media_ids": source_ids,
            },
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.montage_audio is not None
    assert output.action.strategy.montage_audio.source_media_ids == source_ids


def test_main_creator_repairs_native_mixed_media_timing_to_guided() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Photos should have a very fast transition, videos can be a bit longer"
        }
    )
    raw = json.loads(
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media[:8]],
        )
    )
    raw["action"]["strategy"]["render_program"] = "native"

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.render_program == "guided"
    assert output.action.strategy.selected_media_ids == []


def test_main_creator_recognizes_timing_request_from_an_earlier_user_turn() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Yes, use that direction.",
            "conversation": [
                {
                    "role": "user",
                    "content": (
                        "Photos should have a very fast transition, videos can be a bit longer"
                    ),
                }
            ],
        }
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None


def test_main_creator_recognizes_natural_mixed_media_timing_paraphrase() -> None:
    agent_input = _input().model_copy(
        update={"user_message": "Keep the photos snappy and let the videos breathe."}
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None


@pytest.mark.parametrize(
    "message",
    [
        "Don't make the photos very fast; let the videos breathe.",
        "Keep photos snappy, but do not hold the videos longer.",
    ],
)
def test_main_creator_rejects_negated_mixed_media_timing(message: str) -> None:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["mixed_media_timing"] = {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input().model_copy(update={"user_message": message}),
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is None


def test_main_creator_drops_model_invented_timing_for_unrelated_request() -> None:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["mixed_media_timing"] = {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(),
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is None


def test_audio_led_main_creator_output_is_native_and_bounded_to_twelve_clips() -> None:
    agent_input = _input()
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="original_audio",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    strategy = output.action.strategy
    assert strategy.render_program == "native"
    assert len(strategy.selected_media_ids) == 12
    assert all(not media_id.startswith("asset-") for media_id in strategy.selected_media_ids)
    assert len(output.model_dump_json()) < 1800


@pytest.mark.parametrize(
    ("selected", "expected_count"),
    [
        ([], 12),
        (["clip-00-11111111-1111-1111-1111-111111111111"] * 2, 1),
        (["asset-00-22222222-2222-2222-2222-222222222222"], 12),
        (["invented-provider-id"], 12),
    ],
)
def test_native_main_creator_repairs_empty_duplicate_asset_and_invented_ids(
    selected: list[str],
    expected_count: int,
) -> None:
    agent_input = _input()

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="original_audio", selected=selected),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    resolved = output.action.strategy.selected_media_ids
    assert len(resolved) == expected_count
    assert len(resolved) == len(set(resolved))
    assert set(resolved) <= {
        media.media_id
        for media in agent_input.capability_manifest.media
        if not media.media_id.startswith("asset-")
    }
    assert len(output.model_dump_json()) < 1800


@pytest.mark.parametrize(
    ("words", "previous_words", "policy"),
    [
        ("Create a slow Summer in Madrid edit", "", "once"),
        ("Loop the videos", "", "allow_repeat"),
        ("Repeat the first clip at the end", "", "allow_repeat"),
        ("Make the text yellow", "Loop the videos", "allow_repeat"),
        ("Stop looping the clips", "Loop the videos", "once"),
    ],
)
def test_creator_initial_and_followup_reuse_is_grounded(words, previous_words, policy):
    agent_input = _input().model_copy(
        update={
            "user_message": words,
            "conversation": [{"role": "user", "content": previous_words}] if previous_words else [],
        }
    )
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    # The provider cannot grant itself permission to loop footage.
    raw["action"]["strategy"]["video_reuse_policy"] = "allow_repeat"
    result = MainCreatorAgent(None).parse(json.dumps(raw), agent_input)
    assert result.action.strategy.video_reuse_policy == policy
    if policy == "allow_repeat":
        assert result.action.strategy.render_program == "guided"
        assert result.action.strategy.direction == "fast_montage"


def test_schema_retry_names_failed_field_without_echoing_private_value() -> None:
    from app.agents._runtime import SchemaError

    agent = MainCreatorAgent(None)
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["caption_style"] = "private-invalid-style"
    with pytest.raises(SchemaError):
        agent.parse(json.dumps(raw), _input())
    clarification = agent.schema_clarification()
    assert "caption_style" in clarification
    assert "literal_error" in clarification
    assert "private-invalid-style" not in clarification
    agent.parse(_raw(audio_strategy="licensed_music", selected=[]), _input())
    assert "caption_style" not in agent.schema_clarification()


def test_prompt_defines_all_media_as_representative_coverage() -> None:
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert "`all` requires\n  coverage, not playing every raw file in full" in prompt
    assert "Do not\n  ask merely because total raw footage is longer than the output" in prompt
    assert 'A request such as "use these clips" or "use these 17 clips"' in prompt
    assert "do not ask the creator to choose\n  a length or pacing" in prompt


def test_main_creator_prompt_flag_off_is_byte_identical_to_pre_kri127(monkeypatch) -> None:
    """KRI-127 (`settings.clip_intents_enabled`) must not change one byte of the
    rendered prompt while the flag is off (the repo default).

    The pinned hash + length were captured from this exact `_input()` fixture
    BEFORE any KRI-127 Lane D edit touched `app/agents/main_creator.py` or
    `prompts/main_creator.txt` (`git show
    e775533d2:src/apps/api/prompts/main_creator.txt` is the pre-Lane-D text).
    A change to this hash means the flag-off render drifted -- fix the
    template/section wiring, don't re-pin the hash.
    """
    import hashlib

    from app.config import settings

    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert len(prompt) == 23709
    assert (
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        == "cfb4e55f0874b0a45292267ef1bb12dc04cfad8c0f57c8de12be98ee9797e57d"
    )


def test_main_creator_prompt_flag_on_adds_open_vocabulary_clip_intents_section(
    monkeypatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert "OPEN-VOCABULARY CLIP INTENTS" in prompt
    assert "clip_intents" in prompt
    # Diverse examples, not the literal acceptance-test phrases.
    assert "dish" in prompt
    assert "city" in prompt
    assert "dog" in prompt
    assert "pub" not in prompt.casefold()
    assert "talk to the camera" not in prompt.casefold()
