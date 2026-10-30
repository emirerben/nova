"""Leverkusen M2: a shot the creator named is never called "missing" before Kria looks for it.

Leverkusen M2 run 1 (thread d048973b, 2026-10-30, Fly eed794157): "Order: our selfie, the 0:0
scoreboard, the "TOR" on the big screen, then ...". The big screen is 1.5 s inside IMG_1039
(6.0-7.5 s). Turn c0ce1621 ran twice on the same input: attempt 1 proposed a strategy and ran the
clip-intent planner, the resolver and two vision checks; attempt 2's Main Creator answered
``AskUser(reason_code="missing_media")`` and the turn ended on "I couldn't find the shot of 'TOR'
on the big screen. Should I build the edit without it, or do you want to upload it?" with "Build
without it" / "I'll upload it". Nothing had looked inside a clip: an ``AskUser`` went straight to
``ask_user_choice``. Which of the two answers a creator gets was the model's coin flip.

The rule (a code rule, not prompt wording): an ask that says footage is missing, on an item with
clips, goes through the same clip-intent planner, resolver and vision checks a strategy turn
uses. "Upload it" reaches the creator only when every clip was checked (the creator said the shot
is in none of them); otherwise the reply is the clip picker ("which of your clips show ..."),
the still-checking reply, what the check found, or a question naming the clips.

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: the missing-media ask is shown with "I'll upload it" while no clip was looked
    inside; no planner, resolver or vision ran.
 F2 the clips are checked and vision is unsure or finds nothing: the reply still offers "upload
    it" instead of the clip picker that names the clips.
 F3 vision confirms the shot in a clip: the reply still says "couldn't find it" or keeps an
    upload option.
 F4 a vision budget/deadline stop (pending) becomes "missing"/upload instead of the continuable
    still-checking reply.
 F5 a provider failure in the clip pipeline crashes the turn or becomes "upload it".
 F6 every clip was checked and the creator answered the picker "None": the honest upload ask is
    suppressed and the picker loops.
 F7 the clip planner reads no shot from the words (nothing was looked for): "upload it" anyway,
    or a dead end with no question.
 F8 no clips attached: a real upload ask is swallowed.
 F9 a missing voiceover / song ask is sent to vision on footage.
 F10 a different ask (hook wording, pacing, a clarification) is touched or pays for clip checks.
 F11 the gate's vision answers are not cached, so the next turn pays again and may differ.
 F12 detection drifts with the model's reason code (``missing_shot``, ``shot_not_found``) and
     the gate is bypassed; or it guesses from the ask's prose or option labels (KRI-667).
 F13 the clip-intents flag is off: anything changes.
 F14 the gate's own reply is English in a Turkish chat.
 F15 loop: the creator taps "Yes, build it" on the found-it card, the next run says "missing"
     again, and the same card comes back forever.
 F16 a Talking / Subtitled edit (no clip intents) runs footage checks.

The turns run the real Main Creator parser, the real runtime-v2 planner
(``_plan_from_creator_output``) and the real clip-intent planning, resolver and vision plumbing
over this run's prompt, reply and media (``leverkusen_m2_turn1.json``, which also says what is
reconstructed). Only the model transport and storage are replayed: no model is called.
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

from app.agents._runtime import ModelInvocation
from app.agents._skills import load_skill
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.missing_media import FOUND_CONFLICT, asks_for_missing_media
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.pipeline.prompt_loader import load_prompt
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_capabilities import resolve_creator_manifest

FIXTURE = Path(__file__).with_name("leverkusen_m2_turn1.json")
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
CABIN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
    ).read_text()
)
BY_FILE = {m["file_name"]: m["media_id"] for m in CASE["media"] if m["file_name"]}
GOAL_1 = BY_FILE["IMG_1037"]  # Taha celebrating
GOAL_2 = BY_FILE["IMG_1039"]  # the kids, the TOR on the big screen, Taha's scream
SELFIE, SCOREBOARD = (m["media_id"] for m in CASE["media"] if m["kind"] == "image")
EVERYTHING = [m["media_id"] for m in CASE["media"]]
PROMPT = CASE["prompt"]
TOR = CASE["tor"]["label"]
TOR_ID = CASE["tor"]["intent_id"]
TOR_KEY = CASE["tor"]["key"]
INVENTORY: list[dict[str, Any]] = CASE["inventory_turn1"]
ASK: dict[str, Any] = CASE["main_creator_action"]


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED and CLIP_FACTS_ENABLED are on in prod (CLAUDE.md); the picker,
    # skips and the choice cards default on.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


def _intent_clips(media: list[dict[str, Any]] | None = None) -> list[IntentClip]:
    """The chat's two goal clips and two Visuals photos, in the order the picker listed them."""
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": m["record"]},
            gcs_path=f"users/u/{m['media_id']}",
            file_name=m["file_name"],
            duration_s=m["duration_s"],
        )
        for m in (CASE["media"] if media is None else media)
    ]


# -- the model transport and storage, replayed ---------------------------------------------

_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)
_INTENT_LINE = re.compile(r'^- intent_id=(\S+) \| op=(\w+) \| attribute="(.*)"', re.M)
_RECORD = {m["media_id"]: m["record"] for m in CASE["media"]}

# The vision verdicts on "the TOR on the big screen" turn 1a0a5b16 logged: the goal-2 clip
# kept, goal 1 and the scoreboard photo unsure (a photo is never sent to vision: unknown).
SEEN = {GOAL_2: ("yes", 0.9, "The stadium big screen shows TOR after the goal")}
UNSURE = {GOAL_1: ("unknown", 0.0, "")}


def _alias_of(prompt: str, media_id: str) -> str:
    [alias] = re.findall(
        rf"^- (m\d{{3}}) \((?:video|image)\): .*{re.escape(_RECORD[media_id][:40])}", prompt, re.M
    )
    return alias


class _Provider:
    """The inventory planner, the clip resolver and the vision check, as this chat read them.

    The resolver settles the photos and Taha from their records and, as in the run, cannot
    place the big screen from any record, so it sends ``tor_checks`` (the run: both goal clips
    and the scoreboard photo) to vision for it. ``vision`` is the verdict per clip on that
    question; ``fail`` makes every text call raise (a provider outage).
    """

    def __init__(
        self,
        *,
        inventory: list[dict[str, Any]] | None = None,
        vision: dict[str, tuple[str, float, str]] | None = None,
        tor_checks: tuple[str, ...] = (GOAL_1, GOAL_2, SCOREBOARD),
        fail: bool = False,
    ) -> None:
        self.inventory = INVENTORY if inventory is None else inventory
        self.vision = {**SEEN, **UNSURE} if vision is None else vision
        self.tor_checks = tor_checks
        self.fail = fail
        self.calls: list[str] = []
        self.looked_at: list[str] = []  # clips vision looked inside

    def invoke(
        self, *, prompt: str, media_uri: str | None = None, **_kwargs: Any
    ) -> ModelInvocation:
        if media_uri is not None:
            media_id = media_uri.removeprefix("files/users/u/")
            self.calls.append("vision")
            self.looked_at.append(media_id)
            said, confidence, evidence = self.vision.get(media_id, ("unknown", 0.0, ""))
            return ModelInvocation(
                raw_text=json.dumps(
                    {"answer": said, "confidence": confidence, "evidence": evidence}
                )
            )
        if self.fail:
            raise RuntimeError("provider down")
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            return ModelInvocation(
                raw_text=json.dumps({"intents": self.inventory, "question": None})
            )
        if not prompt.startswith(_RESOLVER_PROMPT):
            raise AssertionError(f"unexpected model call: {prompt[:80]!r}")
        self.calls.append("resolver")
        out = []
        for intent_id, _op, _attribute in _INTENT_LINE.findall(prompt):
            entry: dict[str, Any] = {"intent_id": intent_id, "assignments": []}
            if intent_id == "order_selfie_first":
                entry["assignments"] = [
                    {"media": _alias_of(prompt, SELFIE), "confidence": 0.95, "evidence": "selfie"}
                ]
            elif intent_id == "order_scoreboard_second":
                entry["assignments"] = [
                    {"media": _alias_of(prompt, SCOREBOARD), "confidence": 0.95, "evidence": "0:0"}
                ]
            elif intent_id == "order_taha_fourth":
                entry["assignments"] = [
                    {"media": _alias_of(prompt, m), "confidence": 0.9, "evidence": "celebrating"}
                    for m in (GOAL_1, GOAL_2)
                ]
            elif intent_id == TOR_ID:
                entry["needs_vision"] = [
                    {"media": _alias_of(prompt, m), "question": "Is TOR on the big screen?"}
                    for m in self.tor_checks
                ]
            out.append(entry)
        return ModelInvocation(raw_text=json.dumps({"intents": out}))


def _provider(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Provider:
    provider = _Provider(**kwargs)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)

    def _download(object_path: str, local_path: str) -> None:
        with open(local_path, "w") as fh:
            fh.write(object_path)

    def _upload(path: str, timeout: int = 120) -> SimpleNamespace:
        with open(path) as fh:
            return SimpleNamespace(uri=f"files/{fh.read()}", mime_type="video/mp4")

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _upload)
    return provider


# -- one chat turn through the runtime-v2 planner ------------------------------------------


def _agent_input(edit_format: str = "montage") -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = PROMPT
    data["creator_request"] = PROMPT
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": PROMPT}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format=edit_format,
        media=[
            {"media_id": m["media_id"], "kind": "video", "duration_s": m["duration_s"]}
            for m in CASE["media"]
            if m["kind"] == "video"
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action: dict[str, Any] | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    clips: list[IntentClip] | None = None,
    edit_format: str = "montage",
) -> PlannedKriaTurn:
    """One planning turn on the Main Creator's recorded answer; the rest is replayed."""
    agent_input = _agent_input(edit_format)
    raw = {"action": ASK if action is None else action, "brief_updates": []}
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    # The clip-answer cache write (Postgres) is recorded, not run: F11 reads it.
    monkeypatch.setattr(planner, "_persist_turn_vision_answers", AsyncMock(return_value=None))
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=PROMPT,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips() if clips is None else clips,
            creator_request=None,
            raw_creator_request=PROMPT,
        ),
        output=output,
    )


def _offers_upload(result: PlannedKriaTurn) -> bool:
    plan = result.plan
    labels = [o["label"] for o in (plan.choice_question or {}).get("options", [])]
    return any("upload" in text.casefold() for text in [plan.response or "", *labels])


# -- F1: the incident -----------------------------------------------------------------------


def test_the_fixture_is_the_reply_the_creator_saw() -> None:
    reply = CASE["turn1_reply"]
    assert reply.startswith(ASK["question"])
    assert "1. Build without it\n2. I'll upload it" in reply
    assert CASE["turn1_conflict"] == f"ask_user:{ASK['reason_code']}"
    # The answer key puts the big screen inside goal 2, 6.0-7.5 s of 18.3 s.
    goal_2 = next(m for m in CASE["media"] if m["media_id"] == GOAL_2)
    assert "6.0-7.5 TOR on the big screen" in goal_2["shows"]
    # The (reconstructed) record the Main Creator read says nothing about a big screen.
    assert "screen" not in goal_2["record"].casefold()


async def test_f1_the_incident_turn_looks_inside_the_clips_before_saying_missing(
    monkeypatch,
) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch)

    # The shot went through the same planner -> resolver -> vision path a strategy turn takes.
    assert provider.calls[:2] == ["planner", "resolver"]
    assert GOAL_2 in provider.looked_at
    # Vision kept goal 2 and was unsure about goal 1 and the photo. Never "upload it": either
    # the clip picker asks about the TOR, or (once the resolver settles an ask on its confident
    # match, Leverkusen M2 bug 2) the reply says the TOR was found in IMG_1039.
    plan = result.plan
    assert (plan.mode, plan.turn_value) == ("respond", "question")
    assert not _offers_upload(result), plan.response
    assert "couldn't find" not in plan.response
    if plan.clip_question is not None:
        assert plan.choice_question is None
        assert TOR_KEY in [c["key"] for c in plan.clip_question["categories"]]
        assert f'"{TOR}"' in plan.response
    else:
        assert plan.choice_question["conflict"] == FOUND_CONFLICT
        assert f"“{TOR}” (IMG_1039)" in plan.response
    # F11: what vision saw is cached on the clip, so the next turn does not pay for it again.
    saved = planner._persist_turn_vision_answers.await_args.kwargs["planned"]
    assert GOAL_2 in saved.resolution.vision_answers


# -- F2 / F3: what the looking found decides the reply ------------------------------------

NOT_THERE = ("no", 0.95, "Only the pitch and the crowd")


async def test_f2_vision_finds_it_nowhere_asks_with_every_clip_never_upload(monkeypatch) -> None:
    provider = _provider(
        monkeypatch, vision={GOAL_1: NOT_THERE, GOAL_2: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2)
    )

    result = await _turn(monkeypatch)

    assert sorted(provider.looked_at) == sorted([GOAL_1, GOAL_2])
    assert not _offers_upload(result), result.plan.response
    [row] = [c for c in result.plan.clip_question["categories"] if c["key"] == TOR_KEY]
    # Every clip is offered: the creator may know the one vision missed.
    assert GOAL_2 in row["candidate_media_ids"]
    assert f'"{TOR}"' in result.plan.response


FOUND_REPLY = (
    "I looked inside your clips and found every shot you named: “our selfie” (photo 1); "
    '“the 0:0 scoreboard” (photo 2); “the "TOR" on the big screen” (IMG_1039); '
    "“Taha celebrating” (IMG_1037, IMG_1039). Should I build the edit with them?"
)


async def test_f3_vision_finds_every_named_shot_says_where_and_offers_to_build(
    monkeypatch,
) -> None:
    _provider(monkeypatch, vision={**SEEN, GOAL_1: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2))

    result = await _turn(monkeypatch)

    plan = result.plan
    assert (plan.mode, plan.turn_value) == ("respond", "question")
    assert plan.response.startswith(FOUND_REPLY), plan.response
    assert "couldn't find" not in plan.response
    assert not _offers_upload(result)
    card = plan.choice_question
    assert card["conflict"] == FOUND_CONFLICT
    assert [o["label"] for o in card["options"]] == ["Yes, build it", "No, something's wrong"]
    assert plan.clip_question is None


async def test_f3_without_choice_cards_the_same_words_are_the_reply(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
    _provider(monkeypatch, vision={**SEEN, GOAL_1: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2))

    result = await _turn(monkeypatch)

    assert result.plan.response == FOUND_REPLY
    assert result.plan.choice_question is None


# -- F4 / F5: a stopped or failed look is never "missing" ----------------------------------


async def test_f4_a_vision_budget_stop_is_the_still_checking_reply(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_clip_intents_max_vision_requeries", 0)
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch)

    assert provider.looked_at == []
    assert result.plan.turn_value == "recovery"
    assert result.continuable is True
    assert not _offers_upload(result)
    assert result.plan.response == planner._clip_intent_pending_reply()


async def test_f5_a_provider_failure_is_a_retry_not_upload(monkeypatch) -> None:
    _provider(monkeypatch, fail=True)

    result = await _turn(monkeypatch)

    assert result.plan.turn_value == "recovery"
    assert not _offers_upload(result)
    assert result.plan.choice_question is None


# -- F6 / F15: once every clip was checked, the ask stands ---------------------------------


def _picker_answered_none(question: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The thread after the creator tapped "None" for the big screen in the picker."""
    return [
        ("user", {"turn_id": "c0ce1621"}),
        (
            "assistant",
            {"clip_question": question, "turn_id": "c0ce1621-reply", "turn_value": "question"},
        ),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [],
                    "none_keys": [TOR_KEY],
                    "skipped": False,
                },
                "turn_id": "1a0a5b16",
            },
        ),
    ]


async def test_f6_the_creator_said_none_of_the_clips_so_the_upload_ask_stands(
    monkeypatch,
) -> None:
    # Vision finds the TOR nowhere, so the turn asks with the picker; the creator taps "None".
    _provider(
        monkeypatch, vision={GOAL_1: NOT_THERE, GOAL_2: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2)
    )
    asked = await _turn(monkeypatch)

    result = await _turn(monkeypatch, events=_picker_answered_none(asked.plan.clip_question))

    # Every clip was checked (by the creator): the Main Creator's honest ask is shown.
    assert result.plan.response == CASE["turn1_reply"]
    assert result.plan.choice_question["conflict"] == CASE["turn1_conflict"]


async def test_f15_the_found_card_is_never_asked_twice_in_a_row(monkeypatch) -> None:
    _provider(monkeypatch, vision={**SEEN, GOAL_1: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2))
    card = (await _turn(monkeypatch)).plan
    events = [
        ("user", {"turn_id": "c0ce1621"}),
        (
            "assistant",
            {
                "turn_value": card.turn_value,
                "choice_question": card.choice_question,
                "turn_id": "c0ce1621-reply",
            },
        ),
        ("user", {"choice_selection": {"question_id": "q", "option": "option_1"}}),
    ]

    result = await _turn(monkeypatch, events=events)

    assert result.plan.response == CASE["turn1_reply"]


# -- F7: nothing to look for is a question naming the clips --------------------------------


async def test_f7_no_shot_read_from_the_words_asks_which_clip_never_upload(monkeypatch) -> None:
    provider = _provider(monkeypatch, inventory=[i for i in INVENTORY if i["op"] == "exclude"])

    result = await _turn(monkeypatch)

    assert provider.calls[0] == "planner"
    plan = result.plan
    assert (plan.mode, plan.turn_value) == ("respond", "question")
    assert not _offers_upload(result), plan.response
    assert "(IMG_1037, IMG_1039, photo 1, photo 2)" in plan.response
    assert "tell me which one and about when" in plan.response


# -- F8-F10, F13, F16: everything else is unchanged and looks at nothing -------------------


async def test_f8_no_clips_keeps_the_upload_ask(monkeypatch) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch, clips=[])

    assert result.plan.response == CASE["turn1_reply"]
    assert provider.calls == []


@pytest.mark.parametrize(
    "action",
    [
        # F9: a voiceover or song no clip can hold.
        {
            "kind": "ask_user",
            "question": "I don't have your voiceover yet. Record it or upload it?",
            "reason_code": "missing_voiceover",
            "options": ["Record it now", "I'll upload it"],
        },
        {
            "kind": "ask_user",
            "question": "Which song should play?",
            "reason_code": "song_choice",
            "options": ["Pick one for me", "I'll upload my song"],
        },
        # F10: asks that say nothing is missing.
        {
            "kind": "ask_user",
            "question": "Fast cuts or slow ones?",
            "reason_code": "pacing_choice",
            "options": ["Fast", "Slow"],
        },
        {
            "kind": "ask_user",
            "question": "How long should it be?",
            "reason_code": "missing_duration",
            "options": ["12 seconds", "20 seconds"],
        },
    ],
)
async def test_f9_f10_other_asks_are_unchanged_and_look_at_nothing(monkeypatch, action) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch, action=action)

    assert result.plan.response.startswith(action["question"])
    assert provider.calls == []


async def test_f13_with_clip_intents_off_nothing_changes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch)

    assert result.plan.response == CASE["turn1_reply"]
    assert provider.calls == []


async def test_f16_a_talking_edit_runs_no_footage_checks(monkeypatch) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch, edit_format="talking_head")

    assert result.plan.response == CASE["turn1_reply"]
    assert provider.calls == []


# -- F12: the rule reads the ask's machine code, not its prose or options ----------------


@pytest.mark.parametrize(
    ("reason_code", "missing"),
    [
        ("missing_media", True),
        ("missing_shot", True),
        ("missing_footage", True),
        ("Missing-Clip", True),
        ("shot_not_found", True),
        ("media_not_available", True),
        ("no_matching_clip", True),
        ("upload_needed", True),
        ("missing_voiceover", False),
        ("missing_song", False),
        ("song_upload", False),
        ("missing_duration", False),
        ("missing_title", False),
        ("clip_choice", False),
        ("hook_candidate", False),
        ("pacing_choice", False),
    ],
)
def test_f12_missing_footage_asks_are_read_from_the_reason_code(reason_code, missing) -> None:
    # The typed machine code, never the ask's prose or option labels (KRI-667).
    assert asks_for_missing_media(reason_code) is missing


# -- F14: a Turkish chat hears it in Turkish -----------------------------------------------


async def test_f14_a_turkish_chat_gets_the_found_card_in_turkish(monkeypatch) -> None:
    _provider(monkeypatch, vision={**SEEN, GOAL_1: NOT_THERE}, tor_checks=(GOAL_1, GOAL_2))

    with reply_language_for("tr"):
        result = await _turn(monkeypatch)

    assert result.plan.response.startswith(
        "Kliplerinin içine baktım ve istediğin her çekimi buldum: “our selfie” (fotoğraf 1)"
    ), result.plan.response
    assert [o["label"] for o in result.plan.choice_question["options"]] == [
        "Evet, oluştur",
        "Hayır, bir şey yanlış",
    ]


async def test_f14_a_turkish_chat_gets_the_which_clip_question_in_turkish(monkeypatch) -> None:
    _provider(monkeypatch, inventory=[i for i in INVENTORY if i["op"] == "exclude"])

    with reply_language_for("tr"):
        result = await _turn(monkeypatch)

    assert result.plan.response.startswith("O çekimi kliplerin hakkındaki notlarımdan")
    assert "(IMG_1037, IMG_1039, fotoğraf 1, fotoğraf 2)" in result.plan.response
