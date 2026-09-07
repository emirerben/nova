from pathlib import Path

import pytest
from pydantic import ValidationError

from app.cli.kria_contracts import DEFAULT_SNAPSHOT, DEFAULT_TYPES, contract_json
from app.cli.kria_contracts import main as contracts_main
from app.cli.kria_replay import main as replay_main
from app.kria.contracts import KriaProblem, KriaToolReceipt, KriaTurnPlan
from app.kria.language import is_paraphrase_only
from app.kria.registry import KRIA_TOOLS
from app.kria.replay import load_fixture, replay_fixture, trace_as_json

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kria_turns" / "nermin-matcha-update.json"
FIXTURES = FIXTURE.parent


def test_nermin_matcha_turn_replays_to_receipt_backed_decision() -> None:
    fixture = load_fixture(FIXTURE)

    trace = replay_fixture(fixture)

    assert trace.response.turn_value == "decision"
    assert trace.receipts[0].status == "completed"
    assert trace.response.receipt_ids == ["inspect-project"]
    assert "Make a short update" not in trace.response.message
    assert [event["phase"] for event in trace.events] == ["accept", "plan", "tool", "observe"]


def test_replay_is_byte_deterministic() -> None:
    fixture = load_fixture(FIXTURE)
    assert trace_as_json(replay_fixture(fixture)) == trace_as_json(replay_fixture(fixture))


@pytest.mark.parametrize(
    ("name", "turn_value", "next_action"),
    [
        ("nermin-matcha-update.json", "decision", "prepare_draft"),
        ("nermin-travel-diary.json", "decision", "prepare_draft"),
        ("nermin-lifestyle-needs-footage.json", "question", "attach_media"),
    ],
)
def test_nermin_workflows_add_value_without_echo(
    name: str, turn_value: str, next_action: str
) -> None:
    fixture = load_fixture(FIXTURES / name)
    trace = replay_fixture(fixture)

    assert trace.response.turn_value == turn_value
    assert trace.response.next_actions == [next_action]
    assert fixture.user_message.casefold() not in trace.response.message.casefold()


def test_registry_manifest_is_typed_and_stable() -> None:
    manifest = KRIA_TOOLS.manifest()
    assert [(item["name"], item["version"], item["risk"]) for item in manifest] == [
        ("draft.apply_editor_ops", 1, "reversible_draft"),
        ("draft.apply_strategy", 1, "reversible_draft"),
        ("project.inspect", 1, "read"),
        ("render.request", 1, "approval_required"),
    ]
    assert manifest[0]["argument_schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "receipt_status", "turn_value", "next_action"),
    [
        ("render-approval-required.json", "awaiting_approval", "action", "approve_render"),
        (
            "ambiguous-render-dispatch.json",
            "outcome_unknown",
            "progress",
            "reconcile_dispatch",
        ),
    ],
)
def test_consequential_replays_never_claim_a_render_completed(
    name: str, receipt_status: str, turn_value: str, next_action: str
) -> None:
    trace = replay_fixture(load_fixture(FIXTURES / name))

    assert trace.receipts[-1].status == receipt_status
    assert trace.response.turn_value == turn_value
    assert trace.response.next_actions == [next_action]
    assert "rendered" not in trace.response.message.casefold()


def test_stale_replay_refreshes_without_executing_a_tool() -> None:
    trace = replay_fixture(load_fixture(FIXTURES / "stale-project-replan.json"))

    assert trace.receipts == []
    assert trace.response.turn_value == "recovery"
    assert trace.response.next_actions == ["refresh_replan"]
    assert [event["phase"] for event in trace.events] == [
        "accept",
        "plan",
        "policy",
        "observe",
    ]


def test_registry_contract_matches_checked_snapshot() -> None:
    assert DEFAULT_SNAPSHOT.read_text(encoding="utf-8") == contract_json()
    assert DEFAULT_TYPES.is_file()


def test_contract_cli_checks_snapshot_and_reports_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert contracts_main(["--check"]) == 0
    assert "Kria contract matches" in capsys.readouterr().out

    stale_snapshot = tmp_path / "tools.json"
    stale_snapshot.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        contracts_main(["--check", "--snapshot", str(stale_snapshot)])
    assert "Kria tool contract drifted" in capsys.readouterr().err

    stale_types = tmp_path / "kria-runtime-v2.generated.ts"
    stale_types.write_text("// stale\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        contracts_main(["--check", "--types", str(stale_types)])
    assert "Kria TypeScript contract drifted" in capsys.readouterr().err


def test_replay_cli_supports_readable_and_json_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert replay_main(["nermin-matcha-update"]) == 0
    readable = capsys.readouterr().out
    assert "receipt" in readable
    assert "accept" in readable

    assert replay_main(["nermin-matcha-update", "--json"]) == 0
    rendered = capsys.readouterr().out
    assert '"turn_value": "decision"' in rendered
    assert '"status": "completed"' in rendered


def test_replay_cli_rejects_missing_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="2"):
        replay_main(["missing", "--fixtures-root", str(tmp_path)])
    assert "fixture not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    [
        {
            "intent_id": "inspect",
            "tool_name": "project.inspect",
            "tool_version": 1,
            "status": "completed",
        },
        {
            "intent_id": "inspect",
            "tool_name": "project.inspect",
            "tool_version": 1,
            "status": "completed",
            "result": {},
            "error": {
                "code": "tool_failed",
                "phase": "tool",
                "message": "Inspection failed",
                "trace_id": "trace-1",
            },
        },
        {
            "intent_id": "inspect",
            "tool_name": "project.inspect",
            "tool_version": 1,
            "status": "failed",
        },
        {
            "intent_id": "render",
            "tool_name": "render.request",
            "tool_version": 1,
            "status": "outcome_unknown",
        },
    ],
)
def test_receipt_settlement_requires_matching_evidence(payload: dict) -> None:
    with pytest.raises(ValidationError):
        KriaToolReceipt.model_validate(payload)


def test_failed_receipt_accepts_typed_problem() -> None:
    problem = KriaProblem(
        code="tool_failed",
        phase="tool",
        message="Inspection failed",
        trace_id="trace-1",
    )

    receipt = KriaToolReceipt(
        intent_id="inspect",
        tool_name="project.inspect",
        tool_version=1,
        status="failed",
        error=problem,
    )

    assert receipt.error == problem


def test_plan_rejects_forward_dependency() -> None:
    with pytest.raises(ValidationError, match="dependencies must reference earlier intents"):
        KriaTurnPlan.model_validate(
            {
                "schema_version": 2,
                "mode": "act",
                "turn_value": "action",
                "intents": [
                    {
                        "intent_id": "first",
                        "tool_name": "project.inspect",
                        "tool_version": 1,
                        "depends_on": ["second"],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "mode": "respond", "turn_value": "decision"},
        {
            "schema_version": 2,
            "mode": "respond",
            "turn_value": "decision",
            "response": "Open on the whisking close-up.",
            "intents": [
                {
                    "intent_id": "inspect",
                    "tool_name": "project.inspect",
                    "tool_version": 1,
                }
            ],
        },
        {"schema_version": 2, "mode": "act", "turn_value": "action"},
    ],
)
def test_plan_requires_exactly_one_response_or_action_path(payload: dict) -> None:
    with pytest.raises(ValidationError):
        KriaTurnPlan.model_validate(payload)


@pytest.mark.parametrize(
    "assistant_message",
    [
        "You want a fast matcha launch video.",
        "I understand you want a fast matcha launch video.",
        "Make a fast matcha launch video",
    ],
)
def test_paraphrase_only_guard_rejects_machine_acknowledgements(
    assistant_message: str,
) -> None:
    assert is_paraphrase_only(
        user_message="Make a fast matcha launch video", assistant_message=assistant_message
    )


def test_paraphrase_only_guard_accepts_editorial_value() -> None:
    assert not is_paraphrase_only(
        user_message="Make a fast matcha launch video",
        assistant_message="Open on the whisking close-up; it creates immediate movement.",
    )
