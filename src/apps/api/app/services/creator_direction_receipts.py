"""Immutable, owner-safe receipt projection for creator direction snapshots."""

from __future__ import annotations

from typing import Any

from app.services.creator_direction import CreatorDirectionSnapshot
from app.services.creator_direction_capabilities import capability_status


def _status(row: dict[str, Any], *, project_override: bool) -> str:
    normalized_key = row.get("normalized_key")
    structured_value = row.get("structured_value")
    if normalized_key is not None and not structured_value:
        return capability_status(str(normalized_key), enforcement="advisory")
    return capability_status(
        str(normalized_key) if normalized_key is not None else None,
        enforcement="constraint" if project_override else str(row.get("enforcement") or "advisory"),
    )


def stamp_private_receipt(
    private_snapshot: dict[str, Any], snapshot: CreatorDirectionSnapshot
) -> dict[str, Any]:
    """Attach the immutable readable rules used by one private generation snapshot."""

    result = dict(private_snapshot)
    override_keys = {
        row.get("normalized_key") for row in snapshot.overrides if row.get("normalized_key")
    }
    rules: list[dict[str, Any]] = []
    for row in snapshot.items:
        if row.get("normalized_key") in override_keys:
            continue
        rules.append(
            {
                "id": str(row.get("id")),
                "normalized_key": row.get("normalized_key"),
                "instruction": str(row.get("instruction") or "")[:500],
                "status": _status(row, project_override=False),
                "scope_label": "All future videos",
                "source_label": "Account personalization",
                "overridden": False,
            }
        )
    for row in snapshot.overrides:
        rules.append(
            {
                "id": str(row.get("id")),
                "normalized_key": row.get("normalized_key"),
                "instruction": str(row.get("instruction") or "")[:500],
                "status": _status(row, project_override=True),
                "scope_label": "This project only",
                "source_label": "Project override",
                "overridden": True,
            }
        )
    result["receipt_rules"] = rules
    return result


def project_direction_receipt(private_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the stable user receipt without consulting mutable ledger rows."""

    raw_rules = private_snapshot.get("receipt_rules")
    rules = (
        [dict(row) for row in raw_rules if isinstance(row, dict)]
        if isinstance(raw_rules, list)
        else []
    )
    applied_ids = private_snapshot.get("applied_item_ids") or []
    override_ids = private_snapshot.get("applied_override_ids") or []
    applied_count = len(rules) if rules else len(applied_ids) + len(override_ids)
    counts = {
        name: sum(1 for row in rules if row.get("status") == name)
        for name in ("enforced", "advisory", "unsupported", "conflicted")
    }
    if not rules:
        typed = private_snapshot.get("typed_overrides") or {}
        enforced = min(applied_count, len(typed) if isinstance(typed, dict) else 0)
        counts["enforced"] = enforced
        counts["advisory"] = max(0, applied_count - enforced)
    return {
        "enabled": bool(private_snapshot.get("enabled")),
        "memory_revision": int(private_snapshot.get("memory_revision") or 0),
        "applied_count": applied_count,
        "enforced_count": counts["enforced"],
        "advisory_count": counts["advisory"],
        "unsupported_count": counts["unsupported"],
        "conflicted_count": counts["conflicted"],
        "rules": rules,
    }


__all__ = ["project_direction_receipt", "stamp_private_receipt"]
