"""KRI-1139: the chat copilot puts a pool photo or video behind a tilted clip.

`patch_slots.tilt.backdrop = {"kind": "media", "asset_id": ...}` is taught, parsed and applied only
while the snapshot opens `tilt_backdrop_media`, and only for an id in its `tilt_backdrop_pool`
(the item's ready photos and videos). Each test is one way this can go wrong.
"""

from __future__ import annotations

import pytest

from app.agents import editor_ops_v2 as v2
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, _format_snapshot
from app.services.kria_editor_ops import build_editor_snapshot
from tests.agents.test_clip_tilt_ai import (
    _caps,
    _compile,
    _parse,
    _said,
    _three_clips,
    _tilt,
)
from tests.services.test_kria_editor_ops import _job

OPEN_MEDIA = {
    "text_elements": True,
    "timeline": True,
    "clips": {
        "tilt": {"editable": True, "reason": None},
        "tilt_backdrop_media": {"editable": True, "reason": None},
    },
}
OPEN_TILT_ONLY = {
    "text_elements": True,
    "timeline": True,
    "clips": {"tilt": {"editable": True, "reason": None}},
}
POOL = [
    {"id": "pool-photo-1", "kind": "image", "label": "beach.jpg"},
    {"id": "pool-photo-2", "kind": "image", "label": "sunset.jpg", "context": "golden hour"},
    {"id": "pool-video-1", "kind": "video", "label": "waves.mp4", "duration_s": 9.0},
]


def _media(asset_id: str) -> dict:
    return {"kind": "media", "asset_id": asset_id}


def _snapshot(monkeypatch, variant, caps=OPEN_MEDIA, pool=POOL):
    _caps(monkeypatch, caps)
    return build_editor_snapshot(
        _job(variant), variant, clip_context={"background_pool": pool} if pool is not None else None
    )


def test_put_my_second_photo_behind_the_tilted_clip(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0})
    snapshot = _snapshot(monkeypatch, variant)
    op = _tilt({"slot_indexes": [1]}, {"backdrop": _media("pool-photo-2")})

    parsed = _parse(snapshot, op)
    assert parsed.ops == [op]

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 10.0, "backdrop": _media("pool-photo-2")}
    assert "tilt" not in rows[0] or rows[0]["tilt"] is None
    assert _said(draft) == ["Changed the background behind clip 2: photo or video behind it"]


def test_an_angle_and_a_video_backdrop_in_one_patch(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(
        snapshot,
        _tilt({"slot_indexes": [1]}, {"rotation_deg": 10, "backdrop": _media("pool-video-1")}),
    )
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 10.0, "backdrop": _media("pool-video-1")}
    assert _said(draft) == ["Tilted clip 2 10 degrees right, photo or video behind it"]


@pytest.mark.parametrize("asset_id", ["not-in-the-pool", "slot-3", "", "b.jpg"])
def test_an_id_outside_the_pool_never_reaches_the_draft(monkeypatch, asset_id):
    snapshot = _snapshot(monkeypatch, _three_clips())
    parsed = _parse(
        snapshot,
        _tilt({"slot_indexes": [1]}, {"backdrop": _media(asset_id)}),
        reply="Put your photo behind clip 2.",
    )
    assert parsed.ops == []
    assert "Put your photo" not in parsed.reply
    assert "Visuals" in parsed.reply or "photo" in parsed.reply


def test_an_empty_pool_refuses_every_media_backdrop(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), pool=[])
    assert snapshot["tilt_backdrop_pool"] == []
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": _media("pool-photo-1")}))
    assert parsed.ops == []


def test_a_media_backdrop_has_no_colour_and_no_other_keys(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips())
    for backdrop in (
        {"kind": "media", "asset_id": "pool-photo-1", "color": "#000000"},
        {"kind": "media"},
        {"kind": "blur", "asset_id": "pool-photo-1"},
        {"kind": "media", "asset_id": "pool-photo-1", "loop": False},
    ):
        parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": backdrop}))
        assert parsed.ops == [], backdrop


def test_the_pool_and_the_capability_are_absent_while_the_media_gate_is_closed(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0})
    closed = _snapshot(monkeypatch, variant, OPEN_TILT_ONLY)
    assert "tilt_backdrop_media" not in closed["timeline_patch_capabilities"]
    assert "tilt_backdrop_pool" not in closed
    assert "BACKDROP POOL" not in _format_snapshot(closed)
    assert "PHOTO OR VIDEO BEHIND" not in v2.family_fragments(closed)
    assert "CLIP TILT" in v2.family_fragments(closed)

    opened = _snapshot(monkeypatch, variant)
    assert opened["tilt_backdrop_pool"] == POOL
    assert "BACKDROP POOL" in _format_snapshot(opened)
    assert v2.family_fragments(opened).startswith(v2.family_fragments(closed))
    assert "PHOTO OR VIDEO BEHIND" in v2.family_fragments(opened)


def test_a_closed_media_gate_refuses_a_media_backdrop_in_a_plain_sentence(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), OPEN_TILT_ONLY)
    parsed = _parse(
        snapshot,
        _tilt({"slot_indexes": [1]}, {"backdrop": _media("pool-photo-1")}),
        reply="Put your photo behind clip 2.",
    )
    assert parsed.ops == []
    assert "can't" in parsed.reply
    assert "Put your photo" not in parsed.reply


def test_the_prompt_shows_the_pool_and_a_clips_current_media_backdrop(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0, "backdrop": _media("pool-photo-1")})
    text = _format_snapshot(_snapshot(monkeypatch, variant))
    assert "id='pool-photo-2' kind='image' label='sunset.jpg'" in text
    assert "tilt=(rotation_deg=10.00, backdrop=media(pool-photo-1))" in text
    prompt = EditCopilotAgent(None).render_prompt(
        EditCopilotInput(utterance="x", variant_snapshot=_snapshot(monkeypatch, variant))
    )
    assert "asset_id comes ONLY from BACKDROP POOL" in prompt


def test_straightening_removes_the_media_backdrop_too(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0, "backdrop": _media("pool-photo-1")})
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, None))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1].get("tilt") is None
    assert _said(draft) == ["Straightened clip 2"]


def test_the_creator_wire_offers_the_media_backdrop_only_inside_the_clip_tilt():
    from app.agents._creator_wire import copilot_schema  # noqa: PLC0415

    seen: list[dict] = []

    def walk(node, parent=None):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "backdrop" and isinstance(value, dict) and value.get("type") == "object":
                    seen.append(value)
                walk(value, key)
        elif isinstance(node, list):
            for value in node:
                walk(value, parent)

    walk(copilot_schema())
    assert seen
    for backdrop in seen:
        assert backdrop["properties"]["kind"]["enum"] == ["blank", "color", "blur", "media"]
        assert set(backdrop["properties"]) == {"kind", "color", "asset_id"}


def test_the_chat_draft_reaches_the_save_pin_with_the_pool_id(monkeypatch):
    """chat -> compile -> commit payload -> the pin the Save makes first (the AI-to-Save seam)."""
    from app.config import settings
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.client_protocol import clear_request_context, set_client_protocol
    from app.services.kria_editor_ops import compile_editor_ops, editor_payload_dump
    from app.services.phone_speaker_background import backdrop_pin_requests

    variant = _three_clips(tilt={"rotation_deg": 10.0})
    variant["render_destination"] = "device"
    snapshot = _snapshot(monkeypatch, variant)
    op = _tilt({"slot_indexes": [1]}, {"backdrop": _media("pool-photo-2")})
    assert _parse(snapshot, op).ops == [op]

    job = _job(variant)
    payload = EditorCommitRequest.model_validate(
        editor_payload_dump(compile_editor_ops(job, variant, [op]).payload)
    )
    monkeypatch.setattr(settings, "media_tilt_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["clipTilt", "clipBackdropMedia"]
    )
    set_client_protocol(29)
    try:
        assert backdrop_pin_requests(job, variant["variant_id"], payload) == ["pool-photo-2"]
        monkeypatch.setattr(settings, "phone_render_verified_features", ["clipTilt"])
        assert backdrop_pin_requests(job, variant["variant_id"], payload) == []
    finally:
        clear_request_context()
