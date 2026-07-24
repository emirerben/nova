"""Derived render timing summaries for admin/debug tooling."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any


def build_render_summary(job: Any, agent_runs: list[Any] | None = None) -> dict[str, Any] | None:
    """Build a content-safe render timing summary from a Job-like object.

    Returns ``None`` for legacy jobs with no timing anchors and no render-stage
    events so older debug payloads remain quiet.
    """
    trace_events = _render_stage_events(getattr(job, "pipeline_trace", None))
    started_at = _as_dt(getattr(job, "started_at", None))
    created_at = _as_dt(getattr(job, "created_at", None))
    finished_at = _as_dt(getattr(job, "finished_at", None))
    queue_ms = _elapsed_ms(created_at, started_at)
    variant_queue_ms = _variant_queue_ms(getattr(job, "assembly_plan", None))
    if variant_queue_ms:
        queue_ms = (queue_ms or 0) + sum(variant_queue_ms)

    if not trace_events and queue_ms is None and started_at is None:
        return None

    stage_rows = [_stage_row(ev) for ev in trace_events]
    stage_rows = [row for row in stage_rows if row is not None]
    total_stage_ms = sum(int(row["elapsed_ms"]) for row in stage_rows)
    processing_ms = _elapsed_ms(started_at, finished_at) or (total_stage_ms or None)
    slowest = sorted(stage_rows, key=lambda row: int(row["elapsed_ms"]), reverse=True)[:5]

    stage_counts = Counter(str(row["stage"]) for row in stage_rows)
    repeated = [
        {"stage": stage, "count": count}
        for stage, count in sorted(stage_counts.items())
        if count > 1
    ]

    retries = _retry_rows(stage_rows)
    cache = _cache_summary(stage_rows)
    attempts = _attempts(stage_rows)
    trace_id = next((row.get("trace_id") for row in stage_rows if row.get("trace_id")), None)

    agent_ms = sum(int(getattr(run, "latency_ms", 0) or 0) for run in agent_runs or [])
    return {
        "trace_id": trace_id,
        "total_queue_ms": queue_ms,
        "total_processing_ms": processing_ms,
        "slowest_stages": slowest,
        "repeated_stages": repeated,
        "retries": retries,
        "cache": cache,
        "attempts": attempts,
        "agent_work_ms": agent_ms or None,
    }


def build_render_summary_from_debug_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a summary from ``GET /admin/jobs/{id}/debug`` JSON."""
    job = _DictObj(payload.get("job") or {})
    runs = [_DictObj(row) for row in payload.get("agent_runs") or []]
    return build_render_summary(job, runs)


def _render_stage_events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    return [
        ev
        for ev in events
        if isinstance(ev, dict)
        and ev.get("stage") == "render_stage"
        and isinstance(ev.get("data"), dict)
    ]


def _stage_row(ev: dict[str, Any]) -> dict[str, Any] | None:
    data = ev.get("data") or {}
    elapsed = data.get("elapsed_ms")
    try:
        elapsed_ms = int(elapsed)
    except (TypeError, ValueError):
        return None
    row: dict[str, Any] = {
        "stage": str(data.get("stage") or ev.get("event") or "unknown"),
        "elapsed_ms": elapsed_ms,
        "status": str(data.get("status") or "ok"),
    }
    for key in ("trace_id", "variant_id", "render_generation_id", "attempt"):
        if data.get(key) is not None:
            row[key] = data[key]
    if isinstance(data.get("cache"), dict):
        row["cache"] = data["cache"]
    if isinstance(data.get("retry"), dict):
        row["retry"] = data["retry"]
    return row


def _retry_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retries: list[dict[str, Any]] = []
    for row in rows:
        retry = row.get("retry")
        if retry or row.get("status") == "failed":
            retries.append(
                {
                    "stage": row["stage"],
                    "status": row.get("status"),
                    "attempt": row.get("attempt"),
                    "retry": retry or {},
                }
            )
    return retries


def _cache_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        cache = row.get("cache")
        if not isinstance(cache, dict):
            continue
        name = str(cache.get("name") or row["stage"])
        status = str(cache.get("status") or cache.get("result") or "unknown")
        counts[name][status] += 1
    return {name: dict(counter) for name, counter in sorted(counts.items())}


def _attempts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("trace_id") or row.get("render_generation_id") or "default")
        entry = grouped.setdefault(
            key,
            {
                "trace_id": row.get("trace_id"),
                "render_generation_id": row.get("render_generation_id"),
                "stage_count": 0,
                "elapsed_ms": 0,
                "variants": set(),
            },
        )
        entry["stage_count"] += 1
        entry["elapsed_ms"] += int(row["elapsed_ms"])
        if row.get("variant_id"):
            entry["variants"].add(str(row["variant_id"]))
    out: list[dict[str, Any]] = []
    for entry in grouped.values():
        out.append(
            {
                "trace_id": entry.get("trace_id"),
                "render_generation_id": entry.get("render_generation_id"),
                "stage_count": entry["stage_count"],
                "elapsed_ms": entry["elapsed_ms"],
                "variants": sorted(entry["variants"]),
            }
        )
    return out


def _variant_queue_ms(assembly_plan: Any) -> list[int]:
    if not isinstance(assembly_plan, dict):
        return []
    out: list[int] = []
    for variant in assembly_plan.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        elapsed = _elapsed_ms(
            _as_dt(variant.get("render_enqueued_at")),
            _as_dt(variant.get("render_started_at")),
        )
        if elapsed is not None:
            out.append(elapsed)
    return out


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class _DictObj:
    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)
