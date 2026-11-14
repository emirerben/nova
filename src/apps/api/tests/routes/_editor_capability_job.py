"""A pinned phone-authored edit with five videos and a photo, for Save tests.

Catalog index 0 and 1 are the authored fixture's own 12 s videos, 2 to 4 are 20 s videos and
``PHOTO`` is a Visuals-pool photo. ``slot`` and ``section`` build the rows the phone writes.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.config import settings
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from tests.pipeline.test_authored_timeline import binding
from tests.routes.test_authored_playback_speed import _authored_phone_job

PHOTO = 5


def capability_job(monkeypatch: pytest.MonkeyPatch):
    job, _ = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *settings.phone_render_verified_features,
            "splitScreen",
            "splitCollapse",
            "sourceCrop",
            "stillImages",
            "editorMedia",
            "clipTilt",
            "clipTransitions",
            "freezeFrame",
            "crossfade",
            "freehandDrawing",
        ],
    )
    monkeypatch.setattr(settings, "media_tilt_enabled", True)
    variant = job.assembly_plan["variants"][0]
    sources = variant[EDITOR_SOURCES_FIELD]["sources"]
    for index in (2, 3, 4):
        receipt = binding(f"v{index}")
        receipt = receipt.model_copy(
            update={"original": receipt.original.model_copy(update={"duration_s": 20})}
        )
        sources.append(
            {
                **sources[0],
                "media_id": f"v{index}",
                "source_index": index,
                "gcs_path": receipt.proxy_path,
                "duration_s": 20,
                "source_binding": receipt.model_dump(mode="json"),
            }
        )
    sources.append(
        {
            **sources[0],
            "media_id": "photo1",
            "kind": "image",
            "lane": "asset",
            "duration_s": None,
            "source_index": PHOTO,
            "gcs_path": "users/owner/photo.jpg",
            "generation": "7",
            "source_binding": None,
            "visual_binding": {
                "media_id": "photo1",
                "gcs_path": "users/owner/photo.jpg",
                "generation": "7",
                "sha256": "c" * 64,
                "byte_count": 100,
                "kind": "image",
                "width": 1080,
                "height": 1920,
            },
        }
    )
    return job


def slot(clip: int, *, panel: int | None = None, offset: float = 0.0, **row: Any) -> dict:
    row.setdefault("in_s", 0.0)
    row.setdefault("duration_s", 4.0)
    wire = {"slot_id": str(uuid.uuid4()), "clip_index": clip, **row}
    if panel is not None:
        wire.update(split_section_id="s", split_panel_index=panel, split_offset_s=offset)
    return wire


def section(duration: float = 4.0, panels: int = 2, **row: Any) -> dict:
    return {"id": "s", "duration_s": duration, "panel_count": panels, **row}
