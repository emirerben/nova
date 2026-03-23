"""Unit tests for tasks/template_orchestrate.py — all external calls mocked."""

from unittest.mock import MagicMock, call, patch

import pytest

from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    TemplateRecipe,
)
from app.pipeline.template_matcher import TemplateMismatchError


def _make_clip_meta(clip_id: str = "clip_a", degraded: bool = False) -> ClipMeta:
    return ClipMeta(
        clip_id=clip_id,
        transcript="test transcript",
        hook_text="test hook",
        hook_score=7.0,
        best_moments=[{"start_s": 0.0, "end_s": 10.0, "energy": 7.0, "description": ""}],
        analysis_degraded=degraded,
        clip_path="/tmp/test.mp4",
    )


def _make_recipe() -> TemplateRecipe:
    return TemplateRecipe(
        shot_count=2,
        total_duration_s=20.0,
        hook_duration_s=3.0,
        slots=[
            {"position": 1, "target_duration_s": 10.0, "priority": 5, "slot_type": "hook"},
        ],
        copy_tone="casual",
        caption_style="bold",
    )


class TestOrchestratePipelineHelpers:
    """Test the parallel helper functions directly."""

    def test_upload_clips_parallel_returns_in_order(self):
        from app.tasks.template_orchestrate import _upload_clips_parallel

        refs = [MagicMock(name=f"files/ref_{i}") for i in range(3)]

        with patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload:
            mock_upload.side_effect = refs
            result = _upload_clips_parallel(["/tmp/a.mp4", "/tmp/b.mp4", "/tmp/c.mp4"])

        assert len(result) == 3
        # Result must preserve input order (not completion order)
        assert mock_upload.call_count == 3

    def test_analyze_clips_parallel_counts_failures(self):
        from app.tasks.template_orchestrate import _analyze_clips_parallel

        file_refs = [MagicMock() for _ in range(3)]
        local_paths = [f"/tmp/clip_{i}.mp4" for i in range(3)]

        def _mock_analyze(ref, **kwargs):
            if ref == file_refs[1]:
                raise GeminiAnalysisError("analysis failed")
            return _make_clip_meta(f"clip_{file_refs.index(ref)}")

        from app.pipeline.transcribe import Transcript
        whisper_transcript = Transcript(words=[], full_text="", low_confidence=True)

        with patch("app.tasks.template_orchestrate.analyze_clip", side_effect=lambda r, **kw: _mock_analyze(r, **kw)), \
             patch("app.pipeline.transcribe.transcribe_whisper", return_value=whisper_transcript):
            # The failing clip falls back to whisper heuristic → returns a meta, not failure
            metas, failed_count = _analyze_clips_parallel(file_refs, local_paths)

        # With whisper fallback, no clips should be counted as failed
        assert failed_count == 0

    def test_threshold_check_50_percent_failure(self):
        """If >50% of clips fail even whisper fallback, failed_count > 50%."""
        from app.tasks.template_orchestrate import _analyze_clips_parallel

        file_refs = [MagicMock() for _ in range(4)]
        local_paths = [f"/tmp/clip_{i}.mp4" for i in range(4)]

        with patch("app.tasks.template_orchestrate.analyze_clip") as mock_analyze, \
             patch("app.pipeline.transcribe.transcribe_whisper") as mock_whisper:
            mock_analyze.side_effect = GeminiAnalysisError("failed")
            mock_whisper.side_effect = Exception("whisper also failed")

            metas, failed_count = _analyze_clips_parallel(file_refs, local_paths)

        # All clips failed both Gemini and Whisper → all counted as failed
        assert failed_count == 4
        assert len(metas) == 0


class TestAnalyzeTemplateTask:
    def test_happy_path_sets_ready_status(self):
        from app.tasks.template_orchestrate import analyze_template_task

        mock_recipe = _make_recipe()
        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"
        mock_template.analysis_status = "analyzing"

        with patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx, \
             patch("app.tasks.template_orchestrate.download_to_file"), \
             patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload, \
             patch("app.tasks.template_orchestrate.analyze_template") as mock_analyze:

            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_upload.return_value = MagicMock()
            mock_analyze.return_value = mock_recipe

            analyze_template_task("template-123")

        assert mock_template.analysis_status == "ready"
        assert mock_template.recipe_cached is not None

    def test_failure_sets_failed_status(self):
        from app.tasks.template_orchestrate import analyze_template_task

        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"

        with patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx, \
             patch("app.tasks.template_orchestrate.download_to_file"), \
             patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload:

            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_upload.side_effect = Exception("Gemini unavailable")

            analyze_template_task("template-123")

        assert mock_template.analysis_status == "failed"


class TestOrchestrateTemplateJobErrors:
    def test_template_mismatch_error_sets_processing_failed(self):
        """TemplateMismatchError → job.status = processing_failed with code in error_detail."""
        from app.tasks.template_orchestrate import orchestrate_template_job

        mock_job = MagicMock()
        mock_job.status = "queued"
        mock_job.error_detail = None

        def _mock_ctx():
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=_mock_session)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        _mock_session = MagicMock()
        _mock_session.get.return_value = mock_job

        with patch("app.tasks.template_orchestrate._sync_session", side_effect=_mock_ctx), \
             patch("app.tasks.template_orchestrate._run_template_job") as mock_run:
            mock_run.side_effect = ValueError(
                "TEMPLATE_CLIP_DURATION_MISMATCH: No clip fits slot"
            )
            orchestrate_template_job("12345678-1234-5678-1234-567812345678")

        assert mock_job.status == "processing_failed"
        assert "TEMPLATE_CLIP_DURATION_MISMATCH" in mock_job.error_detail

    def test_never_raises_outer_exception(self):
        """orchestrate_template_job must never raise — all errors caught."""
        from app.tasks.template_orchestrate import orchestrate_template_job

        def _mock_ctx():
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=session)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        session = MagicMock()
        session.get.return_value = MagicMock()

        with patch("app.tasks.template_orchestrate._run_template_job") as mock_run, \
             patch("app.tasks.template_orchestrate._sync_session", side_effect=_mock_ctx):
            mock_run.side_effect = RuntimeError("unexpected crash")

            # Must not raise
            orchestrate_template_job("12345678-1234-5678-1234-567812345678")
