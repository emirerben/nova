"""E2 transactional editor commit + E4 editor_capabilities.

Dispatch-level tests (house style of test_generative_timeline.py: fake
SimpleNamespace jobs, no DB) for the validate-all-first / baseline-conflict /
atomic-stage / one-kick contract, plus endpoint-level tests for the plan-items
route (mock-DB pattern of test_text_elements_route.py) covering the title
section and the single-commit + kick-after-commit flow.
"""

from __future__ import annotations

import copy
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.routes.generative_jobs as gj
from app.auth import get_current_user
from app.database import get_db
from app.main import app

REGEN = "app.tasks.generative_build.regenerate_generative_variant"

GRID = [0.0, 0.5, 1.1, 1.6, 2.4, 3.0, 3.8]

_VALID_ELEMENT = {
    "id": "abc123",
    "text": "Hello world",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "position": "middle",
}


def _ai_slots(prefix: str) -> list[dict]:
    return [
        {
            "slot_id": "s1",
            "clip_index": 0,
            "source_gcs_path": f"{prefix}clip_0.mp4",
            "source_duration_s": 10.0,
            "in_s": 0.0,
            "duration_s": 1.1,
            "duration_beats": 2,
            "order": 0,
            "moment_energy": 0.8,
            "moment_description": "crowd wave",
        },
        {
            "slot_id": "s2",
            "clip_index": 1,
            "source_gcs_path": f"{prefix}clip_1.mp4",
            "source_duration_s": 10.0,
            "in_s": 1.0,
            "duration_s": 1.9,
            "duration_beats": 3,
            "order": 1,
            "moment_energy": None,
            "moment_description": None,
        },
    ]


def _job(**variant_extra):
    """Fake Job with one editor-eligible montage variant (song_text)."""
    jid = uuid.uuid4()
    prefix = f"generative-jobs/{jid}/sources/"
    variant = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "render_status": "ready",
        "render_finished_at": "2026-07-01T00:00:00Z",
        "base_video_path": f"generative-jobs/{jid}/base_1.mp4",
        "ai_timeline": {"beat_grid": list(GRID), "slots": _ai_slots(prefix)},
        "mix": 0.5,  # lets the mix section validate on this fixture
        **variant_extra,
    }
    return types.SimpleNamespace(
        id=jid,
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": [f"slot-uploads/u/clip_{i}.mp4" for i in range(3)]},
        status="variants_ready",
        mode="content_plan",
    )


def _arm(monkeypatch, *, object_exists=True):
    from app.config import settings

    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True)
    monkeypatch.setattr(gj.storage, "object_exists", lambda p: object_exists)
    # _variants_for_response re-signs base_video_path on read — keep it local.
    monkeypatch.setattr(gj, "signed_get_url", lambda p, ttl=None: f"https://signed/{p}")


def _slot_edits() -> list[gj.TimelineSlotEdit]:
    return [
        gj.TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=0.0, duration_beats=2),
        gj.TimelineSlotEdit(slot_id="s2", clip_index=1, in_s=1.0, duration_beats=3),
    ]


def _commit_req(**kw) -> gj.EditorCommitRequest:
    kw.setdefault("base_generation", "2026-07-01T00:00:00Z")
    return gj.EditorCommitRequest(**kw)


# ── happy path: all sections stage atomically + exactly one render kick ────────


def test_happy_path_persists_all_sections_and_kicks_once(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        timeline_slots=_slot_edits(),
        mix=gj.EditorCommitMix(music_level=0.2, original_level=0.8),
    )

    prep = gj.prepare_editor_commit(job, "song_text", req)

    v = job.assembly_plan["variants"][0]
    # All sections landed in ONE staged variant update.
    assert v["text_elements"][0]["text"] == "Hello world"
    assert v["text_elements_user_edited"] is True
    assert [s["slot_id"] for s in v["user_timeline"]["slots"]] == ["s1", "s2"]
    assert v["mix"] == 0.2
    assert v["original_audio_level"] == 0.8
    # Generation bumped + variant flipped to rendering.
    assert v["render_generation_id"] == prep["generation"]
    assert v["render_status"] == "rendering"
    assert prep["has_render_section"] is True
    assert prep["sections"] == {"text_elements": True, "timeline": True, "mix": True}

    # Exactly ONE render kick, carrying the new token + the timeline override.
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["render_gen_id"] == prep["generation"]
    assert [s["slot_id"] for s in kwargs["timeline_override"]] == ["s1", "s2"]
    assert kwargs["mix_override"] == 0.2
    # Timeline present → full re-assembly → default queue (no overlay-jobs pin).
    assert "queue" not in calls[0]


def test_text_only_commit_rides_overlay_jobs_queue(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    prep = gj.prepare_editor_commit(
        job, "song_text", _commit_req(text_elements=[dict(_VALID_ELEMENT)])
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    assert calls[0]["queue"] == "overlay-jobs"


def test_commit_succeeds_while_variant_is_rendering(monkeypatch):
    """The whole point of E2: NO require_editable_variant — saving during an
    in-flight render supersedes it instead of 409ing."""
    _arm(monkeypatch)
    job = _job(render_status="rendering", render_generation_id="tok-old")
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        base_generation="tok-old",  # baseline = current render_generation_id
    )
    prep = gj.prepare_editor_commit(job, "song_text", req)
    v = job.assembly_plan["variants"][0]
    assert v["render_generation_id"] == prep["generation"] != "tok-old"


# ── stale baseline → 409 baseline_conflict, nothing persisted ──────────────────


def test_stale_base_generation_409_nothing_persisted(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        timeline_slots=_slot_edits(),
        base_generation="something-older",
    )
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 409
    assert exc.value.detail == "baseline_conflict"
    assert job.assembly_plan == before


def test_baseline_prefers_render_generation_id_over_finished_at(monkeypatch):
    """Once a variant is token-stamped, render_finished_at no longer matches."""
    _arm(monkeypatch)
    job = _job(render_generation_id="tok-live")
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job, "song_text", _commit_req(text_elements=[], base_generation="2026-07-01T00:00:00Z")
        )
    assert exc.value.status_code == 409
    # And the stamped token DOES pass.
    prep = gj.prepare_editor_commit(
        job, "song_text", _commit_req(text_elements=[], base_generation="tok-live")
    )
    assert prep["generation"]


# ── invalid section → 422, nothing persisted ───────────────────────────────────


def test_invalid_timeline_section_422_nothing_persisted(monkeypatch):
    """Valid text section + invalid timeline section must persist NEITHER."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_slots = [gj.TimelineSlotEdit(slot_id="s1", clip_index=99, in_s=0.0, duration_s=1.0)]
    req = _commit_req(text_elements=[dict(_VALID_ELEMENT)], timeline_slots=bad_slots)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "TIMELINE_UNKNOWN_CLIP"}
    assert job.assembly_plan == before


def test_invalid_text_section_422_nothing_persisted(monkeypatch):
    """end_s <= start_s in the text section fails the whole commit."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_element = {**_VALID_ELEMENT, "start_s": 2.0, "end_s": 1.0}
    req = _commit_req(text_elements=[bad_element], timeline_slots=_slot_edits())
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 422
    assert job.assembly_plan == before


def test_editor_commit_strictly_rejects_dropped_text_element(monkeypatch):
    """editor-commit must 422 when TextElement coercion would drop user text."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_element = {**_VALID_ELEMENT, "font_family": "Playfair Display"}

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(text_elements=[bad_element], timeline_slots=_slot_edits()),
        )

    assert exc.value.status_code == 422
    assert "invalid" in str(exc.value.detail)
    assert job.assembly_plan == before


def test_mix_on_variant_without_voice_bed_422(monkeypatch):
    _arm(monkeypatch)
    job = _job(mix=None)  # song variant with no voice bed
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job, "song_text", _commit_req(mix=gj.EditorCommitMix(music_level=0.3))
        )
    assert exc.value.status_code == 422


def test_no_sections_422(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(_job(), "song_text", _commit_req())
    assert exc.value.status_code == 422


def test_unknown_variant_404(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(_job(), "nope", _commit_req(text_elements=[dict(_VALID_ELEMENT)]))
    assert exc.value.status_code == 404


def test_title_only_commit_kicks_no_render(monkeypatch):
    """A title-only commit persists nothing on the variant and enqueues nothing."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    prep = gj.prepare_editor_commit(job, "song_text", _commit_req(title="New title"))
    assert prep["has_render_section"] is False
    assert prep["generation"] == "2026-07-01T00:00:00Z"  # baseline unchanged
    assert job.assembly_plan == before

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert calls == []


# ── endpoint (plan-items surface): title in the same transaction ───────────────


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _owned_item(user_id, *, job):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.theme = "Old title"
    item.current_job = job
    item.current_job_id = job.id
    item.user_edited = False
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan, job=None) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_results])

    async def _get(model, _pk, **_kwargs):
        if model is gj.Job:
            return job
        return plan

    db.get = AsyncMock(side_effect=_get)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_endpoint_happy_path_title_and_text(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/editor-commit",
            json={
                "text_elements": [dict(_VALID_ELEMENT)],
                "title": "Fresh title",
                "base_generation": "2026-07-01T00:00:00Z",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["sections"] == {
        "text_elements": True,
        "timeline": False,
        "mix": False,
        "title": True,
    }
    v = job.assembly_plan["variants"][0]
    assert body["generation"] == v["render_generation_id"]
    assert item.theme == "Fresh title"
    assert item.user_edited is True
    db.commit.assert_awaited_once()  # ONE transaction for job-JSON + title
    db.get.assert_any_await(gj.Job, job.id, with_for_update=True)
    regen.apply_async.assert_called_once()


def test_endpoint_stale_baseline_409(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/editor-commit",
            json={
                "text_elements": [dict(_VALID_ELEMENT)],
                "base_generation": "stale-token",
            },
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "baseline_conflict"
    db.commit.assert_not_awaited()
    regen.apply_async.assert_not_called()


def test_endpoint_empty_title_422_nothing_persisted(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/variants/song_text/editor-commit",
        json={
            "text_elements": [dict(_VALID_ELEMENT)],
            "title": "   ",
            "base_generation": "2026-07-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert job.assembly_plan == before
    assert item.theme == "Old title"
    db.commit.assert_not_awaited()


# ── E4: editor_capabilities per archetype ──────────────────────────────────────


def _caps(job, variant_id: str) -> dict:
    v = next(v for v in gj._variants_for_response(job) if v.get("variant_id") == variant_id)
    return v["editor_capabilities"]


def test_capabilities_montage_song_text_all_on(monkeypatch):
    _arm(monkeypatch)
    caps = _caps(_job(), "song_text")
    assert caps == {
        "text_elements": True,
        "timeline": True,
        "split_clips": True,
        "mix": True,  # fixture carries mix=0.5
        "reason": None,
    }


def test_capabilities_lyrics_variant(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="song_lyrics", text_mode="lyrics", mix=None)
    caps = _caps(job, "song_lyrics")
    assert caps["text_elements"] is False
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["mix"] is False
    assert caps["reason"] == "lyrics_sync"


def test_capabilities_voiceover_variant(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="voiceover_music", mix=None)
    caps = _caps(job, "voiceover_music")
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["mix"] is True  # voiceover variants always mixable
    assert caps["reason"] == "voiceover_bed_fit"


def test_capabilities_talking_head_archetype(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="original_text", resolved_archetype="talking_head", mix=None)
    caps = _caps(job, "original_text")
    assert caps["timeline"] is False
    assert caps["reason"] == "no_slot_timeline"
    assert caps["text_elements"] is True  # text editing is independent of the slot grid


def test_capabilities_caption_archetype_text_elements_off(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        variant_id="subtitled",
        text_mode="none",
        resolved_archetype="subtitled",
        mix=None,
    )
    caps = _caps(job, "subtitled")
    assert caps["text_elements"] is False
    assert caps["reason"] == "captions are edited in the captions tab"


def test_capabilities_expired_sources(monkeypatch):
    """Legacy job cutting from 24h-swept uploads → timeline off with the honest reason.
    (Liveness is the persisted prefix check — no GCS call is made here.)"""
    _arm(monkeypatch)
    job = _job()
    for s in job.assembly_plan["variants"][0]["ai_timeline"]["slots"]:
        s["source_gcs_path"] = "slot-uploads/legacy/clip.mp4"
    caps = _caps(job, "song_text")
    assert caps["timeline"] is False
    assert caps["reason"] == "sources_expired"


def test_capabilities_timeline_flag_off(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", False)
    caps = _caps(_job(), "song_text")
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["reason"] == "disabled"
