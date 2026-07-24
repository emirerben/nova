"""apply_suggestions_to_variant — the ONE apply path (plan 007, decision G1-A).

Extracted from the apply route so the route (async, user-initiated) and the
auto-apply task (sync, zero-click) can never drift. Contract:

  (a) NEVER commits — the caller owns commit semantics (route: `await
      db.commit()`; task: sync `db.commit()` under its row lock).
  (b) Raises HTTPException (the dispatch layer's native error vocabulary:
      404 flag gate, 409 busy, 422 validation). The route lets it propagate;
      the task catches and maps to trace+skip/degrade.
  (c) One unit: per-card overlap re-validation (005-6A drop-the-item), merge,
      SFX persist, suggestion-clear, single-chain dispatch (005-10A: one
      overlay dispatch, terminal SFX reapply; SFX-only sets skip the burn —
      Download bakes them).

Flow:

    staged envelopes ──▶ coerce (5A validators, trim sanitizer)
        │ per-item drop: overlap vs existing cards + each other (decision B)
        ▼
    variant.sound_effects  = existing + staged children
    variant.media_overlays = (via dispatch, full-replace merge)
    variant.overlay_suggestions/… = cleared
        │
        ▼
    dispatch_set_media_overlays (render_status guard, enqueue burn)
"""

from __future__ import annotations

from datetime import datetime

import structlog

from app.models import Job

log = structlog.get_logger()


def validate_fullscreen_constraints(cards: list, variant: dict) -> None:
    """Server-side fullscreen contract (plan 009 E4 + E9) — ONE helper shared by
    BOTH manual write paths (dispatch_set_media_overlays render:true and
    _persist_overlay_metadata_only render:false) so they can never drift.

    Raises ValueError with user-facing copy; routes map it to HTTP 422.

    Rules:
    - E9: fullscreen cards are rejected on lyric variants — lyrics ARE the
      content; the disabled FE toggle is courtesy, THIS is the contract.
    - E4: no card may overlap a fullscreen card's window (both directions).
      Overlapping pip+pip stays legal — z-order resolves it, as today.

    This is the FOURTH coupled overlap site; the other three live on the AI
    path (build_suggestions taken-intervals, apply_suggestions_to_variant
    re-check, _occupied_intervals in tasks/autoplace.py). A future relaxation
    of pip stacking must visit all four.
    """
    fullscreen = [c for c in cards if getattr(c, "display_mode", "pip") == "fullscreen"]
    if not fullscreen:
        return
    is_lyrics = variant.get("text_mode") == "lyrics" or variant.get("variant_id") == "song_lyrics"
    if is_lyrics:
        raise ValueError("Full-screen cutaways aren't available on lyric edits.")
    for fs in fullscreen:
        for other in cards:
            if other is fs:
                continue
            if fs.start_s < other.end_s and other.start_s < fs.end_s:
                raise ValueError("That would overlap a full-screen moment.")


def apply_suggestions_to_variant(
    job: Job,
    variant_id: str,
    suggestions_raw: list[dict],
    *,
    user_id: str,
) -> dict:
    """Merge staged suggestion envelopes into the variant and fire ONE burn chain.

    Returns {"applied": n, "dropped": n, "sfx": n, "dispatched": bool}.
    Mutates job.assembly_plan (with flag_modified) — caller commits.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.agents._schemas.overlay_suggestion import (  # noqa: PLC0415
        coerce_overlay_suggestions,
    )
    from app.routes.generative_jobs import dispatch_set_media_overlays  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        raise ValueError(f"variant {variant_id!r} not found on job {job.id}")

    staged = coerce_overlay_suggestions(suggestions_raw)

    # Re-validate at apply time (decision B): the build-time overlap check ran
    # against a snapshot; edits/concurrent changes happened since. 005-6A
    # semantics — drop the offending card, keep the set.
    def _interval(card: dict) -> tuple[float, float]:
        return float(card.get("start_s", 0.0)), float(card.get("end_s", 0.0))

    taken = [_interval(c) for c in (variant.get("media_overlays") or [])]
    accepted = []
    dropped = 0
    for s in staged:
        st, en = _interval(s.overlay)
        if en <= st or any(st < e and b < en for b, e in taken):
            dropped += 1
            log.info(
                "apply_suggestion_dropped",
                job_id=str(job.id),
                variant_id=variant_id,
                suggestion_id=s.id,
                reason="overlap_or_empty_after_edit",
            )
            continue
        taken.append((st, en))
        accepted.append(s)

    if not accepted:
        return {"applied": 0, "dropped": dropped, "sfx": 0, "dispatched": False}

    from app.config import settings as _settings  # noqa: PLC0415

    def _sfx_scope_ok(sfx: dict) -> bool:
        """Per-user SFX scope check (review C5/C16): the apply route forwards a
        fully client-supplied envelope, so an attacker could reference another
        tenant's audio (users/{victim}/…). Glossary paths (non-users/) are fine;
        a users/-prefixed path MUST be under this user's namespace — the same
        guard dispatch_set_sound_effects enforces. Render-time validate is not
        enough: it only checks the shared 'users/' prefix (any user).
        (s.sfx is the validated SoundEffectPlacement DUMP — a plain dict.)"""
        path = (sfx or {}).get("src_gcs_path", "") or ""
        if path.startswith("users/") and not path.startswith(f"users/{user_id}/"):
            return False
        return True

    from app.services.sfx_spacing import normalize_auto_sfx_placements  # noqa: PLC0415

    merged_overlays = list(variant.get("media_overlays") or []) + [s.overlay for s in accepted]
    # SFX children are dropped when the flag is off (review C15: partial rollout
    # OVERLAY_AUTOAPPLY on / SOUND_EFFECTS off would otherwise persist SFX that
    # never bake and are invisible in the FE) OR when they fail the scope check.
    incoming_sfx = (
        [s.sfx for s in accepted if s.sfx is not None and _sfx_scope_ok(s.sfx)]
        if _settings.sound_effects_enabled
        else []
    )
    existing_sfx = list(variant.get("sound_effects") or [])
    incoming_sfx = normalize_auto_sfx_placements(incoming_sfx, protected=existing_sfx)
    merged_sfx = existing_sfx + incoming_sfx

    # Persist SFX + clear pending suggestions in one write (suggestions are
    # consumed whether the burn dispatch succeeds or raises — a 409 below
    # leaves them intact because the caller won't commit on raise).
    variant["sound_effects"] = merged_sfx or None
    variant["overlay_suggestions"] = None
    variant["overlay_suggest_status"] = None
    variant["overlay_suggest_hash"] = None
    variant["overlay_suggest_wishlist"] = None
    # Apply receipt (plan 009 ARCH-4): apply-time changes are NEVER silent.
    # Fresh per apply; the zero-click task adds its demoted count on top.
    variant["overlay_apply_receipt"] = (
        {
            "dropped": dropped,
            "demoted": 0,
            "reason": "overlap",
            "at": datetime.utcnow().isoformat() + "Z",
        }
        if dropped
        else None
    )
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")

    # Single chain (005-10A): one overlay dispatch; its terminal
    # _reapply_persisted_sfx_if_any bakes the SFX in the same re-encode.
    # The montage branch enqueues inline and returns None — byte-identical to
    # the pre-R1-1 behavior on this path. The caption branch returns a deferred
    # enqueue thunk instead, but AI applies are gated OFF on caption archetypes
    # (OV-5), so it is unreachable here; invoke defensively if a future guard
    # change opens it (this caller's commit follows immediately — the same
    # accepted millisecond race as the montage branch, never a dropped enqueue).
    enqueue = dispatch_set_media_overlays(
        job, variant_id, overlays_raw=merged_overlays, user_id=user_id
    )
    if enqueue is not None:
        enqueue()
    return {
        "applied": len(accepted),
        "dropped": dropped,
        "sfx": len(incoming_sfx),
        "dispatched": True,
    }
