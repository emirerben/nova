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
 F7 the guard loops: a second refusal after the corrective re-run is shown again.
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
    prompt = _prompt(manifest)
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
    monkeypatch.setattr(settings, "phone_talking_mention_cutaways_enabled", False)
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", False)
    off = _prompt(manifest)

    assert SECTION_MARK not in off
    start = on.index("\n" + SECTION_MARK)
    end = on.index("\n", on.index("Say in `summary`", start))
    assert on[:start] + on[end:] == off


# ---------------------------------------------------------------- F3, F4: what it teaches


def test_f3_the_section_maps_the_incident_phrasings_and_forbids_the_refusal(monkeypatch):
    _gates(monkeypatch)
    section = _prompt(_manifest()).split(SECTION_MARK, 1)[1].split("\n\n", 1)[0]

    for phrase in ("when I talk about them", "in between my speech", "use their audio"):
        assert phrase in section
    assert "ask_user" in section and "never" in section.casefold()
    assert "Montage" in section  # named only to forbid it as the answer
    assert "silent version" in section  # ... and the silent offer likewise
    assert "does not pause" in section  # an overlay, not an insert that resumes the take
    assert "ending_clip" in section  # the real "at the very end" route


def test_f4_the_section_says_the_creators_voice_is_muted_under_a_clip_with_sound(monkeypatch):
    _gates(monkeypatch)
    section = _prompt(_manifest()).split(SECTION_MARK, 1)[1].split("\n\n", 1)[0]

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
    ask = AskUser(kind="ask_user", question="I can't.", reason_code=code, options=["a", "b"])

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


async def _turn(monkeypatch, output: MainCreatorOutput, manifest, brief: CreativeBrief):
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_draft_strategy", AsyncMock(return_value=None))
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
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
    assert note and "ask_user" in note
    said = _said(result)
    assert "Montage" not in said and "Silent full-screen" not in said
    assert result.plan.mode == "act"


async def test_f7_a_second_refusal_is_not_shown_and_does_not_loop(monkeypatch):
    _gates(monkeypatch)
    manifest = _manifest()
    rerun = AsyncMock(return_value=_refusal_output("still_unsupported"))
    monkeypatch.setattr(planner, "_call_main_creator", rerun)

    result = await _turn(monkeypatch, _refusal_output(), manifest, _brief())

    assert rerun.await_count == 1
    said = _said(result)
    assert "Montage" not in said and "can't intercut" not in said
    assert "couldn't turn that into a plan" in said  # the honest recovery, not a limit claim


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
