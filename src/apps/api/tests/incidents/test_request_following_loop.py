"""CLI acceptance journey; failure cases preregistered in kri-215/verification-plan.md."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts/ci").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/request-following-loop.py", repo / "scripts")
    shutil.copy(ROOT / "scripts/ci/journey-manifest.json", repo / "scripts/ci")
    api = repo / "src/apps/api"
    (api / "tests/fixtures").mkdir(parents=True)
    (api / "app").symlink_to(ROOT / "src/apps/api/app", target_is_directory=True)
    (api / "tests/__init__.py").touch()
    for name in ("incidents", "gates"):
        shutil.copytree(ROOT / f"src/apps/api/tests/{name}", api / f"tests/{name}")
    shutil.copytree(
        ROOT / "src/apps/api/tests/fixtures/incidents", api / "tests/fixtures/incidents"
    )
    store = tmp_path / "private"
    store.mkdir(mode=0o700)
    source = store / "input.json"
    source.write_text(
        json.dumps(
            {
                "thread_id": "11111111-1111-1111-1111-111111111111",
                "source": "synthetic_acceptance",
                "captured_at": "2026-10-16",
                "sections": {
                    "events": [{"kind": "user_message", "content": "Make a 20 second montage."}],
                    "turns": [],
                },
                "missing": {"final_output": "not_observed"},
            }
        )
    )
    return repo, store, source


def run(workspace, *args, ok=True):
    repo, store, _ = workspace
    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/request-following-loop.py"),
            "--store",
            str(store),
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GEMINI_API_KEY": "test",
            "STORAGE_BUCKET": "test",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
        },
    )
    assert (result.returncode == 0) == ok, result.stderr + result.stdout
    return result


def prepare(workspace):
    repo, store, source = workspace
    imported = json.loads(
        run(workspace, "import", "--input", str(source), "--split", "development").stdout
    )
    assert imported["changed"]
    repeated = json.loads(
        run(workspace, "import", "--input", str(source), "--split", "development").stdout
    )
    assert not repeated["changed"]
    run(
        workspace,
        "propose",
        "--session",
        imported["session"],
        "--id",
        "learning-loop-demo",
        "--ticket",
        "KRI-215",
    )
    path = store / "candidates/learning-loop-demo.json"
    case = json.loads(path.read_text())
    case["ambiguities"] = []
    case["expectations"] = [
        {"text": "20 seconds", "source_pointers": ["/sections/events/0/content"]}
    ]
    case["incident"] = json.loads(
        (
            repo / "src/apps/api/tests/fixtures/incidents/clarify-clear-request-asks-nothing.json"
        ).read_text()
    )
    case["incident"]["id"] = case["id"]
    case["incident"]["repro"]["command"] = "pytest tests/incidents/test_incident_corpus.py"
    path.write_text(json.dumps(case))
    return path, case


def approve(workspace, path):
    return run(
        workspace,
        "review",
        "--candidate",
        str(path),
        "--decision",
        "approved",
        "--reviewer",
        "Synthetic acceptance reviewer",
        "--rationale",
        "Test-only fixture",
        "--redaction-checked",
    )


def test_capture_import_review_promote_real_corpus(workspace):
    repo, store, _ = workspace
    path, _ = prepare(workspace)
    run(workspace, "promote", "--candidate", str(path), ok=False)
    approve(workspace, path)
    run(workspace, "promote", "--candidate", str(path))
    run(workspace, "promote", "--candidate", str(path), ok=False)
    output = repo / "src/apps/api/tests/fixtures/incidents/learning-loop-demo.json"
    assert "Synthetic acceptance reviewer" in output.read_text()
    # Real corpus contract check on the promoted JSON, not a mirror of CLI logic.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/incidents/test_incident_corpus.py",
            "-k",
            "learning-loop-demo and approved_plan",
            "-q",
        ],
        cwd=repo / "src/apps/api",
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GEMINI_API_KEY": "test",
            "STORAGE_BUCKET": "test",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    report = json.loads(run(workspace, "report").stdout)
    assert report["cases"][0]["evidence"]["rendered_output"]["status"] == "missing"


def test_stale_review_source_and_holdout_are_blocked(workspace):
    _, _, source = workspace
    path, _ = prepare(workspace)
    approve(workspace, path)
    reviewed = json.loads(path.read_text())
    reviewed["expectations"][0]["text"] = "30 seconds"
    path.write_text(json.dumps(reviewed))
    run(workspace, "promote", "--candidate", str(path), ok=False)
    approve(workspace, path)
    capture = json.loads(source.read_text())
    capture["sections"]["events"].append({"kind": "user_message", "content": "Actually 30 seconds"})
    source.write_text(json.dumps(capture))
    run(workspace, "import", "--input", str(source), "--split", "development")
    run(workspace, "promote", "--candidate", str(path), ok=False)
    run(workspace, "import", "--input", str(source), "--split", "holdout", ok=False)
    capture["thread_id"] = "22222222-2222-2222-2222-222222222222"
    source.write_text(json.dumps(capture))
    holdout = json.loads(
        run(workspace, "import", "--input", str(source), "--split", "holdout").stdout
    )
    run(workspace, "propose", "--session", holdout["session"], "--id", "leak", ok=False)
    assert json.loads(run(workspace, "report").stdout)["holdout_sessions"] == 1


def test_evidence_must_match_stage_and_source_must_be_creator(workspace):
    _, store, _ = workspace
    path, case = prepare(workspace)
    case["expectations"][0]["source_pointers"] = ["/source"]
    path.write_text(json.dumps(case))
    run(
        workspace,
        "review",
        "--candidate",
        str(path),
        "--decision",
        "approved",
        "--reviewer",
        "test",
        "--rationale",
        "test",
        "--redaction-checked",
        ok=False,
    )
    case["expectations"][0]["source_pointers"] = ["/sections/events/0/content"]
    artifact = store / "result.json"
    artifact.write_text("{}")
    case["evidence"]["rendered_output"] = {
        "status": "pass",
        "level": "offline_replay",
        "artifact": str(artifact),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "command": "pytest example",
        "measured_at": "2026-10-16",
    }
    path.write_text(json.dumps(case))
    run(
        workspace,
        "review",
        "--candidate",
        str(path),
        "--decision",
        "approved",
        "--reviewer",
        "test",
        "--rationale",
        "test",
        "--redaction-checked",
        ok=False,
    )


def test_capture_paginates_and_marks_partial_failure(workspace):
    repo, store, _ = workspace
    # Fake read-only admin boundary, exercising real subprocess capture and pagination.
    (repo / "scripts/admin.py").write_text("""import json, sys
route = sys.argv[-1]
assert sys.argv[-2] == "GET"
thread = "11111111-1111-1111-1111-111111111111"
if "/events?" in route:
    second = "cursor=1" in route
    print(json.dumps({"thread_id": thread, "events": [{"sequence": 2 if second else 1}],
                      "next_cursor": None if second else "1"}))
elif "/turns?" in route:
    sys.exit(1)
else:
    print("{}")
""")
    result = run(workspace, "capture", "--thread", "11111111-1111-1111-1111-111111111111", ok=False)
    assert result.returncode == 2
    capture = json.loads(Path(json.loads(result.stdout)["capture"]).read_text())
    assert len(capture["sections"]["events"]) == 2
    assert capture["missing"]["turns"] == "capture_failed_or_incomplete"
    assert not (Path(json.loads(result.stdout)["capture"]).stat().st_mode & 0o077)


def test_capture_drops_foreign_job_metadata(workspace):
    repo, _, _ = workspace
    (repo / "scripts/admin.py").write_text("""import json, sys
route = sys.argv[-1]
thread = "11111111-1111-1111-1111-111111111111"
job = "22222222-2222-2222-2222-222222222222"
owner = "33333333-3333-3333-3333-333333333333"
if "/events?" in route:
    data = {"thread_id": thread, "events": [], "next_cursor": None}
elif "/turns?" in route:
    data = {"thread_id": thread, "turns": [{"job_ids": [job]}], "next_cursor": None}
elif "/integrity" in route:
    data = {"thread": {"creator_id": owner}, "edges": {"job": {"coherent": False}},
            "video": {"job_id": job, "private": "FOREIGN-MARKER"}}
elif route.startswith("jobs/"):
    data = {"job": {"user_id": "44444444-4444-4444-4444-444444444444"},
            "private": "FOREIGN-MARKER"}
else:
    data = {}
print(json.dumps(data))
""")
    result = run(workspace, "capture", "--thread", "11111111-1111-1111-1111-111111111111", ok=False)
    assert result.returncode == 2
    path = Path(json.loads(result.stdout)["capture"])
    assert "FOREIGN-MARKER" not in path.read_text()
    assert "job-22222222-2222-2222-2222-222222222222" in json.loads(path.read_text())["missing"]
