from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj


def _job(variant: dict):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [variant]},
        status="variants_ready",
    )


def _variant(**extra) -> dict:
    return {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "render_status": "ready",
        "render_generation_id": "old-gen",
        "resolved_archetype": "montage",
        **extra,
    }


def _result(value):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _db(job):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(job))
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_dispatch_set_orientation_404_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", False, raising=False)
    job = _job(_variant())

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_set_orientation(_db(job), job, "song_text", orientation="landscape")

    assert exc.value.status_code == 404


@pytest.mark.parametrize("orientation", ["square", "", None])
def test_validate_orientation_rejects_bad_value(monkeypatch, orientation) -> None:
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True, raising=False)

    with pytest.raises(HTTPException) as exc:
        gj.validate_orientation_section(_variant(), orientation)

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_orientation"


@pytest.mark.parametrize(
    "variant",
    [
        _variant(resolved_archetype="subtitled"),
        _variant(resolved_archetype="narrated"),
        _variant(resolved_archetype="talking_head"),
        _variant(montage_preset="masonry"),
    ],
)
def test_validate_orientation_rejects_unsupported_variant(monkeypatch, variant) -> None:
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True, raising=False)

    with pytest.raises(HTTPException) as exc:
        gj.validate_orientation_section(variant, "landscape")

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "orientation_unsupported"


@pytest.mark.asyncio
async def test_dispatch_set_orientation_persists_and_enqueues(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs)),
        raising=False,
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    job = _job(_variant())

    await gj.dispatch_set_orientation(_db(job), job, "song_text", orientation="landscape")

    variant = job.assembly_plan["variants"][0]
    assert variant["orientation"] == "landscape"
    assert variant["render_generation_id"] != "old-gen"
    assert variant["render_status"] == "rendering"
    assert calls == [
        {
            "args": [str(job.id), "song_text"],
            "kwargs": {
                "render_gen_id": variant["render_generation_id"],
                "orientation_override": "landscape",
            },
        }
    ]


def test_prepare_editor_commit_orientation_stages_render_section(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True, raising=False)
    job = _job(_variant())

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        gj.EditorCommitRequest(orientation="landscape", base_generation="old-gen"),
    )

    variant = job.assembly_plan["variants"][0]
    assert variant["orientation"] == "landscape"
    assert variant["render_generation_id"] == prep["generation"]
    assert prep["orientation_override"] == "landscape"
    assert prep["has_render_section"] is True
    assert prep["sections"]["orientation"] is True


def test_enqueue_editor_commit_render_sends_orientation_override(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs)),
        raising=False,
    )

    gj.enqueue_editor_commit_render(
        "job-1",
        "song_text",
        {
            "has_render_section": True,
            "generation": "gen-2",
            "timeline_override": None,
            "mix_override": None,
            "new_track_id": None,
            "orientation_override": "landscape",
            "text_requires_full_render": False,
            "media_overlays_override": None,
            "sfx_override": None,
            "sections": {
                "caption_cues": False,
                "caption_meta": False,
                "sound_effects": False,
                "media_overlays": False,
                "text_elements": False,
                "timeline": False,
                "mix": False,
                "orientation": True,
                "lyrics": False,
            },
        },
    )

    assert calls == [
        {
            "args": ["job-1", "song_text"],
            "kwargs": {"render_gen_id": "gen-2", "orientation_override": "landscape"},
        }
    ]


def test_editor_capabilities_orientation_states(monkeypatch) -> None:
    job = _job(_variant())
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", False, raising=False)
    disabled = gj._editor_capabilities(job, _variant())
    assert disabled["orientation"] == {
        "editable": False,
        "value": "portrait",
        "reason": "disabled",
    }

    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True, raising=False)
    enabled = gj._editor_capabilities(job, _variant(orientation="landscape"))
    assert enabled["orientation"] == {
        "editable": True,
        "value": "landscape",
        "reason": None,
    }

    unsupported = gj._editor_capabilities(job, _variant(resolved_archetype="subtitled"))
    assert unsupported["orientation"] == {
        "editable": False,
        "value": "portrait",
        "reason": "orientation_unsupported",
    }
