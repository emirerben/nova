import json

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
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


def _raw(*, audio_strategy: str, selected: list[str]) -> str:
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": audio_strategy,
                    "render_program": "guided",
                    "selected_media_ids": selected,
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
