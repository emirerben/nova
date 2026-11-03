"""KRI-1165: a fresh chat draft carries the lanes the status route projects.

A never-Saved phone Talking edit keeps its speaker background only in the pinned
device recipe; the status route projects it on read. The draft snapshot that the
planner and the app's editor start from must carry the same section, or a chat
turn edits (and the app saves) a cut with the background missing.
"""

from __future__ import annotations

from app.kria import drafts
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_subtitled_editor import PHONE_SUBTITLED_EDITOR_LANES_FIELD
from tests.routes.test_speaker_background_editor import (
    PHOTO,
    _background,
    _document,
    background_job,
)


def test_fresh_draft_carries_the_projected_speaker_background(monkeypatch) -> None:
    clear_request_context()
    try:
        job = background_job(monkeypatch, background=_background())
        set_client_protocol(27)

        served = gj._variants_for_response(job)[0]
        assert served["speaker_background"] == _document(PHOTO)

        payload = drafts.bootstrap_editor_payload(job, "subtitled", "first")

        assert payload["sections"]["speaker_background"] == _document(PHOTO)
        assert payload["base_generation"] == "first"
    finally:
        clear_request_context()


def test_fresh_draft_carries_the_projected_colour_fill(monkeypatch) -> None:
    """KRI-1173 (S56): a colour fill has no media id, so a reader keyed on `media_id` drops it."""
    from tests.routes.test_speaker_background_color_editor import WHITE, colour_job

    clear_request_context()
    try:
        job = colour_job(monkeypatch)
        set_client_protocol(34)
        # A never-Saved edit keeps the colour only in the pinned recipe.
        variant = job.assembly_plan["variants"][0]
        for key in ("speaker_background", "sound_effects", "media_overlays"):
            variant.pop(key, None)
        variant.pop(PHONE_SUBTITLED_EDITOR_LANES_FIELD, None)

        served = gj._variants_for_response(job)[0]
        assert served["speaker_background"] == WHITE

        payload = drafts.bootstrap_editor_payload(job, "subtitled", "first")

        assert payload["sections"]["speaker_background"] == WHITE
    finally:
        clear_request_context()
