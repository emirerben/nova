"""The bundled ARNNDN (RNNoise) model the phone engine-noise lane runs with.

`speech_denoise._filter_for_model` only accepts a model whose SHA-256 matches the
configured `PHONE_ENGINE_NOISE_MODEL_SHA256`, and `.env.example` says the model is
"supplied locally, never downloaded". This module is that local supply: the file ships
in the production image through the Dockerfile's `COPY src/apps/api/assets ./assets`
(so it lands at `/app/assets/models/...`), and the pinned digest below is what the Fly
secret must carry. `tests/pipeline/test_engine_noise_model_asset.py` keeps the file,
the digest and FFmpeg's ability to load it in lockstep.

Model: `leavened-quisling-2018-08-31/lq.rnnn` from github.com/GregorR/rnnoise-models
("voice" signal — speech plus non-speech human sounds — against general noise; the
README states the model files are not subject to copyright). It is a stationary-noise
suppressor: steady engine / motor hum is in scope, wind, traffic and crowd noise are not.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Iterable
from pathlib import Path

BUNDLED_MODEL_FILENAME = "rnnoise-lq-2018-08-31.rnnn"
BUNDLED_MODEL_SHA256 = "1957528b752799fddf06270bc5469af7cf54c3badc358544ae2abed730943ff9"
# Where the Dockerfile puts `src/apps/api/assets` (WORKDIR /app + `COPY ... ./assets`).
IMAGE_MODEL_PATH = f"/app/assets/models/{BUNDLED_MODEL_FILENAME}"
REQUIRED_FEATURES = ("processedAudio", "audioMix")
FLY_APP = "nova-video"


def bundled_model_path() -> Path:
    """The model inside this checkout (``src/apps/api/assets/models/...``)."""
    return Path(__file__).resolve().parents[2] / "assets" / "models" / BUNDLED_MODEL_FILENAME


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fly_secrets_commands(verified_features: Iterable[str]) -> list[str]:
    """The exact `fly secrets set` lines that turn the lane on in production.

    ``verified_features`` is the CURRENT production list (read it with
    ``python scripts/admin.py --prod GET admin/kria/phone-render-config``); the result
    adds the two features the lane requires and keeps everything else. Each command
    restarts the api and worker machines, so run them only after the image that carries
    the model has deployed.
    """
    features = sorted(set(verified_features) | set(REQUIRED_FEATURES))
    return [
        " ".join(
            [
                f"fly secrets set -a {FLY_APP}",
                "PHONE_ENGINE_NOISE_REDUCTION_ENABLED=true",
                f"PHONE_ENGINE_NOISE_MODEL_PATH={IMAGE_MODEL_PATH}",
                f"PHONE_ENGINE_NOISE_MODEL_SHA256={BUNDLED_MODEL_SHA256}",
            ]
        ),
        f"fly secrets set -a {FLY_APP} PHONE_RENDER_VERIFIED_FEATURES="
        + shlex.quote(json.dumps(features, separators=(",", ":"))),
    ]


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys

    current = json.loads(sys.stdin.read() or "[]") if not sys.stdin.isatty() else []
    print("\n".join(fly_secrets_commands(current)))
