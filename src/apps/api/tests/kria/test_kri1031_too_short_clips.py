"""KRI-1031: "Skip anything too short to use" is asked with a clip picker, not measured.

Prod thread A228E85B (Plaza de Mayo kit M1, "maximo 3", turn a3c3982c, 2026-10-28 16:17Z):
clips of 14.3, 14.0, 11.6, 14.5 and 23.4 s plus a 0.97 s square shot (J1) and a 0.2 s
mis-tap (J2). The resolver read "anything too short to use" as a question about what the
clips show (``exclude_short_clips:n=0``) and the creator was asked to tap the short clips,
with the 11.6 s bus clip and the 14.3 s palms among the candidates, although the server
knew every length. Now the clip-intent planner marks a length-only skip ``by_length`` and
``app/services/too_short_clips.py`` settles it from durations.

Ways this could go wrong, written down before the code:

1. A length ask ("too short to use", "under a second", "the mis-taps") is asked with the
   picker although every length is known (the incident).
2. The length rule drops footage the creator wanted: an ordinary clip, a whole set of short
   quick-cut clips, a photo or a Visuals clip.
3. A clip of unknown length is judged anyway (it is left to the resolver, as before).
4. The creator's own number ("under 2 seconds") is ignored, or the default rule replaces it.
5. A tap answer for the same ask is overridden by the length rule.
6. No clip is too short: the picker or the "I couldn't tell which clips" notice instead of
   saying plainly that every clip is long enough.
7. The draft never names the clips it left out by length.
8. A measured exclusion linked to the brief's `select` ask is never judged against the
   finished timeline (only taps counted as proof).
9. A content ask ("the blurry ones") is measured, or `by_length` rides another op.
10. Flag off (CLIP_INTENT_EXCLUDE_ENABLED): the planner prompt or the parse change.
11. A strategy persisted before the new fields re-dumps differently.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents._skills import load_skill
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.select_asks import exclusion_facts
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import LENGTH_EVIDENCE, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import ClipSelections, SelectionEntry
from app.services.too_short_clips import resolve_too_short, too_short_clips
from tests.kria.test_kri1026_plaza_exclusions import (
    BUS,
    CASE,
    CLIPS,
    FILMING_ORDER,
    J1,
    J2,
    JUNK,
    REAL,
    _contract,
    _draft,
    _exclude,
    _model_strategy,
    _played,
    _turn,
)
from tests.kria.test_kri1026_plaza_exclusions import _intent_clips as _recorded_clips

SKIP_SHORT = "Skip anything too short to use"
LEFT_OUT_JUNK = "I left out clips 2 and 4 because they are too short to use."
COULDNT_TELL = "I couldn't tell which clips you asked me to leave out"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _intent_clips(clips: list[dict[str, Any]] = CLIPS) -> list[IntentClip]:
    """The turn's clips with their recorded lengths (``clip_assignments[*].duration_s``)."""
    return [
        replace(clip, duration_s=row["duration_s"])
        for clip, row in zip(_recorded_clips(clips), clips, strict=True)
    ]


# -- the length rule (1, 2, 3, 4) ---------------------------------------------


def _lengths(*seconds: float | None, kind: str = "video") -> list[IntentClip]:
    return [
        IntentClip(media_id=f"clip-{i}", kind=kind, analysis=None, duration_s=s)
        for i, s in enumerate(seconds, start=1)
    ]


def test_the_plaza_clips_too_short_to_use_are_the_two_junk_clips() -> None:
    # 1: median 14.0 s; 0.97 s and 0.2 s are under 1.5 s, the 11.6 s bus clip is not.
    assert too_short_clips(_intent_clips()) == [J1, J2]


@pytest.mark.parametrize(
    ("seconds", "short"),
    [
        # 2: a set of quick clips: none is far shorter than the others.
        ((1.0, 1.1, 1.2, 0.9), []),
        # 2: a 2 s clip among long takes is short, not too short to use.
        ((12.0, 14.0, 2.0, 15.0), []),
        # A 1.4 s clip among long takes is.
        ((12.0, 14.0, 1.4, 15.0), ["clip-3"]),
        # A mis-tap among quick clips is far below their median.
        ((1.2, 1.3, 0.2, 1.1), ["clip-3"]),
        # Two clips: the long one sets the median.
        ((10.0, 0.5), ["clip-2"]),
    ],
)
def test_the_rule_is_an_absolute_floor_and_far_below_the_median(
    seconds: tuple[float, ...], short: list[str]
) -> None:
    assert too_short_clips(_lengths(*seconds)) == short


def test_the_creators_own_number_replaces_the_rule() -> None:
    # 4: "skip anything under 2 seconds": exactly that, whatever the median.
    assert too_short_clips(_lengths(10.0, 1.9, 2.0, 0.5), under_s=2.0) == ["clip-2", "clip-4"]
    assert too_short_clips(_lengths(1.0, 1.1, 1.2, 0.9), under_s=1.0) == ["clip-4"]


def test_only_own_videos_with_known_lengths_are_judged() -> None:
    # 3: one unknown length: nothing is decided here (the resolver gets the ask).
    assert too_short_clips(_lengths(10.0, None, 0.3)) is None
    # 2: a photo has no length to be short; a Visuals clip is not a take.
    photo = IntentClip(media_id="photo-1", kind="image", analysis=None)
    visual = IntentClip(media_id="asset-visual", kind="video", analysis=None, duration_s=0.2)
    assert too_short_clips([*_lengths(10.0, 12.0, 0.3), photo, visual]) == ["clip-3"]
    assert too_short_clips([photo]) is None


def test_a_measured_exclusion_is_resolved_with_its_own_evidence() -> None:
    ask = ClipIntent(
        intent_id="exclude_short_clips",
        op="exclude",
        attribute="anything too short to use",
        by_length="too_short",
    )
    resolved = resolve_too_short(ask, _intent_clips())
    assert resolved is not None and resolved.status == "resolved"
    assert [(a.media_id, a.evidence, a.confidence) for a in resolved.assignments] == [
        (J1, LENGTH_EVIDENCE, 1.0),
        (J2, LENGTH_EVIDENCE, 1.0),
    ]
    # 6: nothing too short is a resolved answer with no clips, not a question.
    long_only = [c for c in _intent_clips() if c.media_id not in JUNK]
    nothing = resolve_too_short(ask, long_only)
    assert nothing is not None and nothing.status == "resolved" and nothing.assignments == []
    # A content ask is never measured.
    blurry = ClipIntent(intent_id="skip_blurry", op="exclude", attribute="the blurry ones")
    assert resolve_too_short(blurry, _intent_clips()) is None


# -- the planner records the length ask (9, 10) -------------------------------

_PLANNER_INTENTS: list[dict[str, Any]] = [
    {
        "intent_id": "order_filmed_pm",
        "op": "order",
        "order_by": "capture_time",
        "attribute": "our afternoon at Plaza de Mayo",
        "source_quote": "Montage of our afternoon at Plaza de Mayo, in the order we filmed it",
    },
    {
        "intent_id": "exclude_short_clips",
        "op": "exclude",
        "attribute": "anything too short to use",
        "by_length": "too_short",
        "under_s": None,
        "source_quote": SKIP_SHORT,
    },
    {
        "intent_id": "label_casarosada",
        "op": "label",
        "attribute": "the pink building",
        "creator_text": "Casa Rosada",
        "source_quote": 'label "Casa Rosada" on the pink building',
    },
    {
        "intent_id": "label_catedral",
        "op": "label",
        "attribute": "the columns",
        "creator_text": "Catedral",
        "source_quote": '"Catedral" on the columns',
    },
]
_OBELISCO = {
    "intent_id": "order_last_obelisco",
    "op": "order",
    "position": "last",
    "attribute": "the Obelisco",
    "source_quote": "End on the Obelisco",
}
_CATHEDRAL_LAST = {
    "intent_id": "order_last_cathedral",
    "op": "order",
    "position": "last",
    "attribute": "the Cathedral",
    "source_quote": "End on the Cathedral",
}
RESEND = CASE["prompt"].replace("End on the Obelisco.", "End on the Cathedral.")
# The first turn's strategy: the title ask is still the creator's flag title (KRI-1024), so the
# title-turn strategy's "Plaza de Mayo" would be a wording question of its own.
_FIRST_TURN = _model_strategy(opening_title=None)


def _parse(row: dict[str, Any], *, exclusions: bool = True) -> list[Any]:
    raw = json.dumps({"intents": [row], "question": None}, ensure_ascii=False)
    output = ClipIntentPlannerAgent(None).parse(  # type: ignore[arg-type]
        raw,
        ClipIntentPlannerInput(
            creator_request=CASE["prompt"],
            latest_user_message=CASE["prompt"],
            clip_facts=True,
            clip_exclusions=exclusions,
        ),
    )
    return output.intents


def test_the_planner_prompt_teaches_by_length_only_with_exclusions_on() -> None:
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]

    def prompt(exclusions: bool) -> str:
        return agent.render_prompt(
            ClipIntentPlannerInput(creator_request=CASE["prompt"], clip_exclusions=exclusions)
        )

    assert '"by_length":"too_short|null","under_s":null' in prompt(True)
    assert "`by_length`" in prompt(True)
    assert "by_length" not in prompt(False) and "under_s" not in prompt(False)  # 10


def test_the_planner_keeps_by_length_on_an_exclude_only() -> None:
    [skip] = _parse(_PLANNER_INTENTS[1])
    assert (skip.op, skip.by_length, skip.under_s) == ("exclude", "too_short", None)
    # Flash copies the template: the string "null", or an invented value, means no mark.
    for value in ("null", "short", 3):
        [row] = _parse({**_PLANNER_INTENTS[1], "by_length": value})
        assert row.by_length is None
    # 4: the creator's number of seconds is a length ask even when the mark was left off.
    [row] = _parse({**_PLANNER_INTENTS[1], "by_length": None, "under_s": "1"})
    assert (row.by_length, row.under_s) == ("too_short", 1.0)
    [row] = _parse({**_PLANNER_INTENTS[1], "under_s": -2})
    assert (row.by_length, row.under_s) == ("too_short", None)
    # Never on another op.
    [order] = _parse({**_PLANNER_INTENTS[0], "by_length": "too_short", "under_s": 1})
    assert (order.by_length, order.under_s) == (None, None)
    # 10: exclusions off: no exclude at all (KRI-667, unchanged).
    assert _parse(_PLANNER_INTENTS[1], exclusions=False) == []


# -- the turn, through the real planner and resolver agents (1, 5, 6, 7) -----

_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


class _Provider:
    """The model provider, answering as it did on turn a3c3982c (16:17:30Z).

    Only the transport is a stand-in: the real clip-intent planner and resolver agents render
    their prompts, call it, and parse its raw text. The planner now marks the skip
    ``by_length`` (what the v2026-10-28 prompt asks for); the resolver matched nothing for
    "anything too short to use" (``exclude_short_clips:n=0``) and nothing for the Obelisco.
    """

    def __init__(self, *, ending: dict[str, Any]) -> None:
        self.ending = ending
        self.calls: list[str] = []
        self.resolver_intents: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            intents = [*_PLANNER_INTENTS, self.ending]
            return ModelInvocation(raw_text=json.dumps({"intents": intents, "question": None}))
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            asked = re.findall(r"^- intent_id=(\S+) ", prompt, re.M)
            self.resolver_intents.extend(asked)

            def alias(record_words: str) -> str:
                [found] = re.findall(rf"^- (m\d{{3}}) \(video\): .*{record_words}", prompt, re.M)
                return found

            answers = {
                "label_casarosada": [
                    {"media": alias("pink building"), "value": "Casa Rosada", "confidence": 0.9}
                ],
                "label_catedral": [
                    {"media": alias("columned building"), "value": "Catedral", "confidence": 0.9}
                ],
                "order_last_cathedral": [{"media": alias("columned building"), "confidence": 0.95}],
            }
            rows = [
                {"intent_id": i, "assignments": answers.get(i, []), "needs_vision": []}
                for i in asked
            ]
            return ModelInvocation(raw_text=json.dumps({"intents": rows}))
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _provider(monkeypatch: pytest.MonkeyPatch, ending: dict[str, Any]) -> _Provider:
    provider = _Provider(ending=ending)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


async def test_incident_too_short_is_measured_never_asked(prod_profile, monkeypatch) -> None:
    # 1: the prod turn. Only "End on the Obelisco" (no clip shows it) is asked.
    provider = _provider(monkeypatch, _OBELISCO)
    planned = await _turn(
        monkeypatch, message=CASE["prompt"], strategy=_FIRST_TURN, intent_clips=_intent_clips()
    )

    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    categories = planned.plan.clip_question["categories"]
    assert [c["op"] for c in categories] == ["order"]
    assert "exclude_short_clips" not in provider.resolver_intents
    assert "too short" not in planned.plan.response


async def test_a_resend_drafts_without_the_junk_and_names_it(prod_profile, monkeypatch) -> None:
    # 1, 7: "End on the Cathedral" instead: a draft, the junk measured out and named.
    provider = _provider(monkeypatch, _CATHEDRAL_LAST)
    strategy, reply = _draft(
        await _turn(monkeypatch, message=RESEND, strategy=_FIRST_TURN, intent_clips=_intent_clips())
    )

    assert provider.calls == ["planner", "resolver"]
    assert "exclude_short_clips" not in provider.resolver_intents
    assert set(_played(strategy)) == set(REAL)
    assert _contract(strategy).order_ids == FILMING_ORDER
    assert LEFT_OUT_JUNK in reply
    assert COULDNT_TELL not in reply and "couldn't leave out" not in reply
    skip = next(i for i in strategy["resolved_clip_intents"] if i["op"] == "exclude")
    assert skip["by_length"] == "too_short"
    assert {a["evidence"] for a in skip["assignments"]} == {LENGTH_EVIDENCE}


async def test_no_clip_too_short_says_so_and_keeps_every_clip(prod_profile, monkeypatch) -> None:
    # 6: the five real clips only: no picker, no "I couldn't tell", every clip stays.
    _provider(monkeypatch, _CATHEDRAL_LAST)
    real = [c for c in CLIPS if c["media_id"] not in JUNK]
    planned = await _turn(
        monkeypatch,
        message=RESEND,
        strategy=_FIRST_TURN,
        clips=real,
        intent_clips=_intent_clips(real),
    )
    strategy, reply = _draft(planned)
    assert set(_played(strategy, REAL)) == set(REAL)
    assert "None of your clips is too short to use." in reply
    assert COULDNT_TELL not in reply


async def test_no_short_clips_notice_does_not_claim_another_exclusion_was_kept(
    prod_profile, monkeypatch
) -> None:
    real = [c for c in CLIPS if c["media_id"] not in JUNK]
    clips = _intent_clips(real)
    measured = resolve_too_short(
        ClipIntent(
            intent_id="exclude_short_clips",
            op="exclude",
            attribute="anything too short to use",
            by_length="too_short",
        ),
        clips,
    )
    assert measured is not None and not measured.assignments
    bus_exclusion = _exclude(BUS).model_copy(
        update={"intent_id": "exclude_bus", "attribute": "the bus clip"}
    )
    planned = await _turn(
        monkeypatch,
        message="Skip anything too short to use and leave out the bus clip.",
        strategy=_FIRST_TURN,
        clips=real,
        intent_clips=clips,
        resolved=[measured, bus_exclusion],
    )
    strategy, reply = _draft(planned)
    assert set(_played(strategy, REAL)) == set(REAL) - {BUS}
    assert "None of your clips is too short to use." in reply
    assert "kept them all" not in reply


async def test_a_tap_answer_beats_the_length_rule(monkeypatch) -> None:
    # 5: the creator already tapped only the 0.2 s clip for this ask: that answer stands.
    _provider(monkeypatch, _CATHEDRAL_LAST)
    selections = ClipSelections(
        by_key={"exclude:anything-too-short-to-use": SelectionEntry(media_ids=(J2,))}
    )
    planned = await plan_and_resolve_clip_intents(
        creator_request=RESEND,
        latest_user_message=RESEND,
        candidate_intents=None,
        clips=_intent_clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )
    skip = next(i for i in planned.resolution.intents if i.op == "exclude")
    assert [(a.media_id, a.evidence) for a in skip.assignments] == [(J2, "creator selected")]


async def test_an_unknown_length_goes_to_the_resolver_as_before(monkeypatch) -> None:
    # 3
    provider = _provider(monkeypatch, _CATHEDRAL_LAST)
    clips = _intent_clips()
    clips[1] = IntentClip(**{**clips[1].__dict__, "duration_s": None})
    await plan_and_resolve_clip_intents(
        creator_request=RESEND,
        latest_user_message=RESEND,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
    )
    assert "exclude_short_clips" in provider.resolver_intents


# -- the finished video's receipt (8) and persisted strategies (11) ---------------------


def test_a_measured_exclusion_linked_to_a_select_ask_is_judged() -> None:
    brief = CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [
                {"id": "r3", "kind": "select", "scope": "global", "description": SKIP_SHORT}
            ],
        }
    )
    measured = _exclude(J1, J2, evidence=LENGTH_EVIDENCE).model_copy(
        update={"by_length": "too_short", "requirement_id": "r3"}
    )
    strategy = {"resolved_clip_intents": [measured.model_dump(mode="json")]}
    [fact] = exclusion_facts(strategy, brief)
    assert (fact.requirement_id, set(fact.media_ids), fact.complete) == ("r3", JUNK, True)


@pytest.mark.parametrize("turn", ["strategy_t3", "strategy_t5"])
def test_recorded_intents_re_dump_byte_identically(turn: str) -> None:
    # 11
    for row in CASE[turn]["resolved_clip_intents"]:
        dumped = ResolvedClipIntent.model_validate(row).model_dump(mode="json", exclude_none=True)
        assert dumped == row
