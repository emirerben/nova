"""Consent-safe dataset projection, splitting, and preference construction.

This module is deliberately provider-neutral and storage-agnostic. Callers must
perform the central eligibility check before constructing candidates. The final
``assert_export_safe`` call is a second, fail-closed boundary against accidental
ORM/JSONB expansion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models import EDIT_FEEDBACK_DIMENSIONS
from app.schemas.edit_training import (
    CanonicalEditTrainingRecord,
    DatasetReadiness,
    EditPreferencePair,
)

REQUIRED_REVIEW_DIMENSIONS = frozenset(EDIT_FEEDBACK_DIMENSIONS)

_DENIED_KEYS = frozenset(
    {
        "email",
        "name",
        "raw_text",
        "raw_transcript",
        "transcript",
        "gcs_path",
        "storage_path",
        "source_path",
        "video_path",
        "thumbnail_path",
        "signed_url",
        "output_url",
        "base_video_path",
        "assembly_plan",
        "input_json",
        "output_json",
    }
)
_MEDIA_KEYS = frozenset(
    {
        "media_key",
        "kind",
        "duration_s",
        "width",
        "height",
        "orientation",
        "shot_type",
        "visual_summary",
        "motion",
        "quality",
        "has_speech",
    }
)


@dataclass(frozen=True)
class DatasetCandidate:
    artifact_id: str
    creator_id: str
    plan_item_id: str
    agent: str
    media_summary: Sequence[Mapping[str, Any]]
    user_intent: str
    proposed: Mapping[str, Any]
    execution: Mapping[str, Any]
    labels: Sequence[Mapping[str, Any]]
    versions: Mapping[str, str]
    lineage_parts: Sequence[str]


def _digest(secret: str, namespace: str, value: str) -> str:
    if len(secret) < 16:
        raise ValueError("training dataset split secret must contain at least 16 characters")
    return hmac.new(
        secret.encode("utf-8"),
        f"{namespace}:{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def pseudonymous_key(secret: str, namespace: str, value: str) -> str:
    """Return a stable export-safe identifier without exposing a database id."""
    return _digest(secret, namespace, value)


def dataset_split(secret: str, creator_id: str) -> str:
    """Assign the entire creator to one stable split (80/10/10)."""
    bucket = int(_digest(secret, "creator-split", creator_id)[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


def _safe_media_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{str(k): value for k, value in row.items() if str(k) in _MEDIA_KEYS} for row in rows]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("training export contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise ValueError(f"training export contains unsupported value {type(value).__name__}")


def assert_export_safe(value: Any, *, path: str = "record") -> None:
    """Reject paths, signed URLs, PII fields, and raw model/ORM payload keys."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _DENIED_KEYS or normalized.endswith("_signed_url"):
                raise ValueError(f"training export denied field: {path}.{key}")
            assert_export_safe(child, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            assert_export_safe(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith(("gs://", "s3://")) or "x-goog-signature=" in lowered:
            raise ValueError(f"training export contains a storage URL at {path}")


def canonical_record(candidate: DatasetCandidate, *, secret: str) -> CanonicalEditTrainingRecord:
    creator_group = _digest(secret, "creator", candidate.creator_id)
    plan_item_group = _digest(secret, "plan-item", candidate.plan_item_id)
    lineage = "\x1f".join(str(part) for part in candidate.lineage_parts)
    record = CanonicalEditTrainingRecord(
        artifact_key=_digest(secret, "artifact", candidate.artifact_id),
        creator_group=creator_group,
        plan_item_group=plan_item_group,
        split=dataset_split(secret, candidate.creator_id),
        agent=candidate.agent,  # type: ignore[arg-type]
        media_summary=_safe_media_summary(candidate.media_summary),
        user_intent=candidate.user_intent,
        proposed=_json_safe(candidate.proposed),
        execution=_json_safe(candidate.execution),
        labels=_json_safe(candidate.labels),
        versions={str(k): str(v) for k, v in candidate.versions.items()},
        lineage_key=_digest(secret, "lineage", lineage),
    )
    assert_export_safe(record.model_dump(mode="json"))
    return record


def records_to_jsonl(records: Iterable[CanonicalEditTrainingRecord]) -> bytes:
    lines: list[str] = []
    for record in records:
        payload = record.model_dump(mode="json")
        assert_export_safe(payload)
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def records_to_parquet(records: Sequence[CanonicalEditTrainingRecord], output_path: str) -> None:
    """Write canonical records as Parquet using JSON columns for nested fields."""
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency failure is environment-specific
        raise RuntimeError("Parquet export requires the pinned pyarrow dependency") from exc

    rows = []
    for record in records:
        payload = record.model_dump(mode="json")
        assert_export_safe(payload)
        scalar_keys = (
            "schema_version",
            "artifact_key",
            "creator_group",
            "plan_item_group",
            "split",
            "agent",
            "user_intent",
            "lineage_key",
        )
        rows.append(
            {
                **{key: payload[key] for key in scalar_keys},
                "media_summary_json": json.dumps(
                    payload["media_summary"], ensure_ascii=False, sort_keys=True
                ),
                "proposed_json": json.dumps(
                    payload["proposed"], ensure_ascii=False, sort_keys=True
                ),
                "execution_json": json.dumps(
                    payload["execution"], ensure_ascii=False, sort_keys=True
                ),
                "labels_json": json.dumps(payload["labels"], ensure_ascii=False, sort_keys=True),
                "versions_json": json.dumps(
                    payload["versions"], ensure_ascii=False, sort_keys=True
                ),
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), output_path, compression="zstd")


def _overall_score(record: CanonicalEditTrainingRecord) -> tuple[int, int]:
    weights = {"bad": 0, "mixed": 1, "good": 2}
    scores = [
        weights[str(label.get("rating"))]
        for label in record.labels
        if label.get("dimension") == "overall_quality" and label.get("rating") in weights
    ]
    return (max(scores, default=-1), len(scores))


def build_preference_pairs(
    records: Sequence[CanonicalEditTrainingRecord],
) -> list[EditPreferencePair]:
    """Create pairs only within identical lineage and with explicit human evidence."""
    groups: dict[tuple[str, str, str], list[CanonicalEditTrainingRecord]] = defaultdict(list)
    for record in records:
        groups[(record.agent, record.split, record.lineage_key)].append(record)

    pairs: list[EditPreferencePair] = []
    for (_, _, _), candidates in groups.items():
        ranked = sorted(candidates, key=_overall_score, reverse=True)
        if len(ranked) < 2:
            continue
        chosen, rejected = ranked[0], ranked[-1]
        chosen_score, chosen_labels = _overall_score(chosen)
        rejected_score, rejected_labels = _overall_score(rejected)
        if chosen_labels == 0 or rejected_labels == 0 or chosen_score <= rejected_score:
            continue
        pairs.append(
            EditPreferencePair(
                agent=chosen.agent,
                split=chosen.split,
                lineage_key=chosen.lineage_key,
                context={
                    "media_summary": chosen.media_summary,
                    "user_intent": chosen.user_intent,
                    "versions": chosen.versions,
                },
                chosen={"proposed": chosen.proposed, "execution": chosen.execution},
                rejected={"proposed": rejected.proposed, "execution": rejected.execution},
                rationale="Human overall-quality rating preferred the chosen artifact.",
            )
        )
    return pairs


def dataset_readiness(
    records: Sequence[CanonicalEditTrainingRecord],
    *,
    minimum_reviewed: int = 100,
    minimum_creator_groups: int = 3,
) -> DatasetReadiness:
    reviewed = 0
    missing_counts: dict[str, int] = defaultdict(int)
    for record in records:
        dimensions = {str(label.get("dimension")) for label in record.labels}
        missing = REQUIRED_REVIEW_DIMENSIONS - dimensions
        if not missing:
            reviewed += 1
        for dimension in missing:
            missing_counts[dimension] += 1

    creators = len({record.creator_group for record in records})
    blockers: list[str] = []
    if reviewed < minimum_reviewed:
        blockers.append(f"Need {minimum_reviewed} fully reviewed artifacts; found {reviewed}.")
    if creators < minimum_creator_groups:
        blockers.append(
            f"Need {minimum_creator_groups} independent creator groups; found {creators}."
        )
    return DatasetReadiness(
        ready=not blockers,
        reviewed_artifacts=reviewed,
        creator_groups=creators,
        missing_dimensions=dict(sorted(missing_counts.items())),
        blockers=blockers,
    )


def generic_fine_tuning_rows(
    records: Sequence[CanonicalEditTrainingRecord],
    *,
    agent: str,
) -> list[dict[str, Any]]:
    """Return provider-neutral SFT rows; this function never uploads or promotes."""
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.agent != agent:
            continue
        row = {
            "schema_version": 1,
            "agent": record.agent,
            "split": record.split,
            "input": {
                "media_summary": record.media_summary,
                "user_intent": record.user_intent,
            },
            "target": record.proposed,
            "execution": record.execution,
            "labels": record.labels,
            "versions": record.versions,
        }
        assert_export_safe(row)
        rows.append(row)
    return rows
