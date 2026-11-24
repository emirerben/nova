"""The story sequencer: where the creator did not say, the AI picks the order that fits the story
(``KRIA_STORY_SEQUENCER_ENABLED``, KRI-1593; a child of the live clip order, KRI-1586).

Owner rule: "the AI decides what fits the story; if it can't, upload order". What the creator
said about the order is kept exactly, a clip they placed never moves, and the order is said to be
the AI's wherever it is shown. A small separate model (`app.agents.story_sequencer`) only ranks;
`app.services.story_order` decides what may move and whether the ranking is used.

Ways this can fail, written before the code (each is pinned below; "FM" = failure mode):

 FM1  flag off (either flag): a model call is made, an entry or a snapshot key is written, or a
      card / listing / receipt changes by a byte. An ``ai_story`` entry the plan was only SEEN to
      have laid out (after a render) starts pinning the order.
 FM2  a creator-placed clip moves: one stated alone ("the map video first"), dragged, edited by
      hand, or on the card when the creator tapped "use this order". A hostile ranking (it lists
      the placed clip elsewhere, or last) is the negative control.
 FM3  a clip leaves the group the creator described (a "friends" clip ranked among the scenery),
      or a group's place among the groups changes.
 FM4  the card, the render contract, approval readiness and the worker's own plan disagree on
      the story order (the KRI-503 dead end). Oracle: `preflight_montage_plan`, the phone montage
      planner the worker runs.
 FM5  the model's answer is unusable (a missing, unknown or repeated id; "cannot decide"; a
      provider failure): the turn fails, or a half-applied order is written, instead of the
      order the clips were added in.
 FM6  the sequencer is asked when there is nothing to decide (one open clip; every position
      stated; a random, filming-time or group-first order; the native program; a creator song;
      Visuals in the edit; a story outline that owns its own order; a row the server could not
      resolve; after a render; no brief to pin against), or asked AGAIN on a later turn for
      clips it already looked at.
 FM7  the story order is lost on the next turn (a later draft goes back to upload order), or it
      survives a taken-back turn.
 FM8  the order is passed off as the creator's or as the upload order: the receipt says "the
      order you have them now", an old build is given the list with no word on whose it is, the
      card's clips read as the creator's, or a language is missed (EN / TR / DE).
 FM9  a later statement of the creator's does not win: a drag after the story order still reads
      as the AI's, or the sequencer moves the dragged clip on a later run.
 FM10 a media id, UUID or file name reaches the model as a clip's name, or the model's own
      ``reason`` text reaches anything the creator sees.
 FM11 a clip added after the story order is never placed, or placing it moves a clip the
      creator placed.

Not pinned here: a continued run of the same turn paying twice (the call goes through
`turn_checkpoint.checkpointed`, which has its own guard) and whether the live model's orders are
any good (`tests/evals/test_story_sequencer_evals.py`; the held-out comparison against upload
order needs a paid run and is not part of this module).

Recorded vs reconstructed: the Toroko request and its group shape are the recorded incident
(`test_unstated_order_default.py`); every ranking a "model" returns here is written by hand, so
this proves what the server does with an answer, not that the model gives good ones.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from app.agents._runtime import SchemaError
from app.agents.story_sequencer import StorySequencerAgent, StorySequencerInput
from app.config import settings
from app.kria import planner as planner_module
from app.kria.brief import CreativeBrief
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.services import clip_order_card as card_mod
from app.services import clip_order_ledger as ledger
from app.services import story_order
from app.services.clip_order_ledger import (
    AI_STORY,
    CREATOR_DRAG,
    CREATOR_EDITOR,
    CREATOR_TEXT,
    LEDGER_KEY,
    SNAPSHOT_KEY,
    STORY_SNAPSHOT_KEY,
    LedgerEntry,
    current_clip_order,
    fold_clip_order,
)
from app.services.clip_order_sequence import apply_sequence, sequence_rows
from app.services.creator_render_contract import build_render_contract
from tests.kria import test_unstated_order_default as incident
from tests.kria.test_unstated_order_default import (
    ATTACHMENT,
    ENTRY,
    EXPECTED,
    FRIENDS,
    MAP,
    SCENERY,
    UNMENTIONED,
    _media_id,
)

MAP_CLIP = _media_id(MAP[0])
STORY_EN = "where you didn't say, I put the clips in the order that tells the story best"
STORY_TR = "söylemediğin yerlerde klipleri hikâyeyi en iyi anlatan sıraya ben koydum"
KEPT_EN = "where you didn't say, the clips play in the order you have them now"

_ID_LEAK = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|analysis-proxy|asset-|legacy-clip|\.mp4|\.mov|IMG_",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "clip_intents_enabled",
        "clip_facts_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_unstated_order_default_enabled",
        "kria_clip_order_ledger_enabled",
    ):
        monkeypatch.setattr(settings, name, True)


@pytest.fixture
def story_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_story_sequencer_enabled", True)


def _ids(indexes: tuple[int, ...]) -> list[str]:
    return [_media_id(i) for i in indexes]


def _state(*entries: LedgerEntry, attach: list[str] = ATTACHMENT) -> ledger.ClipOrderState:
    return current_clip_order(attachment_ids=attach, entries=entries)


def _no_order_brief() -> CreativeBrief:
    """ "Make a video from these clips": a length ask and not one word about the order."""
    return CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [incident._brief().model_dump(mode="json")["requirements"][0]],
        }
    )


def _plain_strategy() -> dict[str, Any]:
    return incident._strategy_dict([])


def _story(strategy, snapshot, state=None, *, brief, **kw: Any):
    return story_order.plan_story(strategy, snapshot, state or _state(), brief=brief, **kw)


def _worker_order(brief, strategy, snapshot) -> list[str]:
    from app.kria.brief_checks import plan_facts_from_unified_montage

    plan = incident._planned(brief, strategy, snapshot)
    return list(plan_facts_from_unified_montage(plan.record()).clip_ids)


def _reverse_each(plan: story_order.StoryPlan) -> list[str]:
    """A hand-written "story": every open stretch played back to front."""
    return [item for span in plan.spans for item in reversed(span)]


# ── FM4 + the incident replay: unplaced clips get a story-fit order every reader agrees on ─────


def test_the_incident_replay_gets_a_story_order_the_contract_readiness_and_worker_all_play(
    story_on: None,
) -> None:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    assert plan is not None
    assert list(plan.before) == EXPECTED
    # the map video was named alone: it is the creator's; the three groups and the clips nobody
    # mentioned are four open stretches
    assert plan.fixed == {MAP_CLIP}
    assert [list(span) for span in plan.spans] == [
        _ids(ENTRY),
        _ids(FRIENDS),
        _ids(SCENERY),
        _ids(UNMENTIONED),
    ]

    result = story_order.settle_story(plan, _reverse_each(plan), strategy, snapshot, brief=brief)
    after = list(result.entry.after_ids)
    assert result.changed and result.entry.actor == AI_STORY
    assert after == [
        MAP_CLIP,
        *reversed(_ids(ENTRY)),
        *reversed(_ids(FRIENDS)),
        *reversed(_ids(SCENERY)),
        *reversed(_ids(UNMENTIONED)),
    ]
    assert set(result.entry.story_ids) == set(ATTACHMENT) - {MAP_CLIP}
    stamped = result.snapshot
    assert stamped is not None
    assert stamped[SNAPSHOT_KEY] == after and stamped[STORY_SNAPSHOT_KEY] is True

    contract = build_render_contract(
        strategy, generation_id="story", brief=brief, media_snapshot=stamped
    )
    assert contract is not None and not contract.unresolved
    assert contract.order_required and list(contract.order_ids) == after
    assert _worker_order(brief, strategy, stamped) == after
    report = incident._report(brief, strategy, snapshot=stamped)
    assert report["ready"] is True, report["requirements"]
    assert incident._rows(report)["r2"]["reason"] == STORY_EN


def test_a_request_that_says_nothing_about_order_gets_the_whole_order_from_the_story(
    story_on: None,
) -> None:
    """The common case: no order ask at all. Every clip is open, in one stretch."""
    brief = _no_order_brief()
    strategy = _plain_strategy()
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    assert plan is not None and plan.fixed == frozenset()
    assert [list(span) for span in plan.spans] == [ATTACHMENT]
    ranking = list(reversed(ATTACHMENT))
    result = story_order.settle_story(plan, ranking, strategy, snapshot, brief=brief)
    assert list(result.entry.after_ids) == ranking
    contract = build_render_contract(
        strategy, generation_id="story", brief=brief, media_snapshot=result.snapshot
    )
    assert contract is not None and list(contract.order_ids) == ranking
    assert _worker_order(brief, strategy, result.snapshot) == ranking


# ── FM2 + FM3: what the creator placed never moves, whatever the model answers ─────────────────


def test_fm2_fm3_a_hostile_ranking_cannot_move_a_placed_clip_or_break_a_group(
    story_on: None,
) -> None:
    """Negative control: the "model" lists the placed map clip LAST and ranks every clip in the
    reverse of the whole order, across the groups. The map clip stays first, every group keeps
    its own slots, and only the order inside a stretch changed."""
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    hostile = [*reversed(EXPECTED[1:]), MAP_CLIP]
    result = story_order.settle_story(plan, hostile, strategy, snapshot, brief=brief)
    after = list(result.entry.after_ids)
    assert result.changed
    assert after[0] == MAP_CLIP
    assert set(after[1:4]) == set(_ids(ENTRY))
    assert set(after[4:8]) == set(_ids(FRIENDS))
    assert set(after[8:13]) == set(_ids(SCENERY))
    assert set(after[13:]) == set(_ids(UNMENTIONED))
    # the creator's own rows, seated again on the result, change nothing
    assert apply_sequence(after, sequence_rows(strategy["resolved_clip_intents"])) == after


@pytest.mark.parametrize("actor", [CREATOR_DRAG, CREATOR_EDITOR, CREATOR_TEXT])
def test_fm2_a_clip_the_creator_moved_keeps_its_exact_position(story_on: None, actor: str) -> None:
    brief = _no_order_brief()
    strategy = _plain_strategy()
    moved = ATTACHMENT[7]
    dragged = [ATTACHMENT[0], moved, *(m for m in ATTACHMENT[1:] if m != moved)]
    state = _state(LedgerEntry(actor, tuple(ATTACHMENT), tuple(dragged), "t1"))
    snapshot = ledger.with_clip_order_ids(incident._snapshot(), state)
    plan = _story(strategy, snapshot, state, brief=brief)
    assert plan is not None and plan.fixed == {moved}
    assert moved not in plan.open_ids
    result = story_order.settle_story(
        plan, list(reversed(dragged)), strategy, snapshot, brief=brief
    )
    after = list(result.entry.after_ids)
    assert result.changed and after.index(moved) == 1
    assert after == [dragged[-1], moved, *reversed(dragged[2:-1]), dragged[0]]
    assert moved not in result.entry.story_ids


def test_fm2_use_this_order_on_the_card_fixes_every_clip_on_it(story_on: None) -> None:
    brief = _no_order_brief()
    plan = _story(_plain_strategy(), incident._snapshot(), brief=brief, confirmed=tuple(ATTACHMENT))
    assert plan is None


def test_fm2_a_ranking_that_would_move_a_fixed_clip_through_a_bad_span_is_discarded(
    story_on: None,
) -> None:
    """Belt and braces: `settle_story` re-checks the result, so even a plan whose spans were
    wrong (here: a span that wrongly holds the placed map clip) cannot move a fixed clip."""
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    broken = story_order.StoryPlan(
        plan.before, ((MAP_CLIP, *plan.spans[0]), *plan.spans[1:]), plan.fixed, plan.played
    )
    result = story_order.settle_story(
        broken, [*reversed(plan.spans[0]), MAP_CLIP], strategy, snapshot, brief=brief
    )
    assert not result.changed and result.snapshot is None


# ── FM5: an unusable answer is the order the clips were added in ───────────────────────────────


def _agent_input(plan: story_order.StoryPlan, snapshot: dict[str, Any]) -> StorySequencerInput:
    clips, _alias = story_order.sequencer_clips(plan, snapshot)
    return StorySequencerInput(request="a day on the train", clips=clips)


@pytest.mark.parametrize(
    "answer",
    [
        {"decided": True, "order": ["c1", "c2"]},  # leaves clips out
        {"decided": True, "order": ["c2", "c2", "c3"]},  # repeats one
        {"decided": True, "order": ["c99"]},  # invents one
        {"decided": True, "order": "c1"},  # not a list
        "not json",
    ],
    ids=["missing", "repeated", "unknown", "malformed", "not-json"],
)
def test_fm5_an_answer_that_is_not_a_full_ranking_is_refused_by_the_agent(answer: Any) -> None:
    brief = _no_order_brief()
    snapshot = incident._snapshot()
    plan = _story(_plain_strategy(), snapshot, brief=brief)
    raw = answer if isinstance(answer, str) else json.dumps(answer)
    with pytest.raises(SchemaError):
        StorySequencerAgent(None).parse(raw, _agent_input(plan, snapshot))  # type: ignore[arg-type]


def test_fm5_the_fixed_clip_is_not_a_valid_answer_and_cannot_decide_returns_nothing() -> None:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = story_order.StoryPlan(
        tuple(EXPECTED), (tuple(_ids(ENTRY)), tuple(_ids(FRIENDS))), frozenset({MAP_CLIP})
    )
    agent_input = _agent_input(plan, snapshot)
    agent = StorySequencerAgent(None)  # type: ignore[arg-type]
    open_aliases = agent_input.open_ids()
    assert "c1" not in open_aliases  # c1 is the placed map clip: shown, never open
    with pytest.raises(SchemaError):
        agent.parse(json.dumps({"decided": True, "order": ["c1", *open_aliases]}), agent_input)
    undecided = agent.parse(
        json.dumps({"decided": False, "order": open_aliases, "reason": "all the same view"}),
        agent_input,
    )
    assert undecided.decided is False and undecided.order == []
    # "cannot decide" leaves the order alone and is remembered, so the next turn does not ask
    result = story_order.settle_story(plan, None, strategy, snapshot, brief=brief)
    assert not result.changed and result.snapshot is None
    assert set(result.entry.story_ids) == set(plan.open_ids)


def test_fm5_a_partial_ranking_never_drops_a_clip(story_on: None) -> None:
    """`apply_ranking` is total even if a caller hands it half a ranking."""
    order = ["a", "b", "c", "d"]
    assert story_order.apply_ranking(order, [("a", "b", "c", "d")], ["c"]) == ["c", "a", "b", "d"]
    assert story_order.apply_ranking(order, [("a", "b")], ["zzz", "b", "a"]) == ["b", "a", "c", "d"]
    assert story_order.apply_ranking(order, [], ["d", "c"]) == order


# ── FM6: nothing to decide means no question to the model ──────────────────────────────────────


def _shape(**changes: Any) -> dict[str, Any]:
    return {**_plain_strategy(), **changes}


@pytest.mark.parametrize(
    "strategy",
    [
        _shape(ordering_choice="random"),
        _shape(ordering_choice="chronological"),
        _shape(ordering_choice="group_first"),
        _shape(render_program="native"),
        _shape(audio_strategy="user_song"),
        _shape(edit_format="subtitled"),
        _shape(editorial_plan={"viewer_goal": "x", "beats": []}),
        _shape(
            resolved_clip_intents=[
                {"op": "order", "status": "resolved", "attribute": "by time", "order_by": "capture"}
            ]
        ),
    ],
    ids=[
        "random",
        "filming-time",
        "group-first",
        "native",
        "creator-song",
        "talking",
        "story-outline",
        "order-by",
    ],
)
def test_fm6_an_order_that_is_not_the_upload_order_is_never_re_decided(
    story_on: None, strategy: dict[str, Any]
) -> None:
    assert _story(strategy, incident._snapshot(), brief=_no_order_brief()) is None


def test_fm6_nothing_open_nothing_asked(story_on: None) -> None:
    brief = _no_order_brief()
    snapshot = incident._snapshot()
    strategy = _plain_strategy()
    # after a render the timeline is the order
    assert _story(strategy, snapshot, brief=brief, rendered=True) is None
    # no brief = nothing the contract can pin against
    assert _story(strategy, snapshot, brief=None) is None
    # a Visual in the edit: the contract pins no carried order there
    with_visual = {**snapshot, "visuals": [{"media_id": "asset-v1", "kind": "image"}]}
    shows_visual = {**strategy, "selected_media_ids": [*ATTACHMENT, "asset-v1"]}
    assert _story(shows_visual, with_visual, brief=brief) is None
    # a row the server could not resolve: the creator placed something we cannot identify
    unresolved = incident._brief()
    rows = incident._drafted(unresolved)
    rows["resolved_clip_intents"][1] = {
        **rows["resolved_clip_intents"][1],
        "status": "ambiguous",
        "assignments": [],
    }
    assert story_order.open_spans(EXPECTED, rows["resolved_clip_intents"], fixed=()) == ()
    # every clip named alone (a listed order): all fixed, one clip per row
    listed = incident._drafted(
        unresolved, **{i: {"ordered_members": True} for i, *_rest in incident.GROUPS}
    )
    named = {m for *_x, members, _a in incident.GROUPS for m in _ids(members)}
    spans = story_order.open_spans(
        EXPECTED,
        listed["resolved_clip_intents"],
        fixed=story_order.fixed_ids(_state(), listed),
    )
    assert not named.intersection(item for span in spans for item in span)
    # one open clip has no order to choose
    assert story_order.open_spans(["a", "b"], [], fixed={"a"}) == ()


def test_fm6_the_sequencer_is_not_asked_twice_about_the_same_clips(story_on: None) -> None:
    brief = _no_order_brief()
    strategy = _plain_strategy()
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    for ranking in (list(reversed(ATTACHMENT)), None):  # it chose an order / it could not decide
        result = story_order.settle_story(plan, ranking, strategy, snapshot, brief=brief)
        state = _state(result.entry)
        later = ledger.with_clip_order_ids(incident._snapshot(), state)
        assert _story(strategy, later, state, brief=brief) is None


# ── FM7: the order lasts, and leaves with the turn that wrote it ───────────────────────────────


def test_fm7_the_story_order_is_the_order_on_the_next_turn_and_is_taken_back_with_its_turn(
    story_on: None,
) -> None:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    result = story_order.settle_story(plan, _reverse_each(plan), strategy, snapshot, brief=brief)
    after = list(result.entry.after_ids)
    seated = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    payload = {LEDGER_KEY: [seated.to_payload(), {**result.entry.to_payload(), "turn_id": "t1"}]}
    entries = fold_clip_order([("assistant", payload)])
    assert entries[-1].story_ids == result.entry.story_ids  # the payload round-trips
    state = _state(*entries)
    assert list(state.order) == after
    assert state.story_ids == set(result.entry.story_ids)
    assert state.source_of(MAP_CLIP) == CREATOR_TEXT

    # a later draft (an unrelated ask) carries the same order with no second model call
    later = ledger.with_clip_order_ids(incident._snapshot(), state)
    assert later[SNAPSHOT_KEY] == after and later[STORY_SNAPSHOT_KEY] is True
    assert _story(strategy, later, state, brief=brief) is None
    assert _worker_order(brief, strategy, later) == after

    # the creator removes that message: its story order goes with it
    retract = ("system", {LEDGER_KEY: [ledger.retract_turns_row(["t1"])]})
    assert fold_clip_order([("assistant", payload), retract]) == ()


# ── FM8: the order is said to be the AI's, in every place and language ─────────────────────────


def _story_card(story_on: None = None) -> tuple[dict[str, Any], ledger.ClipOrderState, list[str]]:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    result = story_order.settle_story(plan, _reverse_each(plan), strategy, snapshot, brief=brief)
    seated = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    state = _state(seated, result.entry)
    card = card_mod.build_card(
        state,
        snapshot["clip_assignments"],
        events=[],
        brief_version=1,
        applies=card_mod.card_applies(strategy),
        groups=card_mod.known_group_words((), strategy["resolved_clip_intents"]),
        stated=card_mod.stated_ids(strategy),
    )
    return card, state, list(result.entry.after_ids)


def test_fm8_the_card_says_which_clips_the_ai_placed_and_never_shows_an_id(story_on: None) -> None:
    card, _state_after, after = _story_card()
    assert card["shown"] is True and card["story"] is True
    assert [c["media_id"] for c in card["cards"]] == after
    sources = {c["media_id"]: c["source"] for c in card["cards"]}
    assert sources[MAP_CLIP] == CREATOR_TEXT
    assert {sources[m] for m in after if m != MAP_CLIP} == {AI_STORY}
    visible = json.dumps(
        [{k: v for k, v in c.items() if k != "media_id"} for c in card["cards"]],
        ensure_ascii=False,
    )
    assert not _ID_LEAK.search(visible)


@pytest.mark.parametrize(
    ("language", "head", "reason"),
    [
        (
            "en",
            "Where you didn't set the order, I picked the one that tells the story best.",
            STORY_EN,
        ),
        (
            "tr",
            "Sırayı belirlemediğin yerlerde hikâyeyi en iyi anlatan sırayı ben seçtim.",
            STORY_TR,
        ),
        (
            "de",
            "Wo du keine Reihenfolge festgelegt hast, habe ich die gewählt, die die Geschichte "
            "am besten erzählt.",
            "wo du nichts gesagt hast, habe ich die Clips in die Reihenfolge gebracht, die die "
            "Geschichte am besten erzählt",
        ),
    ],
)
def test_fm8_an_old_build_and_the_receipt_say_the_order_is_the_ais_in_every_language(
    story_on: None, monkeypatch: pytest.MonkeyPatch, language: str, head: str, reason: str
) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    card, _state_after, after = _story_card()
    brief = incident._brief()
    strategy = incident._drafted(brief)
    stamped = ledger.with_order(incident._snapshot(), after, story=True)
    with reply_language_for(language):
        listing = card_mod.listing_text(card)
        report = incident._report(brief, strategy, snapshot=stamped)
    assert listing.startswith(head), listing[:160]
    assert len(listing.splitlines()) == 1 + len(after)
    assert not _ID_LEAK.search(listing)
    assert incident._rows(report)["r2"]["reason"] == reason


def test_fm1_fm8_without_a_story_order_the_card_listing_and_receipt_are_as_before() -> None:
    """Negative control for FM8 and the flag-off half of FM1: the same draft with no story order
    has no ``story`` key, the old list heading and the old disclosure."""
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    seated = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    card = card_mod.build_card(
        _state(seated),
        snapshot["clip_assignments"],
        events=[],
        brief_version=1,
        applies=True,
        stated=card_mod.stated_ids(strategy),
    )
    assert "story" not in card
    assert card_mod.listing_text(card).startswith("Your clips play in this order:")
    report = incident._report(brief, strategy, snapshot=snapshot)
    assert incident._rows(report)["r2"]["reason"] == incident.DISCLOSURE_EN
    # a creator-moved order (no story) still reads "the order you have them now"
    moved = ledger.with_order(snapshot, [m for m in EXPECTED if m != MAP_CLIP] + [MAP_CLIP])
    assert STORY_SNAPSHOT_KEY not in moved


def test_fm1_an_order_the_plan_was_only_seen_to_lay_out_pins_nothing() -> None:
    """An ``ai_story`` entry with no ``story_ids`` is the pre-existing "observed after a render"
    kind: it must not start stamping the snapshot (byte-identical with the new flag unused)."""
    observed = LedgerEntry(AI_STORY, tuple(ATTACHMENT), tuple(reversed(ATTACHMENT)), "t1")
    state = _state(observed)
    assert state.story_ids == frozenset() and state.basis == "attachment_order"
    snapshot = incident._snapshot()
    assert ledger.with_clip_order_ids(snapshot, state) == snapshot
    assert "story_ids" not in observed.to_payload()
    # a creator entry can never smuggle story ids in
    forged = {**observed.to_payload(), "actor": CREATOR_DRAG, "story_ids": list(ATTACHMENT)}
    assert LedgerEntry.from_payload(forged).story_ids == ()


# ── FM9: the creator's later statement wins ────────────────────────────────────────────────────


def test_fm9_a_drag_after_the_story_order_is_the_creators_and_is_never_moved_again(
    story_on: None,
) -> None:
    brief = _no_order_brief()
    strategy = _plain_strategy()
    snapshot = incident._snapshot()
    plan = _story(strategy, snapshot, brief=brief)
    story = story_order.settle_story(
        plan, list(reversed(ATTACHMENT)), strategy, snapshot, brief=brief
    ).entry
    after = list(story.after_ids)
    pulled = after[10]
    dragged = [pulled, *(m for m in after if m != pulled)]
    state = _state(story, LedgerEntry(CREATOR_DRAG, tuple(after), tuple(dragged), "t2"))
    assert list(state.order) == dragged and state.basis == "confirmed"
    assert state.source_of(pulled) == CREATOR_DRAG and pulled not in state.story_ids
    assert state.source_of(after[0]) == AI_STORY
    assert pulled in story_order.fixed_ids(state, strategy)
    # the snapshot still says the rest is the story's
    stamped = ledger.with_clip_order_ids(snapshot, state)
    assert stamped[SNAPSHOT_KEY] == dragged and stamped[STORY_SNAPSHOT_KEY] is True
    assert _worker_order(brief, strategy, stamped) == dragged


# ── FM10: what the model is shown, and what never leaves it ────────────────────────────────────


def test_fm10_the_model_sees_aliases_and_the_creators_words_never_an_id_or_file_name(
    story_on: None,
) -> None:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    snapshot = incident._snapshot()
    snapshot["clip_assignments"][0]["user_note"] = "IMG_0412.MOV"
    snapshot["clip_assignments"][6]["user_note"] = "the ticket office"
    plan = _story(strategy, snapshot, brief=brief)
    clips, alias_to_id = story_order.sequencer_clips(
        plan, snapshot, groups=ledger.group_words_by_clip(strategy["resolved_clip_intents"])
    )
    assert [c["id"] for c in clips] == [f"c{n}" for n in range(1, 20)]
    assert [alias_to_id[c["id"]] for c in clips] == EXPECTED
    assert clips[0]["span"] is None and clips[1]["span"] == "s1"
    assert clips[1]["group"] == "trene giriş kısımları"
    notes = [c.get("note") for c in clips]
    assert "the ticket office" in notes and "IMG_0412.MOV" not in notes
    agent_input = StorySequencerInput(request="x", clips=clips)
    prompt = StorySequencerAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert not _ID_LEAK.search(prompt), _ID_LEAK.search(prompt)
    assert story_order.ranking_ids(["c2", "nope", "c1"], alias_to_id) == [EXPECTED[1], EXPECTED[0]]
    # the entry holds ids and nothing the model wrote
    result = story_order.settle_story(plan, _reverse_each(plan), strategy, snapshot, brief=brief)
    assert set(result.entry.to_payload()) == {
        "actor",
        "before_ids",
        "after_ids",
        "turn_id",
        "story_ids",
    }


# ── FM11: a clip added later ───────────────────────────────────────────────────────────────────


def test_fm11_a_clip_added_later_is_placed_and_the_creators_clip_still_does_not_move(
    story_on: None,
) -> None:
    brief = _no_order_brief()
    strategy = {**_plain_strategy(), "selected_media_ids": list(ATTACHMENT)}
    first_clips = ATTACHMENT[:-1]
    base = incident._snapshot()
    earlier = {
        **base,
        "clip_assignments": base["clip_assignments"][:-1],
        "clip_paths": base["clip_paths"][:-1],
    }
    early_strategy = {**strategy, "selected_media_ids": list(first_clips)}
    plan = story_order.plan_story(early_strategy, earlier, _state(attach=first_clips), brief=brief)
    assert plan is not None
    story = story_order.settle_story(
        plan, list(reversed(first_clips)), early_strategy, earlier, brief=brief
    ).entry
    placed = list(story.after_ids)
    drag = LedgerEntry(CREATOR_DRAG, tuple(placed), (placed[3], *placed[:3], *placed[4:]), "t2")
    # the 19th clip arrives: it sits last, and it is the only clip nobody has looked at
    state = _state(story, drag)
    newcomer = ATTACHMENT[-1]
    assert list(state.order)[-1] == newcomer
    snapshot = ledger.with_clip_order_ids(base, state)
    again = _story(strategy, snapshot, state, brief=brief)
    assert again is not None and newcomer in again.open_ids
    assert placed[3] in again.fixed
    result = story_order.settle_story(
        again, [newcomer, *again.open_ids], strategy, snapshot, brief=brief
    )
    after = list(result.entry.after_ids)
    assert result.changed and after[0] == placed[3]  # the dragged clip is still first
    assert after[1] == newcomer  # the newcomer leads the open stretch
    assert _worker_order(brief, strategy, result.snapshot) == after


# ── FM1 + FM5: the planner hook, with the provider's answer replayed ───────────────────────────


class _Scripted:
    """The provider transport, replayed: answers every call with one fixed body."""

    def __init__(self, body: str | Exception) -> None:
        self.body, self.calls = body, 0

    def invoke(self, **kwargs: Any):  # noqa: ANN201
        from app.agents._runtime import ModelInvocation

        self.calls += 1
        self.prompt = kwargs.get("prompt")
        if isinstance(self.body, Exception):
            raise self.body
        return ModelInvocation(raw_text=self.body)


class _Db:
    async def rollback(self) -> None:
        return None


def _draft_turn(strategy: dict[str, Any], snapshot: dict[str, Any]) -> PlannedKriaTurn:
    plan = KriaTurnPlan(
        mode="act",
        turn_value="action",
        intents=[
            KriaToolIntent(
                intent_id="apply-strategy",
                tool_name="draft.apply_strategy",
                tool_version=1,
                arguments={"strategy": strategy, "summary": "x"},
            )
        ],
    )
    return PlannedKriaTurn(
        plan=plan, manifest_hash=None, context_hash=None, media_snapshot=snapshot
    )


def _hook(
    monkeypatch: pytest.MonkeyPatch,
    brief: CreativeBrief,
    strategy: dict[str, Any],
    client: _Scripted,
    **turn_kw: Any,
) -> tuple[PlannedKriaTurn, PlannedKriaTurn]:
    async def _latest(_db, _thread):  # noqa: ANN001, ANN202
        return brief

    monkeypatch.setattr(planner_module, "load_latest_brief", _latest)
    monkeypatch.setattr(planner_module, "default_client", lambda: client)
    turn = ledger.TurnClipOrder(
        state=_state(), attachment_ids=tuple(ATTACHMENT), rendered=False, labels={}, **turn_kw
    )
    planned = _draft_turn(strategy, incident._snapshot())
    out = asyncio.run(
        planner_module._with_story_order(
            _Db(),  # type: ignore[arg-type]
            planned,
            turn,
            thread_id=incident.THREAD,  # type: ignore[arg-type]
            creator_id=incident.CREATOR,
            item_id=None,
            creator_text=incident.REQUEST_TR,
        )
    )
    return planned, out


def _answer(order: list[str], decided: bool = True) -> str:
    return json.dumps({"decided": decided, "order": order, "reason": "arrival, ride, views"})


def test_the_planner_hook_writes_one_ai_story_entry_and_the_order_on_the_snapshot(
    story_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = incident._brief()
    strategy = incident._drafted(brief)
    # c1 is the placed map clip; the model ranks c2..c19 back to front
    client = _Scripted(_answer([f"c{n}" for n in range(19, 1, -1)]))
    planned, out = _hook(monkeypatch, brief, strategy, client)
    assert client.calls == 1
    assert incident.REQUEST_TR[:40] in client.prompt and not _ID_LEAK.search(client.prompt)
    [payload] = out.clip_order_entries
    entry = LedgerEntry.from_payload(payload)
    assert entry is not None and entry.actor == AI_STORY and entry.turn_id is None
    after = list(entry.after_ids)
    assert list(entry.before_ids) == EXPECTED and after[0] == MAP_CLIP and after != EXPECTED
    assert out.media_snapshot[SNAPSHOT_KEY] == after
    assert out.media_snapshot[STORY_SNAPSHOT_KEY] is True
    assert "arrival" not in json.dumps(payload)  # FM10: the model's words stay out
    assert _worker_order(brief, strategy, out.media_snapshot) == after
    assert planned.media_snapshot.get(SNAPSHOT_KEY) is None  # the input was not mutated


@pytest.mark.parametrize("flag", ["kria_story_sequencer_enabled", "kria_clip_order_ledger_enabled"])
def test_fm1_with_either_flag_off_the_hook_calls_no_model_and_returns_the_turn_untouched(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setattr(settings, "kria_story_sequencer_enabled", True)
    monkeypatch.setattr(settings, flag, False)
    brief = incident._brief()
    client = _Scripted(_answer([f"c{n}" for n in range(19, 1, -1)]))
    planned, out = _hook(monkeypatch, brief, incident._drafted(brief), client)
    assert client.calls == 0 and out is planned


@pytest.mark.parametrize(
    "body",
    [
        RuntimeError("provider down"),
        "not json at all",
        _answer(["c2", "c3"]),
    ],
    ids=["provider-failure", "not-json", "half-a-ranking"],
)
def test_fm5_a_failed_or_unusable_call_leaves_the_turn_as_it_was(
    story_on: None, monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    brief = incident._brief()
    planned, out = _hook(monkeypatch, brief, incident._drafted(brief), _Scripted(body))
    assert out.clip_order_entries == () and out.media_snapshot == planned.media_snapshot


def test_fm5_cannot_decide_is_remembered_but_changes_no_order(
    story_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = incident._brief()
    client = _Scripted(_answer([], decided=False))
    planned, out = _hook(monkeypatch, brief, incident._drafted(brief), client)
    [payload] = out.clip_order_entries
    assert payload["before_ids"] == payload["after_ids"] == EXPECTED
    assert out.media_snapshot == planned.media_snapshot


def test_fm2_the_hook_asks_nothing_once_the_creator_confirmed_the_order(
    story_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = incident._brief()
    client = _Scripted(_answer([f"c{n}" for n in range(19, 1, -1)]))
    planned, out = _hook(
        monkeypatch, brief, incident._drafted(brief), client, confirmed_ids=tuple(EXPECTED)
    )
    assert client.calls == 0 and out is planned
