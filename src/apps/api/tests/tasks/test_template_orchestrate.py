"""Unit tests for tasks/template_orchestrate.py — all external calls mocked."""

import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.models import VideoTemplate
from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    TemplateRecipe,
)


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

        with (
            patch(
                "app.tasks.template_orchestrate.analyze_clip",
                side_effect=lambda r, **kw: _mock_analyze(r, **kw),
            ),
            patch("app.pipeline.transcribe.transcribe_whisper", return_value=whisper_transcript),
        ):
            # The failing clip falls back to whisper heuristic → returns a meta, not failure
            metas, failed_count = _analyze_clips_parallel(file_refs, local_paths)

        # With whisper fallback, no clips should be counted as failed
        assert failed_count == 0

    def test_empty_best_moments_engages_whisper_fallback(self):
        """Defense-in-depth: when analyze_clip 'succeeds' but returns 0
        best_moments (a bug we've seen in prod), the orchestrator must treat
        it the same as an analysis failure and engage the Whisper fallback,
        which generates synthetic fallback moments. Without this, downstream
        matching has nothing to work with and the whole job fails."""
        from app.tasks.template_orchestrate import _analyze_clips_parallel

        file_refs = [MagicMock()]
        local_paths = ["/tmp/clip_0.mp4"]

        # analyze_clip "succeeds" but returns a meta with empty best_moments.
        empty_meta = _make_clip_meta("clip_0")
        empty_meta.best_moments = []

        from app.pipeline.transcribe import Transcript
        whisper_transcript = Transcript(
            words=[], full_text="hello world", low_confidence=False
        )

        with (
            patch(
                "app.tasks.template_orchestrate.analyze_clip",
                return_value=empty_meta,
            ),
            patch(
                "app.pipeline.transcribe.transcribe_whisper",
                return_value=whisper_transcript,
            ),
        ):
            metas, failed_count = _analyze_clips_parallel(file_refs, local_paths)

        # Whisper fallback engaged → meta is present, marked degraded, with
        # synthesized fallback moments.
        assert failed_count == 0
        assert len(metas) == 1
        assert metas[0].analysis_degraded is True
        assert len(metas[0].best_moments) > 0

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


class TestPreBurnCurtainSlotText:
    """Test _pre_burn_curtain_slot_text encodes intermediates with ultrafast."""

    def test_pre_burn_uses_ultrafast_preset(self):
        """Intermediate clip must use preset=ultrafast to avoid 600s timeouts on Fly.io."""
        import tempfile

        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        step = {
            "clip_id": "clip_a",
            "slot": {
                "text_overlays": [
                    {
                        "role": "label",
                        "sample_text": "PERU",
                        "start_s": 0.0,
                        "end_s": 3.0,
                        "position": "bottom",
                    }
                ]
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0

        fake_png_configs = [{"png_path": "/tmp/fake_overlay.png", "start_s": 0.0, "end_s": 3.0}]

        with (
            patch(
                "app.tasks.template_orchestrate.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
            patch(
                "app.pipeline.text_overlay.generate_text_overlay_png",
                return_value=fake_png_configs,
            ),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                _pre_burn_curtain_slot_text(
                    "/tmp/clip.mp4",
                    step,
                    slot_dur=5.0,
                    clip_metas=[_make_clip_meta("clip_a")],
                    subject="PERU",
                    slot_idx=0,
                    tmpdir=tmpdir,
                )

        assert mock_run.called, "subprocess.run was not called — no overlays processed?"
        cmd = mock_run.call_args[0][0]
        assert "-preset" in cmd, f"No -preset flag in cmd: {cmd}"
        preset_idx = cmd.index("-preset")
        assert cmd[preset_idx + 1] == "ultrafast", (
            f"Expected ultrafast preset for intermediate, got: {cmd[preset_idx + 1]}"
        )


class TestConcatDemuxerPreset:
    """_concat_demuxer uses preset=ultrafast for shared-CPU iteration speed."""

    def test_concat_uses_ultrafast_preset(self):
        """Final concat must use preset=ultrafast to fit Fly.io's 600s ffmpeg
        timeout on 24-slot recipes. CRF 18 and the 4M bitrate cap keep quality
        in the visually-lossless band; the scenecut/I-frame benefit of
        preset=fast was costing 9+ minutes per render — not worth it for
        marginal seam quality.
        """
        import tempfile

        from app.tasks.template_orchestrate import _concat_demuxer

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "app.tasks.template_orchestrate.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                _concat_demuxer(
                    ["/tmp/slot_0.mp4", "/tmp/slot_1.mp4"],
                    "/tmp/joined.mp4",
                    tmpdir,
                )

        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert "-preset" in cmd, f"No -preset flag in cmd: {cmd}"
        preset_idx = cmd.index("-preset")
        assert cmd[preset_idx + 1] == "ultrafast", (
            f"concat must use preset=ultrafast, got: {cmd[preset_idx + 1]}"
        )


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


# ── Regression anchors — fail on the old broken code, pass after fix ──────────


class TestBug1AspectRatioRegression:
    """[REGRESSION ANCHOR] Bug #1: aspect_ratio was hardcoded "9:16"."""

    def test_landscape_clip_gets_native_ar_not_9_16(self, tmp_path):
        """probe returns '16:9' → reframe_and_export must receive '16:9', not '9:16'."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            mock_reframe.assert_called_once()
            assert mock_reframe.call_args.kwargs["aspect_ratio"] == "16:9"


class TestBug2TimingRegression:
    """[REGRESSION ANCHOR] Bug #2: end_s used full moment duration instead of slot target."""

    def test_output_duration_clamped_to_slot_target(self, tmp_path):
        """slot target=3s, moment end_s=9s → end_s must be clamped to 3.0, not 9.0."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 9.0}  # 9s moment
        step.slot = {"position": 1, "target_duration_s": 3.0}  # 3s slot

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            kwargs = mock_reframe.call_args.kwargs
            assert kwargs["end_s"] == 3.0, f"Expected end_s=3.0, got {kwargs['end_s']}"
            assert kwargs["end_s"] - kwargs["start_s"] == 3.0


# ── _assemble_clips timing ────────────────────────────────────────────────────


class TestAssembleClipsTiming:
    def _make_step(
        self, clip_id: str, start_s: float,
        end_s: float, target_dur: float,
    ) -> MagicMock:
        step = MagicMock()
        step.clip_id = clip_id
        step.moment = {"start_s": start_s, "end_s": end_s}
        step.slot = {"position": 1, "target_duration_s": target_dur}
        return step

    def test_slot_trimmed_to_target_duration(self, tmp_path):
        """target_duration_s enforced — moment longer than slot is trimmed."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")
        step = self._make_step("clip_a", start_s=2.0, end_s=10.0, target_dur=4.0)

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            kwargs = mock_reframe.call_args.kwargs
            # start=2.0, target=4.0 → end must be min(10.0, 2.0+4.0) = 6.0
            assert kwargs["start_s"] == 2.0
            assert kwargs["end_s"] == 6.0

    def test_zero_target_duration_guard(self, tmp_path):
        """target_duration_s=0.0 must produce at least 0.5s clip."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")
        step = self._make_step("clip_a", start_s=0.0, end_s=5.0, target_dur=0.0)

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            kwargs = mock_reframe.call_args.kwargs
            assert kwargs["end_s"] - kwargs["start_s"] >= 0.5

    def test_end_s_uses_slot_target_not_moment_end(self, tmp_path):
        """Slot target controls duration — moment.end_s is ignored.

        This prevents FFmpeg crashes when Gemini returns a very short moment
        (e.g. 0.03s) for a 1.0s slot. The clip has plenty of footage, so
        we play start_s + target_duration_s worth of it.
        """
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")
        probe = VideoProbe(
            duration_s=30.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = self._make_step("clip_a", start_s=0.0, end_s=2.0, target_dur=5.0)

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            kwargs = mock_reframe.call_args.kwargs
            # end_s = min(0.0 + 5.0, 30.0) = 5.0 — slot target, not moment.end_s
            assert kwargs["end_s"] == 5.0


# ── _assemble_clips aspect ratio ──────────────────────────────────────────────


class TestAssembleClipsAspectRatio:
    def test_aspect_ratio_from_probe_used(self, tmp_path):
        """probe.aspect_ratio flows through to reframe_and_export correctly."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=5.0, fps=30.0, width=1080, height=1920,
            has_audio=True, codec="h264", aspect_ratio="9:16", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            assert mock_reframe.call_args.kwargs["aspect_ratio"] == "9:16"

    def test_probe_failure_falls_back_to_16_9(self, tmp_path):
        """Missing probe entry → reframe_and_export gets '16:9' fallback."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={},  # no entry for this path
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            assert mock_reframe.call_args.kwargs["aspect_ratio"] == "16:9"

    def test_probe_clips_happy_path(self, tmp_path):
        """_probe_clips returns one VideoProbe per path."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _probe_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        fake_probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )

        with patch("app.pipeline.probe.probe_video", return_value=fake_probe):
            result = _probe_clips([str(clip_file)])

        assert str(clip_file) in result
        assert result[str(clip_file)].aspect_ratio == "16:9"


# ── Template audio ────────────────────────────────────────────────────────────


class TestAssembleClipsTimeCursor:
    """Time-cursor prevents same footage repeating when a clip fills multiple slots."""

    def test_same_clip_uses_different_start_times(self, tmp_path):
        """Clip used 3× for 1s slots → three different start_s values."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=30.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        steps = []
        for pos in range(3):
            step = MagicMock()
            step.clip_id = "clip_a"
            step.moment = {"start_s": 5.0, "end_s": 8.0}  # same moment each time
            step.slot = {"position": pos + 1, "target_duration_s": 1.0}
            steps.append(step)

        def fake_reframe(**kwargs):
            # Create the slot file so concat/copy can find it
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"\x00" * 64)

        with (
            patch(
                "app.pipeline.reframe.reframe_and_export",
                side_effect=fake_reframe,
            ) as mock_reframe,
            patch("app.tasks.template_orchestrate.subprocess.run") as mock_ffmpeg,
        ):
            def fake_ffmpeg(cmd, **kw):
                # Create whatever output file the command targets (-y <path>)
                if "-y" in cmd:
                    idx = cmd.index("-y") + 1
                    if idx < len(cmd):
                        with open(cmd[idx], "wb") as f:
                            f.write(b"\x00" * 64)
                return MagicMock(returncode=0)

            mock_ffmpeg.side_effect = fake_ffmpeg
            _assemble_clips(
                steps=steps,
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )

            assert mock_reframe.call_count == 3
            start_times = [c.kwargs["start_s"] for c in mock_reframe.call_args_list]
            # All three uses must start at different times
            assert len(set(start_times)) == 3, (
                f"Expected 3 different start_s, got {start_times}"
            )

    def test_cursor_clamps_when_clip_exhausted(self, tmp_path):
        """Cursor clamps to end of clip when exhausted — never wraps to 0.0."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=3.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        steps = []
        for pos in range(5):
            step = MagicMock()
            step.clip_id = "clip_a"
            step.moment = {"start_s": 0.0, "end_s": 3.0}
            step.slot = {"position": pos + 1, "target_duration_s": 1.0}
            steps.append(step)

        def fake_reframe(**kwargs):
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"\x00" * 64)

        with (
            patch(
                "app.pipeline.reframe.reframe_and_export",
                side_effect=fake_reframe,
            ) as mock_reframe,
            patch("app.tasks.template_orchestrate.subprocess.run") as mock_ffmpeg,
        ):
            def fake_ffmpeg(cmd, **kw):
                if "-y" in cmd:
                    idx = cmd.index("-y") + 1
                    if idx < len(cmd):
                        with open(cmd[idx], "wb") as f:
                            f.write(b"\x00" * 64)
                return MagicMock(returncode=0)

            mock_ffmpeg.side_effect = fake_ffmpeg
            _assemble_clips(
                steps=steps,
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )

            start_times = [c.kwargs["start_s"] for c in mock_reframe.call_args_list]
            # 3s clip, 1s slots → 0.0, 1.0, 2.0, then clamps to 2.0 (end of clip)
            assert start_times == [0.0, 1.0, 2.0, 2.0, 2.0], (
                f"Expected cursor clamp (no wrap), got {start_times}"
            )


class TestTemplateAudio:
    def test_audio_extract_failure_nonfatal(self, tmp_path):
        """FFmpeg non-zero exit → returns False, does not raise."""
        from app.tasks.template_orchestrate import _extract_template_audio

        failed_proc = MagicMock()
        failed_proc.returncode = 1
        failed_proc.stderr = b"error: no audio stream"

        with patch("app.tasks.template_orchestrate.subprocess.run", return_value=failed_proc):
            result = _extract_template_audio("/tmp/template.mp4", str(tmp_path / "audio.m4a"))

        assert result is False

    def test_audio_extract_small_file_returns_false(self, tmp_path):
        """Output file < 1000 bytes → returns False (silent/corrupt audio)."""
        from app.tasks.template_orchestrate import _extract_template_audio

        tiny_file = tmp_path / "audio.m4a"
        tiny_file.write_bytes(b"x" * 100)  # only 100 bytes

        ok_proc = MagicMock()
        ok_proc.returncode = 0

        with patch("app.tasks.template_orchestrate.subprocess.run", return_value=ok_proc):
            result = _extract_template_audio("/tmp/template.mp4", str(tiny_file))

        assert result is False

    def test_mix_audio_happy_path(self, tmp_path):
        """FFmpeg succeeds → output_path written, not a shutil.copy2 fallback."""
        from app.tasks.template_orchestrate import _mix_template_audio

        ok_proc = MagicMock()
        ok_proc.returncode = 0

        with (
            patch("app.tasks.template_orchestrate.download_to_file"),
            patch("app.tasks.template_orchestrate.subprocess.run", return_value=ok_proc),
            patch("app.tasks.template_orchestrate.shutil.copy2") as mock_copy,
        ):
            _mix_template_audio(
                video_path="/tmp/assembled.mp4",
                audio_gcs_path="templates/t1/audio.m4a",
                output_path=str(tmp_path / "final.mp4"),
                tmpdir=str(tmp_path),
            )

        mock_copy.assert_not_called()

    def test_mix_audio_download_failure_fallback(self, tmp_path):
        """download_to_file raises → shutil.copy2 used, no exception raised."""
        from app.tasks.template_orchestrate import _mix_template_audio

        with (
            patch(
                "app.tasks.template_orchestrate.download_to_file",
                side_effect=Exception("GCS unavailable"),
            ),
            patch("app.tasks.template_orchestrate.shutil.copy2") as mock_copy,
        ):
            _mix_template_audio(
                video_path="/tmp/assembled.mp4",
                audio_gcs_path="templates/t1/audio.m4a",
                output_path=str(tmp_path / "final.mp4"),
                tmpdir=str(tmp_path),
            )

        mock_copy.assert_called_once_with("/tmp/assembled.mp4", str(tmp_path / "final.mp4"))

    def test_run_template_job_uses_final_path_when_audio_available(self):
        """With audio_gcs_path set: _mix_template_audio called and final.mp4 uploaded."""
        from app.tasks.template_orchestrate import _run_template_job

        mock_job = MagicMock()
        mock_job.status = "queued"
        mock_job.template_id = "t1"
        mock_job.all_candidates = {"clip_paths": ["gs://bucket/clip_0.mp4"]}
        mock_job.selected_platforms = ["tiktok"]

        mock_template = MagicMock()
        mock_template.analysis_status = "ready"
        mock_template.recipe_cached = {
            "shot_count": 1,
            "total_duration_s": 5.0,
            "hook_duration_s": 3.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 5.0,
                    "priority": 5,
                    "slot_type": "hook",
                },
            ],
            "copy_tone": "casual",
            "caption_style": "bold",
        }
        mock_template.audio_gcs_path = "templates/t1/audio.m4a"

        def _mock_ctx():
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=session)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        session = MagicMock()
        session.get.side_effect = lambda model, pk: (
            mock_template if model is VideoTemplate else mock_job
        )

        _orch = "app.tasks.template_orchestrate"
        with (
            patch(f"{_orch}._sync_session", side_effect=_mock_ctx),
            patch(
                f"{_orch}._download_clips_parallel",
                return_value=["/tmp/clip_0.mp4"],
            ),
            patch(f"{_orch}._probe_clips", return_value={}),
            patch(
                f"{_orch}._upload_clips_parallel",
                return_value=[MagicMock(name="clip_0")],
            ),
            patch(
                f"{_orch}._analyze_clips_parallel",
                return_value=([_make_clip_meta()], 0),
            ),
            patch("app.tasks.template_orchestrate.match") as mock_match,
            patch("app.tasks.template_orchestrate._assemble_clips"),
            patch("app.tasks.template_orchestrate._mix_template_audio") as mock_mix,
            patch("app.tasks.template_orchestrate._extract_hook_text", return_value=""),
            patch("app.tasks.template_orchestrate._extract_transcript", return_value=""),
            patch("app.tasks.template_orchestrate.upload_public_read", return_value="https://cdn/out.mp4"),
        ):
            from app.pipeline.agents.gemini_analyzer import AssemblyPlan, AssemblyStep
            step = AssemblyStep(
                slot={"position": 1, "target_duration_s": 5.0, "priority": 5, "slot_type": "hook"},
                clip_id="clip_0",
                moment={"start_s": 0.0, "end_s": 5.0, "energy": 7.0},
            )
            mock_match.return_value = AssemblyPlan(steps=[step])

            mock_platform_copy = MagicMock()
            mock_platform_copy.model_dump.return_value = {}
            with patch("app.pipeline.agents.copy_writer.generate_copy") as mock_copy:
                mock_copy.return_value = (mock_platform_copy, "generated")
                _run_template_job("12345678-1234-5678-1234-567812345678")

        # Called twice: once for the final video, once for the base (editor preview)
        assert mock_mix.call_count == 2


# ── template_matcher two-pass tolerance ───────────────────────────────────────


class TestTemplateMatcher2Pass:
    def test_tight_match_preferred_over_loose_in_greedy(self):
        """Tight candidate (±2s) preferred over loose-only (±2–6s) in greedy pass.

        Uses 2 slots so coverage assigns one clip per slot, leaving the greedy
        pass to fill the remaining slot where tight preference takes effect.
        """
        from app.pipeline.agents.gemini_analyzer import ClipMeta
        from app.pipeline.template_matcher import DURATION_TOLERANCE_PRIMARY_S, match

        def _clip(clip_id: str, moment_dur: float, energy: float) -> ClipMeta:
            return ClipMeta(
                clip_id=clip_id,
                transcript="",
                hook_text="",
                hook_score=5.0,
                best_moments=[{
                    "start_s": 0.0,
                    "end_s": moment_dur,
                    "energy": energy,
                    "description": "test",
                }],
            )

        target = 5.0
        # clip_a: moment=9s — within ±6s fallback but outside ±2s tight
        # clip_b: moment=5s — within ±2s tight
        # clip_c: moment=5s — tight match, used to ensure coverage + greedy both run
        clip_a = _clip("clip_a", moment_dur=9.0, energy=9.0)   # loose-only, high energy
        clip_b = _clip("clip_b", moment_dur=5.0, energy=7.0)   # tight match, lower energy
        clip_c = _clip("clip_c", moment_dur=5.0, energy=6.0)   # tight match, filler

        from app.pipeline.agents.gemini_analyzer import TemplateRecipe
        recipe = TemplateRecipe(
            shot_count=2,
            total_duration_s=target * 2,
            hook_duration_s=3.0,
            slots=[
                {"position": 1, "target_duration_s": target, "priority": 10, "slot_type": "hook"},
                {"position": 2, "target_duration_s": target, "priority": 5, "slot_type": "broll"},
            ],
            copy_tone="casual",
            caption_style="bold",
        )

        plan = match(recipe, [clip_a, clip_b, clip_c])

        # All 3 clips should be used (coverage pass), and tight candidates
        # (clip_b, clip_c) should be preferred in greedy scoring
        clip_ids = {step.clip_id for step in plan.steps}
        assert len(clip_ids) >= 2
        assert DURATION_TOLERANCE_PRIMARY_S == 2.0  # guard constant value


class TestOrchestrateTemplateJobErrors:
    def test_template_mismatch_error_classifies_as_user_clip_unusable(self):
        """A TemplateMismatchError reaching the outer handler must be classified
        as user_clip_unusable (not unknown). Defense-in-depth path: even if the
        inner _run_template_job lets one slip past the inner _StageError wrap.
        """
        from app.pipeline.template_matcher import TemplateMismatchError
        from app.tasks.template_orchestrate import orchestrate_template_job

        mock_job = MagicMock()
        mock_job.status = "queued"
        mock_job.error_detail = None
        mock_job.failure_reason = None

        def _mock_ctx():
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=_mock_session)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        _mock_session = MagicMock()
        _mock_session.get.return_value = mock_job

        with patch("app.tasks.template_orchestrate._sync_session", side_effect=_mock_ctx), \
             patch("app.tasks.template_orchestrate._run_template_job") as mock_run:
            mock_run.side_effect = TemplateMismatchError(
                "No clip fits slot 2 requiring ~5.0s.",
                code="TEMPLATE_CLIP_DURATION_MISMATCH",
            )
            orchestrate_template_job("12345678-1234-5678-1234-567812345678")

        assert mock_job.status == "processing_failed"
        assert mock_job.failure_reason == "user_clip_unusable"
        assert "TEMPLATE_CLIP_DURATION_MISMATCH" in mock_job.error_detail

    def test_inner_stage_error_user_clip_unusable_propagates(self):
        """_StageError(user_clip_unusable, ...) raised inside _run_template_job
        must reach the DB with failure_reason='user_clip_unusable'. Covers the
        normal path where the inner `except TemplateMismatchError` arm wraps
        the matcher error before the outer handler ever sees it.
        """
        from app.tasks.template_orchestrate import (
            FAILURE_REASON_USER_CLIP_UNUSABLE,
            _StageError,
            orchestrate_template_job,
        )

        mock_job = MagicMock()
        mock_job.status = "queued"
        mock_job.error_detail = None
        mock_job.failure_reason = None

        def _mock_ctx():
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=_mock_session)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        _mock_session = MagicMock()
        _mock_session.get.return_value = mock_job

        with patch("app.tasks.template_orchestrate._sync_session", side_effect=_mock_ctx), \
             patch("app.tasks.template_orchestrate._run_template_job") as mock_run:
            mock_run.side_effect = _StageError(
                FAILURE_REASON_USER_CLIP_UNUSABLE,
                "TEMPLATE_CLIP_DURATION_MISMATCH: No clip fits slot 2",
            )
            orchestrate_template_job("12345678-1234-5678-1234-567812345678")

        assert mock_job.status == "processing_failed"
        assert mock_job.failure_reason == "user_clip_unusable"
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


# ── Beat detection tests ─────────────────────────────────────────────────────


class TestSnapToBeat:
    def test_snap_within_tolerance(self):
        """Target near a beat (within 0.4s) → snaps to beat."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [4.8, 10.0])
        assert result == 4.8

    def test_snap_outside_tolerance(self):
        """Target far from any beat (>0.4s) → returns target unchanged."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [3.0, 8.0])
        assert result == 5.0

    def test_snap_empty_beats(self):
        """No beats → returns target unchanged."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [])
        assert result == 5.0

    def test_snap_exact_match(self):
        """Target equals a beat → returns that beat."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [3.0, 5.0, 8.0])
        assert result == 5.0

    def test_snap_two_beats_equidistant(self):
        """Two beats equally close → picks one (deterministic)."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [4.8, 5.2])
        assert result in (4.8, 5.2)

    def test_snap_prefers_closer_beat(self):
        """When two beats are within tolerance, closer one wins."""
        from app.tasks.template_orchestrate import _snap_to_beat

        result = _snap_to_beat(5.0, [4.9, 5.3])
        assert result == 4.9


class TestDetectAudioBeats:
    def test_happy_path_parses_silence_end(self):
        """FFmpeg stderr with silence_end markers → sorted timestamps."""
        from app.tasks.template_orchestrate import _detect_audio_beats

        fake_stderr = (
            b"[silencedetect @ 0x1234] silence_end: 1.500 | silence_duration: 0.300\n"
            b"[silencedetect @ 0x1234] silence_end: 3.200 | silence_duration: 0.150\n"
            b"[silencedetect @ 0x1234] silence_end: 5.800 | silence_duration: 0.200\n"
        )
        mock_result = MagicMock(returncode=0, stderr=fake_stderr)

        with patch("app.tasks.template_orchestrate.subprocess.run", return_value=mock_result):
            beats = _detect_audio_beats("/tmp/audio.m4a")

        assert beats == [1.5, 3.2, 5.8]

    def test_ffmpeg_failure_returns_empty(self):
        """FFmpeg non-zero exit → returns [], does not raise."""
        from app.tasks.template_orchestrate import _detect_audio_beats

        mock_result = MagicMock(returncode=1, stderr=b"error")

        with patch("app.tasks.template_orchestrate.subprocess.run", return_value=mock_result):
            beats = _detect_audio_beats("/tmp/audio.m4a")

        assert beats == []

    def test_silent_audio_returns_empty(self):
        """FFmpeg succeeds but no silence_end markers → returns []."""
        from app.tasks.template_orchestrate import _detect_audio_beats

        mock_result = MagicMock(returncode=0, stderr=b"size=   0kB time=00:00:30\n")

        with patch("app.tasks.template_orchestrate.subprocess.run", return_value=mock_result):
            beats = _detect_audio_beats("/tmp/audio.m4a")

        assert beats == []

    def test_subprocess_exception_returns_empty(self):
        """subprocess.run raises → returns [], does not propagate."""
        from app.tasks.template_orchestrate import _detect_audio_beats

        with patch(
            "app.tasks.template_orchestrate.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 30),
        ):
            beats = _detect_audio_beats("/tmp/audio.m4a")

        assert beats == []


class TestMergeBeatSources:
    def test_both_sources_merged_and_deduped(self):
        """Gemini and FFmpeg beats combined, near-duplicates removed."""
        from app.tasks.template_orchestrate import _merge_beat_sources

        gemini = [1.5, 3.2, 5.0]
        ffmpeg = [1.48, 3.5, 7.0]  # 1.48 is near 1.5 (within 0.15s threshold)

        result = _merge_beat_sources(gemini, ffmpeg)

        # 1.48 kept (FFmpeg), 1.5 dropped (too close). 3.2, 3.5, 5.0, 7.0 all kept.
        assert 1.48 in result
        assert 1.5 not in result
        assert 3.2 in result
        assert 3.5 in result
        assert 5.0 in result
        assert 7.0 in result

    def test_both_empty_returns_empty(self):
        from app.tasks.template_orchestrate import _merge_beat_sources

        assert _merge_beat_sources([], []) == []

    def test_one_source_empty_returns_other(self):
        from app.tasks.template_orchestrate import _merge_beat_sources

        assert _merge_beat_sources([], [1.0, 2.0]) == [1.0, 2.0]
        assert _merge_beat_sources([1.0, 2.0], []) == [1.0, 2.0]

    def test_result_is_sorted(self):
        from app.tasks.template_orchestrate import _merge_beat_sources

        result = _merge_beat_sources([5.0, 1.0], [3.0, 7.0])
        assert result == sorted(result)


class TestAssembleClipsBeatSnap:
    """Beat-snap integration in _assemble_clips."""

    def test_beat_snap_adjusts_slot_duration(self, tmp_path):
        """With beats, slot end_s is adjusted to align with nearest beat."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=30.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        # Beat at 4.8s — within 0.4s tolerance of slot end (0+5=5.0)
        beats = [4.8, 10.0, 15.0]

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                beat_timestamps_s=beats,
            )
            kwargs = mock_reframe.call_args.kwargs
            # Slot snapped from 5.0 to 4.8 → end_s = 0.0 + 4.8 = 4.8
            assert kwargs["end_s"] == pytest.approx(4.8, abs=0.01)

    def test_no_beats_unchanged(self, tmp_path):
        """Empty beats → same behavior as before (regression guard)."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=30.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                beat_timestamps_s=[],
            )
            kwargs = mock_reframe.call_args.kwargs
            assert kwargs["end_s"] == 5.0

    def test_none_beats_unchanged(self, tmp_path):
        """None beats (backward compat) → same behavior as before."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=30.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {"position": 1, "target_duration_s": 5.0}

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                # beat_timestamps_s not passed → defaults to None
            )
            kwargs = mock_reframe.call_args.kwargs
            assert kwargs["end_s"] == 5.0


class TestTemplateRecipeBackwardCompat:
    def test_old_recipe_without_beats_loads(self):
        """Cached recipe dict without beat_timestamps_s → TemplateRecipe works."""
        old_recipe_dict = {
            "shot_count": 3,
            "total_duration_s": 15.0,
            "hook_duration_s": 3.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 5.0,
                    "priority": 5,
                    "slot_type": "hook",
                },
            ],
            "copy_tone": "casual",
            "caption_style": "bold",
        }

        recipe = TemplateRecipe(**old_recipe_dict)

        assert recipe.beat_timestamps_s == []
        assert recipe.shot_count == 3


# ── Text overlay integration ─────────────────────────────────────────────────


class TestAssembleClipsTextOverlays:
    """Tests for post-join text overlay collection and dedup in _assemble_clips."""

    def _make_step_with_overlays(
        self, clip_id: str = "clip_a", overlays: list | None = None,
    ) -> MagicMock:
        step = MagicMock()
        step.clip_id = clip_id
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {
            "position": 1,
            "target_duration_s": 5.0,
            "text_overlays": overlays or [],
        }
        return step

    def test_overlays_collected_and_burned_post_join(self, tmp_path):
        """Text overlays are collected post-join and burned via _burn_text_overlays."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )

        overlays = [{
            "role": "hook",
            "start_s": 0.5,
            "end_s": 2.5,
            "position": "center",
            "effect": "pop-in",
            "sample_text": "WOW",
        }]
        step = self._make_step_with_overlays(overlays=overlays)
        meta = _make_clip_meta(clip_id="clip_a")

        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate._burn_text_overlays") as mock_burn,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                clip_metas=[meta],
            )
            # Per-slot reframe should NOT get text_overlay_pngs (post-join now)
            kwargs = mock_reframe.call_args.kwargs
            assert "text_overlay_pngs" not in kwargs
            # Post-join burn should be called with collected overlays
            mock_burn.assert_called_once()

    def test_no_overlays_skips_burn(self, tmp_path):
        """No text overlays → _burn_text_overlays not called, copy2 used instead."""
        from app.pipeline.probe import VideoProbe
        from app.tasks.template_orchestrate import _assemble_clips

        clip_file = tmp_path / "clip_0.mp4"
        clip_file.write_bytes(b"fake")

        probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )

        step = self._make_step_with_overlays(overlays=[])

        with (
            patch("app.pipeline.reframe.reframe_and_export"),
            patch("app.tasks.template_orchestrate._burn_text_overlays") as mock_burn,
            patch("app.tasks.template_orchestrate.shutil.copy2") as mock_copy,
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={"clip_a": str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
            )
            mock_burn.assert_not_called()
            mock_copy.assert_called()

    def test_cta_overlay_skipped_in_collection(self):
        """CTA role resolves to empty string → excluded from collected overlays."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step_with_overlays(overlays=[{
            "role": "cta",
            "start_s": 0.5,
            "end_s": 2.5,
            "position": "center",
            "sample_text": "",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert result == []

    def test_curtain_close_slots_skipped_in_collect(self):
        """Curtain-close slot overlays are pre-burned, so skipped in _collect."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step_with_overlays(overlays=[{
            "role": "label",
            "start_s": 0.0,
            "end_s": 5.0,
            "position": "center",
            "effect": "font-cycle",
            "sample_text": "PERU",
        }])

        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.5, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [5.0], None, "Peru",
            interstitial_map=interstitial_map,
        )
        # Curtain-close slot overlays are pre-burned onto slot clip,
        # so _collect_absolute_overlays skips them entirely
        assert len(result) == 0

    def test_subject_label_gets_accel_without_curtain(self):
        """Subject labels always get accel_at_s=8.0 from _LABEL_CONFIG, even without curtain."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step_with_overlays(overlays=[{
            "role": "label",
            "start_s": 0.0,
            "end_s": 5.0,
            "position": "center",
            "effect": "font-cycle",
            "sample_text": "TOKYO",
        }])

        result = _collect_absolute_overlays([step], [5.0], None, "Tokyo")
        assert len(result) == 1
        # Subject label gets accel_at=8.0 from config (no curtain to override)
        assert result[0].get("font_cycle_accel_at_s") == 8.0

    def test_no_accel_for_prefix_without_curtain(self):
        """Non-subject font-cycle labels without curtain don't get accel timestamp."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step_with_overlays(overlays=[{
            "role": "hook",
            "start_s": 0.0,
            "end_s": 5.0,
            "position": "center",
            "effect": "font-cycle",
            "sample_text": "Check this out",
        }])

        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert "font_cycle_accel_at_s" not in result[0]

    def test_curtain_close_static_overlay_also_skipped(self):
        """All curtain-close slot overlays are skipped (pre-burned), including static."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step_with_overlays(overlays=[{
            "role": "label",
            "start_s": 0.0,
            "end_s": 5.0,
            "position": "center",
            "effect": "none",
            "sample_text": "Welcome to",
        }])

        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.5, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [5.0], None, "",
            interstitial_map=interstitial_map,
        )
        # Pre-burned, so skipped
        assert len(result) == 0


class TestCrossSlotMerge:
    """Tests for cross-slot same-text overlay merging (replaces old drop-duplicate logic)."""

    def _make_step_with_overlays(
        self, clip_id: str = "clip_a", overlays: list | None = None,
        position: int = 1,
    ) -> MagicMock:
        step = MagicMock()
        step.clip_id = clip_id
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {
            "position": position,
            "target_duration_s": 5.0,
            "text_overlays": overlays or [],
        }
        return step

    def test_cross_slot_same_text_merged(self):
        """Same text on adjacent non-curtain slots produces one merged overlay."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        # Use non-curtain slots so overlays are collected normally
        step1 = self._make_step_with_overlays(
            position=1, overlays=[{
                "role": "label", "start_s": 0.0, "end_s": 5.0,
                "position": "center", "effect": "none",
                "sample_text": "PERU",
            }],
        )
        step2 = self._make_step_with_overlays(
            clip_id="clip_b", position=2, overlays=[{
                "role": "label", "start_s": 0.0, "end_s": 5.0,
                "position": "center", "effect": "none",
                "sample_text": "PERU",
            }],
        )

        # No curtain-close → both slots' overlays collected
        result = _collect_absolute_overlays(
            [step1, step2], [5.0, 5.0], None, "Peru",
            interstitial_map={},
        )
        # Should be merged into one overlay
        peru_overlays = [o for o in result if o["text"].lower() == "peru"]
        assert len(peru_overlays) == 1

    def test_cross_slot_merge_inherits_effect(self):
        """Merged overlay gets effect from the later slot."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step1 = self._make_step_with_overlays(
            position=1, overlays=[{
                "role": "label", "start_s": 0.0, "end_s": 5.0,
                "position": "center", "effect": "none",
                "sample_text": "PERU",
            }],
        )
        step2 = self._make_step_with_overlays(
            clip_id="clip_b", position=2, overlays=[{
                "role": "label", "start_s": 0.0, "end_s": 5.0,
                "position": "center", "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        )

        # No curtain-close → both slots collected
        result = _collect_absolute_overlays(
            [step1, step2], [5.0, 5.0], None, "Peru",
            interstitial_map={},
        )
        peru_overlays = [o for o in result if o["text"].lower() == "peru"]
        assert len(peru_overlays) == 1
        # Should have font-cycle effect from the second slot
        assert peru_overlays[0]["effect"] == "font-cycle"

    def test_non_adjacent_same_text_not_merged(self):
        """Same text with large gap (>2s) stays separate."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        # Use hook role (not label) to avoid _LABEL_CONFIG timing overrides
        step1 = self._make_step_with_overlays(
            position=1, overlays=[{
                "role": "hook", "start_s": 0.0, "end_s": 2.0,
                "position": "center", "effect": "none",
                "sample_text": "Check this",
            }],
        )
        # Second overlay starts 5s into a 10s slot = 8s gap from first overlay's end
        step2 = self._make_step_with_overlays(
            clip_id="clip_b", position=2, overlays=[{
                "role": "hook", "start_s": 5.0, "end_s": 10.0,
                "position": "center", "effect": "none",
                "sample_text": "Check this",
            }],
        )

        result = _collect_absolute_overlays(
            [step1, step2], [5.0, 10.0], None, "",
        )
        matching = [o for o in result if o["text"].lower() == "check this"]
        assert len(matching) == 2, "Non-adjacent same text should stay separate"

    def test_different_position_same_text_not_merged(self):
        """Same text at different positions stays separate."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step1 = self._make_step_with_overlays(
            position=1, overlays=[
                {
                    "role": "label", "start_s": 0.0, "end_s": 5.0,
                    "position": "top", "effect": "none",
                    "sample_text": "PERU",
                },
                {
                    "role": "label", "start_s": 0.0, "end_s": 5.0,
                    "position": "bottom", "effect": "none",
                    "sample_text": "PERU",
                },
            ],
        )

        result = _collect_absolute_overlays(
            [step1], [5.0], None, "Peru",
        )
        peru_overlays = [o for o in result if o["text"].lower() == "peru"]
        assert len(peru_overlays) == 2, "Same text at different positions should stay separate"
        positions = {o["position"] for o in peru_overlays}
        assert positions == {"top", "bottom"}


class TestResolveOverlayText:
    def test_hook_role_uses_hook_text(self):
        from app.tasks.template_orchestrate import _resolve_overlay_text
        meta = _make_clip_meta()
        result = _resolve_overlay_text("hook", meta, {})
        assert result == "test hook"

    def test_reaction_role_uses_sample_text(self):
        from app.tasks.template_orchestrate import _resolve_overlay_text
        result = _resolve_overlay_text(
            "reaction", None, {"sample_text": "OMG"},
        )
        assert result == "OMG"

    def test_label_role_uses_sample_text(self):
        from app.tasks.template_orchestrate import _resolve_overlay_text
        result = _resolve_overlay_text(
            "label", None, {"sample_text": "Day 1"},
        )
        assert result == "Day 1"

    def test_cta_role_returns_empty(self):
        from app.tasks.template_orchestrate import _resolve_overlay_text
        result = _resolve_overlay_text("cta", _make_clip_meta(), {})
        assert result == ""

    def test_hook_role_no_meta_returns_empty(self):
        from app.tasks.template_orchestrate import _resolve_overlay_text
        result = _resolve_overlay_text("hook", None, {})
        assert result == ""


# ── Timeout & error_detail tests ──────────────────────────────────────────────


class TestAnalyzeTemplateTimeout:
    def test_analyze_timeout_sets_failed_and_error_detail(self):
        """SoftTimeLimitExceeded → analysis_status='failed' + error_detail set."""
        from celery.exceptions import SoftTimeLimitExceeded

        from app.tasks.template_orchestrate import analyze_template_task

        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"
        mock_template.audio_gcs_path = None
        mock_template.error_detail = None

        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1

        with (
            patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx,
            patch("app.tasks.template_orchestrate.download_to_file"),
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload,
            patch("app.tasks.template_orchestrate.redis_lib") as mock_redis_mod,
        ):
            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_redis_mod.from_url.return_value = mock_redis
            mock_upload.side_effect = SoftTimeLimitExceeded()

            analyze_template_task("template-timeout")

        assert mock_template.analysis_status == "failed"
        assert "timed out" in mock_template.error_detail

    def test_analyze_failure_persists_error_detail(self):
        """Generic exception → error_detail = str(exc)[:1000]."""
        from app.tasks.template_orchestrate import analyze_template_task

        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"
        mock_template.audio_gcs_path = None
        mock_template.error_detail = None

        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1

        with (
            patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx,
            patch("app.tasks.template_orchestrate.download_to_file"),
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload,
            patch("app.tasks.template_orchestrate.redis_lib") as mock_redis_mod,
        ):
            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_redis_mod.from_url.return_value = mock_redis
            mock_upload.side_effect = Exception("API quota exceeded")

            analyze_template_task("template-err")

        assert mock_template.analysis_status == "failed"
        assert mock_template.error_detail == "API quota exceeded"

    def test_analyze_clears_stale_error_on_start(self):
        """Successful run clears a prior error_detail."""
        from app.tasks.template_orchestrate import analyze_template_task

        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"
        mock_template.audio_gcs_path = None
        mock_template.error_detail = "old error"

        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1

        mock_recipe = _make_recipe()

        with (
            patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx,
            patch("app.tasks.template_orchestrate.download_to_file"),
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload,
            patch("app.tasks.template_orchestrate.analyze_template") as mock_analyze,
            patch("app.tasks.template_orchestrate.redis_lib") as mock_redis_mod,
        ):
            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_redis_mod.from_url.return_value = mock_redis
            mock_upload.return_value = MagicMock()
            mock_analyze.return_value = mock_recipe

            analyze_template_task("template-clear")

        # error_detail was cleared on start (set to None)
        # The final status should be "ready"
        assert mock_template.analysis_status == "ready"
        # error_detail was set to None during the clearing phase
        # It should remain None since no error occurred
        assert mock_template.error_detail is None

    def test_analyze_bails_on_max_attempts(self):
        """Redis counter > 3 → early return with failed status, no Gemini calls."""
        from app.tasks.template_orchestrate import analyze_template_task

        mock_template = MagicMock()
        mock_template.gcs_path = "templates/test.mp4"

        mock_redis = MagicMock()
        mock_redis.incr.return_value = 4  # exceeds max of 3

        with (
            patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx,
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload,
            patch("app.tasks.template_orchestrate.redis_lib") as mock_redis_mod,
        ):
            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_template

            mock_redis_mod.from_url.return_value = mock_redis

            analyze_template_task("template-maxretry")

        assert mock_template.analysis_status == "failed"
        assert "max analysis attempts" in mock_template.error_detail.lower()
        # Gemini was never called
        mock_upload.assert_not_called()


class TestOrchestrateTemplateJobTimeout:
    def test_orchestrate_template_job_timeout(self):
        """SoftTimeLimitExceeded → job.status='processing_failed' + error_detail set."""
        from celery.exceptions import SoftTimeLimitExceeded

        from app.tasks.template_orchestrate import orchestrate_template_job

        mock_job = MagicMock()
        job_id = str(uuid.uuid4())

        with (
            patch("app.tasks.template_orchestrate._sync_session") as mock_session_ctx,
            patch(
                "app.tasks.template_orchestrate._run_template_job",
                side_effect=SoftTimeLimitExceeded(),
            ),
        ):
            session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)
            session.get.return_value = mock_job

            orchestrate_template_job(job_id)

        assert mock_job.status == "processing_failed"
        assert "timed out" in mock_job.error_detail


# ── Fine-tuning tests (timing overrides, role overrides, curtain clamp) ──────


class TestOverlayFineTuning:
    """Tests for Issue 1-5: timing overrides, role overrides, curtain sync, exit clamp."""

    def _make_step(
        self, overlays: list, position: int = 1, clip_id: str = "clip_a",
    ) -> MagicMock:
        step = MagicMock()
        step.clip_id = clip_id
        step.moment = {"start_s": 0.0, "end_s": 5.0}
        step.slot = {
            "position": position,
            "target_duration_s": 5.0,
            "text_overlays": overlays,
        }
        return step

    # ── Issue 1: Timing overrides ────────────────────────────────────────

    def test_timing_override_start_s(self):
        """start_s_override shifts overlay start from Gemini value."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.5, "end_s": 3.0,
            "start_s_override": 1.0,
            "position": "center", "effect": "pop-in", "sample_text": "WOW",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["start_s"] == 1.0  # overridden from 0.5

    def test_timing_override_end_s(self):
        """end_s_override shifts overlay end from Gemini value."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.0, "end_s": 5.0,
            "end_s_override": 3.5,
            "position": "center", "effect": "pop-in", "sample_text": "WOW",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["end_s"] == 3.5  # overridden from 5.0

    def test_timing_override_not_present(self):
        """Without overrides, Gemini values are used as-is."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.5, "end_s": 3.0,
            "position": "center", "effect": "pop-in", "sample_text": "WOW",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["start_s"] == 0.5
        assert result[0]["end_s"] == 3.0

    def test_negative_timing_override_clamped_to_zero(self):
        """Negative start_s_override is clamped to 0.0."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.5, "end_s": 3.0,
            "start_s_override": -1.0,
            "position": "center", "effect": "pop-in", "sample_text": "WOW",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["start_s"] == 0.0

    # ── Issues 2+3+4: Label config (subject vs prefix) ─────────────────

    def test_label_subject_preserves_recipe_styling(self):
        """Subject-placeholder label (PERU) preserves recipe styling (WYSIWYG)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "none",
            "sample_text": "PERU",
            "text_size": "medium", "font_style": "display", "text_color": "#FFFFFF",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "Peru")
        assert len(result) == 1
        # Recipe styling is preserved — no _LABEL_CONFIG override
        assert result[0]["text_size"] == "medium"
        assert result[0]["font_style"] == "display"
        assert result[0]["text_color"] == "#FFFFFF"

    def test_label_prefix_preserves_recipe_styling(self):
        """Non-subject label ('Welcome to') preserves recipe styling (WYSIWYG)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "none",
            "sample_text": "Welcome to",
            "text_size": "large", "font_style": "sans", "text_color": "#F4D03F",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["text_size"] == "large"

    def test_first_slot_prefix_timing(self):
        """First-slot prefix label starts at 2.0s (not Gemini's 0.0)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "none",
            "sample_text": "Welcome to",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["start_s"] == 2.0

    def test_first_slot_subject_timing(self):
        """First-slot subject label starts at 3.0s (not Gemini's 0.0)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "none",
            "sample_text": "PERU",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "Peru")
        assert len(result) == 1
        assert result[0]["start_s"] == 3.0

    def test_later_slot_timing_unchanged(self):
        """Labels on slots after the first (cumulative_s > 0) keep Gemini timing."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step1 = self._make_step([], position=1)
        step2 = self._make_step([{
            "role": "label", "start_s": 0.5, "end_s": 4.0,
            "position": "center", "effect": "none",
            "sample_text": "Welcome to",
        }], position=2)
        result = _collect_absolute_overlays(
            [step1, step2], [5.0, 5.0], None, "",
        )
        assert len(result) == 1
        # cumulative_s = 5.0 (after first slot), so start_s = 5.0 + 0.5 = 5.5
        assert result[0]["start_s"] == 5.5  # Gemini's 0.5 + cumulative 5.0

    def test_subject_label_preserves_recipe_effect(self):
        """Subject label preserves recipe effect (WYSIWYG, no forced font-cycle)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "none",
            "sample_text": "PERU",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "Peru")
        assert len(result) == 1
        assert result[0]["effect"] == "none"

    def test_subject_label_accel_at_8s(self):
        """Subject label gets font_cycle_accel_at_s=8.0 from config."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 10.0,
            "position": "center", "effect": "none",
            "sample_text": "PERU",
        }])
        step.slot["target_duration_s"] = 10.0
        result = _collect_absolute_overlays([step], [10.0], None, "Peru")
        assert len(result) == 1
        assert result[0].get("font_cycle_accel_at_s") == 8.0

    def test_curtain_slots_pre_burned_not_collected(self):
        """Curtain-close slots are pre-burned so _collect skips them entirely."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 10.0,
            "position": "center", "effect": "font-cycle",
            "sample_text": "PERU",
        }])
        step.slot["target_duration_s"] = 10.0
        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.0, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [10.0], None, "Peru",
            interstitial_map=interstitial_map,
        )
        # Pre-burned onto slot clip → skipped here
        assert len(result) == 0

    def test_non_label_hook_passthrough(self):
        """Hook role with non-label-like text keeps Gemini defaults."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.0, "end_s": 5.0,
            "position": "center", "effect": "pop-in",
            "sample_text": "discovering a hidden river",
            "text_size": "medium", "font_style": "display", "text_color": "#FFFFFF",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["text_size"] == "medium"
        assert result[0]["font_style"] == "display"
        assert result[0]["text_color"] == "#FFFFFF"

    # ── Issue 5: Text exit clamped on curtain-close ──────────────────────

    def test_curtain_slot_overlays_skipped(self):
        """Curtain-close slot overlays are pre-burned, so skipped in _collect."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.0, "end_s": 7.0,
            "position": "center", "effect": "none", "sample_text": "WOW",
        }])
        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.5, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [5.0], None, "",
            interstitial_map=interstitial_map,
        )
        assert len(result) == 0  # pre-burned, skipped

    def test_text_exit_not_clamped_no_curtain(self):
        """Without curtain-close, end_s is NOT clamped to slot end."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.0, "end_s": 7.0,
            "position": "center", "effect": "none", "sample_text": "WOW",
        }])
        result = _collect_absolute_overlays([step], [5.0], None, "")
        assert len(result) == 1
        assert result[0]["end_s"] == 7.0  # not clamped

    def test_curtain_slot_short_overlay_also_skipped(self):
        """Even short overlays on curtain-close slots are pre-burned, skipped."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "hook", "start_s": 0.0, "end_s": 4.0,
            "position": "center", "effect": "none", "sample_text": "WOW",
        }])
        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.5, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [5.0], None, "",
            interstitial_map=interstitial_map,
        )
        assert len(result) == 0  # pre-burned, skipped

    # ── Issue 4: non-curtain accel still works ─────────────────────────

    def test_accel_at_works_without_curtain(self):
        """Subject labels get default accel_at_s from _LABEL_CONFIG without curtain."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = self._make_step([{
            "role": "label", "start_s": 0.0, "end_s": 10.0,
            "position": "center", "effect": "font-cycle", "sample_text": "PERU",
        }])
        step.slot["target_duration_s"] = 10.0

        # No curtain-close → normal collection
        result = _collect_absolute_overlays(
            [step], [10.0], None, "Peru",
            interstitial_map={},
        )
        assert len(result) == 1
        assert result[0].get("font_cycle_accel_at_s") == 8.0


# ── Font-cycle end_s extension on curtain-close slots ────────────────────────


class TestPreBurnCurtainFontCycleEndS:
    """Font-cycle end_s must extend to slot_dur on curtain-close slots."""

    def test_font_cycle_end_s_extended_to_slot_dur(self, tmp_path):
        """Gemini end_s < slot_dur → pre-burn extends to slot_dur for font-cycle."""
        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        clip_file = tmp_path / "slot.mp4"
        clip_file.write_bytes(b"fake")

        step = MagicMock()
        step.clip_id = "clip_a"
        step.slot = {
            "position": 5,
            "target_duration_s": 7.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 5.0,  # Gemini says 5s, slot is 7s
                "position": "center",
                "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        }

        inter = {
            "type": "curtain-close",
            "hold_s": 0.0,
            "animate_s": 2.0,
            "hold_color": "#000000",
        }

        with patch(
            "app.pipeline.text_overlay.generate_text_overlay_png",
        ) as mock_gen:
            mock_gen.return_value = []  # no PNGs → returns original path

            _pre_burn_curtain_slot_text(
                str(clip_file), step, 7.0, None, "Peru", 4, str(tmp_path), inter,
            )

            # Verify the overlay passed to generate_text_overlay_png
            # has end_s extended to slot_dur (7.0), not Gemini's 5.0
            assert mock_gen.call_count == 1
            overlays_arg = mock_gen.call_args[0][0]
            assert len(overlays_arg) == 1
            assert overlays_arg[0]["end_s"] == 7.0, (
                f"Font-cycle end_s should be slot_dur (7.0), got {overlays_arg[0]['end_s']}"
            )

    def test_font_cycle_accel_at_set_correctly(self, tmp_path):
        """accel_at = slot_dur - animate_s, within overlay range."""
        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        clip_file = tmp_path / "slot.mp4"
        clip_file.write_bytes(b"fake")

        step = MagicMock()
        step.clip_id = "clip_a"
        step.slot = {
            "position": 5,
            "target_duration_s": 7.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 5.0,
                "position": "center",
                "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        }

        inter = {
            "type": "curtain-close",
            "hold_s": 0.0,
            "animate_s": 2.0,
            "hold_color": "#000000",
        }

        with patch(
            "app.pipeline.text_overlay.generate_text_overlay_png",
        ) as mock_gen:
            mock_gen.return_value = []

            _pre_burn_curtain_slot_text(
                str(clip_file), step, 7.0, None, "Peru", 4, str(tmp_path), inter,
            )

            overlays_arg = mock_gen.call_args[0][0]
            # accel_at = 7.0 - 2.0 = 5.0
            assert overlays_arg[0].get("font_cycle_accel_at_s") == 5.0

    def test_non_font_cycle_overlay_not_extended(self, tmp_path):
        """Non-font-cycle overlays keep their original end_s."""
        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        clip_file = tmp_path / "slot.mp4"
        clip_file.write_bytes(b"fake")

        step = MagicMock()
        step.clip_id = "clip_a"
        step.slot = {
            "position": 5,
            "target_duration_s": 7.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 3.0,
                "position": "top-center",
                "effect": "fade-in",
                "sample_text": "Welcome to",
            }],
        }

        inter = {
            "type": "curtain-close",
            "hold_s": 0.0,
            "animate_s": 2.0,
            "hold_color": "#000000",
        }

        with patch(
            "app.pipeline.text_overlay.generate_text_overlay_png",
        ) as mock_gen:
            mock_gen.return_value = []

            _pre_burn_curtain_slot_text(
                str(clip_file), step, 7.0, None, "Peru", 4, str(tmp_path), inter,
            )

            overlays_arg = mock_gen.call_args[0][0]
            # Non-font-cycle: end_s stays at original 3.0
            assert overlays_arg[0]["end_s"] == 3.0


# ── Interstitial hold_s=0 skip ───────────────────────────────────────────────


class TestInterstitialZeroHoldSkip:
    """hold_s=0 skips the colour-hold clip so curtain close aligns with beat."""

    def _make_step(self, clip_id, position, target_dur):
        step = MagicMock()
        step.clip_id = clip_id
        step.moment = {"start_s": 0.0, "end_s": target_dur + 2.0}
        step.slot = {"position": position, "target_duration_s": target_dur}
        return step

    def test_zero_hold_skips_insert_interstitial(self, tmp_path):
        """hold_s=0 → _insert_interstitial never called, no extra clip added."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_a = tmp_path / "clip_a.mp4"
        clip_b = tmp_path / "clip_b.mp4"
        clip_a.write_bytes(b"fake")
        clip_b.write_bytes(b"fake")

        steps = [
            self._make_step("clip_a", 1, 5.0),
            self._make_step("clip_b", 2, 5.0),
        ]

        interstitial_list = [
            {"type": "curtain-close", "after_slot": 1, "hold_s": 0.0,
             "animate_s": 2.0, "hold_color": "#000000"},
        ]

        def fake_reframe(**kwargs):
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"\x00" * 64)

        with (
            patch(
                "app.pipeline.reframe.reframe_and_export",
                side_effect=fake_reframe,
            ) as mock_reframe,
            patch(
                "app.tasks.template_orchestrate._insert_interstitial",
            ) as mock_insert,
            patch("app.tasks.template_orchestrate.subprocess.run") as mock_ffmpeg,
        ):
            def fake_ffmpeg(cmd, **kw):
                if "-y" in cmd:
                    idx = cmd.index("-y") + 1
                    if idx < len(cmd):
                        with open(cmd[idx], "wb") as f:
                            f.write(b"\x00" * 64)
                return MagicMock(returncode=0)

            mock_ffmpeg.side_effect = fake_ffmpeg

            _assemble_clips(
                steps=steps,
                clip_id_to_local={
                    "clip_a": str(clip_a),
                    "clip_b": str(clip_b),
                },
                clip_probe_map={},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                interstitials=interstitial_list,
            )

            # _insert_interstitial must NOT be called when hold_s=0
            mock_insert.assert_not_called()
            # Only 2 reframe calls (one per slot), no extra interstitial clip
            assert mock_reframe.call_count == 2

    def test_positive_hold_calls_insert_interstitial(self, tmp_path):
        """hold_s=1.0 → _insert_interstitial IS called."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_a = tmp_path / "clip_a.mp4"
        clip_b = tmp_path / "clip_b.mp4"
        clip_a.write_bytes(b"fake")
        clip_b.write_bytes(b"fake")

        steps = [
            self._make_step("clip_a", 1, 5.0),
            self._make_step("clip_b", 2, 5.0),
        ]

        interstitial_list = [
            {"type": "curtain-close", "after_slot": 1, "hold_s": 1.0,
             "animate_s": 2.0, "hold_color": "#000000"},
        ]

        def fake_reframe(**kwargs):
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"\x00" * 64)

        with (
            patch(
                "app.pipeline.reframe.reframe_and_export",
                side_effect=fake_reframe,
            ),
            patch(
                "app.tasks.template_orchestrate._insert_interstitial",
            ) as mock_insert,
            patch("app.tasks.template_orchestrate.subprocess.run") as mock_ffmpeg,
        ):
            def fake_ffmpeg(cmd, **kw):
                if "-y" in cmd:
                    idx = cmd.index("-y") + 1
                    if idx < len(cmd):
                        with open(cmd[idx], "wb") as f:
                            f.write(b"\x00" * 64)
                return MagicMock(returncode=0)

            mock_ffmpeg.side_effect = fake_ffmpeg

            _assemble_clips(
                steps=steps,
                clip_id_to_local={
                    "clip_a": str(clip_a),
                    "clip_b": str(clip_b),
                },
                clip_probe_map={},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                interstitials=interstitial_list,
            )

            mock_insert.assert_called_once()

    def test_zero_hold_cumulative_stays_in_sync(self, tmp_path):
        """hold_s=0 still adds 0.0 to cumulative_s (no drift)."""
        from app.tasks.template_orchestrate import _assemble_clips

        clip_a = tmp_path / "clip_a.mp4"
        clip_b = tmp_path / "clip_b.mp4"
        clip_a.write_bytes(b"fake")
        clip_b.write_bytes(b"fake")

        steps = [
            self._make_step("clip_a", 1, 5.0),
            self._make_step("clip_b", 2, 5.0),
        ]

        interstitial_list = [
            {"type": "curtain-close", "after_slot": 1, "hold_s": 0.0,
             "animate_s": 2.0, "hold_color": "#000000"},
        ]

        def fake_reframe(**kwargs):
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"\x00" * 64)

        with (
            patch(
                "app.pipeline.reframe.reframe_and_export",
                side_effect=fake_reframe,
            ) as mock_reframe,
            patch(
                "app.tasks.template_orchestrate._insert_interstitial",
            ),
            patch("app.tasks.template_orchestrate.subprocess.run") as mock_ffmpeg,
        ):
            def fake_ffmpeg(cmd, **kw):
                if "-y" in cmd:
                    idx = cmd.index("-y") + 1
                    if idx < len(cmd):
                        with open(cmd[idx], "wb") as f:
                            f.write(b"\x00" * 64)
                return MagicMock(returncode=0)

            mock_ffmpeg.side_effect = fake_ffmpeg

            _assemble_clips(
                steps=steps,
                clip_id_to_local={
                    "clip_a": str(clip_a),
                    "clip_b": str(clip_b),
                },
                clip_probe_map={},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                interstitials=interstitial_list,
            )

            # Both slots rendered — confirms no crash from cumulative_s drift
            assert mock_reframe.call_count == 2


# ── Regression: template_kind kwarg strip ────────────────────────────────────
# Migration 0010 backfilled `template_kind: "multiple_videos"` onto every
# existing recipe. TemplateRecipe is a strict dataclass; without stripping
# the routing-only field, every legacy template crashes at init.

class TestTemplateKindStrip:
    def test_template_recipe_init_succeeds_with_template_kind_in_data(self):
        """Recipe payload (as backfilled by migration 0010) must construct
        cleanly after the orchestrator's strip step."""
        from app.pipeline.agents.gemini_analyzer import TemplateRecipe

        # Realistic shape from a backfilled multiple_videos template
        recipe_data = {
            "template_kind": "multiple_videos",  # ← what the migration added
            "shot_count": 3,
            "total_duration_s": 12.0,
            "hook_duration_s": 3.0,
            "slots": [
                {"position": 1, "target_duration_s": 3.0, "priority": 10, "slot_type": "hook"},
                {"position": 2, "target_duration_s": 4.5, "priority": 8, "slot_type": "content"},
                {"position": 3, "target_duration_s": 4.5, "priority": 8, "slot_type": "content"},
            ],
            "copy_tone": "energetic",
            "caption_style": "default",
            "interstitials": [],
            "beat_timestamps_s": [],
        }

        # Direct init MUST raise — proves the regression existed
        import pytest
        with pytest.raises(TypeError, match="template_kind"):
            TemplateRecipe(**recipe_data)

        # The orchestrator's _build_recipe helper MUST succeed
        from app.tasks.template_orchestrate import _build_recipe
        recipe = _build_recipe(recipe_data)
        assert recipe.shot_count == 3
        assert len(recipe.slots) == 3
