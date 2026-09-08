from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.tiktok_style_observations import (
    effective_persona_style,
    fresh_style_observations,
    persona_style_expires_at,
    style_observations_are_fresh,
)


def _profile(observed_at: datetime) -> dict:
    return {
        "style_observations": {
            "observed_at": observed_at.isoformat(),
            "videos_seen": 3,
            "aggregate": {"font_feel": "bold_display"},
        }
    }


def test_style_observation_reuse_stops_after_ninety_days() -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)

    assert fresh_style_observations(_profile(now - timedelta(days=89)), now=now) is not None
    assert fresh_style_observations(_profile(now - timedelta(days=91)), now=now) is None
    assert not style_observations_are_fresh(_profile(now - timedelta(days=91)), now=now)


def test_missing_or_malformed_observation_timestamp_fails_closed() -> None:
    assert fresh_style_observations({"style_observations": {"videos_seen": 3}}) is None
    assert (
        fresh_style_observations({"style_observations": {"observed_at": "not-a-timestamp"}}) is None
    )


def test_derived_style_expires_with_its_observation_provenance() -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    stale = {
        "status": "ready",
        "style_set_id": "editorial",
        "derived_from": {"observed_style_at": (now - timedelta(days=91)).isoformat()},
    }

    assert effective_persona_style(stale, now=now) is None


def test_user_edited_style_survives_expired_observation_provenance() -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    edited = {
        "status": "edited",
        "style_set_id": "editorial",
        "derived_from": {"observed_style_at": (now - timedelta(days=365)).isoformat()},
    }

    assert effective_persona_style(edited, now=now) == edited


def test_persona_only_derivation_survives_stale_profile_observations() -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    style = {
        "status": "ready",
        "style_set_id": "editorial",
        "derived_from": {"observed_style_at": None},
    }

    assert (
        effective_persona_style(
            style,
            profile=_profile(now - timedelta(days=365)),
            now=now,
        )
        == style
    )


def test_derived_style_deadline_tracks_observation_but_edits_have_none() -> None:
    observed_at = datetime(2026, 6, 1, 12, tzinfo=UTC)
    derived = {
        "status": "ready",
        "derived_from": {"observed_style_at": observed_at.isoformat()},
    }
    edited = {**derived, "status": "edited"}

    assert persona_style_expires_at(derived) == observed_at + timedelta(days=90)
    assert persona_style_expires_at(edited) is None
