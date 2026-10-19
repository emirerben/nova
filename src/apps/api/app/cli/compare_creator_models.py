"""Private, explicit model experiment CLI; default operations are offline.

Run from src/apps/api: python -m app.cli.compare_creator_models --help
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.services.creator_comparison_capture import private_root
from app.services.creator_model_comparison import (
    AGENTS,
    MODELS,
    Arm,
    agent_class,
    build_agent,
    prepare_case,
    prepare_packet,
    schema_audit,
)
from app.services.request_following_cases import digest
from app.services.request_following_private_store import read, write

REPO = Path(__file__).resolve().parents[5]


def _private_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("private files cannot be symlinks")
    path = path.resolve()
    private_root(path.parent)
    if path.exists() and (path.is_symlink() or path.stat().st_mode & 0o077):
        raise ValueError("private file must have mode 600")
    return path


def _arm(value):
    # id:model:effort[:native|json][:legacy]
    parts = value.split(":")
    if not 3 <= len(parts) <= 5:
        raise ValueError("arm syntax id:model:effort[:mode][:legacy]")
    return Arm(
        *parts[:3],
        output_mode=parts[3] if len(parts) > 3 else "json",
        legacy_retries=len(parts) > 4 and parts[4] == "legacy",
    )


def _source(path):
    source = read(Path(path))
    if "source_kind" in source:
        _private_file(path)
        return {**source, "capture_file": str(Path(path).resolve())}
    names = {"nova.creator.main": "main_creator", "nova.edit.copilot": "edit_copilot"}
    return {
        "id": Path(path).stem,
        "agent": names[source["agent"]],
        "input": source["input"],
        "raw_text": source["raw_text"],
        "source_kind": "recorded_replay",
        "split": "development",
        "recorded_prompt_version": source.get("prompt_version"),
        "fixture_sha256": digest(source),
    }


def _eligibility(case, *, require_review=True):
    """Current owner/grant/scope checks through the existing admin CLI, no bodies.

    Human expectations bind separately to the immutable invocation input. A
    mutable queue snapshot must not be mistaken for that historical input.
    """
    from urllib.parse import urlencode
    from uuid import UUID

    if case.get("source_kind") != "exact_invocation" or not case.get("complete"):
        raise ValueError("live comparison requires a complete exact capture")
    if case.get("split") != "development":
        raise ValueError("restricted holdout runner not enabled")
    query = urlencode(
        {
            "thread_id": str(UUID(case["thread_id"])),
            "creator_id": str(UUID(case["creator_id"])),
            "grant_key": case["eligibility_key"],
            "captured_at": case["captured_at"],
            "expires_at": case["expires_at"],
        }
    )
    result = subprocess.run(
        [
            "python3",
            str(REPO / "scripts/admin.py"),
            "--prod",
            "GET",
            "request-following/comparison-eligibility?" + query,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO,
    )
    if result.returncode or json.loads(result.stdout).get("eligible") is not True:
        raise ValueError("current eligibility unavailable")
    if require_review:
        review = case.get("intent_review") or {}
        if (
            review.get("decision") != "approved"
            or not review.get("reviewed_by")
            or review.get("input_sha256") != case["input_sha256"]
            or not review.get("expectations")
        ):
            raise ValueError("human input-bound intent review required")
        return review["expectations"]
    return None


def purge_packet(path, directory, *, check_eligibility=False):
    """Delete our private source copies and all derived run bodies on invalidation.

    Case files are removed only when their bytes match this packet exactly.
    Other sources in the operator's private directory are never traversed.
    """
    path = _private_file(path)
    directory = private_root(directory)
    packet = read(path)
    invalid = False
    for case in packet["cases"]:
        if case.get("expires_at") and datetime.fromisoformat(case["expires_at"]) <= datetime.now(
            UTC
        ):
            invalid = True
        if check_eligibility and case.get("source_kind") == "exact_invocation":
            try:
                _eligibility(case, require_review=False)
            except Exception:
                invalid = True  # Fail closed also on loss of eligibility-service access.
    if not invalid:
        return {"purged": False}
    for case in packet["cases"]:
        if case.get("capture_file"):
            source = _private_file(case["capture_file"])
            if source.exists() and digest(read(source)) == digest(
                {k: v for k, v in case.items() if k != "capture_file"}
            ):
                source.unlink()
    identity = packet["sha256"]
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("invalid packet identity")
    for suffix in (".live.json", ".replay.json"):
        journal = _private_file(directory / (identity + suffix))
        journal.unlink(missing_ok=True)
    path.unlink()
    return {"purged": True, "packet_sha256": identity}


def report_journal(path):
    journal = read(_private_file(path))
    groups = {}
    for row in journal["rows"].values():
        group = groups.setdefault(
            row["arm"],
            {
                "rows": 0,
                "parsed": 0,
                "first_attempt_parsed": 0,
                "unknown": 0,
                "latencies_ms": [],
                "provider_attempts": 0,
            },
        )
        group["rows"] += 1
        group["parsed"] += row["status"] == "parsed"
        group["unknown"] += row["status"] in ("unknown", "started")
        group["first_attempt_parsed"] += bool(row.get("first_attempt_parsed"))
        group["provider_attempts"] += len(row.get("invocations", []))
        if "elapsed_ms" in row:
            group["latencies_ms"].append(row["elapsed_ms"])
    for group in groups.values():
        values = sorted(group.pop("latencies_ms"))
        for p in (0.5, 0.95):
            group[f"p{int(p * 100)}_elapsed_ms"] = (
                values[min(len(values) - 1, int((len(values) - 1) * p))] if values else None
            )
    return {
        "mode": journal["mode"],
        "packet_sha256": journal["packet_sha256"],
        "arms": groups,
        "quality_verdict": "unassessed",
        "persisted_execution": "unassessed",
        "actual_export": "unassessed",
        "latency_scope": "agent plus eligibility check, excludes render",
        "invalidated": journal.get("invalidated", False),
    }


def _execute(packet, directory, *, live, approved_cost=None, approval_sha=None):
    directory = private_root(directory)
    preflight = prepare_packet(packet)
    if live:
        from app.config import settings

        if (
            settings.ai_usage_environment != "development"
            or not settings.ai_cost_control_enabled
            or not settings.ai_paid_test_attribution_required
            or os.getenv("NOVA_EVAL_MODE") != "live"
        ):
            raise ValueError("live needs enforced ledger, attribution and NOVA_EVAL_MODE=live")
        if approval_sha != packet["sha256"] or approved_cost is None or not 0 < approved_cost <= 2:
            raise ValueError("explicit manifest-bound approval and <=$2 cap required")
        if preflight["estimated_max_usd"] > approved_cost:
            raise ValueError("preflight exceeds approved cap; freeze a smaller batch")
        for case in packet["cases"]:
            _eligibility(case)
    run_id = packet["sha256"]
    lock_path = directory / (run_id + ".lock")
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Prevent replay results being confused with paid results in one journal.
        journal_path = directory / (run_id + (".live.json" if live else ".replay.json"))
        journal = (
            read(journal_path)
            if journal_path.exists()
            else {
                "mode": "live" if live else "recorded_replay",
                "packet_sha256": run_id,
                "rows": {},
                "quality_verdict": "unassessed",
                "persisted_execution": "unassessed",
                "actual_export": "unassessed",
            }
        )
        jobs = [
            (case, Arm(**arm), repeat)
            for repeat in range(packet["repetitions"])
            for case in packet["cases"]
            for arm in packet["arms"]
        ]
        if any(r["status"] in ("started", "unknown") for r in journal["rows"].values()):
            raise ValueError("run has an unresolved submission; reconcile before further paid work")
        random.Random(packet["seed"]).shuffle(jobs)
        for case, arm, repeat in jobs:
            key = digest([case["id"], arm.id, repeat])
            if key in journal["rows"]:
                continue  # includes started/unknown: never retry a possibly accepted call
            if live:
                try:
                    _eligibility(case)  # every arm, not merely at import/run start
                except Exception:
                    # Remove source-derived bodies from this run on revocation.
                    for old in journal["rows"].values():
                        old.pop("output", None)
                        old.pop("invocations", None)
                    journal["invalidated"] = True
                    write(journal_path, journal)
                    raise
            attempts = []
            eligibility_lost = False

            class Client:
                supports_clarification_retry = True

                def invoke(self, **kwargs):
                    nonlocal eligibility_lost
                    if live:
                        # Recheck immediately before *each* provider submission,
                        # including runtime retries/fallbacks inside legacy arms.
                        try:
                            _eligibility(case)
                        except Exception as exc:
                            from app.agents._runtime import TerminalError

                            eligibility_lost = True
                            raise TerminalError("comparison eligibility unavailable") from exc
                        result = dispatcher.invoke(**kwargs)
                    else:
                        from app.agents._runtime import ModelInvocation

                        result = ModelInvocation(raw_text=case["raw_text"])
                    attempts.append(
                        {
                            "model": kwargs["model"],
                            "finish": result.finish_status,
                            "tokens_in": result.tokens_in,
                            "tokens_out": result.tokens_out,
                            "tokens_thoughts": result.tokens_thoughts,
                            "tokens_cached": result.tokens_cached,
                            "provider_request_id": result.provider_request_id,
                            "raw_text": result.raw_text,
                            "token_details": result.token_details,
                        }
                    )
                    return result

            if live:
                from app.agents._model_client import ModelDispatcher

                dispatcher = ModelDispatcher()
            agent = build_agent(case["agent"], arm, Client())
            from app.agents._runtime import RunContext

            row = {**prepare_case(case, arm), "repeat": repeat, "status": "started"}
            journal["rows"][key] = row
            write(journal_path, journal)  # durable fence BEFORE submission
            start = time.monotonic()
            try:
                if live:
                    output = agent.run(
                        case["input"],
                        ctx=RunContext(
                            request_id=f"comparison:{run_id}:{key}",
                            usage_purpose="live_eval" if live else None,
                            test_run_id=f"comparison:{run_id}" if live else None,
                            estimated_max_cost_usd=approved_cost,
                            reservation_approved=live,
                            extra={
                                "skip_agent_run_persist": True,
                                "skip_langfuse_trace": True,
                                "skip_comparison_capture": True,
                            },
                        ),
                    )
                else:
                    # Replay validates recorded bytes, never reserves or constructs SDKs.
                    output = agent.parse(
                        case["raw_text"], agent.Input.model_validate(case["input"])
                    )
                from tests.evals.runners.structural import run_structural

                failures = run_structural(
                    agent.spec.name, output, agent.Input.model_validate(case["input"]), case["id"]
                )
                row.update(
                    status="structural_failed" if failures else "parsed",
                    structural_failures=failures,
                    output=output.model_dump(mode="json"),
                )
            except Exception as exc:
                # Do not serialize exception text: it may contain private model output.
                from app.agents._runtime import ProviderOutcomeUnknownError

                row.update(
                    status="unknown" if isinstance(exc, ProviderOutcomeUnknownError) else "failed",
                    error_class=type(exc).__name__,
                )
            if eligibility_lost:
                for previous in journal["rows"].values():
                    previous.pop("output", None)
                    previous.pop("invocations", None)
                journal["invalidated"] = True
                write(journal_path, journal)
                raise ValueError("eligibility lost; purge invalidated private packet")
            row.update(
                elapsed_ms=round((time.monotonic() - start) * 1000),
                invocations=attempts,
                first_attempt_parsed=row["status"] == "parsed" and len(attempts) == 1,
            )
            write(journal_path, journal)
            if row["status"] == "unknown":
                break  # no further paid work until explicit reconciliation
        summary = {
            "mode": journal["mode"],
            "packet_sha256": run_id,
            "cases": len(packet["cases"]),
            "completed_rows": len(journal["rows"]),
            "parsed": sum(r["status"] == "parsed" for r in journal["rows"].values()),
            "unknown": sum(r["status"] in ("unknown", "started") for r in journal["rows"].values()),
            "quality_verdict": "unassessed",
            "actual_export": "unassessed",
            "inference_only": True,
            "private_journal": str(journal_path),
        }
        if live:
            # Read actual and conservatively held cost from the same ledger.
            from sqlalchemy import text

            from app.database import sync_engine

            with sync_engine.connect() as db:
                cost = db.execute(
                    text(
                        "SELECT COALESCE(SUM(settled_cost_usd),0), "
                        "COALESCE(SUM(CASE WHEN status IN "
                        "('reserved','provider_started','unknown') "
                        "THEN estimated_cost_usd ELSE 0 END),0) FROM ai_cost_reservations "
                        "WHERE test_run_id=:id"
                    ),
                    {"id": f"comparison:{run_id}"},
                ).one()
            summary.update(settled_usd=float(cost[0]), reserved_or_unknown_usd=float(cost[1]))
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("schema-audit")
    audit.add_argument("--output", required=True)
    report = sub.add_parser("report")
    report.add_argument("--journal", required=True)
    purge = sub.add_parser("purge")
    purge.add_argument("--packet", required=True)
    purge.add_argument("--directory", required=True)
    purge.add_argument("--check-eligibility", action="store_true")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--case", action="append", required=True)
    freeze.add_argument("--arm", action="append", required=True)
    freeze.add_argument("--repetitions", type=int, default=1)
    freeze.add_argument("--seed", type=int, default=727)
    freeze.add_argument("--output", required=True)
    for name in ("validate", "dry-run", "replay", "live"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--packet", required=True)
        cmd.add_argument("--directory", required=name in ("replay", "live"))
        if name == "live":
            cmd.add_argument("--approve-max-usd", type=float, required=True)
            cmd.add_argument("--approval-sha256", required=True)
    args = parser.parse_args(argv)
    if args.command == "report":
        print(json.dumps(report_journal(args.journal), indent=2))
        return
    if args.command == "purge":
        print(
            json.dumps(
                purge_packet(args.packet, args.directory, check_eligibility=args.check_eligibility)
            )
        )
        return
    if args.command == "schema-audit":
        result = {
            name: {
                model: schema_audit(agent_class(name).Output.model_json_schema(), model)
                for model in MODELS
            }
            for name in AGENTS
        }
        # Contains schema paths/field names, never creator content.
        write(Path(args.output), result)
        print(json.dumps({"output": args.output, "native_schema": "unverified; inspect blockers"}))
        return
    if args.command == "freeze":
        packet = {
            "schema_version": 1,
            "cases": [_source(p) for p in args.case],
            "arms": [asdict(_arm(a)) for a in args.arm],
            "repetitions": args.repetitions,
            "seed": args.seed,
        }
        packet["sha256"] = digest(packet)
        result = prepare_packet(packet)
        packet["bindings"] = result["rows"]
        packet["sha256"] = digest({k: v for k, v in packet.items() if k != "sha256"})
        result = prepare_packet(packet)
        path = _private_file(args.output)
        if path.exists():
            raise ValueError("frozen packets are immutable; use a new output path")
        write(path, packet)
    else:
        packet = read(_private_file(args.packet))
        if args.command in ("replay", "live"):
            if purge_packet(args.packet, args.directory, check_eligibility=args.command == "live")[
                "purged"
            ]:
                raise ValueError("private packet invalidated and purged")
        result = prepare_packet(packet)
        if args.command in ("replay", "live"):
            try:
                result = _execute(
                    packet,
                    args.directory,
                    live=args.command == "live",
                    approved_cost=getattr(args, "approve_max_usd", None),
                    approval_sha=getattr(args, "approval_sha256", None),
                )
            except Exception:
                if args.command == "live":
                    purge_packet(args.packet, args.directory, check_eligibility=True)
                raise
    # Preflight/report contains hashes, configuration and counts only.
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SDK/schema errors may contain private input. Keep CLI diagnostics safe.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_class": type(error).__name__,
                    "reason": "operation failed; see comparison runbook",
                }
            )
        )
        raise SystemExit(1) from None
