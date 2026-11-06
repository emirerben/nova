"""R5-S3: the mute card says what the clip's own measurement heard.

The mute card is dark (``PINNED_TAP_APPLIES_CLIP_SOUND`` ships False), but the day it is served it
must not be a bare "Clip 3": the creator is asked to silence a clip, and the reason is the one thing
the measurement knows.

Failure catalogue (written first; each row is a test below):

  V1  the card says no reason (only a clip name, or nothing at all) (GAP-9).
  V2  the line names the noise the signal measured: wind when the wind head is the stronger one,
      crowd when the crowd head is, in the thread's language, with the card's clip number.
  V3  the line is closed text: an exact template with the clip number (the match above is
      equality), so no transcript word, footage label or number the signal did not measure.
"""

from __future__ import annotations

import pytest

from app.database import sync_session
from app.models import PlanItem
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import (
    KEYS,
    build,
    card_of,
    keep_mute_card,
    kinds_of,
)
from tests.tasks import kria_pinned_suggestion_harness as h

WIND = {"en": "Wind noise on clip {n}", "tr": "{n}. klipte rüzgâr gürültüsü"}
CROWD = {"en": "Crowd noise on clip {n}", "tr": "{n}. klipte kalabalık gürültüsü"}


def tilt_the_noise(seeded: h.Seeded, media_id: str, *, wind: float, crowd: float) -> None:
    """The measured noise of one clip: which head is the stronger one (still measured noisy)."""
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        rows = [dict(r) for r in item.clip_assignments]
        for row in rows:
            if row["media_id"] != media_id:
                continue
            analysis = dict(row["analysis"])
            signals = dict(analysis["signals"])
            quality = dict(signals["audio_quality"])
            quality["data"] = {**quality["data"], "wind_score": wind, "crowd_score": crowd}
            signals["audio_quality"] = quality
            analysis["signals"] = signals
            row["analysis"] = analysis
        item.clip_assignments = rows
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("heard", ["recorded", "crowd"])
async def test_v1_v3_the_mute_card_names_the_noise_the_signal_measured(
    talking_world, monkeypatch, key, heard
):
    keep_mute_card(monkeypatch, "mute_clip")
    rec = Recorder(f"R5 evidence V1-V3 {key} {heard}")
    where = f"{key}/signals/mute_clip/{heard}"
    scene = build(talking_world, key, "signals")
    lang = "tr" if key.endswith("/tr") else "en"
    if heard == "crowd":
        noisy = scene.scn.clips[2].media_id
        tilt_the_noise(scene.seeded, noisy, wind=0.1, crowd=0.9)
    out = await scene.phone.serve()
    card = card_of(out, "mute_clip")
    assert card is not None, f"the thread did not serve a mute card: {kinds_of(out)}"
    n = card.anchor.clip_ordinal
    want = (CROWD if heard == "crowd" else WIND)[lang].format(n=n)
    said = [card.why, *card.evidence]  # with no usable agent reason the first line is the why
    rec.negative(
        f"V1/V2 the {heard} clip: the card says which noise was measured, on which clip",
        where,
        want,
        want in said,
        f"why {card.why!r}; evidence {card.evidence}",
    )
    rec.finish()
