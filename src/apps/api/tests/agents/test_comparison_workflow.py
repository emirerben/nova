"""Offline comparison acceptance: immutable arms, privacy and honest schema support."""

from dataclasses import asdict

import pytest

from app.agents.main_creator import MainCreatorAgent
from app.services.creator_model_comparison import (
    Arm,
    build_agent,
    prepare_case,
    schema_audit,
    validate_packet,
)


def test_arm_is_instance_local_and_unknown_model_fails():
    before = asdict(MainCreatorAgent.spec)
    agent = build_agent("main_creator", Arm("sol", "gpt-6.1-sol", "medium"), None)
    assert agent.spec.model == "gpt-6.1-sol" and not agent.spec.fallback_models
    assert asdict(MainCreatorAgent.spec) == before
    with pytest.raises(ValueError):
        build_agent("main_creator", Arm("bad", "gpt-imaginary", "medium"), None)


def test_native_schema_audit_does_not_claim_generic_dict_ops_are_strict():
    from app.agents.edit_copilot import EditCopilotOutput

    schema = EditCopilotOutput.model_json_schema()
    audit = schema_audit(schema, "gpt-6.1-sol")
    assert not audit["compatible"]
    assert any("open_object" in row for row in audit["blockers"])
    assert schema == EditCopilotOutput.model_json_schema()


def test_preparation_keeps_case_identity_and_never_calls_provider():
    import json
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    case = {
        "id": "control",
        "agent": "main_creator",
        "source_kind": "synthetic_control",
        "split": "development",
        "input": fixture["input"],
        "raw_text": '{"action":{"kind":"ask_user","question":"What video?"}}',
    }
    arm = Arm("sol", "gpt-6.1-sol", "medium")
    a = prepare_case(case, arm)
    b = prepare_case(case, arm)
    assert a == b and a["estimated_max_usd"] > 0
    assert "Help me choose" not in str(a)
    with pytest.raises(ValueError, match="holdout"):
        prepare_case({**case, "split": "holdout"}, arm)


def test_packet_tampering_and_expiry_fail():
    from app.services.request_following_cases import digest

    packet = {"schema_version": 1, "cases": [], "arms": [], "repetitions": 1, "seed": 727}
    packet["sha256"] = digest(packet)
    validate_packet(packet)
    with pytest.raises(ValueError, match="hash"):
        validate_packet({**packet, "seed": 728})


def test_sensitive_inputs_redact_utterance_and_snapshot():
    from app.agents.edit_copilot import EditCopilotAgent

    agent = EditCopilotAgent(None)
    projected = agent.project_input_for_observability(
        {"utterance": "secret", "variant_snapshot": {"captions": "private"}}
    )
    assert projected["redacted"] is True
    assert "secret" not in str(projected) and "private" not in str(projected)


def test_cli_freeze_replay_resume_is_offline_with_ledger_enabled(tmp_path, monkeypatch, capsys):
    import json
    from pathlib import Path

    from app.cli.compare_creator_models import main
    from app.config import settings

    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    tmp_path.chmod(0o700)
    fixture = Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
    packet = tmp_path / "packet.json"
    main(
        [
            "freeze",
            "--case",
            str(fixture),
            "--arm",
            "G:gemini-3.1-pro-preview:high",
            "--arm",
            "S:claude-sonnet-5-5:high",
            "--arm",
            "O:gpt-6.1-sol:medium",
            "--output",
            str(packet),
        ]
    )
    capsys.readouterr()
    # Any ledger/database or provider access would fail this offline run.
    from app.services import ai_cost_control

    monkeypatch.setattr(
        ai_cost_control, "reserve_paid_call", lambda *a: pytest.fail("paid reservation")
    )
    main(["replay", "--packet", str(packet), "--directory", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert result["parsed"] == 3 and result["mode"] == "recorded_replay"
    main(["replay", "--packet", str(packet), "--directory", str(tmp_path)])
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["completed_rows"] == 3
    journal = Path(resumed["private_journal"])
    assert journal.stat().st_mode & 0o077 == 0


def test_frozen_packet_rejects_prompt_drift(tmp_path, monkeypatch, capsys):
    import json
    from pathlib import Path

    from app.cli.compare_creator_models import main
    from app.services.creator_model_comparison import prepare_packet

    tmp_path.chmod(0o700)
    fixture = Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
    packet = tmp_path / "packet.json"
    main(
        ["freeze", "--case", str(fixture), "--arm", "O:gpt-6.1-sol:medium", "--output", str(packet)]
    )
    monkeypatch.setattr(MainCreatorAgent, "render_prompt", lambda *a: "different prompt")
    with pytest.raises(ValueError, match="drift"):
        prepare_packet(json.loads(packet.read_text()))


def test_live_refuses_without_manifest_approval_before_sdk(tmp_path, monkeypatch):
    from dataclasses import asdict

    from app.cli.compare_creator_models import _execute
    from app.config import settings
    from app.services.request_following_cases import digest

    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_paid_test_attribution_required", True)
    monkeypatch.setenv("NOVA_EVAL_MODE", "live")
    tmp_path.chmod(0o700)
    packet = {
        "schema_version": 1,
        "cases": [],
        "arms": [asdict(Arm("O", "gpt-6.1-sol", "medium"))],
        "repetitions": 1,
        "seed": 727,
    }
    packet["sha256"] = digest(packet)
    with pytest.raises(ValueError, match="approval"):
        _execute(packet, tmp_path, live=True)


def test_expired_private_sources_and_derived_outputs_are_purged(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.cli.compare_creator_models import purge_packet
    from app.services.request_following_private_store import write

    tmp_path.chmod(0o700)
    case = {
        "id": "source",
        "source_kind": "exact_invocation",
        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    source = tmp_path / "source.json"
    write(source, case)
    packet = {"sha256": "a" * 64, "cases": [{**case, "capture_file": str(source)}]}
    path = tmp_path / "packet.json"
    write(path, packet)
    journal = tmp_path / (packet["sha256"] + ".live.json")
    write(journal, {"output": "private"})
    assert purge_packet(path, tmp_path)["purged"]
    assert not path.exists() and not source.exists() and not journal.exists()


def test_known_unavailable_model_stays_in_denominator_without_submission(tmp_path):
    import json
    from dataclasses import asdict
    from pathlib import Path

    from app.cli.compare_creator_models import _execute
    from app.services.request_following_cases import digest

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    packet = {
        "schema_version": 1,
        "cases": [
            {
                "id": "case",
                "agent": "main_creator",
                "split": "development",
                "source_kind": "synthetic_control",
                "input": fixture["input"],
                "raw_text": fixture["raw_text"],
            }
        ],
        "arms": [asdict(Arm("S", "claude-sonnet-5-5", "high"))],
        "repetitions": 1,
        "seed": 727,
    }
    packet["sha256"] = digest(packet)
    tmp_path.chmod(0o700)
    result = _execute(
        packet,
        tmp_path,
        live=False,
        unavailable_models={"claude-sonnet-5-5": "account_credit_unavailable"},
    )
    assert result["blocked"] == 1 and result["parsed"] == 0 and result["completed_rows"] == 1


def test_retirement_removes_registered_derived_artifacts(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.cli.compare_creator_models import purge_packet
    from app.services.request_following_private_store import write

    tmp_path.chmod(0o700)
    packet = {
        "sha256": "b" * 64,
        "cases": [
            {"id": "case", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        ],
    }
    packet_path = tmp_path / "packet.json"
    write(packet_path, packet)
    artifact = tmp_path / "private-review.html"
    artifact.write_text("private")
    artifact.chmod(0o600)
    write(
        tmp_path / "derived-artifacts.json",
        {"artifacts": [{"cases": ["case"], "files": [artifact.name]}]},
    )
    assert purge_packet(packet_path, tmp_path)["purged"]
    assert not artifact.exists()


def test_production_schema_profile_is_opt_in_and_versioned(monkeypatch):
    from app.config import settings
    from app.services.creator_model_comparison import configure_creator_agent

    original = MainCreatorAgent(None)
    monkeypatch.setattr(settings, "main_creator_output_mode", "json")
    assert configure_creator_agent(original, "main_creator") is original
    monkeypatch.setattr(settings, "main_creator_output_mode", "native")
    configured = configure_creator_agent(original, "main_creator")
    assert configured.spec.response_schema
    assert configured.spec.prompt_version.endswith("+typed-presence-v2")
    assert MainCreatorAgent.spec.response_schema is None


def test_known_rejected_sonnet_native_profile_requires_explicit_json():
    from app.services.creator_model_comparison import Arm, build_agent

    for name in ("main_creator", "edit_copilot"):
        with pytest.raises(ValueError, match="compiled grammar"):
            build_agent(name, Arm("S", "claude-sonnet-5-5", "high", "native"), None)
        assert build_agent(name, Arm("S-json", "claude-sonnet-5-5", "high", "json"), None)


def test_persistence_regrades_structural_candidate_without_rewriting_live_status(
    tmp_path, monkeypatch, capsys
):
    import json
    from pathlib import Path

    from app.cli.compare_creator_models import main
    from app.services import creator_comparison_persistence

    tmp_path.chmod(0o700)
    fixture = Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
    packet = tmp_path / "packet.json"
    main(
        ["freeze", "--case", str(fixture), "--arm", "O:gpt-6.1-sol:medium", "--output", str(packet)]
    )
    capsys.readouterr()
    main(["replay", "--packet", str(packet), "--directory", str(tmp_path)])
    journal_path = Path(json.loads(capsys.readouterr().out)["private_journal"])
    journal = json.loads(journal_path.read_text())
    row = next(iter(journal["rows"].values()))
    row["status"] = "structural_failed"
    row["structural_failures"] = ["Synthetic reconstruction omitted a recorded link"]
    journal_path.write_text(json.dumps(journal))
    before = journal_path.read_bytes()
    seen = []

    async def verify(case, output, **kwargs):
        seen.append(case["id"])
        return {"status": "verified", "rendered": False}

    monkeypatch.setattr(creator_comparison_persistence, "verify_saved_candidate", verify)
    main(
        [
            "verify-persistence",
            "--packet",
            str(packet),
            "--journal",
            str(journal_path),
            "--directory",
            str(tmp_path),
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["verified"] == 1 and len(seen) == 1
    report = json.loads(Path(summary["private_report"]).read_text())
    assert report["rows"][0]["model_status"] == "structural_failed"
    assert report["rows"][0]["original_structural_failures"] == row["structural_failures"]
    assert journal_path.read_bytes() == before


def test_normalized_import_cannot_be_frozen_as_exact():
    import json
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    case = {
        "id": "historical",
        "agent": "main_creator",
        "source_kind": "exact_invocation",
        "split": "development",
        "input": fixture["input"],
        "origin_input_sha256": "different-recorded-input",
    }
    with pytest.raises(ValueError, match="exact"):
        prepare_case(case, Arm("O", "gpt-6.1-sol", "medium"))


def test_sol_max_effort_fails_experiment_preflight():
    with pytest.raises(ValueError, match="effort"):
        build_agent("main_creator", Arm("O", "gpt-6.1-sol", "max"), None)


@pytest.mark.parametrize("source_kind", ["exact_invocation", "reconstructed_real"])
@pytest.mark.parametrize("eligible", [True, False])
def test_unexpired_real_packet_rechecks_grant_and_only_retires_its_bodies(
    tmp_path, monkeypatch, source_kind, eligible
):
    from datetime import UTC, datetime, timedelta

    from app.cli import compare_creator_models as cli
    from app.services.request_following_private_store import read, write

    tmp_path.chmod(0o700)
    case = {
        "id": "reviewed-case",
        "source_kind": source_kind,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    source = tmp_path / "source.json"
    write(source, case)
    packet = {"sha256": "c" * 64, "cases": [{**case, "capture_file": str(source)}]}
    packet_path = tmp_path / "packet.json"
    write(packet_path, packet)
    journals = [tmp_path / (packet["sha256"] + suffix) for suffix in (".live.json", ".replay.json")]
    for path in journals:
        write(path, {"output": "private"})
    derived = tmp_path / "candidate.json"
    unrelated = tmp_path / "unrelated.json"
    write(derived, {"private": "derived"})
    write(unrelated, {"private": "another packet"})
    index = tmp_path / "derived-artifacts.json"
    write(
        index,
        {
            "artifacts": [
                {"cases": [case["id"]], "files": [derived.name]},
                {"cases": ["another-case"], "files": [unrelated.name]},
            ]
        },
    )
    checked = []

    def current_grant(row, *, require_review):
        checked.append((row["id"], require_review))
        if not eligible:
            raise ValueError("revoked or eligibility unavailable")

    monkeypatch.setattr(cli, "_eligibility", current_grant)
    assert cli.purge_packet(packet_path, tmp_path, check_eligibility=True)["purged"] is (
        not eligible
    )
    assert checked == [(case["id"], False)]
    for path in (source, packet_path, derived, *journals):
        assert path.exists() is eligible
    assert read(unrelated) == {"private": "another packet"}
    assert read(index)["artifacts"][-1] == {"cases": ["another-case"], "files": [unrelated.name]}
