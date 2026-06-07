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
from datetime import UTC, date, datetime

import structlog

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
from app.models import ContentPlan, PlanItem, User
from app.models import Persona as PersonaRow
from app.services.content_plan_dedup import choose_replacements, flag_replacement_indices
from app.worker import celery_app

log = structlog.get_logger()


def _analysis_summary(tiktok_profile: dict | None) -> str:
    """Extract the pre-rendered TikTok analysis summary from a persona's tiktok_profile JSONB.

    Mirrors app.tasks.persona_build._analysis_summary — inlined to avoid a cross-task
    import. Returns "" when the analysis hasn't landed yet (race) or the enrich failed.
    """
    if not tiktok_profile:
        return ""
    analysis = tiktok_profile.get("analysis") or {}
    return str(analysis.get("summary_for_prompts") or "")


@celery_app.task(
    name="app.tasks.content_plan_build.generate_content_plan",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def generate_content_plan(self, plan_id: str) -> None:  # noqa: ANN001
    """Generate plan_items for `content_plans.id == plan_id` and persist them."""
    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            log.warning("content_plan_build.missing_row", plan_id=plan_id)
            return
        persona_row = session.get(PersonaRow, plan.persona_id)
        if persona_row is None or not persona_row.persona:
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
        agent_input = ContentPlanInput(
            persona=Persona(**persona_row.persona),
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            tiktok_analysis=tiktok_summary,
            instruction_level=instruction_level,  # type: ignore[arg-type]
            preferred_edit_format_mix=preferred_edit_format_mix,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        output = _dedup_and_replace(agent, agent_input, output, plan_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("content_plan_build.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
            if plan is not None:
                _fail(session, plan, str(exc))
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            return
        # Replace any prior items (re-generation is idempotent per plan).
        for existing in list(plan.items):
            session.delete(existing)
        session.flush()
        for spec in output.items:
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=spec.day_index,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    rationale=spec.rationale or None,
                    edit_format=spec.edit_format,
                    filming_guide=[s.model_dump() for s in spec.filming_guide],
                    item_status="idea",
                )
            )
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
def regenerate_content_plan(self, plan_id: str) -> None:  # noqa: ANN001
    """Re-tune a plan from the user's feedback (feedback loop, Phase 2).

    User-triggered (never silent). Rolls the user's video_feedback into a bounded
    `preference_summary`, persists it, regenerates the plan with that context, and
    replaces ONLY regenerable items — a day the user hand-edited (`user_edited`) OR
    already started rendering (`current_job_id`) is PROTECTED and kept byte-for-byte.
    This is the "their say" invariant: inferred feedback biases new ideas, but never
    overwrites an explicit edit or orphans an in-flight render.
    """
    from app.services.feedback_summary import rollup_user_feedback  # noqa: PLC0415

    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            log.warning("content_plan_regen.missing_row", plan_id=plan_id)
            return
        persona_row = session.get(PersonaRow, plan.persona_id)
        if persona_row is None or not persona_row.persona:
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
        agent_input = ContentPlanInput(
            persona=Persona(**persona_row.persona),
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            preference_summary=summary or "",
            tiktok_analysis=tiktok_summary,
            instruction_level=instruction_level,  # type: ignore[arg-type]
            preferred_edit_format_mix=preferred_edit_format_mix,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        output = _dedup_and_replace(agent, agent_input, output, plan_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("content_plan_regen.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
            if plan is not None:
                _fail(session, plan, str(exc))
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            return
        # PROTECTED days win: an item the user edited or already started rendering is
        # kept verbatim and never replaced. Everything else is regenerable.
        protected_days = {
            it.day_index for it in plan.items if it.user_edited or it.current_job_id is not None
        }
        for existing in list(plan.items):
            if existing.day_index not in protected_days:
                session.delete(existing)
        session.flush()
        for spec in output.items:
            if spec.day_index in protected_days:
                continue  # never collide with a protected day
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=spec.day_index,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    rationale=spec.rationale or None,
                    edit_format=spec.edit_format,
                    filming_guide=[s.model_dump() for s in spec.filming_guide],
                    item_status="idea",
                )
            )
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


def _dispatch_item_render(
    session,  # noqa: ANN001
    item: PlanItem,
    plan: ContentPlan,
    persona_data: dict,
) -> str | None:
    """Mint a generative Job for an item's clips, persist it, dispatch its render.

    The single source of truth for the PlanItem → render contract, shared by the
    per-item generate task and the activation seed. Reuses the generative pipeline
    verbatim: build_generative_job (shared with the public route) →
    orchestrate_generative_job UNCHANGED. The only plan-specific bits are
    mode="content_plan", the content_plan_item_id reverse link, and the throttled
    queue. Item render state is derived from this Job's status at read time (no
    PlanItem status write — plan T2). Returns the job id, or None if the item had
    no clips / clip validation failed (best-effort — never raises).

    `item.clip_gcs_paths` must already be set on the session before calling.
    """
    from app.services.generative_jobs import build_generative_job  # noqa: PLC0415
    from app.services.job_dispatch import enqueue_orchestrator_sync  # noqa: PLC0415
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    clip_paths = list(item.clip_gcs_paths or [])
    if not clip_paths:
        log.warning("plan_item_render.no_clips", plan_item_id=str(item.id))
        return None
    try:
        job = build_generative_job(
            user_id=plan.user_id,
            clip_paths=clip_paths,
            mode="content_plan",
            content_plan_item_id=item.id,
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
        )
    except ValueError as exc:
        log.warning("plan_item_render.invalid_clips", plan_item_id=str(item.id), error=str(exc))
        return None
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
    enqueue_orchestrator_sync(orchestrate_generative_job, job_id, queue=PLAN_JOBS_QUEUE)
    log.info("plan_item_render.dispatched", plan_item_id=str(item.id), job_id=job_id)
    return job_id


def _load_persona_data(session, plan: ContentPlan) -> dict:  # noqa: ANN001
    """Best-effort persona dict for intro_writer threading. Empty if missing.

    Includes `_tiktok_summary` (the pre-rendered TikTok analysis summary) as a
    private key so _dispatch_item_render can thread it down to build_generative_job
    without changing the public persona schema. The underscore prefix prevents
    accidental use as an LLM field.
    """
    persona_row = session.get(PersonaRow, plan.persona_id)
    if persona_row is not None and persona_row.persona:
        data = dict(persona_row.persona)
        data["_tiktok_summary"] = _analysis_summary(persona_row.tiktok_profile)
        # Thread the per-user style (Creator Agent M1) under a private key so
        # _dispatch_item_render can pass it to build_generative_job without
        # polluting the public persona schema fields.
        data["_user_style"] = dict(persona_row.style) if persona_row.style else None
        return data
    return {}


@celery_app.task(
    name="app.tasks.content_plan_build.generate_plan_item_videos",
    bind=True,
    max_retries=1,
    default_retry_delay=15,
)
def generate_plan_item_videos(self, plan_item_id: str) -> None:  # noqa: ANN001
    """Mint a generative Job for a plan item's themed clips and dispatch its render."""
    with sync_session() as session:
        item = session.get(PlanItem, uuid.UUID(str(plan_item_id)))
        if item is None:
            log.warning("plan_item_videos.missing_item", plan_item_id=plan_item_id)
            return
        plan = session.get(ContentPlan, item.content_plan_id)
        if plan is None:
            return
        _dispatch_item_render(session, item, plan, _load_persona_data(session, plan))


# Activation seed (T8): how many plan items one seed batch may auto-generate. Each
# render lands on the throttled plan-jobs queue, so this is a "show the user range"
# cap, not a throughput limit. Kept in sync with ClipPlanMatcherInput.max_assignments.
_AUTO_GENERATE_LIMIT = 2


@celery_app.task(
    name="app.tasks.content_plan_build.activate_content_plan",
    bind=True,
    max_retries=0,
)
def activate_content_plan(self, plan_id: str) -> None:  # noqa: ANN001
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
    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is None:
            log.warning("activate_plan.missing_row", plan_id=plan_id)
            return
        seed_paths = list(plan.seed_clip_paths or [])
        if not seed_paths:
            _set_activation(session, plan, "failed")
            log.warning("activate_plan.no_seed_clips", plan_id=plan_id)
            return
        items = [
            PlanItemSummary(
                item_id=str(it.id),
                theme=it.theme or "",
                idea=it.idea or "",
                filming_suggestion=it.filming_suggestion or "",
            )
            for it in plan.items
        ]
        persona_data = _load_persona_data(session, plan)
        plan.activation_started_at = datetime.now(UTC)
        plan.activation_phase = "matching_clips"
        _set_activation(session, plan, "activating")

    if not items:
        with sync_session() as session:
            plan = session.get(ContentPlan, pid)
            if plan is not None:
                _set_activation(session, plan, "activated_empty")
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
            plan = session.get(ContentPlan, pid)
            if plan is not None:
                _set_activation(session, plan, "activated_empty")
        return

    # Group assignments by item (the matcher caps assignment count, but two clips
    # could still target one item) → set that item's clips and dispatch one render.
    by_item: dict[str, list[str]] = {}
    for a in matched.assignments:
        by_item.setdefault(a.item_id, []).append(a.clip_gcs_path)

    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is not None:
            _set_activation_phase(session, plan, "picking_days")

    dispatched = 0
    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is None:
            return
        _set_activation_phase(session, plan, "starting_renders")
        for item_id, paths in by_item.items():
            item = session.get(PlanItem, uuid.UUID(item_id))
            if item is None or item.content_plan_id != plan.id:
                continue
            # Assign the matched seed clip(s) to the item server-side. NOTE: these
            # paths live under the plan's `.../seed/` prefix, NOT the item's
            # `.../{item_id}/` prefix that the public attach_clips route enforces.
            # That route check guards untrusted user input; here we are trusted
            # server code assigning a clip the user already owns under the same
            # plan, and build_generative_job only requires the `users/` allowlist —
            # so no GCS copy is needed. Do NOT "fix" this by adding a per-item
            # prefix check: it would break activation.
            item.clip_gcs_paths = list(paths)
            session.flush()
            if _dispatch_item_render(session, item, plan, persona_data) is not None:
                dispatched += 1

        plan = session.get(ContentPlan, pid)
        if plan is not None:
            _set_activation(session, plan, "activated" if dispatched else "activated_empty")
    log.info("activate_plan.done", plan_id=plan_id, dispatched=dispatched)


def _set_activation(session, plan: ContentPlan, status_value: str) -> None:  # noqa: ANN001
    plan.activation_status = status_value
    session.commit()


def _set_activation_phase(session, plan: ContentPlan, phase: str) -> None:  # noqa: ANN001
    plan.activation_phase = phase
    session.add(plan)
    session.commit()


@celery_app.task(
    name="app.tasks.content_plan_build.reroll_plan_item",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def reroll_plan_item(self, item_id: str) -> None:  # noqa: ANN001
    """Re-generate the idea for a single plan item.

    Mirrors _dedup_and_replace: runs ContentPlanGeneratorAgent with all
    current plan ideas excluded, picks one fresh replacement via
    choose_replacements, patches the target item in-place preserving
    day_index. Failure is best-effort — resets item_status to 'idea' so
    the user's original idea survives.
    """
    with sync_session() as session:
        item = session.get(PlanItem, uuid.UUID(str(item_id)))
        if item is None:
            log.warning("reroll_plan_item.missing_item", item_id=item_id)
            return
        plan = session.get(ContentPlan, item.content_plan_id)
        if plan is None:
            log.warning("reroll_plan_item.missing_plan", item_id=item_id)
            return

        # Collect all current ideas to exclude so the fresh idea is distinct.
        all_ideas = [it.idea for it in plan.items if it.idea]

        persona_row = session.get(PersonaRow, plan.persona_id)
        persona = (
            Persona(**persona_row.persona)
            if persona_row is not None and persona_row.persona
            else Persona(
                summary="",
                content_pillars=[],
                tone="",
                audience="",
                posting_cadence="",
                sample_topics=[],
            )
        )
        agent_input = ContentPlanInput(
            persona=persona,
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
            exclude_ideas=all_ideas,
        )
        original_day_index = item.day_index

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        replacements = choose_replacements(1, list(output.items), all_ideas)
    except Exception as exc:  # noqa: BLE001
        log.warning("reroll_plan_item.failed", item_id=item_id, error=str(exc))
        with sync_session() as session:
            item = session.get(PlanItem, uuid.UUID(str(item_id)))
            if item is not None:
                item.item_status = "idea"
                session.commit()
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        item = session.get(PlanItem, uuid.UUID(str(item_id)))
        if item is None:
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
        item.filming_guide = [s.model_dump() for s in (fresh.filming_guide or [])]
        item.edit_format = fresh.edit_format or "montage"
        item.item_status = "idea"
        item.user_edited = False
        session.commit()

    log.info(
        "reroll_plan_item.done",
        item_id=item_id,
        day_index=original_day_index,
        new_idea=replacements[0].idea if replacements else None,
    )
