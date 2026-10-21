"""KRI-806: an `editorial_plan` that cannot run is dropped, never a refused reply.

Don Julio M1 (prod v64, turn 3424b400): the brief kept two spoken lines audible and only
suggested a song; the model paired an editorial_plan with kept clip sound and the parser refused
the whole reply on the last of three attempts ("I couldn't turn that into a plan this time").
Every case parses the recorded prod input of that turn with the real `MainCreatorAgent.parse`.

Failure modes pinned here (written before the code):

* plan + kept clip sound (`montage_audio`), non-licensed audio, a server cadence, recorded
  voiceover, photos or another format still raises and spends a retry (phone clips with no
  phone montage at all stay the montage's own refusal, unchanged);
* the drop also wipes the rest of the answer (kept lines, selection, title, length, story);
* a valid music-led plan is dropped -- for all clips, or for the creator's own selection
  ("skip the blurry shot"), which the old check compared against every clip and refused;
* a plan that invents, adds or leaves out a selected clip survives to the worker (a clip bound
  twice is a schema error the retry is told about, unchanged here);
* the drop is silent in logs, or the log line carries creator text or media ids;
* a `montage_audio` that media normalisation adds (original_audio + phone camera sound) is
  missed because the check ran before it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.agents._schemas.creator_agent import (
    CreatorMediaRef,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "agent_evals"
    / "main_creator"
    / "kri806_don_julio_kept_lines.json"
)
DROPPED = "main_creator_editorial_plan_dropped"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _input(**manifest_update) -> MainCreatorInput:
    agent_input = MainCreatorInput.model_validate(_fixture()["input"])
    if not manifest_update:
        return agent_input
    manifest = agent_input.capability_manifest.model_copy(update=manifest_update)
    return agent_input.model_copy(update={"capability_manifest": manifest})


def _ids(prefix: str) -> str:
    return next(
        media["media_id"]
        for media in _fixture()["input"]["media_context"]
        if media["filename"].startswith(prefix)
    )


ALL = [_ids(f"M1_0{index}") for index in range(1, 10)]
BLURRY = _ids("M1_04")
SELECTED = [media_id for media_id in ALL if media_id != BLURRY]


def _plan(media_ids: list[str]) -> dict:
    half = len(media_ids) // 2
    return {
        "viewer_goal": "what a lunch at Don Julio feels like",
        "beats": [
            {"purpose": "arrival and the waiter", "media_ids": media_ids[:half], "text_goal": None},
            {
                "purpose": "sauces and the toast",
                "media_ids": media_ids[half:],
                "text_goal": "name the two sauces",
            },
        ],
    }


def _recorded_strategy() -> dict:
    return json.loads(_fixture()["raw_text"])["action"]["strategy"]


def _raw(**update) -> str:
    strategy = {**_recorded_strategy(), **update}
    strategy = {key: value for key, value in strategy.items() if value is not _UNSET}
    return json.dumps(
        {"action": {"kind": "propose_strategy", "strategy": strategy, "summary": "Lunch."}}
    )


_UNSET = object()
MUSIC_LED = {"montage_audio": _UNSET, "audio_strategy": "licensed_music"}


def _parse(raw: str, agent_input: MainCreatorInput | None = None):
    with capture_logs() as logs:
        output = MainCreatorAgent(None).parse(raw, agent_input or _input())  # type: ignore[arg-type]
    assert isinstance(output.action, ProposeStrategy)
    return output.action.strategy, [entry for entry in logs if entry["event"] == DROPPED]


def test_m1_reply_drops_the_plan_and_keeps_the_kept_lines_and_the_rest() -> None:
    recorded = _recorded_strategy()

    strategy, dropped = _parse(_fixture()["raw_text"])

    assert strategy.editorial_plan is None
    assert strategy.montage_audio is not None
    assert strategy.montage_audio.preserve_source_audio is True
    assert strategy.montage_audio.source_media_ids == recorded["montage_audio"]["source_media_ids"]
    assert strategy.selected_media_ids == recorded["selected_media_ids"]
    assert strategy.media_scope == "selected"
    assert strategy.opening_title == recorded["opening_title"]
    assert strategy.target_duration_s == 15
    assert strategy.story_structure == recorded["story_structure"]
    assert strategy.audio_strategy == "licensed_music"

    assert len(dropped) == 1
    assert dropped[0]["reasons"] == ["montage_audio"]
    assert dropped[0]["beats"] == 5
    assert dropped[0]["text_goals"] == 0
    logged = json.dumps(dropped[0], default=str)
    plan = recorded["editorial_plan"]
    assert plan["viewer_goal"] not in logged
    assert not any(beat["purpose"] in logged for beat in plan["beats"])
    assert not any(media_id in logged for media_id in ALL)


@pytest.mark.parametrize(
    ("update", "agent_input", "reason"),
    [
        # Kept camera sound written as the audio strategy; normalisation then adds the
        # montage_audio the phone path needs, so both read as reasons.
        ({"audio_strategy": "original_audio", "montage_audio": _UNSET}, None, "audio_strategy"),
        ({**MUSIC_LED, "edit_format": "day_vlog"}, None, "edit_format"),
        (MUSIC_LED, _input(has_voiceover=True), "voiceover"),
        (
            MUSIC_LED,
            _input(
                media=[
                    *_input().capability_manifest.media,
                    CreatorMediaRef(
                        media_id="asset-photo-11111111-1111-1111-1111-111111111111",
                        kind="image",
                    ),
                ]
            ),
            "non_video_media",
        ),
    ],
    ids=["original_audio", "day_vlog", "recorded_voiceover", "photo_in_manifest"],
)
def test_a_plan_the_edit_cannot_run_is_dropped_not_refused(update, agent_input, reason) -> None:
    strategy, dropped = _parse(_raw(**update), agent_input)

    assert strategy.editorial_plan is None
    assert strategy.target_duration_s == 15
    assert strategy.opening_title == _recorded_strategy()["opening_title"]
    assert len(dropped) == 1
    assert reason in dropped[0]["reasons"]


def test_a_plan_beside_a_server_cadence_is_dropped() -> None:
    two = _input(media=_input().capability_manifest.media[:2])
    two = two.model_copy(update={"user_message": "Switch between them every 1 second"})
    pair = [media.media_id for media in two.capability_manifest.media]

    strategy, dropped = _parse(
        _raw(**MUSIC_LED, media_scope=_UNSET, selected_media_ids=[], editorial_plan=_plan(pair)),
        two,
    )

    assert strategy.montage_cadence is not None
    assert strategy.editorial_plan is None
    assert "montage_cadence" in dropped[0]["reasons"]


@pytest.mark.parametrize(
    "update",
    [
        {"media_scope": _UNSET, "selected_media_ids": [], "editorial_plan": _plan(ALL)},
        # "Skip the blurry shot of the empty table": the plan binds the 8 selected clips.
        {
            "media_scope": "selected",
            "selected_media_ids": SELECTED,
            "editorial_plan": _plan(SELECTED),
        },
    ],
    ids=["all_clips", "creator_selection"],
)
def test_a_music_led_plan_is_kept_exactly(update) -> None:
    strategy, dropped = _parse(_raw(**MUSIC_LED, **update))

    assert dropped == []
    assert strategy.editorial_plan is not None
    assert strategy.editorial_plan.model_dump(mode="json") == update["editorial_plan"]


@pytest.mark.parametrize(
    "plan_ids",
    [
        ALL,  # binds the clip the creator skipped
        SELECTED[:-1],  # leaves a selected clip out
        [*SELECTED[:-1], "analysis-proxy-ios-00000000-0000-0000-0000-000000000000.mp4"],
    ],
    ids=["skipped_clip", "missing_clip", "invented_clip"],
)
def test_a_plan_off_the_selected_footage_is_dropped(plan_ids) -> None:
    strategy, dropped = _parse(
        _raw(
            **MUSIC_LED,
            media_scope="selected",
            selected_media_ids=SELECTED,
            editorial_plan=_plan(plan_ids),
        )
    )

    assert strategy.editorial_plan is None
    assert strategy.selected_media_ids == SELECTED
    assert dropped[0]["reasons"] == ["coverage"]
    assert dropped[0]["text_goals"] == 1
