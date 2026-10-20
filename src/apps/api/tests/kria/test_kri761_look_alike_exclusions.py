"""KRI-761: "skip the clip where Caro bends and puts the plate down" and its look-alike.

Prod thread A9100235 (Merope café M1, 2026-10-19). The exclude matched TWO clips at the
resolver's 0.80 bar, M1_08 (pinned first by the creator) and M1_05 (the one to drop); the
file-name twin "M1_05 klibini" matched nothing and printed "couldn't tell"; the next
draft resolved neither and M1_05 played.

Ways this could go wrong, written down before the code (each has a case below):

a. A file-name exclude ("M1_05 klibini") stays unmatched: the resolver never sees file
   names, so the clip plays and the reply says "couldn't tell".
b. An unknown or shared file name is guessed instead of left to the resolver.
c. An exclude takes the clip another live ask pins (first/last, a creator label, a clip the
   strategy places) and the creator is never told.
d. Every exclude looks contested because a capture-time order, a describe-the-clips label
   or a group assigns every clip.
e. A plural, clean exclude ("skip the two dark clips") now asks.
f. Look-alikes at the bar do not ask; a single match asks.
g. The picker is asked again after the creator answered (a tap, "none", a pinned clip, a
   file name): a loop.
h. An include answer is reused for an exclude, or the reverse.
i. Carry-forward revives a stale match: an unrelated ask, a clip no longer attached, or
   a turn that already matched on its own.
j. Reading the earlier draft breaks the turn.
k. Flags: exclusions off, or the picker off, change behaviour they should not.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
)
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.clip_exclusions import (
    BORDERLINE_BELOW,
    carry_forward_exclusions,
    contested_exclusions,
    earlier_exclusions,
    exclusion_question,
)
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.models import CreationThread, CreatorEditDraft
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.clip_selection import ClipSelections, SelectionEntry, fold_clip_selections
from tests.kria.test_kri667_clip_exclusions import (
    ALL,
    CASE,
    HALLWAY,
    MANIFEST,
    OUTDOOR,
    _agent_input,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _montage(**update: Any) -> CreativeStrategy:
    return CreativeStrategy.model_validate({"edit_format": "montage", **update})


def _assign(media_id: str, confidence: float, evidence: str = "") -> ClipAssignment:
    return ClipAssignment(media_id=media_id, confidence=confidence, evidence=evidence)


def _exclude(
    *matches: tuple[str, float],
    intent_id: str = "skip-bending",
    attribute: str = "Caro bends and puts the plate on the table",
    evidence: str = "",
) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="exclude",
        attribute=attribute,
        status="resolved",
        assignments=[_assign(m, c, evidence) for m, c in matches],
    )


def _pin(media_id: str, position: str = "first") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=f"order-{position}",
        op="order",
        position=position,  # type: ignore[arg-type]
        attribute="the tray clip",
        status="resolved",
        assignments=[_assign(media_id, 1.0, "creator selected")],
    )


def _label(media_id: str, *, creator_text: str | None = "Oblea de la casa") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="label-oblea",
        op="label",
        attribute="the plate held to the lens",
        creator_text=creator_text,
        status="resolved",
        assignments=[
            ClipAssignment(media_id=media_id, value="Oblea", confidence=1.0, evidence="x")
        ],
    )


# -- (c, d, e, f) which excludes are contested -------------------------------------------


def test_an_exclude_that_matches_the_first_pinned_clip_is_contested() -> None:
    # The M1 draft: exclude -> M1_08 (pinned first) and M1_05, both 0.80.
    intents = [_pin("m08"), _exclude(("m08", 0.8), ("m05", 0.8))]
    [asked] = contested_exclusions(intents, _montage(media_scope="all"))
    assert asked.matched == ("m08", "m05")
    assert asked.blocked == ("m08",)
    assert asked.protected == frozenset({"m08"})


def test_a_pinned_clip_is_contested_at_any_confidence() -> None:
    intents = [_pin("m03", "last"), _exclude(("m03", 0.99))]
    assert len(contested_exclusions(intents, _montage(media_scope="all"))) == 1


def test_a_clip_the_creator_labelled_is_a_pin() -> None:
    intents = [_label("m06"), _exclude(("m06", 0.95))]
    [asked] = contested_exclusions(intents, _montage(media_scope="all"))
    assert asked.blocked == ("m06",)


def test_media_the_strategy_places_is_a_pin() -> None:
    # "Hi, I'm Karolina ... heard whole": the montage audio source plays whole.
    strategy = _montage(
        media_scope="all",
        montage_audio={"source_media_ids": ["m09"], "preserve_source_audio": True},
    )
    [asked] = contested_exclusions([_exclude(("m09", 0.95))], strategy)
    assert asked.blocked == ("m09",)


def test_groups_orders_by_capture_time_and_clip_descriptions_are_not_pins() -> None:
    # d: these assign many or every clip; none of them is the creator pinning one clip.
    every_clip = [_assign(m, 0.9) for m in ("a", "b", "c")]
    not_pins = [
        ResolvedClipIntent(
            intent_id="filmed",
            op="order",
            attribute="the order I filmed them",
            order_by="capture_time",
            status="resolved",
            assignments=every_clip,
        ),
        _label("b", creator_text=None),  # a model-written description label
        ResolvedClipIntent(
            intent_id="kitchen",
            op="group",
            attribute="kitchen",
            status="resolved",
            assignments=every_clip,
        ),
        ResolvedClipIntent(
            intent_id="keep-b",
            op="include",
            attribute="b",
            status="resolved",
            assignments=[_assign("b", 0.9)],
        ),
    ]
    assert contested_exclusions([*not_pins, _exclude(("b", 0.95))], _montage()) == []


def test_look_alikes_at_the_bar_ask_and_clean_plural_matches_do_not() -> None:
    strategy = _montage(media_scope="all")
    assert BORDERLINE_BELOW > 0.8
    # f: two clips only just over the bar, nothing pinned: the resolver cannot tell them apart.
    [asked] = contested_exclusions([_exclude(("m08", 0.8), ("m05", 0.8))], strategy)
    assert asked.blocked == () and asked.matched == ("m08", "m05")
    # e: "skip the two very dark clips": both clean matches, nothing to ask.
    assert contested_exclusions([_exclude(("a", 0.95), ("b", 0.9))], strategy) == []
    # f: one borderline match is a confident enough single clip.
    assert contested_exclusions([_exclude(("a", 0.8))], strategy) == []


@pytest.mark.parametrize("evidence", ["creator selected", "file name"])
def test_what_the_creator_tapped_or_named_is_never_asked_again(evidence: str) -> None:
    # g: a tap or a file name is the creator's own word, even on a pinned clip (pin wins).
    intents = [_pin("m08"), _exclude(("m08", 1.0), ("m05", 1.0), evidence=evidence)]
    assert contested_exclusions(intents, _montage()) == []


def test_an_unsettled_or_empty_exclude_is_never_contested() -> None:
    pending = _exclude(("m08", 0.8), ("m05", 0.8)).model_copy(update={"status": "needs_creator"})
    assert contested_exclusions([_pin("m08"), pending, _exclude()], _montage()) == []


# -- the picker question -----------------------------------------------------------------


def _clips(*ids: str) -> list[IntentClip]:
    return [IntentClip(media_id=m, kind="video", analysis=None) for m in ids]


def test_the_question_offers_every_clip_but_a_pinned_one_and_suggests_the_matches() -> None:
    intents = [_pin("m08"), _exclude(("m08", 0.8), ("m05", 0.8))]
    asked = contested_exclusions(intents, _montage())
    text, question = exclusion_question(asked, _clips("m02", "m05", "m08", "m12"))
    [category] = question["categories"]
    assert category["op"] == "exclude"
    assert category["key"] == "exclude:caro-bends-and-puts-the-plate-on-the-table"
    assert category["intent_id"] == "skip-bending"
    assert category["candidate_media_ids"] == ["m02", "m05", "m12"]  # never the pinned clip
    assert category["suggested_media_ids"] == ["m05"]
    assert question["allow_none"] is True
    assert "Caro bends and puts the plate on the table" in text
    assert "leave out" in text.lower()


def test_the_question_is_turkish_in_a_turkish_chat() -> None:
    asked = contested_exclusions([_exclude(("a", 0.8), ("b", 0.8))], _montage())
    token = bind_reply_language("tr")
    try:
        text, _question = exclusion_question(asked, _clips("a", "b", "c"))
    finally:
        release_reply_language(token)
    assert "çıkar" in text


def test_several_contested_excludes_share_one_question() -> None:
    first = _exclude(("a", 0.8), ("b", 0.8), intent_id="skip-one", attribute="first look-alike")
    second = _exclude(("b", 0.8), ("c", 0.8), intent_id="skip-two", attribute="second look-alike")
    asked = contested_exclusions([first, second], _montage())
    text, question = exclusion_question(asked, _clips("a", "b", "c"))
    assert [c["intent_id"] for c in question["categories"]] == ["skip-one", "skip-two"]
    assert "first look-alike" in text and "second look-alike" in text


def test_nothing_to_pick_from_asks_nothing() -> None:
    # Every clip is pinned: no tappable candidate, so the pin simply wins (today's behaviour).
    asked = contested_exclusions([_pin("a"), _exclude(("a", 0.8))], _montage())
    assert exclusion_question(asked, _clips("a")) is None


# -- (i) carrying an earlier match forward -----------------------------------------------


def test_an_exclude_that_came_back_empty_keeps_its_earlier_match() -> None:
    # Draft 2 of the M1 run: the same ask, reworded id, resolved to [] .
    earlier = [
        _exclude(
            ("m05", 0.9),
            intent_id="exclude_caro_bending_down",
            attribute="Caro'nun eğilip tabağı masaya bıraktığı klibi",
        )
    ]
    now = [
        _exclude(
            intent_id="exclude_caro_leaning",
            attribute="Caro'nun eğilip tabağı masaya bıraktığı klip",
        )
    ]
    out, carried = carry_forward_exclusions(now, earlier, clip_ids={"m05", "m08"})
    assert carried == 1
    [intent] = out
    assert [a.media_id for a in intent.assignments] == ["m05"]
    assert intent.assignments[0].evidence == "earlier draft"


def test_carry_forward_uses_the_same_id_even_when_the_words_changed() -> None:
    earlier = [_exclude(("m05", 0.9), intent_id="skip-laundry", attribute="laundry clip")]
    now = [_exclude(intent_id="skip-laundry", attribute="the washing machine one")]
    _out, carried = carry_forward_exclusions(now, earlier, clip_ids={"m05"})
    assert carried == 1


def test_carry_forward_never_revives_an_unrelated_ask_or_a_gone_clip() -> None:
    earlier = [_exclude(("m05", 0.9), intent_id="skip-laundry", attribute="laundry clip")]
    unrelated = [_exclude(intent_id="skip-beach", attribute="the beach video")]
    assert carry_forward_exclusions(unrelated, earlier, clip_ids={"m05"})[1] == 0
    # The clip was removed from the chat since: nothing to carry.
    same = [_exclude(intent_id="skip-laundry", attribute="laundry clip")]
    assert carry_forward_exclusions(same, earlier, clip_ids={"m09"})[1] == 0


def test_carry_forward_leaves_a_turn_that_matched_on_its_own() -> None:
    earlier = [_exclude(("m05", 0.9), intent_id="skip-laundry", attribute="laundry clip")]
    now = [_exclude(("m07", 0.95), intent_id="skip-laundry", attribute="laundry clip")]
    out, carried = carry_forward_exclusions(now, earlier, clip_ids={"m05", "m07"})
    assert carried == 0
    assert [a.media_id for a in out[0].assignments] == ["m07"]


def test_carry_forward_ignores_other_ops_and_unresolved_exclusions() -> None:
    other = _pin("m05")
    pending = _exclude(("m05", 0.9), intent_id="skip-x", attribute="x clip").model_copy(
        update={"status": "needs_creator"}
    )
    now = [_exclude(intent_id="skip-x", attribute="x clip")]
    assert carry_forward_exclusions(now, [other, pending], clip_ids={"m05"})[1] == 0


def test_earlier_exclusions_read_a_draft_snapshot_and_fail_open() -> None:
    snapshot = {
        "kind": "strategy",
        "strategy": {
            "resolved_clip_intents": [
                json.loads(_exclude(("m05", 0.9)).model_dump_json()),
                json.loads(_pin("m08").model_dump_json()),
                {"op": "exclude", "not": "an intent"},
            ]
        },
    }
    assert [i.op for i in earlier_exclusions(snapshot)] == ["exclude"]
    for junk in (None, {}, {"strategy": None}, {"strategy": {"resolved_clip_intents": "x"}}, 7):
        assert earlier_exclusions(junk) == []  # j


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _fake_db(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(begin_nested=lambda: _Savepoint(), rollback=AsyncMock(), **kwargs)


async def test_the_planner_reads_the_head_draft_and_never_fails_the_turn() -> None:
    head = {
        "strategy": {
            "resolved_clip_intents": [json.loads(_exclude(("m05", 0.9)).model_dump_json())]
        }
    }
    result = SimpleNamespace(scalar_one_or_none=lambda: head)
    db = _fake_db(execute=AsyncMock(return_value=result))
    [intent] = await planner._earlier_exclusions(db, uuid.uuid4())
    assert intent.op == "exclude"
    db.rollback.assert_awaited_once()  # the read does not pin a connection
    # j: a database error, or a db that is not one, yields no earlier match and no rollback
    # of the turn's own work.
    broken = _fake_db(execute=AsyncMock(side_effect=RuntimeError("boom")))
    assert await planner._earlier_exclusions(broken, uuid.uuid4()) == []
    broken.rollback.assert_not_awaited()
    assert await planner._earlier_exclusions(SimpleNamespace(), uuid.uuid4()) == []


async def test_the_head_draft_in_a_real_database_feeds_the_earlier_match() -> None:
    # The query itself, over real rows: only the head draft of the item is read.
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        for revision, clip, head in ((1, "m-old", False), (2, "m05", True)):
            document = KriaDraftDocument(
                kind="strategy",
                strategy={
                    "edit_format": "montage",
                    "target_duration_s": 20,
                    "resolved_clip_intents": [json.loads(_exclude((clip, 0.9)).model_dump_json())],
                },
                intent="skip the clip",
            )
            body, digest = canonical_snapshot(document)
            db.add(
                CreatorEditDraft(
                    creator_id=user_id,
                    thread_id=thread_id,
                    item_id=item_id,
                    variant_key="first",
                    draft_revision=revision,
                    snapshot_json=body,
                    snapshot_hash=digest,
                    is_head=head,
                )
            )
            db.flush()
        db.commit()
    engine = create_async_engine(
        settings.database_url.replace("postgresql://", "postgresql+asyncpg://"), poolclass=NullPool
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            [intent] = await planner._earlier_exclusions(db, item_id)
            assert await planner._earlier_exclusions(db, uuid.uuid4()) == []
    finally:
        await engine.dispose()
    assert [a.media_id for a in intent.assignments] == ["m05"]


# -- (a, b, g, h) resolution: file names and picker answers settle an exclude -------------


def _named_clip(media_id: str, name: str, subject: str = "café") -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis={"subject": subject},
        gcs_path=f"users/u/clips/{media_id}.mp4",
        file_name=name,
    )


CAFE = [_named_clip("c05", "M1_05"), _named_clip("c08", "M1_08"), _named_clip("c12", "M1_12")]


def _planner_returns(monkeypatch, attribute: str) -> None:
    raw = {
        "intents": [
            {
                "intent_id": "exclude_m1_05",
                "op": "exclude",
                "attribute": attribute,
                "source_quote": attribute,
            }
        ],
        "question": None,
    }

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(json.dumps(raw), input)

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _run)


def _resolver_spy(monkeypatch, output: ClipRequestResolverOutput | None = None) -> list:
    seen: list = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen.append(input)
        return output or ClipRequestResolverOutput(intents=[])

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _run)
    return seen


async def _plan(monkeypatch, attribute: str, clips=CAFE, **kwargs: Any) -> PlannedIntentResolution:
    _planner_returns(monkeypatch, attribute)
    return await planning.plan_and_resolve_clip_intents(
        creator_request=f"{attribute} kullanma",
        latest_user_message=f"{attribute} kullanma",
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
        **kwargs,
    )


async def test_a_file_name_exclude_is_settled_without_the_resolver(monkeypatch) -> None:
    # a: the exact attribute of the M1 run. The resolver's records never carry file names.
    seen = _resolver_spy(monkeypatch)
    result = await _plan(monkeypatch, "M1_05 klibini")
    [intent] = result.resolution.intents
    assert (intent.op, intent.status, intent.media_ids()) == ("exclude", "resolved", ["c05"])
    assert intent.assignments[0].evidence == "file name"
    assert seen == []


async def test_an_unknown_file_name_is_left_to_the_resolver(monkeypatch) -> None:
    # b: nothing is guessed from a name the clips do not carry.
    seen = _resolver_spy(monkeypatch)
    result = await _plan(monkeypatch, "M9_99 klibini")
    assert len(seen) == 1
    [intent] = result.resolution.intents
    assert intent.assignments == []


async def test_a_name_two_clips_share_is_not_guessed(monkeypatch) -> None:
    twins = [_named_clip("x1", "M1_05"), _named_clip("x2", "M1_05"), _named_clip("x3", "M1_12")]
    seen = _resolver_spy(monkeypatch)
    result = await _plan(monkeypatch, "M1_05 klibini", clips=twins)
    assert len(seen) == 1
    assert result.resolution.intents[0].assignments == []


async def test_a_description_that_carries_a_file_name_still_goes_to_the_resolver(
    monkeypatch,
) -> None:
    seen = _resolver_spy(monkeypatch)
    await _plan(monkeypatch, "M1_05 where Caro bends over")
    assert len(seen) == 1


async def test_the_creators_tap_settles_an_exclude_onto_the_clips_to_leave_out(
    monkeypatch,
) -> None:
    seen = _resolver_spy(monkeypatch)
    selections = ClipSelections({"exclude:the-bending-clip": SelectionEntry(media_ids=("c05",))})
    result = await _plan(monkeypatch, "the bending clip", clip_selections=selections)
    [intent] = result.resolution.intents
    assert (intent.op, intent.media_ids()) == ("exclude", ["c05"])
    assert intent.assignments[0].evidence == "creator selected"
    assert seen == []


async def test_none_of_them_drops_the_exclude_for_good(monkeypatch) -> None:
    seen = _resolver_spy(monkeypatch)
    selections = ClipSelections({"exclude:the-bending-clip": SelectionEntry(none=True)})
    result = await _plan(monkeypatch, "the bending clip", clip_selections=selections)
    assert result.resolution.intents == []
    assert seen == []


async def test_an_include_answer_is_not_an_exclude_answer_and_the_reverse(monkeypatch) -> None:
    # h
    seen = _resolver_spy(monkeypatch)
    include_answer = ClipSelections(
        {"include:the-bending-clip": SelectionEntry(media_ids=("c05",))}
    )
    result = await _plan(monkeypatch, "the bending clip", clip_selections=include_answer)
    assert len(seen) == 1 and result.resolution.intents[0].assignments == []

    raw = {
        "intents": [
            {
                "intent_id": "keep",
                "op": "include",
                "attribute": "the bending clip",
                "source_quote": "the bending clip",
            }
        ],
        "question": None,
    }

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(json.dumps(raw), input)

    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _run)
    exclude_answer = ClipSelections(
        {"exclude:the-bending-clip": SelectionEntry(media_ids=("c05",))}
    )
    await planning.plan_and_resolve_clip_intents(
        creator_request="keep the bending clip",
        latest_user_message="keep the bending clip",
        candidate_intents=None,
        clips=CAFE,
        run_context=RunContext(),
        clip_selections=exclude_answer,
    )
    assert len(seen) == 2


async def test_the_creators_answer_to_the_question_holds_on_the_next_turn(monkeypatch) -> None:
    # Ask -> tap -> the next turn re-plans the whole request with the ask reworded. The
    # answer binds to the ask (its id), so the clip stays out and nobody is asked again.
    asked = contested_exclusions(
        [
            _pin("c08"),
            _exclude(
                ("c08", 0.8),
                ("c05", 0.8),
                intent_id="exclude_caro_bending_down",
                attribute="Caro'nun eğilip tabağı masaya bıraktığı klibi",
            ),
        ],
        _montage(),
    )
    _text, question = exclusion_question(asked, CAFE)
    [category] = question["categories"]
    events = [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [{"key": category["key"], "media_ids": ["c05"]}],
                }
            },
        ),
    ]
    raw = {
        "intents": [
            {
                "intent_id": "exclude_caro_bending_down",
                "op": "exclude",
                "attribute": "Caro'nun eğilip tabağı masaya bıraktığı klip",
                "source_quote": "Caro'nun eğilip tabağı masaya bıraktığı klibi kullanma",
            }
        ],
        "question": None,
    }

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(json.dumps(raw), input)

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _run)
    seen = _resolver_spy(monkeypatch)
    result = await planning.plan_and_resolve_clip_intents(
        creator_request="Caro'nun eğilip tabağı masaya bıraktığı klibi kullanma",
        latest_user_message="Caro'nun eğilip tabağı masaya bıraktığı klibi kullanma",
        candidate_intents=None,
        clips=CAFE,
        run_context=RunContext(),
        clip_selections=fold_clip_selections(events),
    )
    [intent] = result.resolution.intents
    assert intent.media_ids() == ["c05"] and intent.assignments[0].evidence == "creator selected"
    assert seen == []
    assert contested_exclusions(result.resolution.intents, _montage()) == []


# -- the turn, through the runtime-v2 planner (c, f, g, k) ---------------------------------

CLIP_IDS = list(ALL)
PINNED = HALLWAY
LOOK_ALIKE = OUTDOOR
SKIP_BENDING = "Make a montage of our cabin. Skip the clip where Caro bends down."


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    resolved: list[ResolvedClipIntent],
    *,
    earlier: list[ResolvedClipIntent] | None = None,
    **strategy_update: Any,
) -> Any:
    """One recorded planning turn; only the model text and the clip resolution are stand-ins."""
    agent_input = _agent_input(SKIP_BENDING)
    strategy = {**copy.deepcopy(CASE["model_strategy"]), "opening_title": None, **strategy_update}
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": CASE["model_summary"],
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    requested = [
        ClipIntent.model_validate(i.model_dump(include=set(ClipIntent.model_fields)))
        for i in resolved
    ]
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(
            return_value=PlannedIntentResolution(requested, IntentResolution(intents=resolved))
        ),
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=earlier or []))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(media_id=m["media_id"], kind=m["kind"], analysis=None) for m in MANIFEST["media"]
    ]
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(MANIFEST["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=SKIP_BENDING,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request=SKIP_BENDING,
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _played(planned: Any) -> set[str]:
    strategy = planned.plan.intents[0].arguments["strategy"]
    selected = strategy.get("selected_media_ids") or []
    return set(ALL) if strategy.get("media_scope") == "all" or not selected else set(selected)


async def test_the_m1_draft_asks_which_clip_to_leave_out_instead_of_guessing(
    prod_profile, monkeypatch
) -> None:
    pin = _pin(PINNED).model_copy(update={"attribute": "the tray clip"})
    skip = _exclude((PINNED, 0.8), (LOOK_ALIKE, 0.8))
    planned = await _turn(monkeypatch, [pin, skip])
    assert planned.plan.mode == "respond"
    assert planned.plan.intents == []
    [category] = planned.plan.clip_question["categories"]
    assert category["op"] == "exclude"
    assert PINNED not in category["candidate_media_ids"]
    assert category["suggested_media_ids"] == [LOOK_ALIKE]
    assert "leave out" in planned.plan.response.lower()


async def test_after_the_tap_the_chosen_clip_leaves_and_the_pinned_one_stays(
    prod_profile, monkeypatch
) -> None:
    # g: no second question.
    pin = _pin(PINNED)
    tapped = _exclude((LOOK_ALIKE, 1.0), evidence="creator selected")
    planned = await _turn(monkeypatch, [pin, tapped])
    assert planned.plan.mode == "act"
    played = _played(planned)
    assert LOOK_ALIKE not in played and PINNED in played


async def test_a_pinned_clip_the_creator_tapped_still_plays_and_is_not_asked_again(
    prod_profile, monkeypatch
) -> None:
    pin = _pin(PINNED)
    tapped = _exclude((PINNED, 1.0), evidence="creator selected")
    planned = await _turn(monkeypatch, [pin, tapped])
    assert planned.plan.mode == "act"
    assert PINNED in _played(planned)


async def test_a_clean_plural_exclude_still_drops_both_without_asking(
    prod_profile, monkeypatch
) -> None:
    # e
    planned = await _turn(monkeypatch, [_exclude((PINNED, 0.95), (LOOK_ALIKE, 0.9))])
    assert planned.plan.mode == "act"
    assert not ({PINNED, LOOK_ALIKE} & _played(planned))


async def test_the_picker_flag_off_keeps_todays_silent_pin_wins(prod_profile, monkeypatch) -> None:
    # k
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    pin = _pin(PINNED)
    planned = await _turn(monkeypatch, [pin, _exclude((PINNED, 0.8), (LOOK_ALIKE, 0.8))])
    assert planned.plan.mode == "act"
    played = _played(planned)
    assert PINNED in played and LOOK_ALIKE not in played


async def test_exclusions_off_never_ask_or_carry(prod_profile, monkeypatch) -> None:
    # k
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    pin = _pin(PINNED)
    planned = await _turn(monkeypatch, [pin, _exclude((PINNED, 0.8), (LOOK_ALIKE, 0.8))])
    assert planned.plan.mode == "act"
    assert _played(planned) == set(ALL)


async def test_an_earlier_match_keeps_the_clip_out_when_the_resolver_flips(
    prod_profile, monkeypatch
) -> None:
    # Draft 2 of the M1 run, minus the file name: the ask resolved to nothing this time.
    earlier = [_exclude((LOOK_ALIKE, 0.9))]
    planned = await _turn(monkeypatch, [_exclude()], earlier=earlier)
    assert planned.plan.mode == "act"
    assert LOOK_ALIKE not in _played(planned)
    assert not [n for n in planned.policy_notices if "couldn't tell" in n]


async def test_a_file_name_exclude_that_matched_never_prints_the_could_not_tell_notice(
    prod_profile, monkeypatch
) -> None:
    named = _exclude(
        (LOOK_ALIKE, 1.0),
        intent_id="exclude_m1_05",
        attribute="M1_05 klibini",
        evidence="file name",
    )
    planned = await _turn(monkeypatch, [named])
    assert LOOK_ALIKE not in _played(planned)
    assert not [n for n in planned.policy_notices if "couldn't tell" in n]
    assert "couldn't tell" not in planned.plan.intents[0].arguments["summary"]


async def test_a_skip_nothing_matched_still_says_so(prod_profile, monkeypatch) -> None:
    # KRI-680 stays: with no earlier match either, the unmatched ask is still reported.
    planned = await _turn(monkeypatch, [_exclude()])
    assert [n for n in planned.policy_notices if "couldn't tell" in n]
