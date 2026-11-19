"""KRI-1464: clips the creator took out of a restated request come back on a later tap.

Kria TikTok video 1 (thread 78787267, 2026-11-14, build 1996 / protocol 51, server 3feec88a9),
a phone Voiceover chat. The 21:42 prompt asked to show eight things on spoken words; three of
them ("the screen recording of typing in the Kria chat", "... of the photo library", "... of
the voice recorder") were never uploaded and the 21:43 reply asked which clips they are. The
creator did not use the picker: at 21:47 they typed the request again without the three. The
brief took them out (r3, r8, r9 ``superseded``, ``superseded_by: "creator"``, v2) and the next
replies asked about other things. At 21:51:04 a "Make it without these" tap made the turn plan
the clip asks again, and the 21:51:34 reply was the 21:43 clip question word for word.

What the recording proves (``kri1464_restated_request.json`` says what is recorded and what is
reconstructed):

* The clip-intent planner reads every creator message joined (`_full_creator_request`), so on
  a turn whose latest message is a tap it wrote ``include_satz`` / ``include_galerie`` /
  ``include_meine_stimme`` again from the first message. Its prompt cancels an earlier ask only
  on an explicit correction; a restatement that leaves it out is not one.
* Nothing between the planner's inventory and the resolver looks at the brief for `include`
  asks, so the three superseded requirements reached the creator as picker rows. KRI-1111
  closed the same gap for authored labels and for a first / last clip only.

Ways this can fail, written before the fix (each is pinned below):

 F1 an `include` ask whose only brief requirement the creator withdrew is planned again from an
    older message and asked with the picker (the incident's three rows, and its reply).
 F2 the replay is not the incident: the planner did not read the recorded words, or the unfixed
    reply differs from the recorded one, so a pass proves nothing.
 F3 over-reach on a live ask: a clip the brief still asks for is dropped because a withdrawn
    requirement also names it (the creator restated "the train", or asked for it again later).
 F4 over-reach on the server's own waivers: a requirement the server left out ("Make it
    without these", ``superseded_by: "server"``) is not the creator's word and retires nothing.
 F5 over-reach without a brief record: an ask the brief never recorded (a brief model miss), or
    no brief at all, is planned and asked as today.
 F6 over-reach on other ops: a leave-out (`exclude`) for the same words is dropped, which would
    put a clip the creator removed back into the edit.
 F7 a near miss is treated as the same thing: a longer ask that only shares words with the
    withdrawn one is retired, or a shorter one ("the screen recording") while a live
    requirement still names a screen recording.
 F8 the fix only covers a fresh planner answer: an inventory reused for a picker tap (KRI-950)
    still carries the withdrawn rows.
 F9 the kept asks change: the five live asks are no longer resolved, or the turn asks about
    them instead.

Runs the real runtime-v2 planner path (`_plan_from_creator_output`): the real clip-intent
planning (`plan_and_resolve_clip_intents`), resolver plumbing and question builder, over the
recorded brief. Only the model transport is replayed (planner inventory, resolver answer); no
model is called and nothing is written. The incident ran on a Voiceover edit; the planning
step is the same for every format that plans clip intents, so the harness's montage manifest
(as in `test_m3_kri1111_repro.py`) stands in for it.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents import _persistence
from app.agents._runtime import ModelInvocation
from app.agents._skills import load_skill
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief, render_brief_request
from app.kria.planner import PlannedKriaTurn
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_planning import withdrawn_clip_asks, without_withdrawn_clip_asks
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_capabilities import resolve_creator_manifest

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("kri1464_restated_request.json").read_text(encoding="utf-8")
)
CABIN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
    ).read_text()
)
LATEST = CASE["latest_message"]
# Exactly as `_load_raw_creator_request` joins the thread's creator messages.
REQUEST = planner._full_creator_request(
    [SimpleNamespace(role="user", content=text, payload=None) for text in CASE["messages"]],
    current_message=LATEST,
)
RECORDED_REPLY = CASE["reply_2151"]
WITHDRAWN = {
    "include_satz": "the screen recording of typing in the Kria chat",
    "include_galerie": "the screen recording of the photo library",
    "include_meine_stimme": "the screen recording of the voice recorder",
}
KEPT = {
    "include_zug_1",
    "include_stadion_1",
    "include_lichtshow_1",
    "include_tor_1",
    "include_die_app_1",
}
VIDEOS = [m for m in CASE["media"] if m["kind"] == "video"]
PHOTOS = [m for m in CASE["media"] if m["kind"] == "image"]


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    # Hermetic: the replayed planner and resolver runs are never written to agent_run.
    monkeypatch.setattr(_persistence, "persist_agent_run", lambda **_kwargs: None)


def _brief(**status_by_id: str | None) -> CreativeBrief:
    """The recorded brief; ``r3="open"`` reopens a requirement, ``r3=None`` never recorded it."""
    data = copy.deepcopy(CASE["brief"])
    rows = []
    for req in data["requirements"]:
        if req["id"] not in status_by_id:
            rows.append(req)
            continue
        status = status_by_id[req["id"]]
        if status is None:
            continue
        facts = {k: v for k, v in req["facts"].items() if k != "superseded_by"}
        rows.append({**req, "status": status, "facts": facts})
    data["requirements"] = rows
    return CreativeBrief.model_validate(data)


def _intent_clips() -> list[IntentClip]:
    """The chat's four videos and two photos, in upload order (the resolver's m001..m006)."""
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": m["shows"]},
            gcs_path=f"users/00000000-0000-4000-8000-00000000c0de/{m['media_id']}",
            capture_time=(
                datetime.fromisoformat(m["capture_time"].replace("Z", "+00:00"))
                if m.get("capture_time")
                else None
            ),
            file_name=m["file_name"],
            duration_s=m.get("duration_s"),
        )
        for m in CASE["media"]
    ]


# -- the model transport, replayed ----------------------------------------------------------

_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)
_INTENT_LINE = re.compile(r'^- intent_id=(\S+) \| op=(\w+) \| attribute="([^"]*)"', re.M)


class _Provider:
    """The 21:51 inventory planner as recorded, and a resolver that finds the five real clips."""

    def __init__(self, inventory: list[dict[str, Any]] | None = None) -> None:
        self.inventory = inventory if inventory is not None else CASE["inventory_2151"]
        self.planner_calls = 0
        self.planner_request: str | None = None
        self.resolved: list[tuple[str, str, str]] = []  # (intent id, op, attribute) asked
        self.recorded = {row["intent_id"]: row for row in CASE["resolver_raw"]["intents"]}

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.planner_calls += 1
            self.planner_request = prompt
            return ModelInvocation(
                raw_text=json.dumps({"intents": self.inventory, "question": None})
            )
        if not prompt.startswith(_RESOLVER_PROMPT):
            raise AssertionError(f"unexpected model call: {prompt[:80]!r}")
        out = []
        for intent_id, op, attribute in _INTENT_LINE.findall(prompt):
            self.resolved.append((intent_id, op, attribute))
            out.append(self.recorded.get(intent_id) or {"intent_id": intent_id, "assignments": []})
        return ModelInvocation(raw_text=json.dumps({"intents": out}, ensure_ascii=False))


def _no_vision(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("no vision check ran on the recorded turn")


def _provider(
    monkeypatch: pytest.MonkeyPatch, inventory: list[dict[str, Any]] | None = None
) -> _Provider:
    provider = _Provider(inventory)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "query_clip_vision", _no_vision)
    return provider


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = LATEST
    data["creator_request"] = REQUEST
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": LATEST}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            *(
                {"media_id": m["media_id"], "kind": "video", "duration_s": m["duration_s"]}
                for m in VIDEOS
            ),
            *({"media_id": m["media_id"], "kind": "image"} for m in PHOTOS),
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn_2151(
    monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief | None
) -> PlannedKriaTurn:
    """The 21:51:04 tap's turn through the runtime-v2 planner, over the recorded brief."""
    agent_input = _agent_input()
    summary = "A montage that follows the voiceover: train, stadium, light show, goal, Kria card."
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 15,
                "rationale": summary,
            },
            "summary": summary,
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_earlier_hidden_parts", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_persist_turn_vision_answers", AsyncMock(return_value=None))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=LATEST,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(),
            creator_request=None,
            raw_creator_request=REQUEST,
        ),
        output=output,
        # Exactly as `plan_live_turn` derives them from the effective brief.
        brief_request=render_brief_request(brief, latest_message=LATEST) if brief else None,
        wants_capture_order=planner._brief_wants_capture_order(brief) if brief else False,
        effective_brief=brief,
    )


def _labels(planned: PlannedKriaTurn) -> list[str]:
    question = planned.plan.clip_question or {}
    return [row["label"] for row in question.get("categories") or []]


def _asked(provider: _Provider) -> set[str]:
    return {intent_id for intent_id, _op, _attribute in provider.resolved}


# -- F2: the replay is the incident ---------------------------------------------------------


@pytest.mark.asyncio
async def test_f2_f5_without_the_brief_the_replay_is_the_recorded_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: with no brief to say the asks were withdrawn, the turn is the 21:51:34
    reply byte for byte, from the creator's own joined messages."""
    provider = _provider(monkeypatch)

    planned = await _turn_2151(monkeypatch, None)

    assert provider.planner_calls == 1
    assert REQUEST in (provider.planner_request or "")
    assert CASE["messages"][0] in REQUEST and CASE["messages"][1] in REQUEST
    assert planned.plan.response == RECORDED_REPLY
    assert _labels(planned) == [row["label"] for row in CASE["clip_question_2151"]["categories"]]
    assert _labels(planned) == list(WITHDRAWN.values())


# -- F1 / F9: the withdrawn screen recordings -----------------------------------------------


@pytest.mark.asyncio
async def test_f1_f9_clips_the_creator_took_out_are_not_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    brief = _brief()
    withdrawn = {req.id for req in brief.requirements if not req.live}
    assert {"r3", "r8", "r9"} <= withdrawn
    assert all(
        brief_req.facts.get("superseded_by") == "creator"
        for brief_req in brief.requirements
        if brief_req.id in {"r3", "r8", "r9"}
    )

    planned = await _turn_2151(monkeypatch, brief)

    # F1: none of the three is asked, in the picker or in words.
    assert not set(_labels(planned)) & set(WITHDRAWN.values()), (
        f"withdrawn asks came back: {_labels(planned)!r}; reply: {planned.plan.response!r}"
    )
    for label in WITHDRAWN.values():
        assert label not in (planned.plan.response or "")
    # ... because they never reach the resolver.
    assert not _asked(provider) & set(WITHDRAWN)
    # F9: the five live asks are resolved as before, and none of them is asked about.
    assert KEPT <= _asked(provider)
    assert planned.plan.clip_question is None, planned.plan.response
    # The turn goes on to a draft instead of a question.
    assert planned.plan.mode == "act"


# -- F3 / F4 / F5: only the creator's own withdrawal, and only while nothing live names it ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_by_id",
    [
        {"r3": "open", "r8": "open", "r9": "open"},
        {"r3": None, "r8": None, "r9": None},
    ],
    ids=["asks-live-again", "never-recorded"],
)
async def test_f3_f5_a_live_or_unrecorded_ask_is_still_asked(
    monkeypatch: pytest.MonkeyPatch, status_by_id: dict[str, str | None]
) -> None:
    _provider(monkeypatch)

    planned = await _turn_2151(monkeypatch, _brief(**status_by_id))

    assert _labels(planned) == list(WITHDRAWN.values())
    assert planned.plan.response == RECORDED_REPLY


def _intents(*rows: dict[str, Any]) -> list[ClipIntent]:
    return [ClipIntent.model_validate(row) for row in rows]


def _ids(intents: list[ClipIntent]) -> list[str]:
    return [intent.intent_id for intent in intents]


def test_f3_a_clip_a_live_requirement_still_names_is_kept() -> None:
    """The creator withdrew "show the train on Zug" and later asked for the train again in
    other words: the live requirement wins."""
    data = copy.deepcopy(CASE["brief"])
    for req in data["requirements"]:
        if req["id"] == "r4":
            req["status"] = "superseded"
            req["facts"] = {**req["facts"], "superseded_by": "creator"}
    data["requirements"].append(
        {
            "id": "r13",
            "kind": "order",
            "scope": "global",
            "facts": {},
            "literal": None,
            "status": "open",
            "description": "End on the train.",
            "source_turn_id": None,
        }
    )
    asks = withdrawn_clip_asks(CreativeBrief.model_validate(data))
    intents = _intents(*CASE["inventory_2151"])

    kept = without_withdrawn_clip_asks(intents, asks)

    assert "include_zug_1" in _ids(kept)
    assert not set(_ids(kept)) & set(WITHDRAWN)


def test_f4_a_server_waiver_retires_nothing() -> None:
    """r2 / r11 were left out by the server ("Make it without these"), not by the creator."""
    data = copy.deepcopy(CASE["brief"])
    for req in data["requirements"]:
        if req["id"] in {"r3", "r8", "r9"}:
            req["facts"] = {**req["facts"], "superseded_by": "server"}
    asks = withdrawn_clip_asks(CreativeBrief.model_validate(data))
    intents = _intents(*CASE["inventory_2151"])

    assert _ids(without_withdrawn_clip_asks(intents, asks)) == _ids(intents)
    # An unmarked removal (stored before the mark existed) has unknown provenance: kept too.
    for req in data["requirements"]:
        req["facts"].pop("superseded_by", None)
    asks = withdrawn_clip_asks(CreativeBrief.model_validate(data))
    assert _ids(without_withdrawn_clip_asks(intents, asks)) == _ids(intents)


def test_f6_f7_a_leave_out_and_a_near_miss_are_kept() -> None:
    intents = _intents(
        # F6: a leave-out for the withdrawn clip stays (dropping it would put the clip back).
        {
            "intent_id": "exclude_galerie",
            "op": "exclude",
            "attribute": "the screen recording of the photo library",
        },
        # F7: more words name something the brief never withdrew.
        {
            "intent_id": "include_library_trip",
            "op": "include",
            "attribute": "the screen recording of the photo library on my old phone",
        },
        # F7: fewer words; every withdrawn ask names "the screen recording".
        {"intent_id": "include_recording", "op": "include", "attribute": "the screen recording"},
        # The withdrawn ask itself, articles and case aside.
        {
            "intent_id": "include_galerie",
            "op": "include",
            "attribute": "Screen recording of the Photo Library",
        },
    )

    # All three screen recordings withdrawn: no live ask names one, so the short form goes too.
    assert _ids(without_withdrawn_clip_asks(intents, withdrawn_clip_asks(_brief()))) == [
        "exclude_galerie",
        "include_library_trip",
    ]
    # One of them still live (r3): "the screen recording" may be that one, so it is kept.
    assert _ids(without_withdrawn_clip_asks(intents, withdrawn_clip_asks(_brief(r3="open")))) == [
        "exclude_galerie",
        "include_library_trip",
        "include_recording",
    ]


def test_f5_no_brief_withdraws_nothing() -> None:
    intents = _intents(*CASE["inventory_2151"])

    assert withdrawn_clip_asks(None) is None
    assert withdrawn_clip_asks(_brief(r3=None, r8=None, r9=None)) is None
    assert _ids(without_withdrawn_clip_asks(intents, None)) == _ids(intents)


# -- F8: an inventory reused for a picker tap -----------------------------------------------


@pytest.mark.asyncio
async def test_f8_a_reused_inventory_drops_the_withdrawn_rows_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-950: a pure picker tap reuses the asking turn's inventory instead of a new planner
    call. That list was checked before the creator restated the request, so it still carries
    the three rows; they are left out of it the same way."""
    provider = _provider(monkeypatch)
    brief = _brief()
    seen: dict[str, Any] = {}
    plan = planning.plan_and_resolve_clip_intents

    async def _spy(**kwargs: Any):  # noqa: ANN202
        first = await plan(**kwargs)
        seen["first"] = first
        # The next turn's picker tap: its stand-in sentence is the only new line, so the
        # saved list is reused (`_reused_inventory`).
        tap = "None of these for The screen recording of the photo library"
        seen["again"] = await plan(
            **{
                **kwargs,
                "creator_request": f"{kwargs['creator_request']}\n{tap}",
                "latest_user_message": tap,
                "checked_inventory": first.inventory,
            }
        )
        return first

    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", _spy)

    await _turn_2151(monkeypatch, brief)

    assert provider.planner_calls == 1, "the second call must reuse the saved inventory"
    for planned in (seen["first"], seen["again"]):
        assert not {intent.intent_id for intent in planned.requested_intents} & set(WITHDRAWN)
