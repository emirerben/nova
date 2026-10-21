"""Real private-file CLI journey; failure catalogue precedes implementation."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
CLI = ROOT / "scripts/editorial-taste.py"


def run(store, *args, ok=True):
    result = subprocess.run(
        [sys.executable, str(CLI), "--store", str(store), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "GEMINI_API_KEY": "", "OPENAI_API_KEY": ""},
    )
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return json.loads(result.stdout) if result.returncode == 0 else result.stderr


def capture(tmp_path, *, foreign=False):
    owner, project = str(uuid.uuid4()), str(uuid.uuid4())
    value = {
        "owner_id": owner,
        "captured_at": "2026-10-21T06:00:00+00:00",
        "examples": [
            {
                "project_id": project,
                "owner_id": str(uuid.uuid4()) if foreign else owner,
                "title": "Race-day disappointment",
                "kind": "video",
                "words": ["I ran 38 km for an empty snack stand"],
                "creator_requests": ["Show what actually happened at the race."],
                "source_descriptions": ["A runner approaches a closed stand."],
                "artifact": {"availability": "metadata_only", "identity": "test-export-v1"},
            }
        ],
        "exposed_project_ids": [project],
    }
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(value))
    return owner, project, path


def seed(tmp_path):
    store = tmp_path / "private"
    owner, project, path = capture(tmp_path)
    result = run(
        store,
        "import",
        "--input",
        path,
        "--owner",
        owner,
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "This is a good post.",
    )
    return store, owner, project, path, result


def test_positive_examples_profile_review_and_context(tmp_path):
    store, owner, project, path, first = seed(tmp_path)
    assert first["revision"] == 1
    assert first["positive_examples"] == 1
    assert store.stat().st_mode & 0o077 == 0
    again = run(
        store,
        "import",
        "--input",
        path,
        "--owner",
        owner,
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "This is a good post.",
    )
    assert again["revision"] == 1  # A retry adds no vote/revision.
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(["Prefer specific observations; do not force a joke."]))
    proposed = run(store, "propose-profile", "--rules", rules, "--expected-revision", 1)
    before = run(store, "context", "--owner", owner, "--query", "race day")
    assert "Prefer specific observations" not in before["context"]
    assert "I ran 38 km" in before["context"]
    assert "not evidence about the current footage" in before["context"]
    run(
        store,
        "approve-profile",
        "--expected-revision",
        1,
        "--profile-sha256",
        proposed["profile_sha256"],
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "That captures my taste.",
        ok=False,
    )
    approved = run(
        store,
        "approve-profile",
        "--expected-revision",
        2,
        "--profile-sha256",
        proposed["profile_sha256"],
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "That captures my taste.",
    )
    assert approved["revision"] == 3
    context = run(store, "context", "--owner", owner, "--query", "race day")
    assert "Prefer specific observations" in context["context"]
    assert context["example_ids"] == [project]
    assert len(context["context"]) <= 8000
    report = run(store, "report")
    assert report["live_quality_evaluation"] == "not_run"
    assert report["fine_tuning_ready"] is False
    run(store, "context", "--owner", uuid.uuid4(), ok=False)
    run(store, "holdout", "--project", project, ok=False)


def test_wrong_owner_changed_material_and_tampering_fail(tmp_path):
    store, owner, project, path, _ = seed(tmp_path)
    body = json.loads(path.read_text())
    body["examples"][0]["words"] = ["Changed export"]
    path.write_text(json.dumps(body))
    run(
        store,
        "import",
        "--input",
        path,
        "--owner",
        owner,
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "Good",
        ok=False,
    )
    other = tmp_path / "other"
    foreign_owner, _, foreign_path = capture(tmp_path, foreign=True)
    run(
        other,
        "import",
        "--input",
        foreign_path,
        "--owner",
        foreign_owner,
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "Good",
        ok=False,
    )
    head = json.loads((store / "HEAD.json").read_text())
    revision = store / "revisions" / head["file"]
    data = json.loads(revision.read_text())
    data["examples"][0]["words"] = ["Injected material"]
    revision.write_text(json.dumps(data))
    run(store, "context", "--owner", owner, ok=False)


def test_holdout_bodies_and_git_store_are_rejected(tmp_path):
    store, owner, _, path, _ = seed(tmp_path)
    held = str(uuid.uuid4())
    run(store, "holdout", "--project", held)
    body = json.loads(path.read_text())
    body["examples"][0]["project_id"] = held
    body["exposed_project_ids"] = [held]
    path.write_text(json.dumps(body))
    run(
        store,
        "import",
        "--input",
        path,
        "--owner",
        owner,
        "--reviewer",
        "Synthetic creator",
        "--approval",
        "Good",
        ok=False,
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere")
    run(checkout / "private", "report", ok=False)


def test_calibration_keeps_ties_reasons_and_source_identity(tmp_path):
    store, owner, project, _, _ = seed(tmp_path)
    calibration = {
        "id": str(uuid.uuid4()),
        "owner_id": owner,
        "source_project_id": project,
        "provenance": "assistant_authored_calibration",
        "options": [
            {"id": "A", "idea": "An honest race review", "needs_creator_input": True},
            {"id": "B", "idea": "Race-day atmosphere", "needs_creator_input": False},
        ],
    }
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration))
    created = run(store, "calibration", "--input", path, "--expected-revision", 1)
    assert created["revision"] == 2
    judgment = {
        "selected": ["A", "B"],
        "reason": "Both are good for different occasions.",
        "reviewer": "Synthetic creator",
        "creator_statement": "Both are good for different occasions.",
    }
    choice = tmp_path / "choice.json"
    choice.write_text(json.dumps(judgment))
    run(
        store,
        "judge",
        "--id",
        calibration["id"],
        "--input",
        choice,
        "--expected-revision",
        1,
        ok=False,
    )
    rated = run(
        store, "judge", "--id", calibration["id"], "--input", choice, "--expected-revision", 2
    )
    assert rated["reviewed_calibrations"] == 1
    assert rated["live_quality_evaluation"] == "not_run"
    context = run(store, "context", "--owner", owner, "--query", "race")
    assert "Both are good for different occasions" in context["context"]
    # A tie stays a tie, never a fabricated winner for preference training.
    assert json.loads(context["context"].split("\n", 1)[1])["calibrations"][0]["selected"] == [
        "A",
        "B",
    ]
    calibration["source_project_id"] = str(uuid.uuid4())
    path.write_text(json.dumps(calibration))
    run(store, "calibration", "--input", path, "--expected-revision", 3, ok=False)


def test_one_rejected_proposal_enters_context_without_an_invented_alternative(tmp_path):
    # Failure catalogue: a single real response must be reviewable without making
    # up a competing idea, turning rejection into a winner or changing rules.
    store, owner, project, _, _ = seed(tmp_path)
    case_id = str(uuid.uuid4())
    proposal = {
        "id": case_id,
        "owner_id": owner,
        "source_project_id": project,
        "provenance": "synthetic_recorded_provider_response",
        "options": [
            {
                "id": "one",
                "idea": "Discover amazing sights. Plan your visit!",
                "needs_creator_input": False,
            }
        ],
    }
    path = tmp_path / "one-proposal.json"
    path.write_text(json.dumps(proposal))
    created = run(store, "calibration", "--input", path, "--expected-revision", 1)
    assert created["revision"] == 2
    before = json.loads(run(store, "context", "--owner", owner)["context"].split("\n", 1)[1])
    assert before["calibrations"] == []
    reason = "Not for me: it is generic advertising copy."
    response = tmp_path / "rejection.json"
    response.write_text(
        json.dumps(
            {
                "selected": [],
                "reason": reason,
                "reviewer": "Synthetic creator",
                "creator_statement": reason,
            }
        )
    )
    judged = run(store, "judge", "--id", case_id, "--input", response, "--expected-revision", 2)
    assert judged["reviewed_calibrations"] == 1
    payload = json.loads(run(store, "context", "--owner", owner)["context"].split("\n", 1)[1])
    assert payload["confirmed_rules"] == []
    assert len(payload["calibrations"]) == 1
    assert payload["calibrations"][0]["options"] == proposal["options"]
    assert payload["calibrations"][0]["selected"] == []
    assert payload["calibrations"][0]["reason"] == reason
    # The existing source and revision fences still apply to the single case.
    run(store, "judge", "--id", case_id, "--input", response, "--expected-revision", 3, ok=False)
    proposal["id"] = str(uuid.uuid4())
    proposal["options"] = []
    path.write_text(json.dumps(proposal))
    run(store, "calibration", "--input", path, "--expected-revision", 3, ok=False)
