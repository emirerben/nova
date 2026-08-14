"""Pure durable-reference checks shared by pool deletion and maintenance GC."""

from __future__ import annotations

from typing import Any


def item_references_pool_path(item: Any, gcs_path: str) -> bool:
    if gcs_path in {path for path in (item.clip_gcs_paths or []) if isinstance(path, str)}:
        return True
    return any(
        isinstance(assignment, dict) and assignment.get("gcs_path") == gcs_path
        for assignment in (item.clip_assignments or [])
    )


def job_references_pool_asset(job: Any, *, asset_id: str, gcs_path: str) -> bool:
    """Find render inputs while ignoring dismissible pending suggestions."""

    def _references(value: object) -> bool:
        if isinstance(value, list):
            return any(_references(entry) for entry in value)
        if not isinstance(value, dict):
            return False
        if str(value.get("asset_id") or "") == asset_id:
            return True
        if any(
            value.get(key) == gcs_path
            for key in ("gcs_path", "src_gcs_path", "source_gcs_path", "raw_storage_path")
        ):
            return True
        return any(
            key != "overlay_suggestions" and _references(child) for key, child in value.items()
        )

    if getattr(job, "raw_storage_path", None) == gcs_path:
        return True
    return _references(job.assembly_plan or {})


def pool_paths_in_payload(value: object, *, prefix: str) -> set[str]:
    """Collect pool source paths from untrusted overlay/suggestion payloads."""
    found: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, list):
            for child in node:
                _walk(child)
            return
        if not isinstance(node, dict):
            return
        for key in ("gcs_path", "src_gcs_path", "source_gcs_path"):
            path = node.get(key)
            if isinstance(path, str) and path.startswith(prefix):
                found.add(path)
        for child in node.values():
            _walk(child)

    _walk(value)
    return found
