"""Fixed-vocabulary request outcomes from the existing consented trace captures."""

from collections import Counter
from datetime import datetime
from statistics import median

from pydantic import ValidationError

from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.request_following_cases import digest, failed_pre_job_turns
from app.services.request_following_lineage import RequestOutcomeLink


def _date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _verdict(entry):
    rows = entry.get("outcomes") or []
    values = {r.get("verdict") for r in rows}
    if entry.get("watched") and rows and values == {"met"}:
        return "fulfilled"
    if values & {"partial", "missed"}:
        return "unfulfilled"
    return "unknown"


def _device_attempts(job, variant_id=None):
    records = ((job or {}).get("assembly_plan") or {}).get(DEVICE_RENDER_FIELD) or {}
    rows = [records.get(variant_id)] if variant_id is not None else records.values()
    return sum(len(row.get("attempts") or {}) for row in rows if isinstance(row, dict))


def request_outcome_report(cases, *, since, until):
    roots, histories, runs = {}, {}, {}
    session_runs = []
    unknown_threads, incomplete = set(), set()
    for case in cases:
        capture = case.capture or {}
        sections = capture.get("sections") or {}
        events = sections.get("events") or []
        links = {}
        for event in events:
            if event.get("actor") != "user" or event.get("kind") != "user_message":
                continue
            payload = event.get("payload") or {}
            try:
                link = RequestOutcomeLink.model_validate(payload.get("request_outcome_link"))
            except ValidationError:
                unknown_threads.add(str(case.thread_id))
                continue
            at = _date(event.get("created_at"))
            if not link.original_known or link.link_status != "known" or at is None:
                unknown_threads.add(str(case.thread_id))
                continue
            if at.tzinfo is None or at > until:
                continue
            turn_id = str(payload.get("turn_id") or "")
            if not turn_id:
                continue
            root_id = str(link.request_turn_id)
            links[turn_id] = root_id
            cohort = (
                "internal"
                if case.eligibility_key.startswith("internal_grant:")
                else "consented_unknown"
            )
            root = roots.setdefault(
                root_id,
                {
                    "at": at,
                    "latest_at": at,
                    "origin_seen": False,
                    "incomplete": False,
                    "metadata": link,
                    "cohort": cohort,
                    "jobs": {},
                    "executions": {},
                    "failed": {},
                    "failed_jobs": set(),
                    "relations": {},
                    "assessments": {},
                },
            )
            if at < root["at"]:
                root.update(at=at, metadata=link)
            root["latest_at"] = max(root["latest_at"], at)
            root["origin_seen"] |= turn_id == root_id and link.relation == "original"
            root["incomplete"] |= bool(capture.get("missing"))
            root["relations"][turn_id] = link.relation
        failed = failed_pre_job_turns(capture)
        for turn in sections.get("turns") or []:
            turn_id = str(turn.get("id") or "")
            root_id = links.get(turn_id)
            if root_id is None:
                continue
            root = roots[root_id]
            if turn_id in failed:
                root["failed"][turn_id] = _date(turn.get("created_at")) or root["at"]
            for job_id in turn.get("job_ids") or []:
                job = sections.get(f"job-{job_id}")
                if isinstance(job, dict):
                    root["jobs"].setdefault(job_id, job)
                    if job.get("status") in {"failed", "processing_failed"}:
                        root["failed_jobs"].add(job_id)
            for execution in turn.get("executions") or []:
                job_id = execution.get("target_job_id")
                if job_id and execution.get("tool_name") == "render.request":
                    root["executions"][str(execution["id"])] = job_id
        for entry in (case.review or {}).get("outcome_history") or []:
            if entry.get("assessment_id"):
                histories[entry["assessment_id"]] = entry
        if links:
            if capture.get("missing"):
                incomplete.add(str(case.id))
            session_runs.append(
                (set(links.values()), (capture.get("metrics") or {}).get("agent_runs") or [])
            )
    roots = {
        key: root
        for key, root in roots.items()
        if root["origin_seen"] and root["latest_at"] >= since
    }
    for linked_roots, recorded_runs in session_runs:
        if not linked_roots.intersection(roots):
            continue
        for run in recorded_runs:
            at = _date(run.get("created_at"))
            if run.get("id") and at is not None and at.tzinfo is not None and since <= at <= until:
                runs.setdefault(run["id"], run)
    for entry in histories.values():
        try:
            link = RequestOutcomeLink.model_validate(entry.get("request_link"))
        except ValidationError:
            continue
        root = roots.get(str(link.request_turn_id))
        if root is None or not link.original_known or link.link_status != "known":
            continue
        key = (entry.get("basis"), digest(entry.get("artifact")))
        previous = root["assessments"].get(key)
        if previous is None or entry["reviewed_at"] > previous["reviewed_at"]:
            root["assessments"][key] = entry
    counts = Counter(
        requests=len(roots),
        original_fulfilled=0,
        original_unfulfilled=0,
        original_unknown=0,
        revision_fulfilled=0,
        recovery_fulfilled=0,
        render_attempts=0,
        device_export_attempts=0,
        pre_job_failure_attempts=0,
        corrections=0,
        failed_render_jobs=0,
        pending_render_jobs=0,
        assessments=0,
        unfulfilled_artifacts=0,
    )
    first, segments, latencies = Counter(fulfilled=0, requests=len(roots), unknown=0), Counter(), []
    for root in roots.values():
        entries = list(root["assessments"].values())
        originals = [e for e in entries if e.get("basis") == "original"]
        verdicts = {_verdict(e) for e in originals}
        status = (
            "fulfilled"
            if "fulfilled" in verdicts
            else "unfulfilled"
            if "unfulfilled" in verdicts
            else "unknown"
        )
        counts[f"original_{status}"] += 1
        counts["assessments"] += len(entries)
        counts["unfulfilled_artifacts"] += sum(_verdict(e) == "unfulfilled" for e in entries)
        counts["revision_fulfilled"] += any(
            e.get("basis") == "current_revision" and _verdict(e) == "fulfilled" for e in entries
        )
        counts["recovery_fulfilled"] += any(
            e.get("basis") == "recovery" and _verdict(e) == "fulfilled" for e in entries
        )
        counts["pre_job_failure_attempts"] += len(root["failed"])
        counts["failed_render_jobs"] += len(root["failed_jobs"])
        counts["corrections"] += sum(
            r in {"retry", "revision", "recovery"} for r in root["relations"].values()
        )
        render_attempts = 0
        job_dates = []
        for job_id, job in root["jobs"].items():
            dispatches = sum(value == job_id for value in root["executions"].values())
            render_attempts += max(1, dispatches)
            counts["device_export_attempts"] += _device_attempts(job)
            counts["pending_render_jobs"] += job.get("status") not in {
                "failed",
                "processing_failed",
                "ready",
                "variants_ready",
            }
            at = _date(job.get("created_at"))
            if at is not None and at.tzinfo is not None:
                job_dates.append(at)
        counts["render_attempts"] += render_attempts
        earliest_failure = min(root["failed"].values(), default=None)
        selected_export_attempts = [
            _device_attempts(
                root["jobs"].get(entry["artifact"]["job_id"]), entry["artifact"]["variant_id"]
            )
            for entry in originals
        ]
        if root["incomplete"]:
            first["unknown"] += 1
        elif earliest_failure is not None and (not job_dates or earliest_failure <= min(job_dates)):
            pass
        elif (
            render_attempts == 1
            and max(selected_export_attempts, default=0) <= 1
            and status != "unknown"
        ):
            first["fulfilled"] += status == "fulfilled"
        else:
            first["unknown"] += 1
        fulfilled_dates = []
        for entry in originals:
            at = _date(entry.get("reviewed_at"))
            if _verdict(entry) == "fulfilled" and at is not None and at >= root["at"]:
                fulfilled_dates.append(at)
        if fulfilled_dates:
            latencies.append((min(fulfilled_dates) - root["at"]).total_seconds())
        meta = root["metadata"]
        segments[
            (
                root["cohort"],
                meta.client_build or "unknown",
                meta.client_protocol,
                meta.server_revision,
            )
        ] += 1
    known_cost = sum(
        row["settled_cost_usd"]
        for row in runs.values()
        if isinstance(row.get("settled_cost_usd"), (int, float))
    )
    return {
        **counts,
        "first_attempt": dict(first),
        "unknown_lineage_sessions": len(unknown_threads),
        "incomplete_captures": len(incomplete),
        "known_cost_usd": round(known_cost, 6) if runs else None,
        "cost_runs": len(runs),
        "unknown_cost_runs": sum(row.get("settled_cost_usd") is None for row in runs.values()),
        "cost_scope": (
            "deduplicated recorded runs associated with selected sessions; "
            "incomplete costs remain unknown"
        ),
        "fulfilled_latency_samples": len(latencies),
        "median_fulfilled_latency_s": round(median(latencies), 3) if latencies else None,
        "segments": [
            {
                "cohort": key[0],
                "client_build": key[1],
                "client_protocol": key[2],
                "server_revision": key[3],
                "requests": count,
            }
            for key, count in sorted(segments.items(), key=lambda item: str(item[0]))
        ],
        "segment_basis": (
            "original request client/server identity; "
            "consent alone does not identify beta or customer cohorts"
        ),
        "scope": (
            "known linked original requests in eligible captured sessions; "
            "selected sample, not population"
        ),
        "first_attempt_limit": (
            "multiple uninspected render attempts remain unknown; "
            "pre-job failure is never first-attempt fulfillment"
        ),
        "attempt_basis": (
            "render dispatch receipts per job; native export attempts counted separately"
        ),
        "window_start": since.isoformat(),
        "window_end": until.isoformat(),
        "creator_acceptance": None,
        "abandonment": None,
    }
