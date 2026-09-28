"""KRI-189: the planner sees clip facts and orders by capture time.

Flag-off behavior is pinned byte-for-byte: a plan input with no `clip_facts`
renders the same prompt, dumps the same media rows and records no ordering.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
    _media_prompt_dict,
    _reorder_beats_by_capture_time,
)
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent


def _fact(kind: str, value: str, provenance: str) -> dict:
    return {"kind": kind, "value": value, "provenance": provenance}


def _time(hour: int) -> dict:
    return _fact("capture_time", f"2026-09-20T{hour:02d}:00:00Z", "exif")


def _media(media_id: str, *facts: dict) -> EditProposalMedia:
    return EditProposalMedia(
        media_id=media_id,
        lane="clip",
        kind="video",
        duration_s=6.0,
        facts=list(facts),
    )


def _input(media: list[EditProposalMedia], **overrides: object) -> EditProposalAgentInput:
    fields: dict[str, object] = {
        "direction": "guided_story",
        "pace": "balanced",
        "target_duration_s": 24,
        "media": media,
        "story_shape": "day_vlog",
        "clip_facts": True,
    }
    fields.update(overrides)
    return EditProposalAgentInput(**fields)  # type: ignore[arg-type]


def _raw(media_ids: list[str]) -> str:
    beats = [
        {
            "topic": f"Stop {index}",
            "thought": f"Moment number {index} of the run.",
            "media_ids": [media_id],
            "layout": "fullscreen",
            "duration_s": 6,
        }
        for index, media_id in enumerate(media_ids, start=1)
    ]
    return json.dumps({"title": "East Run", "duration_s": 24, "story_beats": beats})


def _order(output) -> list[str]:
    return [beat.media_ids[0] for beat in output.story_beats]


def _parse(agent_input: EditProposalAgentInput, attach_order: list[str]):
    return EditProposalAgent(None).parse(_raw(attach_order), agent_input)  # type: ignore[arg-type]


# ── day_vlog uses capture time ───────────────────────────────────────────────


def test_day_vlog_orders_by_capture_time_when_known() -> None:
    # Attached (upload-completion) order is NOT filming order.
    media = [
        _media("eminonu", _time(11)),
        _media("arnavutkoy", _time(7)),
        _media("galata", _time(10)),
        _media("rumeli", _time(9)),
    ]
    output = _parse(_input(media), ["eminonu", "arnavutkoy", "galata", "rumeli"])

    assert _order(output) == ["arnavutkoy", "rumeli", "galata", "eminonu"]
    assert "day_vlog_reordered_by_capture_time" in output.repairs
    assert output.ordering == {"ordering_basis": "capture_time", "ordering_fallback_clip_ids": []}


def test_clips_without_capture_time_keep_their_attachment_slot_and_are_reported() -> None:
    media = [
        _media("a", _time(11)),
        _media("no-time"),
        _media("c", _time(7)),
        _media("d", _time(9)),
    ]
    output = _parse(_input(media), ["a", "no-time", "c", "d"])

    # `no-time` stays in slot 2; the three timed clips fill the other slots in time order.
    assert _order(output) == ["c", "no-time", "d", "a"]
    assert output.ordering == {
        "ordering_basis": "capture_time",
        "ordering_fallback_clip_ids": ["no-time"],
    }


def test_no_capture_times_at_all_falls_back_to_attachment_order_and_says_so() -> None:
    media = [_media("a"), _media("b"), _media("c")]
    output = _parse(_input(media), ["c", "a", "b"])

    assert _order(output) == ["a", "b", "c"]
    assert "day_vlog_reordered_by_attachment" in output.repairs
    assert output.ordering == {
        "ordering_basis": "attachment",
        "ordering_fallback_clip_ids": ["a", "b", "c"],
    }


def test_flag_off_day_vlog_is_attachment_order_with_no_ordering_record() -> None:
    media = [_media("a"), _media("b"), _media("c")]
    output = _parse(_input(media, clip_facts=False), ["c", "a", "b"])

    assert _order(output) == ["a", "b", "c"]
    assert "day_vlog_reordered_by_attachment" in output.repairs
    assert output.ordering is None


def test_flag_off_ignores_facts_even_when_media_carry_them() -> None:
    """A stale stored fact must not change the order once the kill switch is off."""
    media = [_media("a", _time(11)), _media("b", _time(7))]
    output = _parse(_input(media, clip_facts=False), ["a", "b"])

    assert _order(output) == ["a", "b"]
    assert output.ordering is None


# ── by_capture_time / by_route order intents ─────────────────────────────────


def _order_by_intent(order_by: str, media_ids: list[str]) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="i-order-by",
        op="order",
        attribute="the order I filmed them",
        order_by=order_by,
        assignments=[ClipAssignment(media_id=m, confidence=1.0) for m in media_ids],
    )


def test_order_by_capture_time_intent_reorders_a_plain_guided_story() -> None:
    media = [_media("a", _time(11)), _media("b", _time(7)), _media("c", _time(9))]
    agent_input = _input(
        media,
        story_shape=None,
        clip_intents=[_order_by_intent("capture_time", ["a", "b", "c"])],
    )
    output = _parse(agent_input, ["a", "b", "c"])

    assert _order(output) == ["b", "c", "a"]
    assert output.ordering == {"ordering_basis": "capture_time", "ordering_fallback_clip_ids": []}


def test_order_by_on_a_fast_montage_is_recorded_as_not_applied() -> None:
    """A montage keeps its cuts; the receipt must never read as "ordered by capture time"."""
    media = [_media("a", _time(11)), _media("b", _time(7))]
    montage_input = SimpleNamespace(
        clip_facts=True,
        direction="fast_montage",
        media=media,
    )
    output = SimpleNamespace(ordering=None, story_beats=[])
    _reorder_beats_by_capture_time(
        output,
        montage_input,
        [_order_by_intent("capture_time", ["a", "b"])],  # type: ignore[arg-type]
    )
    assert output.ordering == {
        "ordering_basis": "not_applied",
        "ordering_fallback_clip_ids": [],
        "reason": "fast_montage",
    }


def test_order_by_route_uses_capture_order_for_now() -> None:
    media = [_media("a", _time(11)), _media("b", _time(7))]
    agent_input = _input(
        media, story_shape=None, clip_intents=[_order_by_intent("route", ["a", "b"])]
    )
    assert _order(_parse(agent_input, ["a", "b"])) == ["b", "a"]


def test_order_by_intent_is_ignored_when_the_flag_is_off() -> None:
    media = [_media("a", _time(11)), _media("b", _time(7))]
    agent_input = _input(
        media,
        story_shape=None,
        clip_facts=False,
        clip_intents=[_order_by_intent("capture_time", ["a", "b"])],
    )
    output = _parse(agent_input, ["a", "b"])
    assert _order(output) == ["a", "b"]
    assert output.ordering is None


def test_order_by_intent_prompt_clause_is_rendered_in_alias_terms() -> None:
    media = [_media("a", _time(11)), _media("b", _time(7))]
    agent_input = _input(
        media, story_shape=None, clip_intents=[_order_by_intent("capture_time", ["a", "b"])]
    )
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "ORDER (capture_time)" in prompt
    assert "`capture_time` fact" in prompt


# ── prompt + media dump: flag-off byte identity ──────────────────────────────


def test_facts_are_shown_to_the_planner_only_when_the_flag_is_on() -> None:
    media = [
        _media("a", _time(7), _fact("landmark", "Rumeli Hisarı", "inferred")),
        _media("b", _time(9)),
    ]
    on = EditProposalAgent(None).render_prompt(_input(media, story_shape=None))  # type: ignore[arg-type]
    assert "Rumeli Hisarı" in on
    assert '"provenance": "inferred"' in on
    assert "A row may also carry `facts`" in on
    assert "$clip_facts_note" not in on

    off = EditProposalAgent(None).render_prompt(  # type: ignore[arg-type]
        _input([_media("a"), _media("b")], story_shape=None, clip_facts=False)
    )
    assert "A row may also carry `facts`" not in off
    assert "capture_time" not in off
    assert "$clip_facts_note" not in off


def test_media_without_facts_dumps_exactly_as_before() -> None:
    plain = EditProposalMedia(media_id="a", lane="clip", kind="video", duration_s=6.0)
    assert "facts" not in plain.model_dump()
    assert "facts" not in _media_prompt_dict(plain)
    assert "clip_facts" not in _input([plain], clip_facts=False).model_dump()
