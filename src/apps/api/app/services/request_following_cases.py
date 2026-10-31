"""Dependency-free source indexing shared by automatic capture and the CLI."""

import hashlib
import json

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


def build_candidate(source, *, case_id, session_id, source_sha256, tickets):
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
        "session": session_id,
        "source_sha256": source_sha256,
        "split": "development",
        "tickets": tickets,
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
    return candidate


def failed_pre_job_turns(source):
    sections = source.get("sections") or {}
    turns = {str(t["id"]): t for t in sections.get("turns") or [] if isinstance(t, dict)}
    failed = {
        key
        for key, turn in turns.items()
        if turn.get("status") == "failed" and not turn.get("job_ids")
    }
    for event in sections.get("events") or []:
        payload = event.get("payload") or {}
        coverage = payload.get("brief_coverage") or {}
        turn_id = str(payload.get("turn_id") or "")
        if (
            event.get("actor") == "assistant"
            and event.get("kind") == "assistant_response"
            and coverage.get("reason")
            in {
                "creator_planning_failed",
                "editor_planning_failed",
                "request_extraction_failed",
                "request_extraction_provider_failed",
            }
            and turn_id in turns
            and not turns[turn_id].get("job_ids")
        ):
            failed.add(turn_id)
    return frozenset(failed)


def diagnostics(source):
    turns = source["sections"].get("turns", [])
    failed = sorted(
        {t["id"] for t in turns if t["status"] == "failed"} | failed_pre_job_turns(source)
    )
    broken = [
        name
        for name, edge in source["sections"].get("integrity", {}).get("edges", {}).items()
        if not edge["coherent"]
    ]
    return {
        "failed_turns": failed,
        "broken_edges": broken,
        "missing": source["missing"],
        "intent_verdict": "unassessed",
        "rendered_output": "missing",
        "next_action": "Review creator intent and inspect turn receipts"
        if failed or broken
        else "Review creator intent; add a runnable regression and output evidence",
    }


def validate_intent(source, body):
    """Shared intent checks for the database API and private downloaded copies."""
    import re

    decision = body.get("decision")
    if decision not in {"approved", "needs_clarification", "rejected"}:
        raise ValueError("invalid_decision")
    for name, limit in (("reviewer", 120), ("rationale", 4000)):
        if not isinstance(body.get(name), str) or not body[name].strip() or len(body[name]) > limit:
            raise ValueError("reviewer_and_rationale_required")
    if body.get("loss_stage", "unknown") not in (*STAGES, "unknown"):
        raise ValueError("invalid_loss_stage")
    tickets = body.get("tickets", [])
    if (
        not isinstance(tickets, list)
        or len(tickets) > 20
        or any(
            not isinstance(ticket, str) or not re.fullmatch(r"KRI-[0-9]+", ticket)
            for ticket in tickets
        )
    ):
        raise ValueError("invalid_tickets")
    expectations = body.get("expectations", [])
    if not isinstance(expectations, list) or len(expectations) > 50:
        raise ValueError("invalid_expectations")
    if decision == "approved" and (not expectations or source.get("missing")):
        raise ValueError("approval_requires_expectations_and_complete_capture")
    events = source.get("sections", {}).get("events", [])
    for expectation in expectations:
        if not isinstance(expectation, dict) or not isinstance(expectation.get("text"), str):
            raise ValueError("empty_expectation")
        if not expectation["text"].strip() or len(expectation["text"]) > 2000:
            raise ValueError("empty_expectation")
        pointers = expectation.get("source_pointers", [])
        if not isinstance(pointers, list) or not 1 <= len(pointers) <= 20:
            raise ValueError("expectation_requires_creator_source")
        for pointer in pointers:
            match = (
                re.fullmatch(r"/sections/events/([0-9]+)/content", pointer)
                if isinstance(pointer, str)
                else None
            )
            if not match or int(match[1]) >= len(events):
                raise ValueError("expectation_requires_creator_source")
            event = events[int(match[1])]
            if (
                event.get("kind") != "user_message"
                or event.get("actor") != "user"
                or not event.get("content")
            ):
                raise ValueError("expectation_requires_creator_source")
    return {
        "decision": decision,
        "reviewer": body["reviewer"].strip(),
        "rationale": body["rationale"].strip(),
        "loss_stage": body.get("loss_stage", "unknown"),
        "tickets": tickets,
        "expectations": expectations,
    }


def validate_eligibility_receipt(source, source_hash, receipt):
    """Validate an operator's recorded-eligibility receipt for a private copy.

    This binds downloaded evidence, not live server consent or a new grant.
    Only the authenticated database API can re-check current server eligibility.
    """
    import re
    from datetime import UTC, datetime, timedelta

    owner = (source.get("sections", {}).get("integrity", {}).get("thread") or {}).get("creator_id")
    if (
        not owner
        or receipt.get("creator_id") != owner
        or receipt.get("thread_id") != source.get("thread_id")
        or receipt.get("source_sha256") != source_hash
        or not re.fullmatch(
            r"(?:internal_grant|training_consent):[0-9a-f-]{36}", receipt.get("eligibility_key", "")
        )
    ):
        raise ValueError("eligibility_receipt_mismatch")
    captured = datetime.fromisoformat(receipt.get("captured_at") or source.get("captured_at", ""))
    checked = datetime.fromisoformat(receipt["checked_at"])
    expires = datetime.fromisoformat(receipt["expires_at"])
    now = datetime.now(UTC)
    if any(value.tzinfo is None for value in (captured, checked, expires)) or (
        expires <= now
        or captured > now + timedelta(minutes=1)
        or checked > now + timedelta(minutes=1)
        or checked >= expires
        or expires > captured + timedelta(days=30, seconds=1)
    ):
        raise ValueError("eligibility_receipt_expired_or_invalid")
    return receipt
