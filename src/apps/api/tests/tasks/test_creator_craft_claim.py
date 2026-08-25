"""Durable duplicate-delivery protection for creator craft renders."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import app.tasks.generative_build as gb

JOB_ID = "12345678-1234-5678-1234-567812345678"


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        status="processing",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-1",
                    "render_status": "rendering",
                }
            ]
        },
    )


def test_concurrent_creator_craft_delivery_has_one_expensive_render_owner(monkeypatch) -> None:
    job = _job()
    row_lock = threading.Lock()

    class _Session:
        def __init__(self):
            self.locked = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if self.locked:
                row_lock.release()
            return False

        def get(self, *_args, **kwargs):
            if kwargs.get("with_for_update"):
                row_lock.acquire()
                self.locked = True
            return job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_a, **_kw: None)
    barrier = threading.Barrier(2)

    def deliver(task_id: str) -> str:
        barrier.wait(timeout=5)
        return gb._claim_creator_craft_generation(
            JOB_ID,
            "variant-1",
            "generation-1",
            task_id=task_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(
            pool.map(
                deliver,
                ("creator-craft-receipt-generation-1", "creator-craft-receipt-generation-1"),
            )
        )

    assert sorted(states) == ["busy", "claimed"]
    assert job.assembly_plan["variants"][0]["creator_craft_claim"]["generation_id"] == (
        "generation-1"
    )


def test_hard_killed_creator_worker_can_be_reclaimed_after_lease(monkeypatch) -> None:
    job = _job()
    job.assembly_plan["variants"][0]["creator_craft_claim"] = {
        "generation_id": "generation-1",
        "task_id": "creator-craft-dead-worker",
        "claimed_at_epoch_s": time.time() - gb._CREATOR_CRAFT_CLAIM_LEASE_S - 1,
        "heartbeat_at_epoch_s": time.time() - gb._CREATOR_CRAFT_CLAIM_LEASE_S - 1,
    }

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_a, **_kw: None)

    state = gb._claim_creator_craft_generation(
        JOB_ID,
        "variant-1",
        "generation-1",
        task_id="creator-craft-retry-generation-1",
    )

    assert state == "claimed"
    assert (
        job.assembly_plan["variants"][0]["creator_craft_claim"]["task_id"]
        == "creator-craft-retry-generation-1"
    )


def test_duplicate_creator_task_returns_before_expensive_worker(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(gb, "_claim_creator_craft_generation", lambda *_a, **_kw: "busy")
    monkeypatch.setattr(gb, "_owned_job_task_fence", lambda *_a: nullcontext(True))
    monkeypatch.setattr(gb, "_run_regenerate_variant", lambda *_a, **_kw: called.append("render"))

    task = gb.regenerate_generative_variant
    task.push_request(id="creator-craft-receipt-generation-1")
    try:
        task.run(JOB_ID, "variant-1", render_gen_id="generation-1")
    finally:
        task.pop_request()

    assert called == []
