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
class TestRequireAudibleSpeech:
    def test_dead_mic_take_is_declined_with_the_typed_creator_message(
        self, silent_clip: str
    ) -> None:
        from app.services.cloud_render_contract import CloudRenderContractError
        from app.tasks.generative_build import _creator_decline_payload, _require_audible_speech

        with patch("app.services.pipeline_trace.record_pipeline_event"):
            with pytest.raises(CloudRenderContractError) as raised:
                _require_audible_speech(silent_clip, job_id="job-1")
        decline = _creator_decline_payload(raised.value)
        assert decline["decline_reason"] == "evidence_missing"
        assert "no sound" in str(raised.value)
        assert "microphone" in decline["alternative"]

    def test_a_spoken_take_passes(self, spoken_clip: str) -> None:
        from app.tasks.generative_build import _require_audible_speech

        _require_audible_speech(spoken_clip, job_id="job-1")

    def test_an_unmeasurable_file_fails_open(self, tmp_path: Path) -> None:
        from app.tasks.generative_build import _require_audible_speech

        _require_audible_speech(str(tmp_path / "missing.mp4"), job_id="job-1")


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
