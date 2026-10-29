"""Editor Saves whose output must not move while no caption animation is set (KRI-1007).

Each case drives the real `prepare_editor_commit` on a phone variant and digests what the
Save left behind: the pinned device recipe (layout-insensitive, see `recipe_digest`) and the
caption state the iPhone reads back. The golden values were generated from origin/main
before caption animation existed (`caption_animation_off_state.json`).
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.services.device_render import device_status
from tests.pipeline.single_word_fixture import _stable, recipe_digest

GOLDEN_FILE = "caption_animation_off_state.json"


def _json_digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def text_digest(recipe) -> str:
    """The recipe's text layers and capabilities: the song recipe also pins per-run ids."""
    layers = [layer.model_dump(mode="json") for layer in recipe.text_layers]
    return _json_digest(
        {
            "layers": _stable(layers),
            "capabilities": sorted(recipe.required_capabilities),
        }
    )


def narrated_cases(monkeypatch) -> dict[str, str]:
    from app.routes import generative_jobs as gj
    from tests.routes.test_phone_narrated_editor_commit import _CUES, _WORD_CUES, phone_job, save

    out: dict[str, str] = {}
    job = phone_job(monkeypatch)
    save(job, caption_cues=[dict(_CUES[0], text="First we pack it"), _CUES[1]])
    out["narrated-sentence-recipe"] = recipe_digest(device_status(job, "narrated").request.recipe)
    out["narrated-sentence-cues"] = _json_digest(job.assembly_plan["variants"][0]["caption_cues"])

    job = phone_job(monkeypatch, cues=_WORD_CUES, style="word")
    save(job, caption_meta=gj.EditorCommitCaptionMeta(size_px=96))
    out["narrated-word-recipe"] = recipe_digest(device_status(job, "narrated").request.recipe)
    return out


def subtitled_cases(monkeypatch) -> dict[str, str]:
    from app.routes import generative_jobs as gj
    from tests.pipeline.single_word_fixture import saved_recipe, talk_job

    job = talk_job(monkeypatch)
    edited = [dict(cue) for cue in job.assembly_plan["variants"][0]["caption_cues"]]
    edited[0] = {key: value for key, value in edited[0].items() if key != "words"}
    edited[0]["text"] = "A new first line"
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", caption_cues=edited),
        user_id="owner",
        plan_item_id="item",
    )
    return {
        "subtitled-sentence-recipe": recipe_digest(saved_recipe(job)),
        "subtitled-sentence-cues": _json_digest(job.assembly_plan["variants"][0]["caption_cues"]),
    }


def song_caption_save(job, **sections):
    from app.routes import generative_jobs as gj

    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        **sections,
    )
    gj.require_guided_story_editor_commit(job, "guided_story", payload)
    gj.prepare_editor_commit(job, "guided_story", payload)


def song_caption_job(harness, monkeypatch):
    from app.routes import generative_jobs as gj
    from app.tasks import generative_build as gb
    from tests.tasks.test_user_song_montage_job import _lyrics_analysis

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
    )
    gb._run_generative_job(str(job.id))
    return job


SONG_META_CASES: dict[str, dict] = {
    "song-sentence": {"enabled": True, "style": "sentence"},
    "song-word": {"enabled": True, "style": "word"},
    "song-sentence-highlight": {
        "enabled": True,
        "style": "sentence",
        "appearance": {"highlight_spoken_word": True},
    },
}


def song_cases(harness, monkeypatch) -> dict[str, str]:
    from app.routes import generative_jobs as gj

    out: dict[str, str] = {}
    for name, meta in SONG_META_CASES.items():
        job = song_caption_job(harness, monkeypatch)
        song_caption_save(job, caption_meta=gj.EditorCommitCaptionMeta(**copy.deepcopy(meta)))
        variant = job.assembly_plan["variants"][0]
        revision = gj._guided_v2_revision(job, variant)
        out[f"{name}-text"] = text_digest(device_status(job, "guided_story").request.recipe)
        out[f"{name}-meta"] = _json_digest(revision["caption_meta"])
    return out


def all_cases(harness) -> dict[str, str]:
    out: dict[str, str] = {}
    for build in (narrated_cases, subtitled_cases):
        with pytest.MonkeyPatch.context() as monkeypatch:
            out.update(build(monkeypatch))
    with pytest.MonkeyPatch.context() as monkeypatch:
        out.update(song_cases(harness, monkeypatch))
    return out
