"""KRI-470 PR-G: a contract failure never replaces the last accepted phone artifact.

An approved edit is published; the creator then edits it. The edit is checked against
the frozen creator requirements at three points: the editor Save / `pin_device_request`,
the retry re-pin, and the publication of the export. A failure at any of them must leave
the variant's published video / poster / URL and the device record's pinned request,
contract receipts and published attempt exactly as they were, and record WHY beside them.

Failure modes, written before the code:

* an edit that breaks an approved requirement is pinned over the published state
* the refusal is lost (nothing says what was refused) or recorded in place of state
* a refused publication leaves the creator waiting forever (no failure ever surfaces)
* a transient refusal poisons the next valid attempt
* a valid edit stops replacing the artifact (the guard became a wall)
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services import device_render as svc
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import (
    CONTRACT_DECLINE_FIELD,
    CONTRACT_REVISIONS_FIELD,
    contract_decline,
    device_record,
    device_status,
    save_device_record,
)
from tests.routes import test_device_render as _device_routes
from tests.routes.test_device_render import body, mock_storage, routes, scalar
from tests.routes.test_phone_editor_commit import phone_job, save

# pytest resolves fixtures by name: reuse the route suite's TestClient fixture as is.
fixture = _device_routes.fixture
VARIANT = "guided_story"
PUBLISHED_PATH = "user/job/device/attempt-1.mp4"


def _snapshot(job) -> str:
    """Byte-for-byte: every JSON byte of the job's plan."""
    return json.dumps(job.assembly_plan, sort_keys=True)


def _published_job(monkeypatch):
    """A phone job whose approved edit (opening title 'Before') is already published."""
    job = phone_job(monkeypatch)
    contract = build_render_contract({"opening_title": "Before"}, generation_id="first")
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "published"
    record.update(published_attempt="attempt-1", base_generation="attempt-1")
    # Re-pin under the published attempt like `complete_device_export` does.
    save_device_record(job, VARIANT, record)
    job.assembly_plan["variants"][0].update(
        ok=True,
        render_status="ready",
        render_generation_id="attempt-1",
        video_path=PUBLISHED_PATH,
        poster_path="job-posters/attempt-1.jpg",
        output_url="https://storage.example/attempt-1.mp4",
        render_destination="device",
    )
    job.status = "variants_ready"
    return job


def test_an_accepted_edit_replaces_the_pinned_state_and_only_the_pin(monkeypatch):
    job = _published_job(monkeypatch)
    published = copy.deepcopy(job.assembly_plan["variants"][0])

    save(job, generation="attempt-1", text="After", start_s=0, end_s=3)

    assert device_status(job, VARIANT).request.identity.recipe_revision == 2
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"
    # The previous artifact stays on the variant until the new export is published.
    for key in ("video_path", "poster_path", "output_url"):
        assert variant[key] == published[key]
    revised = read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]}
    )
    assert [item.text for item in revised.exact_texts] == ["After"]


def test_a_contract_failing_edit_leaves_the_published_artifact_byte_identical(monkeypatch):
    job = _published_job(monkeypatch)
    before = _snapshot(job)

    with pytest.raises(HTTPException) as caught:
        save(job, generation="attempt-1", text="Before", start_s=1.0, end_s=3.0)

    assert caught.value.status_code == 422
    assert _snapshot(job) == before  # request, receipts, published attempt, video, poster, URL
    assert device_status(job, VARIANT).phase == "published"


def test_pinning_a_recipe_that_breaks_the_contract_never_touches_the_record(monkeypatch):
    job = _published_job(monkeypatch)
    before = _snapshot(job)
    request = device_status(job, VARIANT).request
    # Same recipe, next revision, but the approved text is now absent from it.
    stripped = request.recipe.model_copy(update={"text_layers": []})
    from app.kria.device_render import make_device_request

    revised = make_device_request(job_id=job.id, variant_id=VARIANT, revision=2, recipe=stripped)
    with pytest.raises(CreatorRenderContractError) as caught:
        svc.pin_device_request(job, revised, base_generation="first")
    assert caught.value.decline_reason == "evidence_missing"
    assert caught.value.field_path == "opening_title"
    assert _snapshot(job) == before


def test_a_transient_refusal_does_not_poison_the_next_valid_attempt(monkeypatch):
    job = _published_job(monkeypatch)
    before = _snapshot(job)
    real = svc._approved_source_bindings
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CreatorRenderContractError("I couldn't verify this edit's approved source files.")
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "_approved_source_bindings", flaky)

    with pytest.raises(HTTPException):
        save(job, generation="attempt-1", text="After", start_s=0, end_s=3)
    assert _snapshot(job) == before

    save(job, generation="attempt-1", text="After", start_s=0, end_s=3)  # the cause cleared
    assert device_status(job, VARIANT).request.identity.recipe_revision == 2


def test_the_guard_is_what_protects_the_artifact(monkeypatch):
    """Mutation control: with the contract check off, the same failing edit replaces the
    pinned state. The tests above therefore depend on the guard, not on luck."""
    job = _published_job(monkeypatch)
    before = _snapshot(job)
    monkeypatch.setattr(svc, "_verify_contract_pin", lambda *a, **k: None)
    monkeypatch.setattr("app.services.phone_editor.pin_device_request", svc.pin_device_request)

    save(job, generation="attempt-1", text="Before", start_s=1.0, end_s=3.0)

    assert _snapshot(job) != before
    assert device_status(job, VARIANT).request.identity.recipe_revision == 2


# --- publication ------------------------------------------------------------------


def _syncing(fixture, monkeypatch):
    """The route fixture re-pointed at a published phone job that is being edited."""
    job = _published_job(monkeypatch)
    fixture.job = job
    fixture.user.id = job.user_id
    prep = save(job, generation="attempt-1", text="After", start_s=0, end_s=3)
    assert prep["render_destination"] == "device"
    attempt = str(uuid.uuid4())
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "syncing"
    record["attempts"][attempt] = {
        "path": f"{job.user_id}/{job.id}/device/{attempt}.mp4",
        "size": 12,
        "sha256": "a" * 64,
    }
    save_device_record(job, VARIANT, record)
    fixture.request = device_status(job, VARIANT).request
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value="job-posters/new.jpg"))
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=job.user_id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    return attempt


def _artifact(job) -> dict:
    variant = job.assembly_plan["variants"][0]
    return {
        key: variant.get(key)
        for key in ("ok", "video_path", "poster_path", "output_url", "render_generation_id")
    }


def test_a_publication_the_contract_no_longer_proves_keeps_the_last_good_artifact(
    fixture, monkeypatch
):
    attempt = _syncing(fixture, monkeypatch)
    job = fixture.job
    good = _artifact(job)
    receipts = copy.deepcopy(device_record(job, VARIANT)["contract_receipts"])
    # The approved authority changes underneath the pinned recipe.
    other = build_render_contract({"opening_title": "Something else"}, generation_id="second")
    job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT] = other.model_dump(mode="json")

    response = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 409
    assert _artifact(job) == good  # the previously published output is still the output
    record = device_record(job, VARIANT)
    assert record["status"]["phase"] == "needs_attention"  # the creator will hear about it
    assert record["contract_receipts"] == receipts
    decline = contract_decline(job, VARIANT)
    assert decline["decline_reason"] == "evidence_missing"
    assert decline["stage"] == "publication"
    assert decline["message"]
    fixture.db.commit.assert_awaited()


def test_a_publication_that_still_proves_its_contract_replaces_the_artifact(fixture, monkeypatch):
    attempt = _syncing(fixture, monkeypatch)
    job = fixture.job

    response = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 200, response.text
    variant = job.assembly_plan["variants"][0]
    assert variant["video_path"].endswith(f"{attempt}.mp4")
    assert variant["poster_path"] == "job-posters/new.jpg"
    assert variant["render_generation_id"] == attempt
    assert CONTRACT_DECLINE_FIELD not in device_record(job, VARIANT)


def test_a_retry_that_cannot_be_re_pinned_records_why_and_keeps_the_state(fixture, monkeypatch):
    attempt = _syncing(fixture, monkeypatch)  # noqa: F841
    job = fixture.job
    svc.mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    good = _artifact(job)
    before_request = device_status(job, VARIANT).request

    real = svc._verify_contract_pin
    calls = {"n": 0}

    def refuse_the_repin(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the route's own check of the record still passes
            return real(*args, **kwargs)
        raise CreatorRenderContractError(
            "This edit couldn't keep the confirmed clip order.",
            decline_reason="evidence_missing",
            field_path="ordering_choice",
        )

    monkeypatch.setattr(svc, "_verify_contract_pin", refuse_the_repin)
    response = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/retry",
        json={"identity": before_request.identity.model_dump(mode="json")},
    )

    assert response.status_code == 409
    assert _artifact(job) == good
    assert device_status(job, VARIANT).request == before_request
    decline = contract_decline(job, VARIANT)
    assert (decline["decline_reason"], decline["field_path"], decline["stage"]) == (
        "evidence_missing",
        "ordering_choice",
        "retry",
    )


# --- a FIRST render the contract refuses is not left waiting ---------------------------


def _first_render_syncing(fixture, monkeypatch):
    """No accepted artifact exists yet: the job's first export is being published."""
    job = phone_job(monkeypatch)
    contract = build_render_contract({"opening_title": "Before"}, generation_id="first")
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    fixture.job = job
    fixture.user.id = job.user_id
    attempt = str(uuid.uuid4())
    # Pin under the contract (the worker did), then it starts syncing.
    from app.kria.device_render import make_device_request

    previous = device_status(job, VARIANT).request
    svc.pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=VARIANT, revision=2, recipe=previous.recipe),
        base_generation="first",
    )
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "syncing"
    record["attempts"][attempt] = {
        "path": f"{job.user_id}/{job.id}/device/{attempt}.mp4",
        "size": 12,
        "sha256": "a" * 64,
    }
    save_device_record(job, VARIANT, record)
    fixture.request = device_status(job, VARIANT).request
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value=None))
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=job.user_id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    # The approved authority changes underneath the pinned recipe.
    other = build_render_contract({"opening_title": "Something else"}, generation_id="second")
    job.assembly_plan[CONTRACT_FIELD] = other.model_dump(mode="json")
    return attempt


def test_a_refused_first_render_fails_visibly_instead_of_waiting_for_a_phone_forever(
    fixture, monkeypatch
):
    attempt = _first_render_syncing(fixture, monkeypatch)
    job = fixture.job
    assert not job.assembly_plan["variants"][0].get("video_path")

    response = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 409
    variant = job.assembly_plan["variants"][0]
    assert (variant["ok"], variant["render_status"]) == (False, "needs_attention")
    assert variant["decline_reason"] == "evidence_missing"
    assert job.failure_reason == "creator_render_contract_unverified"
    assert job.error_detail
    assert not variant.get("video_path")  # nothing was published
    record = device_record(job, VARIANT)
    assert record["status"]["phase"] == "needs_attention"
    # The reaper only rescans awaiting_device / syncing records.
    from app.tasks.device_render_reaper import _STALE_PHASES

    assert record["status"]["phase"] not in _STALE_PHASES


def test_a_repeated_refusal_of_a_first_render_changes_nothing_more(fixture, monkeypatch):
    attempt = _first_render_syncing(fixture, monkeypatch)
    job = fixture.job
    post = lambda: fixture.client.post(  # noqa: E731
        f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert post().status_code == 409
    variant_once = copy.deepcopy(job.assembly_plan["variants"][0])
    failure_once = (job.failure_reason, job.error_detail)

    assert post().status_code == 409

    assert job.assembly_plan["variants"][0] == variant_once
    assert (job.failure_reason, job.error_detail) == failure_once


def test_a_refused_edit_keeps_the_previous_artifact_and_the_job_state(fixture, monkeypatch):
    attempt = _syncing(fixture, monkeypatch)
    job = fixture.job
    other = build_render_contract({"opening_title": "Something else"}, generation_id="second")
    job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT] = other.model_dump(mode="json")
    before = (job.status, getattr(job, "failure_reason", None), _artifact(job))
    render_status = job.assembly_plan["variants"][0]["render_status"]

    assert (
        fixture.client.post(
            f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
        ).status_code
        == 409
    )

    assert (job.status, getattr(job, "failure_reason", None), _artifact(job)) == before
    assert job.assembly_plan["variants"][0]["render_status"] == render_status
