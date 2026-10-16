"""KRI-282 clip picker: question payload, authoritative selections, persistence.

Pins: the additive ``clip_question`` (present only with the flag on), the creator's
tapped clips beating the resolver, empty-by-creator intents never re-asked, selections
re-applied from thread events on later turns, and the dodgeball case (0 candidates)
offering every clip.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverOutput
from app.kria.planner import _clip_intent_resolution_plan
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_selection import (
    ClipSelectionIn,
    fold_clip_selections,
    intent_key,
    latest_open_clip_question,
)


def _clips(n: int = 6) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"m{i}",
            kind="video",
            analysis={"subject": "people playing football", "description": "football"},
            gcs_path=f"users/u/clips/m{i}.mp4",
        )
        for i in range(1, n + 1)
    ]


def _dodgeball() -> ClipIntent:
    return ClipIntent(intent_id="g1", op="group", attribute="dodgeball")


def _no_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        ClipRequestResolverAgent,
        "run",
        lambda self, input, *, ctx=None: ClipRequestResolverOutput(),
    )


# ── question payload ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_candidates_offers_every_clip_as_a_candidate(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    _no_candidates(monkeypatch)
    clips = _clips()
    result = await resolve_clip_intents_for_turn(
        intents=[_dodgeball()],
        creator_request="group dodgeball",
        clips=clips,
        run_context=RunContext(),
    )
    assert result.needs_creator
    q = result.clip_question
    assert q is not None and q["version"] == 1 and q["allow_none"] is True
    (cat,) = q["categories"]
    assert cat["key"] == "group:dodgeball" == intent_key(_dodgeball())
    assert cat["label"] == "dodgeball" and cat["op"] == "group"
    assert cat["candidate_media_ids"] == [c.media_id for c in clips]
    assert cat["suggested_media_ids"] == []
    assert "dodgeball" in result.question and "Tap the clips" in result.question


@pytest.mark.asyncio
async def test_candidates_cap_at_fifty(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    _no_candidates(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[_dodgeball()], creator_request="x", clips=_clips(60), run_context=RunContext()
    )
    assert len(result.clip_question["categories"][0]["candidate_media_ids"]) == 50


@pytest.mark.asyncio
async def test_flag_off_is_text_only_and_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", False)
    _no_candidates(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[_dodgeball()], creator_request="x", clips=_clips(), run_context=RunContext()
    )
    assert result.clip_question is None
    assert result.question == 'I couldn\'t find any clips for "dodgeball". Could you clarify?'
    plan = _clip_intent_resolution_plan(question=result.question, status="needs_creator")
    assert "clip_question" not in plan.model_dump(mode="json")


@pytest.mark.asyncio
async def test_authored_label_stays_a_text_question(monkeypatch) -> None:
    """A per-clip authored label needs names, not membership: no picker."""
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    _no_candidates(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[ClipIntent(intent_id="l1", op="label", attribute="sport being played")],
        creator_request="label the sport",
        clips=_clips(),
        run_context=RunContext(),
    )
    assert result.needs_creator and result.clip_question is None


@pytest.mark.asyncio
async def test_mixed_tappable_and_authored_keeps_picker_plus_text(monkeypatch) -> None:
    """Prod thread c66af8e4: unresolved `dodgeball` group + a per-clip authored label.

    The authored label used to drop the picker for EVERY intent (plain text question).
    The tappable intent must still get its picker; the label stays in the question text.
    """
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    _no_candidates(monkeypatch)
    clips = _clips()
    result = await resolve_clip_intents_for_turn(
        intents=[
            _dodgeball(),
            ClipIntent(intent_id="l1", op="label", attribute="sport being played"),
        ],
        creator_request="group dodgeball and label the sport",
        clips=clips,
        run_context=RunContext(),
    )
    assert result.needs_creator
    q = result.clip_question
    assert q is not None
    (cat,) = q["categories"]
    assert cat["key"] == "group:dodgeball"
    assert cat["candidate_media_ids"] == [c.media_id for c in clips]
    assert "Tap the clips" in result.question
    assert "sport being played" in result.question


def test_plan_carries_the_clip_question_only_when_given() -> None:
    cq = {"version": 1, "question_id": "q", "categories": [], "allow_none": True}
    plan = _clip_intent_resolution_plan(question="Tap.", status="needs_creator", clip_question=cq)
    assert plan.model_dump(mode="json")["clip_question"] == cq


# ── input validation ─────────────────────────────────────────────────────────


def test_selection_schema_is_strict_and_bounded() -> None:
    ok = ClipSelectionIn(question_id="q", answers=[{"key": "k", "media_ids": ["a"]}])
    assert ok.skipped is False and ok.none_keys == []
    with pytest.raises(ValidationError):
        ClipSelectionIn(question_id="q", surprise=1)
    with pytest.raises(ValidationError):
        ClipSelectionIn(
            question_id="q", answers=[{"key": "k", "media_ids": [str(i) for i in range(51)]}]
        )
    with pytest.raises(ValidationError):
        ClipSelectionIn(question_id="q", answers=[{"key": "k", "media_ids": ["a", "a"]}])
    with pytest.raises(ValidationError):
        ClipSelectionIn(
            question_id="q", answers=[{"key": "k", "media_ids": ["a"]}], none_keys=["k"]
        )


# ── folding events ───────────────────────────────────────────────────────────


def _question(qid: str = "q1", keys=("group:dodgeball", "group:football")) -> dict:
    return {
        "version": 1,
        "question_id": qid,
        "allow_none": True,
        "categories": [
            {
                "key": k,
                "label": k,
                "op": "group",
                "candidate_media_ids": ["m1", "m2", "m3"],
                "suggested_media_ids": [],
            }
            for k in keys
        ],
    }


def _events(selection: dict | None, qid: str = "q1"):
    events = [("user", None), ("assistant", {"clip_question": _question(qid)})]
    if selection is not None:
        events.append(("user", {"clip_selection": selection}))
    return events


def test_open_question_closes_once_answered() -> None:
    assert latest_open_clip_question(_events(None))["question_id"] == "q1"
    assert latest_open_clip_question(_events({"question_id": "q1", "answers": []})) is None


def test_fold_answers_none_and_skipped() -> None:
    sel = fold_clip_selections(
        _events(
            {
                "question_id": "q1",
                "answers": [{"key": "group:dodgeball", "media_ids": ["m2", "m3"]}],
                "none_keys": ["group:football"],
                "skipped": False,
            }
        )
    )
    assert sel.by_key["group:dodgeball"].media_ids == ("m2", "m3")
    assert sel.by_key["group:football"].none is True
    skipped = fold_clip_selections(_events({"question_id": "q1", "skipped": True}))
    assert all(e.none for e in skipped.by_key.values()) and len(skipped.by_key) == 2


def test_fold_ignores_unknown_questions_and_keys() -> None:
    sel = fold_clip_selections(
        _events(
            {"question_id": "other", "answers": [{"key": "group:dodgeball", "media_ids": ["m1"]}]}
        )
    )
    assert not sel
    sel = fold_clip_selections(
        _events({"question_id": "q1", "answers": [{"key": "group:zzz", "media_ids": ["m1"]}]})
    )
    assert not sel


def test_match_falls_back_to_token_similarity_across_replans() -> None:
    sel = fold_clip_selections(
        _events({"question_id": "q1", "answers": [{"key": "group:dodgeball", "media_ids": ["m1"]}]})
    )
    replanned = ClipIntent(intent_id="x9", op="include", attribute="the dodgeball")
    assert sel.match(replanned) is not None
    assert sel.match(ClipIntent(intent_id="y", op="group", attribute="pub videos")) is None
    # A label op never borrows a membership selection.
    assert sel.match(ClipIntent(intent_id="z", op="label", attribute="dodgeball")) is None


# ── authoritative override + persistence through the planner ─────────────────


def _wire(monkeypatch, intents: list[PlannedClipIntent]):
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = AsyncMock(return_value=service.IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return resolver


def _planned(attribute: str = "dodgeball", op: str = "group") -> PlannedClipIntent:
    return PlannedClipIntent(intent_id="g1", op=op, attribute=attribute, source_quote=attribute)


async def _plan(selections, intents=None, request="group dodgeball"):
    return await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )


def _selection(**kw):
    payload = {"question_id": "q1", **kw}
    return fold_clip_selections(_events(payload))


@pytest.mark.asyncio
async def test_selection_overrides_the_resolver_deterministically(monkeypatch) -> None:
    resolver = _wire(monkeypatch, [_planned()])
    sel = _selection(answers=[{"key": "group:dodgeball", "media_ids": ["m3", "m1"]}])
    result = await _plan(sel)
    resolver.assert_not_called()  # never reaches the resolver or vision
    (intent,) = result.resolution.intents
    assert intent.status == "resolved" and not result.resolution.needs_creator
    assert [(a.media_id, a.confidence, a.evidence) for a in intent.assignments] == [
        ("m1", 1.0, "creator selected"),
        ("m3", 1.0, "creator selected"),
    ]


@pytest.mark.asyncio
async def test_selection_does_not_touch_other_intents(monkeypatch) -> None:
    other = PlannedClipIntent(intent_id="g2", op="group", attribute="pub", source_quote="pub")
    resolver = _wire(monkeypatch, [_planned(), other])
    sel = _selection(answers=[{"key": "group:dodgeball", "media_ids": ["m1"]}])
    result = await _plan(sel)
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["g2"]
    assert {i.intent_id for i in result.requested_intents} == {"g1", "g2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [
        {"none_keys": ["group:dodgeball"]},
        {"skipped": True},
        {"answers": [{"key": "group:dodgeball", "media_ids": []}]},
    ],
)
async def test_none_or_skipped_never_re_asks(monkeypatch, selection) -> None:
    resolver = _wire(monkeypatch, [_planned()])
    result = await _plan(_selection(**selection))
    resolver.assert_not_called()
    assert result.requested_intents == []  # dropped: empty by the creator's say-so
    assert result.resolution.intents == [] and not result.resolution.needs_creator


@pytest.mark.asyncio
async def test_selection_persists_across_later_turns(monkeypatch) -> None:
    """Later turns re-plan the whole request (new intent ids, reworded) and re-apply it."""
    events = _events(
        {"question_id": "q1", "answers": [{"key": "group:dodgeball", "media_ids": ["m2"]}]}
    )
    events += [("user", None), ("assistant", None), ("user", None)]  # unrelated later turns
    sel = fold_clip_selections(events)
    reworded = PlannedClipIntent(
        intent_id="z7", op="include", attribute="the dodgeball", source_quote="dodgeball"
    )
    resolver = _wire(monkeypatch, [reworded])
    result = await _plan(sel)
    resolver.assert_not_called()
    assert result.resolution.intents[0].media_ids() == ["m2"]


@pytest.mark.asyncio
async def test_selected_clips_that_no_longer_exist_fall_back_to_the_resolver(monkeypatch) -> None:
    resolver = _wire(monkeypatch, [_planned()])
    sel = _selection(answers=[{"key": "group:dodgeball", "media_ids": ["deleted"]}])
    await _plan(sel)
    resolver.assert_called_once()


@pytest.mark.asyncio
async def test_placeholder_label_selection_prints_the_system_placeholder(monkeypatch) -> None:
    placeholder = PlannedClipIntent(
        intent_id="p1",
        op="label",
        attribute="the players",
        placeholder=True,
        source_quote="players",
    )
    resolver = _wire(monkeypatch, [placeholder])
    sel = fold_clip_selections(
        [
            (
                "assistant",
                {"clip_question": _question(keys=("label:the-players",))},
            ),
            (
                "user",
                {
                    "clip_selection": {
                        "question_id": "q1",
                        "answers": [{"key": "label:the-players", "media_ids": ["m1"]}],
                    }
                },
            ),
        ]
    )
    result = await _plan(sel)
    resolver.assert_not_called()
    (a,) = result.resolution.intents[0].assignments
    assert (a.value, a.grounding) == ("Name", "placeholder")


# ── KRI-697: answers stay bound to the intent across reworded rows ───────────
#
# Failure modes this guards (written before the fix):
# 1. the planner rewords `attribute` but keeps `intent_id` -> answer lost, row re-asked;
# 2. a new `intent_id` but the same creator-written text -> answer lost;
# 3. an older exact-key answer beats a newer reworded one -> stale answer re-applied;
# 4. a generic id ("g1", "intent_2") reused for another intent -> wrong answer applied;
# 5. the same creator text under an incompatible op -> answer crosses ops;
# 6. questions persisted before the fix (no identity fields) -> must still fold/match.

_RULE3 = "3 · 20 minutes of darkness"


def _rule3_question(qid: str, label: str) -> dict:
    """Prod thread FEFCCF5C events #14/#16/#18: one caption row, reworded each turn."""
    intent = _rule3(label)
    return {
        "version": 1,
        "question_id": qid,
        "allow_none": True,
        "categories": [
            {
                "key": intent_key(intent),
                "label": label,
                "op": "caption",
                "intent_id": "caption_rule3_text",
                "creator_text": _RULE3,
                "candidate_media_ids": ["m1", "m2", "m3", "m4", "m5"],
                "suggested_media_ids": [],
            }
        ],
    }


def _rule3(attribute: str, intent_id: str = "caption_rule3_text") -> ClipIntent:
    return ClipIntent(intent_id=intent_id, op="caption", attribute=attribute, creator_text=_RULE3)


_TURN1 = f'when I say "{_RULE3}"'
_TURN2 = f'clips when creator says "{_RULE3}"'
_TURN3 = "the clip for rule 3"


def _rule3_thread() -> list:
    k1, k2 = intent_key(_rule3(_TURN1)), intent_key(_rule3(_TURN2))
    return [
        ("assistant", {"clip_question": _rule3_question("q14", _TURN1)}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": "q14",
                    "answers": [{"key": k1, "media_ids": ["m5"]}],
                }
            },
        ),
        ("assistant", {"clip_question": _rule3_question("q16", _TURN2)}),
        ("user", {"clip_selection": {"question_id": "q16", "none_keys": [k2]}}),
    ]


def test_prod_three_turn_none_survives_a_reworded_row() -> None:
    """Tigre N2: "None of these" on turn 2 must still hold when turn 3 says "rule 3"."""
    sel = fold_clip_selections(_rule3_thread())
    entry = sel.match(_rule3(_TURN3))
    assert entry is not None and entry.none is True
    # Same intent under the turn-1 wording: the LATER "none" wins over the older tap.
    assert sel.match(_rule3(_TURN1)).none is True
    settled, remaining, dropped = service.apply_creator_selections(
        [_rule3(_TURN3)], clips=_clips(), selections=sel, creator_request=_RULE3
    )
    assert (settled, remaining, dropped) == ([], [], {"caption_rule3_text"})


def test_creator_text_binds_when_the_intent_id_changes() -> None:
    sel = fold_clip_selections(_rule3_thread()[:2])
    entry = sel.match(_rule3(_TURN3, intent_id="rule_three_caption"))
    assert entry is not None and entry.media_ids == ("m5",)


def test_identity_never_crosses_ops_or_generic_ids() -> None:
    sel = fold_clip_selections(_rule3_thread())
    # Same creator text, incompatible op.
    assert (
        sel.match(
            ClipIntent(
                intent_id="caption_rule3_text", op="label", attribute=_TURN3, creator_text=_RULE3
            )
        )
        is None
    )
    # A generic id reused for a different intent never inherits an answer.
    for generic_id in ["g1", "intent_2", "include_1", "clip_1", "media_2", "slot_3"]:
        question = _question()
        for cat in question["categories"]:
            cat["intent_id"] = generic_id
        generic = fold_clip_selections(
            [
                ("assistant", {"clip_question": question}),
                (
                    "user",
                    {"clip_selection": {"question_id": "q1", "none_keys": ["group:dodgeball"]}},
                ),
            ]
        )
        assert (
            generic.match(ClipIntent(intent_id=generic_id, op="group", attribute="pub videos"))
            is None
        ), generic_id


@pytest.mark.asyncio
async def test_question_categories_carry_the_intent_identity(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    _no_candidates(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[_dodgeball()],
        creator_request="group dodgeball",
        clips=_clips(),
        run_context=RunContext(),
    )
    (cat,) = result.clip_question["categories"]
    assert cat["intent_id"] == "g1" and "creator_text" not in cat
