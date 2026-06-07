"""Tests for the derive_user_style Celery task (Creator Agent M1).

Validates the task-level guard logic:
  - Kill switch: settings.user_style_enabled=False → early return, no DB write.
  - Missing row: persona_id doesn't exist → early return, no crash.
  - Edited guard: style.status=='edited' → skip derivation (user's say wins).
  - force=True: bypasses the edited guard (POST /personas/style/rederive).
  - Agent failure: persists status='failed', preserves prior knobs.
  - Success: writes UserStyle(status='ready') to row.style and commits.

No real DB or GPU required — all DB calls are mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PERSONA_ID = "11111111-1111-4111-8111-111111111111"  # fixed valid UUID for all tests


def _make_persona_row(style=None, persona=None, tiktok_profile=None):
    row = MagicMock()
    # Use a real UUID string directly (mock attribute storage is unreliable for
    # 'id' due to Python repr interplay with MagicMock children).
    row.id = uuid.UUID(_PERSONA_ID)
    row.style = style
    row.persona = persona or {
        "summary": "Aesthetic city-walk creator",
        "content_pillars": ["urban photography", "night life"],
        "tone": "calm and cinematic",
        "audience": "travel & lifestyle fans",
    }
    row.tiktok_profile = tiktok_profile
    return row


def _make_session_ctx(row_or_none):
    """Return a mock sync_session() context manager that yields a mock session.

    Magic methods like __enter__ must be configured via .return_value on the
    class-level MagicMock attribute — NOT by assignment on the instance, which
    Python ignores for dunder lookup (it always goes to type(obj).__enter__).
    """
    mock_session = MagicMock()
    mock_session.get.return_value = row_or_none
    cm = MagicMock()
    cm.__enter__.return_value = mock_session  # class-level magic method mock
    cm.__exit__.return_value = False
    return cm, mock_session


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


class TestDeriveUserStyleKillSwitch:
    def test_disabled_returns_early_without_db(self):
        """When user_style_enabled=False the task exits before touching the DB."""
        from app.tasks.style_build import derive_user_style

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session") as mock_db,
        ):
            mock_cfg.user_style_enabled = False
            # Invoke the underlying function directly (no Celery broker needed).
            # `self` param is the Celery task instance; pass a dummy.
            derive_user_style.__wrapped__(str(uuid.uuid4()))
        mock_db.assert_not_called()

    def test_enabled_proceeds_to_db(self):
        """When enabled, at least one DB session is opened."""
        from app.tasks.style_build import derive_user_style

        row = _make_persona_row()
        cm, _session = _make_session_ctx(row)

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", return_value=cm),
            # Raise in catalog load so the task exits early without a model call.
            patch("app.tasks.style_build._build_catalog_inputs", side_effect=RuntimeError("stop")),
        ):
            mock_cfg.user_style_enabled = True
            derive_user_style.__wrapped__(_PERSONA_ID)

        # DB was opened at least once (the initial persona read).
        assert cm.__enter__.call_count >= 1


# ---------------------------------------------------------------------------
# Missing-row guard
# ---------------------------------------------------------------------------


class TestDeriveUserStyleMissingRow:
    def test_missing_row_returns_without_error(self):
        """If the persona row doesn't exist, the task exits cleanly."""
        from app.tasks.style_build import derive_user_style

        cm, _session = _make_session_ctx(None)  # get() returns None

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", return_value=cm),
        ):
            mock_cfg.user_style_enabled = True
            # Should not raise.
            derive_user_style.__wrapped__(str(uuid.uuid4()))

        # No commit on a missing row.
        _session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Edited-guard
# ---------------------------------------------------------------------------


class TestDeriveUserStyleEditedGuard:
    def test_skips_when_style_edited_and_no_force(self):
        """status='edited' + force=False → task exits without touching the agent."""
        from app.tasks.style_build import derive_user_style

        row = _make_persona_row(style={"status": "edited", "style_set_id": "film_mono"})
        cm, _session = _make_session_ctx(row)

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", return_value=cm),
            patch("app.tasks.style_build._build_catalog_inputs") as mock_catalog,
        ):
            mock_cfg.user_style_enabled = True
            derive_user_style.__wrapped__(_PERSONA_ID, force=False)

        # Catalog loading (and therefore the agent) must never be reached.
        mock_catalog.assert_not_called()
        _session.commit.assert_not_called()

    def test_force_true_bypasses_edited_guard(self):
        """force=True must bypass the edited guard and proceed to agent invocation."""
        from app.tasks.style_build import derive_user_style

        row = _make_persona_row(style={"status": "edited", "style_set_id": "film_mono"})
        cm, _session = _make_session_ctx(row)

        from app.agents._schemas.user_style import UserStyle
        from app.agents.style_derivation import StyleDerivationOutput

        derived = StyleDerivationOutput(
            style=UserStyle(style_set_id="editorial_serif", status="ready")
        )

        # StyleDerivationAgent is imported lazily inside the task function body
        # (PLC0415 pattern), so we must patch it at the source module.
        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", return_value=cm),
            patch("app.tasks.style_build._build_catalog_inputs", return_value=([], [])),
            patch("app.agents.style_derivation.StyleDerivationAgent") as MockAgent,
            patch("app.agents._model_client.default_client"),
            patch("app.agents._runtime.RunContext"),
        ):
            mock_cfg.user_style_enabled = True
            MockAgent.return_value.run.return_value = derived
            derive_user_style.__wrapped__(_PERSONA_ID, force=True)

        # Session.commit must have been called (style was written).
        _session.commit.assert_called()

    def test_status_not_edited_proceeds(self):
        """status='ready' (not 'edited') → guard passes, agent is invoked."""
        from app.tasks.style_build import derive_user_style

        row = _make_persona_row(style={"status": "ready"})
        cm, _session = _make_session_ctx(row)

        from app.agents._schemas.user_style import UserStyle
        from app.agents.style_derivation import StyleDerivationOutput

        derived = StyleDerivationOutput(style=UserStyle(style_set_id="default", status="ready"))

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", return_value=cm),
            patch("app.tasks.style_build._build_catalog_inputs", return_value=([], [])),
            patch("app.agents.style_derivation.StyleDerivationAgent") as MockAgent,
            patch("app.agents._model_client.default_client"),
            patch("app.agents._runtime.RunContext"),
        ):
            mock_cfg.user_style_enabled = True
            MockAgent.return_value.run.return_value = derived
            derive_user_style.__wrapped__(_PERSONA_ID)

        _session.commit.assert_called()


# ---------------------------------------------------------------------------
# Agent-failure handling
# ---------------------------------------------------------------------------


class TestDeriveUserStyleAgentFailure:
    def test_agent_failure_persists_failed_status_and_preserves_prior_knobs(self):
        """On agent exception: style.status='failed', prior knobs are preserved."""
        from app.tasks.style_build import derive_user_style

        prior_style = {
            "status": "ready",
            "style_set_id": "film_mono",
            "knobs": {"text_size_px": 55},
        }
        row = _make_persona_row(style=dict(prior_style))

        # We need two separate session calls (read + error write).
        read_cm, read_session = _make_session_ctx(row)
        write_row = _make_persona_row(style=dict(prior_style))  # second get() call
        write_session = MagicMock()
        write_session.get.return_value = write_row
        write_cm = MagicMock()
        write_cm.__enter__ = MagicMock(return_value=write_session)
        write_cm.__exit__ = MagicMock(return_value=False)

        call_count = [0]
        original_prior_style = dict(prior_style)

        def _sync_session_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return read_cm
            return write_cm

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", side_effect=_sync_session_factory),
            patch("app.tasks.style_build._build_catalog_inputs", return_value=([], [])),
            patch("app.agents.style_derivation.StyleDerivationAgent") as MockAgent,
            patch("app.agents._model_client.default_client"),
            patch("app.agents._runtime.RunContext"),
        ):
            mock_cfg.user_style_enabled = True
            MockAgent.return_value.run.side_effect = RuntimeError("model API 500")
            derive_user_style.__wrapped__(_PERSONA_ID)

        # The write session must have been committed with status=failed.
        write_session.commit.assert_called_once()
        written = write_row.style
        assert written["status"] == "failed"
        # Prior knobs must be preserved (not wiped by the failure).
        assert written.get("style_set_id") == original_prior_style["style_set_id"]

    def test_agent_failure_never_overwrites_edited_style(self):
        """If status='edited' is set between agent call and error handler, skip the write."""
        from app.tasks.style_build import derive_user_style

        # First read: status='ready' (guard passes).
        row = _make_persona_row(style={"status": "ready", "style_set_id": "film_mono"})
        read_cm, _read_session = _make_session_ctx(row)

        # Error write: row is now 'edited' (concurrent PATCH happened).
        edited_row = _make_persona_row(
            style={"status": "edited", "style_set_id": "editorial_serif"}
        )
        write_session = MagicMock()
        write_session.get.return_value = edited_row
        write_cm = MagicMock()
        write_cm.__enter__ = MagicMock(return_value=write_session)
        write_cm.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def _factory():
            call_count[0] += 1
            return read_cm if call_count[0] == 1 else write_cm

        with (
            patch("app.tasks.style_build.settings") as mock_cfg,
            patch("app.tasks.style_build.sync_session", side_effect=_factory),
            patch("app.tasks.style_build._build_catalog_inputs", return_value=([], [])),
            patch("app.agents.style_derivation.StyleDerivationAgent") as MockAgent,
            patch("app.agents._model_client.default_client"),
            patch("app.agents._runtime.RunContext"),
        ):
            mock_cfg.user_style_enabled = True
            MockAgent.return_value.run.side_effect = RuntimeError("boom")
            derive_user_style.__wrapped__(_PERSONA_ID)

        # Error handler must NOT commit (edited row should be left alone).
        write_session.commit.assert_not_called()
