"""KRI-476 (PR-C) review fixes: written against the failure first.

* P1-A over-asking: a MODEL-chosen length (the Creator model must always emit one, and
  the server stamps it ``target_duration_requested``) is not a promise by the creator.
* P1-B: the server-owned answer wins over whatever the model re-emits.
* P2-3: a question is open only while it is the live last assistant turn.
* P2-5: client-written drafts cannot carry answers.
* P3: duration cap, option numbering, attachment key gating, non-Postgres wiring proof.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria import planner
from app.kria.brief import BriefRequirement
from app.kria.drafts import _sanitize_strategy_duration_provenance
from app.kria.planner import PlannedKriaTurn
from app.services.choice_questions import (
    ChoiceCapability,
    collect_conflicts,
    latest_open_choice_question,
    match_open_choice,
    resolve_choices,
)
from app.services.creator_render_contract import build_render_contract
from tests.kria.test_choice_conflict_gate import (
    _asked,
    _brief,
    _gate,
    _order,
    _picked,
    _planned,
    _rows,
    _strategy,
    _timing,
)

# ── P1-A ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clips", "seconds"),
    [(21, 15), (31, 24), (42, 30)],
)
async def test_a_model_chosen_length_never_asks(monkeypatch, clips, seconds) -> None:
    """The creator named no length: 21 clips in the model's 15 s, 31 in 24 s, 42 in 30 s."""
    result = await _gate(monkeypatch, _planned(seconds=seconds), rows=_rows(clips), brief=None)
    assert result.plan.mode == "act"
    quiet_brief = _brief(_order())  # prod fecf9337: the brief only says "chronologically"
    result = await _gate(
        monkeypatch, _planned(seconds=seconds), rows=_rows(clips), brief=quiet_brief
    )
    assert result.plan.mode == "act"


def test_the_prod_fecf9337_shape_asks_nothing() -> None:
    """42 dated clips, model-chosen 30 s (flag stamped), brief only 'chronologically'."""
    strategy = CreativeStrategy(
        edit_format="montage",
        media_scope="all",
        target_duration_s=30,
        target_duration_requested=True,
    )
    assert collect_conflicts(strategy, _brief(_order()), {"clip_assignments": _rows(42)}) == []


@pytest.mark.asyncio
async def test_an_explicit_brief_length_still_asks_and_quotes_the_requirement(monkeypatch) -> None:
    result = await _gate(
        monkeypatch, _planned(seconds=15), rows=_rows(30), brief=_brief(_timing(15))
    )
    assert result.plan.choice_question["kind"] == "duration_vs_count"
    assert 'Your brief says "Keep 15 seconds"' in result.plan.response
    assert "You asked for" not in result.plan.response


@pytest.mark.asyncio
async def test_a_brief_length_that_fits_never_asks(monkeypatch) -> None:
    result = await _gate(
        monkeypatch, _planned(seconds=60), rows=_rows(30), brief=_brief(_timing(60))
    )
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_the_creators_selection_is_what_is_counted(monkeypatch) -> None:
    """42 snapshot clips, brief 15 s, 8 selected: 8 x 0.8 s fits, so nothing is asked."""
    eight = [f"c{i:02d}" for i in range(8)]
    planned = _planned(seconds=15, media_scope="selected", selected_media_ids=eight)
    result = await _gate(monkeypatch, planned, rows=_rows(42), brief=_brief(_timing(15)))
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_fewer_chooses_within_the_selection_and_never_re_adds_excluded_clips(
    monkeypatch,
) -> None:
    """30 selected of 42, brief 15 s: asks about 30 (not 42) and `fewer` keeps to the 30."""
    selected = [f"c{i:02d}" for i in range(0, 42, 1) if i % 7 != 0][:30]
    assert len(selected) == 30
    planned = _planned(seconds=15, media_scope="selected", selected_media_ids=selected)
    rows, brief = _rows(42), _brief(_timing(15))
    first = await _gate(monkeypatch, planned, rows=rows, brief=brief)
    question = first.plan.choice_question
    assert question["kind"] == "duration_vs_count"
    assert "uses 30 clips" in first.plan.response and "42" not in first.plan.response
    assert "24" in question["options"][0]["label"]  # 30 x 0.8 s
    asked = _asked(first)
    result = await _gate(
        monkeypatch,
        planned,
        rows=rows,
        brief=brief,
        events=(asked, _picked(question, "fewer")),
    )
    chosen = _strategy(result)["selected_media_ids"]
    assert len(chosen) == 18 and set(chosen) <= set(selected)
    assert not {f"c{i:02d}" for i in range(0, 42, 7)} & set(chosen)


@pytest.mark.asyncio
async def test_clips_the_creator_left_out_are_not_counted(monkeypatch) -> None:
    from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent

    intent = ClipIntent(intent_id="inc", op="include", attribute="the beach clips")
    kept = ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[
            ClipAssignment(media_id=f"c{i:02d}", evidence="x", confidence=0.9) for i in range(10)
        ],
    )
    from app.agents._schemas.creator_agent import ProposeStrategy

    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(edit_format="montage", resolved_clip_intents=[kept]),
        summary="x",
    )
    planned = PlannedKriaTurn(
        plan=planner.adapt_creator_action(action, server_resolved_clip_intents=[kept]),
        manifest_hash="a",
        context_hash="b",
    )
    # 10 kept clips fit in 15 s; the other 20 must not count toward N.
    result = await _gate(monkeypatch, planned, rows=_rows(30), brief=_brief(_timing(15)))
    assert result.plan.mode == "act"


def test_a_creator_selection_we_cannot_count_never_guesses() -> None:
    select = BriefRequirement(
        id="r3", kind="select", scope="global", description="only the funny ones"
    )
    strategy = CreativeStrategy(edit_format="montage")
    brief = _brief(_timing(15), select)
    assert collect_conflicts(strategy, brief, {"clip_assignments": _rows(30)}) == []


# ── P1-B: the answer wins, even over a model that re-emits something else ───


@pytest.mark.asyncio
@pytest.mark.parametrize("model_value", [24, 18, 15])
async def test_extend_wins_over_what_the_model_emits(monkeypatch, model_value) -> None:
    rows, brief = _rows(30), _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "extend"))
    result = await _gate(
        monkeypatch, _planned(seconds=model_value), rows=rows, brief=brief, events=events
    )
    strategy = _strategy(result)
    assert (
        strategy["target_duration_s"] == 24 and strategy["choice_answers"][0]["option"] == "extend"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model_value", [24, 18, 15])
async def test_fewer_wins_over_what_the_model_emits(monkeypatch, model_value) -> None:
    rows, brief = _rows(30), _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "fewer"))
    result = await _gate(
        monkeypatch, _planned(seconds=model_value), rows=rows, brief=brief, events=events
    )
    strategy = _strategy(result)
    assert strategy["target_duration_s"] == 15 and len(strategy["selected_media_ids"]) == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("model_choice", [None, "chronological", "group_first"])
async def test_order_answers_win_over_the_models_own_order_field(monkeypatch, model_choice) -> None:
    rows, brief = _rows(8, dated=False), _brief(_order())
    first = await _gate(monkeypatch, _planned(), rows=rows, brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "unordered"))
    result = await _gate(
        monkeypatch,
        _planned(ordering_choice=model_choice),
        rows=rows,
        brief=brief,
        events=events,
    )
    strategy = _strategy(result)
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot={"clip_assignments": rows}
    )
    assert strategy["choice_answers"][0]["option"] == "unordered"
    assert not contract.order_required and not contract.unresolved


# ── P2-3: a question is open only while it is the live last assistant turn ──


def _q(qid: str = "q1") -> dict:
    return {
        "question_id": qid,
        "conflict": "duration_vs_count",
        "input_digest": "d",
        "options": [
            {"key": "extend", "label": "Extend it to 24 seconds", "aliases": ["longer"]},
            {
                "key": "fewer",
                "label": "Keep 15 seconds with 18 clips",
                "aliases": ["keep it short"],
            },
        ],
    }


def test_a_later_text_question_closes_the_choice_so_its_reply_is_not_converted() -> None:
    events = [
        ("assistant", {"choice_question": _q()}),
        ("user", {}),
        ("assistant", {"turn_value": "question"}),  # "how many clips?"
    ]
    assert latest_open_choice_question(events) is None  # so a reply of "2" is plain text


def test_the_question_stays_open_while_it_is_the_last_assistant_turn() -> None:
    events = [("assistant", {"choice_question": _q()})]
    question = latest_open_choice_question(events)
    assert question is not None
    for alias, key in (("longer", "extend"), ("Keep it short", "fewer"), ("2", "fewer")):
        assert match_open_choice(question, alias) == key
    # ... and stops being convertible once the assistant REPLIED to something the creator
    # said afterwards.
    reply = ("assistant", {"turn_value": "action"})
    assert latest_open_choice_question([*events, ("user", {}), reply]) is None


def test_after_the_re_ask_cap_a_non_answer_closes_it_but_a_restating_recovery_reopens_it() -> None:
    asked = ("assistant", {"choice_question": _q("q1")})
    again = ("assistant", {"choice_question": _q("q2")})
    assert latest_open_choice_question([asked, ("user", {}), again])["question_id"] == "q2"
    spent = [asked, ("user", {}), again, ("user", {})]
    assert latest_open_choice_question(spent) is None  # asked twice, then not answered
    words = ("assistant", {"brief_coverage": {"reason": "unresolved_choice"}})
    assert latest_open_choice_question([*spent, words])["question_id"] == "q2"
    answered = ("user", {"choice_selection": {"question_id": "q2", "option_key": "extend"}})
    assert latest_open_choice_question([*spent, words, answered]) is None


# ── P3 ───────────────────────────────────────────────────────────────────────


def test_extend_is_capped_and_only_fewer_is_offered_beyond_it() -> None:
    strategy = CreativeStrategy(edit_format="montage")
    brief = _brief(_timing(15))
    rows = {"clip_assignments": _rows(30)}
    (offered,) = collect_conflicts(strategy, brief, rows, ChoiceCapability(max_duration_s=20))
    assert [o.key for o in offered.options] == ["fewer"]
    assert offered.recommended().key == "fewer"
    resolved = resolve_choices(strategy, brief, rows, [], ChoiceCapability(max_duration_s=20))
    assert [o.key for o in resolved.question.options] == ["fewer"]
    (normal,) = collect_conflicts(strategy, brief, rows)
    assert [o.key for o in normal.options] == ["extend", "fewer"]


def test_typed_numbers_pick_the_option_listed_at_that_number() -> None:
    options = [
        {"key": "shot_1", "label": "On shot 1", "aliases": ["shot 1"]},
        {"key": "shot_3", "label": "On shot 3", "aliases": ["shot 3"]},
    ]
    question = {"question_id": "q", "conflict": "text_placement:r7", "options": options}
    assert match_open_choice(question, "2") == "shot_3"  # the second LISTED option
    assert match_open_choice(question, "shot 3") == "shot_3"
    assert match_open_choice(question, "shot 2") is None  # not offered


def test_the_attachment_key_needs_a_recorded_answer_to_resolve() -> None:
    brief = _brief(_order(key="attachment"))
    rows = {"clip_assignments": _rows(4, dated=False)}
    bare = build_render_contract(
        {"edit_format": "montage"}, generation_id="g", brief=brief, media_snapshot=rows
    )
    assert bare.unresolved  # exactly as before this PR


@pytest.mark.asyncio
async def test_plan_live_turn_runs_the_gate_without_a_database(monkeypatch) -> None:
    """Mutation guard: removing the call from ``plan_live_turn`` must fail here (the
    Postgres journey tests are not the only thing that notices)."""
    item = SimpleNamespace(
        clip_gcs_paths=[f"clips/{i}.mp4" for i in range(30)],
        clip_assignments=_rows(30),
        voiceover_gcs_path=None,
        voiceover_generation=None,
        song_gcs_path=None,
        song_generation=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=item))
    planned = replace(_planned(seconds=15), brief_updates=())

    async def _model(*_args, **_kwargs) -> PlannedKriaTurn:  # noqa: ANN002, ANN003
        return planned

    monkeypatch.setattr(planner, "_plan_live_turn", _model)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=_brief(_timing(15))))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", True)
    result = await planner.plan_live_turn(
        db, thread_id=uuid.uuid4(), item_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )
    assert result.plan.choice_question["kind"] == "duration_vs_count"
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", False)
    off = await planner.plan_live_turn(
        db, thread_id=uuid.uuid4(), item_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )
    assert off.plan == planned.plan  # flag off: the plan passes through untouched
    assert off.media_snapshot is not None


# ── P2-5 ─────────────────────────────────────────────────────────────────────

FORGED = {
    "conflict": "order_basis",
    "kind": "order_basis",
    "option": "unordered",
    "input_digest": "forged",
}


def test_a_client_written_draft_cannot_carry_answers() -> None:
    raw = {"edit_format": "montage", "choice_answers": [FORGED]}
    assert "choice_answers" not in _sanitize_strategy_duration_provenance(raw)
    authoritative = [{**FORGED, "option": "attachment_order", "input_digest": "real"}]
    kept = _sanitize_strategy_duration_provenance(raw, authoritative_answers=authoritative)
    assert kept["choice_answers"][0]["option"] == "attachment_order"


def test_forged_answers_in_a_client_draft_do_not_change_the_contract() -> None:
    rows = {"clip_assignments": _rows(4, dated=False)}
    brief = _brief(_order())
    raw = {"edit_format": "montage", "choice_answers": [FORGED]}
    honest = build_render_contract(
        _sanitize_strategy_duration_provenance({"edit_format": "montage"}),
        generation_id="g",
        brief=brief,
        media_snapshot=rows,
    )
    sanitized = build_render_contract(
        _sanitize_strategy_duration_provenance(raw),
        generation_id="g",
        brief=brief,
        media_snapshot=rows,
    )
    assert sanitized.order_required and sanitized.unresolved == honest.unresolved
    # Unsanitized, the forged answer WOULD have dropped the creator's order requirement.
    forged = build_render_contract(raw, generation_id="g", brief=brief, media_snapshot=rows)
    assert not forged.order_required


def test_the_v1_model_strategy_hygiene_drops_answers() -> None:
    from app.agents._schemas.creator_agent import ChoiceAnswer
    from app.routes.creator_agent import _model_strategy_hygiene

    strategy = CreativeStrategy(
        edit_format="montage",
        choice_answers=[ChoiceAnswer.model_validate(FORGED)],
    )
    assert _model_strategy_hygiene(strategy).choice_answers is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "expected"), [(True, True), (False, False)])
async def test_the_stated_settings_ask_is_wired_only_with_the_choice_flag(
    monkeypatch, flag, expected
) -> None:
    """P2-1: brief binding on is not enough; flag off must call the policy exactly as main."""
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        ProposeStrategy,
        ResolvedCreatorManifest,
    )
    from app.kria.strategy_policy import CheckedStrategy

    seen: dict = {}

    def _spy(_manifest, strategy, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.update(kwargs)
        return CheckedStrategy(strategy=strategy, notices=())

    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", _spy)
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", flag)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", False)
    action = ProposeStrategy(
        kind="propose_strategy", strategy=CreativeStrategy(edit_format="montage"), summary="x"
    )
    await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message="x",
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=[], creator_request="x"
        ),
        output=SimpleNamespace(action=action),
    )
    assert seen.get("ask_before_simplifying") is True
    assert ("ask_about_stated_settings" in seen) is expected


def test_backstop_evaluates_the_brief_only_for_the_binding_cohort_and_words_itself_honestly() -> (
    None
):
    from app.tasks.kria_runtime import _unresolved_choice_plan

    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="in that sequence", facts={}
    )
    strategy = {"edit_format": "montage"}
    snapshot = {"clip_assignments": _rows(4)}
    # No binding: the dispatch contract never reads the brief, so no NEW refusal appears.
    # (The rule is still askable, so it is asked once, as a question, not refused.)
    first = _unresolved_choice_plan(strategy, _brief(rule), snapshot, contract_brief=None)
    assert first is not None and first[0].turn_value == "question"
    # A strategy the gate already resolved leaves nothing to refuse for that cohort.
    answered = {
        "edit_format": "montage",
        "choice_answers": [
            {
                "conflict": "order_basis",
                "kind": "order_basis",
                "option": "unordered",
                "input_digest": "d",
            }
        ],
    }
    assert _unresolved_choice_plan(answered, _brief(rule), snapshot, contract_brief=None) is None
    # An unaskable unresolved item is a plain message; "draft unchanged" only if there is one.
    bad = BriefRequirement(
        id="r3", kind="text", scope="per_clip", literal="Km 1", description="the first km"
    )
    unresolved_strategy = {"edit_format": "montage", "shot_labels": ["Km 2"]}
    plain, reason = _unresolved_choice_plan(
        unresolved_strategy, _brief(bad), snapshot, contract_brief=_brief(bad), has_draft=False
    )
    assert plain.turn_value == "recovery" and reason == "unresolved_requirement"
    assert "unchanged" not in plain.response
    with_draft, _ = _unresolved_choice_plan(
        unresolved_strategy, _brief(bad), snapshot, contract_brief=_brief(bad), has_draft=True
    )
    assert with_draft.response.endswith("Your current draft is unchanged.")
    assert (
        _unresolved_choice_plan(unresolved_strategy, _brief(bad), snapshot, contract_brief=None)
        is None
    )  # not in the binding cohort: nothing brief-derived to refuse


# ── second review ────────────────────────────────────────────────────────────

from app.services.choice_questions import (  # noqa: E402
    NEVER_CLOSES,
    QUESTION_EVENT_EFFECT,
    count_asks,
    delegated_choice,
    tag_event,
)


def _ev(role: str, event_type: str, **payload: object) -> tuple[str, dict]:
    return tag_event(role, payload, event_type, None)


@pytest.mark.parametrize(
    "event_type",
    sorted(t for t, effect in QUESTION_EVENT_EFFECT.items() if effect == NEVER_CLOSES),
)
def test_async_and_non_conversational_events_never_close_a_question(event_type) -> None:
    """P2-4: a render finishing / a memory write / a status line is not a reply."""
    asked = _ev("assistant", "assistant_response", choice_question=_q(), turn_value="question")
    for with_user in (False, True):
        events = [asked, *([tag_event("user", {}, None, "ok")] if with_user else [])]
        events.append(_ev("assistant", event_type))
        question = latest_open_choice_question(events)
        assert question is not None and question["question_id"] == "q1"
        assert match_open_choice(question, "2") == "fewer"  # a typed '2' still converts


def test_the_denylist_names_every_producer_event_type() -> None:
    expected = {
        "assistant_response",
        "assistant_question",
        "assistant_error",
        "assistant_render_failed",
        "generation_ready",
        "assistant_review",
        "memory_updated",
        "status_update",
        "format_prompt",
        "media_prompt",
        "draft_applied",
        "draft_undone",
    }
    assert set(QUESTION_EVENT_EFFECT) == expected


def test_only_a_reply_to_a_later_user_turn_closes_a_question() -> None:
    asked = _ev("assistant", "assistant_response", choice_question=_q(), turn_value="question")
    text_question = _ev("assistant", "assistant_response", turn_value="question")
    user = tag_event("user", {}, None, "how about 12 clips")
    # No user turn after the question: an assistant_response is not a reply to anything.
    assert latest_open_choice_question([asked, text_question]) is not None
    # The P2-3 scenario: [Q, user, assistant text question, user '2'] must NOT convert.
    assert latest_open_choice_question([asked, user, text_question]) is None
    plan = _ev("assistant", "assistant_response", turn_value="action")
    assert latest_open_choice_question([asked, user, plan]) is None
    voiceover = _ev("assistant", "assistant_question", code="voiceover_required")
    assert latest_open_choice_question([asked, user, voiceover]) is None


def test_async_events_do_not_consume_an_ask() -> None:
    """P4: the second ask is still available after any number of async events."""
    strategy = CreativeStrategy(edit_format="montage")
    rows, brief = {"clip_assignments": _rows(30)}, _brief(_timing(15))
    first = resolve_choices(strategy, brief, rows, []).question
    asked = _ev(
        "assistant",
        "assistant_response",
        choice_question={
            "question_id": "q1",
            "conflict": first.conflict_id,
            "input_digest": first.input_digest,
            "options": [{"key": o.key, "label": o.label} for o in first.options],
        },
        turn_value="question",
    )
    noise = [_ev("assistant", t) for t in ("generation_ready", "memory_updated", "status_update")]
    history = [asked, *noise, tag_event("user", {}, None, "hmm?")]
    assert count_asks(history, first.conflict_id, first.input_digest) == 1
    again = resolve_choices(strategy, brief, rows, history)
    assert again.question is not None and not again.exhausted  # the ONE re-ask is intact


# key unification ------------------------------------------------------------


def test_one_capture_key_set_is_shared_by_contract_planner_and_receipts() -> None:
    from app.kria import brief_checks
    from app.pipeline import unified_montage
    from app.services import choice_questions, clip_facts

    shared = clip_facts.CAPTURE_ORDER_KEYS
    assert shared >= {"capture_time", "chronological", "route", "time", "shot_order"}
    assert choice_questions.CAPTURE_ORDER_KEYS is shared
    assert unified_montage._CAPTURE_ORDER_KEYS is shared
    assert brief_checks._CAPTURE_ORDER_KEYS is shared


@pytest.mark.parametrize("key", ["route", "time", "shot_order", "capture_time"])
def test_route_time_and_shot_order_are_verified_as_capture_order(key) -> None:
    rows = _rows(6)
    brief = _brief(_order(key=key))
    contract = build_render_contract(
        {"edit_format": "montage"},
        generation_id="g",
        brief=brief,
        media_snapshot={"clip_assignments": rows},
    )
    assert contract.order_required and contract.order_basis == "capture_time"
    assert contract.order_ids == tuple(r["media_id"] for r in rows) and not contract.unresolved


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["route", "time", "shot_order"])
async def test_a_dated_route_request_asks_nothing(monkeypatch, key) -> None:
    result = await _gate(monkeypatch, _planned(), rows=_rows(8), brief=_brief(_order(key=key)))
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_an_undated_route_request_says_so_and_offers_honest_options(monkeypatch) -> None:
    result = await _gate(
        monkeypatch,
        _planned(),
        rows=_rows(11, dated=False),
        brief=_brief(_order(key="route")),
    )
    question = result.plan.choice_question
    assert [o["label"] for o in question["options"]] == [
        "Use the order you added the clips",
        "Continue without a fixed order",
    ]
    assert "none of your clips have a filming time" in result.plan.response
    assert "in the order you filmed" not in result.plan.response  # it was a route ask


@pytest.mark.asyncio
async def test_a_keyless_rule_still_asks_even_with_capture_times(monkeypatch) -> None:
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1..8 in that sequence"
    )
    result = await _gate(monkeypatch, _planned(), rows=_rows(8), brief=_brief(rule))
    assert result.plan.choice_question["kind"] == "order_basis"


@pytest.mark.asyncio
async def test_an_explicit_sequence_via_selected_ids_is_asked_about_for_now(monkeypatch) -> None:
    """KNOWN LIMIT, not a false positive (prod thread aed98bf6 shape): the sequence rides
    in `selected_media_ids` (m001..m008) but no renderer is proven to honour that order, so
    the contract cannot verify it. It stays on the ask path until the route resolver
    (PR-D/F) can pin a selected order. The twin record is
    `clarify-explicit-sequence-via-selection-asks-for-now`."""
    ids = [f"c{i:02d}" for i in range(8)]
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1..8 in that sequence"
    )
    planned = _planned(media_scope="selected", selected_media_ids=ids)
    result = await _gate(monkeypatch, planned, rows=_rows(8), brief=_brief(rule))
    assert result.plan.choice_question["kind"] == "order_basis"
    assert "specific order" in result.plan.response


# P3 --------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["whatever", "I don't care", "i don't mind", "Keep 15 seconds"])
def test_a_bare_whatever_is_not_a_delegation(phrase) -> None:
    assert delegated_choice(_q(), phrase) is None
    assert delegated_choice(_q(), "up to you") is not None


@pytest.mark.asyncio
async def test_a_creator_without_a_brief_binding_is_not_asked_brief_questions(monkeypatch) -> None:
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", True)
    planned = replace(_planned(seconds=15), media_snapshot={"clip_assignments": _rows(30)})
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=_brief(_timing(15))))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: False)
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )
    assert result.plan.mode == "act"  # nothing would refuse it, so nothing is asked


def test_a_single_option_is_a_statement_not_a_choice() -> None:
    from app.services.choice_questions import choice_question_text

    strategy = CreativeStrategy(edit_format="montage")
    (offered,) = collect_conflicts(
        strategy,
        _brief(_timing(15)),
        {"clip_assignments": _rows(30)},
        ChoiceCapability(max_duration_s=20),
    )
    text = choice_question_text(offered.candidate())
    assert "Which do you prefer?" not in text and "The most I can do is" in text
    assert "Keep 15 seconds with 18 clips" in text


# P2-5: a later restatement supersedes the answer --------------------------------


async def _answered(monkeypatch, *, clips=31, option="extend"):  # noqa: ANN001, ANN202
    rows, brief = _rows(clips), _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    answer = _picked(asked[1]["choice_question"], option)
    return rows, brief, [asked, tag_event("user", answer[1], None, "Extend it")]


@pytest.mark.asyncio
async def test_extend_gives_24_8_and_the_answer_turn_echo_keeps_it(monkeypatch) -> None:
    rows, brief, events = await _answered(monkeypatch)
    result = await _gate(
        monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=tuple(events)
    )
    assert _strategy(result)["target_duration_s"] == 24.8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "model_seconds"),
    [
        ("no I want exactly 15 seconds with all of them", 15),
        ("make it shorter", 20),
        ("make it shorter", 15),
    ],
)
async def test_a_later_restatement_is_never_silently_overridden(
    monkeypatch, message, model_seconds
) -> None:
    rows, brief, events = await _answered(monkeypatch)
    events.append(tag_event("user", {}, None, message))
    result = await _gate(
        monkeypatch,
        _planned(seconds=model_seconds),
        rows=rows,
        brief=brief,
        events=tuple(events),
    )
    # It asks once more (the old answer is dropped, not re-applied) ...
    assert result.plan.turn_value == "question" and not result.plan.intents
    again = _asked(result)
    # ... and once that second ask is spent the plan goes through UNCHANGED, never 24.8.
    events += [again, tag_event("user", {}, None, message)]
    spent = await _gate(
        monkeypatch,
        _planned(seconds=model_seconds),
        rows=rows,
        brief=brief,
        events=tuple(events),
    )
    strategy = _strategy(spent)
    assert strategy["target_duration_s"] == model_seconds and "choice_answers" not in strategy


@pytest.mark.asyncio
async def test_a_later_message_that_keeps_the_answered_value_keeps_the_answer(monkeypatch) -> None:
    rows, brief, events = await _answered(monkeypatch)
    events.append(tag_event("user", {}, None, "also make the title bigger"))
    result = await _gate(
        monkeypatch, _planned(seconds=24.8), rows=rows, brief=brief, events=tuple(events)
    )
    assert _strategy(result)["choice_answers"][0]["option"] == "extend"


@pytest.mark.asyncio
async def test_a_verbatim_resend_is_not_a_restatement(monkeypatch) -> None:
    rows, brief = _rows(31), _brief(_timing(15))
    original = tag_event("user", {}, None, "Keep 15 seconds, all clips")
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    answer = tag_event("user", _picked(asked[1]["choice_question"], "extend")[1], None, "Extend it")
    resent = tag_event("user", {}, None, "Keep 15 seconds, all clips")
    result = await _gate(
        monkeypatch,
        _planned(seconds=15),
        rows=rows,
        brief=brief,
        events=(original, asked, answer, resent),
    )
    assert _strategy(result)["target_duration_s"] == 24.8
