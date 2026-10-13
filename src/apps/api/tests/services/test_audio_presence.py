"""Audio presence gate (KRI-653).

A recorder bug produced a 192.6 s talk-to-camera take whose audio track was digital
silence (prod job c283f1dd). Every model downstream hallucinated over it. These tests
pin the classifier on REAL ffmpeg-generated audio: the silent take, a quiet-room
take that must still pass, a click-only take, and the measurement-failed cases that
must fail OPEN (a broken probe never blocks a creator's render).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.clip_speech import (
    PRESENCE_ACTIVE_DB,
    audio_presence,
    classify_audio_presence,
)

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def _wav(path: Path, source: str, *, duration_s: float, volume_db: float | None = None) -> Path:
    cmd = ["-f", "lavfi", "-i", f"{source}:d={duration_s}"]
    if volume_db is not None:
        cmd += ["-af", f"volume={volume_db}dB"]
    _ffmpeg(*cmd, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(path))
    return path


@requires_ffmpeg
class TestAudioPresenceOnRealAudio:
    def test_digital_silence_is_silent(self, tmp_path: Path) -> None:
        # The incident: an audio track full of zeros (-91 dB in volumedetect).
        clip = _wav(tmp_path / "dead_mic.wav", "anullsrc=r=16000:cl=mono", duration_s=12.0)
        presence = audio_presence(str(clip))
        assert presence.status == "silent"
        assert presence.active_s == 0.0

    def test_normal_speech_level_passes(self, tmp_path: Path) -> None:
        # ffmpeg's sine source is ~-21 dBFS RMS at 0 dB: this is a normal speech level.
        clip = _wav(tmp_path / "speech.wav", "sine=f=220", duration_s=6.0, volume_db=0.0)
        assert audio_presence(str(clip)).status == "ok"

    def test_quiet_room_talker_still_passes(self, tmp_path: Path) -> None:
        # A soft speaker (~-35 dB RMS windows) is far from digital silence. Blocking this
        # would put a real creator's real take behind a decline.
        clip = _wav(tmp_path / "soft.wav", "sine=f=220", duration_s=6.0, volume_db=-14.0)
        assert audio_presence(str(clip)).status == "ok"

    def test_below_noise_floor_hiss_is_silent(self, tmp_path: Path) -> None:
        clip = _wav(
            tmp_path / "hiss.wav",
            "anoisesrc=c=white:a=0.0004:seed=3",
            duration_s=8.0,
        )
        assert audio_presence(str(clip)).status == "silent"

    def test_a_single_click_is_not_speech(self, tmp_path: Path) -> None:
        # 8 s of silence with one 0.2 s bump: audible, but < 1 s of active audio.
        clip = tmp_path / "click.wav"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono:d=8",
            "-f",
            "lavfi",
            "-i",
            "sine=f=300:d=0.2",
            "-filter_complex",
            "[1]adelay=3000,volume=-10dB[b];[0][b]amix=inputs=2:normalize=0,atrim=0:8",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(clip),
        )
        assert audio_presence(str(clip)).status == "inaudible"

    def test_short_sting_is_not_judged_inaudible(self, tmp_path: Path) -> None:
        # Under PRESENCE_MIN_JUDGED_S a clip is too short to call: a 1 s sting is fine.
        clip = _wav(tmp_path / "sting.wav", "sine=f=500", duration_s=1.0, volume_db=-12.0)
        assert audio_presence(str(clip)).status == "ok"


@requires_ffmpeg
class TestAudioPresenceFailsOpen:
    def test_missing_file_is_unknown(self, tmp_path: Path) -> None:
        assert audio_presence(str(tmp_path / "nope.mp4")).status == "unknown"

    def test_video_without_audio_stream_is_unknown(self, tmp_path: Path) -> None:
        clip = tmp_path / "video_only.mp4"
        _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-pix_fmt", "yuv420p", str(clip))
        assert audio_presence(str(clip)).status == "unknown"


class TestClassifyAudioPresence:
    def test_no_windows_is_unknown(self) -> None:
        assert classify_audio_presence([], 30.0).status == "unknown"

    def test_threshold_edges(self) -> None:
        # -61 dB loudest window -> silent; -59 dB (just above) is not "silent".
        quiet = [(i * 0.05, -61.0) for i in range(200)]
        assert classify_audio_presence(quiet, 10.0).status == "silent"
        barely = [(i * 0.05, -59.0) for i in range(200)]
        assert classify_audio_presence(barely, 10.0).status == "inaudible"
        loud = [(i * 0.05, PRESENCE_ACTIVE_DB + 1) for i in range(200)]
        assert classify_audio_presence(loud, 10.0).status == "ok"
