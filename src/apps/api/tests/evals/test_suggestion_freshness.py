"""Freshness of the Improve cards: when a set is recomputed, and what never comes back.

Free and offline: a fake Redis with a manual clock, a fake Celery ``apply_async``, a fake database
that answers by table name, and the committed ``suggestion_chips`` fixtures for the contexts.
``test_suggestion_freshness_postgres.py`` runs the ring writes against a real database.

Ways this can fail, each pinned below (written before the code):

1. Flag off is not byte-identical: a broker message, a digest, a ring entry or a wire byte differs
   (``test_flag_off_*``).
2. A save during a running turn or render generates anyway, or loses the refresh
   (``test_busy_*``): the worker must look again later, a bounded number of times.
3. Rapid saves enqueue one run each (``test_debounce_*``): one run per window, the first call
   enqueues, the rest are covered by it, and the next window starts a new one.
4. The debounce slot outlives its window, or a Redis outage loses the refresh entirely
   (``test_debounce_*``, ``test_redis_outage_*``).
5. The GET and the worker key the cache differently, so the picks the worker cached are never
   found (``test_the_digest_*``: they share ``plan_for``; the key moves with each input it names
   and with nothing else).
6. A dismissed or applied card comes back in the next set: the card ids change with every edit, so
   the memory has to be the kind and target (``test_dismissed_*``, ``test_applied_*``).
7. The memory is lost when the ring rolls over (``test_the_memory_outlives_the_ring``).
8. A dismiss edits the served set (it is frozen per digest): the filter runs on read and the
   stored set is untouched (``test_the_filter_*``).
9. A dismiss of an unknown or foreign id writes something, or fails (``test_dismiss_*``;
   the foreign-thread 404 and the busy row are in the Postgres file).
10. A trigger fires inside a row lock or raises into the save (``test_triggers_never_raise``).
"""

from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import suggestion_chips as sc
from app.services import suggestion_serve as serve
from app.services import suggestion_store as store
from app.services.suggestions.models import (
    ChipAnchorOut,
    EditorOpsAction,
    Suggestion,
    SuggestionOut,
)
from app.services.suggestions.pinned import ring_suggestions
from tests.evals import suggestion_chips_harness as h

# ── Fakes ────────────────────────────────────────────────────────────────────────────


class FakeRedis:
    """Just enough Redis for the store: SET NX EX with a manual clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.data: dict[str, tuple[str, float | None]] = {}
        self.down = False

    def _live(self, key: str) -> bool:
        item = self.data.get(key)
        if item is None:
            return False
        if item[1] is not None and item[1] <= self.now:
            del self.data[key]
            return False
        return True

    def set(self, key, value, nx=False, ex=None):  # noqa: ANN001, ANN201
        if self.down:
            raise ConnectionError("redis down")
        if nx and self._live(key):
            return None
        self.data[key] = (value, self.now + ex if ex else None)
        return True

    def get(self, key):  # noqa: ANN001, ANN201
        if self.down:
            raise ConnectionError("redis down")
        return self.data[key][0] if self._live(key) else None


class Broker:
    """Replaces ``kria_suggester.apply_async`` and records every message."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    def apply_async(self, *, kwargs, **options):  # noqa: ANN001, ANN201
        if self.fail:
            raise RuntimeError("broker down")
        self.sent.append({"kwargs": kwargs, **options})


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(store, "_client", fake)
    return fake


@pytest.fixture
def broker(monkeypatch):
    from app.tasks import kria_suggester as task

    fake = Broker()
    monkeypatch.setattr(task.kria_suggester, "apply_async", fake.apply_async)
    return fake


@pytest.fixture
def flags(monkeypatch):
    """Chips on for everyone, the agent on, freshness triggers on, 20 s debounce."""
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])
    monkeypatch.setattr(settings, "kria_suggester_max_runs_per_thread", 12)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_refresh_debounce_s", 20)


# ── 1. Flag off is byte-identical ────────────────────────────────────────────────────


def test_flag_off_the_broker_message_is_exactly_what_it_was(broker, redis, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    assert serve.enqueue_suggester("t-1", trigger="lazy", expected_digest="d") is True
    assert broker.sent == [
        {
            "kwargs": {
                "thread_id": "t-1",
                "trigger": "lazy",
                "expected_digest": "d",
                "language": None,
                "protocol": None,
            },
            "queue": "agent-control",
            "expires": 600,
        }
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda: serve.refresh_for_thread("t-1", trigger="draft_commit"),
        lambda: serve.refresh_for_item("00000000-0000-0000-0000-000000000001", trigger="x"),
        lambda: serve.on_signals_computed("00000000-0000-0000-0000-000000000001"),
    ],
    ids=["thread", "item", "signals"],
)
def test_flag_off_no_trigger_touches_redis_the_database_or_the_broker(
    call, broker, redis, monkeypatch
):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)

    def explode(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("flag off must not even look")

    monkeypatch.setattr(serve, "_active_threads", explode)
    monkeypatch.setattr(store, "claim_refresh_slot", explode)
    assert not call()
    assert broker.sent == [] and redis.data == {}


async def test_flag_off_the_async_save_hook_does_not_start_a_thread(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)

    def explode(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("flag off must not even look")

    monkeypatch.setattr(serve.asyncio, "to_thread", explode)
    assert await serve.arefresh_for_item("00000000-0000-0000-0000-000000000001", trigger="s") == 0


FIRST_PROMPT = "fp_speech_and_broll"
READY = "rd_plain_montage"


class FakeDb:
    """Answers the two freshness reads by table name."""

    def __init__(self, head=None, brief=None, fail=False):  # noqa: ANN001
        self.head, self.brief, self.fail = head, brief, fail
        self.statements: list[str] = []

    async def execute(self, statement):  # noqa: ANN001, ANN201
        text = str(statement)
        self.statements.append(text)
        if self.fail:
            raise RuntimeError("db down")
        value = self.head if "creator_edit_drafts" in text else self.brief
        return SimpleNamespace(scalar_one_or_none=lambda: value)


def _ctx(name):  # noqa: ANN001, ANN202
    return h.build_context(h.case_by_name(name))


def _thread():  # noqa: ANN202
    return SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa")


async def test_flag_off_the_digest_is_the_legacy_one_and_no_query_runs(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    ctx = _ctx(READY)
    db = FakeDb(head="h1", brief=3)
    plan = await serve.plan_for(db, _thread(), ctx)
    assert plan == sc.build_candidates(ctx)
    assert db.statements == []


# ── 5. The digest ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [FIRST_PROMPT, READY])
async def test_the_digest_moves_with_each_input_it_names_and_with_nothing_else(name, flags):
    ctx = _ctx(name)
    base = await serve.plan_for(_thread_db := FakeDb(head="h1", brief=3), _thread(), ctx)
    again = await serve.plan_for(FakeDb(head="h1", brief=3), _thread(), ctx)
    assert (base.input_digest, base.set_id) == (again.input_digest, again.set_id), "deterministic"
    assert base.input_digest != sc.build_candidates(ctx).input_digest
    assert base.set_id == sc._set_id(ctx.thread_id, base.input_digest)
    assert len(_thread_db.statements) == 2, "one read for the head draft, one for the brief"
    moved = {
        "a draft revision": FakeDb(head="h2", brief=3),
        "a brief version": FakeDb(head="h1", brief=4),
        "the first draft": FakeDb(head=None, brief=3),
    }
    for label, db in moved.items():
        plan = await serve.plan_for(db, _thread(), ctx)
        assert plan.input_digest != base.input_digest, label
    if ctx.snapshot is not None:
        saved = copy.deepcopy(dict(ctx.snapshot))
        saved["slots"] = [*saved.get("slots", []), {"slot_id": "s-new"}]
        plan = await serve.plan_for(
            FakeDb(head="h1", brief=3), _thread(), dataclasses.replace(ctx, snapshot=saved)
        )
        assert plan.input_digest != base.input_digest, "a native Save changes the Job, no draft"
    # what the candidates themselves do not read stays out of the key
    assert (
        await serve.plan_for(FakeDb(head="h1", brief=3), _thread(), ctx)
    ).default == base.default


async def test_the_digest_keeps_the_legacy_key_when_the_read_fails(flags):
    ctx = _ctx(READY)
    plan = await serve.plan_for(FakeDb(fail=True), _thread(), ctx)
    assert plan == sc.build_candidates(ctx)


async def test_a_thread_with_nothing_to_offer_keeps_its_set_id(flags):
    ctx = h.build_context(h.case_by_name("none_open_question"))
    db = FakeDb(head="h1", brief=1)
    plan = await serve.plan_for(db, _thread(), ctx)
    assert plan == sc.build_candidates(ctx) and db.statements == []


# ── 3, 4. Debounce ───────────────────────────────────────────────────────────────────


def test_debounce_one_run_per_window(flags, broker, redis):
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is True
    for _ in range(5):  # rapid saves
        assert serve.refresh_for_thread("t-1", trigger="editor_save") is False
    assert len(broker.sent) == 1
    sent = broker.sent[0]
    assert sent["countdown"] == 20, "the run starts after the quiet period and reads the latest"
    assert sent["kwargs"]["trigger"] == "editor_save" and "attempt" not in sent["kwargs"]
    assert serve.refresh_for_thread("t-2", trigger="draft_commit") is True, "per thread"
    redis.now += 21
    assert serve.refresh_for_thread("t-1", trigger="draft_commit") is True, "the next window"
    assert len(broker.sent) == 3


def test_debounce_zero_means_no_slot_and_no_countdown(flags, broker, redis, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_refresh_debounce_s", 0)
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is True
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is True
    assert all("countdown" not in m for m in broker.sent) and redis.data == {}


def test_redis_outage_still_enqueues_without_coalescing(flags, broker, redis):
    redis.down = True
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is True
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is True
    assert len(broker.sent) == 2


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        (lambda m: m.setattr(settings, "kria_suggestion_chips_enabled", False), "chips off"),
        (
            lambda m: m.setattr(settings, "kria_suggester_max_runs_per_thread", 0),
            "agent kill lever",
        ),
    ],
    ids=["chips_off", "agent_off"],
)
def test_debounce_slot_is_not_spent_when_nothing_is_enqueued(
    flags, broker, redis, monkeypatch, setup, why
):
    setup(monkeypatch)
    assert serve.refresh_for_thread("t-1", trigger="editor_save", creator_id="u-1") is False, why
    assert broker.sent == []


def test_cohort_account_is_refreshed_while_the_global_flag_is_off(
    flags, broker, redis, monkeypatch
):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", ["u-canary"])
    assert serve.refresh_for_thread("t-1", trigger="editor_save", creator_id="u-other") is False
    assert serve.refresh_for_thread("t-2", trigger="editor_save", creator_id="u-canary") is True


# ── 10. Triggers never raise ─────────────────────────────────────────────────────────


def test_triggers_never_raise(flags, broker, redis, monkeypatch):
    broker.fail = True
    assert serve.refresh_for_thread("t-1", trigger="editor_save") is False

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("db down")

    monkeypatch.setattr(serve, "_active_threads", boom)
    assert serve.refresh_for_item("00000000-0000-0000-0000-000000000001", trigger="s") == 0
    assert serve.refresh_for_item("not-a-uuid", trigger="s") == 0


async def test_the_async_hook_runs_the_lookup_off_the_event_loop(flags, broker, redis, monkeypatch):
    monkeypatch.setattr(serve, "_active_threads", lambda item_id: [("t-1", "u-1"), ("t-2", "u-2")])
    count = await serve.arefresh_for_item("00000000-0000-0000-0000-000000000001", trigger="s")
    assert count == 2 and {m["kwargs"]["thread_id"] for m in broker.sent} == {"t-1", "t-2"}


# ── 2. The worker never generates while a turn or render runs ────────────────────────


def _run_task(monkeypatch, prepared, **kwargs):  # noqa: ANN001, ANN202
    from app.tasks import kria_suggester as task

    async def fake_prepare(*a, **k):  # noqa: ANN002, ANN003
        fake_prepare.calls.append(k)
        return prepared

    fake_prepare.calls = []
    monkeypatch.setattr(task, "_prepare", fake_prepare)
    return task.kria_suggester.run("t-1", **kwargs), fake_prepare


def test_busy_a_refresh_looks_again_later_with_a_longer_attempt(flags, broker, monkeypatch):
    out, prepare = _run_task(monkeypatch, "busy", trigger="editor_save", attempt=2)
    assert out["status"] == "busy_deferred"
    assert prepare.calls == [{"defer_when_busy": True}]
    assert len(broker.sent) == 1
    sent = broker.sent[0]
    assert sent["countdown"] == 20 and sent["kwargs"]["attempt"] == 3
    assert sent["kwargs"]["trigger"] == "editor_save"


def test_busy_the_countdown_never_drops_below_the_floor(flags, broker, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_refresh_debounce_s", 0)
    _run_task(monkeypatch, "busy", trigger="draft_commit")
    assert broker.sent[0]["countdown"] == serve.BUSY_RETRY_MIN_S


def test_busy_gives_up_after_the_bounded_number_of_looks(flags, broker, monkeypatch):
    out, _ = _run_task(monkeypatch, "busy", trigger="editor_save", attempt=serve.BUSY_DEFERRALS_MAX)
    assert out["status"] == "busy_gave_up" and broker.sent == []


def test_busy_only_the_freshness_triggers_ask_for_the_check(flags, broker, monkeypatch):
    _, prepare = _run_task(monkeypatch, "ignored", trigger="render_ready")
    assert prepare.calls == [{}]
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    _, prepare = _run_task(monkeypatch, "ignored", trigger="editor_save")
    assert prepare.calls == [{}], "flag off: the run is what it was"


# ── Ring memory ──────────────────────────────────────────────────────────────────────


def _card(card_id, kind, *, media_id=None, ordinal=None, rank=1):  # noqa: ANN001, ANN202
    return Suggestion(
        id=card_id,
        kind=kind,
        title=kind,
        prompt=f"do {kind}",
        rank=rank,
        anchor=ChipAnchorOut(media_id=media_id, clip_ordinal=ordinal),
        action=EditorOpsAction(ops=[{"op": "set_caption_meta", "enabled": True}], base_digest="d"),
    )


def _entry(set_id, cards, **extra):  # noqa: ANN001, ANN202
    return {
        "set_id": set_id,
        "state": "ready",
        "final": True,
        "chips": [],
        "suggestions": [c.model_dump(mode="json") for c in cards],
        **extra,
    }


MUTE_3 = _card("ss_mute3", "mute_clip", media_id="m3", ordinal=3)
CAPTIONS = _card("ss_caps", "captions", rank=2)


def test_the_fingerprint_follows_the_target_not_the_card_id():
    mute_later = _card("ss_other_id_after_an_edit", "mute_clip", media_id="m3", ordinal=1)
    assert serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3}) == (
        serve.fingerprint(mute_later.kind, mute_later.anchor.model_dump(mode="json"))
    ), "the ordinal moves when a clip is removed; the media id does not"
    assert serve.fingerprint("mute_clip", {"media_id": "m4"}) != serve.fingerprint(
        "mute_clip", {"media_id": "m3"}
    )
    assert serve.fingerprint("sfx", {"media_id": "m3"}) != serve.fingerprint(
        "mute_clip", {"media_id": "m3"}
    )
    assert serve.fingerprint("mute_clip", {"clip_ordinal": 2}) != serve.fingerprint(
        "mute_clip", {"clip_ordinal": 3}
    ), "without a media id the ordinal is all there is"


def test_dismiss_marks_the_entry_and_the_bounded_list():
    state = {"intent": "keep", sc.RING_KEY: [_entry("sg_1", [MUTE_3, CAPTIONS])]}
    new, outcome = serve.apply_dismiss(state, "sg_1", "ss_mute3")
    assert outcome == "dismissed" and new is not None
    fp = serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3})
    assert new[sc.RING_KEY][0][serve.DISMISSED_FIELD] == {"ss_mute3": fp}
    assert new[serve.SUPPRESSED_KEY] == [fp] and new["intent"] == "keep"
    assert state[sc.RING_KEY][0].get(serve.DISMISSED_FIELD) is None, "the input is not mutated"
    again, outcome = serve.apply_dismiss(new, "sg_1", "ss_mute3")
    assert (again, outcome) == (None, "already_dismissed"), "idempotent: nothing to write"


@pytest.mark.parametrize(
    ("state", "set_id", "card_id"),
    [
        ({}, "sg_1", "ss_mute3"),
        ({sc.RING_KEY: []}, "sg_1", "ss_mute3"),
        ({sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}, "sg_1", "ss_nope"),
        ({sc.RING_KEY: [{"set_id": "sg_1", "chips": [{"id": "sc_x"}]}]}, "sg_1", "sc_x"),
        ({sc.RING_KEY: "garbage"}, "sg_1", "ss_mute3"),
        ({sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}, "", ""),
    ],
    ids=["no_ring", "empty_ring", "unknown_id", "legacy_chip_id", "corrupt_ring", "blank"],
)
def test_dismiss_of_an_unknown_id_writes_nothing(state, set_id, card_id):
    assert serve.apply_dismiss(state, set_id, card_id) == (None, "unknown")


def test_dismiss_finds_the_card_after_the_set_changed_under_the_client():
    """GET returned sg_1; a save produced sg_2 (same card id, same kind and target) before the
    dismiss arrived. The id is the key; the set id is a hint."""
    state = {sc.RING_KEY: [_entry("sg_2", [MUTE_3, CAPTIONS])]}
    new, outcome = serve.apply_dismiss(state, "sg_1", "ss_mute3")
    assert outcome == "dismissed" and new[sc.RING_KEY][0][serve.DISMISSED_FIELD]


def test_dismiss_prefers_the_set_the_client_named():
    old = _entry("sg_1", [MUTE_3])
    new_entry = _entry("sg_2", [MUTE_3])
    new, _ = serve.apply_dismiss({sc.RING_KEY: [old, new_entry]}, "sg_1", "ss_mute3")
    assert serve.DISMISSED_FIELD in new[sc.RING_KEY][0]
    assert serve.DISMISSED_FIELD not in new[sc.RING_KEY][1]


def test_the_list_is_bounded_and_keeps_the_newest():
    state: dict = {}
    for n in range(40):
        state = serve._with_suppressed(state, [f"fp{n}"]) or state
    kept = state[serve.SUPPRESSED_KEY]
    assert len(kept) == 32 and kept[-1] == "fp39", "a thread's state never grows without bound"
    assert serve._with_suppressed(state, ["fp39"]) is None, "already there: nothing to write"


def test_applied_cards_are_suppressed_through_the_consumed_mark():
    entry = _entry("sg_1", [MUTE_3, CAPTIONS], consumed={"ss_caps": "turn-1"})
    state = {sc.RING_KEY: [entry]}
    caps_fp = serve.fingerprint("captions", {})
    assert serve.suppressed_set(state) == {caps_fp}
    folded = serve.fold_suppressed(state)
    assert folded[serve.SUPPRESSED_KEY] == [caps_fp]
    assert serve.fold_suppressed(folded) is None, "folding twice writes nothing"


def test_the_memory_outlives_the_ring():
    """The ring keeps 5 sets. The dismissed card's entry is long gone; the card still stays out."""
    state = {sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}
    state, _ = serve.apply_dismiss(state, "sg_1", "ss_mute3")
    state[sc.RING_KEY] = [_entry(f"sg_{n}", [CAPTIONS]) for n in range(2, 7)]
    assert all(serve.DISMISSED_FIELD not in e for e in state[sc.RING_KEY])
    assert serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3}) in (
        serve.suppressed_set(state)
    )


def test_attach_cards_is_idempotent_and_leaves_other_entries_alone():
    bare = {"set_id": "sg_2", "chips": [], "final": True}
    state = {sc.RING_KEY: [_entry("sg_1", [CAPTIONS]), bare]}
    cards = ring_suggestions(
        SimpleNamespace(suggestions=[MUTE_3])  # type: ignore[arg-type]
    )
    new = serve.attach_cards(state, "sg_2", cards)
    assert new[sc.RING_KEY][1]["suggestions"] == cards
    assert new[sc.RING_KEY][0] == state[sc.RING_KEY][0]
    assert serve.attach_cards(new, "sg_2", cards) is None, "already has them"
    assert serve.attach_cards(state, "sg_missing", cards) is None


# ── 8. The filter runs on read; the frozen set is untouched ──────────────────────────


def _served(cards):  # noqa: ANN001, ANN202
    wire = [
        SuggestionOut(
            id=c.id,
            kind=c.kind,
            title=c.title,
            prompt=c.prompt,
            rank=i + 1,
            anchor=c.anchor,
            action_type="editor_ops",
        )
        for i, c in enumerate(cards)
    ]
    out = sc.SuggestionSetOut(set_id="sg_2", state="ready", status="ok", suggestions=wire)
    return serve.Served(out=out, media_digest="d", input_digest="i")


def test_the_filter_hides_dismissed_and_applied_cards_and_renumbers():
    third = _card("ss_third", "sfx", rank=3)
    frozen = _served([MUTE_3, CAPTIONS, third])
    original = frozen.out.model_dump(mode="json")
    state = {
        sc.RING_KEY: [_entry("sg_1", [MUTE_3, CAPTIONS], consumed={"ss_caps": "t"})],
        serve.SUPPRESSED_KEY: [],
    }
    state, _ = serve.apply_dismiss(state, "sg_1", "ss_mute3")
    out = serve._without_suppressed(frozen, SimpleNamespace(state=state))
    assert [(s.id, s.rank) for s in out.out.suggestions] == [("ss_third", 1)]
    assert out.unfiltered.model_dump(mode="json") == original, "the frozen set is not edited"
    assert out.out.set_id == "sg_2", "same set, fewer cards"


def test_the_filter_survives_a_new_digest_because_ids_are_not_the_key():
    """After a save the same card has a new id in a new set; it is still hidden."""
    reoffered = _card("ss_brand_new_id", "mute_clip", media_id="m3", ordinal=2)
    state, _ = serve.apply_dismiss({sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}, "sg_1", "ss_mute3")
    out = serve._without_suppressed(_served([reoffered, CAPTIONS]), SimpleNamespace(state=state))
    assert [s.kind for s in out.out.suggestions] == ["captions"]


def test_the_filter_is_a_no_op_without_marks():
    served = _served([MUTE_3, CAPTIONS])
    before = served.out.model_dump(mode="json")
    out = serve._without_suppressed(served, SimpleNamespace(state={"intent": "x"}))
    assert out.out.model_dump(mode="json") == before and out.needs_fold is False


def test_the_last_card_handled_does_not_bring_its_chip_back_as_the_fallback():
    chip = sc.SuggestionChipOut(id="c1", kind="mute_clip", rank=1, anchor=MUTE_3.anchor)
    other = sc.SuggestionChipOut(id="c2", kind="sfx", rank=2)
    served = _served([MUTE_3])
    served.out = served.out.model_copy(update={"chips": [chip, other]})
    state, _ = serve.apply_dismiss({sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}, "sg_1", "ss_mute3")
    out = serve._without_suppressed(served, SimpleNamespace(state=state)).out
    assert [(c.id, c.rank) for c in out.chips] == [("c2", 1)]


def test_an_empty_filtered_set_serializes_without_the_card_keys():
    state, _ = serve.apply_dismiss({sc.RING_KEY: [_entry("sg_1", [MUTE_3])]}, "sg_1", "ss_mute3")
    out = serve._without_suppressed(_served([MUTE_3]), SimpleNamespace(state=state)).out
    assert "suggestions" not in json.loads(out.model_dump_json())


def test_the_filter_says_when_the_ring_still_lacks_the_cards_it_served():
    served = _served([MUTE_3])
    state = {sc.RING_KEY: [{"set_id": "sg_2", "chips": [], "final": True}]}
    assert serve._without_suppressed(served, SimpleNamespace(state=state)).ring_has_cards is False
    state = {sc.RING_KEY: [_entry("sg_2", [MUTE_3])]}
    assert serve._without_suppressed(_served([MUTE_3]), SimpleNamespace(state=state)).ring_has_cards


# ── 6. Cards stored for a tap are the cards that were served ─────────────────────────


async def test_cards_are_stored_only_when_the_rebuilt_ids_match_the_served_ones(
    flags, redis, monkeypatch
):
    served = _served([MUTE_3, CAPTIONS])
    served.ctx = SimpleNamespace(thread_id="t-1")
    served.plan = object()
    served.unfiltered = served.out

    def built(ids):  # noqa: ANN001, ANN202
        cards = [MUTE_3, CAPTIONS]
        return SimpleNamespace(suggestions=[c for c in cards if c.id in ids]), {}

    monkeypatch.setattr(serve, "assemble_suggestions", lambda *a: built({"ss_mute3", "ss_caps"}))
    assert [c["id"] for c in await serve._server_cards(served)] == ["ss_mute3", "ss_caps"]
    # the picks expired from Redis and the deterministic order differs: store nothing, so a
    # tap answers "no longer fits" instead of running a different change
    monkeypatch.setattr(serve, "assemble_suggestions", lambda *a: built({"ss_caps"}))
    assert await serve._server_cards(served) is None
    monkeypatch.setattr(serve, "assemble_suggestions", lambda *a: (None, {}))
    assert await serve._server_cards(served) is None


async def test_no_cards_are_stored_for_a_legacy_chips_only_set(flags):
    legacy = serve.Served(
        out=sc.SuggestionSetOut(set_id="sg_1", status="ok"), media_digest="", input_digest=""
    )
    assert await serve._server_cards(legacy) is None


# ── The whole read path: flag off is byte-identical, flag on is frozen and filtered ──


CAPTIONS_READY = "rd_captions_off_with_cues"


def _serve_env(monkeypatch, name, db):  # noqa: ANN001, ANN202
    """``serve_suggestion_set`` over a committed fixture context; no model, Redis faked."""
    ctx = h.build_context(h.case_by_name(name))

    async def load(*a, **k):  # noqa: ANN002, ANN003
        return ctx

    async def same(db_, thread_, ctx_):  # noqa: ANN001, ANN202
        return ctx_

    monkeypatch.setattr(serve, "load_suggestion_context", load)
    monkeypatch.setattr(serve, "with_transcripts", same)
    return ctx


async def _get(db, state=None):  # noqa: ANN001, ANN202
    thread = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", state=state or {})
    return await serve.serve_suggestion_set(db, thread, None, 2)


async def test_flag_off_the_served_bytes_and_ids_are_the_legacy_ones(flags, redis, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    db = FakeDb(head="h1", brief=3)
    ctx = _serve_env(monkeypatch, CAPTIONS_READY, db)
    legacy = sc.build_candidates(ctx)
    store.put_result(
        "00000000-0000-0000-0000-0000000000aa",
        legacy.input_digest,
        store.fallback_result("x", prompt_version=sc.PROMPT_VERSION),
    )
    served = await _get(db, {sc.RING_KEY: [{"set_id": "x", "suggestions": []}]})
    assert db.statements == [], "no freshness read"
    assert (served.input_digest, served.out.set_id) == (legacy.input_digest, legacy.set_id)
    assert served.unfiltered is None and served.needs_fold is False
    assert served.out == sc.assemble_set(ctx, legacy, None)


async def test_flag_on_the_set_is_keyed_by_the_edit_frozen_per_digest_and_filtered_on_read(
    flags, redis, monkeypatch
):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    thread_id = "00000000-0000-0000-0000-0000000000aa"
    db = FakeDb(head="h1", brief=3)
    ctx = _serve_env(monkeypatch, CAPTIONS_READY, db)
    plan = await serve.plan_for(db, _thread(), ctx)
    store.put_result(
        thread_id, plan.input_digest, store.fallback_result("x", prompt_version=sc.PROMPT_VERSION)
    )

    first = await _get(db)
    assert first.out.status == "ok" and first.out.set_id == plan.set_id
    kinds = [s.kind for s in first.out.suggestions]
    assert "captions" in kinds and len(kinds) > 1, "several cards, one of them to dismiss"
    card = next(s for s in first.out.suggestions if s.kind == "captions")

    cards = await serve._server_cards(first)
    assert [c["id"] for c in cards] == [s.id for s in first.out.suggestions]
    state = {sc.RING_KEY: [{"set_id": plan.set_id, "state": "ready", "suggestions": cards}]}
    state, outcome = serve.apply_dismiss(state, plan.set_id, card.id)
    assert outcome == "dismissed"

    again = await _get(db, state)
    assert again.out.set_id == plan.set_id
    assert [s.kind for s in again.out.suggestions] == [k for k in kinds if k != "captions"]
    assert [s.rank for s in again.out.suggestions] == list(range(1, len(kinds)))
    assert [c.kind for c in again.out.chips] == [
        c.kind for c in first.out.chips if c.kind != "captions"
    ]
    assert [c.rank for c in again.out.chips] == list(range(1, len(again.out.chips) + 1))
    assert again.unfiltered.model_dump(mode="json") == first.out.model_dump(mode="json"), "frozen"

    # the creator edits (a new draft): a new digest, a new set, the dismissed card still out
    moved = FakeDb(head="h2", brief=3)
    plan2 = await serve.plan_for(moved, _thread(), ctx)
    store.put_result(
        thread_id, plan2.input_digest, store.fallback_result("x", prompt_version=sc.PROMPT_VERSION)
    )
    third = await _get(moved, state)
    assert third.out.set_id == plan2.set_id != plan.set_id
    assert "captions" in [s.kind for s in third.unfiltered.suggestions], "offered again"
    assert "captions" not in [s.kind for s in third.out.suggestions], "...and kept out"
