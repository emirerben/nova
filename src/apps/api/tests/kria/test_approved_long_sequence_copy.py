"""An approved paragraph shown word by word keeps its exact words through brief, plan and text pass.

KRI-1352, second failure: the creator approved a Turkish paragraph for the middle and its English
translation for the bottom, both word by word. The English was longer than the 200-character
brief literal, so it was filed as a description with no literal; the plan seeded no bar for it,
and the text pass could not add a bar and split it in one bundle, so the render failed. Synthetic
wording; provider answers stubbed; brief, planner, composer parse, replay and compile are real.
"""

from __future__ import annotations

import json

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creation_text_composition import (
    CreationTextComposer,
    CreationTextCompositionError,
    compose_creation_text,
)

LOCAL = (
    "Kuruluş Günü yalnızca bir anma günü değil, kurucumuzun mirasını yaşatma günüdür. Bizi "
    "kurtaran ve bu ülkeyi kuran lidere minnet borcumuz vardır. Onun açtığı yolda yürümeye devam."
)
ENGLISH = (
    "Founders Day is not just a day of remembrance, but a day to keep our founder's legacy alive. "
    "We owe a debt of gratitude to the leader who saved us and built this country. We will keep "
    "walking the road he opened."
)


def _sequence(req_id: str, literal: str, position: str) -> BriefRequirement:
    return BriefRequirement(
        id=req_id,
        kind="text_sequence",
        scope="title",
        literal=literal,
        description="word by word",
        facts={"mode": "agent_choice", "unit": "word", "position": position},
    )


def _brief() -> CreativeBrief:
    return CreativeBrief(
        requirements=[_sequence("r1", LOCAL, "middle"), _sequence("r2", ENGLISH, "bottom")]
    )


def _plan(brief: CreativeBrief):
    clips = [
        UnifiedClip(
            media_id=f"archive-{index}",
            proxy_path=f"private/archive-{index}.mp4",
            generation="1",
            duration_s=6,
            analysis={"understanding": {"summary": "Archival footage of a crowd"}},
        )
        for index in range(4)
    ]
    return plan_unified_montage(clips, brief_view(brief))


def test_the_fixture_matches_the_incident_shape() -> None:
    assert len(LOCAL) <= 200 < len(ENGLISH) <= 280


def test_an_approved_sequence_longer_than_200_characters_keeps_its_literal() -> None:
    update = BriefUpdate(
        kind="text_sequence",
        scope="title",
        literal=ENGLISH,
        facts={"mode": "agent_choice", "unit": "word", "position": "bottom"},
    )

    brief = apply_updates(CreativeBrief(), [update], source_turn_id=None)

    assert [req.literal for req in brief.live()] == [ENGLISH]


@pytest.mark.parametrize("model", [BriefUpdate, BriefRequirement])
def test_other_literals_keep_the_200_character_limit(model) -> None:
    fields = {"kind": "text", "scope": "title", "literal": ENGLISH}
    if model is BriefRequirement:
        fields["id"] = "r1"

    with pytest.raises(ValueError, match="200"):
        model(**fields)


def test_a_long_known_sequence_literal_is_refiled_whole() -> None:
    from app.kria.text_sequence import refile_text_sequence

    update = BriefUpdate(
        kind="text",
        scope="title",
        description="the translation, word by word",
        facts={"captions": "word_by_word"},
    )

    refiled = refile_text_sequence(update, creator_message="use it", known_literal=ENGLISH)

    assert refiled.kind == "text_sequence" and refiled.literal == ENGLISH


def _shaper(*, pieces=None, places=None, windows=None, extra=()):
    """A stubbed composer answer: retime, place and split both approved paragraphs.

    The default is the requested shape: both for the whole video, one word per piece, Turkish in
    the middle and English at the bottom.
    """
    places = places or {LOCAL: "middle", ENGLISH: "bottom"}

    def answer(agent, input, *, ctx=None):  # noqa: ARG001
        bars = {bar["text"]: bar for bar in input.variant_snapshot["text_bars"]}
        total = input.variant_snapshot["total_duration_s"]
        spans = windows or {LOCAL: (0, total), ENGLISH: (0, total)}
        ops = [
            {
                "op": "set_texts_timing",
                "selector": {"ids": [bars[text]["id"]]},
                "start_s": spans[text][0],
                "end_s": spans[text][1],
            }
            for text in (LOCAL, ENGLISH)
        ]
        ops += [
            {
                "op": "replace_text_sequence",
                "selector": {"ids": [bars[text]["id"]]},
                "segments": (pieces or str.split)(text),
                "patch": {"position": places[text]},
            }
            for text in (LOCAL, ENGLISH)
        ]
        reply = {"intent": "edit", "confidence": 0.9, "reply": "Shaped.", "ops": [*ops, *extra]}
        return agent.parse(json.dumps(reply), input)

    return answer


_split_both = _shaper()


def _two_chunks(text: str) -> list[str]:
    words = text.split()
    half = len(words) // 2
    return [" ".join(words[:half]), " ".join(words[half:])]


def _pieces(rows: list[dict], text: str) -> list[dict]:
    words = text.split()
    picked = sorted((row for row in rows if row["text"] in words), key=lambda row: row["start_s"])
    return picked


def test_both_approved_paragraphs_compile_as_concurrent_word_sequences(monkeypatch) -> None:
    monkeypatch.setattr(CreationTextComposer, "run", _split_both)
    brief = _brief()
    plan = _plan(brief)

    assert plan.snapshot.opening_title == LOCAL
    assert all(label.text != ENGLISH for label in plan.snapshot.clip_labels or [])
    composed = compose_creation_text(
        plan.snapshot, creator_request="Use this wording", text_requirements=brief.live()
    )

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    local, english = _pieces(rows, LOCAL), _pieces(rows, ENGLISH)
    assert [row["text"] for row in local] == LOCAL.split()
    assert [row["text"] for row in english] == ENGLISH.split()
    assert {row["position"] for row in english} == {"bottom"}
    assert local[0]["start_s"] < english[-1]["end_s"] and english[0]["start_s"] < local[-1]["end_s"]
    assert compose_creation_text(composed, creator_request="same") == composed


def test_the_text_pass_cannot_reword_the_approved_translation(monkeypatch) -> None:
    def reword(agent, input, *, ctx=None):  # noqa: ARG001
        bar = next(b for b in input.variant_snapshot["text_bars"] if b["text"] == ENGLISH)
        reply = {
            "intent": "edit",
            "confidence": 0.9,
            "reply": "Shortened.",
            "ops": [{"op": "rewrite_text", "selector": {"ids": [bar["id"]]}, "text": "Short"}],
        }
        return agent.parse(json.dumps(reply), input)

    monkeypatch.setattr(CreationTextComposer, "run", reword)
    brief = _brief()
    drops = []
    # KRI-1376: the reword is dropped; the approved words render, still word by word.
    composed = compose_creation_text(
        _plan(brief).snapshot,
        creator_request="Use this wording",
        text_requirements=brief.live(),
        drops=drops,
    )
    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert "Short" not in [row["text"] for row in rows]
    assert [row["text"] for row in _pieces(rows, ENGLISH)] == ENGLISH.split()
    assert [(drop.action, drop.op, drop.text) for drop in drops] == [
        ("dropped", "rewrite_text", ENGLISH)
    ]


def _compose(monkeypatch, answer):
    monkeypatch.setattr(CreationTextComposer, "run", answer)
    brief = _brief()
    composed = compose_creation_text(
        _plan(brief).snapshot, creator_request="Use this wording", text_requirements=brief.live()
    )
    return brief, composed


def test_a_saved_program_replays_with_the_same_requirements(monkeypatch) -> None:
    brief, composed = _compose(monkeypatch, _split_both)

    again = compose_creation_text(
        composed, creator_request="Use approved wording", text_requirements=brief.live()
    )

    assert again == composed


def test_asked_only_drops_an_unrequested_line_beside_the_split(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "text_composer_asked_only_enabled", True)
    extra = {"op": "add_text", "text": "UNREQUESTED EXTRA", "start_s": 0, "end_s": 2}

    _, composed = _compose(monkeypatch, _shaper(extra=[extra]))

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert "UNREQUESTED EXTRA" not in {row["text"] for row in rows}
    assert [row["text"] for row in _pieces(rows, ENGLISH)] == ENGLISH.split()


def _words_of(length: int) -> str:
    text = " ".join(["word"] * (length // 5 + 1))
    return text[:length].rstrip()


@pytest.mark.parametrize("quote", ['"{}"', "“{}”", "«{}»"])
def test_a_quoted_paragraph_up_to_280_characters_is_refiled_whole(quote) -> None:
    from app.kria.text_sequence import refile_text_sequence

    words = _words_of(280)
    update = BriefUpdate(
        kind="text", scope="title", description="word by word", facts={"captions": "word_by_word"}
    )

    refiled = refile_text_sequence(update, creator_message="show " + quote.format(words))

    assert refiled.kind == "text_sequence" and refiled.literal == words


def test_a_quote_over_280_characters_is_not_cut_into_a_literal() -> None:
    from app.kria.text_sequence import refile_text_sequence

    update = BriefUpdate(
        kind="text", scope="title", description="word by word", facts={"captions": "word_by_word"}
    )

    refiled = refile_text_sequence(update, creator_message=f'show "{_words_of(290)}"')

    assert refiled == update


def test_other_quote_readers_keep_their_200_character_bound() -> None:
    from app.services.spoken_quotes import quoted_texts

    assert quoted_texts(f'"{_words_of(240)}"') == []
    assert quoted_texts(f'"{_words_of(240)}"', max_chars=280) == [_words_of(240)]


@pytest.mark.parametrize(
    "shape",
    [
        {"pieces": _two_chunks},
        {"places": {LOCAL: "top", ENGLISH: "top"}},
        {"windows": {LOCAL: (0, 12), ENGLISH: (12, 24)}},
    ],
)
def test_the_server_corrects_a_composer_that_shapes_approved_words_wrongly(
    monkeypatch, shape
) -> None:
    """Unit, place and 'together' of approved word sequences are the server's (KRI-1365)."""
    _, composed = _compose(monkeypatch, _shaper(**shape))

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    local, english = _pieces(rows, LOCAL), _pieces(rows, ENGLISH)
    assert [row["text"] for row in local] == LOCAL.split()
    assert [row["text"] for row in english] == ENGLISH.split()
    assert {row["position"] for row in local} == {"middle"}
    assert {row["position"] for row in english} == {"bottom"}
    assert abs(local[0]["start_s"] - english[0]["start_s"]) <= 1 / 30
    assert abs(local[-1]["end_s"] - english[-1]["end_s"]) <= 1 / 30


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ({"pieces": _two_chunks}, "one word at a time"),
        ({"places": {LOCAL: "top", ENGLISH: "top"}}, "moved off its requested"),
        ({"windows": {LOCAL: (0, 12), ENGLISH: (12, 24)}}, "not on screen together"),
    ],
)
def test_a_saved_program_edited_after_composition_fails_on_replay(
    monkeypatch, shape, reason
) -> None:
    from app.agents._model_client import default_client
    from app.schemas.edit_proposal import EditProposalSnapshot

    captured = {}

    def remember(agent, input, *, ctx=None):
        captured["input"] = input
        return _split_both(agent, input, ctx=ctx)

    _, composed = _compose(monkeypatch, remember)
    tampered = _shaper(**shape)(CreationTextComposer(default_client()), captured["input"])
    data = json.loads(composed.model_dump_json())
    data["text_composition"]["then"] = tampered.ops

    with pytest.raises(CreationTextCompositionError, match=reason):
        compile_proposal_execution_plan(EditProposalSnapshot.model_validate(data))
