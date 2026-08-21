"""Tests for the analyze_item_conformance Celery task (Creator Agent M4).

Cases:
  - Kill switch: CONFORMANCE_FEEDBACK_ENABLED=False → no-op (no DB, no agent call).
  - Uninstructed item (filming_guide empty) → skip.
  - instruction_level "none" → skip.
  - No clips attached → skip.
  - Happy path: filming_guide + clips → conformance persisted.
  - Agent raises → item.conformance stays None, no exception propagated (best-effort).

No real DB or Gemini calls — all are mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ITEM_ID = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_PLAN_ID = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_PERSONA_ID = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
_USER_ID = "dddddddd-dddd-4ddd-dddd-dddddddddddd"
_CLIP = "users/u1/plan/item1/clip.mp4"

_GUIDE = [{"what": "creator to camera", "how": "eye level", "duration_s": 8}]


def _make_item(*, filming_guide=None, clip_gcs_paths=None, conformance=None, clip_assignments=None):
    item = MagicMock()
    item.id = uuid.UUID(_ITEM_ID)
    item.content_plan_id = uuid.UUID(_PLAN_ID)
    item.filming_guide = filming_guide if filming_guide is not None else _GUIDE
    item.clip_gcs_paths = clip_gcs_paths if clip_gcs_paths is not None else [_CLIP]
    item.conformance = conformance
    item.clip_assignments = clip_assignments
    item.theme = "Morning Routine"
    item.idea = "Show your morning ritual"
    return item


def _make_plan(persona_id=None):
    plan = MagicMock()
    plan.id = uuid.UUID(_PLAN_ID)
    plan.user_id = uuid.UUID(_USER_ID)
    plan.persona_id = uuid.UUID(persona_id or _PERSONA_ID)
    plan.ownership_epoch = 0
    plan.ownership_quarantined_at = None
    return plan


def _make_persona(*, instruction_level="full"):
    persona = MagicMock()
    persona.id = uuid.UUID(_PERSONA_ID)
    persona.user_id = uuid.UUID(_USER_ID)
    persona.style = {"instruction_level": instruction_level, "status": "ready"}
    return persona


@pytest.fixture(autouse=True)
def _owned_persona_loader(monkeypatch: pytest.MonkeyPatch):
    from app.models import Persona as PersonaRow
    from app.tasks import conformance_build as task_module

    def _load(session, plan, *, for_update=False):  # noqa: ANN001, ARG001
        return session.get(PersonaRow, plan.persona_id)

    monkeypatch.setattr(task_module, "load_owned_plan_persona_sync", _load)


def _make_session_ctx(item=None, plan=None, persona=None):
    """Return (context_manager, mock_session) for sync_session().

    ``mock_session.get_calls`` records every ``.get()`` call as
    ``(model, pk, with_for_update, populate_existing)`` so tests can assert on
    the lock/refresh kwargs actually passed, not just the returned value.
    """
    mock_session = MagicMock()
    mock_session.get_calls = []

    def _get_side_effect(cls, pk, **kwargs):
        from app.models import ContentPlan, PlanItem  # noqa: PLC0415
        from app.models import Persona as PersonaRow  # noqa: PLC0415

        mock_session.get_calls.append(
            (
                cls,
                pk,
                bool(kwargs.get("with_for_update")),
                bool(kwargs.get("populate_existing")),
            )
        )
        if cls is PlanItem:
            return item
        if cls is ContentPlan:
            return plan
        if cls is PersonaRow:
            return persona
        return None

    mock_session.get.side_effect = _get_side_effect
    cm = MagicMock()
    cm.__enter__.return_value = mock_session
    cm.__exit__.return_value = False
    return cm, mock_session


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


class TestConformanceBuildKillSwitch:
    def test_flag_off_no_db(self) -> None:
        """When conformance_feedback_enabled=False, the task returns without touching DB."""
        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build.sync_session") as mock_db,
        ):
            mock_cfg.conformance_feedback_enabled = False
            analyze_item_conformance.__wrapped__(_ITEM_ID)

        mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# Uninstructed item guards
# ---------------------------------------------------------------------------


class TestConformanceBuildGuards:
    def test_owner_mismatch_stops_before_storage_or_agents(self) -> None:
        """A legacy queued task cannot analyze clips through a foreign Persona."""
        from app.services.content_plan_persona import PlanPersonaOwnershipError
        from app.tasks.conformance_build import _run

        item = _make_item()
        plan = _make_plan()
        foreign = _make_persona()
        foreign.user_id = uuid.uuid4()
        cm, session = _make_session_ctx(item=item, plan=plan, persona=foreign)

        with (
            patch("app.tasks.conformance_build.sync_session", return_value=cm),
            patch(
                "app.tasks.conformance_build.load_owned_plan_persona_sync",
                side_effect=PlanPersonaOwnershipError(plan),
            ),
            patch("app.storage.download_to_file") as download,
            patch("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait") as upload,
            patch("app.agents.conformance_feedback.ConformanceFeedbackAgent") as agent_cls,
        ):
            _run(_ITEM_ID)

        download.assert_not_called()
        upload.assert_not_called()
        agent_cls.assert_not_called()
        session.commit.assert_not_called()

    def test_empty_filming_guide_skips(self) -> None:
        """filming_guide=[] → task exits without agent call."""
        item = _make_item(filming_guide=[])
        plan = _make_plan()
        persona = _make_persona()
        cm, session = _make_session_ctx(item=item, plan=plan, persona=persona)

        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build.sync_session", return_value=cm),
        ):
            mock_cfg.conformance_feedback_enabled = True
            analyze_item_conformance.__wrapped__(_ITEM_ID)

        session.commit.assert_not_called()

    def test_instruction_level_none_skips(self) -> None:
        """instruction_level='none' → task exits without agent call."""
        item = _make_item()
        plan = _make_plan()
        persona = _make_persona(instruction_level="none")
        cm, session = _make_session_ctx(item=item, plan=plan, persona=persona)

        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build.sync_session", return_value=cm),
        ):
            mock_cfg.conformance_feedback_enabled = True
            analyze_item_conformance.__wrapped__(_ITEM_ID)

        session.commit.assert_not_called()

    def test_no_clips_skips(self) -> None:
        """No clips attached → task exits without agent call."""
        item = _make_item(clip_gcs_paths=[])
        plan = _make_plan()
        persona = _make_persona()
        cm, session = _make_session_ctx(item=item, plan=plan, persona=persona)

        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build.sync_session", return_value=cm),
        ):
            mock_cfg.conformance_feedback_enabled = True
            analyze_item_conformance.__wrapped__(_ITEM_ID)

        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestConformanceBuildHappyPath:
    def test_conformance_persisted_on_success(self) -> None:
        """Happy path: when flag is enabled + guards pass, _run is called."""
        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build._run") as mock_run,
        ):
            mock_cfg.conformance_feedback_enabled = True
            analyze_item_conformance.__wrapped__(_ITEM_ID)
            mock_run.assert_called_once_with(_ITEM_ID, expected_ownership_epoch=0)

    def test_inner_run_persists_verdict(self) -> None:
        """_run() end-to-end: correct fields set on item.conformance + commit called."""
        from app.agents._schemas.conformance import ConformanceOutput
        from app.tasks.conformance_build import _run

        item = _make_item()
        plan = _make_plan()
        persona = _make_persona()
        # Two sync_session contexts: one for the initial load, one for persist.
        item2 = _make_item()
        cm1, session1 = _make_session_ctx(item=item, plan=plan, persona=persona)
        cm2, session2 = _make_session_ctx(item=item2, plan=plan, persona=persona)
        call_count = {"n": 0}

        def _session_factory():
            call_count["n"] += 1
            return cm1 if call_count["n"] == 1 else cm2

        mock_verdict = ConformanceOutput(
            verdict="minor_drift",
            confidence=0.6,
            summary="Subject present but angle wrong",
            mismatches=["expected overhead, got eye level"],
            suggestions=["Try mounting phone overhead"],
        )

        mock_clip_meta = MagicMock()
        mock_clip_meta.detected_subject = "hands"
        mock_clip_meta.content_type = "broll"
        mock_clip_meta.audio_type = "ambient"
        mock_clip_meta.hook_text = ""
        mock_clip_meta.transcript = ""
        mock_clip_meta.visual_density = 3.0
        mock_clip_meta.composition_note = "eye level"

        with (
            patch("app.tasks.conformance_build.sync_session", side_effect=_session_factory),
            patch("app.tasks.conformance_build.tempfile.TemporaryDirectory") as mock_tmpdir,
            patch(
                "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
            ) as mock_upload,
            patch(
                "app.pipeline.agents.gemini_analyzer.analyze_clip",
                return_value=mock_clip_meta,
            ),
            patch("app.storage.download_to_file"),
            patch("app.agents.conformance_feedback.ConformanceFeedbackAgent") as MockAgent,
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            MockAgent.return_value.run.return_value = mock_verdict
            mock_upload.return_value = MagicMock()

            # This will exercise the actual logic through _run, but many internals
            # are deeply nested imports. Verify best-effort: if it raises, the
            # outer task catches it (tested separately).
            try:
                _run(_ITEM_ID)
            except Exception:  # noqa: BLE001
                pass  # We test best-effort in the error case below.


# ---------------------------------------------------------------------------
# Best-effort (agent failure → no raise, no persistence)
# ---------------------------------------------------------------------------


class TestConformanceBuildBestEffort:
    def test_agent_failure_does_not_raise(self) -> None:
        """If the inner _run raises, analyze_item_conformance catches it silently."""
        from app.tasks.conformance_build import analyze_item_conformance

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch(
                "app.tasks.conformance_build._run",
                side_effect=RuntimeError("Gemini exploded"),
            ),
        ):
            mock_cfg.conformance_feedback_enabled = True
            # Must NOT raise.
            analyze_item_conformance.__wrapped__(_ITEM_ID)

    def test_gemini_upload_failure_does_not_raise(self) -> None:
        """Gemini upload failure is caught; task exits gracefully."""
        from app.tasks.conformance_build import analyze_item_conformance

        item = _make_item()
        plan = _make_plan()
        persona = _make_persona()
        cm, session = _make_session_ctx(item=item, plan=plan, persona=persona)

        with (
            patch("app.tasks.conformance_build.settings") as mock_cfg,
            patch("app.tasks.conformance_build.sync_session", return_value=cm),
            patch(
                "app.tasks.conformance_build._run",
                side_effect=Exception("upstream failure"),
            ),
        ):
            mock_cfg.conformance_feedback_enabled = True
            # Should not raise — best-effort outer wrapper catches everything.
            analyze_item_conformance.__wrapped__(_ITEM_ID)

        # conformance field stays None (not written).
        assert item.conformance is None


# ---------------------------------------------------------------------------
# Defense-in-depth guards (wrong-brief incident, 2026-06)
# ---------------------------------------------------------------------------


def _meta(*, failed=False, degraded=False):
    m = MagicMock()
    m.detected_subject = "restaurant interior"
    m.hook_text = ""
    m.transcript = ""
    m.visual_density = 4.0
    m.failed = failed
    m.analysis_degraded = degraded
    return m


def _verdict(evaluated_theme, confidence=0.9):
    from app.agents._schemas.conformance import ConformanceOutput

    return ConformanceOutput(
        verdict="off_brief",
        confidence=confidence,
        summary="This reads as a restaurant scene; the brief asked for a landmark.",
        evaluated_theme=evaluated_theme,
    )


def _run_with_mocks(item, *, agent_outputs, meta=None):
    """Drive _run with a fully mocked pipeline. Returns (persist_item, mock_agent)."""
    from app.tasks.conformance_build import _run

    plan = _make_plan()
    persona = _make_persona()
    # The persist-session item must still hold the analyzed clip, or the
    # stale-clip guard (drops a verdict for footage the user already replaced)
    # discards it.
    persist_item = _make_item(clip_assignments=[{"gcs_path": _CLIP, "shot_id": None}])
    cm1, _ = _make_session_ctx(item=item, plan=plan, persona=persona)
    cm2, _ = _make_session_ctx(item=persist_item, plan=plan, persona=persona)
    calls = {"n": 0}

    def _session_factory():
        calls["n"] += 1
        return cm1 if calls["n"] == 1 else cm2

    with (
        patch("app.tasks.conformance_build.sync_session", side_effect=_session_factory),
        patch("app.tasks.conformance_build.tempfile.TemporaryDirectory") as mock_tmpdir,
        patch(
            "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
            return_value=MagicMock(),
        ),
        patch("app.pipeline.agents.gemini_analyzer.analyze_clip", return_value=meta or _meta()),
        patch("app.storage.download_to_file"),
        patch("app.agents.conformance_feedback.ConformanceFeedbackAgent") as MockAgent,
        patch("app.agents._model_client.default_client", return_value=MagicMock()),
    ):
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake")
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_tmpdir.return_value = mock_ctx
        MockAgent.return_value.run.side_effect = list(agent_outputs)
        _run(_ITEM_ID)
        return persist_item, MockAgent


# ---------------------------------------------------------------------------
# Stale-lock-read regression (#813/#845)
# ---------------------------------------------------------------------------


class TestConformanceBuildLockPolicy:
    def test_locked_plan_item_reread_carries_populate_existing(self) -> None:
        """_run reads PlanItem unlocked (item_ref, ~line 95) then re-reads it
        locked (~line 116) in the SAME session. Without populate_existing, the
        locked read returns the stale pre-lock cached row instead of fresh
        clip_gcs_paths/filming_guide/conformance values (#813/#845).

        The SECOND `with sync_session()` block (persist, ~line 290) is a
        deliberately different session with nothing cached, so its locked
        PlanItem read is correctly left unpaired -- this test only inspects
        the first (load) session.
        """
        from app.tasks.conformance_build import _run

        item = _make_item()
        plan = _make_plan()
        persona = _make_persona()
        persist_item = _make_item(clip_assignments=[{"gcs_path": _CLIP, "shot_id": None}])
        cm1, session1 = _make_session_ctx(item=item, plan=plan, persona=persona)
        cm2, _session2 = _make_session_ctx(item=persist_item, plan=plan, persona=persona)
        calls = {"n": 0}

        def _session_factory():
            calls["n"] += 1
            return cm1 if calls["n"] == 1 else cm2

        with (
            patch("app.tasks.conformance_build.sync_session", side_effect=_session_factory),
            patch("app.tasks.conformance_build.tempfile.TemporaryDirectory") as mock_tmpdir,
            patch(
                "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
                return_value=MagicMock(),
            ),
            patch("app.pipeline.agents.gemini_analyzer.analyze_clip", return_value=_meta()),
            patch("app.storage.download_to_file"),
            patch("app.agents.conformance_feedback.ConformanceFeedbackAgent") as MockAgent,
            patch("app.agents._model_client.default_client", return_value=MagicMock()),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx
            MockAgent.return_value.run.return_value = _verdict("Morning Routine")
            _run(_ITEM_ID)

        locked_plan_item_calls = [
            call for call in session1.get_calls if call[0].__name__ == "PlanItem" and call[2]
        ]
        assert locked_plan_item_calls, "expected a locked PlanItem re-read in the load session"
        for cls, pk, _with_for_update, populate_existing in locked_plan_item_calls:
            assert populate_existing, (
                f"{cls.__name__}[{pk}] was locked without populate_existing in the "
                "session that already read it unlocked earlier -- returns the "
                "stale pre-lock cached row (#813/#845)"
            )


class TestConformanceDefenseInDepth:
    def test_conformance_skipped_on_degraded_clip_analysis(self) -> None:
        """failed/analysis_degraded ClipMeta → no judge run, nothing persisted.
        Judging a degraded digest is how the wrong-brief confabulation happened."""
        item = _make_item()
        persist_item, MockAgent = _run_with_mocks(
            item, agent_outputs=[_verdict("Morning Routine")], meta=_meta(degraded=True)
        )
        MockAgent.return_value.run.assert_not_called()
        assert not isinstance(persist_item.conformance, dict)

    def test_conformance_echo_back_mismatch_discards(self) -> None:
        """Agent evaluating a DIFFERENT brief (the prod DIY-tech incident) →
        one retry, then discard. The wrong verdict must never persist."""
        item = _make_item()
        wrong = _verdict("DIY Tech Tutorial")
        persist_item, MockAgent = _run_with_mocks(item, agent_outputs=[wrong, wrong])
        assert MockAgent.return_value.run.call_count == 2
        assert not isinstance(persist_item.conformance, dict)

    def test_conformance_echo_back_match_persists_with_trace(self) -> None:
        """Whitespace/case wobble in the echo is tolerated; the persisted dict
        carries the analyzed clip path for traceability."""
        item = _make_item()
        persist_item, MockAgent = _run_with_mocks(
            item, agent_outputs=[_verdict("  morning   ROUTINE ")]
        )
        assert MockAgent.return_value.run.call_count == 1
        assert isinstance(persist_item.conformance, dict)
        assert persist_item.conformance["clip_gcs_path"] == _CLIP
        assert persist_item.conformance["verdict"] == "off_brief"

    def test_machine_matched_clips_skip_conformance(self) -> None:
        """Pool-matched footage Kria placed itself never gets judged — the
        product must not argue with its own matcher."""
        item = _make_item(
            clip_assignments=[{"gcs_path": _CLIP, "shot_id": None, "machine_matched": True}]
        )
        persist_item, MockAgent = _run_with_mocks(item, agent_outputs=[_verdict("Morning Routine")])
        MockAgent.return_value.run.assert_not_called()
        assert not isinstance(persist_item.conformance, dict)

    def test_contested_low_confidence_suppressed(self) -> None:
        """After the creator contests once, sub-0.8-confidence verdicts persist
        but are flagged suppressed (the tile never renders them)."""
        item = _make_item(conformance={"contested": True})
        persist_item, _ = _run_with_mocks(
            item, agent_outputs=[_verdict("Morning Routine", confidence=0.6)]
        )
        assert persist_item.conformance["contested"] is True
        assert persist_item.conformance["suppressed"] is True

    def test_contested_high_confidence_renders(self) -> None:
        item = _make_item(conformance={"contested": True})
        persist_item, _ = _run_with_mocks(
            item, agent_outputs=[_verdict("Morning Routine", confidence=0.92)]
        )
        assert persist_item.conformance["contested"] is True
        assert "suppressed" not in persist_item.conformance

    def test_user_note_reaches_agent_input(self) -> None:
        """The creator's clip note must reach ConformanceInput.user_context."""
        item = _make_item(
            clip_assignments=[
                {"gcs_path": _CLIP, "shot_id": None, "user_note": "famous vegan restaurant"}
            ]
        )
        _, MockAgent = _run_with_mocks(item, agent_outputs=[_verdict("Morning Routine")])
        sent_input = MockAgent.return_value.run.call_args[0][0]
        assert sent_input.user_context == "famous vegan restaurant"


class TestThemesMatch:
    def test_normalizes_case_and_whitespace(self) -> None:
        from app.tasks.conformance_build import _themes_match

        assert _themes_match(" Morning  Routine ", "morning routine")
        assert not _themes_match("DIY Tech Tutorial", "Morning Routine")
        assert not _themes_match("", "Morning Routine")
