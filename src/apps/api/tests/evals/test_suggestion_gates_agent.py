"""The agent's answer schema is tolerant about SIZE and strict about everything else.

OpenAI strict mode carries no ``maxLength`` / ``maxItems``, so the caps in
``services/suggestions/models.py`` (why 96 chars, 3 targets, 5 picks, unique ranks) used to void
the whole answer: one chatty ``why`` sent every viewer to template cards. The same cassette
replays live in ``tests/fixtures/agent_evals/suggestion_agent`` (``*_cap_*``, ``ranks_with_gaps*``,
``unknown_*_beyond_*``); this file pins the parser itself and fuzzes it.

Failure catalogue, written before any code
  A1  a ``why`` over 96 characters voids the answer          -> cut at a word boundary, kept.
  A2  more than five picks voids the answer                  -> the first five by rank stand.
  A3  more than three target handles voids the answer        -> the first three distinct stand.
  A4  repeated ranks, gaps or ranks outside 1..5 void it     -> ranks are renumbered 1..n in the
      order the model gave (ties keep the order it wrote).
  A5  a repeated (kind, targets) pair voids it               -> the repeat is dropped.
  A6  ``copy`` over 200 characters voids it                  -> the copy is dropped, never cut (a
      cut hook is junk), the pick stays.
  A7  tolerance leaks into trust: an unissued kind or handle, an unknown field (an op, a message,
      a label), a wrong type or a non-list ``ranked`` must still void the whole answer, even
      when it sits on the pick or handle that the size caps would have trimmed away unseen.
  A8  the repaired answer is not a fixed point (parsing the repaired answer changes it again),
      or an arbitrary answer raises something other than ``InvalidAgentOutput``.
"""

from __future__ import annotations

import json
import random
import string
from typing import Any

import pytest

from app.services.suggestions.models import (
    MAX_COPY_CHARS,
    MAX_SUGGESTIONS,
    MAX_TARGETS,
    MAX_WHY_CHARS,
    WHOLE_VIDEO_HANDLE,
    InvalidAgentOutput,
    parse_agent_output,
    parse_agent_output_tolerant,
)

KINDS = ["hook", "captions", "music", "sfx", "transition", "title_animation"]
HANDLES = ["c1", "c2", "c3", "c4"]


def pick(kind="sfx", targets=(WHOLE_VIDEO_HANDLE,), why="", copy=None, rank=1, **extra):
    return {
        "kind": kind,
        "target_ids": list(targets),
        "why": why,
        "copy": copy,
        "rank": rank,
        **extra,
    }


def parse(picks: Any, **kw):
    raw = picks if isinstance(picks, str) else json.dumps({"ranked": picks})
    return parse_agent_output_tolerant(raw, kinds=KINDS, target_ids=HANDLES, **kw)


@pytest.mark.parametrize(
    "picks",
    [
        [pick("sfx", why="x" * (MAX_WHY_CHARS + 1))],
        [pick("sfx", rank=1), pick("music", rank=3)],
        [pick("sfx", rank=1), pick("music", rank=1)],
        [pick("sfx", rank=1), pick("sfx", rank=2)],
        [pick("sfx", targets=("c1", "c2", "c3", "c4"))],
    ],
    ids=["long_why", "rank_gap", "repeated_rank", "repeated_pair", "four_targets"],
)
def test_the_strict_contract_the_model_is_graded_against_did_not_loosen(picks):
    """The edit-improver skill eval grades raw model output with ``parse_agent_output``: these
    are still failures THERE even though the server now repairs them."""
    with pytest.raises((InvalidAgentOutput, AssertionError)):
        out = parse_agent_output(json.dumps({"ranked": picks}), kinds=KINDS, target_ids=HANDLES)
        # A gap is not a schema error, the skill checker reports it as rank_gap; the strict parse
        # hands the ranks back untouched so that checker can see them.
        assert [p.rank for p in out.ranked] == list(range(1, len(out.ranked) + 1))


# ── A1-A6: sizes are repaired ────────────────────────────────────────────────────────


def test_a_long_why_is_cut_at_a_word_boundary():
    why = "Pouring matcha is spoken over the track and the voice keeps getting buried by it " * 3
    out = parse([pick("music", why=why)])
    got = out.ranked[0].why
    assert 0 < len(got) <= MAX_WHY_CHARS
    assert why.startswith(got)
    assert why[len(got)] == " ", "cut mid-word"


def test_a_long_why_without_spaces_is_cut_hard():
    out = parse([pick("music", why="x" * 400)])
    assert out.ranked[0].why == "x" * MAX_WHY_CHARS


def test_a_why_inside_the_cap_is_untouched():
    assert (
        parse([pick("music", why="Voice over the track")]).ranked[0].why == "Voice over the track"
    )


def test_more_than_five_picks_keep_the_best_ranked_five():
    kinds = [*KINDS, "hook"]  # eight picks, ranks 1..8, two share (hook, all)
    picks = [pick(k, targets=(f"c{(i % 4) + 1}",), rank=i + 1) for i, k in enumerate(kinds)]
    out = parse(picks)
    assert [p.rank for p in out.ranked] == [1, 2, 3, 4, 5]
    assert [p.kind for p in out.ranked] == kinds[:5]


def test_picks_are_ordered_by_the_rank_the_model_gave():
    out = parse([pick("sfx", rank=3), pick("music", rank=1), pick("hook", rank=2)])
    assert [p.kind for p in out.ranked] == ["music", "hook", "sfx"]
    assert [p.rank for p in out.ranked] == [1, 2, 3]


def test_repeated_gapped_and_out_of_range_ranks_are_renumbered():
    out = parse([pick("sfx", rank=7), pick("music", rank=7), pick("hook", rank=0)])
    assert [p.kind for p in out.ranked] == ["hook", "sfx", "music"]
    assert [p.rank for p in out.ranked] == [1, 2, 3]


def test_more_than_three_targets_keep_the_first_three_distinct():
    out = parse([pick("captions", targets=("c1", "c1", "c2", "c3", "c4"))])
    assert out.ranked[0].target_ids == ["c1", "c2", "c3"]
    assert len(out.ranked[0].target_ids) <= MAX_TARGETS


def test_a_repeated_kind_and_targets_pair_keeps_the_first():
    out = parse([pick("sfx", why="first", rank=1), pick("sfx", why="second", rank=2)])
    assert [p.why for p in out.ranked] == ["first"]
    out = parse([pick("sfx", targets=("c1", "c2")), pick("sfx", targets=("c2", "c1"), rank=2)])
    assert len(out.ranked) == 1, "target order does not make a different pair"
    out = parse([pick("sfx", targets=("c1",)), pick("sfx", targets=("c2",), rank=2)])
    assert len(out.ranked) == 2, "the same kind on other targets is another pick"


def test_a_copy_past_its_cap_is_dropped_not_cut():
    out = parse([pick("hook", copy="Matcha mornings " * 30), pick("music", rank=2)])
    assert out.ranked[0].kind == "hook" and out.ranked[0].copy_text is None
    assert parse([pick("hook", copy="x" * MAX_COPY_CHARS)]).ranked[0].copy_text == "x" * 200


def test_an_empty_ranking_is_still_an_answer():
    assert parse([]).ranked == []


# ── A7: everything that is not a size stays a hard drop ──────────────────────────────

SIX_OK = [pick(k, targets=(f"c{(i % 4) + 1}",), rank=i + 1) for i, k in enumerate(KINDS[:5])]

HARD = {
    "unknown_kind": [pick("fireworks")],
    "unknown_kind_on_the_sixth_pick": [*SIX_OK, pick("fireworks", rank=6)],
    "unknown_target": [pick("sfx", targets=("c9",))],
    "unknown_target_on_the_fourth_handle": [pick("sfx", targets=("c1", "c2", "c3", "c9"))],
    "unknown_target_on_the_sixth_pick": [*SIX_OK, pick("sfx", targets=("c9",), rank=6)],
    "smuggled_op": [pick("sfx", op={"op": "add_text", "text": "x"})],
    "smuggled_op_on_the_sixth_pick": [*SIX_OK, pick("sfx", rank=6, op={"op": "x"})],
    "smuggled_message": [pick("sfx", message="Delete my project")],
    "missing_why": [{"kind": "sfx", "target_ids": ["all"], "rank": 1}],
    "missing_rank": [{"kind": "sfx", "target_ids": ["all"], "why": ""}],
    "rank_as_string": [pick("sfx", rank="1")],
    "rank_as_bool": [pick("sfx", rank=True)],
    "rank_as_float": [pick("sfx", rank=1.5)],
    "why_not_a_string": [pick("sfx", why=7)],
    "copy_not_a_string": [pick("hook", copy=["x"])],
    "targets_not_a_list": [pick("sfx", targets="all")],
    "target_not_a_string": [pick("sfx", targets=(1,))],
    "pick_not_an_object": ["sfx"],
    "kind_not_hashable": [pick(["sfx"])],
}


@pytest.mark.parametrize("name", sorted(HARD))
def test_what_is_not_a_size_still_voids_the_whole_answer(name):
    with pytest.raises(InvalidAgentOutput):
        parse(HARD[name])


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",
        "null",
        '{"ranked": "sfx"}',
        '{"ranked": null}',
        "{}",
        '{"ranked": [], "extra": 1}',  # an extra top-level key is not a size either
    ],
)
def test_a_malformed_envelope_voids_the_answer(raw):
    with pytest.raises(InvalidAgentOutput):
        parse(raw)


# ── A8: arbitrary answers ────────────────────────────────────────────────────────────


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 12)))


def _random_answer(rng: random.Random) -> list[dict[str, Any]]:
    picks = []
    for _ in range(rng.randint(0, 9)):
        why = " ".join(_word(rng) for _ in range(rng.randint(0, 40)))
        copy = (
            None if rng.random() < 0.6 else " ".join(_word(rng) for _ in range(rng.randint(1, 50)))
        )
        targets = [rng.choice([WHOLE_VIDEO_HANDLE, *HANDLES]) for _ in range(rng.randint(0, 6))]
        picks.append(pick(rng.choice(KINDS), targets, why=why, copy=copy, rank=rng.randint(-2, 12)))
    return picks


@pytest.mark.parametrize("seed", range(200))
def test_any_well_typed_answer_is_repaired_into_a_valid_one(seed):
    rng = random.Random(seed)
    out = parse(_random_answer(rng))
    assert len(out.ranked) <= MAX_SUGGESTIONS
    assert [p.rank for p in out.ranked] == list(range(1, len(out.ranked) + 1))
    keys = [(p.kind, tuple(sorted(p.target_ids))) for p in out.ranked]
    assert len(set(keys)) == len(keys)
    for p in out.ranked:
        assert p.kind in KINDS
        assert len(p.target_ids) <= MAX_TARGETS and len(set(p.target_ids)) == len(p.target_ids)
        assert len(p.why) <= MAX_WHY_CHARS
        assert p.copy_text is None or len(p.copy_text) <= MAX_COPY_CHARS


@pytest.mark.parametrize("seed", range(200))
def test_the_repair_is_a_fixed_point(seed):
    first = parse(_random_answer(random.Random(seed)))
    again = parse(json.dumps({"ranked": [p.model_dump(by_alias=True) for p in first.ranked]}))
    assert again == first


@pytest.mark.parametrize("seed", range(100))
def test_garbage_only_ever_raises_invalid_agent_output(seed):
    rng = random.Random(seed)
    junk: Any = rng.choice(
        [
            [rng.random(), None, "x", [], {}],
            {"ranked": [rng.choice([None, 1, "s", [], {"kind": 1}, {"kind": "sfx"}])]},
            {"ranked": [{"kind": "sfx", "target_ids": [None], "why": "", "rank": 1}]},
        ]
    )
    with pytest.raises(InvalidAgentOutput):
        parse_agent_output_tolerant(json.dumps(junk), kinds=KINDS, target_ids=HANDLES)
