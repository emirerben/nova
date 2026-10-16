#!/usr/bin/env python3
"""Private capture -> proposed cases -> human review -> existing incident corpus.

No provider calls, prompt mutations, production writes or inferred pass verdicts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src/apps/api"
STAGES = ("understanding", "handoff", "operations", "persistence", "rendering")
CHANNELS = ("understanding", "persisted_execution", "rendered_output")
CONTEXT = (
    "requests",
    "media",
    "followups",
    "manual_edits",
    "agent_decisions",
    "saved_operations",
    "final_output",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def private_store(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Evidence store must be outside the repository")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("Evidence store must have mode 700")
    return path


def safe_id(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", value):
        raise ValueError("Use a lowercase opaque case id")
    return value


def admin_get(route, prod):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/admin.py"),
            *(["--prod"] if prod else []),
            "GET",
            route,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        # CLI errors can contain response bodies. Never copy them into a public report.
        raise ValueError("Admin GET failed; inspect access separately with scripts/admin.py")
    return json.loads(result.stdout)


def capture(args):
    store = private_store(args.store)
    thread = str(uuid.UUID(args.thread))
    data = {
        "source": "production" if args.prod else "local",
        "thread_id": thread,
        "captured_at": datetime.now(UTC).isoformat(),
        "sections": {},
        "missing": {},
    }
    for section in ("events", "turns"):
        rows, cursor, seen = [], None, set()
        try:
            for _ in range(1000):
                query = urlencode({"cursor": cursor}) if cursor else ""
                page = admin_get(f"creation-threads/{thread}/{section}?{query}", args.prod)
                if page.get("thread_id") != thread or not isinstance(page.get(section), list):
                    raise ValueError("Unexpected capture response")
                rows.extend(page[section])
                cursor = page.get("next_cursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("Repeated pagination cursor")
                seen.add(cursor)
            else:
                raise ValueError("Pagination limit reached")
            data["sections"][section] = rows
        except (ValueError, subprocess.TimeoutExpired):
            data["missing"][section] = "capture_failed_or_incomplete"
    for section, route in (
        ("integrity", f"creation-threads/{thread}/integrity"),
        ("trace", f"kria/trace?{urlencode({'thread_id': thread})}"),
    ):
        try:
            data["sections"][section] = admin_get(route, args.prod)
        except (ValueError, subprocess.TimeoutExpired):
            data["missing"][section] = "capture_failed"
    integrity = data["sections"].get("integrity", {})
    item = integrity.get("edges", {}).get("plan_item", {})
    linked = []
    if item.get("exists") and item.get("coherent") and item.get("id"):
        linked.append(("plan_item", f"plan-items/{uuid.UUID(item['id'])}/debug"))
    job_ids = {job for turn in data["sections"].get("turns", []) for job in turn.get("job_ids", [])}
    video = integrity.get("video") or {}
    if video.get("job_id"):
        job_ids.add(video["job_id"])
    for job_id in sorted(job_ids):
        linked.append((f"job-{uuid.UUID(job_id)}", f"jobs/{uuid.UUID(job_id)}/debug"))
    for section, route in linked:
        try:
            data["sections"][section] = admin_get(route, args.prod)
        except (ValueError, subprocess.TimeoutExpired):
            data["missing"][section] = "capture_failed"
    # Linked debug metadata preserves available media/agent/output references.
    # It never downloads media or upgrades a render status to output proof.
    # Immutable snapshots retain earlier requests/corrections when sessions evolve.
    target = store / "captures" / f"{digest(data)}.json"
    write(target, data)
    print(json.dumps({"capture": str(target), "missing": data["missing"]}))
    return 0 if not data["missing"] else 2


def import_capture(args):
    store = private_store(args.store)
    data = read(args.input)
    thread = str(uuid.UUID(data["thread_id"]))
    key = "session-" + digest(thread)[:16]
    index_path = store / "index.json"
    index = read(index_path) if index_path.exists() else {}
    previous = index.get(key)
    if previous and previous["split"] != args.split:
        raise ValueError("Conversation split is immutable; derivatives inherit it")
    # Content digest excludes capture timestamp, preserving idempotency on unchanged reads.
    content = {k: v for k, v in data.items() if k != "captured_at"}
    source_hash = digest(content)
    target = store / "sessions" / key / f"{source_hash}.json"
    write(target, data)
    index[key] = {"split": args.split, "source_sha256": source_hash, "path": str(target)}
    write(index_path, index)
    print(
        json.dumps(
            {
                "session": key,
                "split": args.split,
                "changed": not previous or previous["source_sha256"] != source_hash,
            }
        )
    )


def propose(args):
    store = private_store(args.store)
    index = read(store / "index.json")
    session = index[args.session]
    if session["split"] == "holdout":
        raise ValueError("Holdout conversations cannot produce development candidates")
    case_id = safe_id(args.id)
    path = store / "candidates" / f"{case_id}.json"
    if path.exists():
        raise ValueError("Candidate exists; preserve its review/history, use a new id")
    source = read(session["path"])
    context = {name: {"status": "missing", "source_pointers": []} for name in CONTEXT}
    events = source.get("sections", {}).get("events", [])
    requests = []
    for i, event in enumerate(events):
        pointer = f"/sections/events/{i}"
        kind = event.get("kind", "")
        categories = []
        if kind == "user_message":
            categories = ["requests" if not requests else "followups"]
            requests.append((pointer + "/content", event.get("content", "")))
        if kind in ("media_added", "action_remove_media"):
            categories.append("media")
        if kind == "draft_applied":
            categories.append("saved_operations")
        for name in categories:
            context[name]["status"] = "available_unreviewed"
            context[name]["source_pointers"].append(pointer)
    if source.get("sections", {}).get("turns"):
        context["agent_decisions"] = {
            "status": "available_unreviewed",
            "source_pointers": ["/sections/turns"],
        }
    # Source excerpts are proposals, never inferred expected outcomes.
    variation_instructions = {
        "paraphrase": "Rewrite the initial request with the same constraints; review equivalence.",
        "combined_requests": "Combine requests; resolve superseded constraints.",
        "different_footage": "Substitute media facts and review which expectations still apply.",
        "multi_turn_correction": "Change one constraint in a later turn.",
    }
    candidate = {
        "schema_version": 1,
        "id": case_id,
        "session": args.session,
        "source_sha256": session["source_sha256"],
        "split": session["split"],
        "tickets": args.ticket,
        "fix_prs": [],
        "loss_stage": "unknown",
        "context": context,
        "request_excerpts": [{"source_pointer": p, "text": t} for p, t in requests],
        "expectations": [],
        "ambiguities": ["Creator-derived expectations need review"],
        "evidence": {
            name: {"status": "missing", "level": None, "artifact": None} for name in CHANNELS
        },
        "variations": [
            {
                "kind": kind,
                "status": "needs_authoring",
                "request": None,
                "proposal": variation_instructions[kind],
                "expected_changes": None,
            }
            for kind in (
                "paraphrase",
                "combined_requests",
                "different_footage",
                "multi_turn_correction",
            )
        ],
        "incident": None,
        "review": None,
    }
    write(path, candidate)
    print(str(path))


def payload(candidate):
    return {k: v for k, v in candidate.items() if k != "review"}


def pointer_exists(data, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    try:
        for bit in pointer[1:].split("/"):
            bit = bit.replace("~1", "/").replace("~0", "~")
            data = data[int(bit)] if isinstance(data, list) else data[bit]
        return data is not None
    except (KeyError, IndexError, ValueError, TypeError):
        return False


def validate_candidate(candidate, store):
    safe_id(candidate["id"])
    session = read(store / "index.json")[candidate["session"]]
    if candidate["split"] != "development" or session["split"] != "development":
        raise ValueError("Holdout cannot be promoted")
    if candidate["source_sha256"] != session["source_sha256"]:
        raise ValueError("Source changed; re-propose and re-review")
    if candidate["loss_stage"] not in (*STAGES, "unknown"):
        raise ValueError("Invalid loss stage")
    if candidate["ambiguities"] or not candidate["expectations"]:
        raise ValueError("Resolve ambiguities and record creator-derived expectations")
    source = read(session["path"])
    if digest({k: v for k, v in source.items() if k != "captured_at"}) != session["source_sha256"]:
        raise ValueError("Source file changed outside import; reimport and re-review")
    for expectation in candidate["expectations"]:
        if not expectation.get("text") or not expectation.get("source_pointers"):
            raise ValueError("Every expectation needs creator-source pointers")
        if not all(
            re.fullmatch(r"/sections/events/\d+/content", p)
            and pointer_exists(source, p)
            and source["sections"]["events"][int(p.split("/")[3])].get("kind") == "user_message"
            for p in expectation["source_pointers"]
        ):
            raise ValueError("Expectation source pointer does not resolve")
    for channel in CHANNELS:
        evidence = candidate["evidence"][channel]
        if evidence["status"] not in ("missing", "unassessed", "pass", "fail"):
            raise ValueError("Invalid evidence status")
        if evidence["status"] in ("pass", "fail"):
            allowed = {
                "understanding": ("offline_replay", "live_provider"),
                "persisted_execution": ("integration_db", "production_capture"),
                "rendered_output": ("render_artifact", "native_device"),
            }[channel]
            artifact = Path(evidence.get("artifact") or "")
            if (
                evidence.get("level") not in allowed
                or not artifact.is_file()
                or not evidence.get("command")
                or not evidence.get("measured_at")
                or evidence.get("sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest()
            ):
                raise ValueError(f"{channel} requires independently measured evidence")
    return session


def review(args):
    store = private_store(args.store)
    candidate_path = Path(args.candidate).resolve()
    if (store / "candidates").resolve() != candidate_path.parent:
        raise ValueError("Candidate must be in this store's candidates directory")
    candidate = read(candidate_path)
    if args.decision == "approved":
        validate_candidate(candidate, store)
        if not args.redaction_checked:
            raise ValueError("Approval requires a human redaction check")
    if not args.reviewer.strip() or not args.rationale.strip():
        raise ValueError("Reviewer and rationale are required")
    candidate["review"] = {
        "decision": args.decision,
        "reviewer": args.reviewer,
        "rationale": args.rationale,
        "redaction_checked": args.redaction_checked,
        "at": datetime.now(UTC).isoformat(),
        "sha256": digest(payload(candidate)),
    }
    write(store / "reviews" / f"{digest(candidate)}.json", candidate)
    write(args.candidate, candidate)
    print(json.dumps({"id": candidate["id"], "decision": args.decision}))


def promote(args):
    store = private_store(args.store)
    candidate_path = Path(args.candidate).resolve()
    if (store / "candidates").resolve() != candidate_path.parent:
        raise ValueError("Candidate must be in this store's candidates directory")
    candidate = read(candidate_path)
    validate_candidate(candidate, store)
    approval = candidate.get("review") or {}
    if (
        approval.get("decision") != "approved"
        or not approval.get("redaction_checked")
        or approval.get("sha256") != digest(payload(candidate))
    ):
        raise ValueError("Current proposal needs human approval")
    sys.path.insert(0, str(API))
    from tests.incidents.loader import unresolved_reference, validate_against_current_schemas
    from tests.incidents.models import IncidentRecord

    record = IncidentRecord.model_validate(candidate["incident"])
    if record.id != candidate["id"]:
        raise ValueError("Incident id must equal candidate id")
    validate_against_current_schemas(record)
    if record.repro.status != "available" or unresolved_reference(record.repro.command):
        raise ValueError("Promotion needs a resolvable existing regression command")
    if not any(
        getattr(record.expect, key) is not None
        for key in ("contract", "refusal", "cloud", "question", "output_facts", "route")
    ):
        raise ValueError("Promotion needs executable expectations")
    target = API / "tests/fixtures/incidents" / f"{record.id}.json"
    if target.exists():
        raise ValueError("Refusing to overwrite an existing incident")
    raw = record.model_dump(mode="json", exclude_unset=True)
    provenance = (
        f"Learning loop {candidate['session']} capture {candidate['source_sha256']}; "
        f"reviewed by {approval['reviewer']} at {approval['at']}."
    )
    raw.setdefault("approved", {})["provenance"] = provenance
    # Do not copy private source payloads or evidence paths into git.
    text = json.dumps(raw)
    if re.search(r"https?://|gs://|s3://|Bearer\s|AIza|sk-[A-Za-z0-9]{16}", text):
        raise ValueError(
            "Remove URLs/credentials from the promoted fixture; use ticket ids and opaque media ids"
        )
    write(target, raw)
    print(
        json.dumps(
            {
                "fixture": str(target),
                "verification": record.repro.command,
                "claim": "Promoted reviewed expectations; no test or render has run",
            }
        )
    )


def report(args):
    store = private_store(args.store)
    index_path = store / "index.json"
    index = read(index_path) if index_path.exists() else {}
    fixtures = [(p, read(p)) for p in sorted((API / "tests/fixtures/incidents").glob("*.json"))]
    gates = read(API / "tests/gates/reliability-gates.json")["gates"]
    journeys = read(ROOT / "scripts/ci/journey-manifest.json")["journeys"]
    fixture_index = {}
    for fixture in sorted((API / "tests/fixtures").rglob("*.json")):
        for ticket in set(re.findall(r"KRI-?\d+", fixture.read_text().upper())):
            normalized = "KRI-" + ticket.removeprefix("KRI").lstrip("-")
            fixture_index.setdefault(normalized, []).append(str(fixture.relative_to(ROOT)))
    rows = []
    for path in sorted((store / "candidates").glob("*.json")):
        case = read(path)
        session = index.get(case["session"], {})
        if session.get("split") == "holdout":
            continue
        tickets = set(case["tickets"])
        matching = [g for g in gates if tickets.intersection(g["tickets"])]
        review_state = (case.get("review") or {}).get("decision", "proposed")
        if case.get("review") and case["review"]["sha256"] != digest(payload(case)):
            review_state = "stale_review"
        if case["source_sha256"] != session.get("source_sha256"):
            review_state = "stale_source"
        rows.append(
            {
                "id": case["id"],
                "tickets": sorted(tickets),
                "review": review_state,
                "loss_stage": case["loss_stage"],
                "evidence": case["evidence"],
                "missing_context": [
                    k for k, v in case["context"].items() if v["status"] == "missing"
                ],
                "incident_fixtures": [
                    p.name for p, r in fixtures if r["incident"]["ticket"] in tickets
                ],
                "gates": [g["id"] for g in matching],
                "related_fixture_references_only": sorted(
                    {p for ticket in tickets for p in fixture_index.get(ticket, [])}
                ),
                "fix_prs_references_only": sorted(
                    set(case["fix_prs"] + [p for g in matching for p in g["fix_prs"]])
                ),
                "journeys": [
                    j["id"] for j in journeys if any(t.lower() in j["id"] for t in tickets)
                ],
                "open_neighbours": [n for g in matching for n in g.get("neighbours_gap", [])],
            }
        )
    output = {
        "schema_version": 1,
        "claim": "Inventory only; references and merged PRs are not measured proof",
        "sessions": len(index),
        "holdout_sessions": sum(s["split"] == "holdout" for s in index.values()),
        "existing_incidents": len(fixtures),
        "existing_gates": len(gates),
        "cases": rows,
        "repository_inventory": [
            {
                "ticket": ticket,
                "fixtures_references_only": paths,
                "incident_records": [
                    p.name for p, r in fixtures if r["incident"]["ticket"] == ticket
                ],
                "gates": [g["id"] for g in gates if ticket in g["tickets"]],
            }
            for ticket, paths in sorted(fixture_index.items())
        ],
    }
    if args.output:
        destination = Path(args.output).resolve()
        if store not in destination.parents:
            raise ValueError("Reports include private evidence references; output inside store")
        write(destination, output)
    else:
        print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="Private directory outside git, mode 700")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--thread", required=True)
    p.add_argument("--prod", action="store_true")
    p = sub.add_parser("import")
    p.add_argument("--input", required=True)
    p.add_argument("--split", choices=("development", "holdout"), required=True)
    p = sub.add_parser("propose")
    p.add_argument("--session", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--ticket", action="append", default=[])
    p = sub.add_parser("review")
    p.add_argument("--candidate", required=True)
    p.add_argument("--decision", choices=("approved", "rejected"), required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--redaction-checked", action="store_true")
    p = sub.add_parser("promote")
    p.add_argument("--candidate", required=True)
    p = sub.add_parser("report")
    p.add_argument("--output")
    args = parser.parse_args()
    try:
        return {
            "capture": capture,
            "import": import_capture,
            "propose": propose,
            "review": review,
            "promote": promote,
            "report": report,
        }[args.command](args) or 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
