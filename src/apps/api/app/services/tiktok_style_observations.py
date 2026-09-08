"""Retention boundary for optional TikTok visual-style observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

STYLE_OBSERVATION_RETENTION = timedelta(days=90)


def _parse_timestamp(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        observed_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at


def _timestamp_is_fresh(raw: object, *, now: datetime | None = None) -> bool:
    observed_at = _parse_timestamp(raw)
    if observed_at is None:
        return False
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return observed_at >= reference - STYLE_OBSERVATION_RETENTION


def persona_style_expires_at(
    style: dict | None,
    *,
    profile: dict | None = None,
) -> datetime | None:
    """Return the reuse deadline for AI style derived from observations.

    Edited styles and persona-only derivations are creator-authored/non-observation
    state and therefore have no observation-retention deadline.
    """

    if not isinstance(style, dict) or not style or style.get("status") == "edited":
        return None
    derived_from = style.get("derived_from")
    if isinstance(derived_from, dict) and "observed_style_at" in derived_from:
        raw_observed_at = derived_from.get("observed_style_at")
        if raw_observed_at is None:
            return None
    else:
        observations = (profile or {}).get("style_observations") or {}
        raw_observed_at = observations.get("observed_at")
    observed_at = _parse_timestamp(raw_observed_at)
    return observed_at + STYLE_OBSERVATION_RETENTION if observed_at is not None else None


def fresh_style_observations(
    profile: dict | None,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Return observations only while their advertised reuse window is live."""

    observations = (profile or {}).get("style_observations") or {}
    raw_observed_at = observations.get("observed_at")
    if not raw_observed_at:
        return None
    if not _timestamp_is_fresh(raw_observed_at, now=now):
        return None
    return dict(observations)


def style_observations_are_fresh(
    profile: dict | None,
    *,
    now: datetime | None = None,
) -> bool:
    return fresh_style_observations(profile, now=now) is not None


def effective_persona_style(
    style: dict | None,
    *,
    profile: dict | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Exclude expired AI-derived visual style while preserving user edits."""

    if not isinstance(style, dict) or not style:
        return None
    resolved = dict(style)
    if resolved.get("status") == "edited":
        return resolved
    derived_from = resolved.get("derived_from")
    if isinstance(derived_from, dict) and "observed_style_at" in derived_from:
        observed_style_at = derived_from.get("observed_style_at")
        # Explicit null means the derivation did not consume retained visual
        # observations (for example, persona-only derivation after expiry).
        if observed_style_at is None:
            return resolved
        return resolved if _timestamp_is_fresh(observed_style_at, now=now) else None
    if isinstance(profile, dict) and profile.get("style_observations") is not None:
        return resolved if style_observations_are_fresh(profile, now=now) else None
    return resolved
