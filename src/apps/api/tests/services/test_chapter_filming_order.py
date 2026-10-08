"""KRI-516: chapter captions under "order by when I filmed them" read forward.

Prod thread 0b1f9556 (stress kit M3 "Berlin"): "order the videos by the time I filmed them,
morning to night; chapter titles: Sabah, Üniversite, Öğle arası, Spor, Akşam". The resolver
never saw when a clip was filmed, so the 18:55 fridge and 19:30 cooking clips became
"Öğle arası" (lunch break) after "Spor" (gym). Now the resolver reads the clips in filming
order with their place in it, and a deterministic guard drops any chapter membership that
would still run backwards.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverInput
from app.config import settings
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    _build_resolver_input,
    keep_chapters_in_filming_order,
)

T0 = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
REQUEST = (
    "Berlin'de öğrenci olarak bir günüm. Videoları çektiğim saat sırasına göre diz, sabahtan "
    "geceye. Bölüm başlıkları koy: Sabah, Üniversite, Öğle arası, Spor, Akşam."
)
CHAPTERS = [
    ("caption_sabah", "Sabah"),
    ("caption_uni", "Üniversite"),
    ("caption_ogle", "Öğle arası"),
    ("caption_spor", "Spor"),
    ("caption_aksam", "Akşam"),
]
GOLDEN = Path(__file__).parents[1] / "fixtures/agent_evals/clip_request_resolver/golden"


def _clip(media_id: str, minutes: int | None, summary: str = "") -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis={"understanding": {"summary": summary}} if summary else None,
        capture_time=None if minutes is None else T0 + timedelta(minutes=minutes),
    )


def _chapters(**members: list[tuple[str, float]]) -> list[ResolvedClipIntent]:
    return [
        ResolvedClipIntent(
            intent_id=intent_id,
            op="caption",
            attribute=f"{text} chapter",
            creator_text=text,
            caption_text=text,
            assignments=[
                ClipAssignment(media_id=m, confidence=c) for m, c in members.get(intent_id, [])
            ],
        )
        for intent_id, text in CHAPTERS
    ]


def _members(intents: list[ResolvedClipIntent]) -> dict[str, list[str]]:
    return {i.intent_id: i.media_ids() for i in intents}


# Attachment order is scrambled; minutes after 05:00 UTC give the filming order.
DAY = [
    _clip("gym", 630),
    _clip("lecture", 95),
    _clip("elif", 970),
    _clip("moka", 12),
    _clip("doner", 340),
    _clip("cooking", 750),
    _clip("ubahn", 48),
    _clip("bridge", 885),
    _clip("library", 200),
    _clip("fridge", 715),
    _clip("bike", 425),
]


# ── the resolver input ────────────────────────────────────────────────────────


def test_filming_order_lists_clips_in_filming_order_with_their_place() -> None:
    resolver_input, alias_to_media, _ = _build_resolver_input(
        [ClipIntent(intent_id="c", op="caption", attribute="x", creator_text="Sabah")],
        REQUEST,
        DAY,
        filming_order=True,
    )
    listed = [alias_to_media[c.alias] for c in resolver_input.clips]
    assert listed == [
        "moka", "ubahn", "lecture", "library", "doner", "bike",
        "gym", "fridge", "cooking", "bridge", "elif",
    ]  # fmt: skip
    assert [c.record["filmed_order"] for c in resolver_input.clips] == list(range(1, 12))


def test_without_filming_order_the_input_is_unchanged() -> None:
    resolver_input, alias_to_media, _ = _build_resolver_input(
        [ClipIntent(intent_id="c", op="caption", attribute="x", creator_text="Sabah")],
        REQUEST,
        DAY,
    )
    assert [alias_to_media[c.alias] for c in resolver_input.clips] == [c.media_id for c in DAY]
    assert all("filmed_order" not in c.record for c in resolver_input.clips)


def test_a_clip_without_a_capture_time_keeps_its_slot_and_gets_no_place() -> None:
    clips = [_clip("b", 50), _clip("untimed", None), _clip("a", 10)]
    resolver_input, alias_to_media, _ = _build_resolver_input(
        [ClipIntent(intent_id="c", op="caption", attribute="x", creator_text="Sabah")],
        REQUEST,
        clips,
        filming_order=True,
    )
    rows = [(alias_to_media[c.alias], c.record.get("filmed_order")) for c in resolver_input.clips]
    assert rows == [("a", 1), ("untimed", None), ("b", 3)]


# ── the guard ────────────────────────────────────────────────────────────────


def test_forward_chapters_are_untouched() -> None:
    intents = _chapters(
        caption_sabah=[("moka", 0.9), ("ubahn", 0.8)],
        caption_uni=[("lecture", 0.9), ("library", 0.9)],
        caption_ogle=[("doner", 0.9)],
        caption_spor=[("bike", 0.9), ("gym", 0.9)],
        caption_aksam=[("fridge", 0.8), ("cooking", 0.9), ("bridge", 0.9), ("elif", 0.9)],
    )
    kept, dropped = keep_chapters_in_filming_order(intents, DAY, REQUEST)
    assert dropped == 0
    assert kept is intents


def test_the_incident_drops_the_backwards_lunch_titles_not_the_gym() -> None:
    """The prod resolution: fridge + cooking under "Öğle arası" after "Spor"."""
    intents = _chapters(
        caption_sabah=[("moka", 0.9)],
        caption_uni=[("lecture", 0.9), ("library", 0.9)],
        caption_ogle=[("fridge", 0.8), ("cooking", 0.8), ("doner", 0.8)],
        caption_spor=[("bike", 0.9), ("gym", 0.9)],
        caption_aksam=[("bridge", 0.9), ("elif", 0.9)],
    )
    kept, dropped = keep_chapters_in_filming_order(intents, DAY, REQUEST)
    assert dropped == 2
    assert _members(kept)["caption_ogle"] == ["doner"]
    assert _members(kept)["caption_spor"] == ["bike", "gym"]
    assert _members(kept)["caption_aksam"] == ["bridge", "elif"]


def test_a_clip_in_two_chapters_keeps_the_one_that_reads_forward() -> None:
    intents = _chapters(
        caption_sabah=[("moka", 0.9)],
        caption_uni=[("lecture", 0.9)],
        caption_spor=[("gym", 0.9), ("lecture", 0.5)],
    )
    kept, dropped = keep_chapters_in_filming_order(intents, DAY, REQUEST)
    assert dropped == 1
    assert _members(kept)["caption_uni"] == ["lecture"]
    assert _members(kept)["caption_spor"] == ["gym"]


@pytest.mark.parametrize(
    ("request_text", "clips"),
    [
        ("Make chapters for my day.", DAY),  # chapter words are not in the request
        (REQUEST, [_clip(c.media_id, None) for c in DAY]),  # no capture times
    ],
    ids=["chapter-order-unknown", "no-capture-time"],
)
def test_without_a_known_sequence_nothing_changes(request_text: str, clips) -> None:
    intents = _chapters(caption_ogle=[("fridge", 0.8)], caption_spor=[("gym", 0.9)])
    kept, dropped = keep_chapters_in_filming_order(intents, clips, request_text)
    assert (kept, dropped) == (intents, 0)


def test_other_intents_and_untimed_clips_are_never_touched() -> None:
    clips = [*DAY, _clip("untimed", None)]
    other = ResolvedClipIntent(
        intent_id="include_all",
        op="include",
        attribute="all",
        assignments=[ClipAssignment(media_id="fridge", confidence=0.9)],
    )
    intents = [
        *_chapters(caption_ogle=[("fridge", 0.8), ("untimed", 0.8)], caption_spor=[("gym", 0.9)]),
        other,
    ]
    kept, dropped = keep_chapters_in_filming_order(intents, clips, REQUEST)
    assert dropped == 1
    assert _members(kept)["caption_ogle"] == ["untimed"]
    assert kept[-1] is other


def test_the_recorded_miss_is_repaired_to_a_forward_day() -> None:
    """A verbatim live reply (prompt 2026-10-08.1) that still put the fridge under lunch."""
    fixture = json.loads((GOLDEN / "kri516_berlin_chapters_guard_case.json").read_text())
    resolver_input = ClipRequestResolverInput.model_validate(fixture["input"])
    output = ClipRequestResolverAgent(MagicMock()).parse(fixture["raw_text"], resolver_input)
    by_id = {i.intent_id: i for i in resolver_input.intents}
    clips = [
        _clip(c.alias, int(c.record["filmed_order"]) * 10)
        for c in resolver_input.clips
        if "filmed_order" in c.record
    ]
    intents = [
        ResolvedClipIntent(
            intent_id=block.intent_id,
            op=by_id[block.intent_id].op,
            attribute=by_id[block.intent_id].attribute,
            creator_text=by_id[block.intent_id].creator_text,
            position=by_id[block.intent_id].position,
            assignments=[
                ClipAssignment(media_id=a.media, confidence=a.confidence) for a in block.assignments
            ],
        )
        for block in output.intents
    ]
    fridge = "m009"  # filmed_order 9, after the gym (m008)
    assert fridge in _members(intents)["caption_oglearasi"]

    kept, dropped = keep_chapters_in_filming_order(intents, clips, resolver_input.creator_request)

    assert dropped == 1
    assert fridge not in _members(kept)["caption_oglearasi"]
    order = ["caption_sabah", "caption_uni", "caption_oglearasi", "caption_spor", "caption_aksam"]
    chapter_of = {m: order.index(i) for i in order for m in _members(kept)[i]}
    walk = [chapter_of[c.media_id] for c in clips if c.media_id in chapter_of]
    assert walk == sorted(walk)


# ── the wiring ────────────────────────────────────────────────────────────────


def _wire(monkeypatch, intents: list[PlannedClipIntent], resolution: IntentResolution):
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(planning, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    resolver = AsyncMock(return_value=resolution)
    monkeypatch.setattr(planning, "resolve_clip_intents_for_turn", resolver)
    return resolver


def _planned(*, chrono: bool) -> list[PlannedClipIntent]:
    rows = [
        PlannedClipIntent(
            intent_id=intent_id,
            op="caption",
            attribute=f"{text} chapter",
            creator_text=text,
            source_quote=REQUEST,
        )
        for intent_id, text in CHAPTERS
    ]
    if chrono:
        rows.append(
            PlannedClipIntent(
                intent_id="order_chrono",
                op="order",
                attribute="all clips",
                order_by="capture_time",
                source_quote=REQUEST,
            )
        )
    return rows


@pytest.mark.asyncio
async def test_a_filming_time_order_reaches_the_resolver_and_the_guard(monkeypatch) -> None:
    backwards = _chapters(caption_ogle=[("cooking", 0.8)], caption_spor=[("gym", 0.9)])
    resolver = _wire(monkeypatch, _planned(chrono=True), IntentResolution(intents=backwards))

    result = await planning.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message=REQUEST,
        candidate_intents=None,
        clips=DAY,
        run_context=RunContext(),
    )

    assert resolver.await_args.kwargs["filming_order"] is True
    members = {i.intent_id: i.media_ids() for i in result.resolution.intents}
    assert members["caption_ogle"] == []
    assert members["caption_spor"] == ["gym"]
    assert "order_chrono" in members  # the filming order itself is still resolved


@pytest.mark.asyncio
async def test_without_a_filming_time_order_the_call_and_result_are_unchanged(
    monkeypatch,
) -> None:
    backwards = _chapters(caption_ogle=[("cooking", 0.8)], caption_spor=[("gym", 0.9)])
    resolution = IntentResolution(intents=backwards)
    resolver = _wire(monkeypatch, _planned(chrono=False), resolution)

    result = await planning.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message=REQUEST,
        candidate_intents=None,
        clips=DAY,
        run_context=RunContext(),
    )

    assert "filming_order" not in resolver.await_args.kwargs
    assert result.resolution is resolution
