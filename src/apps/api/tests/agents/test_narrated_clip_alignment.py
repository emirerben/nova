"""Strict parse contract for the narrated clip-alignment agent (KRI-456)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.narrated_clip_alignment import (
    NarratedClipAlignmentAgent,
    NarratedClipAlignmentInput,
    _order_mode_text,
)


def _input(*, order_locked: bool = True) -> NarratedClipAlignmentInput:
    return NarratedClipAlignmentInput(
        words=[
            {"word_id": f"w{i:06d}", "text": text, "start_s": float(i), "end_s": i + 0.9}
            for i, text in enumerate(["Boil", "the", "pasta.", "Now", "grate", "cheese.", "Eat."])
        ],
        clips=[
            {"clip_id": "a", "description": "pasta in boiling water", "creator_label": "boiling"},
            {"clip_id": "b", "description": "hand grating cheese"},
            {"clip_id": "c", "description": "person eating"},
        ],
        creator_request="eating at the very end",
        order_locked=order_locked,
        language="en",
    )


def _raw(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {
            "placements": [
                {"clip_id": clip_id, "start_word_id": word_id, "reason": "r"}
                for clip_id, word_id in pairs
            ]
        }
    )


def _parse(raw: str, **kwargs):
    return NarratedClipAlignmentAgent(None).parse(raw, _input(**kwargs))


def test_valid_placements_round_trip() -> None:
    output = _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000006")))

    assert [(p.clip_id, p.start_word_id) for p in output.placements] == [
        ("a", "w000000"),
        ("b", "w000003"),
        ("c", "w000006"),
    ]
    assert output.placements[0].reason == "r"


def test_first_clip_may_start_after_the_first_word_when_order_is_free() -> None:
    # The worker pins the first step to 0.0 so the intro stays with the first clip.
    output = _parse(_raw(("b", "w000001"), ("a", "w000003"), ("c", "w000006")), order_locked=False)
    assert output.placements[0].start_word_id == "w000001"


def test_locked_order_pins_the_first_clip_to_the_first_word() -> None:
    # A model that put the first clip's subject later (its words are not increasing in
    # list order) must still parse: placement 0 is the first word, its reason is kept.
    output = _parse(_raw(("a", "w000004"), ("b", "w000002"), ("c", "w000006")))

    assert [(p.clip_id, p.start_word_id) for p in output.placements] == [
        ("a", "w000000"),
        ("b", "w000002"),
        ("c", "w000006"),
    ]
    assert output.placements[0].reason == "r"


def test_locked_order_still_rejects_other_non_increasing_starts() -> None:
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000004"), ("b", "w000005"), ("c", "w000002")))
    # Pinning the first clip to w000000 does not excuse a second clip on word 0.
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000004"), ("b", "w000000"), ("c", "w000006")))
    with pytest.raises(SchemaError, match="unknown start word"):
        _parse(_raw(("a", "w000099"), ("b", "w000002"), ("c", "w000006")))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"placements": "nope"}),
        json.dumps({"placements": [1, 2, 3]}),
    ],
)
def test_malformed_payloads_are_rejected(raw: str) -> None:
    with pytest.raises(SchemaError):
        _parse(raw)


def test_unknown_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="unknown clip"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("invented", "w000006")))


def test_unknown_word_is_rejected() -> None:
    with pytest.raises(SchemaError, match="unknown start word"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000099")))


def test_duplicate_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="duplicate clip"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("b", "w000006")))


def test_missing_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="expected 3 placements"):
        _parse(_raw(("a", "w000000"), ("b", "w000003")))


def test_non_increasing_words_are_rejected() -> None:
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000003")))
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000000"), ("b", "w000005"), ("c", "w000002")))


def test_reorder_is_rejected_when_order_is_locked() -> None:
    reordered = _raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006"))
    with pytest.raises(SchemaError, match="order is locked"):
        _parse(reordered, order_locked=True)


def test_reorder_is_accepted_when_order_is_free() -> None:
    output = _parse(_raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006")), order_locked=False)
    assert [p.clip_id for p in output.placements] == ["b", "a", "c"]


def test_prompt_states_the_order_mode_and_carries_labels() -> None:
    agent = NarratedClipAlignmentAgent(None)
    locked = agent.render_prompt(_input(order_locked=True))
    free = agent.render_prompt(_input(order_locked=False))

    assert "CLIP ORDER: LOCKED" in locked
    assert "CLIP ORDER: FREE" in free
    assert '"creator_label": "boiling"' in locked
    assert "w000006" in locked
    assert "first listed clip always starts at the very first word" in locked


# --- PINNED mode (KRI-532) --------------------------------------------------


def _pinned_input(**pins) -> NarratedClipAlignmentInput:
    base = _input(order_locked=False)
    return base.model_copy(update=pins)


def _pinned_parse(raw: str, **pins):
    return NarratedClipAlignmentAgent(None).parse(raw, _pinned_input(**pins))


def test_pinned_last_accepts_a_free_middle_and_the_pinned_tail() -> None:
    # c is pinned last; a and b are free, so the model may put b before a.
    output = _pinned_parse(
        _raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006")), pinned_last=["c"]
    )
    assert [p.clip_id for p in output.placements] == ["b", "a", "c"]


def test_pinned_first_must_open_the_video() -> None:
    ok = _pinned_parse(
        _raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006")), pinned_first=["b"]
    )
    assert ok.placements[0].clip_id == "b"
    with pytest.raises(SchemaError, match="pinned opening clips"):
        _pinned_parse(
            _raw(("a", "w000000"), ("b", "w000003"), ("c", "w000006")), pinned_first=["b"]
        )


def test_pinned_last_rejects_a_wrong_tail() -> None:
    with pytest.raises(SchemaError, match="pinned closing clips"):
        _pinned_parse(_raw(("a", "w000000"), ("c", "w000003"), ("b", "w000006")), pinned_last=["c"])


def test_pinned_head_and_tail_keep_their_stated_order() -> None:
    input_ = NarratedClipAlignmentInput(
        words=_input().words,
        clips=[{"clip_id": cid} for cid in "abcd"],
        pinned_first=["a", "b"],
        pinned_last=["d"],
    )
    agent = NarratedClipAlignmentAgent(None)
    assert [
        p.clip_id
        for p in agent.parse(
            _raw(("a", "w000000"), ("b", "w000002"), ("c", "w000004"), ("d", "w000006")), input_
        ).placements
    ] == ["a", "b", "c", "d"]
    with pytest.raises(SchemaError, match="pinned opening clips"):
        agent.parse(
            _raw(("b", "w000000"), ("a", "w000002"), ("c", "w000004"), ("d", "w000006")), input_
        )


def test_pinned_mode_still_requires_increasing_starts_and_pins_the_first_word() -> None:
    with pytest.raises(SchemaError, match="strictly increasing"):
        _pinned_parse(_raw(("a", "w000000"), ("b", "w000004"), ("c", "w000004")), pinned_last=["c"])
    # The opening clip owns the first word even when the model started it later.
    output = _pinned_parse(
        _raw(("b", "w000002"), ("a", "w000003"), ("c", "w000006")), pinned_last=["c"]
    )
    assert output.placements[0].start_word_id == "w000000"


def test_unknown_or_duplicate_pins_are_rejected_at_input_time() -> None:
    with pytest.raises(ValueError, match="not in clips"):
        NarratedClipAlignmentInput.model_validate(
            {**_input().model_dump(), "pinned_last": ["ghost"]}
        )
    with pytest.raises(ValueError, match="only once"):
        NarratedClipAlignmentInput.model_validate(
            {**_input().model_dump(), "pinned_first": ["a"], "pinned_last": ["a"]}
        )


def test_locked_wins_over_pins() -> None:
    reordered = _raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006"))
    with pytest.raises(SchemaError, match="order is locked"):
        NarratedClipAlignmentAgent(None).parse(
            reordered,
            NarratedClipAlignmentInput.model_validate(
                {**_input(order_locked=True).model_dump(), "pinned_last": ["c"]}
            ),
        )


def test_prompt_states_the_pins() -> None:
    agent = NarratedClipAlignmentAgent(None)
    prompt = agent.render_prompt(_pinned_input(pinned_last=["a"]))
    assert "CLIP ORDER: PINNED" in prompt
    assert "`a` (boiling) must be the LAST clip" in prompt
    assert "CLIP ORDER: FREE" not in prompt
    first = agent.render_prompt(_pinned_input(pinned_first=["b"]))
    assert "`b` must be the FIRST clip" in first
    assert NarratedClipAlignmentAgent.spec.prompt_version == "2026-10-08.1"


# --- Cappadocia regression: prod job 38caaebd -------------------------------

_FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures/agent_evals/narrated_clip_alignment/golden/cappadocia_pinned_last.json"
)


def _cappadocia():
    data = json.loads(_FIXTURE.read_text())
    return data, NarratedClipAlignmentInput.model_validate(data["input"])


def test_cappadocia_prod_answer_was_rejected_under_the_old_locked_mode() -> None:
    data, input_ = _cappadocia()
    locked = input_.model_copy(
        update={"order_locked": True, "pinned_last": [], "ordered_groups": []}
    )
    # The raw prod answer lists clips in the locked input order but places them on the
    # words that describe them (non-monotonic): the bug that failed the job.
    with pytest.raises(SchemaError, match="strictly increasing"):
        NarratedClipAlignmentAgent(None).parse(data["meta"]["prod_unsorted_raw_text"], locked)


def _cappadocia_steps(output, input_):
    from app.pipeline.narrated_alignment import resolve_aligned_steps

    start = {w["word_id"]: float(w["start_s"]) for w in input_.words}
    order = [p.clip_id for p in output.placements]
    starts = [start[p.start_word_id] for p in output.placements]
    return resolve_aligned_steps(order, starts, 25.504)


def test_cappadocia_sorted_answer_is_accepted_in_pinned_mode() -> None:
    data, input_ = _cappadocia()
    assert input_.pinned_last == ["files/l3b7i9r61r8s"] and not input_.order_locked
    output = NarratedClipAlignmentAgent(None).parse(data["raw_text"], input_)

    assert output.resorted is False
    steps = _cappadocia_steps(output, input_)
    assert steps is not None and len(steps) == 6
    assert steps[-1][0] == "files/l3b7i9r61r8s"


def test_cappadocia_raw_unsorted_prod_answer_is_resorted_and_accepted() -> None:
    data, input_ = _cappadocia()
    output = NarratedClipAlignmentAgent(None).parse(data["meta"]["prod_unsorted_raw_text"], input_)

    assert output.resorted is True
    assert [p.clip_id for p in output.placements] == [
        p["clip_id"] for p in data["output"]["placements"]
    ]
    steps = _cappadocia_steps(output, input_)
    assert steps is not None and len(steps) == 6
    assert steps[-1][0] == "files/l3b7i9r61r8s"


def test_cappadocia_order_mode_text() -> None:
    _data, input_ = _cappadocia()
    assert _order_mode_text(input_) == (
        "PINNED: the creator fixed only the clips named here. "
        "`files/l3b7i9r61r8s` (the sunset valley) must be the LAST clip. "
        "Every other clip is free: place it where the narration describes it and list the "
        "placements in on-screen order."
    )


def test_resort_is_pinned_mode_only_and_still_checks_the_pins() -> None:
    # Free mode keeps requiring the listed order (no re-sort).
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000000"), ("b", "w000005"), ("c", "w000002")), order_locked=False)
    # A re-sorted answer that breaks the pinned tail is still rejected.
    with pytest.raises(SchemaError, match="pinned closing clips"):
        _pinned_parse(_raw(("c", "w000001"), ("a", "w000003"), ("b", "w000006")), pinned_last=["c"])
    # Ties keep the listed order and are then rejected as non-increasing.
    with pytest.raises(SchemaError, match="strictly increasing"):
        _pinned_parse(_raw(("a", "w000000"), ("b", "w000004"), ("c", "w000004")), pinned_last=["c"])


def _grouped_input(groups, **extra) -> NarratedClipAlignmentInput:
    return NarratedClipAlignmentInput(
        words=_input().words,
        clips=[{"clip_id": cid, "creator_label": f"the {cid}"} for cid in "abcd"],
        ordered_groups=groups,
        **extra,
    )


def test_ordered_groups_require_each_group_before_the_next() -> None:
    agent = NarratedClipAlignmentAgent(None)
    input_ = _grouped_input([["a", "b"], ["c"]])  # d is in no group: free
    ok = agent.parse(
        _raw(("d", "w000000"), ("b", "w000002"), ("a", "w000003"), ("c", "w000006")), input_
    )
    assert [p.clip_id for p in ok.placements] == ["d", "b", "a", "c"]
    # Members of one group need not be contiguous: d may sit between a and b.
    agent.parse(
        _raw(("a", "w000000"), ("d", "w000002"), ("b", "w000003"), ("c", "w000006")), input_
    )
    with pytest.raises(SchemaError, match="must all come before"):
        agent.parse(
            _raw(("a", "w000000"), ("c", "w000002"), ("b", "w000003"), ("d", "w000006")), input_
        )


def test_ordered_groups_are_resorted_then_checked() -> None:
    agent = NarratedClipAlignmentAgent(None)
    input_ = _grouped_input([["a"], ["b"], ["c"]])
    # Listed in input order, but the start words follow the creator's sequence.
    output = agent.parse(
        _raw(("a", "w000000"), ("c", "w000006"), ("b", "w000003"), ("d", "w000004")), input_
    )
    assert output.resorted is True
    assert [p.clip_id for p in output.placements] == ["a", "b", "d", "c"]


def test_prompt_names_the_described_sequence() -> None:
    prompt = NarratedClipAlignmentAgent(None).render_prompt(
        _grouped_input([["a"], ["b", "c"]], pinned_last=["d"])
    )
    assert "CLIP ORDER: PINNED" in prompt
    assert "`d` (the d) must be the LAST clip" in prompt
    assert "`a` (the a), then `b` (the b) and `c` (the c)" in prompt


def test_invalid_groups_are_rejected_at_input_time() -> None:
    with pytest.raises(ValueError, match="not in clips"):
        _grouped_input([["ghost"]])
    with pytest.raises(ValueError, match="only once"):
        _grouped_input([["a"], ["a"]])
    with pytest.raises(ValueError, match="only once"):
        _grouped_input([["a"]], pinned_last=["a"])
