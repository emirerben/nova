"""Import authorized private source records without pretending to review them.

Recorded inputs and reconstructed context are distinct. The only reversible
redaction is a proven empty string; nonempty redactions make a source incomplete.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta

from app.services.creator_comparison_capture import sanitize_input
from app.services.request_following_cases import digest


def _bound_case(export, *, input, agent, identity, captured_at, source_kind):
    receipt = export["eligibility"]
    review = export["candidate"]["intent_review"]
    if (
        review.get("decision") != "approved"
        or not review.get("reviewer")
        or not review.get("expectations")
    ):
        raise ValueError("source human review required")
    safe, complete = sanitize_input(input)
    if not complete:
        raise ValueError("required input contains removed capabilities")
    captured = datetime.fromisoformat(captured_at)
    expiry = min(datetime.fromisoformat(receipt["expires_at"]), captured + timedelta(days=30))
    if expiry <= datetime.now(UTC):
        raise ValueError("source expired")
    return {
        **{k: receipt[k] for k in ("creator_id", "thread_id", "eligibility_key", "source_sha256")},
        "id": digest([agent, identity, safe]),
        "agent": agent,
        "input": safe,
        "input_sha256": digest(safe),
        "split": "development",
        "source_kind": source_kind,
        "complete": True,
        "captured_at": captured.isoformat(),
        "expires_at": expiry.isoformat(),
        "review_queue_case_id": export["candidate"]["id"].removeprefix("queue-"),
        "event_group_ids": [receipt["thread_id"]],
        "intent_review": {
            "decision": "source_reviewed",
            "reviewed_by": review["reviewer"],
            "source_sha256": receipt["source_sha256"],
            "source_review_version": review["version"],
            "expectations": review["expectations"],
            "input_sha256": digest(safe),
        },
        "quality_verdict": "unassessed",
        "original_prompt_available": False,
        "evaluation_prompt": "current pinned source prompt",
    }


def from_recorded_main(export, recorded):
    from app.agents.main_creator import MainCreatorAgent

    input = copy.deepcopy(recorded["input_json"])
    recovered = []
    for key, value in input.items():
        if isinstance(value, dict) and value.get("redacted"):
            if value.get("chars") != 0 or value.get("sha256") != hashlib.sha256(b"").hexdigest():
                raise ValueError("nonempty redaction cannot be recovered")
            input[key] = ""
            recovered.append(key)
    events = export["capture"]["sections"]["events"]
    if not any(
        e.get("kind") == "user_message"
        and e.get("actor") == "user"
        and e.get("content") == input["user_message"]
        for e in events
    ):
        raise ValueError("recorded request is not in the owned source")
    validated = MainCreatorAgent.Input.model_validate(input)
    result = _bound_case(
        export,
        input=validated.model_dump(mode="json"),
        agent="main_creator",
        identity=recorded["id"],
        captured_at=recorded["created_at"],
        source_kind="exact_invocation",
    )
    result.update(
        origin_agent_run_id=recorded["id"],
        origin_prompt_version=recorded["prompt_version"],
        origin_input_sha256=digest(input),
        recovered_empty_fields=recovered,
    )
    return result


def from_editor_snapshot(export, job, *, request_event_index):
    from app.agents.edit_copilot import EditCopilotInput
    from app.services.kria_editor_ops import build_editor_snapshot

    events = export["capture"]["sections"]["events"]
    event = events[request_event_index]
    if event.get("kind") != "user_message" or event.get("actor") != "user":
        raise ValueError("creator-authored source request required")
    if str(job.user_id) != export["eligibility"]["creator_id"]:
        raise ValueError("editor source owner mismatch")
    # Pin a persisted prior variant, not a manufactured editor/media example.
    variants = job.assembly_plan.get("variants", [])
    if not variants:
        raise ValueError("persisted editor variant required")
    snapshot = build_editor_snapshot(job, copy.deepcopy(variants[0]))
    prior = [
        {"role": e["actor"], "content": e["content"]}
        for e in events[:request_event_index]
        if e.get("actor") in ("user", "assistant") and e.get("content")
    ][-12:]
    original = next(
        e["content"] for e in events if e.get("kind") == "user_message" and e.get("actor") == "user"
    )
    input = EditCopilotInput(
        utterance=event["content"],
        original_request=original,
        prior_turns=prior,
        variant_snapshot=snapshot,
    )
    result = _bound_case(
        export,
        input=input.model_dump(mode="json"),
        agent="edit_copilot",
        identity=[str(job.id), request_event_index],
        captured_at=datetime.now(UTC).isoformat(),
        source_kind="reconstructed_real",
    )
    result.update(
        source_pointer=f"/sections/events/{request_event_index}/content",
        source_job_id=str(job.id),
        snapshot_sha256=digest(snapshot),
        reconstruction_notes=[
            "Snapshot assembled with current server builder from persisted prior job.",
            "Original Copilot invocation was not recorded; this is not exact historical replay.",
        ],
    )
    return result
