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
    # id:model:effort[:native|json|json_guarded][:legacy]
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


def _eligibility(case, *, require_review=True, sealed=False):
    """Current owner/grant/scope checks through the existing admin CLI, no bodies.

    Human expectations bind separately to the immutable invocation input. A
    mutable queue snapshot must not be mistaken for that historical input.
    """
    from urllib.parse import urlencode
    from uuid import UUID

    if case.get("source_kind") not in ("exact_invocation", "reconstructed_real") or not case.get(
        "complete"
    ):
        raise ValueError("live comparison requires a complete exact capture")
    if case.get("split") != ("holdout" if sealed else "development"):
        raise ValueError("wrong evaluation split")
    if case.get("review_queue_case_id") and not sealed:
        source_id = str(UUID(case["review_queue_case_id"]))
        response = subprocess.run(
            [
                "python3",
                str(REPO / "scripts/admin.py"),
                "--prod",
                "GET",
                "request-following/" + source_id + "/export",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO,
        )
        if response.returncode:
            raise ValueError("current source eligibility unavailable")
        exported = json.loads(response.stdout)
        receipt = exported["eligibility"]
        if any(
            receipt.get(k) != case.get(k)
            for k in ("creator_id", "thread_id", "eligibility_key", "source_sha256")
        ):
            raise ValueError("source eligibility changed")
        if datetime.fromisoformat(receipt["expires_at"]) <= datetime.now(UTC):
            raise ValueError("source expired")
        if require_review:
            original = exported["candidate"]["intent_review"]
            review = case.get("intent_review") or {}
            if (
                original.get("decision") != "approved"
                or review.get("decision") != "source_reviewed"
                or review.get("source_sha256") != receipt["source_sha256"]
                or review.get("source_review_version") != original.get("version")
                or review.get("expectations") != original.get("expectations")
            ):
                raise ValueError("original human review changed or missing")
            return review["expectations"]
        return None
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
            "request-following/"
            + ("comparison-holdout-eligibility?" if sealed else "comparison-eligibility?")
            + query,
            *(["--comparison-evaluator"] if sealed else []),
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
    index_path = directory / "derived-artifacts.json"
    if index_path.exists():
        index = read(_private_file(index_path))
        affected = {c["id"] for c in packet["cases"]}
        retained = []
        for entry in index.get("artifacts", []):
            if affected.intersection(entry.get("cases", [])):
                for name in entry.get("files", []):
                    if Path(name).name != name:
                        raise ValueError("derived artifact must stay in private store")
                    _private_file(directory / name).unlink(missing_ok=True)
            else:
                retained.append(entry)
        write(index_path, {"artifacts": retained})
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


def _execute(
    packet,
    directory,
    *,
    live,
    approved_cost=None,
    approval_sha=None,
    registration=None,
    envelope=None,
    unavailable_models=None,
):
    directory = private_root(directory)
    if registration is not None:
        from app.services.creator_comparison_holdout import validate_registration

        validate_registration(packet, registration)
    sealed = registration is not None
    preflight = prepare_packet(packet, holdout=sealed)
    envelope_id = None
    if envelope is not None:
        from uuid import UUID

        envelope_id = "comparison:" + str(UUID(envelope["id"]))
        if (
            envelope.get("sha256") != digest({k: v for k, v in envelope.items() if k != "sha256"})
            or packet["sha256"] not in envelope.get("batches", {})
            or approved_cost != envelope["batches"][packet["sha256"]]
            or not 0 < envelope["approved_total_usd"] <= 5.66
        ):
            raise ValueError("invalid approved pilot envelope")
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
            _eligibility(case, sealed=sealed)
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
            if unavailable_models and arm.model in unavailable_models:
                journal["rows"][key] = {
                    **prepare_case(case, arm, allow_holdout=sealed),
                    "repeat": repeat,
                    "status": "blocked",
                    "reason": unavailable_models[arm.model],
                    "invocations": [],
                    "first_attempt_parsed": False,
                }
                write(journal_path, journal)
                continue
            if live:
                try:
                    _eligibility(case, sealed=sealed)  # every arm, not merely at import/run start
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
                            _eligibility(case, sealed=sealed)
                        except Exception as exc:
                            from app.agents._runtime import TerminalError

                            eligibility_lost = True
                            raise TerminalError("comparison eligibility unavailable") from exc
                        try:
                            result = dispatcher.invoke(**kwargs)
                        except Exception as error:
                            cause = error.__cause__
                            detail = getattr(cause, "body", None)
                            attempts.append(
                                {
                                    "model": kwargs["model"],
                                    "finish": "provider_error",
                                    "error_class": type(error).__name__,
                                    "provider_http_status": getattr(cause, "status_code", None),
                                    "provider_error": str(detail)[:2000] if detail else None,
                                }
                            )
                            raise
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

            row = {
                **prepare_case(case, arm, allow_holdout=sealed),
                "repeat": repeat,
                "status": "started",
            }
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
                            test_run_id=(
                                envelope_id + ":" + run_id
                                if envelope_id
                                else f"comparison:{run_id}"
                            )
                            if live
                            else None,
                            estimated_max_cost_usd=approved_cost,
                            reservation_approved=live,
                            extra={
                                "skip_agent_run_persist": True,
                                "skip_langfuse_trace": True,
                                "skip_comparison_capture": True,
                                "comparison_case_id": case["id"],
                                **(
                                    {
                                        "paid_experiment_envelope_id": envelope_id,
                                        "approved_envelope_max_usd": envelope["approved_total_usd"],
                                    }
                                    if envelope_id
                                    else {}
                                ),
                            },
                        ),
                    )
                else:
                    # Replay validates recorded bytes, never reserves or constructs SDKs.
                    raw = case["raw_text"]
                    if hasattr(agent, "wire_contract"):
                        base = agent_class(case["agent"])(None)
                        value = (
                            base.parse(raw, base.Input.model_validate(case["input"])).model_dump(
                                mode="json"
                            )
                            if case["agent"] == "main_creator"
                            else json.loads(raw)
                        )
                        raw = json.dumps(agent.wire_contract.encode(value))
                    output = agent.parse(raw, agent.Input.model_validate(case["input"]))
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
            "blocked": sum(r["status"] == "blocked" for r in journal["rows"].values()),
            "actual_export": "unassessed",
            "inference_only": True,
            **({} if sealed else {"private_journal": str(journal_path)}),
            "split": "holdout" if sealed else "development",
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
                    {"id": envelope_id + ":" + run_id if envelope_id else f"comparison:{run_id}"},
                ).one()
            summary.update(settled_usd=float(cost[0]), reserved_or_unknown_usd=float(cost[1]))
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imported = sub.add_parser("import-copilot")
    imported.add_argument("--source", required=True)
    imported.add_argument("--debug", required=True)
    imported.add_argument("--run-id", required=True)
    imported.add_argument("--event-index", required=True, type=int)
    imported.add_argument("--output", required=True)
    persistence = sub.add_parser("verify-persistence")
    persistence.add_argument("--packet", required=True)
    persistence.add_argument("--journal", required=True)
    persistence.add_argument("--execution-contexts")
    persistence.add_argument("--directory", required=True)
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
    sealed_cmd = sub.add_parser("sealed-run")
    sealed_cmd.add_argument("--packet", required=True)
    sealed_cmd.add_argument("--registration", required=True)
    sealed_cmd.add_argument("--directory", required=True)
    sealed_cmd.add_argument("--live", action="store_true")
    sealed_cmd.add_argument("--approve-max-usd", type=float)
    sealed_cmd.add_argument("--approval-sha256")
    sealed_cmd.add_argument("--envelope")
    register = sub.add_parser("seal")
    register.add_argument("--packet", required=True)
    register.add_argument("--development-groups", required=True)
    register.add_argument("--output", required=True)
    approve = sub.add_parser("approve-envelope")
    approve.add_argument("--packet", action="append", required=True)
    approve.add_argument("--max-usd", type=float, required=True)
    approve.add_argument("--output", required=True)
    for name in ("validate", "dry-run", "replay", "live"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--packet", required=True)
        cmd.add_argument("--directory", required=name in ("replay", "live"))
        if name == "live":
            cmd.add_argument("--approve-max-usd", type=float, required=True)
            cmd.add_argument("--approval-sha256", required=True)
            cmd.add_argument("--envelope")
    args = parser.parse_args(argv)
    if args.command == "verify-persistence":
        import asyncio

        from app.services.creator_comparison_persistence import verify_saved_candidate
        from app.services.creator_model_comparison import runtime_digest, validate_packet

        packet = read(_private_file(args.packet))
        validate_packet(packet)
        if any(c.get("split") == "holdout" for c in packet["cases"]):
            raise ValueError("holdout persistence belongs to the sealed evaluator")
        journal = read(_private_file(args.journal))
        if journal.get("packet_sha256") != packet["sha256"]:
            raise ValueError("journal does not match frozen packet")
        contexts = read(_private_file(args.execution_contexts)) if args.execution_contexts else {}
        directory = private_root(args.directory)
        app_root = Path(__file__).resolve().parents[1]
        verifier_hash = digest(
            {
                str(path.relative_to(app_root)): path.read_text()
                for path in sorted(
                    [
                        app_root / "cli/compare_creator_models.py",
                        app_root / "kria/drafts.py",
                        app_root / "services/creator_comparison_persistence.py",
                        app_root / "services/creator_capabilities.py",
                        *app_root.glob("services/kria_editor_ops*.py"),
                        *app_root.glob("services/editor_ops_v2/*.py"),
                    ]
                )
            }
        )
        identity = digest([journal, contexts, runtime_digest(), verifier_hash])
        target = directory / (packet["sha256"] + ".persistence-" + identity[:16] + ".json")
        if target.exists():
            result = read(_private_file(target))
        else:

            async def verify():
                cases = {c["id"]: c for c in packet["cases"]}
                rows = []
                for key, row in journal.get("rows", {}).items():
                    case = cases.get(row.get("case"))
                    bindings = [
                        b
                        for b in packet.get("bindings", [])
                        if b["case"] == row.get("case") and b["arm"] == row.get("arm")
                    ]
                    if (
                        case is None
                        or len(bindings) != 1
                        or any(
                            row.get(k) != bindings[0].get(k)
                            for k in (
                                "input_sha256",
                                "prompt_sha256",
                                "spec_sha256",
                                "runtime_sha256",
                            )
                        )
                    ):
                        raise ValueError("candidate row differs from frozen bindings")
                    record = {
                        "key": key,
                        "case": row["case"],
                        "arm": row["arm"],
                        "model_status": row["status"],
                        "original_structural_failures": row.get("structural_failures", []),
                    }
                    # A synthetic structural rejection is a distinct result
                    # from compiling returned bytes against the owned source
                    # execution base. Preserve the original journal/verdict.
                    if row["status"] not in {"parsed", "structural_failed"} or not row.get(
                        "output"
                    ):
                        record.update(status="not_run", reason="candidate_not_parsed")
                    else:
                        try:
                            record.update(
                                await verify_saved_candidate(
                                    case, row["output"], execution_context=contexts.get(case["id"])
                                )
                            )
                        except Exception as exc:
                            record.update(
                                status="verification_failed", error_class=type(exc).__name__
                            )
                    rows.append(record)
                return rows

            result = {
                "mode": "scratch_postgres_persistence",
                "packet_sha256": packet["sha256"],
                "verification_runtime_sha256": runtime_digest(),
                "verifier_sha256": verifier_hash,
                "actual_export": "unassessed",
                "rows": asyncio.run(verify()),
            }
            write(target, result)
            index_path = directory / "derived-artifacts.json"
            index = read(_private_file(index_path)) if index_path.exists() else {"artifacts": []}
            index["artifacts"].append(
                {
                    "cases": [c["id"] for c in packet["cases"]],
                    "files": [target.name],
                    "expires_at": min(
                        (c["expires_at"] for c in packet["cases"] if c.get("expires_at")),
                        default=None,
                    ),
                }
            )
            write(index_path, index)
        print(
            json.dumps(
                {
                    "mode": result["mode"],
                    "rows": len(result["rows"]),
                    "verified": sum(r["status"] == "verified" for r in result["rows"]),
                    "not_run": sum(r["status"] == "not_run" for r in result["rows"]),
                    "not_applicable": sum(r["status"] == "not_applicable" for r in result["rows"]),
                    "failed": sum(r["status"] == "verification_failed" for r in result["rows"]),
                    "private_report": str(target),
                    "actual_export": "unassessed",
                }
            )
        )
        return
    if args.command == "import-copilot":
        from app.services.creator_comparison_sources import from_recorded_copilot

        case = from_recorded_copilot(
            read(_private_file(args.source)),
            read(_private_file(args.debug)),
            run_id=args.run_id,
            request_event_index=args.event_index,
        )
        output = _private_file(args.output)
        if output.exists():
            raise ValueError("recorded imports are immutable")
        write(output, case)
        print(
            json.dumps(
                {
                    "id": case["id"],
                    "source_kind": case["source_kind"],
                    "input_sha256": case["input_sha256"],
                    "quality_verdict": "unassessed",
                }
            )
        )
        return
    if args.command == "approve-envelope":
        import uuid

        batches = {}
        for path in args.packet:
            packet = read(_private_file(path))
            preflight = prepare_packet(packet)
            if preflight["estimated_max_usd"] > 2:
                raise ValueError("batch exceeds $2")
            batches[packet["sha256"]] = preflight["estimated_max_usd"]
        if not 0 < args.max_usd <= 5.66 or sum(batches.values()) > args.max_usd:
            raise ValueError("pilot estimate exceeds approved total")
        envelope = {
            "id": str(uuid.uuid4()),
            "approved_total_usd": args.max_usd,
            "batches": batches,
            "approval": "user approved $5.66 pilot in this conversation",
        }
        envelope["sha256"] = digest(envelope)
        output = _private_file(args.output)
        if output.exists():
            raise ValueError("approval envelopes are immutable")
        write(output, envelope)
        print(
            json.dumps(
                {
                    "approved_total_usd": args.max_usd,
                    "estimated_max_usd": sum(batches.values()),
                    "batches": len(batches),
                }
            )
        )
        return
    if args.command == "seal":
        from app.services.creator_comparison_holdout import register_holdout

        packet = read(_private_file(args.packet))
        registration = register_holdout(
            packet, development_groups=read(_private_file(args.development_groups))
        )
        output = _private_file(args.output)
        if output.exists():
            raise ValueError("registrations are immutable")
        write(output, registration)
        print(json.dumps(registration))
        return
    if args.command == "sealed-run":
        packet = read(_private_file(args.packet))
        registration = read(_private_file(args.registration))
        result = _execute(
            packet,
            args.directory,
            live=args.live,
            approved_cost=args.approve_max_usd,
            approval_sha=args.approval_sha256,
            registration=registration,
            envelope=read(_private_file(args.envelope)) if args.envelope else None,
        )
        print(json.dumps(result))
        return
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
                    envelope=read(_private_file(args.envelope))
                    if getattr(args, "envelope", None)
                    else None,
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
