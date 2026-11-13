"""A retry recovers an approved paragraph a pre-fix brief filed as an over-length description.

KRI-1352, after the 280-character sequence literal shipped: a "Try again" reused the brief written
before it, where the English word sequence had `literal: null` and a description of the form
"<label> (exceeds literal length limit): <paragraph>". The new approval pinned that brief, no bar
held the paragraph, and the render failed again. The planning turn now writes the paragraph back as
the requirement's literal, as a new brief version before approval, only for that exact legacy
shape. Synthetic wording; provider answers stubbed; brief, planner, composer parse, replay and
compile are real.
"""

from __future__ import annotations

import json

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.text_sequence import recover_overflowed_literals
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.services.creation_text_composition import (
    CreationTextComposer,
    compose_creation_text,
)
from tests.kria.test_approved_long_sequence_copy import (
    ENGLISH,
    LOCAL,
    _pieces,
    _plan,
    _split_both,
)

MARKER = "(exceeds literal length limit): "
FACTS = {"mode": "agent_choice", "unit": "word", "position": "bottom"}


def _legacy(description: str, **fields) -> CreativeBrief:
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text_sequence",
                scope="title",
                literal=LOCAL,
                description="one large text in the middle, word by word",
                facts={"mode": "agent_choice", "unit": "word", "position": "middle"},
            ),
            BriefRequirement(
                id="r2",
                kind="text_sequence",
                literal=None,
                description=description,
                facts=dict(FACTS),
                **{"scope": "title", **fields},
            ),
        ],
    )


LEGACY = _legacy("English translation " + MARKER + ENGLISH)


def _recovered(brief: CreativeBrief, updates=()) -> CreativeBrief:
    return apply_updates(
        brief, recover_overflowed_literals(brief, tuple(updates)), source_turn_id=None
    )


def test_the_legacy_paragraph_becomes_the_requirement_literal() -> None:
    recovered = _recovered(LEGACY)

    english = next(req for req in recovered.live() if req.id == "r2")
    assert english.literal == ENGLISH
    assert english.facts == FACTS and english.kind == "text_sequence" and english.scope == "title"
    assert english.description == "English translation"
    assert next(req for req in recovered.live() if req.id == "r1").literal == LOCAL
    (change,) = recover_overflowed_literals(LEGACY, ())
    assert change.operation == "change" and change.expected_version == LEGACY.version


def _words_of(length: int) -> str:
    return (" ".join(["word"] * (length // 5 + 1)))[:length].rstrip()


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("English translation " + MARKER + _words_of(150), id="never-overflowed"),
        pytest.param(("Label " + MARKER + _words_of(300))[:300], id="cut-at-description-limit"),
        pytest.param("English translation " + MARKER + _words_of(230) + "...", id="ellipsis"),
        pytest.param(
            "A " + MARKER + _words_of(110) + " B " + MARKER + _words_of(110), id="two-markers"
        ),
        pytest.param("English translation: " + ENGLISH, id="no-marker"),
        pytest.param(
            "English translation (exceeds literal length limit):" + ENGLISH, id="marker-no-space"
        ),
        pytest.param(
            "English translation (exceeds the literal length limit): " + ENGLISH,
            id="marker-reworded",
        ),
        pytest.param(MARKER + ENGLISH, id="empty-label"),
        pytest.param("English translation " + MARKER + " " + ENGLISH, id="double-space-after"),
        pytest.param("English translation  " + MARKER + ENGLISH, id="double-space-before"),
    ],
)
def test_anything_but_the_exact_legacy_shape_is_left_alone(description: str) -> None:
    brief = _legacy(description)

    assert recover_overflowed_literals(brief, ()) == ()


def test_a_requirement_this_turn_changes_is_left_to_that_change() -> None:
    change = BriefUpdate(
        operation="change",
        target_requirement_id="r2",
        expected_version=2,
        description="English, shorter please",
    )

    assert recover_overflowed_literals(LEGACY, (change,)) == (change,)


@pytest.mark.parametrize("scope", ["title", "global"])
@pytest.mark.parametrize("unit", ["word", "phrase"])
def test_supported_sequence_scopes_and_units_are_recovered(scope: str, unit: str) -> None:
    brief = _legacy("English translation " + MARKER + ENGLISH, scope=scope)
    brief.requirements[1].facts["unit"] = unit

    (change,) = recover_overflowed_literals(brief, ())
    assert change.literal == ENGLISH and change.scope == scope
    assert change.facts["unit"] == unit


@pytest.mark.parametrize("scope", ["clip:archive-1", "per_clip"])
def test_a_clip_scoped_sequence_is_never_recovered(scope: str) -> None:
    words = _words_of(211)
    brief = _legacy("English translation " + MARKER + words, scope=scope)

    assert recover_overflowed_literals(brief, ()) == ()


def test_the_card_names_every_restored_word() -> None:
    from app.kria.text_sequence import recovered_copy_notice

    restored = recover_overflowed_literals(LEGACY, ())

    notice = recovered_copy_notice(LEGACY, restored)

    assert notice == f'Review the restored text for English translation: "{ENGLISH}"'


def test_the_card_names_nothing_the_server_did_not_restore() -> None:
    from app.kria.text_sequence import recovered_copy_notice

    own = BriefUpdate(
        operation="change",
        target_requirement_id="r2",
        expected_version=LEGACY.version,
        kind="text_sequence",
        scope="title",
        literal="Something the model wrote itself.",
        description="English translation",
        facts=dict(FACTS),
    )

    assert recovered_copy_notice(LEGACY, (own,)) is None
    assert recovered_copy_notice(None, recover_overflowed_literals(LEGACY, ())) is None


@pytest.mark.parametrize("status", ["met", "partial", "not_possible", "superseded"])
def test_only_an_open_requirement_is_recovered(status: str) -> None:
    brief = _legacy("English translation " + MARKER + ENGLISH, status=status)

    assert recover_overflowed_literals(brief, ()) == ()


def test_only_word_sequences_are_recovered() -> None:
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r2",
                kind="text",
                scope="title",
                description="Title " + MARKER + ENGLISH,
            )
        ],
    )

    assert recover_overflowed_literals(brief, ()) == ()


def test_no_brief_means_nothing_to_recover() -> None:
    assert recover_overflowed_literals(None, ()) == ()


def _render(monkeypatch, brief: CreativeBrief):
    monkeypatch.setattr(CreationTextComposer, "run", _split_both)
    return compose_creation_text(
        _plan(brief).snapshot, creator_request="Try again", text_requirements=brief.live()
    )


def test_the_recovered_brief_renders_both_paragraphs_word_by_word(monkeypatch) -> None:
    composed = _render(monkeypatch, _recovered(LEGACY))

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert [row["text"] for row in _pieces(rows, ENGLISH)] == ENGLISH.split()
    assert [row["text"] for row in _pieces(rows, LOCAL)] == LOCAL.split()
    assert {row["position"] for row in _pieces(rows, ENGLISH)} == {"bottom"}


def test_the_legacy_brief_still_has_no_bar_for_the_paragraph(monkeypatch) -> None:
    def split_what_exists(agent, input, *, ctx=None):  # noqa: ARG001
        held = {bar["text"] for bar in input.variant_snapshot["text_bars"]}
        reply = {
            "intent": "edit",
            "confidence": 0.9,
            "reply": "Only the Turkish exists.",
            "ops": [],
            "unmet_requests": [
                {"request": "English word by word", "reason": "No text holds the English yet"}
            ]
            if ENGLISH not in held
            else [],
        }
        return agent.parse(json.dumps(reply), input)

    monkeypatch.setattr(CreationTextComposer, "run", split_what_exists)
    drops = []

    # KRI-1376 (prod 3c312af4 / 4e8c925d): the video renders what exists and says what it could
    # not do, instead of failing on the composer's unmet English.
    composed = compose_creation_text(
        _plan(LEGACY).snapshot,
        creator_request="Try again",
        text_requirements=LEGACY.live(),
        drops=drops,
    )
    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert _pieces(rows, ENGLISH) == []
    assert [row["text"] for row in _pieces(rows, LOCAL)] == LOCAL.split()
    assert [(drop.reason, drop.action) for drop in drops] == [("incomplete", "shrunk")]
