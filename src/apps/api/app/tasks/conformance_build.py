"""Celery task: run ConformanceFeedbackAgent at clip-attach time (best-effort).

Triggered fire-and-forget by attach_clips after the item's clip_gcs_paths commit.
The verdict is persisted on plan_items.conformance (nullable JSONB) for the item
page to display — it never blocks the attach response or the Generate button.

Guard: CONFORMANCE_FEEDBACK_ENABLED=False → no-op (early return).
Guard: uninstructed item (filming_guide empty OR instruction_level == "none") → skip.
Best-effort: any exception is caught + logged; item.conformance stays NULL.
"""

from __future__ import annotations

import tempfile
import uuid

import structlog

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, PlanItem
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="app.tasks.conformance_build.analyze_item_conformance",
    bind=True,
    max_retries=0,  # best-effort; no retry
    soft_time_limit=120,
    time_limit=150,
)
def analyze_item_conformance(self, plan_item_id: str) -> None:  # noqa: ANN001
    """Analyze clip conformance for a plan item and persist the verdict.

    Best-effort: any exception is caught and logged. The attach response and
    Generate button are completely independent of this task.
    """
    if not settings.conformance_feedback_enabled:
        log.debug("conformance_build.disabled", plan_item_id=plan_item_id)
        return

    try:
        _run(plan_item_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "conformance_build.failed",
            plan_item_id=plan_item_id,
            error=str(exc)[:400],
        )


def _run(plan_item_id: str) -> None:
    """Inner implementation — separated from the task wrapper so best-effort
    wrapping and logging live in one place."""
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents._schemas.conformance import ConformanceInput  # noqa: PLC0415
    from app.agents.conformance_feedback import ConformanceFeedbackAgent  # noqa: PLC0415
    from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
        GeminiAnalysisError,
        GeminiRefusalError,
        gemini_upload_and_wait,
    )
    from app.storage import download_to_file  # noqa: PLC0415

    iid = uuid.UUID(str(plan_item_id))

    # ── Load the item and check guards ────────────────────────────────────────
    with sync_session() as session:
        item = session.get(PlanItem, iid)
        if item is None:
            log.warning("conformance_build.missing_item", plan_item_id=plan_item_id)
            return

        filming_guide = list(item.filming_guide or [])
        if not filming_guide:
            log.debug("conformance_build.no_filming_guide", plan_item_id=plan_item_id)
            return

        # instruction_level lives in the owning user's personas.style JSONB.
        # Resolve it via the ContentPlan → Persona join, null-safe, default "full".
        instruction_level = _get_instruction_level(session, item)
        if instruction_level == "none":
            log.debug(
                "conformance_build.uninstructed_item",
                plan_item_id=plan_item_id,
                instruction_level=instruction_level,
            )
            return

        # Need at least one clip to analyze.
        clip_paths = list(item.clip_gcs_paths or [])
        if not clip_paths:
            log.debug("conformance_build.no_clips", plan_item_id=plan_item_id)
            return

        # Pick the first shot-assigned clip (by guide order) when available (T5/D15).
        # Fallback to clips[0] for legacy/uninstructed items with no assignments.
        # "By guide order" = first shot_id in filming_guide that has a matching assignment.
        assignments = item.clip_assignments or []
        assigned_by_shot: dict[str, str] = {
            a["shot_id"]: a["gcs_path"]
            for a in assignments
            if isinstance(a, dict) and a.get("shot_id") and a.get("gcs_path")
        }
        clip_gcs_path: str | None = None
        for shot in filming_guide:
            sid = shot.get("shot_id") if isinstance(shot, dict) else None
            if sid and sid in assigned_by_shot:
                clip_gcs_path = assigned_by_shot[sid]
                break
        if clip_gcs_path is None:
            clip_gcs_path = clip_paths[0]
        theme = str(item.theme or "")
        idea = str(item.idea or "")

        # The chosen clip's assignment entry: creator note + machine-matched flag.
        chosen = next(
            (a for a in assignments if isinstance(a, dict) and a.get("gcs_path") == clip_gcs_path),
            {},
        )
        user_note = str(chosen.get("user_note") or "")
        if chosen.get("machine_matched"):
            # Pool-matched footage Kria picked itself: running the judge on it
            # would have the product argue with its own matcher. Conformance
            # runs only after the user touches the slot (keep / swap / replace).
            log.info(
                "conformance_build.skipped_machine_matched",
                plan_item_id=plan_item_id,
                clip_gcs_path=clip_gcs_path,
            )
            return
        # Contested flag survives note-edit re-runs on the SAME footage; a fresh
        # attach nulls conformance entirely (D7), which also resets it.
        contested = bool((item.conformance or {}).get("contested"))

    # ── Download → Gemini upload → ClipMetadataAgent ──────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        import os  # noqa: PLC0415

        local_path = os.path.join(tmpdir, "clip.mp4")
        download_to_file(clip_gcs_path, local_path)

        try:
            file_ref = gemini_upload_and_wait(local_path)
        except (GeminiAnalysisError, GeminiRefusalError, Exception) as exc:  # noqa: BLE001
            log.warning(
                "conformance_build.gemini_upload_failed",
                plan_item_id=plan_item_id,
                error=str(exc)[:300],
            )
            return

        # Run ClipMetadataAgent via the analyze_clip shim.
        from app.pipeline.agents.gemini_analyzer import analyze_clip  # noqa: PLC0415

        try:
            clip_meta = analyze_clip(file_ref, job_id=None)
        except (GeminiAnalysisError, GeminiRefusalError, Exception) as exc:  # noqa: BLE001
            log.warning(
                "conformance_build.clip_metadata_failed",
                plan_item_id=plan_item_id,
                error=str(exc)[:300],
            )
            return

    if getattr(clip_meta, "failed", False) or getattr(clip_meta, "analysis_degraded", False):
        # Garbage-in gate (wrong-brief incident): judging a degraded digest
        # invites confabulation on BOTH sides of the comparison. No verdict
        # beats a confidently wrong one.
        log.info(
            "conformance_build.skipped_degraded_analysis",
            plan_item_id=plan_item_id,
            clip_gcs_path=clip_gcs_path,
            meta_failed=bool(getattr(clip_meta, "failed", False)),
            meta_degraded=bool(getattr(clip_meta, "analysis_degraded", False)),
        )
        return

    # ── Build ConformanceInput from the clip metadata digest ─────────────────
    clip_digest = {
        "detected_subject": str(getattr(clip_meta, "detected_subject", "") or ""),
        "content_type": str(getattr(clip_meta, "content_type", "") or ""),
        "audio_type": str(getattr(clip_meta, "audio_type", "") or ""),
        "hook_text": str(getattr(clip_meta, "hook_text", "") or ""),
        "transcript": str(getattr(clip_meta, "transcript", "") or "")[:300],
        "visual_density": float(getattr(clip_meta, "visual_density", 5.0) or 5.0),
        "composition_note": str(getattr(clip_meta, "composition_note", "") or ""),
    }

    conformance_input = ConformanceInput(
        filming_guide=filming_guide,
        clip_digest=clip_digest,
        theme=theme,
        idea=idea,
        user_context=user_note,
    )

    # ── Run ConformanceFeedbackAgent (echo-back guarded) ─────────────────────
    agent = ConformanceFeedbackAgent(default_client())
    output = None
    for attempt in (1, 2):
        candidate = agent.run(conformance_input, ctx=RunContext(job_id=None))
        if _themes_match(candidate.evaluated_theme, theme):
            output = candidate
            break
        # Wrong-brief incident guard: the agent judged against something other
        # than this item's brief (contaminated input or confabulation). Discard;
        # one retry, then give up — no verdict beats a wrong one.
        log.warning(
            "conformance_brief_mismatch",
            plan_item_id=plan_item_id,
            clip_gcs_path=clip_gcs_path,
            expected_theme=theme[:80],
            evaluated_theme=str(candidate.evaluated_theme)[:80],
            attempt=attempt,
        )
    if output is None:
        return

    verdict_dict = output.model_dump()
    # Traceability (wrong-brief incident): which footage this verdict describes.
    verdict_dict["clip_gcs_path"] = clip_gcs_path

    # ── Persist verdict ───────────────────────────────────────────────────────
    with sync_session() as session:
        item = session.get(PlanItem, iid)
        if item is None:
            log.warning("conformance_build.item_gone_after_agent", plan_item_id=plan_item_id)
            return

        # Re-read the live flags HERE, not at task start — the user may have
        # dismissed or contested while the agent ran, and that intent must
        # survive (review finding: dismissed was dropped on every fresh persist,
        # and a contest mid-run was lost).
        current = item.conformance if isinstance(item.conformance, dict) else {}
        now_contested = bool(current.get("contested")) or contested
        if current.get("dismissed"):
            verdict_dict["dismissed"] = True
        if now_contested:
            verdict_dict["contested"] = True
            # After the creator contested once on this footage, only
            # high-confidence verdicts may render again.
            if output.confidence < 0.8:
                verdict_dict["suppressed"] = True

        # Guard against persisting a verdict for footage the user already
        # replaced (a note-PATCH + attach can race; the older task must not
        # land last). If the analyzed clip is no longer attached, drop it.
        live_paths = {
            a.get("gcs_path")
            for a in (item.clip_assignments or [])
            if isinstance(a, dict)
        }
        if clip_gcs_path not in live_paths:
            log.info(
                "conformance_build.stale_clip_discarded",
                plan_item_id=plan_item_id,
                clip_gcs_path=clip_gcs_path,
            )
            return

        item.conformance = verdict_dict
        session.commit()

    log.info(
        "conformance_build.done",
        plan_item_id=plan_item_id,
        verdict=output.verdict,
        confidence=output.confidence,
    )


def _themes_match(evaluated: str, expected: str) -> bool:
    """Echo-back comparison: whitespace/case tolerant, never fuzzy.

    The agent copies the theme verbatim; minor whitespace/case wobble from the
    model is fine, but any substantive difference means it judged against the
    wrong brief — exactly the contamination class this guard exists to catch.
    """

    def norm(s: str) -> str:
        return " ".join(str(s or "").split()).strip().lower()

    return norm(evaluated) == norm(expected)


def _get_instruction_level(session, item: PlanItem) -> str:
    """Read instruction_level from the owning user's personas.style JSONB.

    Null-safe chain: item → ContentPlan → Persona → style → instruction_level.
    Any missing link → default "full".
    """
    from app.models import Persona  # noqa: PLC0415

    try:
        plan = session.get(ContentPlan, item.content_plan_id)
        if plan is None:
            return "full"
        persona = session.get(Persona, plan.persona_id)
        if persona is None:
            return "full"
        style = persona.style or {}
        return str(style.get("instruction_level", "full") or "full")
    except Exception:  # noqa: BLE001
        return "full"
