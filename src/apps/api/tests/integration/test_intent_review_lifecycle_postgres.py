"""Paid intent review lifecycle, from an evidence_ready row to a durable outcome.

Failure modes (written before the flow; each has a test below, matrix row in parentheses):

  stale generation at claim / at the start fence / after the response   (1)
  a change only the input digest sees (dispatch settings), same three points (1)
  ownership epoch or binding changed                                     (2)
  frames from another generation, or bytes that do not match their hash  (3)
  corrupt, oversized, missing or too many frames                         (6)
  provider outcome unknown (5xx, timeout): liability kept, never retried (7)
  provider rejection (4xx, 429): reservation released, never retried     (8)
  estimate above the per-review ceiling; prompt above the context cap    (10, 43)
  monthly capability cap exhausted (including a cap of 0)                (11)
  claim after the start deadline                                         (12)
  paid flag switched off after admission                                 (13)
  internal account without a canary id                                   (15)
  two workers on one row: one provider call, one ledger row              (16)
  ledger cannot record the start fence: release, no call, no liability   (7, 17)
  crash before the start fence: reclaim without liability, then exhaust  (17)
  crash after the start fence: unknown, never re-called                  (18)
  crash after the response is stored: settle from the stored usage       (19)
  ledger settled but response lost                                       (19)
  lost enqueue republished; stale rows expired; evidence tier ignored    (20)
  no row lock held while the provider call is in flight                  (36)
  evidence tier alone never reviews or silences the legacy review        (37, 41)

The only fake is the OpenAI SDK object. Admission, the frame decoder's output contract, the
cost ledger and every state transition run for real on PostgreSQL. The Linux-only ffmpeg
decoder is replaced by synthetic JPEG evidence at its function boundary; its own contract is
covered by test_render_review_evidence.py on Ubuntu.
"""

# ruff: noqa: F811

import asyncio
import base64
import copy
import hashlib
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.agents._openai_vision import OpenAIVisionClient
from app.agents._runtime import (
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
    RunContext,
)
from app.config import settings
from app.database import sync_engine, sync_session
from app.kria import intent_review_tier as tier
from app.kria.render_review_context import TimestampedObservation
from app.models import (
    CreationThread,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorEditDraft,
    CreatorGenerationReview,
    Job,
)
from app.services import ai_cost_control as costs
from app.services import intent_review_flow as flow
from app.services import render_review_evidence as evidence
from app.services.pipeline_trace import pipeline_trace_for
from app.services.render_review_admission import (
    reconcile_generation_reviews,
    run_generation_review,
)
from app.tasks.render_review import review_generation_intent
from tests.kria.test_render_review_admission_postgres import (  # noqa: F401
    admit_case,
    approved_publication,
)
from tests.services.test_openai_vision_transport import HttpError, Sdk, response

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

# The fake provider reports 9100 input (32 cached), 700 output (400 of it reasoning) tokens.
# gpt-6.1-sol bills $2/M input, $0.10/M cached input, $10/M output; reasoning is inside output.
SETTLED_USD = (9100 - 32) * 2e-6 + 32 * 0.10e-6 + 700 * 10e-6
SETTLED_USAGE_JSON = {
    "tokens_in": 9100,
    "tokens_out": 700,
    "tokens_thoughts": 0,
    "tokens_cached": 32,
    "tokens_tool": 0,
    "token_details": {"reasoning_tokens": 400, "output_tokens_inclusive": 700},
    "_budget_capability": "intent_review",
}


class Crash(BaseException):
    """A worker killed mid-flight: nothing in the code under test may catch it."""


class Builder:
    prompt_version = "intent_review_test"

    def __init__(self, prompt="Review the frames against the request."):
        self.prompt = prompt
        self.seen = []

    def build(self, packet, frames):
        self.seen.append((packet, list(frames)))
        return flow.BuiltReview(
            prompt=self.prompt,
            response_schema=SCHEMA,
            catalog={"frames": [frame.frame_id for frame in frames]},
            prompt_version=self.prompt_version,
        )


class Provider(Sdk):
    def __init__(self, outcome=None, *, hook=None, delay=0.0):
        super().__init__(outcome if outcome is not None else response())
        self.hook, self.delay = hook, delay

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.hook:
            self.hook(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    @property
    def client(self):
        return OpenAIVisionClient(client=self)


@pytest.fixture(autouse=True)
def no_live_openai(monkeypatch):
    def refuse(self):
        if self._client is None:
            raise AssertionError("a test constructed a live OpenAI client")
        return self._client

    monkeypatch.setattr(OpenAIVisionClient, "_get", refuse)


def _clean_ledger():
    with sync_engine.begin() as db:
        db.execute(
            text(
                "DELETE FROM ai_cost_reservations "
                "WHERE feature IN ('intent_review', 'seeded_customer_spend')"
            )
        )


@pytest.fixture
def paid(monkeypatch):
    _clean_ledger()
    for name, value in dict(
        kria_intent_review_enabled=True,
        kria_intent_review_paid_enabled=True,
        kria_intent_review_repair_enabled=False,
        kria_intent_review_user_ids=["*"],
        kria_intent_review_canary_id="",
        kria_intent_review_max_usd=0.10,
        openai_api_key="sk-test",
        ai_cost_control_enabled=True,
        ai_usage_environment="production",
        ai_production_monthly_budget_usd=1000.0,
        ai_openai_monthly_budget_usd=500.0,
        ai_openai_intent_review_budget_usd=50.0,
        ai_release_canary_monthly_budget_usd=100.0,
        ai_reservation_ttl_seconds=900,
        ai_unknown_reservation_ttl_seconds=900,
    ).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "review")
    builder = Builder()
    monkeypatch.setattr(flow, "_PROMPT_BUILDER", builder)
    publish = Mock()
    monkeypatch.setattr(review_generation_intent, "apply_async", publish)
    yield SimpleNamespace(publish=publish, builder=builder)
    _clean_ledger()


def _jpeg(shade: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (160, 120), (shade, 40, 90)).save(buffer, "JPEG", quality=60)
    return buffer.getvalue()


def _bundle(proof, count=3):
    frames = []
    for index in range(count):
        data = _jpeg(40 + 50 * index)
        frames.append(
            evidence.FrameEvidence(
                image_bytes=data,
                image_sha256=hashlib.sha256(data).hexdigest(),
                artifact_sha256=proof.sha256,
                timestamp_s=float(index),
                width=160,
                height=120,
                decoder_version="synthetic",
                anchor_ref="title-0" if index == 0 else None,
            )
        )
    observations = tuple(
        TimestampedObservation(
            evidence_id=f"frame-{index}-{frame.image_sha256[:16]}",
            artifact_sha256=proof.sha256,
            start_s=frame.timestamp_s,
            end_s=frame.timestamp_s,
            source="direct_frame",
            observer="render-review-frames-v2",
            observation="synthetic frame",
        )
        for index, frame in enumerate(frames)
    )
    return evidence.EvidenceBundle(
        verified_artifact=proof, frames=tuple(frames), observations=observations
    )


async def ready_review(case, monkeypatch, *, frames=3):
    """Admit through the real observer, then reach evidence_ready through the real runner."""
    rid = await admit_case(case, monkeypatch)
    bundle = _bundle(case[3], frames)
    seen = []

    def synthetic(basis, **kwargs):
        seen.append(kwargs)
        return evidence.EvidenceResult(status="ready", bundle=bundle)

    monkeypatch.setattr(evidence, "obtain_render_review_evidence", synthetic)
    assert await asyncio.to_thread(run_generation_review, rid) == "evidence_ready"
    return rid, bundle, seen


def review_row(rid):
    with sync_session() as db:
        return db.get(CreatorGenerationReview, rid)


def ledger():
    with sync_engine.connect() as db:
        return (
            db.execute(
                text(
                    "SELECT idempotency_key, status, provider, usage_purpose, release_canary_id, "
                    "creator_id, estimated_cost_usd, settled_cost_usd, requested_model, "
                    "provider_request_id, usage_json FROM ai_cost_reservations "
                    "WHERE feature = 'intent_review' ORDER BY created_at"
                )
            )
            .mappings()
            .all()
        )


def edit_row(rid, **values):
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        for name, value in values.items():
            setattr(row, name, value)
        db.commit()


def expire_lease(rid):
    edit_row(rid, lease_until=datetime.now(UTC) - timedelta(seconds=1))


def supersede_generation(job_id):
    with sync_session() as db:
        job = db.get(Job, job_id)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0]["render_generation_id"] = "later-generation"
        job.assembly_plan = assembly
        db.commit()


def bump_ownership(job_id):
    with sync_session() as db:
        db.get(Job, job_id).content_plan_ownership_epoch += 1
        db.commit()


def change_dispatch_settings(job_id):
    """A change `_inputs` accepts without raising: only the stored input digest notices it."""
    with sync_session() as db:
        job = db.get(Job, job_id)
        job.all_candidates = {
            **(job.all_candidates or {}),
            "creator_strategy": {"target_duration_s": 9},
        }
        db.commit()


def ctx_for(row):
    return RunContext(
        creator_id=str(row.owner_id),
        job_id=row.inputs["basis"]["job_id"],
        request_id=f"intent-review:{row.id}",
        request_id_authoritative=True,
    )


def run(rid, provider):
    return flow.run_intent_review(rid, client=provider.client)


async def run_async(rid, provider):
    return await asyncio.to_thread(run, rid, provider)


# --- the happy path ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_call_with_provenance_and_a_settled_ledger_row(
    approved_publication, paid, monkeypatch
):
    _, _, job_id, proof = approved_publication
    rid, bundle, seen = await ready_review(approved_publication, monkeypatch)
    row = review_row(rid)
    assert (row.tier, float(row.max_usd), row.status) == ("review", 0.10, "evidence_ready")
    assert row.evidence_ready_at is not None
    paid.publish.assert_called_once()
    assert paid.publish.call_args.kwargs["task_id"] == f"generation-review:{rid}:intent"
    assert paid.publish.call_args.kwargs["queue"] == "agent-control"
    limits = seen[0]["limits"]
    recipe_layers = {layer["id"] for layer in row.inputs["recipe"]["text_layers"]}
    assert limits.anchor_refs and set(limits.anchor_refs) <= recipe_layers

    provider = Provider(response(text='{"schema_version":1,"findings":[]}'))
    with pipeline_trace_for(job_id):
        assert await run_async(rid, provider) == "unavailable"

    assert len(provider.calls) == 1
    (message,) = provider.calls[0]["input"]
    request_blob = json.dumps(provider.calls[0])
    assert "generativelanguage" not in request_blob and "files/" not in request_blob
    sent = [
        hashlib.sha256(
            base64.b64decode(part["image_url"].removeprefix("data:image/jpeg;base64,"))
        ).hexdigest()
        for part in message["content"][1:]
    ]
    assert sent == [frame.image_sha256 for frame in bundle.frames]

    row = review_row(rid)
    assert (row.status, row.reason, row.review_attempts) == (
        "unavailable",
        "disposition_unavailable",
        1,
    )
    assert row.lease_token is None and row.lease_until is None
    review = row.review
    assert review["version"] == 1 and review["prompt_version"] == "intent_review_test"
    assert review["model"] == "gpt-6.1-sol"
    assert review["catalog"] == {"frames": ["f0", "f1", "f2"]}
    assert review["raw_output"] == '{"schema_version":1,"findings":[]}'
    provenance = review["provenance"]
    assert provenance["attribution"] == {
        "job_id": str(job_id),
        "session_id": str(approved_publication[0].session_id),
        "turn_id": provenance["attribution"]["turn_id"],
        "execution_id": str(approved_publication[0].execution_id),
    }
    assert provenance["frames_sent"] == len(bundle.frames) == 3
    assert provenance["frame_hashes"] == sent
    assert provenance["frame_set_digest"] == hashlib.sha256("\n".join(sent).encode()).hexdigest()
    assert provenance["visual_input_received"] is True
    assert provenance["provider_outcome"] == "ok"
    assert provenance["response_id"] == "resp_1" and provenance["response_status"] == "completed"
    assert provenance["usage"] == {
        "input_tokens": 9100,
        "output_tokens": 700,
        "reasoning_tokens": 400,
        "cached_tokens": 32,
    }
    assert provenance["settled_usd"] == pytest.approx(SETTLED_USD, abs=1e-6)
    assert provenance["settled_usd"] < provenance["reserved_usd"] <= 0.10
    assert provenance["price_version"] == costs.PRICE_VERSION
    (entry,) = ledger()
    assert entry["status"] == "settled" and entry["provider"] == "openai"
    assert entry["idempotency_key"] == row.paid_call_key
    assert entry["usage_purpose"] == "optional_background"
    assert str(entry["creator_id"]) == str(row.owner_id)
    assert provenance["reservation"]["id"] and entry["provider_request_id"] == "resp_1"
    assert float(entry["settled_cost_usd"]) == pytest.approx(SETTLED_USD, abs=1e-6)
    assert entry["usage_json"] == SETTLED_USAGE_JSON
    # The text-only P3 invariant is untouched: only the transport records visual input.
    assert row.result["review_model_received_visual_input"] is False
    assert bundle.review_model_received_visual_input is False
    with sync_engine.connect() as db:
        trace = db.execute(
            text("SELECT pipeline_trace FROM jobs WHERE id = :id"), {"id": str(job_id)}
        ).scalar_one()
        runs = db.execute(
            text("SELECT agent_name, outcome FROM agent_run WHERE job_id = :id"),
            {"id": str(job_id)},
        ).all()
    assert any(event["stage"] == "intent_review" for event in trace)
    assert ("nova.kria.intent_review", "ok") in [tuple(r) for r in runs]


@pytest.mark.asyncio
async def test_incomplete_response_is_settled_and_never_claims_visual_input(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider(response(status="incomplete", reason="max_output_tokens", text="{"))
    assert await run_async(rid, provider) == "unavailable"
    row = review_row(rid)
    assert row.reason == "invalid_output"
    assert row.review["provenance"]["visual_input_received"] is False
    assert row.review["provenance"]["response_status"] == "truncated"
    assert ledger()[0]["status"] == "settled"


@pytest.mark.asyncio
async def test_raw_output_is_capped_and_flagged(approved_publication, paid, monkeypatch):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    await run_async(rid, Provider(response(text="x" * 20_000)))
    review = review_row(rid).review
    assert len(review["raw_output"]) == 16_384 and review["raw_output_truncated"] is True


@pytest.mark.asyncio
async def test_review_task_runs_the_flow_under_the_job_trace(
    approved_publication, paid, monkeypatch
):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider()
    monkeypatch.setattr(
        "app.services.bounded_media_review.OpenAIVisionClient", lambda: provider.client
    )
    result = await asyncio.to_thread(review_generation_intent.run, str(rid))
    assert result == {"review_id": str(rid), "status": "unavailable"}
    assert len(provider.calls) == 1
    with sync_engine.connect() as db:
        trace = db.execute(
            text("SELECT pipeline_trace FROM jobs WHERE id = :id"), {"id": str(job_id)}
        ).scalar_one()
    assert any(event["stage"] == "intent_review" for event in trace)


# --- admission stamps the tier ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ceiling,overrides,expected_tier,published",
    [
        ("evidence", {}, "evidence", False),
        ("review", {}, "review", True),
        ("review", {"kria_intent_review_paid_enabled": False}, "evidence", False),
    ],
)
async def test_admission_stamps_tier_and_only_reviewing_tiers_publish(
    approved_publication, paid, monkeypatch, ceiling, overrides, expected_tier, published
):
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", ceiling)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    row = review_row(rid)
    assert row.tier == expected_tier
    assert (row.max_usd is not None) == published
    assert paid.publish.called is published
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert len(provider.calls) == (1 if published else 0)
    assert review_row(rid).reason == ("disposition_unavailable" if published else "paid_disabled")


@pytest.mark.asyncio
async def test_a_creator_outside_the_allowlist_is_not_admitted_and_does_not_spin(
    approved_publication, paid, monkeypatch
):
    from app.tasks.kria_runtime import _observe_dispatched_execution
    from app.tasks.render_review import prepare_generation_review

    monkeypatch.setattr(settings, "kria_intent_review_user_ids", ["someone-else"])
    monkeypatch.setattr(prepare_generation_review, "apply_async", Mock())
    claim = approved_publication[0]
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        assert (
            db.scalar(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.execution_id == claim.execution_id
                )
            )
            is None
        )
        marker = db.get(CreatorAgentExecution, claim.execution_id).result["intent_review_admission"]
    assert marker["status"] == "not_admitted"


# --- stale authority (rows 1, 2) --------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_at_claim(approved_publication, paid, monkeypatch):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    supersede_generation(job_id)
    provider = Provider()
    assert await run_async(rid, provider) == "stale"
    assert review_row(rid).reason == "generation_changed"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_ownership_epoch_change_is_stale(approved_publication, paid, monkeypatch):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    bump_ownership(job_id)
    provider = Provider()
    assert await run_async(rid, provider) == "stale"
    assert review_row(rid).reason == "ownership_changed"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_stale_at_start_releases_the_reservation_before_any_transfer(
    approved_publication, paid, monkeypatch
):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    real = costs.reserve_paid_call

    def reserve_then_supersede(request):
        receipt = real(request)
        supersede_generation(job_id)
        return receipt

    monkeypatch.setattr(costs, "reserve_paid_call", reserve_then_supersede)
    provider = Provider()
    assert await run_async(rid, provider) == "stale"
    assert review_row(rid).reason == "generation_changed"
    assert provider.calls == []
    assert [entry["status"] for entry in ledger()] == ["released"]


@pytest.mark.asyncio
async def test_stale_after_the_response_keeps_the_text_and_settles_the_ledger(
    approved_publication, paid, monkeypatch
):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider(hook=lambda _: supersede_generation(job_id))
    assert await run_async(rid, provider) == "stale"
    row = review_row(rid)
    assert row.reason == "generation_changed"
    assert row.review["raw_output"] == '{"findings":[]}'
    assert len(provider.calls) == 1
    assert [entry["status"] for entry in ledger()] == ["settled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moment,calls,ledger_states",
    [("claim", 0, []), ("start", 0, ["released"]), ("response", 1, ["settled"])],
)
async def test_a_change_only_the_input_digest_sees_is_stale_at_every_checkpoint(
    approved_publication, paid, monkeypatch, moment, calls, ledger_states
):
    _, _, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    hook = None
    if moment == "claim":
        change_dispatch_settings(job_id)
    elif moment == "start":
        real = costs.reserve_paid_call

        def reserve_then_change(request):
            receipt = real(request)
            change_dispatch_settings(job_id)
            return receipt

        monkeypatch.setattr(costs, "reserve_paid_call", reserve_then_change)
    else:

        def hook(_request):
            change_dispatch_settings(job_id)

    provider = Provider(hook=hook)
    assert await run_async(rid, provider) == "stale"
    row = review_row(rid)
    assert (row.status, row.reason) == ("stale", "generation_changed")
    assert len(provider.calls) == calls
    assert [entry["status"] for entry in ledger()] == ledger_states
    if moment == "response":
        assert row.review["raw_output"] == '{"findings":[]}'


# --- evidence integrity (rows 3, 6) -----------------------------------------------------


def _mutate_evidence(rid, change):
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        result = copy.deepcopy(row.result)
        change(result["evidence"])
        row.result = result
        db.commit()


def _garble(frame):
    data = base64.urlsafe_b64decode(frame["image_bytes"])
    frame["image_bytes"] = base64.urlsafe_b64encode(data[:-1] + bytes([data[-1] ^ 1])).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,reason",
    [
        ("other_generation", "frame_hash_mismatch"),
        ("bytes_swapped", "frame_hash_mismatch"),
        ("packet_other_artifact", "frame_hash_mismatch"),
        ("not_a_jpeg", "media_corrupt"),
        ("oversize", "media_corrupt"),
        ("no_frames", "media_corrupt"),
        ("seven_frames", "media_corrupt"),
        ("not_base64", "media_corrupt"),
    ],
)
async def test_unverifiable_frames_never_reserve_or_transfer(
    approved_publication, paid, monkeypatch, case, reason
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)

    def change(evidence_json):
        frames = evidence_json["frames"]
        if case == "other_generation":
            frames[1]["artifact_sha256"] = "b" * 64
        elif case == "bytes_swapped":
            _garble(frames[1])
        elif case == "not_a_jpeg":
            frames[0]["image_bytes"] = base64.b64encode(b"GIF89a" + b"\x00" * 40).decode()
        elif case == "oversize":
            frames[0]["image_bytes"] = base64.b64encode(
                b"\xff\xd8\xff" + b"\x00" * (128 * 1024)
            ).decode()
        elif case == "no_frames":
            evidence_json["frames"] = []
        elif case == "seven_frames":
            evidence_json["frames"] = frames * 3
        elif case == "not_base64":
            frames[2]["image_bytes"] = "###"

    if case == "packet_other_artifact":
        with sync_session() as db:
            row = db.get(CreatorGenerationReview, rid)
            result = copy.deepcopy(row.result)
            result["packet"]["verified_artifact"]["sha256"] = "c" * 64
            row.result = result
            db.commit()
    else:
        _mutate_evidence(rid, change)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == reason
    assert provider.calls == [] and ledger() == []


# --- provider outcomes (rows 7, 8) ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [HttpError(500), HttpError(None), TimeoutError("timed out")])
async def test_unknown_outcome_holds_liability_and_is_never_retried(
    approved_publication, paid, monkeypatch, failure
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider(failure)
    assert await run_async(rid, provider) == "review_unknown"
    row = review_row(rid)
    assert row.reason == "provider_unknown_outcome"
    assert row.review["provenance"]["provider_outcome"] == "unknown"
    assert [entry["status"] for entry in ledger()] == ["unknown"]
    assert await run_async(rid, provider) == "ignored"
    expire_lease(rid)
    flow.recover_intent_reviews()
    assert len(provider.calls) == 1
    with pytest.raises(ProviderOutcomeUnknownError):
        costs.reserve_paid_call(
            costs.PaidCallRequest(
                idempotency_key=row.paid_call_key,
                feature="intent_review",
                model="gpt-6.1-sol",
                estimated_cost_usd=0.05,
                ctx=ctx_for(row),
                provider="openai",
                capability="intent_review",
            )
        )


@pytest.mark.asyncio
async def test_an_untranslated_client_failure_is_unknown_not_released(
    approved_publication, paid, monkeypatch
):
    class Broken:
        def invoke_images(self, *args, **kwargs):
            raise RuntimeError("the socket died after the request was written")

    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    assert await asyncio.to_thread(flow.run_intent_review, rid, client=Broken()) == "review_unknown"
    assert [entry["status"] for entry in ledger()] == ["unknown"]


@pytest.mark.asyncio
async def test_a_worker_whose_lease_was_taken_over_stops_before_any_spend(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    claim = flow.claim_intent_review(rid)
    edit_row(rid, lease_token=uuid.uuid4())
    provider = Provider()
    assert await asyncio.to_thread(flow._call_review, claim, provider.client) == "ignored"
    assert provider.calls == [] and ledger() == []
    assert review_row(rid).status == "reviewing"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [HttpError(400), HttpError(422), HttpError(429)])
async def test_rejection_releases_the_reservation_and_is_not_retried(
    approved_publication, paid, monkeypatch, failure
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider(failure)
    assert await run_async(rid, provider) == "unavailable"
    row = review_row(rid)
    assert row.reason == "provider_rejected"
    assert row.review["provenance"]["provider_outcome"] == "rejected"
    assert [entry["status"] for entry in ledger()] == ["released"]
    assert await run_async(rid, provider) == "ignored"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,status,reason",
    [
        (
            ProviderOutcomeUnknownError("key already started"),
            "review_unknown",
            "provider_unknown_outcome",
        ),
        (CostControlUnavailableError("ledger down"), "unavailable", "ledger_unavailable"),
        (
            AiCostControlPolicyError(scope="x", reason="creator_attribution_required"),
            "unavailable",
            "paid_gate_closed",
        ),
    ],
    ids=["key-already-started", "ledger-down", "policy"],
)
async def test_a_reserve_that_cannot_complete_never_reaches_the_provider(
    approved_publication, paid, monkeypatch, failure, status, reason
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)

    def refuse(request):
        raise failure

    monkeypatch.setattr(costs, "reserve_paid_call", refuse)
    provider = Provider()
    assert await run_async(rid, provider) == status
    assert review_row(rid).reason == reason
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [CostControlUnavailableError("ledger down"), ProviderOutcomeUnknownError("already started")],
    ids=["ledger-down", "key-already-started"],
)
async def test_a_start_fence_the_ledger_cannot_record_releases_and_never_calls(
    approved_publication, paid, monkeypatch, failure
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)

    def refuse(receipt):
        raise failure

    monkeypatch.setattr(costs, "mark_paid_call_started", refuse)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    row = review_row(rid)
    assert (row.status, row.reason) == ("unavailable", "ledger_unavailable")
    assert row.review["provenance"]["provider_outcome"] == "not_sent"
    assert provider.calls == []
    assert [entry["status"] for entry in ledger()] == ["released"]


# --- cost bounds (rows 10, 11, 12, 13, 43) ----------------------------------------------


@pytest.mark.asyncio
async def test_estimate_over_the_stamped_ceiling_exhausts_without_reserving(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    edit_row(rid, max_usd=0.01)
    provider = Provider()
    assert await run_async(rid, provider) == "exhausted"
    assert review_row(rid).reason == "budget_review"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_prompt_over_the_context_cap_makes_no_call(approved_publication, paid, monkeypatch):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    paid.builder.prompt = "x" * 24_001
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "context_too_large"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_exhausted_capability_budget_rejects_before_any_transfer(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", 0.01)
    provider = Provider()
    assert await run_async(rid, provider) == "exhausted"
    row = review_row(rid)
    assert row.reason == "budget_monthly"
    assert row.review["provenance"]["detail"] == "provider:openai:capability:intent_review"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_a_zero_capability_budget_still_cannot_reserve_if_the_tier_gate_is_bypassed(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", 0)
    monkeypatch.setattr(flow, "intent_review_tier", lambda _: "review")
    provider = Provider()
    assert await run_async(rid, provider) == "exhausted"
    assert review_row(rid).reason == "budget_monthly"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unknown_liability_counts_toward_the_cap_for_the_next_review(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    assert await run_async(rid, Provider(HttpError(500))) == "review_unknown"
    held = float(ledger()[0]["estimated_cost_usd"])
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", held + 0.01)
    second = Provider()
    with sync_session() as db:
        other = db.get(CreatorGenerationReview, rid)
        clone = CreatorGenerationReview(
            id=uuid.uuid4(),
            execution_id=other.execution_id,
            owner_id=other.owner_id,
            review_key=str(uuid.uuid4()),
            input_digest=other.input_digest,
            inputs=copy.deepcopy(other.inputs),
            status="evidence_ready",
            attempts=1,
            result=copy.deepcopy(other.result),
            tier="review",
            max_usd=0.10,
            evidence_ready_at=datetime.now(UTC),
        )
        db.add(clone)
        db.commit()
        clone_id = clone.id
    assert await run_async(clone_id, second) == "exhausted"
    assert review_row(clone_id).reason == "budget_monthly"
    assert second.calls == []


@pytest.mark.asyncio
async def test_claim_after_the_start_deadline_exhausts(approved_publication, paid, monkeypatch):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    edit_row(rid, evidence_ready_at=datetime.now(UTC) - timedelta(seconds=181))
    provider = Provider()
    assert await run_async(rid, provider) == "exhausted"
    assert review_row(rid).reason == "deadline"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_paid_flag_off_after_admission_makes_no_call(approved_publication, paid, monkeypatch):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "kria_intent_review_paid_enabled", False)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "paid_disabled"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_no_prompt_builder_is_unavailable_without_a_call(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(flow, "_PROMPT_BUILDER", None)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "prompt_builder_unavailable"
    assert provider.calls == [] and ledger() == []


# --- attribution (row 15) ---------------------------------------------------------------


def _grant_internal(owner_id):
    with sync_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO internal_account_grants (id, creator_id, status, granted_by, "
                "idempotency_key) VALUES (:id, :creator, 'active', 'pytest', :key)"
            ),
            {"id": str(uuid.uuid4()), "creator": str(owner_id), "key": f"intent-{uuid.uuid4()}"},
        )


@pytest.mark.asyncio
async def test_internal_account_without_a_canary_id_is_unattributed_and_never_calls(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _grant_internal(review_row(rid).owner_id)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "canary_unattributed"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_internal_account_with_a_canary_id_is_booked_to_the_canary(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _grant_internal(review_row(rid).owner_id)
    monkeypatch.setattr(settings, "kria_intent_review_canary_id", "intent-review-2026-11")
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "disposition_unavailable"
    (entry,) = ledger()
    assert (entry["usage_purpose"], entry["release_canary_id"]) == (
        "internal_canary",
        "intent-review-2026-11",
    )


@pytest.mark.asyncio
async def test_advisory_review_yields_to_customer_work_near_the_environment_cap(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    row = review_row(rid)
    costs.reserve_paid_call(
        costs.PaidCallRequest(
            idempotency_key="a-customer-render-already-booked",
            feature="seeded_customer_spend",
            model="gemini-3-flash",
            estimated_cost_usd=850.0,
            ctx=ctx_for(row),
            provider="google",
        )
    )
    provider = Provider()
    assert await run_async(rid, provider) == "exhausted"
    assert review_row(rid).reason == "budget_monthly"
    assert provider.calls == []
    assert ledger() == []


@pytest.mark.asyncio
async def test_outside_production_the_ledger_closes_the_gate_without_an_envelope(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_manual_qa_reservation_approved", False)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    row = review_row(rid)
    assert row.reason == "paid_gate_closed"
    assert row.review["provenance"]["detail"] == "paid_test_attribution_required"
    assert provider.calls == [] and ledger() == []


@pytest.mark.asyncio
async def test_outside_production_the_manual_qa_envelope_admits_the_call(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_development_monthly_budget_usd", 100.0)
    monkeypatch.setattr(settings, "ai_manual_qa_reservation_approved", True)
    monkeypatch.setattr(settings, "ai_manual_qa_test_run_id", f"manual-{uuid.uuid4()}")
    monkeypatch.setattr(settings, "ai_manual_qa_max_cost_usd", 1.0)
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "disposition_unavailable"
    assert len(provider.calls) == 1 and ledger()[0]["usage_purpose"] == "optional_background"


# --- concurrency and locks (rows 16, 36) ------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_workers_make_exactly_one_provider_call(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider(delay=0.8)
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        return run(rid, provider)

    def both():
        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(f.result() for f in [pool.submit(worker), pool.submit(worker)])

    assert await asyncio.to_thread(both) == ["ignored", "unavailable"]
    assert len(provider.calls) == 1
    assert len(ledger()) == 1
    assert review_row(rid).review_attempts == 1


@pytest.mark.asyncio
async def test_no_row_lock_is_held_while_the_provider_call_is_in_flight(
    approved_publication, paid, monkeypatch
):
    claim, thread, job_id, _ = approved_publication
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    with sync_session() as db:
        draft_id = db.get(CreatorAgentExecution, claim.execution_id).target_draft_id
    contended = []

    def probe(_):
        targets = [
            (Job, job_id),
            (CreationThread, thread),
            (CreatorAgentSession, claim.session_id),
            (CreatorAgentExecution, claim.execution_id),
            (CreatorEditDraft, draft_id),
            (CreatorGenerationReview, rid),
        ]
        for model, key in targets:
            with sync_session() as db:
                try:
                    db.execute(
                        select(model.id).where(model.id == key).with_for_update(nowait=True)
                    ).one()
                except OperationalError:
                    contended.append(model.__name__)

    assert await run_async(rid, Provider(hook=probe)) == "unavailable"
    assert contended == []


# --- crashes (rows 17-19) ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_after_claim_reclaims_without_liability(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    assert isinstance(flow.claim_intent_review(rid), flow.Claim)
    row = review_row(rid)
    assert (row.status, row.review_attempts) == ("reviewing", 1)
    flow.recover_intent_reviews()
    assert review_row(rid).status == "reviewing"
    expire_lease(rid)
    paid.publish.reset_mock()
    flow.recover_intent_reviews()
    row = review_row(rid)
    assert row.status == "evidence_ready"
    with sync_engine.connect() as db:
        assert db.execute(
            text("SELECT review IS NULL FROM creator_generation_reviews WHERE id = :id"),
            {"id": str(rid)},
        ).scalar_one()
    assert paid.publish.call_args.kwargs["task_id"] == f"generation-review:{rid}:intent"
    assert ledger() == []
    provider = Provider()
    assert await run_async(rid, provider) == "unavailable"
    assert len(provider.calls) == 1 and review_row(rid).review_attempts == 2


def _crash_once(monkeypatch, target, name):
    real = getattr(target, name)
    state = {"armed": True}

    def wrapper(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise Crash()
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)


@pytest.mark.asyncio
async def test_crash_between_reserve_and_start_releases_then_retries_once(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _crash_once(monkeypatch, costs, "mark_paid_call_started")
    provider = Provider()
    with pytest.raises(Crash):
        await run_async(rid, provider)
    assert [entry["status"] for entry in ledger()] == ["reserved"]
    expire_lease(rid)
    flow.recover_intent_reviews()
    assert review_row(rid).status == "evidence_ready"
    assert [entry["status"] for entry in ledger()] == ["released"]
    assert await run_async(rid, provider) == "unavailable"
    assert len(provider.calls) == 1
    assert [entry["status"] for entry in ledger()] == ["settled"]
    assert review_row(rid).review_attempts == 2


@pytest.mark.asyncio
async def test_two_pre_transfer_crashes_exhaust_the_attempts(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    provider = Provider()
    for _ in range(2):
        _crash_once(monkeypatch, costs, "mark_paid_call_started")
        with pytest.raises(Crash):
            await run_async(rid, provider)
        expire_lease(rid)
        flow.recover_intent_reviews()
    row = review_row(rid)
    assert (row.status, row.reason, row.review_attempts) == ("exhausted", "attempts", 2)
    assert provider.calls == []
    assert [entry["status"] for entry in ledger()] == ["released"]


@pytest.mark.asyncio
async def test_crash_after_the_start_fence_is_unknown_and_never_recalled(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)

    def die(_):
        raise Crash()

    provider = Provider(hook=die)
    with pytest.raises(Crash):
        await run_async(rid, provider)
    assert [entry["status"] for entry in ledger()] == ["provider_started"]
    assert flow.recover_intent_reviews() == 0
    expire_lease(rid)
    flow.recover_intent_reviews()
    row = review_row(rid)
    assert (row.status, row.reason) == ("review_unknown", "provider_unknown_outcome")
    assert [entry["status"] for entry in ledger()] == ["unknown"]
    assert await run_async(rid, provider) == "ignored"
    flow.recover_intent_reviews()
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_crash_after_the_response_is_stored_settles_from_the_stored_usage(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _crash_once(monkeypatch, costs, "settle_paid_call")
    provider = Provider()
    with pytest.raises(Crash):
        await run_async(rid, provider)
    assert [entry["status"] for entry in ledger()] == ["provider_started"]
    assert review_row(rid).review["raw_output"] == '{"findings":[]}'
    expire_lease(rid)
    flow.recover_intent_reviews()
    row = review_row(rid)
    assert (row.status, row.reason) == ("unavailable", "disposition_unavailable")
    (entry,) = ledger()
    assert entry["status"] == "settled"
    assert float(entry["settled_cost_usd"]) == pytest.approx(SETTLED_USD, abs=1e-6)
    assert entry["usage_json"] == SETTLED_USAGE_JSON
    assert row.review["provenance"]["settled_usd"] == pytest.approx(SETTLED_USD, abs=1e-6)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_settled_ledger_row_without_a_stored_response_is_a_lost_response(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    claim = flow.claim_intent_review(rid)
    receipt = costs.reserve_paid_call(
        costs.PaidCallRequest(
            idempotency_key=claim.paid_call_key,
            feature="intent_review",
            model="gpt-6.1-sol",
            estimated_cost_usd=0.05,
            ctx=ctx_for(review_row(rid)),
            provider="openai",
            capability="intent_review",
        )
    )
    costs.mark_paid_call_started(receipt)
    costs.settle_paid_call(receipt, costs.UsageMeter(tokens_in=100, tokens_out=10))
    expire_lease(rid)
    flow.recover_intent_reviews()
    row = review_row(rid)
    assert (row.status, row.reason) == ("review_unknown", "response_lost")


# --- republish and expiry (row 20) ------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_republishes_a_lost_enqueue_and_expires_a_late_one(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    paid.publish.reset_mock()
    reconcile_generation_reviews()
    paid.publish.assert_not_called()
    edit_row(
        rid,
        evidence_ready_at=datetime.now(UTC) - timedelta(seconds=60),
        last_publish_attempt_at=datetime.now(UTC) - timedelta(seconds=60),
    )
    reconcile_generation_reviews()
    assert paid.publish.call_args.kwargs["task_id"] == f"generation-review:{rid}:intent"
    paid.publish.reset_mock()
    reconcile_generation_reviews()
    paid.publish.assert_not_called()
    edit_row(
        rid,
        evidence_ready_at=datetime.now(UTC) - timedelta(seconds=200),
        last_publish_attempt_at=None,
    )
    reconcile_generation_reviews()
    paid.publish.assert_not_called()
    row = review_row(rid)
    assert (row.status, row.reason) == ("exhausted", "deadline")


@pytest.mark.asyncio
async def test_evidence_tier_rows_are_never_republished_or_expired(
    approved_publication, paid, monkeypatch
):
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "evidence")
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    edit_row(rid, evidence_ready_at=datetime.now(UTC) - timedelta(seconds=600))
    reconcile_generation_reviews()
    paid.publish.assert_not_called()
    assert review_row(rid).status == "evidence_ready"


# --- review-finding regressions ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["repair_staged", "repair_verified", "repair_reverted"])
async def test_deleting_the_repair_draft_with_its_project_succeeds(
    approved_publication, paid, monkeypatch, status
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    row = review_row(rid)
    edit_row(
        rid,
        status=status,
        reason="repair",
        repair_draft_id=uuid.UUID(row.inputs["draft_id"]),
        repair_staged_at=datetime.now(UTC),
        lease_token=None,
        lease_until=None,
    )
    with sync_session() as db:
        db.execute(
            text("DELETE FROM creator_edit_drafts WHERE id = :id"), {"id": row.inputs["draft_id"]}
        )
        db.commit()
    with sync_session() as db:
        assert db.get(CreatorGenerationReview, rid) is None


@pytest.mark.asyncio
async def test_a_retry_after_a_full_lease_still_gets_its_one_review(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _crash_once(monkeypatch, costs, "mark_paid_call_started")
    provider = Provider()
    with pytest.raises(Crash):
        await run_async(rid, provider)
    lease_elapsed = datetime.now(UTC) - timedelta(seconds=flow.LEASE_S + 5)
    edit_row(
        rid, lease_until=datetime.now(UTC) - timedelta(seconds=1), evidence_ready_at=lease_elapsed
    )
    flow.recover_intent_reviews()
    assert review_row(rid).status == "evidence_ready"
    assert await run_async(rid, provider) == "unavailable"
    assert len(provider.calls) == 1
    assert review_row(rid).reason == "disposition_unavailable"


@pytest.mark.asyncio
async def test_crash_after_settlement_records_the_real_cost_everywhere(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    _crash_once(monkeypatch, flow, "_finish")
    provider = Provider()
    with pytest.raises(Crash):
        await run_async(rid, provider)
    assert [entry["status"] for entry in ledger()] == ["settled"]
    expire_lease(rid)
    flow.recover_intent_reviews()
    row = review_row(rid)
    assert row.review["provenance"]["settled_usd"] == pytest.approx(SETTLED_USD, abs=1e-6)
    with sync_engine.connect() as db:
        recorded = db.execute(
            text(
                "SELECT settled_cost_usd, cost_usd FROM agent_run "
                "WHERE agent_name = 'nova.kria.intent_review' ORDER BY created_at DESC LIMIT 1"
            )
        ).one()
    assert float(recorded[0]) == pytest.approx(SETTLED_USD, abs=1e-6)
    assert float(recorded[1]) == pytest.approx(SETTLED_USD, abs=1e-6)


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "reviewed"), [(1, False), (2, True)])
async def test_one_creator_cannot_spend_past_the_daily_review_cap(
    approved_publication, paid, monkeypatch, limit, reviewed
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "kria_intent_review_daily_limit_per_user", limit)
    row = review_row(rid)
    costs.reserve_paid_call(
        costs.PaidCallRequest(
            idempotency_key="an-earlier-review-today",
            feature="intent_review",
            model="gpt-6.1-sol",
            estimated_cost_usd=0.05,
            ctx=ctx_for(row),
            provider="openai",
            capability="intent_review",
        )
    )
    provider = Provider()
    outcome = await run_async(rid, provider)
    if reviewed:
        assert outcome == "unavailable" and len(provider.calls) == 1
    else:
        assert outcome == "exhausted" and provider.calls == []
        row = review_row(rid)
        assert row.reason == "budget_user_day"
        assert [entry["status"] for entry in ledger()] == ["reserved"]


def _generation_of(case):
    claim, _, job_id, _ = case
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        return SimpleNamespace(
            session_id=execution.session_id,
            job_id=job_id,
            variant_id=execution.target_variant_id,
            generation_id=execution.result["render_generation_id"],
        )


def _retired(target, generation_id=None):
    from app.services.intent_review_legacy import legacy_review_retired, retirement_probe

    with sync_session() as db:
        return legacy_review_retired(
            db.execute(
                retirement_probe(
                    target.session_id,
                    job_id=target.job_id,
                    variant_id=target.variant_id,
                    generation_id=generation_id or target.generation_id,
                )
            ).first()
        )


@pytest.mark.asyncio
async def test_the_legacy_review_returns_when_the_intent_review_gave_no_verdict(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    target = _generation_of(approved_publication)
    assert _retired(target) is True
    for status in ("unavailable", "exhausted", "review_unknown", "stale"):
        edit_row(rid, status=status, reason="no_verdict")
        assert _retired(target) is False, status
    for status in ("no_findings", "suggestion", "needs_confirmation"):
        edit_row(rid, status=status, reason="verdict")
        assert _retired(target) is True, status
    monkeypatch.setattr(settings, "kria_intent_review_paid_enabled", False)
    assert _retired(target) is False


@pytest.mark.asyncio
async def test_another_generations_review_never_retires_the_legacy_review_of_this_one(
    approved_publication, paid, monkeypatch
):
    await ready_review(approved_publication, monkeypatch)
    target = _generation_of(approved_publication)
    assert _retired(target) is True
    assert _retired(target, generation_id="a-generation-with-no-intent-review") is False


def _queue_legacy_review(case, monkeypatch):
    """The poll that saw the ready render before the intent review's admission committed."""
    from app.tasks import creator_quality_review as legacy

    target = _generation_of(case)
    publish = Mock()
    monkeypatch.setattr(legacy.quality_review_creator_session, "apply_async", publish)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, target.session_id)
        session.phase = "awaiting_feedback"
        session.target_job_id = target.job_id
        session.target_variant_id = target.variant_id
        session.target_generation_id = target.generation_id
        session.last_review = None
        legacy.queue_creator_quality_review(
            session,
            job_id=str(target.job_id),
            variant_id=target.variant_id,
            render_generation_id=target.generation_id,
        )
        db.commit()
    publish.assert_called_once()
    return target, publish


def _receipt(session_id):
    with sync_session() as db:
        return dict(db.get(CreatorAgentSession, session_id).last_review or {})


async def _poll(session_id):
    from app.database import AsyncSessionLocal
    from app.services.creator_sessions import reconcile_render_state

    async with AsyncSessionLocal() as db:
        session = await db.get(CreatorAgentSession, session_id, with_for_update=True)
        changed = await reconcile_render_state(db, session)
        await db.commit()
        return changed


@pytest.mark.asyncio
async def test_a_legacy_review_retired_at_claim_closes_its_receipt_and_returns_on_no_verdict(
    approved_publication, paid, monkeypatch
):
    from app.tasks.creator_quality_review import claim_exact_review

    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    target, publish = _queue_legacy_review(approved_publication, monkeypatch)
    assert _receipt(target.session_id)["dispatch_status"] == "queued"

    with sync_session() as db:
        claimed = claim_exact_review(
            db,
            session_id=str(target.session_id),
            job_id=str(target.job_id),
            variant_id=target.variant_id,
            generation_id=target.generation_id,
        )
    assert claimed is None
    receipt = _receipt(target.session_id)
    assert receipt["status"] == "unavailable"
    assert receipt["error_code"] == "legacy_review_retired"
    assert receipt["dispatch_status"] != "queued"

    await _poll(target.session_id)
    publish.assert_called_once()

    edit_row(rid, status="unavailable", reason="no_verdict")
    await _poll(target.session_id)
    assert publish.call_count == 2
    assert _receipt(target.session_id)["dispatch_status"] == "queued"
