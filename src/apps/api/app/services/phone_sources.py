"""Bind approved analysis decisions to device originals using stored upload receipts.

Only call with server-owned PlanItem assignments, never request-body dictionaries.
The binding is private job state; portable recipes expose only original identities.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.media_sources import (
    MediaUploadContract,
    OriginalMediaDescriptor,
    is_analysis_proxy_path,
)
from app.kria.render_assets import OriginalRenderAsset, RenderFingerprint

PHONE_SOURCES_FIELD = "_phone_sources_v1"


class PhoneSourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    media_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    proxy_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    original: OriginalMediaDescriptor

    @model_validator(mode="after")
    def require_proxy(self) -> PhoneSourceBinding:
        if not is_analysis_proxy_path(self.proxy_path):
            raise ValueError("phone source requires a reserved analysis proxy")
        return self

    def render_asset(self) -> OriginalRenderAsset:
        return OriginalRenderAsset(
            id=self.media_id,
            media_id=self.media_id,
            fingerprint=RenderFingerprint(
                sha256=self.original.sha256, byte_count=self.original.byte_count
            ),
        )


def bind_phone_sources(
    assignments: list[dict], selected_paths: list[str]
) -> tuple[PhoneSourceBinding, ...]:
    """Require one unambiguous immutable receipt for every selected proxy.

    Mixed cloud/device sources require a separate explicit consent flow. Reject
    them here, as well as stale or conflicting assignments, before job creation.
    """
    if not selected_paths or len(set(selected_paths)) != len(selected_paths):
        raise ValueError("phone source selection must be nonempty and unique")
    by_path: dict[str, PhoneSourceBinding] = {}
    by_id: dict[str, PhoneSourceBinding] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict) or assignment.get("gcs_path") not in selected_paths:
            continue
        contract = MediaUploadContract.model_validate(assignment.get("upload_contract") or {})
        if contract.purpose != "analysis_proxy" or contract.proxy is None:
            raise ValueError("phone source lacks its verified proxy receipt")
        duration = assignment.get("duration_s")
        has_audio = assignment.get("has_audio")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not isinstance(has_audio, bool)
        ):
            raise ValueError("phone source lacks verified media metadata")
        import math

        if not math.isfinite(duration):
            raise ValueError("phone source duration must be finite")
        contract.proxy.verify_registered(duration, has_audio)
        binding = PhoneSourceBinding(
            media_id=assignment.get("media_id"),
            proxy_path=assignment["gcs_path"],
            generation=assignment.get("storage_generation"),
            original=contract.proxy.original,
        )
        if assignment.get("manifest_identity") != binding.media_id:
            raise ValueError("phone source manifest identity mismatch")
        for prior in (by_path.get(binding.proxy_path), by_id.get(binding.media_id)):
            if prior is not None and prior != binding:
                raise ValueError("conflicting phone source receipts")
        by_path[binding.proxy_path] = binding
        by_id[binding.media_id] = binding
    if set(by_path) != set(selected_paths):
        raise ValueError("every phone source requires a verified proxy receipt")
    return tuple(by_path[path] for path in selected_paths)


def require_bound_moment(
    bindings: tuple[PhoneSourceBinding, ...], *, media_id: str, path: str, generation: str
) -> PhoneSourceBinding:
    matches = [
        binding
        for binding in bindings
        if (binding.media_id, binding.proxy_path, binding.generation)
        == (media_id, path, generation)
    ]
    if len(matches) != 1:
        raise ValueError("approved moment does not match its immutable phone source")
    return matches[0]
