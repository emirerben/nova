"""Replay a request-following fixture through the real turn paths and score it.

Engines (per turn):

* `recorded` — the fixture carries the outcome (a prod capture or an authored reference).
* `v1_copilot` — the recorded *model output* is replayed through the REAL v1 copilot path:
  `EditCopilotAgent` -> `_honest_outcome` -> `compile_editor_ops` (the same call chain
  `execute_copilot_edit` runs, minus the DB row locks). The reply and the resulting edit are
  computed, not stored, so a change to any of those functions moves the score.
* `v2_kria` — `app.kria.replay.replay_thread` (multi-turn) supplies the reply; v2 replay only
  inspects a project, so the resulting edit is the recorded one.

Replay mode is offline and runs in CI. `mode="live"` swaps the recorded model text for a real
Gemini call on `v1_copilot` turns and is opt-in (same cost guards as the rest of `tests/evals`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from .models import (
    FinalPlan,
    Footage,
    PlanText,
    RFFixture,
    ThreadResult,
    Turn,
    TurnResult,
)
from .scorer import score_thread

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "request_following"
FOOTAGE_DIR = FIXTURE_ROOT / "footage"
THREAD_DIR = FIXTURE_ROOT / "threads"

Mode = Literal["replay", "live"]


# ── Loading ──────────────────────────────────────────────────────────────────


def load_footage(footage_id: str, root: Path = FOOTAGE_DIR) -> Footage:
    return Footage.model_validate_json((root / f"{footage_id}.json").read_text(encoding="utf-8"))


def load_fixture(path: Path) -> RFFixture:
    return RFFixture.model_validate_json(path.read_text(encoding="utf-8"))


def discover_fixture_paths(root: Path = THREAD_DIR) -> list[Path]:
    return sorted(root.glob("*.json")) if root.exists() else []


# ── v1 copilot engine ────────────────────────────────────────────────────────


def _plan_with_texts(plan: FinalPlan, elements: list[dict[str, Any]]) -> FinalPlan:
    """Project committed editor text elements back onto the plan, keeping the role a text
    lane already had (the editor payload does not carry title/label roles)."""
    roles = {t.id: t for t in plan.texts}
    texts: list[PlanText] = []
    for element in elements:
        if element.get("removed"):
            continue
        known = roles.get(str(element.get("id")))
        texts.append(
            PlanText(
                id=str(element.get("id")),
                role=known.role if known else "other",
                text=str(element.get("text") or ""),
                start_s=float(element.get("start_s") or 0.0),
                end_s=float(element.get("end_s") or 0.0),
                font_family=element.get("font_family"),
                clip_id=known.clip_id if known else None,
            )
        )
    return plan.model_copy(update={"texts": texts})


def replay_v1_copilot(
    turn: Turn, plan_before: FinalPlan, *, mode: Mode = "replay"
) -> tuple[FinalPlan, str, list[str]]:
    """Run one recorded copilot turn through the real v1 chat-edit path."""
    from app.agents.edit_copilot import EditCopilotAgent
    from app.routes._copilot import _honest_outcome
    from app.services.creation_editor_actions import _applied_summary, _rejection_reply
    from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops

    from ..runners.eval_runner import CassetteModelClient, build_eval_run_context
    from ..runners.snapshot_variant import build_synthetic_variant_and_job

    payload = turn.copilot or {}
    agent_input = payload["agent_input"]
    if mode == "live":
        from app.agents._model_client import default_client

        client = default_client()
    else:
        client = CassetteModelClient(str(payload["raw_text"]))
    output = EditCopilotAgent(client).run(
        agent_input,
        ctx=build_eval_run_context(f"request-following:{turn.turn_id}", is_live=mode == "live"),
    )

    # The same gate run_copilot_turn applies before it derives the honest outcome.
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(output, ops, supports_proposed=True)
    notes = [f"copilot outcome={outcome}", f"ops={[op.get('op') for op in ops]}"]
    if not ops:
        return plan_before, reply, notes

    job, variant = build_synthetic_variant_and_job(agent_input.get("variant_snapshot") or {}, ops)
    try:
        compiled = compile_editor_ops(job, variant, ops)
    except KriaEditorOpError as exc:
        notes.append(f"compile rejected: {exc}")
        return plan_before, _rejection_reply(exc), notes

    changes = list(compiled.changes)
    notes.append(f"changes={changes}")
    elements = compiled.payload.text_elements
    plan_after = _plan_with_texts(plan_before, elements) if elements is not None else plan_before
    if compiled.payload.timeline_slots is not None:
        notes.append("timeline_slots changed but are not projected by this harness")
    return plan_after, f"{_applied_summary(changes)}. Everything else is unchanged.", notes


# ── Thread replay ────────────────────────────────────────────────────────────


def _v2_traces(fixture: RFFixture) -> dict[str, Any]:
    from app.kria.replay import KriaReplayFixture, replay_thread

    v2_turns = [t for t in fixture.turns if t.engine == "v2_kria"]
    if not v2_turns:
        return {}
    fixtures = [KriaReplayFixture.model_validate(t.kria) for t in v2_turns]
    trace = replay_thread(fixture.fixture_id, fixtures)
    return {t.turn_id: tr for t, tr in zip(v2_turns, trace.traces, strict=True)}


def replay_turns(fixture: RFFixture, *, mode: Mode = "replay") -> list[TurnResult]:
    v2 = _v2_traces(fixture)
    results: list[TurnResult] = []
    current = FinalPlan()
    for turn in fixture.turns:
        if turn.engine == "v1_copilot":
            plan, reply, notes = replay_v1_copilot(turn, current, mode=mode)
            result = TurnResult(
                turn_id=turn.turn_id, engine=turn.engine, plan_after=plan, reply=reply, notes=notes
            )
        elif turn.recorded is None:
            # Authored and awaiting a recording: carry the edit forward, score nothing.
            result = TurnResult(
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=current,
                reply=None,
                unrecorded=True,
            )
        else:
            reply = (
                v2[turn.turn_id].response.message
                if turn.engine == "v2_kria"
                else turn.recorded.reply
            )
            result = TurnResult(
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=turn.recorded.plan_after,
                reply=reply,
            )
        results.append(result)
        current = result.plan_after
    return results


def run_thread(
    fixture: RFFixture, footage: Footage | None = None, *, mode: Mode = "replay"
) -> ThreadResult:
    footage = footage or load_footage(fixture.footage)
    turns = replay_turns(fixture, mode=mode)
    if any(t.unrecorded for t in turns):
        return ThreadResult(
            fixture_id=fixture.fixture_id,
            provenance=fixture.provenance,
            turns=turns,
            scores=[],
            unrecorded=True,
        )
    return ThreadResult(
        fixture_id=fixture.fixture_id,
        provenance=fixture.provenance,
        turns=turns,
        scores=score_thread(fixture, footage, turns),
    )


def score_reference(fixture: RFFixture, footage: Footage | None = None) -> ThreadResult:
    """Score the fixture's known-good outcome. Every requirement must come out `met`; if one
    does not, the checker (or the requirement) is unsatisfiable and cannot judge anything."""
    from .scorer import score

    footage = footage or load_footage(fixture.footage)
    reference = fixture.reference
    if reference is None:
        raise ValueError(f"{fixture.fixture_id} has no reference outcome")
    scores = score(
        fixture.requirements,
        reference.plan_after,
        reference.reply,
        footage=footage,
        previous_plan=reference.plan_before,
    )
    turn = TurnResult(
        turn_id="reference",
        engine="recorded",
        plan_after=reference.plan_after,
        reply=reference.reply,
    )
    return ThreadResult(
        fixture_id=fixture.fixture_id, provenance=fixture.provenance, turns=[turn], scores=scores
    )


def stamp_baseline(path: Path) -> dict[str, str]:
    """Replay a fully recorded fixture and pin today's statuses into its `baseline`.

    The pin is what makes a drift visible: when a phase lands and a status moves, the
    replay test fails until the baseline is re-stamped on purpose and the change is reviewed.
    """
    fixture = load_fixture(path)
    result = run_thread(fixture)
    if result.unrecorded:
        raise ValueError(f"{fixture.fixture_id} is not fully recorded; nothing to pin")
    baseline = {s.requirement_id: s.status for s in result.scores}
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["baseline"] = baseline
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return baseline


def dump_json(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True)
