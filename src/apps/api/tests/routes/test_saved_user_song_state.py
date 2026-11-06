"""Saved intent must survive recipe failure without trusting a stale render plan.

Failure cases: old recipe resurrects removal, intent is exposed for the wrong
generation/approval, a client-injected marker is trusted, or read projection mutates storage.
"""

import copy
import socket

import pytest

from app.pipeline.editor_render_window import RenderWindowUnavailable
from app.routes import generative_jobs as gj
from app.services import phone_editor
from tests.routes.test_phone_song_editor_commit import _phone_profile, background_job  # noqa: F401


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("external calls forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(gj, "signed_get_url", lambda *a, **k: "https://signed.test/media")
    monkeypatch.setattr(
        gj.storage, "signed_download_url", lambda *a, **k: "https://signed.test/media"
    )


def saved_recovery(monkeypatch, **song):
    job, _ = background_job()
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        editor_state_version=2,
        user_song=gj.EditorCommitUserSong(**song),
    )

    def unavailable(*args, **kwargs):
        raise RenderWindowUnavailable("Reconnect the source")

    monkeypatch.setattr(phone_editor, "_compile_editor_commit", unavailable)
    prep = gj.prepare_editor_commit(job, "guided_story", payload)
    assert prep["render_recovery"]
    assert gj._variant_user_song(job, job.assembly_plan["variants"][0]) is not None
    return job


@pytest.mark.parametrize("song", [{"removed": True}, {"volume": 0.3, "window_start_s": 2}])
def test_status_and_timeline_expose_saved_intent_despite_old_song_plan(monkeypatch, song):
    job = saved_recovery(monkeypatch, **song)
    before = copy.deepcopy(job.assembly_plan)
    variant = job.assembly_plan["variants"][0]
    public = gj._variants_for_response(job)[0]
    marker = public["saved_user_song"]
    assert marker["job_id"] == str(job.id)
    assert marker["variant_id"] == "guided_story"
    assert marker["generation"] == gj.variant_render_baseline(variant)
    assert marker["revision_number"] == variant["guided_edit_revision"]["revision_number"]
    for key, value in song.items():
        assert marker["state"][key] == value
    assert gj.GenerativeVariant.model_validate(public).saved_user_song is not None
    timeline = gj.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda *_: "https://signed.test/media"
    )
    assert (
        gj.TimelineResponse.model_validate(timeline).saved_user_song.model_dump(mode="json")
        == marker
    )
    assert job.assembly_plan == before


def test_marker_requires_current_generation_and_valid_persisted_approval(monkeypatch):
    from app.services.saved_user_song import saved_user_song_for_variant

    job = saved_recovery(monkeypatch, removed=True)
    raw = job.assembly_plan["variants"][0]
    assert saved_user_song_for_variant(job, {**raw, "render_generation_id": "other"}) is None
    raw["guided_edit_revision"]["approval_version"] = -1
    assert saved_user_song_for_variant(job, raw) is None


def test_missing_revision_cannot_echo_a_stored_or_client_marker():
    from app.services.saved_user_song import saved_user_song_for_variant

    job, _ = background_job()
    raw = job.assembly_plan["variants"][0]
    raw["saved_user_song"] = {"state": {"removed": True}}
    assert saved_user_song_for_variant(job, raw) is None
    assert "saved_user_song" not in gj._variants_for_response(job)[0]
