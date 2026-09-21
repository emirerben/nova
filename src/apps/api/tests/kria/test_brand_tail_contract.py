"""The server's brand-tail durations must match the assets the phone ships.

`BRAND_TAIL_SECONDS` is what `_verify_export` adds to a recipe's own duration
before checking an uploaded phone render, and what the publish payload records
as `duration_s`. The number is owned by the server precisely so a client cannot
choose it — which means nothing on the client side stops the bundled outro from
being re-cut to a different length and silently invalidating every upload.

This is that stop. The API image does not ship `brand/` or the iOS package, so
the constant cannot be derived at runtime; it is asserted here against the
checked-in asset instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.kria.device_render import BRAND_TAIL_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[5]
BUNDLED_OUTRO = (
    REPO_ROOT
    / "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Resources"
    / "kria-outro-paper.mp4"
)
# What `brand/social/build.py` generates and copies into the package.
KIT_OUTRO = REPO_ROOT / "brand/social/dist/outro/kria-outro-paper.mp4"

# One frame at 30fps. The tail only has to be accurate enough that the
# verifier's own max(0.1, 2/fps) tolerance still bites on real drift.
ONE_FRAME_S = 1 / 30


def _duration(path: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return float(json.loads(probe.stdout)["format"]["duration"])


def test_no_tail_means_no_extra_duration():
    assert BRAND_TAIL_SECONDS["none"] == 0.0


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="FFmpeg is required")
def test_standard_tail_matches_the_bundled_outro():
    assert BUNDLED_OUTRO.exists(), f"the phone's outro asset is missing: {BUNDLED_OUTRO}"
    measured = _duration(BUNDLED_OUTRO)
    assert abs(measured - BRAND_TAIL_SECONDS["standard"]) <= ONE_FRAME_S, (
        f"the bundled outro is {measured:.3f}s but the server expects "
        f"{BRAND_TAIL_SECONDS['standard']}s — every branded phone render would "
        "fail verification with 'export duration mismatch'"
    )


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="FFmpeg is required")
def test_the_phone_bundles_the_asset_the_brand_kit_generated():
    """Guards the other direction: the kit rebuilt but the package not resynced."""
    if not KIT_OUTRO.exists():
        pytest.skip("brand kit output is not present in this checkout")
    assert BUNDLED_OUTRO.read_bytes() == KIT_OUTRO.read_bytes(), (
        "brand/social/dist and the Swift package have drifted — "
        "re-run brand/social/build.py"
    )
