"""A refusal leaves its reason code on the turn row (KRI-1382).

Real `run_kria_turn` and the real completion transaction on Postgres; the planner is scripted
with the replies the real planner builds (the strategy check's own refusal, `_request_recovery`).

Failure modes (written before the code):

* a refusal completes as an ordinary assistant message, so the turn row has `status=completed`
  and no code: it counts as a success, or as nothing, and the reason is gone;
* the main creator refuses through `ask_user` (its prompt says to, and `is_refusal_code` is the
  server's own test for it): that turn is `question`, has no brief coverage and so counts as a
  success unless the planner stamps it; an ordinary clarifying `ask_user` must not be stamped;
* a turn closed because a newer message arrived is stamped as a failure, though it is not an
  attempt;
* a reply path added later forgets to stamp: a coded fallback exists so a recovery reply is never
  silently uncoded (`unclassified_recovery` is visible in the health page, an empty cell is not);
* an ordinary question or answer is stamped as a refusal and the success rate drops for no reason;
* the code is written to `turn.error`, and `media_collection_stories` reads a completed turn with
  an error as broken;
* stamping replaces diagnostics a plan already carried (a pinned-card refusal);
* the planner's own conflict question ("none of your clips has a filming time, so I can't put
  them in that order") completes as a plain `question` with no brief coverage, so it counts as a
  success while the same question asked by the commit gate is `blocked`;
* the planner's routine title proposals (`creative_copy_wording`, `creative_copy_authorship`) carry
  a choice `kind` too, so they are stamped `blocked` and the headline rate drops for the normal
  title hand-off while a model's own clarifying question counts as a success;
* the planner's order-vs-group conflict ("chronological AND grouped by sport, but your clips were
  filmed mixed together") is the one collector whose choice payload names no `kind`, so it
  completed uncoded and counted as a success while its sibling conflicts were `blocked`;
* the planner's unclear-word hold asks the same question as the commit gate (coded
  `unresolved_requirement`) but returns a plain question with no brief coverage, so the planner
  gate, which runs first, left it uncoded;
* "Change my request" on a held draft replies "Nothing is made yet" and completes uncoded, which
  counts as a successful attempt though nothing was asked for and nothing made;
* the song-order and song-range questions hold a draft back exactly as the moment-order question
  does, so leaving them unclassified counts a held-back draft as a success;
* the strategy check's "Should I make a simpler version?" refusal is stamped `refused` while the
  commit gate stamps the same question `blocked`, so one code shows up as two reasons;
* the editor copilot's own refusals ("I can't do that", a stale or failed pass, a no-op) return a
  `recovery` reply with no code, so they are pooled with six unrelated causes under
  `unclassified_recovery` and the top-reasons list cannot tell them apart;
* a message that is no creation request (the same text sent again, a status question) is a
  completed turn with no code, so it inflates both attempts and successes.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import planner
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import (
    PlannedKriaTurn,
    _change_request_turn,
    _clip_intent_resolution_plan,
    _gate_unresolved_choices,
    _ordering_conflict_question,
    _refused_turn,
    _request_recovery,
    adapt_creator_action,
    adapt_editor_action,
)
from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
from app.kria.turn_outcome import classify_response_turn, read_outcome
from app.models import CreatorAgentTurn
from app.services.choice_questions import (
    CHANGE_REQUEST_KEY,
    CONFLICT_ORDER_BASIS,
    KEEP_OPEN_REASON,
    SIMPLER_VERSION_REASON,
    ConflictCandidate,
    build_choice_question,
    choice_question_text,
    simpler_version_question,
)
from app.services.creation_health import creation_health
from app.services.creative_copy_decisions import authorship_question, wording_question
from app.services.song_range import build_song_range_question, song_range_question_text
from app.tasks.kria_runtime import run_kria_turn
from tests.agents.test_main_creator_agent import _manifest
from tests.kria.test_pinned_text_limits import TOP, _strategy
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_turn_auto_continue_postgres import (  # noqa: F401
    FINAL_REPLY,
    _publishes,
    _respond,
    _runtime_enabled,
    _script_planner,
    _submit,
)


def _unique_day() -> datetime:
    """A day no other test or earlier run has rows on, so the report counts only ours."""
    return datetime(1100, 1, 1, 12, tzinfo=UTC) + timedelta(days=uuid.uuid4().int % 330_000)


def _real_strategy_refusal() -> PlannedKriaTurn:
    manifest = _manifest()
    refused = check_strategy_for_runtime_v2(manifest, _strategy([TOP] * 5))
    assert isinstance(refused, RefusedStrategy)
    return _refused_turn(manifest, refused)


def _simpler_version_refusal() -> PlannedKriaTurn:
    refused = RefusedStrategy(
        question="Should I make a simpler version?", code=SIMPLER_VERSION_REASON
    )
    return _refused_turn(_manifest(), refused)


def _real_request_recovery() -> PlannedKriaTurn:
    return _request_recovery(_manifest(), None, reason="creator_planning_failed")


def _readiness_block() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="I can't make this yet:"),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_coverage={"stage": "draft", "reason": "approval_requirements_not_ready"},
    )


def _ordinary_question() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="Which clip first?"),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _ask_user(reason_code: str) -> PlannedKriaTurn:
    action = AskUser(
        kind="ask_user",
        question="I can't intercut those videos with their own audio. Switch to a Montage?",
        reason_code=reason_code,
        options=["Switch to Montage", "Keep it as is"],
    )
    return PlannedKriaTurn(
        plan=adapt_creator_action(action), manifest_hash="a" * 64, context_hash="b" * 64
    )


def _conflict_question() -> PlannedKriaTurn:
    candidate = ConflictCandidate(
        conflict_id="order_basis:1",
        intro="Unfortunately none of your clips have a filming time,",
        reason="so I can't put them in that order.",
        options=(),
        kind=CONFLICT_ORDER_BASIS,
    )
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=choice_question_text(candidate),
            choice_question=build_choice_question(candidate),
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _hidden_part_question() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=_clip_intent_resolution_plan(
            question="Which part of the clip should stay hidden?",
            status="needs_creator",
            diagnostics={"stage": "hidden_part", "reason": "needs_creator"},
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _voiceover_script_writer_down() -> PlannedKriaTurn:
    from types import SimpleNamespace  # noqa: PLC0415

    from app.kria import voiceover_script  # noqa: PLC0415

    class DownWriter:
        def __init__(self, _client: object) -> None: ...

        def run(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("writer down")

    draft = voiceover_script.draft_script(
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        manifest=SimpleNamespace(media=[]),
        clips=[],
        creator_words=["write me a script"],
        prior=None,
        revision_note=None,
        deadline_monotonic=None,
    )
    # The real writer path, run on its own loop: the caller is already inside a test's loop.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(voiceover_script, "VoiceoverScriptWriterAgent", DownWriter)
        patch.setattr(voiceover_script, "_writer_client", lambda: None)
        with ThreadPoolExecutor(max_workers=1) as pool:
            plan = pool.submit(asyncio.run, draft).result()
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


def _creative_copy_question(real_question) -> PlannedKriaTurn:
    question = real_question()
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response="Does this wording work?",
            choice_question=question,
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _wording() -> dict:
    return wording_question(
        target="opening_title", candidate="Matcha mornings", dependency_digest="d"
    )


def _authorship() -> dict:
    return authorship_question(target="opening_title", dependency_digest="d")


def _moment_order_question() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response="Which clip has the goal?",
            moment_order_question={"question_id": "q-1", "kind": "moment_order"},
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _song_range_question() -> PlannedKriaTurn:
    question = build_song_range_question(1, 200.0)
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=song_range_question_text(question),
            song_range_question=question,
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _editor_reply(unmet: bool) -> PlannedKriaTurn:
    plan = adapt_editor_action(
        reply="I can't change the font on a photo.",
        ops=[],
        unmet_requests=[{"request": "font on photo", "reason": "not supported"}] if unmet else (),
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


def _on_its_own_loop(coroutine):  # noqa: ANN001, ANN202
    """Run a real async planner step from a sync case builder inside a test's running loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _order_vs_group_conflict() -> PlannedKriaTurn:
    def clip(media_id: str, minute: int) -> SimpleNamespace:
        return SimpleNamespace(
            media_id=media_id, capture_time=datetime(2026, 1, 1, 10, minute, tzinfo=UTC)
        )

    def group(name: str, *media_ids: str) -> SimpleNamespace:
        return SimpleNamespace(
            op="group",
            attribute=name,
            status="resolved",
            placeholder=False,
            assignments=[SimpleNamespace(media_id=media_id) for media_id in media_ids],
        )

    asked = _ordering_conflict_question(
        [group("tennis", "t1", "t2"), group("padel", "p1", "p2")],
        [clip("t1", 1), clip("p1", 2), clip("t2", 3), clip("p2", 4)],
        wants_capture_order=True,
        creator_request="chronological, grouped by sport",
    )
    assert asked is not None and "I can't do both" in asked.response
    return PlannedKriaTurn(plan=asked, manifest_hash="a" * 64, context_hash="b" * 64)


def _unclear_word_hold() -> PlannedKriaTurn:
    act = KriaTurnPlan(
        mode="act",
        turn_value="action",
        intents=[
            KriaToolIntent(
                intent_id="apply-strategy",
                tool_name="draft.apply_strategy",
                tool_version=1,
                arguments={"summary": "Captions.", "strategy": {}},
            )
        ],
    )
    hold = "I couldn't locate the unclear word reliably. Which caption word or line do you mean?"
    from app.kria import unclear_word_ask  # noqa: PLC0415

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(unclear_word_ask, "clarification_question", lambda *_a, **_k: hold)
        patch.setattr(planner, "load_latest_brief", _none_brief)
        patch.setattr(planner, "_load_thread_events", _no_events)
        gated = _on_its_own_loop(
            _gate_unresolved_choices(
                None,  # type: ignore[arg-type]
                PlannedKriaTurn(plan=act, manifest_hash="a" * 64, context_hash="b" * 64),
                thread_id=uuid.uuid4(),
                creator_id=uuid.uuid4(),
            )
        )
    assert gated.plan.mode == "respond" and gated.plan.response == hold
    return gated


async def _none_brief(*_args: object, **_kwargs: object) -> None:
    return None


async def _no_events(*_args: object, **_kwargs: object) -> list:
    return []


def _change_request_events() -> list[tuple[str, dict]]:
    question = simpler_version_question()
    return [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": CHANGE_REQUEST_KEY,
                }
            },
        ),
    ]


def _change_request_reply() -> PlannedKriaTurn:
    async def events(*_args: object, **_kwargs: object) -> list:
        return _change_request_events()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "kria_choice_questions_enabled", True)
        patch.setattr(planner, "_load_thread_events", events)
        planned = _on_its_own_loop(_change_request_turn(None, uuid.uuid4()))  # type: ignore[arg-type]
    assert planned is not None and "Nothing is made yet" in planned.plan.response
    return planned


CASES = {
    "strategy_refusal": (_real_strategy_refusal, ("refused", "pinned_text_too_many")),
    "simpler_version_refusal": (
        _simpler_version_refusal,
        ("blocked", "simplification_requires_choice"),
    ),
    "request_recovery": (_real_request_recovery, ("recovery", "creator_planning_failed")),
    "readiness_block": (_readiness_block, ("blocked", "approval_requirements_not_ready")),
    "unclassified_recovery": (
        lambda: _respond(FINAL_REPLY, continuable=False),
        ("recovery", "unclassified_recovery"),
    ),
    "ordinary_question": (_ordinary_question, None),
    "editor_unmet_request": (lambda: _editor_reply(True), ("refused", "editor_request_unmet")),
    "editor_plain_reply": (lambda: _editor_reply(False), None),
    "planner_conflict_question": (_conflict_question, ("blocked", "unresolved_choice:order_basis")),
    "planner_order_vs_group_conflict": (
        _order_vs_group_conflict,
        ("blocked", "unresolved_choice:order_vs_group"),
    ),
    "planner_unclear_word_hold": (_unclear_word_hold, ("blocked", "unresolved_requirement")),
    "hidden_part_clip_question": (_hidden_part_question, ("blocked", "clip_question:hidden_part")),
    "voiceover_script_writer_down": (
        _voiceover_script_writer_down,
        ("recovery", "voiceover_script_failed"),
    ),
    "creative_copy_wording_proposal": (lambda: _creative_copy_question(_wording), None),
    "creative_copy_authorship_ask": (lambda: _creative_copy_question(_authorship), None),
    "moment_order_question": (_moment_order_question, ("blocked", "moment_order_question")),
    "song_range_question": (_song_range_question, ("blocked", "song_range_question")),
    "ask_user_refusal": (
        lambda: _ask_user("unsupported_audio_intercut"),
        ("refused", "unsupported_audio_intercut"),
    ),
    "ask_user_refusal_free_text_code": (
        lambda: _ask_user("Cannot: audio intercut!"),
        ("refused", "cannot_audio_intercut"),
    ),
    "ask_user_clarifying_question": (lambda: _ask_user("which_clip_first"), None),
}


async def _run_case(
    monkeypatch: pytest.MonkeyPatch, planned: PlannedKriaTurn, day: datetime
) -> uuid.UUID:
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, planned)
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Make it exactly like my last post", 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    turn_id = uuid.UUID(accepted.turn_id)
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).values(created_at=day)
        )
        db.commit()
    return turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CASES))
async def test_every_refusal_path_records_a_code_on_the_completed_turn(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    build, expected = CASES[name]
    turn_id = await _run_case(monkeypatch, build(), _unique_day())

    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        assert turn is not None and turn.status == "completed"
        recorded = ((turn.plan_json or {}).get("diagnostics") or {}).get("outcome")
        assert turn.error is None
    if expected is None:
        assert recorded is None
    else:
        assert (recorded["kind"], recorded["code"]) == expected


@pytest.mark.asyncio
async def test_the_health_report_counts_each_refusal_under_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    for build, _expected in CASES.values():
        await _run_case(monkeypatch, build(), day)

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)

    assert report["turns"]["attempts"] == len(CASES)
    successes = sum(1 for _build, expected in CASES.values() if expected is None)
    assert report["turns"]["successes"] == successes == 5
    assert report["turns"]["success_rate"] == round(successes / len(CASES), 4)
    reasons = {(r["kind"], r["code"]): r["count"] for r in report["top_failure_reasons"]}
    assert reasons == {
        ("refused", "pinned_text_too_many"): 1,
        ("blocked", "simplification_requires_choice"): 1,
        ("recovery", "creator_planning_failed"): 1,
        ("blocked", "approval_requirements_not_ready"): 1,
        ("recovery", "unclassified_recovery"): 1,
        ("refused", "unsupported_audio_intercut"): 1,
        ("refused", "cannot_audio_intercut"): 1,
        ("refused", "editor_request_unmet"): 1,
        ("blocked", "unresolved_choice:order_basis"): 1,
        ("blocked", "unresolved_choice:order_vs_group"): 1,
        ("blocked", "unresolved_requirement"): 1,
        ("blocked", "clip_question:hidden_part"): 1,
        ("recovery", "voiceover_script_failed"): 1,
        ("blocked", "moment_order_question"): 1,
        ("blocked", "song_range_question"): 1,
    }


def test_both_gates_record_the_same_code_for_one_held_back_conflict():
    plan = _conflict_question().plan
    planner_gate = classify_response_turn(plan, None, None)
    commit_gate = classify_response_turn(plan, {"stage": "draft", "reason": KEEP_OPEN_REASON}, None)

    assert read_outcome(commit_gate.diagnostics) == read_outcome(planner_gate.diagnostics)
    assert read_outcome(commit_gate.diagnostics) == ("blocked", "unresolved_choice:order_basis")


@pytest.mark.asyncio
async def test_a_turn_closed_by_a_newer_message_is_not_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(FINAL_REPLY, continuable=True))
    _publishes(monkeypatch)
    first = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    second = await _submit(user_id, thread_id, "Actually make it shorter", first.thread_revision)
    assert second.status == "queued"

    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)

    assert result == {"turn_id": first.turn_id, "status": "completed"}
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id.in_([uuid.UUID(first.turn_id), uuid.UUID(second.turn_id)]))
            .values(created_at=day)
        )
        db.commit()
        closed = db.get(CreatorAgentTurn, uuid.UUID(first.turn_id))
        outcome = ((closed.plan_json or {}).get("diagnostics") or {}).get("outcome")
    assert outcome == {"kind": "superseded", "code": "continuation_superseded"}

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)
    assert report["turns"]["attempts"] == 0
    assert report["top_failure_reasons"] == []


@pytest.mark.asyncio
async def test_a_repeated_send_and_a_status_question_are_not_creation_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(FINAL_REPLY, continuable=True))
    _publishes(monkeypatch)
    first = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    repeated = await _submit(
        user_id, thread_id, "Caption every clip with what happens", first.thread_revision
    )
    status = await _submit(user_id, thread_id, "status", repeated.thread_revision)

    with sync_session() as db:
        rows = {
            turn_id: db.get(CreatorAgentTurn, uuid.UUID(turn_id))
            for turn_id in (first.turn_id, repeated.turn_id, status.turn_id)
        }
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id.in_([row.id for row in rows.values()]))
            .values(created_at=day)
        )
        db.commit()
        outcomes = {
            key: (row.plan_json or {}).get("diagnostics", {}).get("outcome")
            for key, row in rows.items()
        }
    assert outcomes[repeated.turn_id] == {"kind": "inert", "code": "duplicate_send"}
    assert outcomes[status.turn_id] == {"kind": "inert", "code": "status_question"}

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)
    assert report["turns"]["attempts"] == 0
    assert report["turns"]["in_progress"] == 1
    assert report["top_failure_reasons"] == []


@pytest.mark.asyncio
async def test_change_my_request_after_a_held_draft_is_not_a_creation_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    turn_id = await _run_case(monkeypatch, _change_request_reply(), day)

    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        recorded = ((turn.plan_json or {}).get("diagnostics") or {}).get("outcome")
    assert recorded == {"kind": "inert", "code": "change_request_selected"}

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)
    assert report["turns"]["attempts"] == 0
    assert report["top_failure_reasons"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("unsupported", ("refused", "editor_unsupported")),
        ("stale", ("refused", "editor_stale")),
        ("failed", ("refused", "editor_failed")),
        ("no_effect", ("refused", "editor_no_effect")),
        ("clarification", None),
    ],
)
async def test_each_editor_copilot_refusal_records_its_own_code(
    monkeypatch: pytest.MonkeyPatch, outcome: str, expected: tuple[str, str] | None
) -> None:
    monkeypatch.setattr(
        planner,
        "_load_editor_target",
        AsyncMock(
            return_value=planner._EditorTarget(job_id=uuid.uuid4(), snapshot={}, conversation=[])
        ),
    )
    copilot = SimpleNamespace(ops=[], outcome=outcome, reply="That is not something I can do.")
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=copilot))
    plan = await planner._plan_editor_revision(
        SimpleNamespace(rollback=AsyncMock()),
        thread_id=uuid.uuid4(),
        item=SimpleNamespace(id=uuid.uuid4()),
        user_message="make it pop",
    )
    assert plan is not None

    turn_id = await _run_case(
        monkeypatch,
        PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64),
        _unique_day(),
    )

    with sync_session() as db:
        recorded = (
            (db.get(CreatorAgentTurn, turn_id).plan_json or {}).get("diagnostics") or {}
        ).get("outcome")
    assert (
        (recorded is None)
        if expected is None
        else ((recorded["kind"], recorded["code"]) == expected)
    )


@pytest.mark.parametrize(
    "miss", ["render_in_flight", "editor_state_stale", "failed_draft_unavailable", "no_render"]
)
def test_each_editor_target_miss_records_its_own_recovery_code(miss: str) -> None:
    token = planner._editor_target_miss.set(miss)
    try:
        planned = planner._editor_target_recovery(_manifest())
    finally:
        planner._editor_target_miss.reset(token)
    assert read_outcome(planned.plan.diagnostics) == ("recovery", f"editor_target_{miss}")
