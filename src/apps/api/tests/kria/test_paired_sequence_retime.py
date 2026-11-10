"""Retiming one of two word sequences shown together keeps the pair on one span (KRI-1365).

The creator's Turkish text (middle) and its English translation (bottom) play word by word over
the same time. "Make each text shorter" retimed one; the other stayed at its old length, so the
translation outlived (or ended before) the text it translates and the 'shown together' contract the
text pass built (`_check_research_layers`) was lost.

Failure modes, written before the fix:
 1. retime one of a pair: the partner follows to the same span;
 2. a sequence with no partner at the same time is retimed alone;
 3. a partner shown at a different time (not together) is left alone;
 4. retiming both, in either order, ends on one span, and a repeat is a no-op;
 5. a partner that cannot read at the new span refuses honestly and changes nothing.
"""

from __future__ import annotations

import copy

import pytest

from app.services.kria_editor_ops import compile_editor_ops
from tests.services.test_duration_text_preservation import sequence


def _pair(monkeypatch, *, partner_span=None, partner_position="bottom", partner_words=None):
    job, variant = sequence(monkeypatch, end_s=10.8)
    opening = variant["text_elements"]
    start, end = partner_span or (opening[0]["start_s"], opening[-1]["end_s"])
    count = partner_words or len(opening)
    partner = [
        {
            **copy.deepcopy(opening[0]),
            "id": f"translation::sequence-{i + 1}",
            "text": f"kelime{i + 1}",
            "position": partner_position,
            "start_s": round(start + i * (end - start) / count, 6),
            "end_s": round(start + (i + 1) * (end - start) / count, 6),
            "source_params": {"sequence_source_id": "translation"},
        }
        for i in range(count)
    ]
    variant["text_elements"] = [*opening, *partner]
    return job, variant


def _retime(source: str, rows: list[dict], end_s: float) -> dict:
    ids = [r["id"] for r in rows if r["id"].startswith(source + "::")]
    return {
        "op": "retime_text_sequence",
        "selector": {"ids": ids},
        "target_ids": ids,
        "expected_count": len(ids),
        "start_s": 0.0,
        "end_s": end_s,
    }


def _span(rows: list[dict], source: str) -> tuple[float, float]:
    own = [r for r in rows if r["id"].startswith(source + "::")]
    return min(r["start_s"] for r in own), max(r["end_s"] for r in own)


def _compile(job, variant, *ops):
    return compile_editor_ops(job, variant, list(ops)).payload.text_elements


def test_retiming_one_of_a_pair_retimes_its_partner_too(monkeypatch):
    job, variant = _pair(monkeypatch)
    rows = variant["text_elements"]

    after = _compile(job, variant, _retime("opening", rows, 8.0))

    assert _span(after, "opening") == pytest.approx((0, 8.0), abs=1e-3)
    assert _span(after, "translation") == pytest.approx((0, 8.0), abs=1e-3)
    assert [r["text"] for r in after if r["id"].startswith("translation::")] == [
        r["text"] for r in rows if r["id"].startswith("translation::")
    ]


def test_a_sequence_with_no_partner_is_retimed_alone(monkeypatch):
    job, variant = _pair(monkeypatch)
    variant["text_elements"] = [
        r for r in variant["text_elements"] if r["id"].startswith("opening::")
    ]
    rows = variant["text_elements"]

    after = _compile(job, variant, _retime("opening", rows, 8.0))

    assert _span(after, "opening") == pytest.approx((0, 8.0), abs=1e-3)
    assert not [r for r in after if r["id"].startswith("translation::")]


def test_a_sequence_shown_at_another_time_is_not_a_partner(monkeypatch):
    job, variant = _pair(monkeypatch, partner_span=(0.0, 5.0))
    rows = variant["text_elements"]
    before = _span(rows, "translation")

    after = _compile(job, variant, _retime("opening", rows, 8.0))

    assert _span(after, "opening") == pytest.approx((0, 8.0), abs=1e-3)
    assert _span(after, "translation") == before


def test_a_sequence_at_the_same_place_is_not_a_partner(monkeypatch):
    job, variant = _pair(monkeypatch, partner_position="middle")
    rows = variant["text_elements"]
    before = _span(rows, "translation")

    after = _compile(job, variant, _retime("opening", rows, 8.0))

    assert _span(after, "translation") == before


@pytest.mark.parametrize("order", [("opening", "translation"), ("translation", "opening")])
def test_retiming_both_lands_on_one_span_and_repeating_changes_nothing(monkeypatch, order):
    job, variant = _pair(monkeypatch)
    rows = variant["text_elements"]
    first, second = order

    once = _compile(job, variant, _retime(first, rows, 8.0), _retime(second, rows, 8.0))

    assert _span(once, "opening") == pytest.approx((0, 8.0), abs=1e-3)
    assert _span(once, "translation") == pytest.approx((0, 8.0), abs=1e-3)
    again = _compile(
        job, copy.deepcopy({**variant, "text_elements": once}), _retime(first, once, 8.0)
    )
    assert [(r["id"], r["start_s"], r["end_s"]) for r in again] == [
        (r["id"], r["start_s"], r["end_s"]) for r in once
    ]


def test_a_partner_that_cannot_read_at_the_new_span_refuses_and_changes_nothing(monkeypatch):
    # The text reads at 8 s (27 words); its 40-word translation needs at least 10 s.
    job, variant = _pair(monkeypatch, partner_words=40)
    rows = variant["text_elements"]
    before = copy.deepcopy(variant)

    with pytest.raises(ValueError, match="10"):
        _compile(job, variant, _retime("opening", rows, 8.0))

    assert variant == before
