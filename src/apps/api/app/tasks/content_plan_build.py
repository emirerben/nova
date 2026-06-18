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
from app.services.seed_provenance import match_specs_to_seeds
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
        # M1 Bring-Your-Own-Ideas: extract seed texts from the persona row.
        # Keep full dicts (id + text) so T5 provenance matching can write
        # source_idea_seed_id at persist time. Empty list → byte-identical
        # baseline (no user-ideas block injected).
        raw_seeds = persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        seeds_with_ids = [
            s for s in raw_seeds if isinstance(s, dict) and s.get("text") and s.get("id")
        ]
        idea_seed_texts = [str(s["text"]) for s in seeds_with_ids]
        persona_id_for_seeds = plan.persona_id
        agent_input = ContentPlanInput(
            persona=Persona(**persona_row.persona),
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
            persona_row_p = session.get(PersonaRow, persona_id_for_seeds)
            if persona_row_p is not None:
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
        # M1 Bring-Your-Own-Ideas: carry user seeds into the regenerate pass so the
        # "their say" invariant covers seeds too (regenerate biases toward what the
        # user explicitly said they want, not just their feedback reactions).
        # Keep full dicts (id + text) for T5 provenance matching at persist time.
        raw_seeds_regen = persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        seeds_with_ids_regen = [
            s for s in raw_seeds_regen if isinstance(s, dict) and s.get("text") and s.get("id")
        ]
        idea_seed_texts_regen = [str(s["text"]) for s in seeds_with_ids_regen]
        persona_id_for_seeds_regen = plan.persona_id
        agent_input = ContentPlanInput(
            persona=Persona(**persona_row.persona),
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
            persona_row_p = session.get(PersonaRow, persona_id_for_seeds_regen)
            if persona_row_p is not None:
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
    path_by_shot: dict[str, str] = {
        str(a.get("shot_id")): str(a.get("gcs_path"))
        for a in assignments
        if isinstance(a, dict) and a.get("shot_id") and a.get("gcs_path")
    }
    known_paths = set(clip_paths)
    ordered: list[str] = []
    for shot in guide:
        sid = str(shot.get("shot_id") or "")
        path = path_by_shot.get(sid)
        if path and path in known_paths and path not in ordered:
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
    # Narrative clip order (filming-guide alignment): reorder clip_paths so the
    # guide's shot clips come first IN GUIDE ORDER (clip_assignments stores them
    # in attach-request order, which is client-controlled and not the guide
    # order), pool clips after. narrative_shot_count tells the render path how
    # many of the leading paths form the narrative spine.
    clip_paths, narrative_shot_count = _narrative_clip_order(item, clip_paths)
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
            # Route through set_item_clips (D16 single-writer contract).
            from app.services.plan_clips import ClipAssignment, set_item_clips  # noqa: PLC0415

            set_item_clips(item, [ClipAssignment(gcs_path=p, shot_id=None) for p in paths])
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
def match_pool_clips(self, plan_id: str) -> None:  # noqa: ANN001
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
    try:
        _run_pool_match(plan_id)
    except Exception as exc:  # noqa: BLE001
        # Never let the pool wedge in "matching" forever — ANY failure (soft time
        # limit, a DB error in the write-back block that the inner try/except
        # doesn't cover, a worker kill) flips the status so the UI shows
        # "Match again" instead of polling indefinitely.
        log.warning("pool_match.failed_terminal", plan_id=plan_id, error=str(exc)[:300])
        try:
            with sync_session() as session:
                plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
                if plan is not None and (plan.pool or {}).get("status") == "matching":
                    _set_pool_status(session, plan, "match_failed")
        except Exception:  # noqa: BLE001
            pass
        raise


def _run_pool_match(plan_id: str) -> None:
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
        plan = session.get(ContentPlan, pid)
        if plan is None:
            log.warning("pool_match.missing_row", plan_id=plan_id)
            return
        pool = dict(plan.pool or {})
        pool_clips = [c for c in pool.get("clips", []) if isinstance(c, dict) and c.get("gcs_path")]
        unmatched = [c["gcs_path"] for c in pool_clips if not c.get("matched_item_id")]
        if not unmatched:
            _set_pool_status(session, plan, "matched_empty" if not pool_clips else "matched")
            return
        # Pending = items the pool may fill: no render yet, no clips yet.
        items = [
            PlanItemSummary(
                item_id=str(it.id),
                theme=it.theme or "",
                idea=it.idea or "",
                filming_suggestion=it.filming_suggestion or "",
            )
            for it in plan.items
            if it.current_job_id is None and not (it.clip_gcs_paths or [])
        ]
        _set_pool_status(session, plan, "matching")

    if not items:
        with sync_session() as session:
            plan = session.get(ContentPlan, pid)
            if plan is not None:
                _set_pool_status(session, plan, "matched_empty")
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
            plan = session.get(ContentPlan, pid)
            if plan is not None:
                _set_pool_status(session, plan, "match_failed")
        return

    by_item: dict[str, list[str]] = {}
    for a in matched.assignments:
        by_item.setdefault(a.item_id, []).append(a.clip_gcs_path)

    assigned_paths: dict[str, str] = {}  # gcs_path → item_id actually attached
    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is None:
            return
        for item_id, paths in by_item.items():
            item = session.get(PlanItem, uuid.UUID(item_id))
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
        # M1: carry seeds into reroll so the replacement idea still respects the
        # user's stated intent (best-effort; no seeds = byte-identical to prior).
        raw_seeds_reroll = (
            persona_row.idea_seeds
            if persona_row is not None and isinstance(persona_row.idea_seeds, list)
            else []
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
    name="app.tasks.content_plan_build.add_ideas_to_plan",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
    soft_time_limit=120,
    time_limit=180,
)
def add_ideas_to_plan(self, plan_id: str) -> None:  # noqa: ANN001
    """Generate one plan item per pending idea seed and append to the plan.

    Lightweight alternative to full regeneration — N pending seeds → one small
    LLM call (horizon_days=N) → N new PlanItems appended after the last existing
    item. Protected/existing items are never touched.
    """
    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            return

        persona_row = session.get(PersonaRow, plan.persona_id)
        if persona_row is None:
            plan.plan_status = "ready"
            session.commit()
            return

        raw_seeds = persona_row.idea_seeds if isinstance(persona_row.idea_seeds, list) else []
        pending_seeds = [
            s
            for s in raw_seeds
            if isinstance(s, dict) and s.get("text") and s.get("status") != "in_plan"
        ]

        if not pending_seeds:
            plan.plan_status = "ready"
            session.commit()
            return

        seed_texts = [str(s["text"]) for s in pending_seeds]
        # T5 provenance: subset with stable id for matching at persist time.
        seeds_for_matching_add = [s for s in pending_seeds if s.get("id")]
        persona_id_for_seeds_add = plan.persona_id
        existing_items = list(plan.items or [])
        all_ideas = [it.idea for it in existing_items if it.idea]
        horizon = plan.horizon_days or 30
        used_days = {it.day_index for it in existing_items if it.day_index is not None}
        # Prefer empty slots within the horizon; fall back to extending beyond it.
        free_slots = [d for d in range(1, horizon + 1) if d not in used_days]
        max_day = max(used_days, default=0)

        persona = (
            Persona(**persona_row.persona)
            if persona_row.persona
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
            # horizon_days = N seeds → target_item_count = N (one item per seed).
            horizon_days=len(seed_texts),
            exclude_ideas=all_ideas,
            user_idea_seeds=seed_texts,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        new_specs = list(output.items)[: len(seed_texts)]
    except Exception as exc:  # noqa: BLE001
        log.warning("add_ideas_to_plan.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
            if plan is not None:
                plan.plan_status = "ready"
                session.commit()
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            return

        # T5 provenance: match new specs → seeds before the persist loop.
        seed_by_index_add = match_specs_to_seeds(new_specs, seeds_for_matching_add)
        matched_seed_ids_add: set[str] = set()

        for i, spec in enumerate(new_specs):
            if i < len(free_slots):
                slot = free_slots[i]
            else:
                slot = max_day + 1 + (i - len(free_slots))
            seed_id = seed_by_index_add.get(i)
            item = PlanItem(
                content_plan_id=plan.id,
                day_index=slot,
                position=slot,
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
                source_idea_seed_id=seed_id,
            )
            session.add(item)
            if seed_id:
                matched_seed_ids_add.add(seed_id)

        # Flip matched seeds → in_plan so IdeasCard shows "✓ in your plan"
        # and re-submission doesn't double-generate items for the same seed.
        if matched_seed_ids_add:
            persona_row_add = session.get(PersonaRow, persona_id_for_seeds_add)
            if persona_row_add is not None:
                raw_add = (
                    persona_row_add.idea_seeds
                    if isinstance(persona_row_add.idea_seeds, list)
                    else []
                )
                persona_row_add.idea_seeds = [
                    {**s, "status": "in_plan"}
                    if isinstance(s, dict)
                    and s.get("id") in matched_seed_ids_add
                    and s.get("status") != "in_plan"
                    else s
                    for s in raw_add
                ]

        plan.plan_status = "ready"
        session.commit()

    log.info(
        "add_ideas_to_plan.done",
        plan_id=plan_id,
        added=len(new_specs),
        seeds=len(seed_texts),
    )


@celery_app.task(
    name="app.tasks.content_plan_build.generate_ideas_into_plan",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
    soft_time_limit=120,
    time_limit=180,
)
def generate_ideas_into_plan(self, plan_id: str) -> None:  # noqa: ANN001
    """Expand bare ideas in-place into full plan items (idea-centric mode).

    Called by POST /content-plans/{id}/generate-ideas. For each item with
    day_index=None, runs IdeaExpanderAgent and writes theme/filming_guide/
    rationale/day_index back to that same row. Ideas graduate from the sidebar
    to the calendar rather than spawning duplicate rows.

    Items that fail to expand are left untouched (remain as bare ideas).
    Idempotency: if there are no bare ideas, sets plan_status="ready" and returns.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: I001, PLC0415
    from app.agents.idea_expander import IdeaExpanderAgent, IdeaExpanderInput  # noqa: PLC0415
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    pid = uuid.UUID(str(plan_id))

    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is None:
            return

        persona_row = session.get(PersonaRow, plan.persona_id)

        existing_items = list(plan.items or [])
        # Bare ideas: user-added items with no calendar slot yet.
        bare_items = sorted(
            [it for it in existing_items if it.day_index is None],
            key=lambda it: it.position,
        )

        if not bare_items:
            plan.plan_status = "ready"
            session.commit()
            return

        # Assign calendar slots: find free day_index values within horizon.
        used_days = {it.day_index for it in existing_items if it.day_index is not None}
        horizon = plan.horizon_days or 30
        free_slots = [d for d in range(1, horizon + 1) if d not in used_days]
        # Extend beyond horizon if more ideas than free slots.
        extra = len(bare_items) - len(free_slots)
        if extra > 0:
            free_slots += list(range(horizon + 1, horizon + 1 + extra))

        # Collect (item_id, idea_text, assigned_slot) for the agent loop.
        work = [
            (item.id, item.idea or "", free_slots[i])
            for i, item in enumerate(bare_items)
        ]

        persona = (
            Persona(**persona_row.persona)
            if persona_row and persona_row.persona
            else Persona(
                summary="",
                content_pillars=[],
                tone="",
                audience="",
                posting_cadence="",
                sample_topics=[],
            )
        )

    # Run IdeaExpanderAgent for each bare idea — outside DB session.
    results: list[tuple[uuid.UUID, int, object]] = []
    with pipeline_trace_for(pid):
        agent = IdeaExpanderAgent(default_client())
        for item_id, idea_text, slot in work:
            try:
                output = agent.run(
                    IdeaExpanderInput(
                        idea=idea_text,
                        persona_summary=persona.summary,
                        content_pillars=list(persona.content_pillars),
                    ),
                    ctx=RunContext(job_id=None),
                )
                results.append((item_id, slot, output))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "generate_ideas_into_plan.item_failed",
                    plan_id=plan_id,
                    idea=idea_text,
                    error=str(exc),
                )
                # Item stays as bare idea — partial success is fine.

    if not results:
        # Every item failed — surface error and allow retry.
        with sync_session() as session:
            plan = session.get(ContentPlan, pid)
            if plan is not None:
                plan.plan_status = "ready"
                session.commit()
        raise self.retry(exc=RuntimeError("all idea expansions failed")) from None

    # Write expansions back to the existing items.
    with sync_session() as session:
        plan = session.get(ContentPlan, pid)
        if plan is None:
            return

        for item_id, slot, output in results:
            item = session.get(PlanItem, item_id)
            if item is None:
                continue
            item.theme = output.theme
            item.filming_suggestion = output.filming_suggestion or None
            item.rationale = output.rationale or None
            item.day_index = slot
            item.filming_guide = [
                {**s.model_dump(), "shot_id": uuid.uuid4().hex}
                for s in (output.filming_guide or [])
            ]
            flag_modified(item, "filming_guide")

        plan.plan_status = "ready"
        session.commit()

    log.info(
        "generate_ideas_into_plan.done",
        plan_id=plan_id,
        expanded=len(results),
        skipped=len(work) - len(results),
    )
