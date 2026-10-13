"""Job-time silent-audio gate (KRI-653).

Prod job c283f1dd reached `variants_ready` with a 4.9 s video because nothing asked
whether the 192 s talk-to-camera take had any sound. These tests run real ffmpeg audio
through the gate the phone subtitled job and the cloud archetype resolver both use.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")


def _clip(path: Path, source: str, *, duration_s: float = 6.0) -> str:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=64x64:d={duration_s}:r=10",
            "-f",
            "lavfi",
            "-i",
            f"{source}:d={duration_s}",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


@pytest.fixture
def silent_clip(tmp_path: Path) -> str:
    return _clip(tmp_path / "dead_mic.mp4", "anullsrc=r=16000:cl=mono")


@pytest.fixture
def spoken_clip(tmp_path: Path) -> str:
    return _clip(tmp_path / "talk.mp4", "sine=f=220")


@requires_ffmpeg
class TestSilentAudioStatus:
    def test_dead_mic_take_is_silent(self, silent_clip: str) -> None:
        from app.tasks.generative_build import _silent_audio_status

        with patch("app.services.pipeline_trace.record_pipeline_event") as record:
            assert _silent_audio_status(silent_clip, job_id="job-1") == "silent"
        record.assert_called_once()
        assert record.call_args.args[:2] == ("assembly", "silent_audio_declined")

    def test_a_spoken_take_passes(self, spoken_clip: str) -> None:
        from app.tasks.generative_build import _silent_audio_status

        assert _silent_audio_status(spoken_clip, job_id="job-1") is None

    def test_an_unmeasurable_file_fails_open(self, tmp_path: Path) -> None:
        from app.tasks.generative_build import _silent_audio_status

        assert _silent_audio_status(str(tmp_path / "missing.mp4"), job_id="job-1") is None


class TestSilentAudioFailureCopy:
    """The creator must read THIS message, not "your iPhone can't render this yet"."""

    def test_decline_is_creator_facing_and_deterministic(self) -> None:
        from app.pipeline.phone_guided_plan import (
            SilentAudioPlanDeclined,
            UnsupportedPhonePlan,
        )
        from app.tasks.content_plan_build import (
            CREATOR_FACING_DETAIL_CODES,
            job_failure_message,
        )
        from app.tasks.kria_runtime import _DETERMINISTIC_JOB_FAILURE_CODES

        assert issubclass(SilentAudioPlanDeclined, UnsupportedPhonePlan)
        assert "silent_audio_declined" in CREATOR_FACING_DETAIL_CODES
        assert "silent_audio_declined" in _DETERMINISTIC_JOB_FAILURE_CODES
        detail = "Your video has no sound, so there's nothing to caption or cut. Record it again."
        assert job_failure_message("silent_audio_declined", detail) == detail
        # With no stored detail it still reads as this problem, never the generic iPhone line.
        assert "no sound" in (job_failure_message("silent_audio_declined", None) or "")

    def test_turkish_copy_exists(self) -> None:
        from app.tasks.kria_runtime import _JOB_FAILURE_MESSAGES_TR

        assert "ses yok" in _JOB_FAILURE_MESSAGES_TR["silent_audio_declined"]


@requires_ffmpeg
class TestSubtitledArchetypeResolution:
    def _resolve(self, local: str, *, plan_authority: bool):
        from app.tasks.generative_build import _resolve_archetype

        with (
            patch("app.config.settings.subtitled_archetype_enabled", True),
            patch("app.services.pipeline_trace.record_pipeline_event"),
        ):
            return _resolve_archetype(
                "subtitled",
                [],
                {"clip-1": local},
                job_id="job-1",
                plan_authority=plan_authority,
            )

    def test_silent_clip_declines_under_plan_authority(self, silent_clip: str) -> None:
        from app.services.cloud_render_contract import CloudRenderContractError

        with pytest.raises(CloudRenderContractError, match="no sound"):
            self._resolve(silent_clip, plan_authority=True)

    def test_silent_clip_falls_back_without_plan_authority(self, silent_clip: str) -> None:
        assert self._resolve(silent_clip, plan_authority=False) == ("montage", None, "no_speech")

    def test_spoken_clip_still_resolves_to_subtitled(self, spoken_clip: str) -> None:
        assert self._resolve(spoken_clip, plan_authority=True) == ("subtitled", None, None)
