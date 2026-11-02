"""The chat planner knows the Talking cutaway features that shipped under parent KRI-1151.

Prod thread 6fa36e51, turn 7aa16b1a (2026-11-02 06:14 UTC, five minutes AFTER
PHONE_TALKING_MENTION_CUTAWAYS_ENABLED, PHONE_TALKING_RANGE_EDITS_ENABLED and
PHONE_TALKING_CUTAWAY_SOUND_ENABLED went true). The creator wrote:

    "Show the related parts of the talks when I talk about them. Place them in between my
     speech. Show the talks in full screen and use their audio. Place the stadium full screen
     when I talk but keep my audio"

The Main Creator (nova.creator.main v108) answered ``AskUser(reason_code=
"unsupported_audio_intercut")``: "I still can't intercut those videos with their own audio into
your main talk in this format. Should I show them as silent full-screen videos while you speak,
or switch to a Montage?" No draft, no job. The worker could have built it: a multi-clip Talking
edit shows the clips full screen where the creator mentions them (`phone_talking_mentions`) and
plays a clip's own sound with the creator's voice muted under it
(`phone_talking_cutaway_sound`). The planner never learned that: the prompt teaches
`ending_clip` / `reaction_beats` only, and the two gates are read after a draft exists.

Failure modes, written before the code (each is pinned below):

 F1 the prompt says nothing about the lane while its gates are open (the incident).
 F2 the prompt teaches the lane while a gate is closed (a flag off, one clip, a voiceover, a
    montage), or its flag-off bytes drift (the section must be the only difference).
 F3 the section lets the model keep refusing, or teaches a silent version / a Montage switch
    for an ask the Talking route can fulfil, or promises an insert that pauses the take.
 F4 the section hides that the creator's own voice is muted under a clip that plays its sound.
 F5 the sound paragraph is taught while only the mention gate is open (and the reverse).
 F6 a refusal-shaped ``ask_user`` reason code is shown as a question while the gates are open
    and the live brief asks for what the lane does (the incident turn), or the guard fires on
    a question that is not a refusal, on closed gates, or without such an ask.
 F7 the guard loops: a second refusal after the corrective re-run triggers another run (bounded
    to one; the creator then reads the plain unmet reply, `test_r7_*`).
 F8 the corrective re-run changes the prompt of every other turn (``server_note`` unset must
    add no bytes).
 F9 the finished-video receipt for a clip that plays its sound does not say the voice is muted.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import AskUser, CreativeStrategy
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput, MainCreatorOutput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services import creator_capabilities as capabilities

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1151_thread_6fa36e51.json"
THREAD = json.loads(_FIXTURE.read_text(encoding="utf-8"))
REQUEST = (
    "Show the related parts of the talks when I talk about them. Place them in between my "
    "speech. Show the talks in full screen and use their audio. Place the stadium full screen "
    "when I talk but keep my audio"
)
PROXY = "analysis-proxy-ios-"
CLIPS = [PROXY + media_id for media_id in THREAD["clip_media_ids"]]
DURATIONS = [57.97, 15.0, 15.0, 13.0, 10.0]
SECTION_MARK = "CLIPS SHOWN FULL SCREEN OVER THE TALK"
INCIDENT_CODE = "unsupported_audio_intercut"


def _gates(monkeypatch: pytest.MonkeyPatch, *, mention: bool = True, sound: bool = True) -> None:
    from tests._prod_profile import apply_prod_profile

    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)
    monkeypatch.setattr(settings, "phone_talking_mention_cutaways_enabled", mention)
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", sound)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", True)


def _manifest(*, edit_format: str = "subtitled", clips: int = 5):
    media = [
        {"media_id": media_id, "kind": "video", "duration_s": duration}
        for media_id, duration in list(zip(CLIPS, DURATIONS, strict=True))[:clips]
    ]
    return capabilities.resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format=edit_format,
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media],
        phone_rendering_allowed=True,
    )


def _agent_input(manifest, **extra: Any) -> MainCreatorInput:
    return MainCreatorInput(
        user_message=REQUEST,
        creator_request=REQUEST,
        capability_manifest=manifest,
        media_context=[{"media_id": m, "kind": "video", "filename": m} for m in CLIPS],
        conversation=[
            {"role": "assistant", "content": THREAD["first_draft_response"]},
            {"role": "user", "content": REQUEST},
        ],
        brief_enabled=True,
        **extra,
    )


def _prompt(manifest, **extra: Any) -> str:
    return MainCreatorAgent(None).render_prompt(_agent_input(manifest, **extra))


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(manifest, *, ending_open: bool = True) -> str:
    from app.agents.main_creator import _talking_cutaways_section

    found = capabilities.talking_footage_cutaways(manifest)
    assert found is not None
    return _talking_cutaways_section(found, ending_open=ending_open)


def _brief(*, only: tuple[str, ...] = ("r1", "r2", "r3", "r4")) -> CreativeBrief:
    reqs = [BriefRequirement(**r) for r in THREAD["brief"]["requirements"] if r["id"] in only]
    return CreativeBrief(version=2, requirements=reqs)


# ---------------------------------------------------------------- F1, F2, F5: availability


def test_f1_the_open_gates_are_known_to_the_planner_context(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()

    found = capabilities.talking_footage_cutaways(manifest)

    assert found is not None
    assert (found.mention, found.sound) == (True, True)
    assert SECTION_MARK in _prompt(manifest)


@pytest.mark.parametrize(
    ("mention", "sound", "expected"),
    [(True, False, (True, False)), (False, True, (False, True)), (False, False, None)],
)
def test_f5_each_gate_opens_only_its_own_part(monkeypatch, mention, sound, expected):
    _gates(monkeypatch, mention=mention, sound=sound)
    manifest = _manifest()

    found = capabilities.talking_footage_cutaways(manifest)

    assert (None if found is None else (found.mention, found.sound)) == expected
    prompt = _flat(_prompt(manifest))
    assert (SECTION_MARK in prompt) is (expected is not None)
    if expected is not None:
        assert ("voice is muted" in prompt) is expected[1]
        assert ("where the creator talks about" in prompt) is expected[0]


def _close(monkeypatch: pytest.MonkeyPatch, reason: str) -> Any:
    if reason == "one_clip":
        return _manifest(clips=1)
    if reason == "montage":
        return _manifest(edit_format="montage")
    if reason == "multi_clip_off":
        monkeypatch.setattr(settings, "talking_multi_clip_enabled", False)
    if reason == "windows_off_sound_only":
        monkeypatch.setattr(settings, "phone_talking_mention_cutaways_enabled", False)
        monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", False)
    return _manifest()


@pytest.mark.parametrize(
    "reason", ["one_clip", "montage", "multi_clip_off", "windows_off_sound_only"]
)
def test_f2_a_closed_gate_teaches_nothing(monkeypatch, reason):
    _gates(monkeypatch)
    manifest = _close(monkeypatch, reason)

    assert capabilities.talking_footage_cutaways(manifest) is None
    assert SECTION_MARK not in _prompt(manifest)


def test_f2_a_voiceover_edit_teaches_nothing(monkeypatch):
    _gates(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="subtitled",
        has_voiceover=True,
        media=[{"media_id": m, "kind": "video", "duration_s": 10.0} for m in CLIPS],
        phone_source_media_ids=CLIPS,
        phone_rendering_allowed=True,
    )

    assert capabilities.talking_footage_cutaways(manifest) is None


def test_f2_flag_off_bytes_are_the_prompt_without_the_section(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    on = _prompt(manifest)
    section = _section(manifest)
    monkeypatch.setattr(settings, "phone_talking_mention_cutaways_enabled", False)
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", False)
    off = _prompt(manifest)

    assert SECTION_MARK not in off
    assert on.count("\n" + section) == 1
    assert on.replace("\n" + section, "", 1) == off


# ---------------------------------------------------------------- F3, F4: what it teaches


def test_f3_the_section_maps_the_incident_phrasings_and_forbids_the_refusal(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    section = _section(manifest)
    assert section in _prompt(manifest)
    section = _flat(section)

    for phrase in ("when I talk about them", "in between my speech", "use their audio"):
        assert phrase in section
    assert "ask_user" in section and "never" in section.casefold()
    assert "Montage" in section  # named only to forbid it as the answer
    assert "silent version" in section  # ... and the silent offer likewise
    assert "does not pause" in section  # an overlay, not an insert that resumes the take
    assert "ending_clip" in section  # the real "at the very end" route


def test_f4_the_section_says_the_creators_voice_is_muted_under_a_clip_with_sound(monkeypatch):
    _gates(monkeypatch)
    section = _flat(_section(_manifest()))

    assert "voice is muted" in section
    assert "not heard" in section
    assert "`summary`" in section  # the reply tells the creator so before they approve


# ---------------------------------------------------------------- F6, F7: the guard


@pytest.mark.parametrize(
    "code",
    [INCIDENT_CODE, "audio_not_supported", "cannot_intercut", "feature_unavailable", "Unsupported"],
)
def test_f6_a_refusal_shaped_code_is_stale_while_the_gates_are_open(monkeypatch, code):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="I can't play their audio over your talk.",
        reason_code=code,
        options=["a", "b"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is not None


@pytest.mark.parametrize(
    "code", ["hook_candidate", "which_clip_is_yours", "missing_media", "style_choice"]
)
def test_f6_a_question_that_is_not_a_refusal_is_left_alone(monkeypatch, code):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(kind="ask_user", question="Which?", reason_code=code, options=["a", "b"])

    assert stale_capability_refusal(ask, _manifest(), _brief()) is None


def test_f6_closed_gates_and_unrelated_briefs_keep_the_refusal(monkeypatch):
    from app.kria.ask_refusals import stale_capability_refusal

    ask = AskUser(kind="ask_user", question="I can't.", reason_code=INCIDENT_CODE, options=[])
    _gates(monkeypatch, mention=False, sound=False)
    assert stale_capability_refusal(ask, _manifest(), _brief()) is None

    _gates(monkeypatch)
    unrelated = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1", kind="timing", scope="global", description="30 s", facts={"duration_s": 30}
            )
        ],
    )
    assert stale_capability_refusal(ask, _manifest(), unrelated) is None
    assert stale_capability_refusal(ask, _manifest(), None) is None
    assert stale_capability_refusal(ask, _manifest(edit_format="montage"), _brief()) is None


def _strategy_output(manifest) -> MainCreatorOutput:
    strategy = CreativeStrategy.model_validate(
        {
            "direction": "fast_montage",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 58,
            "selected_media_ids": [m.media_id for m in manifest.media],
            "rationale": "The talk stays the spine; the other clips cover it full screen.",
        }
    )
    return MainCreatorOutput.model_validate(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy.model_dump(mode="json", exclude_none=True),
                "summary": "Your voice is muted under each clip that plays its own sound.",
            }
        }
    )


def _refusal_output(code: str = INCIDENT_CODE) -> MainCreatorOutput:
    return MainCreatorOutput.model_validate(
        {
            "action": {
                "kind": "ask_user",
                "question": "I still can't intercut those videos with their own audio.",
                "reason_code": code,
                "options": ["Silent full-screen", "Switch to Montage"],
            }
        }
    )


class _Db:
    """A session stand-in that knows whether a transaction is still open."""

    def __init__(self) -> None:
        self.open = True

    async def rollback(self) -> None:
        self.open = False


async def _turn(
    monkeypatch,
    output: MainCreatorOutput,
    manifest,
    brief: CreativeBrief,
    *,
    db: Any = None,
    **extra: Any,
):
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_draft_strategy", AsyncMock(return_value=None))
    return await planner._plan_from_creator_output(
        db if db is not None else _Db(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=REQUEST,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=_agent_input(manifest),
            intent_clips=[],
            creator_request=None,
            raw_creator_request=REQUEST,
        ),
        output=output,
        effective_brief=brief,
        **extra,
    )


def _said(result) -> str:
    plan = result.plan
    labels = [o["label"] for o in (plan.choice_question or {}).get("options", [])]
    return " ".join([plan.response or "", *labels])


async def test_f6_the_incident_turn_is_rerun_not_shown(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert rerun.await_count == 1
    note = rerun.await_args.args[0].agent_input.server_note
    assert note and "Montage" in note and "out of date" in note
    said = _said(result)
    assert "Montage" not in said and "Silent full-screen" not in said
    assert result.plan.mode == "act"


async def test_f7_a_failed_rerun_keeps_the_original_answer(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(side_effect=RuntimeError("planning failed"))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert rerun.await_count == 1
    assert "Montage" in _said(result)  # fail open: the creator still gets a question


async def test_f6_a_plain_question_never_pays_for_a_rerun(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock()
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output("which_clip_is_yours"), manifest, _brief())

    rerun.assert_not_awaited()
    assert "Montage" in _said(result)


async def test_f6_closed_gates_show_the_models_refusal_as_before(monkeypatch):
    _gates(monkeypatch, mention=False, sound=False)
    manifest = _manifest()
    rerun = AsyncMock()
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    rerun.assert_not_awaited()
    assert "Montage" in _said(result)


# ---------------------------------------------------------------- F8: the note adds no bytes


def test_f8_an_unset_server_note_adds_no_bytes(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    plain = _prompt(manifest)
    noted = _prompt(manifest, server_note="Server check: x.")

    assert "server_note" not in _agent_input(manifest).model_dump_json(exclude_none=True)
    assert noted.startswith(plain.rstrip("\n"))
    assert noted.endswith("Server check: x.\n") or "Server check: x." in noted[len(plain) - 2 :]


# ---------------------------------------------------------------- F9: the receipt


def test_f9_the_finished_receipt_says_the_voice_is_muted(monkeypatch):
    from app.kria.brief_checks import PlanFacts, check_requirement

    _gates(monkeypatch)
    req = BriefRequirement(**next(r for r in THREAD["brief"]["requirements"] if r["id"] == "r2"))
    record = {
        "version": 1,
        "windows": True,
        "rows": [
            {
                "requirement_id": "r2",
                "state": "on",
                "status": "met",
                "reason_code": None,
                "on_windows": [[10.0, 14.0]],
                "off_windows": [],
                "line": None,
                "mismatch_s": 0.0,
            }
        ],
    }
    facts = PlanFacts(
        phone_talking=True,
        edit_format="subtitled",
        rendered_variant=True,
        clip_ids=tuple(CLIPS),
        clip_sound_toggles=record,
    )

    receipt = check_requirement(req, facts)

    assert receipt.status == "met"
    assert "voice" in receipt.reason and "muted" in receipt.reason


# ---------------------------------------------------------------- the replay fixture


_GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "agent_evals"
    / "main_creator"
    / "kri1151_talking_cutaways_show_when_mentioned.json"
)


def test_the_replay_fixture_renders_the_lane_and_answers_without_a_refusal(monkeypatch):
    fixture = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert fixture["meta"]["source"] == "hand_authored_golden"  # no live capture was possible
    agent_input = MainCreatorInput.model_validate(fixture["input"])
    assert agent_input.user_message == REQUEST

    _gates(monkeypatch)
    prompt = MainCreatorAgent(None).render_prompt(agent_input)
    assert SECTION_MARK in prompt

    action = fixture["output"]["action"]
    assert action["kind"] == "propose_strategy"
    assert action["strategy"]["edit_format"] == "subtitled"
    assert "muted" in action["summary"] and "voice" in action["summary"]


# ---------------------------------------------------------------- review of PR #2057, round 1
#
# Failure modes written before the fixes:
#  R1 (M1) the corrective re-run dies on a deadline / quota / budget error that
#     `_call_main_creator` re-raises, and the whole turn is lost with a valid first answer.
#  R2 (M2) the re-run holds a database transaction open across a model call.
#  R3 (M3) the finished receipt says "voice muted" for a sound-on ask that played no cutaway.
#  R4 (M4) the prompt promises more than the open gates build (sound-only, mention-only), and
#     states durations the scheduler does not use.
#  R5 (M5) the guard fires on a refusal about something else (insert, pause, footage, hook), and
#     the server note can talk the model into muting a voice the creator wanted paused.
#  R6 (f) token shapes the model really writes slip past the guard; a Montage proposal is never
#     checked.
#  R7 (a-e) a second refusal reaches the creator as the invented card; the re-run is
#     unattributed, ignores the last batch, and its answer / brief updates are dropped by the
#     caller.


def _provider_errors():
    from app.agents._runtime import (
        AiBudgetExceededError,
        ProviderDeadlineExceededError,
        ProviderOutcomeUnknownError,
        ProviderQuotaExceededError,
    )

    return [
        ProviderDeadlineExceededError("slow"),
        ProviderOutcomeUnknownError("unknown"),
        ProviderQuotaExceededError(provider="gemini"),
        AiBudgetExceededError(scope="user", reset_at="2026-11-03", cached_behavior_available=False),
    ]


@pytest.mark.parametrize("exc", _provider_errors(), ids=lambda e: type(e).__name__)
async def test_r1_a_rerun_that_cannot_finish_keeps_the_first_answer(monkeypatch, exc):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(side_effect=exc)
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert rerun.await_count == 1
    assert "Montage" in _said(result)  # the first answer, not a failed turn


async def test_r1_no_rerun_when_the_turn_has_no_room_for_a_model_call(monkeypatch):
    import time

    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    token = planner.turn_deadline.set(time.monotonic() + 25.0)
    try:
        result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())
    finally:
        planner.turn_deadline.reset(token)

    rerun.assert_not_awaited()
    assert "Montage" in _said(result)


async def test_r1_a_rerun_runs_when_the_turn_has_room(monkeypatch):
    import time

    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    token = planner.turn_deadline.set(time.monotonic() + 190.0)
    try:
        await _turn(monkeypatch, _refusal_output(), manifest, _brief())
    finally:
        planner.turn_deadline.reset(token)

    assert rerun.await_count == 1


async def test_r2_no_transaction_is_open_while_the_model_runs(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    db = _Db()
    open_during_call: list[bool] = []

    async def rerun(*_args, **_kwargs):
        open_during_call.append(db.open)
        return _strategy_output(manifest)

    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    await _turn(monkeypatch, _refusal_output(), manifest, _brief(), db=db)

    assert open_during_call == [False]


def _sound_on_receipt(*, scope: str, on_windows: list):
    from app.kria.brief_checks import PlanFacts, check_requirement
    from app.kria.clip_sound_toggle_ask import brief_sound_toggle_asks

    req = BriefRequirement(
        id="r1",
        kind="audio",
        facts={"clip_sound": "on"},
        scope=scope,
        status="open",
        literal=None,
        description="Use my own audio",
    )
    record = {
        "version": 1,
        "windows": True,
        "rows": [
            {
                "requirement_id": "r1",
                "state": "on",
                "status": "met",
                "reason_code": None,
                "on_windows": on_windows,
                "off_windows": [],
                "line": None,
                "mismatch_s": 0.0,
            }
        ],
    }
    facts = PlanFacts(
        phone_talking=True,
        edit_format="subtitled",
        rendered_variant=True,
        clip_ids=tuple(CLIPS),
        clip_sound_toggles=record,
    )
    ask = brief_sound_toggle_asks(CreativeBrief(version=1, requirements=[req]))[0]
    return check_requirement(req, facts), ask


@pytest.mark.parametrize("scope", ["global", "clip:" + CLIPS[0]])
def test_r3_a_sound_on_ask_that_played_no_cutaway_keeps_the_plain_receipt(scope):
    from app.kria.brief_checks import _toggle_summary

    receipt, ask = _sound_on_receipt(scope=scope, on_windows=[])

    assert receipt.status == "met"
    assert receipt.reason == _toggle_summary(ask)
    assert "muted" not in receipt.reason


def test_r3_a_cutaway_that_played_its_sound_says_the_voice_is_muted():
    receipt, _ask = _sound_on_receipt(scope="global", on_windows=[[10.0, 14.0]])

    assert "muted" in receipt.reason and "voice" in receipt.reason


def _section_for(monkeypatch, *, mention: bool, sound: bool) -> str:
    _gates(monkeypatch, mention=mention, sound=sound)
    return _flat(_section(_manifest()))


def test_r4_durations_in_the_prompt_are_the_ones_the_scheduler_uses(monkeypatch):
    from app.pipeline.phone_talking_cutaways import MAX_WINDOW_S
    from app.pipeline.phone_talking_mentions import SOUND_WINDOW_S

    both = _section_for(monkeypatch, mention=True, sound=True)
    spread_sound = _section_for(monkeypatch, mention=False, sound=True)
    mention_only = _section_for(monkeypatch, mention=True, sound=False)
    spread_only_numbers = f"{MAX_WINDOW_S:g} seconds"
    long_numbers = f"{SOUND_WINDOW_S:g} seconds"

    assert spread_only_numbers in both and long_numbers in both
    assert spread_only_numbers in mention_only and long_numbers not in mention_only
    assert spread_only_numbers in spread_sound
    assert long_numbers not in spread_sound  # spread scheduling never extends a sound window


def test_r4_sound_only_does_not_promise_placement_or_cover_for_the_talks(monkeypatch):
    section = _section_for(monkeypatch, mention=False, sound=True)

    assert "where the creator talks about" not in section
    assert "in between my speech" not in section
    assert "the clips that are not the creator speaking to camera" not in section
    assert "cannot be pinned to a word" in section
    assert "voice is muted" in section


def test_r4_mention_only_does_not_promise_sound(monkeypatch):
    section = _section_for(monkeypatch, mention=True, sound=False)

    assert "where the creator talks about" in section
    assert "voice is muted" not in section


def test_r4_the_section_tells_the_worker_fallbacks_and_the_stale_reply(monkeypatch):
    section = _section_for(monkeypatch, mention=True, sound=True)

    assert "out of date" in section  # the earlier "works on a montage ... not in this edit" reply
    assert "montage_audio" in section  # a source list drops the clips' sound
    assert "stays silent" in section  # a sounding clip no mention placed
    assert "order" in section  # an asked clip order keeps every clip in place


A_REFUSAL_ABOUT_SOUND = {
    "question": "I can't play those videos with their own audio over your talk. Silent instead?",
    "options": ["Silent full screen", "Switch to Montage"],
}


@pytest.mark.parametrize(
    "code",
    [
        "footage_limited",
        "hook_declined",
        "not_available_yet",
        "insert_unavailable",
        "insert_not_available",
        "unsupported_pause_and_resume",
    ],
)
def test_r5_a_refusal_about_something_else_is_left_alone(monkeypatch, code):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="I can't stop your talk, play the clip and then continue it.",
        reason_code=code,
        options=["Overlay it instead", "Put it at the end"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is None


def test_r5_a_refusal_naming_pause_is_left_alone_whatever_its_code(monkeypatch):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="Pausing your talk while the clip plays is not available. Cover it instead?",
        reason_code=INCIDENT_CODE,
        options=["Cover it", "Keep it as is"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is None


def test_r5_a_vague_code_is_stale_when_the_model_says_it_is_about_the_audio(monkeypatch):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        reason_code="feature_unavailable",
        **A_REFUSAL_ABOUT_SOUND,
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is not None


def test_r5_a_vague_code_with_nothing_about_the_asks_is_left_alone(monkeypatch):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="I can't use that many clips.",
        reason_code="feature_unavailable",
        options=["Fewer clips", "Keep all"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is None


def test_r5_the_note_speaks_plainly_and_allows_an_honest_pause_answer():
    from app.kria import ask_refusals

    for capability in (ask_refusals.CAPABILITY_MENTION, ask_refusals.CAPABILITY_SOUND):
        note = ask_refusals.correction_note(capability)
        for jargon in ("ask_user", "reason_code", "gates", "gate "):
            assert jargon not in note
        assert "pause" in note  # a real pause ask is answered honestly, not muted
        assert "out of date" in note  # the earlier "not in this edit" reply in the chat


@pytest.mark.parametrize(
    "code",
    ["unsupportedAudioIntercut", "not supported", "can't_intercut", "capability_gap"],
)
def test_r6_refusal_shapes_the_model_writes_are_read_as_refusals(monkeypatch, code):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="I can't intercut those with their own audio.",
        reason_code=code,
        options=["a", "b"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is not None


@pytest.mark.parametrize("code", ["requires_montage", "montage_only"])
def test_r6_a_montage_only_claim_is_a_refusal(monkeypatch, code):
    from app.kria.ask_refusals import stale_capability_refusal

    _gates(monkeypatch)
    ask = AskUser(
        kind="ask_user",
        question="Their audio needs another format. Switch?",
        reason_code=code,
        options=["Switch to Montage", "Keep talking edit"],
    )

    assert stale_capability_refusal(ask, _manifest(), _brief()) is not None


def _montage_switch_output(manifest) -> MainCreatorOutput:
    strategy = CreativeStrategy.model_validate(
        {
            "direction": "fast_montage",
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 58,
            "selected_media_ids": [m.media_id for m in manifest.media],
            "rationale": "A montage plays each clip with its own sound.",
        }
    )
    return MainCreatorOutput.model_validate(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy.model_dump(mode="json", exclude_none=True),
                "summary": "I'll switch to a montage.",
            }
        }
    )


async def test_r6_a_montage_switch_for_an_ask_the_talking_edit_builds_is_rerun(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _montage_switch_output(manifest), manifest, _brief())

    assert rerun.await_count == 1
    assert result.main_creator_output is rerun.return_value


async def test_r6_a_talking_strategy_is_never_rerun(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock()
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    await _turn(monkeypatch, _strategy_output(manifest), manifest, _brief())

    rerun.assert_not_awaited()


async def test_r7_a_second_refusal_is_the_honest_unmet_reply_not_the_invented_card(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    second = _refusal_output("still_unsupported_audio")
    rerun = AsyncMock(return_value=second)
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert rerun.await_count == 1
    said = _said(result)
    assert said.strip()
    assert "Montage" not in said and "Silent full-screen" not in said
    assert "unsupported" not in said
    assert result.plan.choice_question is None


async def test_r7_a_second_answer_about_something_else_stands(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    second = _refusal_output("hook_declined")
    second.action.question = "Which of the two hooks should open the video?"
    second.action.options = ["The stadium hook", "The talk hook"]
    rerun = AsyncMock(return_value=second)
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert "Which of the two hooks" in _said(result)


async def test_r7_the_rerun_is_attributed_to_the_session_and_reads_the_last_batch(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    session = uuid.uuid4()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    base = _agent_input(manifest)
    last = planner._CreatorInputs(
        agent_input=base.model_copy(update={"creator_request": "the LAST batch"}),
        intent_clips=[],
        creator_request=None,
        raw_creator_request=REQUEST,
    )

    await _turn(
        monkeypatch,
        _refusal_output(),
        manifest,
        _brief(),
        creator_agent_session_id=session,
        rerun_inputs=last,
    )

    assert rerun.await_args.kwargs["creator_agent_session_id"] == session
    sent = rerun.await_args.args[0].agent_input
    assert sent.creator_request == "the LAST batch" and sent.server_note


async def test_r7_the_caller_can_tell_which_answer_was_planned(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    first = _refusal_output()

    replaced = await _turn(monkeypatch, first, manifest, _brief())
    untouched = await _turn(monkeypatch, _refusal_output("which_clip_is_yours"), manifest, _brief())

    assert replaced.main_creator_output is rerun.return_value
    assert untouched.main_creator_output is None


def test_r7_the_reruns_new_brief_updates_are_kept_and_duplicates_are_not():
    from app.kria.brief import BriefUpdate

    asked = BriefUpdate(
        kind="style",
        scope="global",
        description="Show them full screen",
        facts={"display_mode": "fullscreen"},
    )
    same = BriefUpdate(
        kind="style",
        scope="global",
        description="Full screen please",
        facts={"display_mode": "fullscreen"},
    )
    new = BriefUpdate(
        kind="audio", scope="global", description="Their own audio", facts={"clip_sound": "on"}
    )
    rerun = SimpleNamespace(brief_updates=[same, new])

    merged = planner._updates_with_rerun(None, (asked,), rerun, creator_text=REQUEST)

    assert merged == (asked, new)
    assert planner._updates_with_rerun(None, (asked,), None, creator_text=REQUEST) == (asked,)


# ---------------------------------------------------------------- Turkish phrasing

TURKISH_REQUEST = (
    "Konuştuğum yerde ilgili konuşma kliplerini araya tam ekran koy ve kendi sesiyle çalsın. "
    "Stadyum klibini de konuşurken tam ekran koy ama benim sesim kalsın"
)


def test_the_turkish_request_reaches_a_prompt_that_teaches_the_lane(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    agent_input = _agent_input(
        manifest,
    ).model_copy(
        update={
            "user_message": TURKISH_REQUEST,
            "creator_request": TURKISH_REQUEST,
            "reply_language": "tr",
        }
    )

    prompt = _flat(MainCreatorAgent(None).render_prompt(agent_input))

    assert SECTION_MARK in prompt and "konuştuğum yerde tam ekran göster" in prompt
    assert TURKISH_REQUEST in prompt


async def test_a_turkish_refusal_about_the_audio_is_rerun(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_strategy_output(manifest))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    refusal = MainCreatorOutput.model_validate(
        {
            "action": {
                "kind": "ask_user",
                "question": "Bu klipleri kendi sesleriyle araya koyamıyorum. Sessiz mi olsun?",
                "reason_code": "feature_unavailable",
                "options": ["Sessiz tam ekran", "Montaja geç"],
            }
        }
    )

    result = await _turn(monkeypatch, refusal, manifest, _brief())

    assert rerun.await_count == 1
    assert "Montaj" not in _said(result) and "Sessiz" not in _said(result)


async def test_a_turkish_question_about_something_else_is_left_alone(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock()
    monkeypatch.setattr(planner, "_call_main_creator", rerun)
    question = MainCreatorOutput.model_validate(
        {
            "action": {
                "kind": "ask_user",
                "question": "Bu kadar çok klibi kullanamıyorum. Hangilerini çıkarayım?",
                "reason_code": "footage_limited",
                "options": ["Son ikisini", "İlk ikisini"],
            }
        }
    )

    await _turn(monkeypatch, question, manifest, _brief())

    rerun.assert_not_awaited()
