"""Replay a request-following fixture through the real turn paths and score it.

Engines (per turn):

* `recorded` — the fixture carries the outcome (a prod capture or an authored reference).
* `v1_copilot` — the recorded *model output* is replayed through the REAL v1 copilot path:
  `EditCopilotAgent` -> `_honest_outcome` -> `compile_editor_ops` (the same call chain
  `execute_copilot_edit` runs, minus the DB row locks). The reply and the resulting edit are
  computed, not stored, so a change to any of those functions moves the score.
* `v2_kria` — cassettes execute the real agent, adapter, registry and edit compiler.
  Strategy cassettes compile an inert plan; inspection-only fixtures preserve state.
  Neither path borrows its result from a recorded final answer.

Replay mode is offline and runs in CI. `mode="live"` swaps the recorded model text for a real
Gemini call on `v1_copilot` turns and is opt-in (same cost guards as the rest of `tests/evals`).
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Literal

from .models import (
    EvalEvidence,
    FinalPlan,
    Footage,
    ObservedLiveBudget,
    PlanText,
    RFFixture,
    RolloutGateResult,
    ThreadResult,
    Turn,
    TurnExecutionProof,
    TurnResult,
)
from .scorer import score_thread

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "request_following"
FOOTAGE_DIR = FIXTURE_ROOT / "footage"
THREAD_DIR = FIXTURE_ROOT / "threads"

Mode = Literal["replay", "live"]


class _ObservedClient:
    """Record the actual model and prompt seen by an eval-only invocation."""

    def __init__(self, delegate: Any, *, fallback_model: str | None = None):
        self.delegate = delegate
        self.fallback_model = fallback_model
        self.model: str | None = None
        self.prompt: str | None = None
        self.calls = 0

    def invoke(self, **kwargs: Any):
        self.prompt = kwargs.get("prompt")
        self.calls += 1
        invocation = self.delegate.invoke(**kwargs)
        self.model = getattr(invocation, "model_used", None) or self.fallback_model
        return invocation


# ── Loading ──────────────────────────────────────────────────────────────────


def load_footage(footage_id: str, root: Path = FOOTAGE_DIR) -> Footage:
    return Footage.model_validate_json((root / f"{footage_id}.json").read_text(encoding="utf-8"))


def load_fixture(path: Path) -> RFFixture:
    return RFFixture.model_validate_json(path.read_text(encoding="utf-8"))


def fixture_hash(fixture: RFFixture) -> str:
    payload = json.dumps(
        fixture.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evaluate_rollout_gate(
    results: list[ThreadResult],
    *,
    required_fixture_ids: list[str] | tuple[str, ...] = (),
    observed_live_budget: ObservedLiveBudget | None = None,
) -> RolloutGateResult:
    """Classify evidence without promoting replay/compile-only work to a live rollout proof."""
    observed_ids = [result.fixture_id for result in results]
    reasons: list[str] = []
    missing = [fixture_id for fixture_id in required_fixture_ids if fixture_id not in observed_ids]
    if missing:
        reasons.append(f"missing required fixtures: {', '.join(missing)}")
    for result in results:
        if result.unrecorded:
            reasons.append(f"{result.fixture_id}: unrecorded")
        evidence = result.evidence
        if evidence is None or evidence.replay_only or evidence.execution_path == "replay_only":
            reasons.append(f"{result.fixture_id}: replay-only evidence")
        if not result.scores:
            reasons.append(f"{result.fixture_id}: no requirement coverage")
        if any(score.reply_overclaims for score in result.scores):
            reasons.append(f"{result.fixture_id}: reply overclaim")
        if any(score.status != "met" for score in result.scores):
            reasons.append(f"{result.fixture_id}: requirement not met")
        if any(
            "expected outcome disagreement" in note for turn in result.turns for note in turn.notes
        ):
            reasons.append(f"{result.fixture_id}: expected outcome disagreement")
    budget_bad = (
        observed_live_budget is None
        or observed_live_budget.source != "ledger"
        or not observed_live_budget.complete
        or observed_live_budget.spent_usd is None
        or observed_live_budget.spent_usd > observed_live_budget.cap_usd
    )
    if budget_bad:
        reasons.append("live budget evidence incomplete or exhausted")
    quality_failure = any(
        marker in reason
        for marker in ("reply overclaim", "requirement not met", "expected outcome disagreement")
        for reason in reasons
    )
    return RolloutGateResult(
        status="failed" if quality_failure else "incomplete" if reasons else "passed",
        reasons=reasons,
        required_fixture_ids=list(required_fixture_ids),
        observed_fixture_ids=observed_ids,
        compile_only_count=sum(
            bool(result.evidence and result.evidence.execution_path == "compile_only")
            for result in results
        ),
        rendered_count=0,
    )


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
                role=(
                    known.role
                    if known
                    else element.get("role")
                    if element.get("role") in {"title", "label", "other"}
                    else "other"
                ),
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


def _proof(observed: _ObservedClient, *, prompt_version: str, compiler: Any) -> TurnExecutionProof:
    return TurnExecutionProof(
        execution_path="compile_only",
        model_actual=observed.model,
        prompt_hash=(
            hashlib.sha256(observed.prompt.encode("utf-8")).hexdigest()
            if observed.prompt is not None
            else None
        ),
        prompt_version=prompt_version,
        compiler_hash=hashlib.sha256(inspect.getsource(compiler).encode("utf-8")).hexdigest(),
        model_call_observed=observed.calls > 0,
    )


def replay_v2_editor(
    turn: Turn, plan_before: FinalPlan, *, mode: Mode = "replay"
) -> tuple[FinalPlan, str, list[str], TurnExecutionProof]:
    """Execute a cassette through the real v2 editor adapter and compiler.

    This stops at an in-memory compiled edit: staging, DB dispatch, device apply,
    save, render, and export are intentionally reported as unexecuted.
    """
    cassette = (turn.kria or {}).get("cassette") or {}
    payload = cassette.get("agent_input") or {}
    raw = cassette.get("model_output")
    if mode == "replay" and raw is None:
        raise ValueError(f"{turn.turn_id}: v2 cassette needs model_output")
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)

    from app.agents._model_client import default_client
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.kria.planner import adapt_editor_action
    from app.kria.registry import KRIA_TOOLS
    from app.routes._copilot import _honest_outcome
    from app.services.creation_editor_actions import _applied_summary, _rejection_reply
    from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
    from tests.evals.runners.eval_runner import CassetteModelClient, build_eval_run_context
    from tests.evals.runners.snapshot_variant import build_synthetic_variant_and_job

    observed = _ObservedClient(
        CassetteModelClient(str(raw)) if mode == "replay" else default_client(),
        fallback_model="cassette" if mode == "replay" else None,
    )
    snapshot = dict(payload.get("variant_snapshot") or payload.get("snapshot") or {})
    if plan_before.texts or plan_before.clips:
        # Fixture context may describe media, but mutable text comes from the
        # actual preceding compile. A later cassette cannot repair an earlier
        # failure by quietly injecting an expected editor result.
        seed_rows = {str(row.get("id")): row for row in snapshot.get("text_bars") or []}
        snapshot["text_bars"] = [
            {**seed_rows.get(text.id, {}), **text.model_dump(exclude_none=True)}
            for text in plan_before.texts
        ]
    agent_input = EditCopilotInput.model_validate(
        {
            "utterance": payload.get("utterance") or turn.user_message,
            "prior_turns": payload.get("prior_turns") or [],
            "variant_snapshot": snapshot,
            "original_request": payload.get("original_request"),
        }
    )
    output = EditCopilotAgent(observed).run(
        agent_input,
        ctx=build_eval_run_context(f"request-following:{turn.turn_id}", is_live=mode == "live"),
    )
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(output, ops, supports_proposed=True, message=turn.user_message)
    notes = [
        f"v2 editor outcome={outcome}",
        f"ops={[op.get('op') for op in ops]}",
        "unexecuted: stage/save/DB dispatch/device apply/render/export",
    ]
    proof = _proof(
        observed, prompt_version=EditCopilotAgent.spec.prompt_version, compiler=compile_editor_ops
    )
    if not ops:
        snapshot_plan = _plan_with_texts(
            FinalPlan(), list(agent_input.variant_snapshot.get("text_bars") or [])
        )
        return (
            (plan_before if (plan_before.clips or plan_before.texts) else snapshot_plan),
            reply,
            notes,
            proof,
        )

    typed_plan = adapt_editor_action(reply=reply, ops=ops)
    if not typed_plan.intents:
        notes.append("adapter returned no executable editor intent")
        return plan_before, reply, notes, proof
    intent = typed_plan.intents[0]
    registered = KRIA_TOOLS.get(intent.tool_name, intent.tool_version)
    operations = registered.arguments_model.model_validate(intent.arguments).operations
    snapshot_plan = _plan_with_texts(
        FinalPlan(), list(agent_input.variant_snapshot.get("text_bars") or [])
    )
    source_plan = plan_before if (plan_before.clips or plan_before.texts) else snapshot_plan
    job, variant = build_synthetic_variant_and_job(agent_input.variant_snapshot, operations)
    try:
        compiled = compile_editor_ops(job, variant, operations)
    except KriaEditorOpError as exc:
        notes.append(f"compile rejected: {exc}")
        return source_plan, _rejection_reply(exc), notes, proof
    notes.append(f"changes={list(compiled.changes)}")
    elements = compiled.payload.text_elements
    plan_after = _plan_with_texts(source_plan, elements) if elements is not None else source_plan
    if compiled.payload.timeline_slots is not None:
        notes.append("timeline slots compiled but cannot be projected into FinalPlan")
    return (
        plan_after,
        f"{_applied_summary(list(compiled.changes))}. Everything else is unchanged.",
        notes,
        proof,
    )


def replay_v2_strategy(
    turn: Turn, plan_before: FinalPlan, *, mode: Mode = "replay"
) -> tuple[FinalPlan, str, list[str], TurnExecutionProof]:
    """Replay main-creator output and compile its bounded strategy without fabricating media."""
    cassette = (turn.kria or {}).get("strategy_cassette") or {}
    payload = cassette.get("agent_input") or {}
    raw = cassette.get("model_output")
    if mode == "replay" and raw is None:
        raise ValueError(f"{turn.turn_id}: strategy cassette needs model_output")
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    from app.agents._model_client import default_client
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
    from app.kria.planner import adapt_creator_action
    from app.kria.registry import KRIA_TOOLS
    from app.services.creator_capabilities import compile_strategy_to_plan
    from tests.evals.runners.eval_runner import CassetteModelClient, build_eval_run_context

    observed = _ObservedClient(
        CassetteModelClient(str(raw)) if mode == "replay" else default_client(),
        fallback_model="cassette" if mode == "replay" else None,
    )
    output = MainCreatorAgent(observed).run(
        MainCreatorInput.model_validate(payload),
        ctx=build_eval_run_context(f"request-following:{turn.turn_id}", is_live=mode == "live"),
    )
    proof = _proof(
        observed,
        prompt_version=MainCreatorAgent.spec.prompt_version,
        compiler=compile_strategy_to_plan,
    )
    typed_plan = adapt_creator_action(output.action)
    if not typed_plan.intents:
        response = getattr(output.action, "question", None) or getattr(output.action, "summary", "")
        return plan_before, str(response), ["strategy planner returned no draft intent"], proof
    intent = typed_plan.intents[0]
    strategy = (
        KRIA_TOOLS.get(intent.tool_name, intent.tool_version)
        .arguments_model.model_validate(intent.arguments)
        .strategy
    )
    manifest = payload.get("capability_manifest")
    if not manifest:
        raise ValueError(f"{turn.turn_id}: strategy cassette needs capability_manifest")
    edit_plan = compile_strategy_to_plan(ResolvedCreatorManifest.model_validate(manifest), strategy)
    notes = [
        "strategy compiled to inert CreatorEditPlan; no timeline/render/export was fabricated",
        "unexecuted: draft persistence/DB dispatch/render/device export",
        f"commands={[command.command for command in edit_plan.commands]}",
        f"strategy title={edit_plan.strategy.opening_title!r}",
        f"strategy duration={edit_plan.strategy.target_duration_s}",
    ]
    return (
        plan_before,
        str(getattr(output.action, "summary", edit_plan.strategy.rationale)),
        notes,
        proof,
    )


# ── Thread replay ────────────────────────────────────────────────────────────


def _v2_traces(fixture: RFFixture) -> dict[str, Any]:
    v2_turns = [
        t
        for t in fixture.turns
        if t.engine == "v2_kria"
        and not (t.kria or {}).get("cassette")
        and not (t.kria or {}).get("strategy_cassette")
    ]
    if not v2_turns:
        return {}
    from app.kria.replay import KriaReplayFixture, replay_thread

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
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=plan,
                reply=reply,
                notes=notes,
                expected_plan_after=turn.expected_outcome.plan_after
                if turn.expected_outcome
                else None,
                expected_reply=turn.expected_outcome.reply if turn.expected_outcome else None,
            )
        elif turn.engine == "v2_kria" and (turn.kria or {}).get("cassette"):
            plan, reply, notes, proof = replay_v2_editor(turn, current, mode=mode)
            if turn.expected_outcome is not None and (
                plan != turn.expected_outcome.plan_after
                or (
                    turn.expected_outcome.reply is not None and reply != turn.expected_outcome.reply
                )
            ):
                notes.append("expected outcome disagreement: actual editor compile differs")
            result = TurnResult(
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=plan,
                reply=reply,
                notes=notes,
                expected_plan_after=turn.expected_outcome.plan_after
                if turn.expected_outcome
                else None,
                expected_reply=turn.expected_outcome.reply if turn.expected_outcome else None,
                execution_proof=proof,
            )
        elif turn.engine == "v2_kria" and (turn.kria or {}).get("strategy_cassette"):
            plan, reply, notes, proof = replay_v2_strategy(turn, current, mode=mode)
            if turn.expected_outcome is not None and (
                plan != turn.expected_outcome.plan_after
                or (
                    turn.expected_outcome.reply is not None and reply != turn.expected_outcome.reply
                )
            ):
                notes.append("expected outcome disagreement: actual strategy compile differs")
            result = TurnResult(
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=plan,
                reply=reply,
                notes=notes,
                expected_plan_after=turn.expected_outcome.plan_after
                if turn.expected_outcome
                else None,
                expected_reply=turn.expected_outcome.reply if turn.expected_outcome else None,
                execution_proof=proof,
            )
        elif turn.recorded is None:
            # Authored and awaiting a recording: carry the edit forward, score nothing.
            result = TurnResult(
                turn_id=turn.turn_id,
                engine=turn.engine,
                plan_after=current,
                reply=None,
                unrecorded=True,
                expected_plan_after=turn.expected_outcome.plan_after
                if turn.expected_outcome
                else None,
                expected_reply=turn.expected_outcome.reply if turn.expected_outcome else None,
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
                notes=(
                    ["runtime-v2 replay trace only; planner/compiler/staging/render unexecuted"]
                    if turn.engine == "v2_kria"
                    else []
                ),
                expected_plan_after=turn.expected_outcome.plan_after
                if turn.expected_outcome
                else None,
                expected_reply=turn.expected_outcome.reply if turn.expected_outcome else None,
            )
        results.append(result)
        current = result.plan_after
    return results


def run_thread(
    fixture: RFFixture, footage: Footage | None = None, *, mode: Mode = "replay"
) -> ThreadResult:
    footage = footage or load_footage(fixture.footage)
    turns = replay_turns(fixture, mode=mode)
    has_v2_trace_only = any(
        turn.engine == "v2_kria"
        and not (turn.kria or {}).get("cassette")
        and not (turn.kria or {}).get("strategy_cassette")
        for turn in fixture.turns
    )
    proofs = [turn.execution_proof for turn in turns if turn.execution_proof]
    evidence = EvalEvidence(
        fixture_hash=fixture_hash(fixture),
        execution_path="replay_only" if has_v2_trace_only or not proofs else "compile_only",
        model_actual=(
            "cassette"
            if mode == "replay" and proofs
            else (proofs[0].model_actual if proofs else None)
        ),
        prompt_version=next(
            (proof.prompt_version for proof in proofs if proof.prompt_version), None
        ),
        compiler_version=next(
            (proof.compiler_hash for proof in proofs if proof.compiler_hash), None
        ),
        replay_only=mode == "replay" or has_v2_trace_only,
        live_budget=fixture.live_budget if mode == "replay" else None,
    )
    if any(t.unrecorded for t in turns):
        return ThreadResult(
            fixture_id=fixture.fixture_id,
            provenance=fixture.provenance,
            turns=turns,
            scores=[],
            unrecorded=True,
            evidence=evidence,
        )
    return ThreadResult(
        fixture_id=fixture.fixture_id,
        provenance=fixture.provenance,
        turns=turns,
        scores=score_thread(fixture, footage, turns),
        evidence=evidence,
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
