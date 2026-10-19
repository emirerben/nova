"""Point-in-time real-source import; no invented creator text or human review."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.creator_comparison_sources import from_recorded_main


def test_only_provably_empty_redaction_can_be_recovered():
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    now = datetime.now(UTC)
    inp = fixture["input"]
    message = inp["user_message"]
    source = {
        "eligibility": {
            "thread_id": "thread",
            "creator_id": "creator",
            "eligibility_key": "grant",
            "source_sha256": "a" * 64,
            "expires_at": (now + timedelta(days=1)).isoformat(),
        },
        "candidate": {
            "id": "queue-case",
            "intent_review": {
                "decision": "approved",
                "reviewer": "actual human",
                "version": 1,
                "expectations": [
                    {
                        "text": "Keep the requested intent",
                        "source_pointers": ["/sections/events/0/content"],
                    }
                ],
            },
        },
        "capture": {
            "sections": {"events": [{"kind": "user_message", "actor": "user", "content": message}]}
        },
    }
    recorded = {
        "id": "run",
        "prompt_version": "historical",
        "created_at": now.isoformat(),
        "input_json": copy.deepcopy(inp),
    }
    recorded["input_json"]["creator_direction"] = {
        "redacted": True,
        "chars": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    case = from_recorded_main(source, recorded)
    assert case["input"]["creator_direction"] == "" and case["source_kind"] == "exact_invocation"
    assert case["intent_review"]["decision"] == "source_reviewed"
    assert case["intent_review"]["reviewed_by"] == "actual human"
    recorded["input_json"]["creator_direction"]["chars"] = 10
    with pytest.raises(ValueError):
        from_recorded_main(source, recorded)
