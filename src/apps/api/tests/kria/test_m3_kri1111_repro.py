"""KRI-1111: asks the creator withdrew come back on the next turn's clip picker.

Leverkusen–Nürnberg M3 run 1 (thread 83f2dcaa "Mt3", 2026-10-31, Fly 7b5504165). The first
prompt asked "Start with my ticket, but blur the QR code" and "Label each goal clip with who
scored and in which minute". The creator withdrew both: 13:20 "Then leave the ticket out.",
13:22:58 "Leave the names and minutes out, just write "TOR!" on both goal clips.", 13:26:57
"Leave the ticket out completely." The 13:28:02 reply still asked which clip shows "my ticket"
(pre-ticking IMG_1028, a light-show video), and "I couldn't find any clips for "who scored";
I couldn't find any clips for "in which minute". Could you clarify?". The scorer sentence came
back twice more (13:30:42, 13:32:54) on turns that only answered the picker.

What the recorded brief and traces prove (`m3_kri1111_withdrawn.json` says what is recorded and
what is reconstructed):

* Brief v1..v8 all keep r1 ``facts.first_clip: "my ticket"``. r1 was ``change``d by the Main
  Creator on the "leave the ticket out" turn (v2: new ``source_turn_id``, every field identical)
  and again in v4 (``last_clip`` rewritten); a change that leaves ``first_clip`` out keeps it
  (``brief._carry_sticky_facts``, KRI-522), only an explicit null withdraws it (KRI-1068).
* v4 holds r10, a live ``select`` ask scoped to the ticket photo itself
  (``clip:asset-18868513…``, "Leave the ticket out completely"), and r5 (the scorer/minute ask)
  is ``superseded`` since v3.
* The 13:27:47 clip-intent planner wrote NO first pin for the ticket; it wrote an exclude for it
  (``exclude_ticket_r10``). The resolver's input still carried ``order-first-my-ticket``: the
  server's own row (``with_brief_anchors``, KRI-1067, from ``stated_anchors(brief)``), and its
  picker row pre-ticked the first filmed clip (``with_filming_end_ticks``).
* The planner (a model reading every creator message joined) re-emitted ``label_goal_scorer`` /
  ``label_goal_minute``; nothing between its output and the resolver checks the brief, so the
  superseded r5 reached the creator as ``_build_question``'s "I couldn't find any clips for".

Ways this can fail, written before any fix (each is pinned below):

 F1 a brief first/last clip the creator withdrew is put back by the server as a picker row the
    planner did not write (the incident's "my ticket" row).
 F2 that row pre-ticks a clip by filming order (IMG_1028) and the reply says "I ticked the first
    clip you filmed for "my ticket"".
 F3 the same media is offered in one card both as the clip to open on and as the clip to leave
    out, so any answer contradicts the other row.
 F4 an authored per-clip label whose only brief ask is superseded is still asked about ("I
    couldn't find any clips for "who scored"").
 F5 the fix over-reaches: the live exclusion of the ticket is no longer asked or applied (the
    creator's "leave the ticket out completely" must still win).
 F6 the replay is not the incident: the planner input or the resolver's answer differ from the
    recording, so a pass proves nothing.
 F7 the opening is only kept off the picker, while the brief still says "my ticket": the next
    draft's render contract refuses it ("I couldn't tell which clip "my ticket" means"), so
    the withdrawn ask comes back one turn later. The turn must take it off the brief (as a
    null, KRI-1068), keep the model's own change of that requirement (its new ending), and
    say so, whether the model left ``first_clip`` out of its change or repeated it.
 F8 over-reach on the opening: "start with X" with no leave-out of X (KRI-1067, KRI-522), a
    leave-out of something else ("the blurry clip") or of a near miss ("the ticket stub")
    loses the opening. Only a leave-out of the same thing (case, articles and "my" / "your"
    aside) beats it.
 F9 over-reach on labels: a live authored-label ask, or a brief that never recorded one (a
    Main Creator miss), loses the planner's labels; or the creator's own words ("TOR!") are
    dropped with them.

KRI-1067 / KRI-1068 behaviour stays as pinned in ``test_kri1067_filming_order_end_picker.py``
and ``test_kri1068_pin_withdrawal.py``.

Runs the real runtime-v2 planner path (`_plan_from_creator_output`): the real clip-intent
planning (`plan_and_resolve_clip_intents`, `with_brief_anchors`, `with_filming_end_ticks`), the
real resolver plumbing and question builder, and the real exclusion picker, over the recorded
brief v4. F7 applies the turn's brief withdrawal to the recorded brief v3 and the Main
Creator's 13:26:57 batch as v3 -> v4 records it. Only the model transport is replayed (planner
inventory, resolver answer, the one vision answer); no model is called and nothing is written.
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
from app.agents.clip_question import ClipQuestionOutput
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates, render_brief_request
from app.kria.planner import PlannedKriaTurn
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_order_sequence import stated_anchors
from app.services.creator_capabilities import resolve_creator_manifest

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("m3_kri1111_withdrawn.json").read_text(encoding="utf-8")
)
CABIN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
    ).read_text()
)
BY_FILE = {m["file_name"]: m["media_id"] for m in CASE["media"]}
BY_ALIAS = {m["alias"]: m["media_id"] for m in CASE["media"]}
TICKET = BY_FILE["M3_06_visual"]  # the ticket screenshot (a Visuals photo), alias m006
LIGHT_SHOW = BY_FILE["IMG_1028"]  # first filmed (19:36:41Z), the row's recorded pre-tick
VIDEOS = [m for m in CASE["media"] if m["kind"] == "video"]
REQUEST = CASE["creator_request"]
LATEST = CASE["latest_message"]
RECORDED_REPLY = CASE["reply_1328"]
MY_TICKET = "my ticket"
TICKED_MY_TICKET = 'I ticked the first clip you filmed for "my ticket".'
ENDING = "the Nürnberg players thanking their fans (IMG_1043)"  # r1 last_clip since v4
DROPPED = (
    'As you asked, I dropped "my ticket", so the video starts with the first clip you filmed. '
    "Tell me which clip to start with if you'd like a different one."
)
SUMMARY = "A 15-second montage of the match, from kick-off to the Nürnberg players at the end."


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # Prod at the time: clip intents, clip facts, the exclude op, the picker and the choice gate
    # on (the turn's picker carried an `exclude` row, so the exclude op was live).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    # Hermetic: the replayed planner and resolver runs are never written to agent_run.
    monkeypatch.setattr(_persistence, "persist_agent_run", lambda **_kwargs: None)


def _brief_v4() -> CreativeBrief:
    return CreativeBrief.model_validate(copy.deepcopy(CASE["brief_v4"]))


def _brief_v3() -> CreativeBrief:
    return CreativeBrief.model_validate(copy.deepcopy(CASE["brief_v3"]))


def _with_r5(status: str | None) -> CreativeBrief:
    """Brief v4 with the scorer/minute ask r5 open again, or (None) never recorded at all."""
    data = copy.deepcopy(CASE["brief_v4"])
    data["requirements"] = [
        {**req, "status": status} if req["id"] == "r5" else req
        for req in data["requirements"]
        if status is not None or req["id"] != "r5"
    ]
    return CreativeBrief.model_validate(data)


def _intent_clips() -> list[IntentClip]:
    """The chat's five videos and the ticket photo, in upload order (the resolver's m001..m006)."""
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": m["shows"]},
            gcs_path=f"users/00000000-0000-4000-8000-00000000c0de/{m['media_id']}",
            capture_time=datetime.fromisoformat(m["capture_time"].replace("Z", "+00:00")),
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
_CLIP_LINE = re.compile(r"^- (m\d{3}) \((video|image)\)", re.M)


class _Provider:
    """The 13:27:47 inventory planner and the 13:27:52 resolver, as recorded."""

    def __init__(self) -> None:
        self.planner_calls = 0
        self.planner_request: str | None = None
        self.resolved: list[tuple[str, str, str]] = []  # (intent id, op, attribute) asked
        self.recorded = {row["intent_id"]: row for row in CASE["resolver_raw"]["intents"]}

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.planner_calls += 1
            self.planner_request = prompt
            return ModelInvocation(
                raw_text=json.dumps(
                    {"intents": CASE["inventory_1327"], "question": None}, ensure_ascii=False
                )
            )
        if not prompt.startswith(_RESOLVER_PROMPT):
            raise AssertionError(f"unexpected model call: {prompt[:80]!r}")
        # The aliases are the upload order, as in prod (m006 = the ticket photo).
        kinds = dict(_CLIP_LINE.findall(prompt))
        assert kinds == {m["alias"]: m["kind"] for m in CASE["media"]}, kinds
        out = []
        for intent_id, op, attribute in _INTENT_LINE.findall(prompt):
            self.resolved.append((intent_id, op, attribute))
            row = self.recorded.get(intent_id) or {"intent_id": intent_id, "assignments": []}
            out.append(row)
        return ModelInvocation(raw_text=json.dumps({"intents": out}, ensure_ascii=False))


def _vision(clip: IntentClip, question: str, **_kwargs: Any) -> ClipQuestionOutput:
    """The one vision check the turn ran (13:28:02Z): "the two goal clips" -> no."""
    if question != CASE["vision"]["question"]:
        raise AssertionError(f"unexpected vision check: {question!r}")
    return ClipQuestionOutput.model_validate(CASE["vision"]["answer"])


def _provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    provider = _Provider()
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "query_clip_vision", _vision)
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
            {"media_id": TICKET, "kind": "image"},
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn_1328(
    monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief | None = None
) -> PlannedKriaTurn:
    """The 13:26:57 message's turn through the runtime-v2 planner, over brief v4."""
    brief = brief or _brief_v4()
    agent_input = _agent_input()
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 15,
                "rationale": SUMMARY,
            },
            "summary": SUMMARY,
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    # Nothing was tapped before this turn and no draft existed yet.
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_earlier_hidden_parts", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    # The vision answer is cached on the clip rows in prod; no database here.
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
        brief_request=render_brief_request(brief, latest_message=LATEST),
        wants_capture_order=planner._brief_wants_capture_order(brief),
        effective_brief=brief,
    )


def _rows(planned: PlannedKriaTurn) -> list[dict[str, Any]]:
    question = planned.plan.clip_question or {}
    return list(question.get("categories") or [])


# -- F6: the replay is the incident --------------------------------------------------------


@pytest.mark.asyncio
async def test_f5_f6_the_replay_reads_the_recorded_words_and_still_asks_to_leave_the_ticket_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)

    planned = await _turn_1328(monkeypatch)

    # F6: one planner call over the creator's own joined messages, as recorded. (On origin/main
    # 9c5a453b0 the replayed reply equals the recorded 13:28:02 reply byte for byte; the failing
    # tests below print it.)
    assert provider.planner_calls == 1
    assert REQUEST in (provider.planner_request or "")
    assert ("exclude_ticket_r10", "include", "the ticket") in provider.resolved
    # F5: the live "leave the ticket out completely" ask is never dropped with the opening: a
    # card that asks what to leave out still offers the ticket.
    excludes = [row for row in _rows(planned) if row["op"] == "exclude"]
    assert all(TICKET in row["candidate_media_ids"] for row in excludes), excludes


# -- F1 / F2 / F3: the withdrawn "start with my ticket" -------------------------------------


@pytest.mark.asyncio
async def test_f1_f2_f3_a_withdrawn_ticket_opening_is_not_put_back_on_the_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)

    planned = await _turn_1328(monkeypatch)

    # The planner wrote no first pin this turn: only the exclusion of the ticket.
    planned_firsts = [row for row in CASE["inventory_1327"] if row.get("position") == "first"]
    assert planned_firsts == []
    # F1: so the creator is never asked which clip the edit opens on with "my ticket".
    rows = _rows(planned)
    my_ticket_rows = [row for row in rows if row.get("label") == MY_TICKET]
    assert my_ticket_rows == [], (
        f"brief r1 first_clip {MY_TICKET!r} (withdrawn) came back as {my_ticket_rows!r}; "
        f"resolver was asked {provider.resolved!r}; reply: {planned.plan.response!r}"
    )
    # F2: and no clip is ticked for it by filming order.
    assert TICKED_MY_TICKET not in planned.plan.response
    assert all(LIGHT_SHOW not in (row.get("suggested_media_ids") or []) for row in rows)
    # F3: the ticket is never both "open on it" and "leave it out" in one card.
    ops_offering_ticket = {row["op"] for row in rows if TICKET in row["candidate_media_ids"]}
    assert ops_offering_ticket <= {"exclude"}, ops_offering_ticket


# -- F4: the withdrawn "who scored and in which minute" -------------------------------------


@pytest.mark.asyncio
async def test_f4_a_superseded_scorer_label_ask_is_never_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    r5 = next(req for req in _brief_v4().requirements if req.id == "r5")
    assert (r5.kind, r5.scope, r5.status) == ("text", "per_clip", "superseded")
    # No live brief ask is an authored per-clip label any more (only "TOR!" and the title).
    assert [
        req.id
        for req in _brief_v4().live()
        if req.kind == "text" and req.scope != "title" and not req.literal
    ] == []

    settled_by_name: list[tuple[str, str, str | None]] = []
    by_name = planning.apply_file_name_references

    def _spy(intents, **kwargs):  # noqa: ANN001, ANN003, ANN202
        settled = by_name(intents, **kwargs)
        settled_by_name.extend((r.intent_id, r.op, r.creator_text) for r in settled[0])
        return settled

    monkeypatch.setattr(planning, "apply_file_name_references", _spy)
    planned = await _turn_1328(monkeypatch)

    response = planned.plan.response
    assert "who scored" not in response and "in which minute" not in response, (
        f"superseded r5 asked again: {response!r}"
    )
    # F9: the creator's own words for those clips are still placed. Since KRI-1112 the
    # earlier "The goals are IMG_1037 and IMG_1039." settles "the two goal clips" by file
    # name, so the caption is settled there instead of by the resolver.
    placed = {(i, op) for i, op, _ in settled_by_name} | {(i, op) for i, op, _ in provider.resolved}
    assert ("caption_goal_tor", "caption") in placed
    assert not {i for i, op in placed if op == "label"}, placed


@pytest.mark.asyncio
@pytest.mark.parametrize("r5_status", ["open", None], ids=["r5-live", "never-recorded"])
async def test_f9_a_live_or_unrecorded_label_ask_is_still_asked(
    monkeypatch: pytest.MonkeyPatch, r5_status: str | None
) -> None:
    """Guard: only a brief whose authored-label asks were ALL withdrawn drops them. A live one,
    or none ever recorded (the Main Creator missed it), leaves the planner's labels as today."""
    _provider(monkeypatch)

    planned = await _turn_1328(monkeypatch, _with_r5(r5_status))

    assert "who scored" in planned.plan.response and "in which minute" in planned.plan.response


# -- F7: the brief stops holding the opening -------------------------------------------------


def _batch_1326(*, model_kept_first: bool) -> list[BriefUpdate]:
    """The Main Creator's 13:26:57 brief updates, as v3 -> v4 records them.

    Its output is redacted at write time; these three updates reproduce v4 exactly whether
    its r1 change left ``first_clip`` out (carried back, KRI-522) or repeated it."""
    first = {"first_clip": MY_TICKET} if model_kept_first else {}
    return [
        BriefUpdate(
            operation="change",
            target_requirement_id="r1",
            expected_version=3,
            kind="order",
            scope="global",
            description="chronological from kick-off to the end",
            facts={"key": "capture_time", "last_clip": ENDING, **first},
        ),
        BriefUpdate(
            operation="change",
            target_requirement_id="r8",
            expected_version=3,
            kind="text",
            scope="global",
            literal="TOR!",
            description="only on the two goal clips",
        ),
        BriefUpdate(
            kind="select",
            scope=f"clip:{TICKET}",
            description="Leave the ticket out completely",
        ),
    ]


def _rows_of(brief: CreativeBrief) -> list[tuple[Any, ...]]:
    return [
        (r.id, r.kind, r.scope, r.status, r.literal, r.description, r.facts)
        for r in brief.requirements
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_kept_first", [False, True], ids=["omitted", "repeated"])
async def test_f7_the_turn_takes_the_opening_off_the_brief_and_says_so(
    monkeypatch: pytest.MonkeyPatch, model_kept_first: bool
) -> None:
    _provider(monkeypatch)
    batch = _batch_1326(model_kept_first=model_kept_first)
    turn = CASE["brief_v4"]["requirements"][0]["source_turn_id"]
    # The reconstruction is the recording: v3 + this batch is v4, row for row.
    assert _rows_of(apply_updates(_brief_v3(), batch, source_turn_id=turn)) == _rows_of(_brief_v4())

    planned = await _turn_1328(monkeypatch)

    # What the turn's planning found: the creator left out the opening the brief names.
    assert planner.turn_excluded_anchors.get() == {"first": MY_TICKET}
    # `_plan_live_turn`'s last step, over the brief the turn started from and its updates.
    planned, updates, notices = planner._without_excluded_anchors(
        planned, _brief_v3(), tuple(batch), ()
    )
    after = apply_updates(_brief_v3(), updates, source_turn_id=turn)
    r1 = next(r for r in after.live() if r.id == "r1")
    # Only the opening goes: filming order and the model's new ending stay, no null is stored.
    assert r1.facts == {"key": "capture_time", "last_clip": ENDING}
    # What the render contract reads: nothing left to refuse on "my ticket".
    assert stated_anchors(after) == {"last": ENDING}
    # The rest of the batch is the model's, untouched (r8 moved, r10 added).
    assert _rows_of(after)[1:] == _rows_of(_brief_v4())[1:]
    # Said on the draft (`pin_notices`) and, as this turn asks first, after the question.
    assert notices == (DROPPED,)
    assert planned.plan.response == (
        'I couldn\'t tell which of your clips to leave out for "the ticket". Tap the ones to '
        f"leave out, or tell me none. {DROPPED}"
    )


def test_f7_an_ending_the_model_withdrew_in_the_same_change_stays_withdrawn() -> None:
    """The model's own change nulls the ending while the opening is left out: neither is
    carried back from v3 by the server's replacement of that change."""
    from app.services.anchor_exclusions import withdraw_excluded_anchors  # noqa: PLC0415

    batch = _batch_1326(model_kept_first=True)
    batch[0] = batch[0].model_copy(update={"facts": {"key": "capture_time", "last_clip": None}})

    withdrawal = withdraw_excluded_anchors(_brief_v3(), batch, {"first": MY_TICKET})

    after = apply_updates(_brief_v3(), withdrawal.updates, source_turn_id="t")
    assert next(r for r in after.live() if r.id == "r1").facts == {"key": "capture_time"}
    assert stated_anchors(after) == {}


def _inventory(leave_out: str | None) -> list[ClipIntent]:
    """The 13:27:47 inventory, its ticket exclusion replaced by ``leave_out`` (None: none)."""
    rows = [r for r in CASE["inventory_1327"] if r["op"] != "exclude"]
    if leave_out is not None:
        rows.append({"intent_id": "exclude_x", "op": "exclude", "attribute": leave_out})
    return [
        ClipIntent.model_validate({k: v for k, v in r.items() if k != "source_quote"}) for r in rows
    ]


@pytest.mark.parametrize(
    ("leave_out", "kept"),
    [
        (None, True),
        ("the blurry clip", True),
        ("the ticket stub", True),
        ("the ticket", False),
        ("My Ticket", False),
        ("your ticket", False),
    ],
)
def test_f8_only_leaving_out_the_same_thing_beats_the_opening(
    leave_out: str | None, kept: bool
) -> None:
    anchors = stated_anchors(_brief_v4())
    assert anchors == {"first": MY_TICKET, "last": ENDING}

    intents = planning.with_brief_anchors(_inventory(leave_out), anchors)

    firsts = [i.attribute for i in intents if i.op == "order" and i.position == "first"]
    assert firsts == ([MY_TICKET] if kept else [])


# -- F10: asking for the clip again brings it back ------------------------------------------


def _brief_after_withdrawal() -> CreativeBrief:
    """Brief v4 once the 13:26:57 turn took the left-out opening off it (F7)."""
    from app.services.anchor_exclusions import withdraw_excluded_anchors  # noqa: PLC0415

    batch = _batch_1326(model_kept_first=True)
    withdrawal = withdraw_excluded_anchors(_brief_v3(), batch, {"first": MY_TICKET})
    return apply_updates(_brief_v3(), withdrawal.updates, source_turn_id="t")


def test_f10_a_left_out_clip_asked_for_again_comes_back_as_an_ordinary_clip() -> None:
    """Asked again ("Put the ticket back in") after "Leave the ticket out": nothing on the
    brief still holds the ticket out, so the planner's inventory (no leave-out this turn)
    stands as it is: the ticket is an ordinary clip again, and it does not open the video
    unasked."""
    after = _brief_after_withdrawal()
    anchors = stated_anchors(after)
    assert anchors == {"last": ENDING}

    inventory = _inventory(None)
    assert planning.excluded_anchors(inventory, anchors) == {}

    intents = planning.with_brief_anchors(inventory, anchors)

    assert not [i for i in intents if i.op == "exclude"]
    assert [i.attribute for i in intents if i.op == "order" and i.position == "first"] == []


def test_f10_asking_to_start_with_it_again_puts_the_opening_back() -> None:
    """Asked to start with it again ("Start with my ticket"): the brief takes the
    opening again and the turn's planning seats it first, as before the leave-out."""
    from app.services.anchor_exclusions import withdraw_excluded_anchors  # noqa: PLC0415

    prior = _brief_after_withdrawal()
    again = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=prior.version,
        kind="order",
        scope="global",
        description="chronological from kick-off to the end, starting with my ticket",
        facts={"key": "capture_time", "first_clip": MY_TICKET, "last_clip": ENDING},
    )
    inventory = _inventory(None)
    anchors_now = {"first": MY_TICKET, "last": ENDING}

    withdrawal = withdraw_excluded_anchors(
        prior, [again], planning.excluded_anchors(inventory, anchors_now)
    )

    assert withdrawal.updates == (again,) and withdrawal.notices == ()
    after = apply_updates(prior, withdrawal.updates, source_turn_id="t2")
    assert stated_anchors(after) == anchors_now
    intents = planning.with_brief_anchors(inventory, stated_anchors(after))
    assert [i.attribute for i in intents if i.op == "order" and i.position == "first"] == [
        MY_TICKET
    ]
