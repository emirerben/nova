"""Leverkusen M3 run 1 (KRI-1108): "End on the trophy celebration." was seated on a clip with no
trophy, and Kria never said there is no trophy shot.

Prod thread 83f2dcaa (2026-10-31, Fly 7b5504165, TestFlight 1819). The game was a second-round
cup match: nothing like a trophy was filmed. IMG_1043 (alias m005) shows the Nürnberg players at
the away end thanking their fans, stewards in front. Its stored clip record (ClipUnderstanding)
says "confetti falls from stands" / "Confetti or similar debris is thrown from the stands" and
"players gather on pitch", and no record in the batch mentions a trophy. The clip-request
resolver nevertheless matched the ``order`` / ``last`` ask "the trophy celebration" to m005 in
both turns that carried it: 13:21:02 (``order_trophy_last``, evidence "players gather on the
pitch after a match, crowd cheering, confetti falls", confidence 0.8) and 13:24:00
(``order_celebration_last``, "... Confetti or similar debris is thrown from the stands.", 0.9).
``resolve_clip_intents_for_turn`` keeps any membership assignment at confidence >= 0.6
(``MEMBERSHIP_MIN_CONFIDENCE``) without looking at what it says, so the ask resolved silently:
no picker, no "I found no clips for ...", and the brief kept ``last_clip: "the trophy
celebration"``. The creator had to say "There is no trophy in my footage" at 13:26:57.

Ways this can fail, written before the fix (each is pinned below):

 F1 the incident: a first/last ask whose subject no clip record mentions is seated on the
    closest-looking clip at membership confidence, silently (both recorded turns).
 F2 the creator is never told: the ask stays ``resolved``, so the turn asks nothing about it and
    the receipts never say "I found no clips for the trophy celebration".
 F3 a fix that only checks ``evidence`` against the clip record passes this match: the evidence
    here IS the record's own words (confetti, players gather on the pitch), the record is what
    is wrong about the footage, and the trophy is in no record at all.
 F4 a fix that only raises the confidence bar passes it: the recorded confidences are 0.8 and
    0.9, at or above every bar the service has (label bar 0.8).
 F5 over-correction: a real ending described in other words ("the Nürnberg players thanking
    their fans (IMG_1043)", evidence "players gather on the pitch after a match", 13:35) or
    "my ticket" -> the ticket photo stops resolving.
 F6 a fix that sends the clip to the vision check reads an unknown or "no" answer as a match.
 F7 the creator is asked, but the clip picker comes with IMG_1043 ticked (KRI-991 pre-ticks
    what the resolver put forward), so one tap on Done keeps the invented match.
 F8 the question reads as Kria being unsure ("I couldn't tell which of your clips show ..."),
    never that nothing in the footage shows it, so the creator does not learn it is missing.
 F9 a hard reject: the trophy row offers no clips and no "None of these", so a creator who
    knows a clip shows it cannot pick it, and one who knows it doesn't cannot say so.
 F10 an ask written in Turkish is judged against clip records the analyzer writes in English:
    no word of it can ever be found, so every Turkish first/last pin would be held.

Found while fixing, pinned by existing guards: a clip-by-clip check drops the second goal clip
from "Taha celebrating in both goal clips" (only one record says "celebrating";
``test_leverkusen_m2_missing_shot`` F3); a check without word endings misses "my friend
smiling" on a record that says "smiles" (``test_kri991_picker_suggestions``); and an ask no
record says any word of ("the final whistle", "my friend in the red cap" on one-line records)
is beyond what words can judge, so it stays the resolver's (``test_kri1067_filming_order_end_
picker`` F10/F11, ``test_kri802_question_batch``).

Feeds the RECORDED resolver input (clip records, intents, creator request) through the real
``resolve_clip_intents_for_turn`` with the RECORDED model output (``raw_text``) replayed by a stub
client, and checks the prompt the replay sent is byte-identical to what the recorded input
renders. A vision check, if a fix asks one, answers from the tester's own look at the footage
(``footage_truth``: no clip shows a trophy). No model is called. Fixture:
``m3_kri1108_resolver.json`` (says what is recorded and what is reconstructed).
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents.clip_question import ClipQuestionOutput
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverInput
from app.config import settings
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.schemas.clip_understanding import UNDERSTANDING_KEY
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    resolve_clip_intents_for_turn,
)

CASE: dict[str, Any] = json.loads(Path(__file__).with_name("m3_kri1108_resolver.json").read_text())
CLIPS = [
    IntentClip(
        media_id=c["media_id"],
        kind=c["kind"],
        analysis={UNDERSTANDING_KEY: c["record"]},
        file_name=c["file_name"],
    )
    for c in CASE["clips"]
]
BY_ALIAS = {c["alias"]: c["media_id"] for c in CASE["clips"]}
IMG_1043 = BY_ALIAS["m005"]
TICKET = BY_ALIAS["m006"]
# (recorded turn, the trophy intent's id in that turn, its recorded confidence on m005)
TROPHY_TURNS = [("t2_1321", "order_trophy_last", 0.8), ("t3_1324", "order_celebration_last", 0.9)]


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    # On in prod for this thread (CLAUDE.md; the 13:28 turn showed a clip picker).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


class _RecordedResolver:
    """The recorded model output for one turn; keeps every prompt it was sent."""

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        self.prompts.append(prompt)
        return ModelInvocation(raw_text=self.raw_text)


def _footage(clip: IntentClip, question: str, **_kwargs: Any) -> ClipQuestionOutput:
    """F6: the tester's look at the clips. No clip shows a trophy; nothing else is known."""
    if "troph" in question.casefold():
        assert CASE["footage_truth"]["trophy_anywhere"] is False
        return ClipQuestionOutput(answer="no", confidence=0.9, evidence="no trophy in the clip")
    return ClipQuestionOutput()


async def _replay(monkeypatch: pytest.MonkeyPatch, turn: str) -> IntentResolution:
    recorded = CASE["turns"][turn]
    provider = _RecordedResolver(recorded["raw_text"])
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "query_clip_vision", _footage)
    intents = [
        ClipIntent(
            intent_id=i["intent_id"],
            op=recorded["planner_op"][i["intent_id"]],
            attribute=i["attribute"],
            position=i["position"],
            creator_text=i["creator_text"],
            caption_attribute=i["caption_attribute"],
            ordered_members=i["ordered_members"],
        )
        for i in recorded["resolver_intents"]
    ]
    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=recorded["creator_request"],
        clips=CLIPS,
        run_context=RunContext(),
    )
    # The replay is the prod turn: one resolver call, sent exactly what the recorded input renders.
    prod_input = ClipRequestResolverInput.model_validate(
        {
            "creator_request": recorded["creator_request"],
            "intents": recorded["resolver_intents"],
            "clips": [
                {"alias": c["alias"], "kind": c["kind"], "record": c["record"]}
                for c in CASE["clips"]
            ],
            "reply_language": recorded["reply_language"],
        }
    )
    expected = ClipRequestResolverAgent(provider).render_prompt(prod_input)
    assert len(provider.prompts) == 1
    diff = list(
        difflib.unified_diff(
            _key_order_free(expected),
            _key_order_free(provider.prompts[0]),
            "recorded",
            "replay",
            n=0,
        )
    )
    assert not diff, "\n".join(diff[:40])
    return result


_CLIP_LINE = re.compile(r"^- (m\d{3}) \((video|image)\): (\{.*\})$")


def _key_order_free(prompt: str) -> list[str]:
    """The prompt's lines with each clip record's keys sorted: the trace stores records as
    Postgres JSONB, which reorders keys, so only the key ORDER of a clip line may differ."""
    lines = []
    for line in prompt.splitlines():
        match = _CLIP_LINE.match(line)
        if match:
            record = json.dumps(json.loads(match[3]), ensure_ascii=False, sort_keys=True)
            line = f"- {match[1]} ({match[2]}): {record}"
        lines.append(line)
    return lines


def _intent(result: IntentResolution, intent_id: str) -> ResolvedClipIntent:
    [intent] = [i for i in result.intents if i.intent_id == intent_id]
    return intent


def _record_text(alias: str) -> str:
    [clip] = [c for c in CASE["clips"] if c["alias"] == alias]
    return json.dumps(clip["record"], ensure_ascii=False).casefold()


# -- F3 / F4: what the recorded data says ------------------------------------------------------


def test_the_record_names_confetti_and_no_record_names_a_trophy() -> None:
    """F3: the resolver's evidence quotes m005's record; the trophy is in no record at all."""
    assert "confetti falls from stands" in _record_text("m005")
    assert not any("troph" in _record_text(c["alias"]) for c in CASE["clips"])
    for turn, intent_id, confidence in TROPHY_TURNS:
        [block] = [
            b
            for b in json.loads(CASE["turns"][turn]["raw_text"])["intents"]
            if b["intent_id"] == intent_id
        ]
        [assignment] = block["assignments"]
        assert (assignment["media"], assignment["confidence"]) == ("m005", confidence)
        assert "confetti" in assignment["evidence"].casefold()


# -- F1 / F2 / F6: the incident ---------------------------------------------------------------


@pytest.mark.parametrize(("turn", "intent_id", "_confidence"), TROPHY_TURNS, ids=["13:21", "13:24"])
async def test_the_trophy_ending_is_not_seated_on_a_clip_without_a_trophy(
    monkeypatch: pytest.MonkeyPatch, turn: str, intent_id: str, _confidence: float
) -> None:
    result = await _replay(monkeypatch, turn)
    trophy = _intent(result, intent_id)
    members = [a.media_id for a in trophy.assignments]
    assert IMG_1043 not in members, (
        f"{intent_id} ({trophy.attribute!r}) resolved as {trophy.status} on IMG_1043 "
        f"with evidence {[a.evidence for a in trophy.assignments]!r}"
    )
    # Nothing in the footage shows a trophy, so no clip is the trophy celebration.
    assert members == []
    # F2: the creator hears about it: the turn asks about the trophy, or the ask settles with
    # no clips, which the receipts report as "I found no clips for the trophy celebration".
    if trophy.status == "needs_creator":
        assert "trophy" in (result.question or "").casefold(), result.question


# -- F5: real endings described in other words still resolve -----------------------------------


async def test_the_real_ending_on_img_1043_and_the_ticket_still_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fans = _intent(await _replay(monkeypatch, "t6_1335"), "order_nuremberg_fans_last")
    assert (fans.status, [a.media_id for a in fans.assignments]) == ("resolved", [IMG_1043])
    ticket = _intent(await _replay(monkeypatch, "t2_1321"), "order-first-my-ticket")
    assert (ticket.status, [a.media_id for a in ticket.assignments]) == ("resolved", [TICKET])


# -- F7 / F8 / F9: the honest path -------------------------------------------------------------


async def test_the_picker_says_the_trophy_was_not_found_and_ticks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _replay(monkeypatch, "t3_1324")
    assert result.status == "needs_creator"
    assert result.clip_question is not None, result.question
    [row] = [
        c for c in result.clip_question["categories"] if c["intent_id"] == "order_celebration_last"
    ]
    # F7: nothing comes ticked, so Done never keeps the invented match.
    assert row["suggested_media_ids"] == []
    # F9: every video is still offered, and the creator can answer "None of these".
    videos = [c["media_id"] for c in CASE["clips"] if c["kind"] == "video"]
    assert set(videos) <= set(row["candidate_media_ids"])
    assert result.clip_question["allow_none"] is True
    # F8: the creator learns it was not found, not that Kria was unsure which clip.
    question = (result.question or "").casefold()
    assert "couldn't find" in question and '"the trophy celebration"' in question, question
    assert "couldn't tell" not in question, question
    # The turn's other asks are untouched: the ticket opens, the match runs in order.
    assert [a.media_id for a in _intent(result, "order_ticket_first").assignments] == [TICKET]
    assert _intent(result, "order_ticket_first").status == "resolved"
    assert _intent(result, "order_match_chrono").status == "resolved"


async def test_without_the_picker_the_text_question_says_the_trophy_was_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    result = await _replay(monkeypatch, "t3_1324")
    assert result.status == "needs_creator"
    assert result.clip_question is None
    question = (result.question or "").casefold()
    assert "couldn't find" in question and '"the trophy celebration"' in question, question


# -- F10: an ask in Turkish is never judged against English records -----------------------------

_TR_GOLDEN = (
    Path(__file__).parents[1]
    / "fixtures/agent_evals/clip_request_resolver/golden/kri845_cafe_look_alike_skip.json"
)


async def test_a_turkish_first_clip_ask_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """KRI-845's golden (hand-authored from the Merope cafe M1 thread): "Caro'nun elinde siyah
    tepsiyle geldiği klip ilk olsun" is m002, whose English record says "black round tray". No
    Turkish word is in any record, and the match is right."""
    golden = json.loads(_TR_GOLDEN.read_text())
    provider = _RecordedResolver(golden["raw_text"])
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "query_clip_vision", _footage)
    clips = [
        IntentClip(
            media_id=f"media-{c['alias']}",
            kind=c["kind"],
            analysis={UNDERSTANDING_KEY: c["record"]},
        )
        for c in golden["input"]["clips"]
    ]
    intents = [
        ClipIntent(
            intent_id=i["intent_id"],
            op="exclude" if i["intent_id"].startswith("exclude_") else i["op"],
            attribute=i["attribute"],
            position=i["position"],
        )
        for i in golden["input"]["intents"]
    ]
    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=golden["input"]["creator_request"],
        clips=clips,
        run_context=RunContext(),
    )
    tray = _intent(result, "order_first_tray")
    assert (tray.status, [a.media_id for a in tray.assignments]) == ("resolved", ["media-m002"])
