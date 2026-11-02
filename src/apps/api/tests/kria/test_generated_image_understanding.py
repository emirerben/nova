"""A Kria-generated image never holds a turn in "still checking".

Prod thread b61b7673, 2026-11-02: the creator's chat made two generated images
(``PlanItemAsset`` rows, ``analysis = {"source": "generated", width, height, model}``). The
background analyser only reads ``clip_assignments``, never pool assets, so no analysis ever
landed; the turn's clip-understanding wait (45 s) therefore ran to its end on EVERY run, the
run answered "clips still checking" (continuable) and the turn ran again, up to its run cap.
Two turns took 133 s and 144 s and failed.
"""

from __future__ import annotations

import time

from app.services.clip_intent_planning import wait_for_clip_understanding
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_understanding import understanding_incomplete

GENERATED = {"width": 1024, "height": 1536, "source": "generated", "model": "gemini-image"}


def _generated_clip() -> IntentClip:
    return IntentClip(
        media_id="asset-6b63a5d7",
        kind="image",
        analysis=dict(GENERATED),
        gcs_path="users/u/plan/i/pool/op/abc.png",
        asset_id="6b63a5d7",
    )


async def test_the_wait_does_not_sit_on_a_generated_image() -> None:
    clips = [_generated_clip()]

    async def refresh() -> list[IntentClip]:
        return clips

    started = time.monotonic()
    _, spent = await wait_for_clip_understanding(
        clips, refresh=refresh, until=time.monotonic() + 5.0
    )

    assert spent < 1.0
    assert time.monotonic() - started < 1.0


def test_a_generated_image_is_not_awaiting_analysis_but_a_blank_upload_still_is() -> None:
    assert not understanding_incomplete(GENERATED, kind="image")
    # The wait still holds for media that analysis can fill in.
    assert understanding_incomplete(None, kind="image")
    assert understanding_incomplete({"width": 1024, "height": 1536}, kind="image")
