"""Review C3 regression: geometry arbitration is opted into ONLY via protected_boxes.

- protected_boxes=None (v1 / legacy callers) → NO arbitration, even when the
  caller passes a layout_receipt_out sink — pre-v2 layout stays byte-stable.
- protected_boxes=[] (v2 with nothing protected) → arbitration still runs, so
  simultaneous cards keep their mutual-overlap dedup.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.agents._schemas.media_overlay import MediaOverlay
from app.pipeline import media_overlay as mo


def _stacked_cards() -> list[MediaOverlay]:
    return [
        MediaOverlay.model_validate(
            {
                "id": card_id,
                "kind": "video",
                "src_gcs_path": f"users/u/plan/p/overlays/{card_id}.mp4",
                "position": "custom",
                "x_frac": 0.5,
                "y_frac": 0.5,
                "scale": 0.3,
                "start_s": 0.0,
                "end_s": 3.0,
            }
        )
        for card_id in ("first", "second")
    ]


def _fake_download(_gcs, local):  # noqa: ANN001
    with open(local, "wb") as f:
        f.write(b"x")


def _apply(cards, protected_boxes):
    receipts: list[dict] = []
    applied: list[dict] = []
    with (
        patch.object(mo.storage, "download_to_file", side_effect=_fake_download),
        patch.object(mo.storage, "upload_public_read", return_value="https://signed"),
        patch.object(mo.subprocess, "run", return_value=MagicMock(returncode=0, stderr=b"")),
    ):
        mo.apply_media_overlays(
            "base/key.mp4",
            cards,
            "out/key.mp4",
            job_id="j1",
            protected_boxes=protected_boxes,
            layout_receipt_out=receipts,
            applied_cards_out=applied,
        )
    return receipts, applied


def test_legacy_none_protection_skips_arbitration_despite_receipt_sink() -> None:
    receipts, applied = _apply(_stacked_cards(), protected_boxes=None)

    assert receipts == []
    assert [(c["x_frac"], c["y_frac"]) for c in applied] == [(0.5, 0.5), (0.5, 0.5)]


def test_v2_empty_protection_list_still_dedupes_stacked_cards() -> None:
    receipts, applied = _apply(_stacked_cards(), protected_boxes=[])

    assert len(receipts) == 2
    assert receipts[0]["decision"] == "kept"
    assert receipts[1]["decision"] in {"moved", "shrunk"}
    assert (applied[0]["x_frac"], applied[0]["y_frac"]) != (
        applied[1]["x_frac"],
        applied[1]["y_frac"],
    )
