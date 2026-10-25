"""The bundled engine-noise model, its pinned digest and FFmpeg stay in lockstep.

Failure modes this pins (listed before the asset landed):
- the file is missing from the checkout / image copy set (the Dockerfile copies the
  whole assets tree, so presence here is presence in the image);
- the pinned digest drifts from the file (the worker would refuse the model as
  "unavailable or changed" and the lane would silently stay off);
- the file is not an RNNoise model FFmpeg's `arnndn` can load;
- the three settings plus the two verified features are not the complete gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.pipeline import engine_noise_model as model
from app.pipeline.speech_denoise import _filter_for_model
from app.services.phone_noise_reduction import engine_noise_reduction_available


def test_bundled_model_is_present_and_matches_the_pinned_digest() -> None:
    path = model.bundled_model_path()
    assert path.is_file(), f"{path} must ship with the API assets"
    assert model.file_sha256(path) == model.BUNDLED_MODEL_SHA256
    assert path.name == model.BUNDLED_MODEL_FILENAME
    assert model.IMAGE_MODEL_PATH.endswith(model.BUNDLED_MODEL_FILENAME)


def test_bundled_model_is_an_rnnoise_model_the_worker_accepts() -> None:
    path = model.bundled_model_path()
    with path.open("rb") as stream:
        assert stream.readline().startswith(b"rnnoise-nu model file version 1")
    filter_arg, kind = _filter_for_model(path, model.BUNDLED_MODEL_SHA256)
    assert kind == "arnndn"
    assert filter_arg.startswith("arnndn=m=") and filter_arg.endswith(":mix=0.8")
    with pytest.raises(Exception, match="unavailable or changed"):
        _filter_for_model(path, "0" * 64)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_ffmpeg_arnndn_loads_the_bundled_model(tmp_path: Path) -> None:
    filter_arg, _ = _filter_for_model(model.bundled_model_path(), model.BUNDLED_MODEL_SHA256)
    out = tmp_path / "denoised.wav"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=1:c=pink:a=0.1:r=48000",
            "-filter_complex",
            f"[0:a][1:a]amix=inputs=2[m];[m]{filter_arg}",
            "-ac",
            "1",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size > 48_000  # a second of 16-bit mono PCM, not an empty file


def test_the_three_secrets_and_two_features_are_the_whole_gate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "phone_engine_noise_reduction_enabled", True)
    monkeypatch.setattr(settings, "phone_engine_noise_model_path", model.IMAGE_MODEL_PATH)
    monkeypatch.setattr(settings, "phone_engine_noise_model_sha256", model.BUNDLED_MODEL_SHA256)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["audioMix"])
    assert engine_noise_reduction_available() is False  # processedAudio still missing
    monkeypatch.setattr(settings, "phone_render_verified_features", list(model.REQUIRED_FEATURES))
    assert engine_noise_reduction_available() is True


def test_fly_commands_keep_the_current_features_and_add_the_required_ones() -> None:
    commands = model.fly_secrets_commands(["variableSpeed", "audioMix"])
    assert commands[0].startswith("fly secrets set -a nova-video ")
    assert f"PHONE_ENGINE_NOISE_MODEL_SHA256={model.BUNDLED_MODEL_SHA256}" in commands[0]
    assert f"PHONE_ENGINE_NOISE_MODEL_PATH={model.IMAGE_MODEL_PATH}" in commands[0]
    assert commands[1].startswith("fly secrets set -a nova-video PHONE_RENDER_VERIFIED_FEATURES=")
    assert '"audioMix","processedAudio","variableSpeed"' in commands[1]
