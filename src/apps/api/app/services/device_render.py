"""Immutable phone recipe receipts. Call mutations while holding the Job row lock."""

from __future__ import annotations

import copy
from typing import Any

from app.kria.device_render import DeviceRenderRequest, DeviceRenderStatus

DEVICE_RENDER_FIELD = "_device_render_v1"


def device_record(job: Any, variant_id: str) -> dict:
    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD) or {}
    record = records.get(variant_id)
    if not isinstance(record, dict):
        raise KeyError("device recipe unavailable")
    return copy.deepcopy(record)


def device_status(job: Any, variant_id: str) -> DeviceRenderStatus:
    return DeviceRenderStatus.model_validate(device_record(job, variant_id)["status"])


def save_device_record(job: Any, variant_id: str, record: dict) -> None:
    assembly = copy.deepcopy(job.assembly_plan or {})
    records = assembly.setdefault(DEVICE_RENDER_FIELD, {})
    records[variant_id] = copy.deepcopy(record)
    job.assembly_plan = assembly


def pin_device_request(job: Any, request: DeviceRenderRequest, *, base_generation: str) -> bool:
    """Pin one approved recipe once. A redelivery cannot rewrite that revision's decisions."""
    if request.identity.job_id != job.id:
        raise ValueError("recipe job identity mismatch")
    try:
        previous = device_status(job, request.identity.variant_id).request
    except KeyError:
        previous = None
    if previous is not None:
        if previous == request:
            return False
        if request.identity.recipe_revision <= previous.identity.recipe_revision:
            raise ValueError("recipe revision already pinned")
    save_device_record(
        job,
        request.identity.variant_id,
        {
            "status": DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
                mode="json"
            ),
            "base_generation": base_generation,
            "attempts": {},
        },
    )
    return True
