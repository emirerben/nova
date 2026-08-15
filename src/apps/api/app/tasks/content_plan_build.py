"""Celery task: generate a 30-day content plan from a persona.

Off-Job work — enqueued with a plain `.delay()` from the content-plans route.
Loads the `content_plans` row + its `personas` row, runs
`ContentPlanGeneratorAgent`, and writes the resulting `plan_items`. Failure is
non-fatal: the plan row goes to `plan_status='failed'` + `error_detail` and the
user can retry. Partial garbage is never persisted — the agent's `parse()`
clamps/dedupes before this task ever sees items.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import structlog
from celery.exceptions import Retry
from sqlalchemy import select

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.content_plan import (
    CONTENT_PLAN_PROMPT_VERSION,
    ContentPlanInput,
    ContentPlanOutput,
)
from app.agents._schemas.persona import Persona
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.database import sync_session
from app.models import ContentPlan, Job, PlanItem, User
from app.models import Persona as PersonaRow
from app.services.content_plan_dedup import choose_replacements, flag_replacement_indices
from app.services.content_plan_persona import (
    PlanPersonaOwnershipError,
    load_owned_plan_persona_sync,
)
from app.services.job_status import PLAN_ITEM_JOB_TERMINAL
from app.services.seed_provenance import match_specs_to_seeds
from app.worker import celery_app

log = structlog.get_logger()


def _plan_epoch(plan: ContentPlan) -> int:
    """Return the ownership generation captured around off-database work."""
    return int(getattr(plan, "ownership_epoch", 0) or 0)


def _coerce_dispatch_epoch(value: object) -> int | None:
    """Normalize the producer-bound ownership epoch.

    Tokenless pre-R1 deliveries are epoch 0. They can finish only while the plan
    is still epoch 0; containment/repair increments the row and makes the same
    old delivery stale. Booleans, negative values, and non-integers are invalid.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _lock_owned_plan_persona(
    session,  # noqa: ANN001
    plan_id: uuid.UUID,
    *,
    expected_epoch: int | None = None,
) -> tuple[ContentPlan, PersonaRow] | None:
    """Lock and validate a plan/persona pair in the global mutation order.

    The caller must acquire any PlanItem and Job locks only after this helper.
    A changed epoch, quarantine, missing persona, or owner mismatch is the same
    fail-closed condition: no plan-derived write may land.
    """
    # populate_existing is required, not decorative. When this session already
    # touched the row without a lock, SQLAlchemy returns the cached instance and
    # does NOT write the freshly locked row onto it: the SELECT ... FOR UPDATE
    # serializes correctly, but the Python attributes stay pre-lock. The epoch
    # comparison below would then read a stale ownership_epoch and wave through
    # the exact stale worker this fence exists to stop.
    plan = session.get(ContentPlan, plan_id, with_for_update=True, populate_existing=True)
    if plan is None:
        return None
    persona = load_owned_plan_persona_sync(session, plan, for_update=True)
    if expected_epoch is not None and _plan_epoch(plan) != expected_epoch:
        raise PlanPersonaOwnershipError(plan)
    return plan, persona


def _lock_plan_items(session, items: list[PlanItem]) -> list[PlanItem]:  # noqa: ANN001
    """Lock existing items after Plan and Persona, in deterministic id order."""
    locked: list[PlanItem] = []
    for item in sorted(items, key=lambda row: str(row.id)):
        # Every caller passes list(plan.items) -- already-loaded relationship
        # objects -- so the identity map is always populated here and the locked
        # re-read would otherwise hand back pre-lock attribute values.
        current = session.get(PlanItem, item.id, with_for_update=True, populate_existing=True)
        if current is not None:
            locked.append(current)
    return locked


def _analysis_summary(tiktok_profile: dict | None) -> str:
    """Extract the pre-rendered TikTok analysis summary from a persona's tiktok_profile JSONB.

    Mirrors app.tasks.persona_build._analysis_summary — inlined to avoid a cross-task
    import. Returns "" when the analysis hasn't landed yet (race) or the enrich failed.
    """
    if not tiktok_profile:
        return ""
    analysis = tiktok_profile.get("official_analysis") or tiktok_profile.get("analysis") or {}
    return str(analysis.get("summary_for_prompts") or "")


@celery_app.task(
    name="app.tasks.content_plan_build.generate_content_plan",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def generate_content_plan(
    self,
    plan_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Generate plan_items for `content_plans.id == plan_id` and persist them."""
    pid = uuid.UUID(str(plan_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("content_plan_build.missing_dispatch_epoch", plan_id=plan_id)
        return
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("content_plan_build.invalid_persona", plan_id=plan_id)
            return
        if owned is None:
            log.warning("content_plan_build.missing_row", plan_id=plan_id)
            return
        plan, persona_row = owned
        ownership_epoch = _plan_epoch(plan)
        if not persona_row.persona:
            _fail(session, plan, "persona is not ready")
            return
        tiktok_summary = _analysis_summary(persona_row.tiktok_profile)
        from app.config import settings as _settings  # noqa: PLC0415

        user_style = dict(persona_row.style) if persona_row.style else None
        instruction_level = "full"
        preferred_edit_format_mix: dict[str, float] = {}
        if _settings.user_style_enabled and user_style:
            instruction_level = str(user_style.get("instruction_level", "full") or "full")
            raw_mix = user_style.get("preferred_edit_format_mix") or {}
            if isinstance(raw_mix, dict):
                preferred_edit_format_mix = {
                    str(k): float(v) for k, v in raw_mix.items() if isinstance(v, (int, float))
                }
        # M1 Bring-Your-Own-Ideas: extract seed texts from the persona row.
        # Keep full dicts (id + text) so T5 provenance matching can write
        # source_idea_seed_id at persist time. Empty list → byte-identical
        # baseline (no user-ideas block injected).
        raw_seeds = persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        seeds_with_ids = [
            s for s in raw_seeds if isinstance(s, dict) and s.get("text") and s.get("id")
        ]
        idea_seed_texts = [str(s["text"]) for s in seeds_with_ids]
        try:
            persona = Persona(**persona_row.persona)
        except Exception as exc:  # noqa: BLE001 — invalid readiness payload
            _fail(session, plan, "persona is not ready")
            log.warning("content_plan_build.persona_not_ready", plan_id=plan_id, error=str(exc))
            return
        agent_input = ContentPlanInput(
            persona=persona,
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            tiktok_analysis=tiktok_summary,
            instruction_level=instruction_level,  # type: ignore[arg-type]
            preferred_edit_format_mix=preferred_edit_format_mix,
            user_idea_seeds=idea_seed_texts,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        output = _dedup_and_replace(agent, agent_input, output, plan_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("content_plan_build.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("content_plan_build.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _fail(session, owned[0], str(exc))
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("content_plan_build.stale_result", plan_id=plan_id)
            return
        if owned is None:
            return
        plan, persona_row_p = owned
        # Replace any prior items (re-generation is idempotent per plan).
        for existing in _lock_plan_items(session, list(plan.items)):
            session.delete(existing)
        session.flush()
        # T5 provenance: match each generated spec back to the seed it
        # honours, then write source_idea_seed_id on the PlanItem.
        spec_list = list(output.items)
        seed_by_index = match_specs_to_seeds(spec_list, seeds_with_ids)
        for i, spec in enumerate(spec_list):
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=spec.day_index,
                    position=spec.day_index,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    rationale=spec.rationale or None,
                    edit_format=spec.edit_format,
                    # Stamp stable shot_id at persist time (D15) so assignments can
                    # survive rerolls without dangling positional pointers.
                    filming_guide=[
                        {**s.model_dump(), "shot_id": uuid.uuid4().hex} for s in spec.filming_guide
                    ],
                    item_status="idea",
                    source_idea_seed_id=seed_by_index.get(i),
                )
            )
        # Flip matched seeds → in_plan (monotonic: never demote).
        matched_seed_ids = set(seed_by_index.values())
        if matched_seed_ids:
            raw = persona_row_p.idea_seeds if isinstance(persona_row_p.idea_seeds, list) else []
            persona_row_p.idea_seeds = [
                {**s, "status": "in_plan"}
                if isinstance(s, dict)
                and s.get("id") in matched_seed_ids
                and s.get("status") != "in_plan"
                else s
                for s in raw
            ]
        plan.plan_status = "ready"
        if plan.start_date is None:
            plan.start_date = date.today()
        plan.prompt_version = CONTENT_PLAN_PROMPT_VERSION
        user = session.get(User, plan.user_id)
        if user is not None and user.onboarding_status in ("pending", "persona_ready"):
            user.onboarding_status = "plan_ready"
        session.commit()
    log.info("content_plan_build.ready", plan_id=plan_id, item_count=len(output.items))


def _fail(session, plan: ContentPlan, detail: str) -> None:  # noqa: ANN001
    # content_plans has no error_detail column (Phase 2 schema) — log + mark failed.
    # A failed plan is simply re-generatable from the route.
    log.warning("content_plan_build.mark_failed", plan_id=str(plan.id), detail=detail[:300])
    plan.plan_status = "failed"
    session.commit()


def _dedup_and_replace(
    agent: ContentPlanGeneratorAgent,
    agent_input: ContentPlanInput,
    output: ContentPlanOutput,
    plan_id: str,
) -> ContentPlanOutput:
    """Replace near-duplicate ideas via one constrained regeneration call.

    The whole-plan LLM pass self-imposes variety poorly (~1 in 5 plans repeats a
    concept). We detect near-dupes deterministically (services/content_plan_dedup),
    then re-invoke the SAME generator once with the kept ideas as an explicit
    "avoid these" list and swap the fresh, distinct ideas into the duplicate day
    slots — keeping each slot's day_index so the plan stays full-length.

    Best-effort by design: no dupes → no extra LLM call; a failed/short regen
    leaves the original plan untouched. Dedup must never degrade or fail a plan.
    """
    items = list(output.items)
    flagged = flag_replacement_indices(items)
    if not flagged:
        return output

    flagged_set = set(flagged)
    kept_ideas = [it.idea for i, it in enumerate(items) if i not in flagged_set]
    try:
        regen = agent.run(
            agent_input.model_copy(update={"exclude_ideas": kept_ideas}),
            ctx=RunContext(job_id=None),
        )
    except Exception as exc:  # noqa: BLE001 — dedup is best-effort, never fail the plan
        log.warning(
            "content_plan_dedup.regen_failed", plan_id=plan_id, flagged=len(flagged), error=str(exc)
        )
        return output

    replacements = choose_replacements(len(flagged), list(regen.items), kept_ideas)
    new_items = list(items)
    for slot_idx, repl in zip(flagged, replacements):  # zip stops short → unfilled slots kept
        new_items[slot_idx] = repl.model_copy(update={"day_index": items[slot_idx].day_index})
    new_items.sort(key=lambda it: it.day_index)
    log.info(
        "content_plan_dedup.replaced",
        plan_id=plan_id,
        flagged=len(flagged),
        replaced=len(replacements),
        candidates=len(regen.items),
    )
    return ContentPlanOutput(items=new_items)


@celery_app.task(
    name="app.tasks.content_plan_build.regenerate_content_plan",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def regenerate_content_plan(
    self,
    plan_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Re-tune a plan from the user's feedback (feedback loop, Phase 2).

    User-triggered (never silent). Rolls the user's video_feedback into a bounded
    `preference_summary`, persists it, regenerates the plan with that context, and
    replaces ONLY regenerable items — a day the user hand-edited (`user_edited`) OR
    already started rendering (`current_job_id`) is PROTECTED and kept byte-for-byte.
    This is the "their say" invariant: inferred feedback biases new ideas, but never
    overwrites an explicit edit or orphans an in-flight render.
    """
    from app.services.feedback_summary import rollup_user_feedback  # noqa: PLC0415

    pid = uuid.UUID(str(plan_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("content_plan_regen.missing_dispatch_epoch", plan_id=plan_id)
        return
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("content_plan_regen.invalid_persona", plan_id=plan_id)
            return
        if owned is None:
            log.warning("content_plan_regen.missing_row", plan_id=plan_id)
            return
        plan, persona_row = owned
        ownership_epoch = _plan_epoch(plan)
        if not persona_row.persona:
            _fail(session, plan, "persona is not ready")
            return
        summary = rollup_user_feedback(session, plan.user_id)
        plan.preference_summary = summary or None
        session.commit()
        tiktok_summary = _analysis_summary(persona_row.tiktok_profile)
        from app.config import settings as _settings  # noqa: PLC0415

        user_style = dict(persona_row.style) if persona_row.style else None
        instruction_level = "full"
        preferred_edit_format_mix: dict[str, float] = {}
        if _settings.user_style_enabled and user_style:
            instruction_level = str(user_style.get("instruction_level", "full") or "full")
            raw_mix = user_style.get("preferred_edit_format_mix") or {}
            if isinstance(raw_mix, dict):
                preferred_edit_format_mix = {
                    str(k): float(v) for k, v in raw_mix.items() if isinstance(v, (int, float))
                }
        # M1 Bring-Your-Own-Ideas: carry user seeds into the regenerate pass so the
        # "their say" invariant covers seeds too (regenerate biases toward what the
        # user explicitly said they want, not just their feedback reactions).
        # Keep full dicts (id + text) for T5 provenance matching at persist time.
        raw_seeds_regen = persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        seeds_with_ids_regen = [
            s for s in raw_seeds_regen if isinstance(s, dict) and s.get("text") and s.get("id")
        ]
        idea_seed_texts_regen = [str(s["text"]) for s in seeds_with_ids_regen]
        try:
            persona = Persona(**persona_row.persona)
        except Exception as exc:  # noqa: BLE001 — invalid readiness payload
            _fail(session, plan, "persona is not ready")
            log.warning("content_plan_regen.persona_not_ready", plan_id=plan_id, error=str(exc))
            return
        agent_input = ContentPlanInput(
            persona=persona,
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            preference_summary=summary or "",
            tiktok_analysis=tiktok_summary,
            instruction_level=instruction_level,  # type: ignore[arg-type]
            preferred_edit_format_mix=preferred_edit_format_mix,
            user_idea_seeds=idea_seed_texts_regen,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        output = _dedup_and_replace(agent, agent_input, output, plan_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("content_plan_regen.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("content_plan_regen.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _fail(session, owned[0], str(exc))
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("content_plan_regen.stale_result", plan_id=plan_id)
            return
        if owned is None:
            return
        plan, persona_row_p = owned
        locked_items = _lock_plan_items(session, list(plan.items))
        # PROTECTED days win: an item the user edited or already started rendering is
        # kept verbatim and never replaced. Everything else is regenerable.
        protected_days = {
            it.day_index for it in locked_items if it.user_edited or it.current_job_id is not None
        }
        for existing in locked_items:
            if existing.day_index not in protected_days:
                session.delete(existing)
        session.flush()
        # T5 provenance: match specs to seeds and write source_idea_seed_id.
        # Only count a seed as used when its spec is actually persisted (not on a
        # protected day) so we never flip a seed in_plan for a skipped spec.
        spec_list_regen = list(output.items)
        seed_by_index_regen = match_specs_to_seeds(spec_list_regen, seeds_with_ids_regen)
        matched_seed_ids_regen: set[str] = set()
        for i, spec in enumerate(spec_list_regen):
            if spec.day_index in protected_days:
                continue  # never collide with a protected day
            seed_id = seed_by_index_regen.get(i)
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=spec.day_index,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    rationale=spec.rationale or None,
                    edit_format=spec.edit_format,
                    # Stamp stable shot_id at persist time (D15).
                    filming_guide=[
                        {**s.model_dump(), "shot_id": uuid.uuid4().hex} for s in spec.filming_guide
                    ],
                    item_status="idea",
                    source_idea_seed_id=seed_id,
                )
            )
            if seed_id:
                matched_seed_ids_regen.add(seed_id)
        # Flip matched seeds → in_plan (monotonic: never demote).
        if matched_seed_ids_regen:
            raw = persona_row_p.idea_seeds if isinstance(persona_row_p.idea_seeds, list) else []
            persona_row_p.idea_seeds = [
                {**s, "status": "in_plan"}
                if isinstance(s, dict)
                and s.get("id") in matched_seed_ids_regen
                and s.get("status") != "in_plan"
                else s
                for s in raw
            ]
        plan.plan_status = "ready"
        if plan.start_date is None:
            plan.start_date = date.today()
        plan.prompt_version = CONTENT_PLAN_PROMPT_VERSION
        session.commit()
    log.info(
        "content_plan_regen.ready",
        plan_id=plan_id,
        protected=len(protected_days),
        has_summary=bool(summary),
    )


# Throttled queue: per-item generative renders are heavy (3 variants each). The
# worker consumes `plan-jobs` with --concurrency=1 so generate-first-week can't
# fire 7 simultaneous renders and OOM the 6GB worker (plan T3). See fly.toml.
PLAN_JOBS_QUEUE = "plan-jobs"


DispatchOutcome = Literal[
    "dispatched",
    "already_active",
    "invalid_clips",
    "invalid_persona",
    "missing_row",
    "publish_failed",
    "proposal_required",
    "proposal_draft",
    "proposal_stale",
    "proposal_analyzing",
]


@dataclass(frozen=True)
class DispatchResult:
    """Typed outcome of the PlanItem → render dispatch (plans/014).

    outcome:
      dispatched     — Job minted + orchestrator enqueued (job_id set)
      already_active — a non-terminal render already exists (job_id = that job)
      invalid_clips  — no clips, or build_generative_job rejected them
      invalid_persona — the plan/persona ownership fence rejected the request
                        before a Job was minted or queued
      missing_row    — item/plan row not found (or malformed id)
      publish_failed — Job minted but the broker publish failed; the Job row was
                       flipped to processing_failed/dispatch_publish_failed so it
                       can never sit as a forever-"queued" ghost (the reaper
                       deliberately never reaps `queued` — see tasks/reaper.py)
    """

    outcome: DispatchOutcome
    job_id: str | None = None


def _narrative_clip_order(item: PlanItem, clip_paths: list[str]) -> tuple[list[str], int]:
    """Reorder clip_paths so guide-shot clips lead IN GUIDE ORDER, pool after.

    Returns (ordered_paths, narrative_shot_count). narrative_shot_count == 0
    means no usable guide ordering (no guide, no shot-bound clips, or stale
    shot_ids) — callers then dispatch with today's behavior, unchanged.

    clip_assignments stores {gcs_path, shot_id} in attach-request order, which
    is client-controlled; the filming guide's shot sequence is the narrative
    truth, so we derive the order from the guide. shot_ids are re-validated
    against the live guide (a reroll can demote assignments; stale ids become
    pool clips). Paths in clip_paths but not in clip_assignments (legacy rows)
    join the pool tail in their existing order.
    """
    guide = list(item.filming_guide or [])
    assignments = list(item.clip_assignments or [])
    if not guide or not assignments:
        return clip_paths, 0
    paths_by_shot: dict[str, list[str]] = {}
    for a in assignments:
        if not isinstance(a, dict):
            continue
        sid = str(a.get("shot_id") or "")
        path = str(a.get("gcs_path") or "")
        if sid and path:
            paths_by_shot.setdefault(sid, []).append(path)
    known_paths = set(clip_paths)
    ordered: list[str] = []
    for shot in guide:
        sid = str(shot.get("shot_id") or "")
        for path in paths_by_shot.get(sid, []):
            if path in known_paths and path not in ordered:
                ordered.append(path)
    if not ordered:
        return clip_paths, 0
    pool = [p for p in clip_paths if p not in ordered]
    log.info(
        "plan_item_render.narrative_order",
        plan_item_id=str(item.id),
        shot_clips=len(ordered),
        pool_clips=len(pool),
    )
    return ordered + pool, len(ordered)


def _dispatch_item_render(
    session,  # noqa: ANN001
    item: PlanItem,
    plan: ContentPlan,
    persona_data: dict,
    *,
    ownership_epoch: int,
) -> DispatchResult:
    """Mint a generative Job for an item's clips, persist it, dispatch its render.

    The single source of truth for the PlanItem → render contract, shared by the
    per-item generate path (`dispatch_item_render_for` — route AND task) and the
    activation seed. Reuses the generative pipeline verbatim:
    build_generative_job (shared with the public route) →
    orchestrate_generative_job UNCHANGED. The only plan-specific bits are
    mode="content_plan", the content_plan_item_id reverse link, and the throttled
    queue. Item render state is derived from this Job's status at read time (no
    PlanItem status write — plan T2). Never raises (plans/014 C7: a publish
    failure must not abort the activation loop's remaining items) — every exit
    is a typed DispatchResult.

    `item.clip_gcs_paths` must already be set on the session before calling.
    """
    from app.config import settings  # noqa: PLC0415
    from app.schemas.montage_preset import coerce_montage_preset  # noqa: PLC0415
    from app.services.generative_jobs import (  # noqa: PLC0415
        CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
        build_generative_job,
    )
    from app.services.job_dispatch import enqueue_orchestrator_sync  # noqa: PLC0415
    from app.services.smart_captions import (  # noqa: PLC0415
        resolve_smart_captions_context_sync,
    )
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    approved_proposal: dict | None = None
    if settings.guided_edit_capability_enabled or settings.guided_edit_enforcement_enabled:
        from app.services.edit_proposals import (  # noqa: PLC0415
            mark_edit_proposal_stale,
            validate_approved_proposal_media_sync,
        )

        proposal_error, approved_proposal = validate_approved_proposal_media_sync(
            session, item, owner_id=plan.user_id
        )
        if proposal_error:
            if proposal_error == "proposal_stale":
                mark_edit_proposal_stale(item)
                session.commit()
            if settings.guided_edit_enforcement_enabled:
                return DispatchResult(proposal_error)
            approved_proposal = None

    content_plan_id = plan.id
    plan_item_id = item.id
    clip_paths = list(item.clip_gcs_paths or [])
    if not clip_paths and approved_proposal is not None:
        # Asset-only guided stories are valid. build_generative_job still needs
        # one server-validated seed/raw path for its generic Job contract, but
        # the strict worker reads every exact source from the approved snapshot.
        approved_snapshot = approved_proposal["snapshot"]
        selected_ids = {
            media_id for beat in approved_snapshot["story_beats"] for media_id in beat["media_ids"]
        }
        first_selected = next(
            (
                ref["gcs_path"]
                for ref in approved_snapshot["media"]
                if ref["media_id"] in selected_ids
            ),
            None,
        )
        if first_selected:
            clip_paths = [first_selected]
    if not clip_paths:
        log.warning("plan_item_render.no_clips", plan_item_id=str(item.id))
        return DispatchResult("invalid_clips")
    # Narrative clip order (filming-guide alignment): reorder clip_paths so the
    # guide's shot clips come first IN GUIDE ORDER (clip_assignments stores them
    # in attach-request order, which is client-controlled and not the guide
    # order), pool clips after. narrative_shot_count tells the render path how
    # many of the leading paths form the narrative spine.
    clip_paths, narrative_shot_count = _narrative_clip_order(item, clip_paths)
    smart_context = resolve_smart_captions_context_sync(
        user_id=plan.user_id,
        edit_format=str(item.edit_format or "montage"),
        requested=getattr(item, "smart_captions_enabled", False) is True,
        sound_design_enabled=(getattr(item, "smart_sound_design_enabled", None) is not False),
        db=session,
    )
    try:
        job = build_generative_job(
            user_id=plan.user_id,
            clip_paths=clip_paths,
            mode="content_plan",
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=ownership_epoch,
            persona_tone=str(persona_data.get("tone", "") or ""),
            persona_pillars=list(persona_data.get("content_pillars", []) or []),
            item_theme=str(item.theme or ""),
            item_idea=str(item.idea or ""),
            # Feedback-loop steer for future hooks: the plan's bounded preference
            # summary rides the same persona channel down to intro_writer.
            preference_summary=str(plan.preference_summary or ""),
            # The plan's declared edit shape → render archetype dispatch.
            edit_format=str(item.edit_format or "montage"),
            # Deep TikTok analysis — the creator's proven style, threaded down to
            # intro_writer so the hook voice matches what already works for them.
            tiktok_summary=str(persona_data.get("_tiktok_summary", "") or ""),
            # Per-user persistent style (Creator Agent M1). Private key from
            # _load_persona_data — not part of the public persona schema.
            user_style=persona_data.get("_user_style"),
            # Filming guide (Creator Agent M3 / B2). Plain plan data, threaded down
            # to intro_writer so the hook voice reflects the intended shots.
            filming_guide=list(item.filming_guide or []),
            # Narrated-walkthrough: user-recorded voiceover rides all_candidates so the
            # narrated archetype can force-align the script and per-step trim the clips.
            voiceover_gcs_path=str(item.voiceover_gcs_path) if item.voiceover_gcs_path else None,
            # Landscape-clip preference (plan-item editor). Defaults to "fit" via the
            # column server_default; getattr guard tolerates pre-column in-flight rows.
            landscape_fit=str(getattr(item, "landscape_fit", "fit") or "fit"),
            # Montage visual preset. Defaults to "classic"; the builder omits it
            # from all_candidates unless the user selected the masonry preset.
            montage_preset=coerce_montage_preset(getattr(item, "montage_preset", None)),
            # Original-audio bed level for the narrated archetype (None → Kria default).
            voiceover_bed_level=(
                float(item.voiceover_bed_level) if item.voiceover_bed_level is not None else None
            ),
            # Narrated caption style ("sentence" | "word"; None → sentence captions).
            voiceover_caption_style=(
                str(item.voiceover_caption_style) if item.voiceover_caption_style else None
            ),
            # Filming-guide alignment: how many leading clip_paths are guide
            # shots (in guide order). 0 = no narrative ordering (pure greedy).
            narrative_shot_count=narrative_shot_count,
            # Creator clip notes (feedback #3) — ride all_candidates for
            # render-time consumers + admin/debug.
            clip_notes={
                a["gcs_path"]: a["user_note"]
                for a in (item.clip_assignments or [])
                if isinstance(a, dict) and a.get("gcs_path") and a.get("user_note")
            },
            variant_policy=CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
            smart_captions=smart_context,
        )
    except ValueError as exc:
        log.warning("plan_item_render.invalid_clips", plan_item_id=str(item.id), error=str(exc))
        return DispatchResult("invalid_clips")
    if approved_proposal is not None:
        snapshot = dict(job.assembly_plan or {})
        snapshot["guided_edit"] = {
            "proposal_version": approved_proposal["proposal_version"],
            "media_digest": approved_proposal["media_digest"],
            "approved_proposal": approved_proposal["snapshot"],
            "media_identities": [
                {
                    "lane": ref["lane"],
                    "media_id": ref["media_id"],
                    "gcs_path": ref["gcs_path"],
                    "generation": ref["generation"],
                    "kind": ref["kind"],
                }
                for ref in approved_proposal["snapshot"]["media"]
            ],
        }
        job.assembly_plan = snapshot
    # Caller holds Plan -> Persona -> PlanItem locks and has revalidated this
    # exact epoch. Job is last in the global lock/write order.
    if _plan_epoch(plan) != ownership_epoch:
        return DispatchResult("invalid_persona")
    session.add(job)
    session.flush()  # populate job.id
    item.current_job_id = job.id
    # task_id == job id (the orchestrator contract); persist it before commit
    # so the admin/reaper can correlate the Celery task with the Job row.
    job.celery_task_id = str(job.id)
    job_id = str(job.id)
    session.commit()

    # Dispatch onto the throttled plan-jobs queue (concurrency=1 worker) via the
    # shared sync helper — keeps celery_task_id correlation and routes the queue
    # without bypassing the job_dispatch contract (guarded in tests).
    try:
        enqueue_orchestrator_sync(orchestrate_generative_job, job_id, queue=PLAN_JOBS_QUEUE)
    except Exception as exc:  # noqa: BLE001
        # Containment (plans/014 A1/C4): the Job row is already committed, and
        # the reaper deliberately never reaps `queued` — an uncontained publish
        # failure would strand a forever-"queued" ghost the item reads as
        # "generating". Flip it terminal so the UI shows failed + retry.
        #
        # CONDITIONAL on status still being "queued" (review 2026-08-04, CX1):
        # apply_async raising does not prove Redis rejected the message. If it
        # was actually delivered, the worker may already have flipped the job
        # to "processing" — an unconditional write would mark a RUNNING render
        # failed and invite a duplicate. rowcount 0 ⇒ the worker owns it ⇒
        # report dispatched (the render is genuinely under way).
        try:
            owned = _lock_owned_plan_persona(
                session,
                content_plan_id,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning(
                "plan_item_render.publish_failed_stale_owner",
                plan_item_id=str(item.id),
                job_id=job_id,
            )
            return DispatchResult("invalid_persona")
        if owned is None:
            return DispatchResult("missing_row", job_id=job_id)
        locked_item = session.get(PlanItem, plan_item_id, with_for_update=True)
        if locked_item is None or locked_item.content_plan_id != content_plan_id:
            return DispatchResult("missing_row", job_id=job_id)
        locked_job = session.get(
            Job,
            uuid.UUID(job_id),
            populate_existing=True,
            with_for_update=True,
        )
        if locked_job is None:
            return DispatchResult("missing_row", job_id=job_id)
        if (
            locked_job.status == "processing_failed"
            and locked_job.failure_reason == "dispatch_publish_failed"
        ):
            # The central dispatch helper already won its queued-only recovery
            # CAS.  Preserve the caller contract instead of treating that
            # terminalized row as evidence that a worker claimed it.
            return DispatchResult("publish_failed", job_id=job_id)
        if locked_job.status != "queued":
            log.warning(
                "plan_item_render.publish_raised_but_claimed",
                plan_item_id=str(item.id),
                job_id=job_id,
                error=str(exc),
            )
            return DispatchResult("dispatched", job_id=job_id)
        locked_job.status = "processing_failed"
        locked_job.failure_reason = "dispatch_publish_failed"
        locked_job.error_detail = "The render couldn't be handed to the queue. Give it another go."
        session.commit()
        log.error(
            "plan_item_render.publish_failed",
            plan_item_id=str(item.id),
            job_id=job_id,
            error=str(exc),
        )
        return DispatchResult("publish_failed", job_id=job_id)
    log.info("plan_item_render.dispatched", plan_item_id=str(item.id), job_id=job_id)
    return DispatchResult("dispatched", job_id=job_id)


def _persona_data(persona_row: PersonaRow) -> dict:
    """Build the private render snapshot from an already-owned persona row.

    Includes `_tiktok_summary` (the pre-rendered TikTok analysis summary) as a
    private key so _dispatch_item_render can thread it down to build_generative_job
    without changing the public persona schema. The underscore prefix prevents
    accidental use as an LLM field.
    """
    if not persona_row.persona:
        raise ValueError("persona is not ready")
    data = dict(persona_row.persona)
    data["_tiktok_summary"] = _analysis_summary(persona_row.tiktok_profile)
    # Thread the per-user style (Creator Agent M1) under a private key so
    # _dispatch_item_render can pass it to build_generative_job without
    # polluting the public persona schema fields.
    data["_user_style"] = dict(persona_row.style) if persona_row.style else None
    return data


def _load_persona_data(session, plan: ContentPlan) -> dict:  # noqa: ANN001
    """Load a render snapshot only through the compound owner predicate."""
    return _persona_data(load_owned_plan_persona_sync(session, plan))


def dispatch_item_render_for(
    plan_item_id: str,
    expected_ownership_epoch: int | None = None,
) -> DispatchResult:
    """Load + lock a plan item, re-check for an active render, then dispatch.

    The ONE entry point shared by the interactive generate route (which runs
    it in a worker thread — plans/014) and the generate_plan_item_videos task,
    so the two paths can never drift. The SELECT … FOR UPDATE on the item row
    makes the active-render re-check race-safe: two concurrent Generate
    requests serialize here — the first mints, the second sees the fresh
    current_job_id inside the lock and returns already_active.
    `jobs.content_plan_item_id` has no uniqueness constraint (retries mint new
    rows by design), so this lock is the duplicate-mint guard for every
    minting path (the activation loop applies the same lock+re-check inline).

    This remains a task-side trust boundary even when a route already checked
    ownership: delayed deliveries and direct Celery calls must fail closed too.
    """
    with sync_session() as session:
        try:
            item_uuid = uuid.UUID(str(plan_item_id))
        except (TypeError, ValueError):
            log.warning("plan_item_videos.bad_item_id", plan_item_id=str(plan_item_id))
            return DispatchResult("missing_row")
        expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
        if expected_ownership_epoch is None:
            log.error("plan_item_videos.invalid_dispatch_epoch", plan_item_id=plan_item_id)
            return DispatchResult("invalid_persona")
        # Resolve the parent without a lock, then acquire every mutation lock in
        # the global Plan -> Persona -> PlanItem -> Job order.
        item_ref = session.get(PlanItem, item_uuid)
        if item_ref is None:
            log.warning("plan_item_videos.missing_item", plan_item_id=plan_item_id)
            return DispatchResult("missing_row")
        try:
            owned = _lock_owned_plan_persona(
                session,
                item_ref.content_plan_id,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("plan_item_videos.invalid_persona", plan_item_id=plan_item_id)
            return DispatchResult("invalid_persona")
        if owned is None:
            log.warning("plan_item_videos.missing_plan", plan_item_id=plan_item_id)
            return DispatchResult("missing_row")
        plan, persona_row = owned
        ownership_epoch = _plan_epoch(plan)
        # The unlocked item_ref read above already cached this row, so without
        # populate_existing the lock serializes but current_job_id stays at its
        # pre-lock value -- two concurrent Generate posts would each see None and
        # each mint a Job, which is the precise duplicate this lock prevents.
        item = session.get(PlanItem, item_uuid, with_for_update=True, populate_existing=True)
        if item is None or item.content_plan_id != plan.id:
            log.warning("plan_item_videos.missing_item", plan_item_id=plan_item_id)
            return DispatchResult("missing_row")
        if item.current_job_id is not None:
            current = session.get(
                Job, item.current_job_id, with_for_update=True, populate_existing=True
            )
            if current is not None and current.status not in PLAN_ITEM_JOB_TERMINAL:
                return DispatchResult("already_active", job_id=str(current.id))
        if not persona_row.persona:
            log.error("plan_item_videos.persona_not_ready", plan_item_id=plan_item_id)
            return DispatchResult("invalid_persona")
        persona_data = _persona_data(persona_row)
        return _dispatch_item_render(
            session,
            item,
            plan,
            persona_data,
            ownership_epoch=ownership_epoch,
        )


@celery_app.task(
    name="app.tasks.content_plan_build.generate_plan_item_videos",
    bind=True,
    max_retries=1,
    default_retry_delay=15,
)
def generate_plan_item_videos(
    self,
    plan_item_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Mint a generative Job for a plan item's themed clips and dispatch its render."""
    result = dispatch_item_render_for(str(plan_item_id), expected_ownership_epoch)
    if result.outcome == "invalid_persona":
        # Legacy/direct Celery deliveries must be visibly failed rather than
        # proceeding with a Job; this error log is its terminal task outcome.
        log.error("plan_item_videos.invalid_persona", plan_item_id=str(plan_item_id))
        return
    if result.outcome not in ("dispatched", "already_active"):
        log.warning(
            "plan_item_videos.not_dispatched",
            plan_item_id=str(plan_item_id),
            outcome=result.outcome,
        )


# Activation seed (T8): how many plan items one seed batch may auto-generate. Each
# render lands on the throttled plan-jobs queue, so this is a "show the user range"
# cap, not a throughput limit. Kept in sync with ClipPlanMatcherInput.max_assignments.
_AUTO_GENERATE_LIMIT = 2


@celery_app.task(
    name="app.tasks.content_plan_build.activate_content_plan",
    bind=True,
    max_retries=0,
)
def activate_content_plan(
    self,
    plan_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Match a plan's seed clips to its items and auto-generate the top picks.

    The content-plan activation seed: analyze the user's uploaded seed batch with
    clip_metadata, run clip_plan_matcher to assign best-fit clips to plan items,
    and dispatch a render for the top items so the user sees a finished video
    before any per-item themed upload.

    Best-effort by design — a generative job never hard-fails the plan. Failure to
    download/analyze, an empty match, or a matcher error all land the plan in a
    terminal activation_status (`failed` / `activated_empty`) with the items
    untouched; the user keeps their full plan and falls back to per-item uploads.
    """
    import tempfile  # noqa: PLC0415

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.clip_plan_matcher import (  # noqa: PLC0415
        ClipPlanMatcherAgent,
        ClipPlanMatcherInput,
        ClipSummary,
        PlanItemSummary,
    )
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.tasks.generative_build import _ingest_clips  # noqa: PLC0415

    pid = uuid.UUID(str(plan_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("activate_plan.missing_dispatch_epoch", plan_id=plan_id)
        return
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("activate_plan.invalid_persona", plan_id=plan_id)
            return
        if owned is None:
            log.warning("activate_plan.missing_row", plan_id=plan_id)
            return
        plan, persona_row = owned
        ownership_epoch = _plan_epoch(plan)
        if not persona_row.persona:
            _set_activation(session, plan, "failed")
            log.warning("activate_plan.persona_not_ready", plan_id=plan_id)
            return
        seed_paths = list(plan.seed_clip_paths or [])
        if not seed_paths:
            _set_activation(session, plan, "failed")
            log.warning("activate_plan.no_seed_clips", plan_id=plan_id)
            return
        locked_items = _lock_plan_items(session, list(plan.items))
        items = [
            PlanItemSummary(
                item_id=str(it.id),
                theme=it.theme or "",
                idea=it.idea or "",
                filming_suggestion=it.filming_suggestion or "",
            )
            for it in locked_items
        ]
        persona_data = _persona_data(persona_row)
        plan.activation_started_at = datetime.now(UTC)
        plan.activation_phase = "matching_clips"
        _set_activation(session, plan, "activating")

    if not items:
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("activate_plan.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _set_activation(session, owned[0], "activated_empty")
        return

    # Synthetic non-UUID trace scope (no single Job owns this) — matches the
    # `track:<id>` off-job convention; agent_run persistence is skipped for it.
    trace_scope = f"activation-{plan_id}"
    try:
        with pipeline_trace_for(trace_scope), tempfile.TemporaryDirectory() as tmpdir:
            ingest = _ingest_clips(seed_paths, tmpdir, job_id=trace_scope)
            clip_id_to_gcs: dict[str, str] = ingest["clip_id_to_gcs"]
            clips: list[ClipSummary] = []
            for meta in ingest["clip_metas"]:
                gcs = clip_id_to_gcs.get(getattr(meta, "clip_id", ""))
                if not gcs:
                    continue
                clips.append(
                    ClipSummary(
                        clip_gcs_path=gcs,
                        hook_text=str(getattr(meta, "hook_text", "") or ""),
                        hook_score=float(getattr(meta, "hook_score", 0.0) or 0.0),
                        detected_subject=str(getattr(meta, "detected_subject", "") or ""),
                        transcript_excerpt=str(getattr(meta, "transcript", "") or ""),
                    )
                )
            if not clips:
                raise ValueError("no seed clip produced a usable metadata summary")
            agent = ClipPlanMatcherAgent(default_client())
            matched = agent.run(
                ClipPlanMatcherInput(
                    clips=clips, items=items, max_assignments=_AUTO_GENERATE_LIMIT
                ),
                ctx=RunContext(job_id=None),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; never hard-fail the plan
        log.warning("activate_plan.match_failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("activate_plan.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _set_activation(session, owned[0], "activated_empty")
        return

    # Group assignments by item (the matcher caps assignment count, but two clips
    # could still target one item) → set that item's clips and dispatch one render.
    by_item: dict[str, list[str]] = {}
    for a in matched.assignments:
        by_item.setdefault(a.item_id, []).append(a.clip_gcs_path)

    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("activate_plan.stale_result", plan_id=plan_id)
            return
        if owned is not None:
            _set_activation_phase(session, owned[0], "picking_days")

    dispatched = 0
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("activate_plan.stale_result", plan_id=plan_id)
            return
        if owned is None:
            return
        _set_activation_phase(session, owned[0], "starting_renders")

    for item_id, paths in by_item.items():
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("activate_plan.stale_result", plan_id=plan_id)
                return
            if owned is None:
                return
            plan, _persona_row = owned
            # FOR UPDATE + active-render skip (review 2026-08-04, CA2/CX2): the
            # activation analysis runs for minutes while the user can attach
            # clips and hit Generate on the same items. Without the lock +
            # re-check, activation would silently clobber the user's clip
            # assignments AND mint a second Job over their in-flight render.
            # Same guard as dispatch_item_render_for — every minting path locks.
            item = session.get(PlanItem, uuid.UUID(item_id), with_for_update=True)
            if item is None or item.content_plan_id != plan.id:
                continue
            if item.current_job_id is not None:
                current = session.get(Job, item.current_job_id, with_for_update=True)
                if current is not None and current.status not in PLAN_ITEM_JOB_TERMINAL:
                    log.info(
                        "activate_plan.skip_active_item",
                        plan_id=plan_id,
                        item_id=item_id,
                        job_id=str(item.current_job_id),
                    )
                    continue
            # Assign the matched seed clip(s) to the item server-side. NOTE: these
            # paths live under the plan's `.../seed/` prefix, NOT the item's
            # `.../{item_id}/` prefix that the public attach_clips route enforces.
            # That route check guards untrusted user input; here we are trusted
            # server code assigning a clip the user already owns under the same
            # plan, and build_generative_job only requires the `users/` allowlist —
            # so no GCS copy is needed. Do NOT "fix" this by adding a per-item
            # prefix check: it would break activation.
            # Route through set_item_clips (D16 single-writer contract).
            from app.services.plan_clips import ClipAssignment, set_item_clips  # noqa: PLC0415

            set_item_clips(item, [ClipAssignment(gcs_path=p, shot_id=None) for p in paths])
            session.flush()
            result = _dispatch_item_render(
                session,
                item,
                plan,
                persona_data,
                ownership_epoch=ownership_epoch,
            )
            if result.outcome == "invalid_persona":
                log.warning("activate_plan.invalid_persona", plan_id=plan_id)
                return
            if result.outcome == "dispatched":
                dispatched += 1

    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("activate_plan.stale_result", plan_id=plan_id)
            return
        if owned is not None:
            _set_activation(
                session,
                owned[0],
                "activated" if dispatched else "activated_empty",
            )
    log.info("activate_plan.done", plan_id=plan_id, dispatched=dispatched)


def _set_activation(session, plan: ContentPlan, status_value: str) -> None:  # noqa: ANN001
    plan.activation_status = status_value
    session.commit()


def _set_activation_phase(session, plan: ContentPlan, phase: str) -> None:  # noqa: ANN001
    plan.activation_phase = phase
    session.add(plan)
    session.commit()


# Footage pool (dogfood feedback #4): how many pending items one pool match may
# fill. Unlike the activation seed there is NO auto-render (the user keeps/swaps
# first), so this is a spread cap, not a render-budget cap. MUST stay within
# ClipPlanMatcherInput.max_assignments' schema bound (le=7) — pinned by
# test_pool_match_limit_within_matcher_schema.
_POOL_MATCH_LIMIT = 7


def _set_pool_status(session, plan: ContentPlan, status_value: str) -> None:  # noqa: ANN001
    pool = dict(plan.pool or {})
    pool["status"] = status_value
    pool["updated_at"] = datetime.now(UTC).isoformat()
    plan.pool = pool
    session.add(plan)
    session.commit()


@celery_app.task(
    name="app.tasks.content_plan_build.match_pool_clips",
    bind=True,
    max_retries=0,
    # Celery time-limit invariant (CLAUDE.md / prod 08532ba3): must stay strictly
    # under the broker visibility_timeout (1900s) or a long ingest gets redelivered
    # and double-runs — duplicate Gemini spend + tmpfs blowout. Mirrors the render
    # orchestrators. Pinned by tests/tasks/test_task_time_limits.py.
    soft_time_limit=1740,
    time_limit=1800,
)
def match_pool_clips(
    self,
    plan_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Distribute the plan's footage pool across PENDING items (provisional).

    Activation's sibling, with three deliberate differences: it matches only
    UNMATCHED pool clips into items that have no clips yet, attaches them as
    machine_matched provisional assignments (dashed "Matched — keep?" chips;
    the conformance judge skips them until the user touches the slot), and it
    NEVER auto-renders — the user keeps/swaps, then generates.

    Best-effort: any failure (including the soft time limit) lands
    pool.status="match_failed" with items untouched; the user can hit
    "Match again".
    """
    pid = uuid.UUID(str(plan_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("pool_match.missing_dispatch_epoch", plan_id=plan_id)
        return
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("pool_match.invalid_persona", plan_id=plan_id)
            return
        if owned is None:
            log.warning("pool_match.missing_row", plan_id=plan_id)
            return
        ownership_epoch = _plan_epoch(owned[0])

    try:
        _run_pool_match(plan_id, ownership_epoch=ownership_epoch)
    except Exception as exc:  # noqa: BLE001
        # Never let the pool wedge in "matching" forever — ANY failure (soft time
        # limit, a DB error in the write-back block that the inner try/except
        # doesn't cover, a worker kill) flips the status so the UI shows
        # "Match again" instead of polling indefinitely.
        log.warning("pool_match.failed_terminal", plan_id=plan_id, error=str(exc)[:300])
        try:
            with sync_session() as session:
                try:
                    owned = _lock_owned_plan_persona(
                        session,
                        pid,
                        expected_epoch=ownership_epoch,
                    )
                except PlanPersonaOwnershipError:
                    log.warning("pool_match.stale_result", plan_id=plan_id)
                    return
                if owned is not None and (owned[0].pool or {}).get("status") == "matching":
                    _set_pool_status(session, owned[0], "match_failed")
        except Exception:  # noqa: BLE001 — preserve the original task failure
            pass
        raise


def _run_pool_match(plan_id: str, *, ownership_epoch: int | None = None) -> None:
    """Inner body of match_pool_clips (separated so the soft-time-limit handler
    can wrap it and still mark the pool failed)."""
    import tempfile  # noqa: PLC0415

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.clip_plan_matcher import (  # noqa: PLC0415
        ClipPlanMatcherAgent,
        ClipPlanMatcherInput,
        ClipSummary,
        PlanItemSummary,
    )
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.services.plan_clips import ClipAssignment, set_item_clips  # noqa: PLC0415
    from app.tasks.generative_build import _ingest_clips  # noqa: PLC0415

    pid = uuid.UUID(str(plan_id))
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("pool_match.invalid_persona", plan_id=plan_id)
            return
        if owned is None:
            log.warning("pool_match.missing_row", plan_id=plan_id)
            return
        plan, _persona_row = owned
        if ownership_epoch is None:
            ownership_epoch = _plan_epoch(plan)
        pool = dict(plan.pool or {})
        pool_clips = [c for c in pool.get("clips", []) if isinstance(c, dict) and c.get("gcs_path")]
        unmatched = [c["gcs_path"] for c in pool_clips if not c.get("matched_item_id")]
        if not unmatched:
            _set_pool_status(session, plan, "matched_empty" if not pool_clips else "matched")
            return
        locked_items = _lock_plan_items(session, list(plan.items))
        # Pending = items the pool may fill: no render yet, no clips yet.
        items = [
            PlanItemSummary(
                item_id=str(it.id),
                theme=it.theme or "",
                idea=it.idea or "",
                filming_suggestion=it.filming_suggestion or "",
            )
            for it in locked_items
            if it.current_job_id is None and not (it.clip_gcs_paths or [])
        ]
        _set_pool_status(session, plan, "matching")

    if not items:
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("pool_match.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _set_pool_status(session, owned[0], "matched_empty")
        return

    trace_scope = f"pool-match-{plan_id}"
    try:
        with pipeline_trace_for(trace_scope), tempfile.TemporaryDirectory() as tmpdir:
            # min_success_fraction=0.0: matching WHATEVER analyzed beats matching
            # nothing — a Gemini 503 spike on half the batch must not abort the
            # pool (unmatched clips stay listed with "Match again").
            ingest = _ingest_clips(unmatched, tmpdir, job_id=trace_scope, min_success_fraction=0.0)
            clip_id_to_gcs: dict[str, str] = ingest["clip_id_to_gcs"]
            clips: list[ClipSummary] = []
            for meta in ingest["clip_metas"]:
                gcs = clip_id_to_gcs.get(getattr(meta, "clip_id", ""))
                if not gcs:
                    continue
                clips.append(
                    ClipSummary(
                        clip_gcs_path=gcs,
                        hook_text=str(getattr(meta, "hook_text", "") or ""),
                        hook_score=float(getattr(meta, "hook_score", 0.0) or 0.0),
                        detected_subject=str(getattr(meta, "detected_subject", "") or ""),
                        transcript_excerpt=str(getattr(meta, "transcript", "") or ""),
                    )
                )
            if not clips:
                raise ValueError("no pool clip produced a usable metadata summary")
            matched = ClipPlanMatcherAgent(default_client()).run(
                ClipPlanMatcherInput(clips=clips, items=items, max_assignments=_POOL_MATCH_LIMIT),
                ctx=RunContext(job_id=None),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; items stay untouched
        log.warning("pool_match.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("pool_match.stale_result", plan_id=plan_id)
                return
            if owned is not None:
                _set_pool_status(session, owned[0], "match_failed")
        return

    by_item: dict[str, list[str]] = {}
    for a in matched.assignments:
        by_item.setdefault(a.item_id, []).append(a.clip_gcs_path)

    assigned_paths: dict[str, str] = {}  # gcs_path → item_id actually attached
    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                pid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("pool_match.stale_result", plan_id=plan_id)
            return
        if owned is None:
            return
        plan, _persona_row = owned
        locked_by_id = {str(item.id): item for item in _lock_plan_items(session, list(plan.items))}
        for item_id, paths in by_item.items():
            item = locked_by_id.get(item_id)
            if item is None or item.content_plan_id != plan.id:
                continue
            if item.current_job_id is not None or (item.clip_gcs_paths or []):
                continue  # raced: item got footage/render since the load
            # Same trusted-server prefix argument as activation seed paths.
            set_item_clips(
                item,
                [ClipAssignment(gcs_path=p, shot_id=None, machine_matched=True) for p in paths],
            )
            session.flush()
            for p in paths:
                assigned_paths[p] = item_id

        # Write back per-clip match results + terminal status.
        pool = dict(plan.pool or {})
        clips_out = []
        for c in pool.get("clips", []):
            if not isinstance(c, dict):
                continue
            entry = dict(c)
            if entry.get("gcs_path") in assigned_paths:
                entry["matched_item_id"] = assigned_paths[entry["gcs_path"]]
            clips_out.append(entry)
        pool["clips"] = clips_out
        pool["status"] = "matched" if assigned_paths else "matched_empty"
        pool["updated_at"] = datetime.now(UTC).isoformat()
        plan.pool = pool
        session.add(plan)
        session.commit()
    log.info("pool_match.done", plan_id=plan_id, assigned=len(assigned_paths))


@celery_app.task(
    name="app.tasks.content_plan_build.reroll_plan_item",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def reroll_plan_item(
    self,
    item_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Re-generate the idea for a single plan item.

    Mirrors _dedup_and_replace: runs ContentPlanGeneratorAgent with all
    current plan ideas excluded, picks one fresh replacement via
    choose_replacements, patches the target item in-place preserving
    day_index. Failure is best-effort — resets item_status to 'idea' so
    the user's original idea survives.
    """
    iid = uuid.UUID(str(item_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("reroll_plan_item.missing_dispatch_epoch", item_id=item_id)
        return
    with sync_session() as session:
        item_ref = session.get(PlanItem, iid)
        if item_ref is None:
            log.warning("reroll_plan_item.missing_item", item_id=item_id)
            return
        try:
            owned = _lock_owned_plan_persona(
                session,
                item_ref.content_plan_id,
                expected_epoch=expected_ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.error("reroll_plan_item.invalid_persona", item_id=item_id)
            return
        if owned is None:
            log.warning("reroll_plan_item.missing_plan", item_id=item_id)
            return
        plan, persona_row = owned
        content_plan_id = plan.id
        ownership_epoch = _plan_epoch(plan)
        locked_items = _lock_plan_items(session, list(plan.items))
        item = next((row for row in locked_items if row.id == iid), None)
        if item is None:
            log.warning("reroll_plan_item.missing_item", item_id=item_id)
            return

        # Collect all current ideas to exclude so the fresh idea is distinct.
        all_ideas = [it.idea for it in locked_items if it.idea]

        if not persona_row.persona:
            item.item_status = "idea"
            session.commit()
            log.warning("reroll_plan_item.persona_not_ready", item_id=item_id)
            return
        try:
            persona = Persona(**persona_row.persona)
        except Exception as exc:  # noqa: BLE001 — invalid readiness payload, no agent call
            item.item_status = "idea"
            session.commit()
            log.warning("reroll_plan_item.persona_not_ready", item_id=item_id, error=str(exc))
            return
        # M1: carry seeds into reroll so the replacement idea still respects the
        # user's stated intent (best-effort; no seeds = byte-identical to prior).
        raw_seeds_reroll = (
            persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        )
        idea_seed_texts_reroll = [
            str(s["text"]) for s in raw_seeds_reroll if isinstance(s, dict) and s.get("text")
        ]
        agent_input = ContentPlanInput(
            persona=persona,
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            exclude_ideas=all_ideas,
            user_idea_seeds=idea_seed_texts_reroll,
        )
        original_day_index = item.day_index

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        replacements = choose_replacements(1, list(output.items), all_ideas)
    except Exception as exc:  # noqa: BLE001
        log.warning("reroll_plan_item.failed", item_id=item_id, error=str(exc))
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    content_plan_id,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("reroll_plan_item.stale_result", item_id=item_id)
                return
            if owned is None:
                return
            item = session.get(PlanItem, iid, with_for_update=True)
            if item is not None and item.content_plan_id == content_plan_id:
                item.item_status = "idea"
                session.commit()
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        try:
            owned = _lock_owned_plan_persona(
                session,
                content_plan_id,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("reroll_plan_item.stale_result", item_id=item_id)
            return
        if owned is None:
            return
        item = session.get(PlanItem, iid, with_for_update=True)
        if item is None or item.content_plan_id != content_plan_id:
            return

        if not replacements:
            # Generator returned nothing usable — silently keep old idea.
            log.info("reroll_plan_item.no_replacement", item_id=item_id)
            item.item_status = "idea"
            session.commit()
            return

        fresh = replacements[0]
        # Patch the item in-place, keeping day_index.
        item.theme = fresh.theme
        item.idea = fresh.idea
        item.filming_suggestion = fresh.filming_suggestion or None
        item.rationale = fresh.rationale or None
        # Stamp fresh shot_ids (D15) — old ids are gone, old assignments dangle.
        item.filming_guide = [
            {**s.model_dump(), "shot_id": uuid.uuid4().hex} for s in (fresh.filming_guide or [])
        ]
        item.edit_format = fresh.edit_format or "montage"
        item.item_status = "idea"
        item.user_edited = False

        # Reroll demote (D15): move all shot-assigned clips to the pool so they
        # remain visible as extra footage rather than dangling against the new guide.
        # The read-time reconciliation in plan_item_response is a safety net; this
        # explicit demote makes the intent clear in the write path.
        from app.services.plan_clips import ClipAssignment, set_item_clips  # noqa: PLC0415

        existing_assignments = item.clip_assignments or []
        # Demote shot → pool but carry the per-clip metadata: user_note is about
        # the CLIP not the slot, and machine_matched must survive a reroll
        # (dropping them silently wiped creator context — review finding).
        demoted = [
            ClipAssignment(
                gcs_path=a["gcs_path"],
                shot_id=None,
                user_note=str(a.get("user_note") or ""),
                machine_matched=bool(a.get("machine_matched")),
            )
            for a in existing_assignments
            if isinstance(a, dict) and a.get("gcs_path")
        ]
        set_item_clips(item, demoted)

        session.commit()

    log.info(
        "reroll_plan_item.done",
        item_id=item_id,
        day_index=original_day_index,
        new_idea=replacements[0].idea if replacements else None,
    )


@celery_app.task(
    name="app.tasks.content_plan_build.generate_ideas_into_plan",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
    soft_time_limit=120,
    time_limit=180,
)
def generate_ideas_into_plan(
    self,
    plan_id: str,
    generation_token: str | None = None,
    expected_ownership_epoch: int | None = None,
) -> None:  # noqa: ANN001
    """Generate exactly one fresh unscheduled AI idea for a content plan."""
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    pid = uuid.UUID(str(plan_id))
    expected_ownership_epoch = _coerce_dispatch_epoch(expected_ownership_epoch)
    if expected_ownership_epoch is None:
        log.error("generate_ideas_into_plan.missing_dispatch_epoch", plan_id=plan_id)
        return
    generation_started_at = (
        datetime.fromisoformat(generation_token) if generation_token is not None else None
    )

    def _is_current_attempt(plan: ContentPlan) -> bool:
        if plan.plan_status != "generating":
            return False
        if generation_token is None:
            return True
        started_at = plan.generation_started_at
        return started_at is not None and started_at == generation_started_at

    def _mark_failed(expected_epoch: int | None) -> None:
        if expected_epoch is None:
            return
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=expected_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("generate_ideas_into_plan.stale_result", plan_id=plan_id)
                return
            if owned is not None and _is_current_attempt(owned[0]):
                owned[0].plan_status = "failed"
                session.commit()

    ownership_epoch: int | None = None
    try:
        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=expected_ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.error("generate_ideas_into_plan.invalid_persona", plan_id=plan_id)
                return
            if owned is None:
                return
            plan, persona_row = owned
            if not _is_current_attempt(plan):
                log.info(
                    "generate_ideas_into_plan.stale_delivery",
                    plan_id=plan_id,
                    plan_status=plan.plan_status,
                )
                return
            ownership_epoch = _plan_epoch(plan)

            if not persona_row.persona:
                plan.plan_status = "failed"
                session.commit()
                log.warning("generate_ideas_into_plan.persona_not_ready", plan_id=plan_id)
                return
            try:
                persona = Persona(**dict(persona_row.persona))
            except Exception as exc:  # noqa: BLE001 — invalid readiness payload
                plan.plan_status = "failed"
                session.commit()
                log.warning(
                    "generate_ideas_into_plan.persona_not_ready",
                    plan_id=plan_id,
                    error=str(exc),
                )
                return

            existing_items = list(plan.items or [])
            events_text = str((plan.events or {}).get("text", "") or "")
            exclude_ideas = [it.idea for it in existing_items if it.idea]

        agent_input = ContentPlanInput(
            persona=persona,
            events=events_text,
            horizon_days=1,
            exclude_ideas=exclude_ideas,
            user_idea_seeds=[],
        )
        with pipeline_trace_for(pid):
            agent = ContentPlanGeneratorAgent(default_client())
            output = agent.run(agent_input, ctx=RunContext(job_id=None))
            new_specs = list(output.items)[:1]
        if not new_specs:
            log.warning("generate_ideas_into_plan.fresh_empty", plan_id=plan_id)
            raise RuntimeError("fresh idea generation returned no items")

        with sync_session() as session:
            try:
                owned = _lock_owned_plan_persona(
                    session,
                    pid,
                    expected_epoch=ownership_epoch,
                )
            except PlanPersonaOwnershipError:
                log.warning("generate_ideas_into_plan.stale_result", plan_id=plan_id)
                return
            if owned is None:
                return
            plan = owned[0]
            if not _is_current_attempt(plan):
                log.info(
                    "generate_ideas_into_plan.stale_result",
                    plan_id=plan_id,
                    plan_status=plan.plan_status,
                )
                return
            spec = new_specs[0]
            # The agent ran outside the transaction.  A creator may have added
            # an idea meanwhile, so lock and reload the full child set only
            # after the Plan/Persona pair is locked, then derive placement from
            # the current rows rather than the stale pre-agent snapshot.
            locked_items = list(
                session.execute(
                    select(PlanItem)
                    .where(PlanItem.content_plan_id == plan.id)
                    .order_by(PlanItem.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                .scalars()
                .all()
            )
            # Simple mock sessions used by unit tests do not materialize query
            # rows; the relationship is equivalent there.  Real PostgreSQL
            # always takes the explicit row locks above.
            if not locked_items:
                locked_items = list(plan.items or [])
            next_position = (
                max(
                    (it.position for it in locked_items if it.position is not None),
                    default=0,
                )
                + 1
            )
            new_idea_key = (spec.idea or "").strip().casefold()
            if new_idea_key and any(
                (it.idea or "").strip().casefold() == new_idea_key for it in locked_items
            ):
                # Retry from a fresh exclusion snapshot instead of persisting a
                # duplicate returned against stale context.
                raise RuntimeError("fresh idea duplicated a concurrently added item")
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=None,
                    position=next_position,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    rationale=spec.rationale or None,
                    edit_format=spec.edit_format or "montage",
                    filming_guide=[
                        {**s.model_dump(), "shot_id": uuid.uuid4().hex}
                        for s in (spec.filming_guide or [])
                    ],
                    item_status="idea",
                )
            )
            plan.plan_status = "ready"
            session.commit()
        log.info(
            "generate_ideas_into_plan.fresh_done",
            plan_id=plan_id,
            added=len(new_specs),
        )
    except Exception as exc:  # noqa: BLE001
        retries = int(getattr(self.request, "retries", 0) or 0)
        max_retries = int(self.max_retries or 0)
        if retries < max_retries:
            log.warning(
                "generate_ideas_into_plan.fresh_retry",
                plan_id=plan_id,
                retry=retries + 1,
                max_retries=max_retries,
                error=str(exc),
            )
            try:
                raise self.retry(exc=exc) from exc
            except Retry:
                raise
            except Exception:  # noqa: BLE001 — retry publish itself failed
                _mark_failed(ownership_epoch)
                raise
        log.warning(
            "generate_ideas_into_plan.fresh_failed",
            plan_id=plan_id,
            retries=retries,
            error=str(exc),
        )
        _mark_failed(ownership_epoch)
        raise
