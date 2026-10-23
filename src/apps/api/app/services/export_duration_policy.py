"""Exclusive full-file limits from the immutable approved brief.

The stored v1 contract and device request stay readable by older workers. Their
existing brief digest binds the creator's exact timing words; a response-only
field gives the phone the same limit that publication independently enforces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.kria.brief import CreativeBrief
from app.kria.reply_language import say
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.length_policy import creator_max_export_duration_s, strict_duration_matches


def approved_export_maximum(assembly: Mapping[str, Any], contract: Any) -> float | None:
    if contract is None or contract.brief_digest is None:
        return None
    bindings = assembly.get("creator_brief_bindings") or {}
    if not isinstance(bindings, Mapping):
        raise CreatorRenderContractError("I couldn't read this edit's approved length.")
    # Presence is authority: a broken generation entry cannot fall back to root.
    raw = bindings.get(contract.generation_id, assembly.get("creator_brief_binding"))
    brief = raw.get("brief") if isinstance(raw, Mapping) else None
    digest = hashlib.sha256(
        json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if digest != contract.brief_digest:
        raise CreatorRenderContractError("This edit no longer matches its approved brief.")
    try:
        return creator_max_export_duration_s(brief=CreativeBrief.model_validate(brief))
    except ValueError as exc:
        raise CreatorRenderContractError("I couldn't read this edit's approved length.") from exc


def check_export_maximum(actual_s: float, maximum_s: float) -> None:
    """No rounding allowance and no lower bound: 40.000 fails 'under 40'."""
    if strict_duration_matches(actual_s, maximum_s):
        return
    raise CreatorRenderContractError(
        say(
            en=f"You asked for a video under {maximum_s:g} seconds including its ending; "
            f"this video is {actual_s:g} seconds.",
            tr=f"Kapanış dahil {maximum_s:g} saniyenin altında bir video istedin; "
            f"bu video {actual_s:g} saniye.",
        ),
        decline_reason="requirement_conflict",
        field_path="target_duration_s",
        alternative=say(
            en="Choose a shorter part or allow a longer video.",
            tr="Daha kısa bir bölüm seç veya daha uzun bir videoya izin ver.",
        ),
    )
