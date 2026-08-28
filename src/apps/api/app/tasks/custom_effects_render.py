"""app.tasks.custom_effects_render — `apply_custom_effect` execution (PR6 of
the Nova AI sandboxed effect-language train).

PR5 (`app/pipeline/custom_effects.py`) landed the validator + serializer,
inert. This module wires the accepted side: a Celery task that burns an
ALREADY-VALIDATED (but never trusted) `EffectSpec` onto a variant's video via
FFmpeg and persists the result. Dark behind `settings.custom_effects_enabled`
(gated at the copilot-op and route layers, not here — this task assumes a
caller already decided the flag is on).

Modeled directly on `rerender_caption_camera_effects` /
`_run_rerender_caption_camera_effects` in `app/tasks/generative_build.py`
(~line 12979): same `soft_time_limit=1740, time_limit=1800` render-orchestrator
budget, same `pipeline_trace_for` wrapping, same gen-guarded
`_update_variant_entry` terminal writes, same "clean-base rebuild" shape —
download the variant's clean base, transform it, re-compose captions/text on
top, upload under a fresh GCS key, delete the superseded blob. Generalized
beyond that task's subtitled-only scope: `apply_custom_effect` is not
archetype-restricted, so this reads `base_video_path` when present (any
archetype that separates a clean base from its caption/text burn) and falls
back to the variant's current `video_path` when it isn't (the effect then
burns over whatever is already composited — see the module docstring note on
`burning_onto_clean_base` below for the caption/text recomposition trade-off
this implies).

Threat model: `effect` arrives here as whatever the client's PATCH body
contained, routed straight through by `dispatch_apply_custom_effect`
(`app/routes/generative_jobs.py`) with no re-validation guarantee surviving a
Celery retry or a tampered stored value — so this module NEVER reads a filter
name or param value except by first calling `validate_effect_spec` again,
here, at execution time. Every downstream decision (the FFmpeg command, the
persisted `custom_effects` patch) reads exclusively from the freshly
re-validated `EffectSpec`, never from the raw `effect` dict.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError

from app.database import sync_session as _sync_session
from app.models import Job
from app.worker import celery_app

log = structlog.get_logger()

MAX_ERROR_DETAIL_LEN = 2000

# Generous ceiling for a single ffmpeg burn — well under the task's own
# soft_time_limit so a hang here still leaves room for the caption/text
# recompose pass that may follow it in the same task run.
_FFMPEG_TIMEOUT_S = 900


def _build_custom_effect_command(input_path: str, output_path: str, filter_chain: str) -> list[str]:
    """Build the single FFmpeg command that burns a validated effect chain.

    `filter_chain` MUST come from `effect_spec_to_filter_chain(spec)` on an
    already re-validated `EffectSpec` — never a raw client string; the
    validator, not this function, is the injection boundary (see
    app/pipeline/custom_effects.py's module docstring).

    The `_encoding_args(..., preset="fast")` call sits directly in this
    function so the encoder-policy AST gate (tests/test_encoder_policy.py)
    attributes it here: this is a final-output encode (the bytes that ship
    to the user), so `fast` (mb-tree + psy-rd stay on) per the repo's
    final-output preset policy — never `ultrafast`, which visibly
    macroblocks smooth gradients.
    """
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    return [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vf",
        filter_chain,
        *_encoding_args(output_path, preset="fast"),
    ]


def _run_ffmpeg_effect(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(
            f"custom-effect ffmpeg burn failed (rc={result.returncode}): {stderr_tail}"
        )


def reapply_persisted_custom_effect(
    local_video_path: str,
    variant: dict[str, Any],
    tmpdir: str,
) -> tuple[str, bool]:
    """Re-burn a variant's persisted `custom_effects` entry (if any) onto a
    freshly rebuilt local base video.

    REAPPLY-ON-REBURN, not one-shot — the same contract this repo already
    applies to persisted SFX and media-overlay lanes (see
    `_reapply_user_media_layers` / `_will_reapply_media_layers` in
    generative_build.py and CLAUDE.md's "every caption reburn re-applies
    persisted lanes"). Every task that rebuilds a variant's video from
    `base_video_path` (or an equivalent freshly-assembled clean base) and then
    burns captions/text on top MUST call this FIRST — a custom effect renders
    UNDER captions/text, same layering rule camera effects use, so it can
    never be the outermost layer the way SFX/overlays are.

    Never trusts the stored spec: a persisted `custom_effects` entry reached
    the DB through the same untrusted PATCH surface a fresh chat-authored one
    does, so it is re-validated via `validate_effect_spec` here exactly like
    at first-apply time — a hand-tampered row, or one that predates a
    since-tightened ALLOWED_FILTERS, must not reach FFmpeg.

    Returns `(path, cleared)`:
      - No persisted effect: `(local_video_path, False)` — untouched, so a
        variant with no custom effect reburns byte-identically to before this
        function existed.
      - Valid effect: `(new_local_path, False)` — a new local file with the
        effect burned in; the caller uses this as the input to its
        caption/text burn instead of `local_video_path`.
      - Invalid/tampered spec OR the burn itself fails: `(local_video_path,
        True)` — fails OPEN (the caller proceeds with the unmodified video),
        records a `("render", "custom_effect_reapply_failed")`
        `pipeline_trace` event, and tells the caller (`cleared=True`) to clear
        the persisted `custom_effects` entry in its patch so the UI stops
        claiming an effect that isn't actually on the video.
    """
    effects = variant.get("custom_effects")
    if not isinstance(effects, list) or not effects:
        return local_video_path, False
    raw_effect = effects[0]
    if not isinstance(raw_effect, dict):
        return local_video_path, False

    from app.pipeline.custom_effects import (  # noqa: PLC0415
        EffectValidationError,
        effect_spec_to_filter_chain,
        validate_effect_spec,
    )
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    variant_id = variant.get("variant_id")
    try:
        spec = validate_effect_spec(raw_effect)
    except EffectValidationError as exc:
        log.warning(
            "custom_effect_reapply_rejected",
            variant_id=variant_id,
            reason=exc.reason,
        )
        record_pipeline_event(
            "render",
            "custom_effect_reapply_failed",
            {"variant_id": variant_id, "reason": exc.reason, "stage": "validate"},
        )
        return local_video_path, True

    try:
        filter_chain = effect_spec_to_filter_chain(spec)
        effected_local = os.path.join(tmpdir, f"custom_effect_reapply_{uuid.uuid4().hex[:8]}.mp4")
        _run_ffmpeg_effect(
            _build_custom_effect_command(local_video_path, effected_local, filter_chain)
        )
    except Exception as exc:  # noqa: BLE001 — fail open, never break a reburn
        log.warning(
            "custom_effect_reapply_burn_failed",
            variant_id=variant_id,
            error=str(exc)[:500],
        )
        record_pipeline_event(
            "render",
            "custom_effect_reapply_failed",
            {"variant_id": variant_id, "reason": "burn_failed", "stage": "render"},
        )
        return local_video_path, True

    return effected_local, False


def _run_apply_custom_effect(
    job_id: str,
    variant_id: str,
    effect: dict[str, Any],
    render_gen_id: str | None,
    terminal_state: dict[str, bool],
) -> None:
    from app.pipeline.custom_effects import (  # noqa: PLC0415
        EffectValidationError,
        effect_spec_to_filter_chain,
        validate_effect_spec,
    )
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        download_to_file,
        upload_public_read,
    )
    from app.tasks.generative_build import (  # noqa: PLC0415
        _CAPTION_TASK_SOFT_TIME_LIMIT_S,
        _REAPPLY_DEADLINE_MARGIN_S,
        _burn_persisted_captions_onto_base,
        _compose_subtitled_final,
        _fresh_variant_snapshot,
        _project_carousel_timed_lanes,
        _reapply_user_media_layers,
        _rendered_duration_s,
        _should_compose_subtitled_final,
        _update_variant_entry,
        _will_reapply_media_layers,
    )

    # Symmetric direction of REAPPLY-ON-REBURN: this task itself rebuilds from
    # base_video_path, so any persisted SFX/media-overlay lanes on the
    # variant must be reapplied on top of the effected+captioned output —
    # same contract every caption/camera-effect reburn task uses (see
    # reapply_persisted_custom_effect's docstring for the mirror direction).
    reapply_deadline = (
        time.monotonic() + _CAPTION_TASK_SOFT_TIME_LIMIT_S - _REAPPLY_DEADLINE_MARGIN_S
    )

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("custom_effect_render_job_not_found", job_id=job_id)
            return
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        raise ValueError(f"variant {variant_id} not found on job {job_id}")

    # Re-validate at execution time — never trust the value handed to this
    # task (see module docstring). A rejection here is a soft failure: the
    # variant returns to "ready" with a machine-readable error, not "failed",
    # since nothing has been touched yet.
    try:
        spec = validate_effect_spec(effect)
    except EffectValidationError as exc:
        log.warning(
            "custom_effect_render_rejected_at_execution",
            job_id=job_id,
            variant_id=variant_id,
            reason=exc.reason,
        )
        _update_variant_entry(
            job_id,
            variant_id,
            {"render_status": "ready", "render_error": f"invalid effect: {exc.reason}"[:500]},
            expected_render_gen_id=render_gen_id,
            outcome="custom_effect_rejected_at_execution",
            cleanup_followup="none",
        )
        return

    # Prefer the clean (caption/text-free) base so the effect renders BEHIND
    # any burned-in text, same layering rule camera effects use. Not every
    # archetype separates one out (e.g. a montage variant with everything
    # already composited into video_path) — fall back to the current final
    # video and accept the effect burning over whatever is already there.
    base_path = variant.get("base_video_path")
    source_path = base_path or variant.get("video_path")
    if not source_path:
        raise ValueError(f"variant {variant_id} has no source video to apply an effect to")
    burning_onto_clean_base = bool(base_path)

    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="custom_effect_render_start",
    ):
        return

    record_pipeline_event(
        "custom_effect",
        "burn_start",
        {"variant_id": variant_id, "filters": len(spec.filters)},
    )

    render_variant = _project_carousel_timed_lanes(variant)
    rank = variant.get("rank") or 1
    old_video_path = variant.get("video_path")

    with tempfile.TemporaryDirectory(prefix="nova_custom_effect_") as tmpdir:
        source_local = os.path.join(tmpdir, "source.mp4")
        download_to_file(source_path, source_local)
        # Probed only to keep parity with the sibling reburn tasks' pattern
        # (and as a cheap corruption check on the download) — the filter
        # chain's own `enable=between(t,start,end)` windowing is naturally
        # bounded by the real clip duration; no clamping needed here.
        probe_video(source_local)

        effected_local = os.path.join(tmpdir, "effected.mp4")
        filter_chain = effect_spec_to_filter_chain(spec)
        _run_ffmpeg_effect(_build_custom_effect_command(source_local, effected_local, filter_chain))

        if burning_onto_clean_base and _should_compose_subtitled_final(render_variant):
            final_local, _matte_path = _compose_subtitled_final(
                effected_local,
                {**render_variant, "subject_matte_path": None},
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=str(source_path),
            )
        elif burning_onto_clean_base and render_variant.get("caption_cues"):
            final_local = os.path.join(tmpdir, "final.mp4")
            _burn_persisted_captions_onto_base(effected_local, final_local, render_variant, tmpdir)
        else:
            final_local = effected_local

        suffix = uuid.uuid4().hex[:8]
        new_video_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_fx_{suffix}.mp4"
        output_url = upload_public_read(final_local, new_video_gcs)
        duration_s = _rendered_duration_s(final_local)

    # Symmetric REAPPLY-ON-REBURN direction: this rebuild replaces video_path
    # from the clean base, so any persisted SFX/media-overlay snapshots point
    # at the SUPERSEDED video and must be reset — the reapply chain below
    # (when will_reapply) rebuilds them fresh on top of this output, same as
    # every caption/camera-effect reburn task.
    fresh_for_reapply = _fresh_variant_snapshot(job_id, variant_id) or variant
    will_reapply = _will_reapply_media_layers(fresh_for_reapply)

    # v1: a single active custom effect (replace semantics) — a later spec
    # entirely replaces the prior one rather than stacking.
    patch: dict[str, Any] = {
        "video_path": new_video_gcs,
        "output_url": output_url,
        "custom_effects": [spec.model_dump(mode="json")],
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
    }
    if duration_s is not None:
        patch["duration_s"] = duration_s
    if will_reapply:
        # OV-7: the reapply chain owns the final ready/failed — no effect-less
        # "ready" observable between this burn and the reapply.
        patch["render_status"] = "rendering"
    else:
        patch["render_status"] = "ready"
        patch["render_finished_at"] = datetime.utcnow().isoformat() + "Z"

    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="custom_effect_render",
        cleanup_followup="media_layers",
        accepted_state=terminal_state,
    ):
        delete_object_best_effort(new_video_gcs)
        return

    terminal_state["accepted"] = True
    if old_video_path and old_video_path != new_video_gcs:
        delete_object_best_effort(old_video_path)
    record_pipeline_event(
        "custom_effect",
        "burn_done",
        {"variant_id": variant_id, "filters": len(spec.filters)},
    )
    log.info(
        "custom_effect_render_done",
        job_id=job_id,
        variant_id=variant_id,
        filters=len(spec.filters),
    )
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=render_gen_id,
        deadline_monotonic=reapply_deadline,
    ):
        # R1-3: deferred terminal status (OV-7) but the chain no-oped —
        # finalize so no path leaves the variant stranded in "rendering".
        _update_variant_entry(
            job_id,
            variant_id,
            {
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=render_gen_id,
            outcome="custom_effect_render_reapply_noop",
            cleanup_followup="none",
        )


@celery_app.task(
    name="apply_custom_effect_render",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    soft_time_limit=1740,
    time_limit=1800,
)
def apply_custom_effect_render(
    self,
    job_id: str,
    variant_id: str,
    effect: dict[str, Any],
    render_gen_id: str | None = None,
) -> None:
    """Burn Nova's sandboxed effect language onto a variant's video.

    `time_limit`/`soft_time_limit` mirror every other render orchestrator
    (`tests/tasks/test_task_time_limits.py` pins this against the broker's
    `visibility_timeout` — see app/worker.py). `self`/`autoretry_for` follow
    `rerender_caption_camera_effects`'s shape byte-for-byte.
    """
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.tasks.generative_build import _update_variant_entry  # noqa: PLC0415

    terminal_state = {"accepted": False}
    with pipeline_trace_for(job_id):
        try:
            _run_apply_custom_effect(job_id, variant_id, effect, render_gen_id, terminal_state)
        except OperationalError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error(
                "custom_effect_render_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc)[:MAX_ERROR_DETAIL_LEN],
                exc_info=True,
            )
            _update_variant_entry(
                job_id,
                variant_id,
                {
                    "render_status": "failed" if terminal_state["accepted"] else "ready",
                    "render_error": str(exc)[:500],
                },
                expected_render_gen_id=render_gen_id,
                outcome=(
                    "custom_effect_render_failed_post_swap"
                    if terminal_state["accepted"]
                    else "custom_effect_render_failed"
                ),
            )
