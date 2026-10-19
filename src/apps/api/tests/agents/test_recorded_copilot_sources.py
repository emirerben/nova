"""Source-bound historical Copilot import: reject plausible but wrong context."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app.services import creator_comparison_sources as sources


def material():
    now = datetime.now(UTC)
    message = "Move the upper title to the bottom right"
    export = {
        "eligibility": {
            "creator_id": "owner",
            "thread_id": "thread",
            "eligibility_key": "grant",
            "source_sha256": "a" * 64,
            "expires_at": (now + timedelta(days=1)).isoformat(),
        },
        "candidate": {
            "id": "queue-source",
            "intent_review": {
                "decision": "approved",
                "reviewer": "human source reviewer",
                "version": 1,
                "expectations": [
                    {
                        "text": "Move the upper title",
                        "source_pointers": ["/sections/events/0/content"],
                    }
                ],
            },
        },
        "capture": {
            "sections": {
                "job-job": {
                    "id": "job",
                    "assembly_plan": {"variants": [{"text_elements": [{"text": "Later state"}]}]},
                },
                "events": [
                    {
                        "kind": "user_message",
                        "actor": "user",
                        "content": message,
                        "created_at": (now - timedelta(seconds=10)).isoformat(),
                    },
                    {
                        "kind": "user_message",
                        "actor": "user",
                        "content": "Next edit",
                        "created_at": (now + timedelta(seconds=10)).isoformat(),
                    },
                ],
            }
        },
    }
    run = {
        "id": "invocation",
        "agent_name": "nova.edit.copilot",
        "prompt_version": "historical",
        "created_at": now.isoformat(),
        "input_json": {
            "utterance": message,
            "prior_turns": [],
            "original_request": None,
            "variant_snapshot": {
                "allowed_op_families": ["text"],
                "text_bars": [{"id": "title", "text": "Original state"}],
            },
        },
    }
    debug = {"job": {"id": "job", "user_id": "owner"}, "agent_runs": [run]}
    return export, debug


def test_recorded_copilot_keeps_original_snapshot_and_source_review():
    export, debug = material()
    original = copy.deepcopy(debug)
    case = sources.from_recorded_copilot(export, debug, run_id="invocation", request_event_index=0)
    assert case["input"] == debug["agent_runs"][0]["input_json"]
    assert case["input"]["variant_snapshot"]["text_bars"][0]["text"] == "Original state"
    assert case["source_kind"] == "exact_invocation"
    assert case["origin_agent_run_id"] == "invocation"
    assert case["source_pointer"] == "/sections/events/0/content"
    assert case["intent_review"]["decision"] == "source_reviewed"
    assert case["quality_verdict"] == "unassessed"
    assert debug == original


@pytest.mark.parametrize(
    "failure",
    [
        "owner",
        "job",
        "agent",
        "message",
        "before",
        "after",
        "naive",
        "nested_redaction",
        "snapshot",
        "duplicate_run",
    ],
)
def test_wrong_or_incomplete_historical_context_is_refused(failure):
    export, debug = material()
    run = debug["agent_runs"][0]
    if failure == "owner":
        debug["job"]["user_id"] = "other"
    elif failure == "job":
        debug["job"]["id"] = "other"
    elif failure == "agent":
        run["agent_name"] = "nova.creator.main"
    elif failure == "message":
        run["input_json"]["utterance"] = "Manufactured request"
    elif failure == "before":
        run["created_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif failure == "after":
        run["created_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif failure == "naive":
        run["created_at"] = datetime.now().isoformat()
    elif failure == "nested_redaction":
        run["input_json"]["variant_snapshot"]["text_bars"] = [
            {"text": {"redacted": True, "chars": 10}}
        ]
    elif failure == "snapshot":
        run["input_json"]["variant_snapshot"] = {}
    elif failure == "duplicate_run":
        debug["agent_runs"].append(copy.deepcopy(run))
    with pytest.raises(ValueError):
        sources.from_recorded_copilot(export, debug, run_id="invocation", request_event_index=0)


def test_operator_cli_publishes_immutable_private_import(tmp_path, capsys):
    import json

    from app.cli.compare_creator_models import main
    from app.services.request_following_private_store import write

    tmp_path.chmod(0o700)
    export, debug = material()
    write(tmp_path / "source.json", export)
    write(tmp_path / "debug.json", debug)
    output = tmp_path / "case.json"
    args = [
        "import-copilot",
        "--source",
        str(tmp_path / "source.json"),
        "--debug",
        str(tmp_path / "debug.json"),
        "--run-id",
        "invocation",
        "--event-index",
        "0",
        "--output",
        str(output),
    ]
    main(args)
    printed = capsys.readouterr().out
    assert "Original state" not in printed and "Move the upper" not in printed
    assert json.loads(output.read_text())["source_kind"] == "exact_invocation"
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        main(args)
