import json

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    CreatorNarrationIdentity,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MAIN_CREATOR_PROMPT_VERSION, MainCreatorAgent, MainCreatorInput


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


def test_brief_prompt_requires_explicit_versioned_target_for_changes() -> None:
    prompt = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"brief_enabled": True})
    )

    assert MAIN_CREATOR_PROMPT_VERSION == "2026-10-31-v107"
    assert '`operation`: "change"' in prompt
    assert "target_requirement_id" in prompt
    assert "expected_version" in prompt
    assert "Compatible requirements with the same kind and scope coexist" in prompt
    for fact in (
        "heading_level",
        "stack_id",
        "below_text",
        "smaller_than",
        "triggers",
        "caption_language",
    ):
        assert f"`{fact}`" in prompt or f'"{fact}"' in prompt


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


def test_main_creator_prompt_flag_off_teaches_only_transcript_label_source(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    prompt = MainCreatorAgent(None).render_prompt(_input())
    assert '"label_source":"transcript"' in prompt
    assert "OPEN-VOCABULARY CLIP INTENTS" not in prompt
    for field in ("participant_labels", "score_labels", "sport_labels", "context_label"):
        assert field not in prompt


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


# --- KRI-178: reaction beats (name/word-triggered photo/sticker + sound
# pop-ins, plus a held closing shot) on a phone `subtitled` (Talking) edit ---


def _manifest_with_reaction_beats_available() -> ResolvedCreatorManifest:
    manifest = _manifest()
    return manifest.model_copy(
        update={
            "edit_format": "subtitled",
            "capabilities": {
                **manifest.capabilities,
                "reaction_beats": CapabilityAvailability(available=True),
            },
        }
    )


def test_reaction_beats_prompt_section_omitted_when_capability_unavailable() -> None:
    """Byte-identical to before this field existed: with no `reaction_beats`
    entry on the manifest (the default `_manifest()` / `_input()` shape), the
    rendered prompt equals substituting the SAME template with the
    `$reaction_beats_section` line removed entirely and every other slot
    resolved exactly as `render_prompt` resolves it today."""
    import json as json_module
    from string import Template

    import app.agents.main_creator as main_creator_module
    from app.config import settings
    from app.pipeline.prompt_loader import _get_raw

    agent_input = _input()
    actual = MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "REACTION BEATS" not in actual
    assert "reaction_beats_section" not in actual

    raw_without_slot = (
        _get_raw("main_creator")
        .replace("$reaction_beats_section", "")
        .replace("$brief_section", "")
        .replace("$user_song_section", "")
        .replace("$voice_mode_section", "")
        .replace("$voiceover_script_section", "")
    )
    prompt_manifest = agent_input.capability_manifest.model_dump_json(
        exclude_none=True, exclude={"narration": True}
    )
    rules = Template(raw_without_slot).safe_substitute(
        clip_intents_section=(
            main_creator_module._CLIP_INTENTS_PROMPT_SECTION
            if settings.clip_intents_enabled
            else ""
        ),
        described_text_exception=(
            main_creator_module._DESCRIBED_TEXT_EXCEPTION if settings.clip_intents_enabled else ""
        ),
        story_shape_section=(
            main_creator_module._STORY_SHAPE_PROMPT_SECTION
            if main_creator_module._story_shapes_available(agent_input.capability_manifest)
            else ""
        ),
    )
    turn = Template(_get_raw("main_creator_turn")).safe_substitute(
        creator_context=agent_input.creator_context or "(not available)",
        creator_direction=agent_input.creator_direction or "(none)",
        item_context=agent_input.item_context or "(not available)",
        media_context=json_module.dumps(agent_input.media_context, ensure_ascii=False),
        capability_manifest=prompt_manifest,
        conversation=json_module.dumps(agent_input.conversation, ensure_ascii=False),
        creator_request=agent_input.creator_request or agent_input.user_message,
        user_message=agent_input.user_message,
        creative_copy_state=json_module.dumps(agent_input.creative_copy_state, ensure_ascii=False),
        # KRI-189: no clip carries facts, so the slot renders empty.
        clip_facts_note="",
    )
    expected = rules.rstrip("\n") + "\n\n" + turn
    skills = (
        main_creator_module.load_skill("edit-story")
        + "\n\n"
        + main_creator_module.load_skill("grounded-hook")
        + "\n\n"
        + main_creator_module.load_skill("meaningful-moment")
        + "\n\n"
    )
    assert actual == skills + expected


def test_reaction_beats_prompt_section_present_when_capability_available() -> None:
    agent_input = _input().model_copy(
        update={"capability_manifest": _manifest_with_reaction_beats_available()}
    )
    prompt = MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "REACTION BEATS" in prompt
    assert "reaction_beats" in prompt
    assert "closing_media" in prompt
    assert "visual_id" in prompt
    assert "Never set `licensed_sfx`" in prompt
    assert 'edit_format: "subtitled"' in prompt


def test_reaction_beats_prompt_keeps_the_creators_title() -> None:
    """KRI-851: the beats section used to end "...stays `subtitled` with no
    `opening_title`" (written in KRI-178, when the subtitled compiler refused every
    title). Phone Talking titles work since KRI-467 / KRI-514, so on a Talking edit
    with beats that line contradicted the title rules and the creator's title could be
    dropped. Beats are added alongside a requested title, never instead of it."""
    agent_input = _input().model_copy(
        update={"capability_manifest": _manifest_with_reaction_beats_available()}
    )
    prompt = " ".join(
        MainCreatorAgent(None).render_prompt(agent_input).split()  # type: ignore[arg-type]
    )

    assert "REACTION BEATS (iPhone Talking only)" in prompt
    assert "with no `opening_title`" not in prompt
    assert "Beats never replace a title" in prompt
    assert "still set `opening_title` / `closing_title`" in prompt


def test_voiceover_reaction_beats_prompt_section_is_worded_for_the_voiceover() -> None:
    """KRI-519: a phone Voiceover manifest that advertises beats gets the Voiceover
    wording -- same field contract, but never `subtitled`, and an explicit rule that a
    photo timed to the voiceover is a beat, not the guided `execution_contract` the
    iPhone chat can never run (stress kit N3)."""
    manifest = _manifest_with_reaction_beats_available().model_copy(
        update={"edit_format": "narrated_planned", "has_voiceover": True}
    )
    prompt = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"capability_manifest": manifest})
    )

    assert "REACTION BEATS (iPhone Voiceover)" in prompt
    assert "iPhone Talking only" not in prompt
    assert 'edit_format: "subtitled"' not in prompt
    assert "never set `execution_contract` for it" in prompt
    assert "when I say the medal, show my medal photo" in prompt


def test_timed_cards_prompt_section_follows_the_manifest_capability() -> None:
    """Exact-timed small cards: taught ONLY when the manifest advertises `timed_cards`;
    every other prompt (the default manifest, a Talking manifest with beats) stays as is."""
    import app.agents.main_creator as main_creator_module

    manifest = _manifest().model_copy(
        update={
            "capabilities": {
                **_manifest().capabilities,
                "timed_cards": CapabilityAvailability(available=True),
            }
        }
    )
    prompt = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"capability_manifest": manifest})
    )
    assert "SMALL CARDS AT AN EXACT TIME" in prompt
    assert '"at_s": seconds into the finished' in prompt
    assert "REACTION BEATS" not in prompt
    assert "\n\n" not in main_creator_module._TIMED_CARDS_PROMPT_SECTION

    assert "SMALL CARDS AT AN EXACT TIME" not in MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input()
    )
    beats_only = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(
            update={"capability_manifest": _manifest_with_reaction_beats_available()}
        )
    )
    assert "SMALL CARDS AT AN EXACT TIME" not in beats_only


def test_voiceover_reaction_beats_share_the_talking_field_contract() -> None:
    import app.agents.main_creator as main_creator_module

    talking = main_creator_module._REACTION_BEATS_PROMPT_SECTION
    voiceover = main_creator_module._VOICEOVER_REACTION_BEATS_PROMPT_SECTION
    fields = talking[talking.index("set `reaction_beats`:") : talking.index("Never set `licensed")]
    assert fields in voiceover
    assert '"hold_s": seconds on screen' in fields
    assert "\n\n" not in voiceover


def test_reaction_beats_prompt_section_absent_when_flag_off_but_manifest_shaped_like_montage() -> (
    None
):
    """A manifest that never advertises `reaction_beats` (e.g. flag off, or a
    non-subtitled edit) keeps rendering the exact same prompt regardless of
    edit_format -- the model is never invited to author beats it cannot use."""
    agent_input = _input()
    assert agent_input.capability_manifest.capabilities.get("reaction_beats") is None
    prompt = MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "REACTION BEATS" not in prompt


def test_parse_accepts_propose_strategy_with_reaction_beats_and_closing_media() -> None:
    agent_input = _input().model_copy(
        update={"capability_manifest": _manifest_with_reaction_beats_available()}
    )
    raw = json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "fast_montage",
                    "edit_format": "subtitled",
                    "audio_strategy": "original_audio",
                    "render_program": "native",
                    "selected_media_ids": [],
                    "target_duration_s": 24,
                    "rationale": "Talking edit with named reaction beats.",
                    "reaction_beats": [
                        {
                            "beat_id": "greenwood",
                            "trigger": "Mason Greenwood",
                            "visual_id": "asset-00-22222222-2222-2222-2222-222222222222",
                            "visual_role": "photo",
                        },
                        {
                            "beat_id": "greenwood-no",
                            "trigger": "no",
                            "after": "Mason Greenwood",
                            "visual_id": "asset-01-22222222-2222-2222-2222-222222222222",
                            "visual_role": "sticker",
                            "sound": "buzzer",
                        },
                    ],
                    "closing_media": {
                        "visual_id": "asset-04-22222222-2222-2222-2222-222222222222",
                        "from_trigger": "Salah",
                    },
                },
                "summary": "A Talking edit with named reaction beats.",
            }
        }
    )
    output = MainCreatorAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]

    assert isinstance(output.action, ProposeStrategy)
    strategy = output.action.strategy
    assert strategy.reaction_beats is not None
    assert [beat.beat_id for beat in strategy.reaction_beats] == ["greenwood", "greenwood-no"]
    assert strategy.closing_media is not None
    assert strategy.closing_media.from_trigger == "Salah"


# --- KRI-238: "Add captions" on a one-clip phone Talking edit ---

_PHONE_CLIP = "analysis-proxy-ios-4DB8A9C3-80FE-4269-9D22-7C19280DC543.mp4"


def _phone_talking_manifest(*media_ids: str) -> ResolvedCreatorManifest:
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="subtitled",
        render_program="native",
        media=[
            CreatorMediaRef(media_id=media_id, kind="video", duration_s=14.8)
            for media_id in media_ids or (_PHONE_CLIP,)
        ],
        # As resolved in prod: no guided proposals on a phone Talking edit.
        capabilities={
            "edit_format:subtitled": available,
            "phone_source_audio": available,
            "phone_format:subtitled": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _add_captions_raw(*, media_scope: str | None, nested_evidence: bool = False) -> str:
    """The Main Creator's answer to "Add captions" (local live run, 2026-10-01)."""
    strategy = {
        "direction": "native",
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "media_scope": media_scope,
        "caption_style": "auto",
        "render_program": "native",
        "selected_media_ids": [_PHONE_CLIP],
        "target_duration_s": 14.8,
        "rationale": "Play the whole clip so every spoken line gets a caption.",
    }
    evidence = {"opening_title": None, "licensed_sfx": None}
    if nested_evidence:
        strategy["render_intent_evidence"] = evidence
    action = {
        "kind": "propose_strategy",
        "strategy": strategy,
        "summary": "I'll add captions to your clip and keep its original audio.",
    }
    if not nested_evidence:
        action["render_intent_evidence"] = evidence
    return json.dumps({"action": action})


def _phone_talking_input(manifest: ResolvedCreatorManifest) -> MainCreatorInput:
    return MainCreatorInput(user_message="Add captions", capability_manifest=manifest)


def test_all_media_scope_on_one_clip_phone_talking_edit_is_that_clip() -> None:
    # Prod thread 467a02c4: "all" routed to guided proposals, which a phone
    # Talking edit never has, so the answer was refused on every attempt.
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _add_captions_raw(media_scope="all"), _phone_talking_input(_phone_talking_manifest())
    )

    assert isinstance(output.action, ProposeStrategy)
    strategy = output.action.strategy
    assert strategy.media_scope == "selected"
    assert strategy.selected_media_ids == [_PHONE_CLIP]
    assert strategy.render_program == "native"
    assert strategy.edit_format == "subtitled"


def test_all_media_scope_stays_strict_when_native_cannot_cover_every_clip() -> None:
    from app.agents._runtime import SchemaError

    manifest = _phone_talking_manifest("clip-a.mp4", "clip-b.mp4")
    raw = _add_captions_raw(media_scope="all").replace(_PHONE_CLIP, "clip-a.mp4")
    agent = MainCreatorAgent(None)  # type: ignore[arg-type]

    with pytest.raises(SchemaError):
        agent.parse(raw, _phone_talking_input(manifest))
    # The retry is told which rule failed instead of repeating the answer blind.
    assert "all-media scope requires the guided proposal capability" in (
        agent.schema_clarification()
    )


def test_render_intent_evidence_nested_in_strategy_is_moved_beside_it() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _add_captions_raw(media_scope=None, nested_evidence=True),
        _phone_talking_input(_phone_talking_manifest()),
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.render_intent_evidence is not None


# ── KRI-586: prod 2026-10-11, thread e3971fd6. Asked to write a hook, the model had no
# documented `creative_decision` shape and returned these three, all rejected x3. ──


def _hook_raw(decision: object, *, kind: str = "ask_user", nested: bool = False) -> str:
    action: dict = {
        "kind": kind,
        "question": "How about Sunrise miles as your opening hook?",
        "reason_code": "hook_candidate",
        "options": [],
    }
    data: dict = {"action": action}
    if nested:
        action["creative_decision"] = decision
    else:
        data["creative_decision"] = decision
    return json.dumps(data)


@pytest.mark.parametrize(
    "decision",
    [
        # attempt 2 of the incident: resolution/text instead of status/proposed_text
        {"target": "opening_title", "resolution": "candidate", "text": "Marathon morning vibes"},
        # attempt 3: requirement id as target, `candidate` instead of proposed_text
        {"target": "r1", "status": "candidate", "candidate": "Race day sunrise", "language": "en"},
        # attempt 1: requirement id target + `state`
        {"target": "r1", "state": "candidate", "candidate": "Sunrise steps", "language": "en"},
        # the server's own state row echoed back, with its extra digest key
        {
            "target": "opening_title",
            "status": "generate",
            "candidate": "Sunrise miles",
            "dependency_digest": "abc",
        },
    ],
)
def test_incident_creative_decision_shapes_parse_as_inert_candidates(decision: dict) -> None:
    output = MainCreatorAgent(None).parse(_hook_raw(decision), _input())  # type: ignore[arg-type]

    assert output.creative_decision is not None
    assert output.creative_decision.target == "opening_title"
    assert output.creative_decision.status == "candidate"
    assert output.creative_decision.proposed_text


def test_creative_decision_nested_in_action_is_hoisted() -> None:
    decision = {"target": "opening_title", "status": "candidate", "proposed_text": "Sunrise miles"}
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _hook_raw(decision, nested=True), _input()
    )

    assert output.creative_decision is not None
    assert output.creative_decision.proposed_text == "Sunrise miles"


def test_echoed_server_owned_states_never_become_consent() -> None:
    for status in ("approved", "stale"):
        decision = {"target": "opening_title", "status": status, "candidate": "Sunrise miles"}
        output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
            _hook_raw(decision), _input()
        )
        assert output.creative_decision is None


def test_repair_never_invents_creator_supplied_or_its_target() -> None:
    agent = MainCreatorAgent(None)
    # "write_my_own" is the server waiting for the creator: an inert `unresolved`, not supplied.
    waiting = {"target": "opening_title", "status": "write_my_own"}
    parsed = agent.parse(_hook_raw(waiting), _input())  # type: ignore[arg-type]
    assert parsed.creative_decision is not None
    assert parsed.creative_decision.status == "unresolved"
    # A consent-bearing status keeps a strict target: a requirement id is still a schema error.
    from app.agents._runtime import SchemaError

    supplied = {
        "target": "r1",
        "status": "creator_supplied",
        "proposed_text": "My words",
        "source_evidence": "My words",
    }
    with pytest.raises(SchemaError):
        agent.parse(_hook_raw(supplied), _input())  # type: ignore[arg-type]


def test_creative_decision_failure_names_the_object_and_class_without_values() -> None:
    from app.agents._runtime import SchemaError

    agent = MainCreatorAgent(None)
    bad = {"target": "opening_title", "status": "PRIVATE-WORDS"}
    with pytest.raises(SchemaError) as exc_info:
        agent.parse(_hook_raw(bad), _input())  # type: ignore[arg-type]

    clarification = agent.schema_clarification()
    assert "creative_decision" in clarification
    assert "proposed_text" in clarification
    assert "PRIVATE-WORDS" not in clarification
    error_class = exc_info.value.error_class  # type: ignore[attr-defined]
    assert error_class == "creative_decision:status:literal_error"
    assert "PRIVATE-WORDS" not in error_class
    import re

    assert re.fullmatch(r"[a-z_:,]{1,80}", error_class)


def test_strategy_rationale_beside_strategy_is_accepted() -> None:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["rationale"] = raw["action"]["strategy"].pop("rationale")

    output = MainCreatorAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.rationale == "Build a concise visual arc."


def test_missing_rationale_everywhere_is_still_a_classed_failure() -> None:
    from app.agents._runtime import SchemaError

    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"].pop("rationale")
    with pytest.raises(SchemaError) as exc_info:
        MainCreatorAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]

    assert exc_info.value.error_class == "strategy_rationale_missing"  # type: ignore[attr-defined]


def test_prompt_documents_the_creative_decision_contract() -> None:
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    for key in ("proposed_text", "source_evidence", "opening_title", "closing_title"):
        assert key in prompt
    assert "BESIDE `action`" in prompt


# ── KRI-687: prod 2026-10-15, thread 1390E686. A rich multi-part montage prompt ended in "I
# couldn't turn that into a plan": one story line ran past 120 characters (attempts 2-3) and
# the first attempt wrote `summary` inside `strategy` as well as beside it (extra_forbidden).
# Every case feeds raw model JSON through the same `parse` entry point the agent runs. ──

_LONG_STORY_LINE = (
    "Open with the title for three seconds, then cut through the boat and cabin clips in the "
    "order they were filmed, and finish on your spoken sign-off."
)


def _story_raw(
    story_structure: object,
    *,
    top_summary: object = "A concise visual story.",
    nested_summary: object = None,
    extra_strategy: dict | None = None,
) -> str:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["story_structure"] = story_structure
    raw["action"]["summary"] = top_summary
    if nested_summary is not None:
        raw["action"]["strategy"]["summary"] = nested_summary
    raw["action"]["strategy"].update(extra_strategy or {})
    return json.dumps(raw)


def _parse_story(raw: str) -> ProposeStrategy:
    output = MainCreatorAgent(None).parse(raw, _input())  # type: ignore[arg-type]
    assert isinstance(output.action, ProposeStrategy)
    return output.action


def test_overlong_story_line_is_trimmed_at_a_word_instead_of_failing_the_turn() -> None:
    assert len(_LONG_STORY_LINE) > 120
    lines = ["Hook the viewer", _LONG_STORY_LINE, "Land the payoff"]

    got = _parse_story(_story_raw(lines)).strategy.story_structure

    # Same count and order; only the one over-long line changes.
    assert len(got) == 3
    assert got[0] == lines[0] and got[2] == lines[2]
    assert len(got[1]) <= 120
    assert got[1].endswith("…")
    kept = got[1].removesuffix("…")
    assert _LONG_STORY_LINE.startswith(kept)
    # Cut between words, never inside one.
    assert _LONG_STORY_LINE[len(kept)] in " ,;:"


def test_overlong_story_line_with_a_sentence_end_keeps_the_whole_first_sentence() -> None:
    first = "Open on the harbour at sunrise with the title card held for three seconds."
    line = f"{first} Then cut through the cabin and deck clips in the order they were filmed."
    assert len(line) > 120

    got = _parse_story(_story_raw([line])).strategy.story_structure

    assert got == [first]


@pytest.mark.parametrize(
    "line",
    ["x" * 300, "ab" * 70 + " " + "cd" * 90],
    ids=["one_long_token", "no_space_in_range"],
)
def test_overlong_story_line_without_a_usable_boundary_is_still_cut_to_fit(line: str) -> None:
    got = _parse_story(_story_raw([line])).strategy.story_structure

    assert len(got) == 1
    assert 0 < len(got[0]) <= 120


def test_story_line_padded_with_whitespace_is_only_stripped_not_shortened() -> None:
    line = "Open with the title"
    got = _parse_story(_story_raw([f"   {line}" + " " * 150])).strategy.story_structure

    assert got == [line]


@pytest.mark.parametrize("bad", [[""], ["   "], ["fine", " \n "], [5], [None]])
def test_blank_or_non_text_story_lines_are_still_rejected(bad: list) -> None:
    from app.agents._runtime import SchemaError

    with pytest.raises(SchemaError):
        MainCreatorAgent(None).parse(_story_raw(bad), _input())  # type: ignore[arg-type]


def test_more_than_eight_story_lines_are_still_rejected() -> None:
    from app.agents._runtime import SchemaError

    lines = [f"beat {n}" for n in range(9)]
    with pytest.raises(SchemaError):
        MainCreatorAgent(None).parse(_story_raw(lines), _input())  # type: ignore[arg-type]


def test_stored_strategy_schema_stays_strict_about_story_line_length() -> None:
    from pydantic import ValidationError

    from app.agents._schemas.creator_agent import CreativeStrategy

    with pytest.raises(ValidationError):
        CreativeStrategy(story_structure=["x" * 121])


def test_summary_written_both_beside_and_inside_strategy_keeps_the_documented_one() -> None:
    action = _parse_story(
        _story_raw(["Hook"], top_summary="The real summary.", nested_summary="A copy.")
    )

    assert action.summary == "The real summary."


def test_nested_summary_fills_a_blank_summary_beside_strategy() -> None:
    action = _parse_story(_story_raw(["Hook"], top_summary="", nested_summary="Moved up."))

    assert action.summary == "Moved up."


@pytest.mark.parametrize("nested", [["a list", "of bullets"], {"text": "an object"}])
def test_unusable_nested_summary_is_dropped_when_the_documented_one_exists(nested: object) -> None:
    action = _parse_story(_story_raw(["Hook"], nested_summary=nested))

    assert action.summary == "A concise visual story."


def test_other_unknown_strategy_keys_are_still_rejected() -> None:
    from app.agents._runtime import SchemaError

    # A misspelt real field (here the SFX one) must stop the turn, not vanish silently.
    raw = _story_raw(["Hook"], extra_strategy={"licenced_sfx": {"name": "fah"}})
    with pytest.raises(SchemaError):
        MainCreatorAgent(None).parse(raw, _input())  # type: ignore[arg-type]


def test_render_intent_evidence_written_twice_is_still_rejected() -> None:
    from app.agents._runtime import SchemaError

    raw = json.loads(_story_raw(["Hook"]))
    raw["action"]["render_intent_evidence"] = {"opening_title": "A"}
    raw["action"]["strategy"]["render_intent_evidence"] = {"opening_title": "B"}
    with pytest.raises(SchemaError):
        MainCreatorAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]


def test_the_prod_turn_with_both_problems_parses_on_the_first_attempt() -> None:
    raw = _story_raw(
        ["Hook the viewer", _LONG_STORY_LINE],
        top_summary="The real summary.",
        nested_summary="A copy.",
    )

    action = _parse_story(raw)

    assert action.summary == "The real summary."
    assert all(len(line) <= 120 for line in action.strategy.story_structure)
