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
