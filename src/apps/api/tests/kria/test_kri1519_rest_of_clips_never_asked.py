"""KRI-1519: "Fill the rest with the other clips" is a fill instruction, never a clip question.

Cusco DJI T4 "Altının yolu" (KRI-1517, thread 04611131, event #17, 2026-11-17, build 2016): a
Voiceover with four shots named on quoted words and "Fill the rest with the other clips." The
clip-intent planner made the last sentence ``include: the other clips``; the resolver matched it
to exactly the five clips no named shot used (the kit's "anywhere" clips, EXPECTED.md), all at
one confidence, so the Voiceover tie check (KRI-962) asked the creator to keep ONE of them
ticked for "the other clips". Nobody can answer that: the words mean every clip not already
placed.

Ways this can go wrong (written before the code; each has a case below):

a. The Altının yolu row is still asked (the bug).
b. A named shot is swallowed: "the other clips of the city", "her clips", the singular "the
   other clip" / "the other one", "diğer şehir klipleri" must still be asked like any shot.
c. "The other clips" falls out of the plan, so the five fill clips lose their include.
d. Nothing is left over (every clip already named): the resolver's own picker asks "I couldn't
   find "the other clips"".
e. Turkish or German wordings are missed ("diğer klipleri", "geri kalanlar", "kalan
   videolarla", "die übrigen Videos", "den Rest").
f. Another op with rest words is silenced: only an ``include`` fills the rest.
g. The multi-row tie sentence still says "keep the clip" though the picker takes several ticks
   (same message, the ticket's "Expected"): EN, TR and DE.
h. "The rest" claims a clip a named shot needs: one leftover clip the resolver also put under
   "the other clips" must not silence the paintings tie (the rest takes only what is left).

Replayed from the run's own records (``fixtures/kri1519_altinin_yolu_turn.json``). Only the
model calls are stand-ins (clip-intent planner, resolver); the parse, the resolver, the tie
check, the picker builder and the runtime-v2 planner are the real ones. No network, no paid
model call.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import clip_ties, planner
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import fills_the_rest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RUN: dict[str, Any] = json.loads((FIXTURES / "kri1519_altinin_yolu_turn.json").read_text())
# A real phone Voiceover turn (narrated_planned + a recorded voiceover, runtime v2); its media
# are swapped for the Altının yolu clips, as KRI-962's replay does.
TIGRE: dict[str, Any] = json.loads((FIXTURES / "kri593_tigre_voiceover_turn.json").read_text())

REQUEST = RUN["creator_request"]
ALL = [c["media_id"] for c in RUN["clips"]]
KIT = {c["kit_file"]: c["media_id"] for c in RUN["clips"]}
FILL = RUN["kit_fill_clips"]  # T4_04/05/06/08/09: the kit's "anywhere" clips
PAINTINGS = "include:the-paintings-in-gold-frames"
MOUNTAINS = "include:the-snowy-mountains"
OTHER = "include_other_clips"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_voiceover_tie_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind=c["kind"],
            analysis={"understanding": c["record"]},
            gcs_path=f"users/u/clips/{c['media_id']}",
        )
        for c in RUN["clips"]
    ]


def _matches() -> dict[str, list[tuple[str, float, str]]]:
    return {iid: [tuple(m) for m in ms] for iid, ms in RUN["resolver_matches"].items()}


def _intents(**attributes: str) -> list[dict[str, Any]]:
    """The run's clip-intent inventory, with ``attribute`` replaced per intent id."""
    return [
        {**i, "attribute": attributes.get(i["intent_id"], i["attribute"])}
        for i in RUN["clip_intents"]
    ]


def _replay_models(
    monkeypatch: pytest.MonkeyPatch,
    raw_intents: list[dict[str, Any]],
    matches: dict[str, list[tuple[str, float, str]]],
) -> None:
    """The REAL clip-intent parse and resolver, over the models' recorded answers."""
    alias = {c["media_id"]: f"m{i + 1:03d}" for i, c in enumerate(RUN["clips"])}

    def _plan(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(json.dumps({"intents": raw_intents, "question": None}), input)

    def _resolve(self, input, *, ctx=None):  # noqa: A002, ANN001
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=intent.intent_id,
                    assignments=[
                        ResolverAssignment(media=alias[m], confidence=c, evidence=e)
                        for m, c, e in matches.get(intent.intent_id, [])
                    ],
                )
                for intent in input.intents
            ]
        )

    def _no_vision(self, input, *, ctx=None):  # noqa: A002, ANN001
        raise AssertionError("a confident match never pays for a vision check")

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _plan)
    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolve)
    monkeypatch.setattr(ClipQuestionAgent, "run", _no_vision)


def _agent_input(message: str, language: str) -> MainCreatorInput:
    data = copy.deepcopy(TIGRE["main_creator_input"])
    data["user_message"] = message
    data["creator_request"] = REQUEST
    data["reply_language"] = language
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
    data["capability_manifest"]["media"] = [
        {"kind": c["kind"], "label": None, "media_id": c["media_id"], "duration_s": c["duration_s"]}
        for c in RUN["clips"]
    ]
    return MainCreatorInput.model_validate(data)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_intents: list[dict[str, Any]] | None = None,
    matches: dict[str, list[tuple[str, float, str]]] | None = None,
    events: list | None = None,
    message: str = REQUEST,
    language: str = "en",
) -> Any:
    """One Altının yolu planning turn through the REAL runtime-v2 `_plan_from_creator_output`."""
    raw_intents = RUN["clip_intents"] if raw_intents is None else raw_intents
    agent_input = _agent_input(message, language)
    strategy = {
        **copy.deepcopy(RUN["model_strategy"]),
        "clip_intents": [{k: v for k, v in i.items() if k != "source_quote"} for i in raw_intents],
    }
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": "Your Turkish narration leads; each named view lands on its words.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    _replay_models(monkeypatch, raw_intents, _matches() if matches is None else matches)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    token = bind_reply_language(language)
    try:
        return await planner._plan_from_creator_output(
            SimpleNamespace(),
            thread_id=uuid.uuid4(),
            item_id=uuid.UUID(manifest.item_id),
            creator_id=uuid.uuid4(),
            user_message=message,
            manifest=manifest,
            inputs=planner._CreatorInputs(
                agent_input=agent_input,
                intent_clips=_clips(),
                creator_request=None,
                raw_creator_request=REQUEST,
            ),
            output=output,
        )
    finally:
        release_reply_language(token)


def _keys(planned: Any) -> list[str]:
    question = planned.plan.clip_question
    return [c["key"] for c in question["categories"]] if question else []


def _resolved(planned: Any) -> dict[str, list[str]]:
    strategy = planned.plan.intents[0].arguments["strategy"]
    return {
        i["intent_id"]: [a["media_id"] for a in i["assignments"]]
        for i in strategy.get("resolved_clip_intents") or []
    }


def _answered(planned: Any, picks: dict[str, list[str]]) -> list:
    question = planned.plan.clip_question
    return [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [
                        {"key": c["key"], "media_ids": picks[c["key"]]}
                        for c in question["categories"]
                    ],
                }
            },
        ),
    ]


# -- the incident (a, c, g) -------------------------------------------------------------------


async def test_replay_rebuilds_event_17_when_the_rest_reads_as_a_shot(
    prod_profile, monkeypatch
) -> None:
    """The replay is the incident: read as a named shot, "the other clips" is asked again."""
    monkeypatch.setattr(clip_ties, "fills_the_rest", lambda _intent: False)
    monkeypatch.setattr(clip_intent_resolution, "fills_the_rest", lambda _intent: False)

    planned = await _turn(monkeypatch)

    assert _keys(planned) == RUN["event_17"]["category_keys"]
    [other] = [c for c in planned.plan.clip_question["categories"] if c["intent_id"] == OTHER]
    assert other["suggested_media_ids"] == RUN["event_17"]["suggested_media_ids"][OTHER]
    assert '"the other clips"' in planned.plan.response


async def test_altinin_yolu_asks_only_about_the_paintings_and_the_mountains(
    prod_profile, monkeypatch
) -> None:
    planned = await _turn(monkeypatch)

    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    assert _keys(planned) == [PAINTINGS, MOUNTAINS]  # a
    assert "other clips" not in planned.plan.response
    assert planned.plan.response == (
        'More than one of your clips shows "the paintings in gold frames", '
        '"the snowy mountains". For each one, keep the clips to use ticked and untick '
        "the others."  # g
    )
    suggested = {
        c["key"]: c["suggested_media_ids"] for c in planned.plan.clip_question["categories"]
    }
    assert suggested == {
        PAINTINGS: [KIT["T4_01"], KIT["T4_10"]],
        MOUNTAINS: [KIT["T4_03"], KIT["T5_01"]],
    }


async def test_after_the_two_taps_the_draft_keeps_the_five_fill_clips(
    prod_profile, monkeypatch
) -> None:
    asked = await _turn(monkeypatch)
    events = _answered(
        asked,
        {PAINTINGS: [KIT["T4_01"], KIT["T4_10"]], MOUNTAINS: [KIT["T4_03"]]},
    )

    planned = await _turn(monkeypatch, events=events, message="I tapped the clips you asked about.")

    assert planned.plan.clip_question is None
    assert planned.plan.turn_value != "question"
    resolved = _resolved(planned)
    assert resolved[OTHER] == FILL  # c: the rest is still used, exactly the kit's fill clips
    assert resolved["include_gold_frames"] == [KIT["T4_01"], KIT["T4_10"]]
    assert resolved["include_snowy_mountains"] == [KIT["T4_03"]]


@pytest.mark.parametrize(
    ("language", "start", "keep"),
    [
        ("tr", "Şunlar birden fazla klibinde görünüyor:", "kullanılacak klipleri işaretli bırak"),
    ],
)
async def test_turkish_chat_gets_the_same_two_rows_in_turkish(
    prod_profile, monkeypatch, language: str, start: str, keep: str
) -> None:
    planned = await _turn(monkeypatch, language=language)

    assert _keys(planned) == [PAINTINGS, MOUNTAINS]
    assert planned.plan.response.startswith(start)
    assert keep in planned.plan.response  # g


async def test_the_rest_never_takes_a_clip_a_named_shot_ties_on(prod_profile, monkeypatch) -> None:
    """(h) One paintings clip also under "the other clips": the paintings tie is still asked."""
    matches = _matches()
    matches[OTHER] = [(KIT["T4_10"], 0.9, "record match")]

    planned = await _turn(monkeypatch, matches=matches)

    [paintings] = [c for c in planned.plan.clip_question["categories"] if c["key"] == PAINTINGS]
    assert paintings["suggested_media_ids"] == [KIT["T4_01"], KIT["T4_10"]]
    assert _keys(planned) == [PAINTINGS, MOUNTAINS]


# -- nothing left over (d) ----------------------------------------------------------------------


async def test_nothing_left_over_never_asks_about_the_other_clips(
    prod_profile, monkeypatch
) -> None:
    """Every clip already named: the resolver finds nothing for the rest, and nothing is asked."""
    one_each = {
        "include_gold_frames": [(KIT["T4_01"], 0.95, "record match")],
        "include_stone_wall": [(KIT["T4_02"], 0.95, "record match")],
        "include_snowy_mountains": [(KIT["T4_03"], 0.95, "record match")],
        "include_wide_city_view": [(KIT["T4_07"], 0.95, "record match")],
        OTHER: [],
    }

    planned = await _turn(monkeypatch, matches=one_each)

    assert planned.plan.clip_question is None
    assert planned.plan.turn_value != "question"
    assert "other clips" not in (planned.plan.response or "")
    assert _resolved(planned)[OTHER] == []


async def test_a_named_shot_with_nothing_found_is_still_asked(prod_profile, monkeypatch) -> None:
    """Control for (d): the same empty match on a real subject still asks."""
    one_each = {
        "include_gold_frames": [(KIT["T4_01"], 0.95, "record match")],
        "include_stone_wall": [(KIT["T4_02"], 0.95, "record match")],
        "include_snowy_mountains": [(KIT["T4_03"], 0.95, "record match")],
        "include_wide_city_view": [(KIT["T4_07"], 0.95, "record match")],
        OTHER: [],
    }

    planned = await _turn(
        monkeypatch,
        raw_intents=_intents(include_other_clips="the llamas on the terrace"),
        matches=one_each,
    )

    assert _keys(planned) == ["include:the-llamas-on-the-terrace"]


# -- wrong-content controls (b, f) --------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute",
    ["the other clips of the city", "the other stone wall clips", "her clips", "the other one"],
)
async def test_a_named_shot_described_with_rest_words_is_still_asked(
    prod_profile, monkeypatch, attribute: str
) -> None:
    """Same five tied clips, but the words name a subject: that is a shot, and it is asked."""
    planned = await _turn(monkeypatch, raw_intents=_intents(include_other_clips=attribute))

    keys = _keys(planned)
    assert keys[:2] == [PAINTINGS, MOUNTAINS]
    [other] = [c for c in planned.plan.clip_question["categories"] if c["intent_id"] == OTHER]
    assert other["suggested_media_ids"] == FILL


# -- the words (b, e, f) -----------------------------------------------------------------------


def _include(attribute: str, **extra: Any) -> ClipIntent:
    return ClipIntent(intent_id="include_x", op="include", attribute=attribute, **extra)


@pytest.mark.parametrize(
    "attribute",
    [
        # English
        "the other clips",
        "the rest",
        "the rest of the clips",
        "the remaining clips",
        "all the other videos",
        "the others",
        "everything else",
        "my leftover footage",
        "the unused shots",
        "all my clips",
        # Turkish (with and without Turkish letters)
        "diğer klipler",
        "diğer klipleri",
        "diger kliplerle",
        "geri kalan videolar",
        "geri kalanlar",
        "kalan videolarla",
        "kalanlar",
        "diğerleri",
        "geri kalan her şey",
        "tüm kliplerim",
        "başka çekimler",
        # German (with and without umlauts)
        "die anderen Clips",
        "die übrigen Videos",
        "die uebrigen Videos",
        "den Rest",
        "alle restlichen Clips",
        "alles andere",
        "die restlichen Aufnahmen",
    ],
)
def test_the_rest_of_the_creators_clips_fills(attribute: str) -> None:
    assert fills_the_rest(_include(attribute))


@pytest.mark.parametrize(
    "attribute",
    [
        "the other clips of the city",  # a subject
        "the other stone wall clips",
        "the other beach clips",
        "her clips",  # someone's clips
        "his videos",
        "the other clip",  # one clip
        "the other one",
        "the clips",  # no rest word: names nothing
        "my videos",
        "the remaining clips (excluding the talk video)",
        "diğer şehir klipleri",
        "diğer klip",
        "die anderen Clips vom Markt",
        "the snowy mountains",
    ],
)
def test_a_named_shot_is_never_the_rest(attribute: str) -> None:
    assert not fills_the_rest(_include(attribute))


@pytest.mark.parametrize("op", ["order", "group", "exclude", "caption", "label", "hide_part"])
def test_only_an_include_fills_the_rest(op: str) -> None:
    extra = {"creator_text": "x"} if op in {"caption", "label"} else {}
    intent = ClipIntent(intent_id="x", op=op, attribute="the other clips", **extra)
    assert not fills_the_rest(intent)


def test_an_only_about_ask_is_never_the_rest() -> None:
    assert not fills_the_rest(_include("the other clips", exclusive=True))
