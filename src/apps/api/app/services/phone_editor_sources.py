"""Private, append-only source admission records for the phone guided editor."""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.schemas.edit_proposal import MAX_EDIT_PROPOSAL_MEDIA
from app.schemas.guided_edit_revision import GuidedEditorSource
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

EDITOR_SOURCES_FIELD = "_editor_sources_v1"
PREPARATION_LEASE_S = 420
MAX_IMPORTS = 200
_GUIDED_FIELDS = ("media_id", "lane", "gcs_path", "generation", "kind", "duration_s")


def _registry(variant: dict) -> dict[str, Any]:
    raw = variant.get(EDITOR_SOURCES_FIELD)
    return raw if isinstance(raw, dict) else {}


def canonical_source(row: dict) -> dict:
    return GuidedEditorSource.model_validate(
        {key: row[key] for key in _GUIDED_FIELDS if key in row}
    ).model_dump(mode="json")


def editor_sources_for_variant(variant: dict) -> list[dict]:
    """Return validated admitted sources in permanent index order."""
    rows = _registry(variant).get("sources") or []
    ready = [row for row in rows if isinstance(row, dict) and row.get("status") == "ready"]
    indices = [row["source_index"] for row in ready]
    if any(type(index) is not int or index < 0 for index in indices) or len(set(indices)) != len(
        indices
    ):
        raise ValueError("editor_source_catalog_invalid")
    result = [canonical_source(row) for row in sorted(ready, key=lambda row: row["source_index"])]
    if len({row["media_id"] for row in result}) != len(result):
        raise ValueError("editor_source_catalog_invalid")
    return result


def merge_editor_sources(approved_sources: list[dict], variant: dict) -> list[dict]:
    """Preserve source identities and reject conflicting or noncontiguous indices."""
    merged = [canonical_source(row) for row in approved_sources]
    by_id = {row["media_id"]: index for index, row in enumerate(merged)}
    if len(by_id) != len(merged):
        raise ValueError("editor_source_catalog_invalid")
    indexed = {
        row["media_id"]: row["source_index"]
        for row in _registry(variant).get("sources", [])
        if row.get("status") == "ready"
    }
    for row in editor_sources_for_variant(variant):
        media_id = row["media_id"]
        if media_id in by_id:
            index = by_id[media_id]
            if merged[index] != row or indexed[media_id] != index:
                raise ValueError("editor_source_identity_conflict")
            continue
        if indexed[media_id] != len(merged):
            raise ValueError("editor_source_catalog_invalid")
        by_id[media_id] = len(merged)
        merged.append(row)
    if len(merged) > MAX_EDIT_PROPOSAL_MEDIA:
        raise ValueError("editor_source_limit")
    return merged


def begin_attempt(record: dict, *, now: float | None = None) -> str:
    token = str(uuid.uuid4())
    record.update(
        status="preparing",
        attempt_id=token,
        lease_expires_at=(time.time() if now is None else now) + PREPARATION_LEASE_S,
        error=None,
        reason_code=None,
        retryable=False,
    )
    return token


def attempt_matches(record: object, token: str) -> bool:
    return (
        isinstance(record, dict)
        and record.get("status") == "preparing"
        and record.get("attempt_id") == token
    )


def lease_expired(record: dict, *, now: float | None = None) -> bool:
    return record.get("status") == "preparing" and float(record.get("lease_expires_at") or 0) <= (
        time.time() if now is None else now
    )


def _bindings(variant: dict, key: str, model: type[Any]) -> tuple[Any, ...]:
    rows = _registry(variant).get("sources") or []
    return tuple(
        model.model_validate(row[key])
        for row in rows
        if isinstance(row, dict) and row.get("status") == "ready" and isinstance(row.get(key), dict)
    )


def editor_source_bindings(variant: dict) -> tuple[PhoneSourceBinding, ...]:
    return _bindings(variant, "source_binding", PhoneSourceBinding)


def editor_visual_bindings(variant: dict) -> tuple[PhoneVisualBinding, ...]:
    return _bindings(variant, "visual_binding", PhoneVisualBinding)
