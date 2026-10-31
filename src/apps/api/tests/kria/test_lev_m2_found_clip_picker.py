"""Leverkusen M2: the clip picker hid the clip it had found and pre-ticked the wrong ones.

Prod thread d048973b (chat "Taha 1", 2026-10-30, Fly eed794157). Turn 1a0a5b16 asked "which
of your clips show the "TOR" on the big screen" and offered the other goal clip and both
photos, pre-ticking the other goal clip and the 0:0 scoreboard. Vision had found the TOR in
IMG_1039 (the stats say ``order_tor_screen_third:n=0:nv=3`` with 2 vision calls), so the clip
was "already confirmed" and left out of its own row; the row was asked only because the
checks on IMG_1037 and the photo were unsure. A tap replaces the resolver's answer, so "Done"
would have moved the TOR beat onto the wrong clips. The same turn's skip row ("the kids at
the start of the second goal clip") offered only the scoreboard photo: IMG_1039 was hidden as
a pinned clip. Turn befdbf7e asked the same card again, and its skip row pre-ticked BOTH goal
clips, so "Done" would have removed every Taha moment, the TOR and the scream.

Ways this could go wrong, written down before the code (each has a case below):

a. The clip the resolver or vision already found is missing from its own picker row.
b. A tap replaces the resolver's answer, so a row without the found clip loses it on "Done".
c. Unsure checks next to a confident match are pre-ticked, so "Done" adds the wrong clips.
d. An ask with a confident match is held open only by unsure checks: an extra question
   whose answer is what was already found, and still no video.
e. The text says "I couldn't tell which of your clips show X" although X was found.
f. Settling on the confident match cuts footage: an "only about X" scope (exclusive
   include) would drop its unsure clips from the edit, so it still asks, keeping them ticked.
g. Settling hides a real gap: no match at all, a resolver question, or a technical failure
   (pending, provider, budget, missing media) must still ask or report as before.
h. A per-clip authored label (not answered by tapping) loses its text question.
i. A skip row's pre-ticks take every clip another live ask stands on (both Taha goal clips;
   the TOR's only clip), also when that ask is itself still open.
j. A skip row hides the clip it is about, so the creator cannot answer it (only a photo was
   offered for "the kids at the start of the second goal clip").
k. Offering a pinned clip lets "Done" drop it.
l. A filming-time order or a group counts as standing on every clip it shares with the skip,
   so no skip is ever pre-ticked (KRI-807's suspects lost), or a skip can no longer take part
   of a "start with" group (the open PR #1972's seat photo).

Decided after writing these, for j and k: a ONE-clip pin stays hidden from a skip row
(KRI-761: tapping it would undo the creator's own placement). The M2 goal clips were hidden
because "then the moments where Taha is celebrating in both goal clips" (last, two clips) was
read as a pin; PR #1972 makes such an ask a group, so its clips are offered. This change only
makes sure an offered clip is never pre-ticked when that would leave another ask with none.
m. A photo found for the ask is narrowed out of the row in a one-video project (KRI-779).
n. A Turkish chat gets English copy.
o. An ask that hides footage settles on its confident clip while an unsure one may still show
   what must not be seen (the privacy-trim session adds such an ask). Settling, and dropping
   the unsure pre-ticks, is an allowlist of asks that place, add or print
   (``_FOUND_SETTLES_OPS``), so a new kind of ask keeps today's question.

The picker cap (``MAX_QUESTION_CLIPS``) equals the most media a project can hold, so it
never cuts a found clip from a row.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionOutput
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipAssignment, ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import (
    ANSWERS_KEY,
    IntentClip,
    normalize_question,
    resolve_clip_intents_for_turn,
)
from app.services.creator_capabilities import resolve_creator_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "lev_m2_picker_turns.json").read_text())
# The KRI-623 cabin turn supplies the rest of a real Main Creator input (conversation shape,
# language, copy state); only its clips and words are swapped for the M2 ones.
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())
MEDIA = {m["file_name"]: m for m in CASE["media"]}
ALL = [m["media_id"] for m in CASE["media"]]  # attachment order
GOAL_1 = MEDIA["IMG_1037"]["media_id"]  # first goal; Taha 5.0-9.8 s
GOAL_2 = MEDIA["IMG_1039"]["media_id"]  # kids 0-2.5 s, TOR 6.0-7.5 s, Taha's scream 12-13.8 s
SELFIE = MEDIA["selfie"]["media_id"]
SCOREBOARD = MEDIA["scoreboard"]["media_id"]
ALIAS = {media_id: f"m{i:03d}" for i, media_id in enumerate(ALL, 1)}
TOR = 'the "TOR" on the big screen'
KIDS = "the kids at the start of the second goal clip"
COULD_NOT_TELL_TOR = 'show "the "TOR"'


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _quote(text: str) -> str:
    assert text in CASE["creator_request"], text
    return text


def _planned_intents(turn: str) -> list[dict[str, Any]]:
    """The clip-intent planner's six asks, as its ids in each turn name them.

    Turn 2 pinned the selfie (first, and a label in the creator's words) and both goal clips
    (Taha last): its skip row offered only the scoreboard. Turn 4 pinned nothing: its skip
    row offered all four. The ids are the recorded ones; the rest is the creator's words.
    """
    second = turn == "1a0a5b16"
    sfx = {"scoreboard": "_second", "tor": "_third", "taha": "_fourth"} if second else {}
    selfie_label: dict[str, Any] = {
        "intent_id": "label_selfie",
        "op": "label",
        "attribute": "our selfie",
        "source_quote": _quote('Label the selfie "Taha 🤝 me"'),
    }
    if second:
        selfie_label["creator_text"] = "Taha 🤝 me"
    return [
        {
            "intent_id": "order_selfie_first",
            "op": "order",
            **({"position": "first"} if second else {}),
            "attribute": "our selfie",
            "source_quote": _quote("Order: our selfie"),
        },
        {
            "intent_id": f"order_scoreboard{sfx.get('scoreboard', '')}",
            "op": "order",
            "attribute": "the 0:0 scoreboard",
            "source_quote": _quote("the 0:0 scoreboard"),
        },
        {
            "intent_id": f"order_tor_screen{sfx.get('tor', '')}",
            "op": "order",
            "attribute": TOR,
            "source_quote": _quote(TOR),
        },
        {
            "intent_id": f"order_taha_celebrating{sfx.get('taha', '')}",
            "op": "order",
            **({"position": "last"} if second else {}),
            "attribute": "the moments where Taha is celebrating in both goal clips",
            "source_quote": _quote(
                "then only the moments where Taha is celebrating in both goal clips"
            ),
        },
        {
            "intent_id": "exclude_kids",
            "op": "exclude",
            "attribute": KIDS,
            "source_quote": _quote(
                "The kids at the start of the second goal clip are another family's children"
            ),
        },
        selfie_label,
    ]


def _resolved(turn: str) -> dict[str, dict[str, Any]]:
    """What the resolver answered, read off the turn's ``intent_stats`` (n, min, nv)."""
    second = turn == "1a0a5b16"
    by_role = {i["intent_id"]: i for i in _planned_intents(turn)}
    ids = list(by_role)
    taha = 0.8 if second else 0.9

    def assign(media_id: str, confidence: float, value: str | None = None) -> dict[str, Any]:
        out = {"media": ALIAS[media_id], "confidence": confidence, "evidence": "record"}
        return {**out, "value": value} if value else out

    def vision(*media_ids: str) -> list[dict[str, str]]:
        return [{"media": ALIAS[m], "question": "Is this it?"} for m in media_ids]

    kids_suspects = (GOAL_2,) if second else (GOAL_1, GOAL_2)
    return {
        ids[0]: {"assignments": [assign(SELFIE, 1.0)], "needs_vision": []},
        ids[1]: {"assignments": [assign(SCOREBOARD, 1.0)], "needs_vision": []},
        ids[2]: {"assignments": [], "needs_vision": vision(GOAL_1, GOAL_2, SCOREBOARD)},
        ids[3]: {"assignments": [assign(GOAL_1, taha), assign(GOAL_2, taha)], "needs_vision": []},
        ids[4]: {"assignments": [], "needs_vision": vision(*kids_suspects)},
        ids[5]: {"assignments": [assign(SELFIE, 1.0, "Taha me")], "needs_vision": []},
    }


_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


class _Provider:
    """The model provider, answering as it did on the recorded turn.

    Only the transport is a stand-in: the real clip-intent planner and resolver agents
    render their prompts, call it, and parse its raw text.
    """

    def __init__(self, turn: str, intents: list[dict[str, Any]] | None = None) -> None:
        self.turn = turn
        self.intents = intents
        self.calls: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            intents = self.intents or _planned_intents(self.turn)
            return ModelInvocation(raw_text=json.dumps({"intents": intents, "question": None}))
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            # The prompt lists the clips by alias in attachment order.
            assert re.findall(r"^- (m\d{3}) \(", prompt, re.M) == list(ALIAS.values())
            answers = _resolved(self.turn)
            return ModelInvocation(
                raw_text=json.dumps(
                    {"intents": [{"intent_id": k, **v} for k, v in answers.items()]}
                )
            )
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


# Vision on the TOR row: IMG_1039 shows it; the IMG_1037 check came back unsure. Photos are
# never sent to vision (KRI-291), so the scoreboard is an unsure "unknown" too.
VISION = {GOAL_2: ClipQuestionOutput(answer="yes", confidence=0.9, evidence="TOR on the screen")}


def _vision(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def _query(clip: IntentClip, question: str, **_kwargs: Any) -> ClipQuestionOutput:
        asked.append(clip.media_id)
        return VISION.get(clip.media_id, ClipQuestionOutput())

    monkeypatch.setattr(resolution, "query_clip_vision", _query)
    return asked


def _cached_answers() -> dict[str, dict[str, Any]]:
    """Turn 4's clips carry turn 2's TOR answers (``vision_cached=2``)."""
    key = normalize_question(resolution._membership_question(TOR))
    return {
        media_id: {key: VISION.get(media_id, ClipQuestionOutput()).model_dump()}
        for media_id in (GOAL_1, GOAL_2)
    }


def _intent_clips(turn: str = "1a0a5b16") -> list[IntentClip]:
    cached = _cached_answers() if turn == "befdbf7e" else {}
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={
                "understanding": m["record"],
                **({ANSWERS_KEY: cached[m["media_id"]]} if m["media_id"] in cached else {}),
            },
            gcs_path=f"users/u/creation-threads/t/{m['media_id']}",
            file_name=m["file_name"],
            duration_s=m["duration_s"],
        )
        for m in CASE["media"]
    ]


def _provider(monkeypatch: pytest.MonkeyPatch, turn: str, **kwargs: Any) -> _Provider:
    provider = _Provider(turn, **kwargs)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


def _agent_input(message: str) -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = message
    data["creator_request"] = message
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": m["media_id"], "kind": m["kind"], "duration_s": m["duration_s"]}
            for m in CASE["media"]
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(monkeypatch: pytest.MonkeyPatch, turn: str) -> Any:
    """One recorded planning turn, through the real resolution and the runtime-v2 planner."""
    message = f"{CASE['creator_request']}\n{CASE['turns'][turn]['message']}"
    agent_input = _agent_input(message)
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 12,
                "rationale": "A quick reaction edit about Taha.",
            },
            "summary": "Here's a 12-second reaction edit about Taha.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    # The vision answers are cached on the plan item; the database is a stand-in.
    monkeypatch.setattr(planner, "persist_clip_intent_vision_answers", AsyncMock())
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace()), commit=AsyncMock(), rollback=AsyncMock()
    )
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        db,
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=message,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(turn),
            creator_request=None,
            raw_creator_request=message,
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _rows(planned: Any) -> dict[str, dict[str, Any]]:
    question = planned.plan.clip_question or {"categories": []}
    return {c["op"]: c for c in question["categories"]}


# -- the two recorded turns, through the planner (a, b, c, d, e, i, j, k) -------------------


@pytest.mark.parametrize("turn", ["1a0a5b16", "befdbf7e"])
async def test_the_replay_is_the_prod_turn(prod_profile, monkeypatch, turn: str) -> None:
    # The scripted resolver and vision give back exactly the recorded counts.
    provider = _provider(monkeypatch, turn)
    asked = _vision(monkeypatch)
    planned = await _turn(monkeypatch, turn)
    assert provider.calls == ["planner", "resolver"]
    diagnostics = planned.plan.diagnostics
    recorded = CASE["turns"][turn]
    if "intent_stats" in diagnostics:
        assert diagnostics["intent_stats"] == recorded["intent_stats"]
    assert len(asked) == recorded["vision_calls"]


@pytest.mark.parametrize("turn", ["1a0a5b16", "befdbf7e"])
async def test_the_tor_found_by_vision_is_never_asked_again(
    prod_profile, monkeypatch, turn: str
) -> None:
    # d, e: IMG_1039 shows the TOR; two unsure checks beside it hold nothing open.
    _provider(monkeypatch, turn)
    _vision(monkeypatch)
    planned = await _turn(monkeypatch, turn)
    assert "order" not in _rows(planned)
    assert COULD_NOT_TELL_TOR not in planned.plan.response
    assert "TOR" not in planned.plan.response


@pytest.mark.parametrize("turn", ["1a0a5b16", "befdbf7e"])
async def test_the_kids_row_pre_ticks_no_goal_clip(prod_profile, monkeypatch, turn: str) -> None:
    # i: the goal clips carry the TOR and Taha: never pre-ticked to leave out. Turn 4 pinned
    # nothing, so every clip is offered (turn 2's pins: see j and k above).
    _provider(monkeypatch, turn)
    _vision(monkeypatch)
    planned = await _turn(monkeypatch, turn)
    kids = _rows(planned)["exclude"]
    assert kids["label"] == KIDS
    assert kids["suggested_media_ids"] == []
    if turn == "befdbf7e":
        assert kids["candidate_media_ids"] == ALL
    assert "leave out" in planned.plan.response


async def test_tapping_done_on_the_kids_row_keeps_both_goal_clips(
    prod_profile, monkeypatch
) -> None:
    # i: the answer the pre-ticks would have sent removes nothing a live ask needs.
    _provider(monkeypatch, "befdbf7e")
    _vision(monkeypatch)
    planned = await _turn(monkeypatch, "befdbf7e")
    kids = _rows(planned)["exclude"]
    assert not {GOAL_1, GOAL_2} & set(kids["suggested_media_ids"])


# -- the resolver alone (a, b, c, f, g, h, m) -----------------------------------------------


def _intent(intent_id: str, op: str, attribute: str, **extra: Any) -> ClipIntent:
    return ClipIntent(intent_id=intent_id, op=op, attribute=attribute, **extra)


class _ScriptedResolver:
    """Answers each intent with the given assignments / vision checks, by media id."""

    def __init__(self, answers: dict[str, dict[str, Any]], clips: list[IntentClip]) -> None:
        self.answers = answers
        self.alias = {c.media_id: f"m{i:03d}" for i, c in enumerate(clips, 1)}

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        assert prompt.startswith(_RESOLVER_PROMPT)
        intents = []
        for intent_id, answer in self.answers.items():
            intents.append(
                {
                    "intent_id": intent_id,
                    "assignments": [
                        {"media": self.alias[m], "confidence": c, "evidence": "record", **v}
                        for m, c, v in answer.get("assignments", [])
                    ],
                    "needs_vision": [
                        {"media": self.alias[m], "question": "Is this it?"}
                        for m in answer.get("needs_vision", [])
                    ],
                    **({"question": answer["question"]} if answer.get("question") else {}),
                }
            )
        return ModelInvocation(raw_text=json.dumps({"intents": intents}))


async def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    intents: list[ClipIntent],
    answers: dict[str, dict[str, Any]],
    *,
    clips: list[IntentClip] | None = None,
    vision: dict[str, ClipQuestionOutput] | None = None,
    max_vision_requeries: int = 18,
) -> Any:
    clips = clips or _intent_clips()
    resolver = _ScriptedResolver(answers, clips)
    monkeypatch.setattr(resolution, "default_client", lambda: resolver)
    table = VISION if vision is None else vision

    def _query(clip: IntentClip, question: str, **_kwargs: Any) -> ClipQuestionOutput:
        return table.get(clip.media_id, ClipQuestionOutput())

    monkeypatch.setattr(resolution, "query_clip_vision", _query)
    return await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=CASE["creator_request"],
        clips=clips,
        run_context=RunContext(),
        max_vision_requeries=max_vision_requeries,
    )


TOR_ASK = {"needs_vision": [GOAL_1, GOAL_2, SCOREBOARD]}


async def test_the_tor_settles_on_the_clip_vision_confirmed(monkeypatch) -> None:
    # d: the turn-2 TOR row alone. Resolved on IMG_1039; IMG_1037 and the photo stay out.
    result = await _resolve(monkeypatch, [_intent("tor", "order", TOR)], {"tor": TOR_ASK})
    assert result.status == "resolved" and result.question is None
    assert result.clip_question is None
    [tor] = result.intents
    assert (tor.status, tor.media_ids()) == ("resolved", [GOAL_2])


async def test_an_only_about_scope_still_asks_with_the_found_clip_ticked(monkeypatch) -> None:
    # a, b, e, f: "only about the TOR" would cut the unsure clips from the edit, so it asks;
    # the found clip is in the row and ticked, the unsure ones stay ticked (kept), and the
    # words say what was found.
    intent = _intent("tor", "include", TOR, exclusive=True)
    result = await _resolve(monkeypatch, [intent], {"tor": TOR_ASK})
    assert result.status == "needs_creator"
    [row] = result.clip_question["categories"]
    assert row["candidate_media_ids"] == ALL
    assert row["suggested_media_ids"] == [GOAL_1, GOAL_2, SCOREBOARD]
    assert "couldn't tell" not in result.question
    assert "found" in result.question and TOR in result.question


def test_a_row_asked_with_a_found_clip_ticks_it_and_leaves_the_unsure_ones_unticked() -> None:
    # a, b, c: whatever still holds a row open, its found clip is in it and ticked (a tap
    # replaces the answer), and an unsure check beside it is offered, never pre-ticked.
    work = resolution._IntentWork(
        intent=_intent("tor", "order", TOR),
        kept=[ClipAssignment(media_id=GOAL_2, confidence=0.9, evidence="TOR")],
        failed_media_ids={GOAL_1, SCOREBOARD},
        considered_media_ids={GOAL_1, GOAL_2, SCOREBOARD},
    )
    question = resolution._build_clip_question([work], _intent_clips())
    [row] = question["categories"]
    assert row["candidate_media_ids"] == ALL
    assert row["suggested_media_ids"] == [GOAL_2]


async def test_with_nothing_found_the_unsure_clips_are_still_pre_ticked(monkeypatch) -> None:
    # g (KRI-991 kept): vision found nothing, so the unsure clips are the picker's pre-ticks.
    result = await _resolve(
        monkeypatch, [_intent("tor", "order", TOR)], {"tor": TOR_ASK}, vision={}
    )
    assert result.status == "needs_creator"
    [row] = result.clip_question["categories"]
    assert row["candidate_media_ids"] == ALL
    assert row["suggested_media_ids"] == [GOAL_1, GOAL_2, SCOREBOARD]
    assert "couldn't tell which of your clips show" in result.question


async def test_a_found_clip_never_hides_a_pending_check(monkeypatch) -> None:
    # g: the IMG_1037 check is over the vision cap: pending, never settled or asked.
    result = await _resolve(
        monkeypatch,
        [_intent("tor", "order", TOR)],
        {"tor": {"needs_vision": [GOAL_2, GOAL_1]}},
        max_vision_requeries=1,
    )
    assert result.status == "pending"
    assert result.clip_question is None


async def test_a_resolver_question_still_asks(monkeypatch) -> None:
    # g: the resolver could not read the ask at all.
    result = await _resolve(
        monkeypatch,
        [_intent("tor", "order", TOR)],
        {"tor": {"question": "Which big screen do you mean?"}},
    )
    assert result.status == "needs_creator"
    assert "big screen" in result.question


async def test_an_authored_label_keeps_its_text_question(monkeypatch) -> None:
    # h: "label each clip with what it shows": a per-clip text a tap cannot answer.
    intent = _intent("what", "label", "what each goal clip shows")
    result = await _resolve(
        monkeypatch,
        [intent],
        {"what": {"needs_vision": [GOAL_1, GOAL_2]}},
        vision={GOAL_2: ClipQuestionOutput(answer="Goal", confidence=0.9, evidence="goal")},
    )
    assert result.status == "needs_creator"
    assert result.clip_question is None
    assert result.question


async def test_a_found_photo_is_offered_in_a_one_video_project(monkeypatch) -> None:
    # m: one video + the scoreboard photo the resolver found at the bar; the row is asked
    # because the only-about scope keeps unsure clips, and the photo is in it, ticked.
    clips = [c for c in _intent_clips() if c.media_id in {GOAL_2, SCOREBOARD}]
    intent = _intent("score", "include", "the 0:0 scoreboard", exclusive=True)
    result = await _resolve(
        monkeypatch,
        [intent],
        {"score": {"assignments": [(SCOREBOARD, 0.9, {})], "needs_vision": [GOAL_2]}},
        clips=clips,
        vision={},
    )
    [row] = result.clip_question["categories"]
    assert SCOREBOARD in row["candidate_media_ids"]
    assert SCOREBOARD in row["suggested_media_ids"]


async def test_the_found_row_is_turkish_in_a_turkish_chat(monkeypatch) -> None:
    # n
    token = bind_reply_language("tr")
    try:
        intent = _intent("tor", "include", TOR, exclusive=True)
        result = await _resolve(monkeypatch, [intent], {"tor": TOR_ASK})
    finally:
        release_reply_language(token)
    assert "found" not in result.question and "anlayamadım" not in result.question
    assert TOR in result.question


# -- the skip row on its own (i, k, l) --------------------------------------------------


def _montage() -> Any:
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415

    return CreativeStrategy(edit_format="montage", media_scope="all", rationale="M2")


def _resolved_intent(intent_id: str, op: str, *media_ids: str, **extra: Any) -> Any:
    from app.schemas.clip_intents import ResolvedClipIntent  # noqa: PLC0415

    return ResolvedClipIntent(
        intent_id=intent_id,
        op=op,
        attribute=intent_id,
        assignments=[ClipAssignment(media_id=m, confidence=0.9) for m in media_ids],
        **extra,
    )


def _kids_row(*others: Any) -> dict[str, Any]:
    from app.kria.clip_exclusions import exclusion_question, exclusions_to_ask  # noqa: PLC0415

    asked = exclusions_to_ask(
        [*others, _resolved_intent("exclude_kids", "exclude")],
        _montage(),
        suspects={"exclude_kids": [GOAL_1, GOAL_2]},
        clip_count=len(ALL),
    )
    _text, question = exclusion_question(asked, _intent_clips())
    [row] = question["categories"]
    return row


def test_a_clip_an_open_ask_found_is_never_pre_ticked_to_leave_out() -> None:
    # i: the TOR row is still open (needs_creator) and IMG_1039 is the only clip it found.
    tor = _resolved_intent("tor", "order", GOAL_2, status="needs_creator")
    row = _kids_row(tor)
    assert row["suggested_media_ids"] == [GOAL_1]
    assert row["candidate_media_ids"] == ALL


def test_a_skip_never_pre_ticks_every_clip_of_another_ask() -> None:
    # i: turn 4: "the moments where Taha is celebrating in both goal clips" (no position).
    row = _kids_row(_resolved_intent("taha", "order", GOAL_1, GOAL_2))
    assert row["candidate_media_ids"] == ALL
    assert row["suggested_media_ids"] == []


def test_a_skip_may_still_take_part_of_a_group_or_a_filming_order() -> None:
    # l: both asks keep a clip after "Done", so the suspects stay pre-ticked.
    everything = _resolved_intent("filmed", "order", *ALL, order_by="capture_time")
    group = _resolved_intent("stadium", "group", GOAL_1, GOAL_2, SCOREBOARD)
    assert _kids_row(everything, group)["suggested_media_ids"] == [GOAL_1, GOAL_2]
