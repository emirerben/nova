from __future__ import annotations

import copy
import types
import uuid

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj

_VALID_ELEMENT = {
    "id": "user-text-1",
    "text": "Creator note",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "position": "middle",
}


def _job(**variant_extra):
    variant = {
        "variant_id": "song_lyrics",
        "text_mode": "lyrics",
        "music_track_id": "track-1",
        "lyrics_available": True,
        "render_status": "ready",
        "render_finished_at": "base-gen",
        "base_video_path": "generative-jobs/j/lyric-base.mp4",
        "video_path": "generative-jobs/j/lyrics.mp4",
        **variant_extra,
    }
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["slot-uploads/u/clip.mp4"]},
        status="variants_ready",
    )


def _track():
    return types.SimpleNamespace(id="track-1", lyrics_cached={"language": "en"})


def _lyrics_req(**kwargs) -> gj.LyricsSectionRequest:
    return gj.LyricsSectionRequest(**kwargs)


def _commit_req(**kwargs) -> gj.EditorCommitRequest:
    kwargs.setdefault("base_generation", "base-gen")
    return gj.EditorCommitRequest(**kwargs)


def test_editor_commit_lyrics_and_text_elements_one_default_render(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs)),
        raising=False,
    )
    job = _job()
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        lyrics=_lyrics_req(
            enabled=True,
            line_overrides={
                "L7": {"text": "New lyric", "orig_text": "Old lyric", "orig_start_s": 3.5}
            },
        ),
    )

    prep = gj.prepare_editor_commit(job, "song_lyrics", req, music_track=_track())

    variant = job.assembly_plan["variants"][0]
    assert variant["text_elements"][0]["text"] == "Creator note"
    assert variant["lyrics_enabled"] is True
    assert variant["lyric_line_overrides"]["L7"]["text"] == "New lyric"
    assert variant["render_status"] == "rendering"
    assert prep["sections"]["text_elements"] is True
    assert prep["sections"]["lyrics"] is True

    gj.enqueue_editor_commit_render(str(job.id), "song_lyrics", prep)
    assert len(calls) == 1
    assert calls[0]["kwargs"]["render_gen_id"] == prep["generation"]
    assert "queue" not in calls[0]


def test_editor_commit_stale_base_generation_persists_nothing(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_lyrics",
            _commit_req(base_generation="stale", lyrics=_lyrics_req(enabled=True)),
            music_track=_track(),
        )

    assert exc.value.status_code == 409
    assert job.assembly_plan == before


def test_editor_commit_lyrics_section_rejected_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", False, raising=False)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            _job(),
            "song_lyrics",
            _commit_req(lyrics=_lyrics_req(enabled=False)),
            music_track=_track(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "lyrics_editor_disabled"
