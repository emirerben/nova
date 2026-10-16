"""Real local HTTP review of imported private evidence; no production writes."""

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.services.request_following_cases import digest

ROOT = Path(__file__).resolve().parents[5]


def test_imported_private_capture_review_and_export(tmp_path):
    store = tmp_path / "private"
    store.mkdir(mode=0o700)
    now = datetime.now(UTC)
    owner, thread = str(uuid.uuid4()), str(uuid.uuid4())
    source = {
        "source": "synthetic_acceptance",
        "thread_id": thread,
        "captured_at": now.isoformat(),
        "sections": {
            "events": [{"kind": "user_message", "actor": "user", "content": "Keep subtitles."}],
            "turns": [],
            "integrity": {"thread": {"creator_id": owner}, "edges": {}},
        },
        "missing": {},
    }
    source_hash = digest({k: v for k, v in source.items() if k != "captured_at"})
    receipt = {
        "source_sha256": source_hash,
        "thread_id": thread,
        "creator_id": owner,
        "eligibility_key": "internal_grant:" + str(uuid.uuid4()),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
    }
    source_path, receipt_path = tmp_path / "source.json", tmp_path / "receipt.json"
    source_path.write_text(json.dumps(source))
    receipt_path.write_text(json.dumps(receipt))
    command = [
        sys.executable,
        str(ROOT / "scripts/request-following-loop.py"),
        "--store",
        str(store),
    ]
    imported = subprocess.run(
        command
        + [
            "import",
            "--input",
            str(source_path),
            "--split",
            "development",
            "--eligibility-file",
            str(receipt_path),
        ],
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    session = json.loads(imported.stdout)["session"]
    proposed = subprocess.run(
        command + ["propose", "--session", session, "--id", "private-review"],
        capture_output=True,
        text=True,
    )
    assert proposed.returncode == 0, proposed.stderr
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/request-following-review.py"),
            "--store",
            str(store),
            "--port",
            "0",
            "--no-open",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        line = process.stdout.readline().strip()
        assert line.startswith("Review app: "), (
            process.stderr.read() if process.poll() is not None else line
        )
        base = line.removeprefix("Review app: ")
        with httpx.Client(base_url=base, timeout=10) as client:
            token = client.get("/config").json()["session"]
            headers = {"X-Review-Session": token, "Origin": base}
            assert client.get("/api/cases").status_code == 403
            queue = client.get("/api/cases", headers=headers).json()
            assert queue["collector"]["mode"] == "private_store"
            assert len(queue["items"]) == 1
            path = "/api/cases/" + queue["items"][0]["id"]
            detail = client.get(path, headers=headers).json()
            assert detail["capture"]["sections"]["events"][0]["content"] == "Keep subtitles."
            assert detail["eligibility_checked_at"] == receipt["checked_at"]
            assert client.get(path + "/export", headers=headers).status_code == 409
            body = {
                "source_sha256": source_hash,
                "review_version": 0,
                "decision": "approved",
                "reviewer": "Synthetic reviewer",
                "rationale": "Explicit creator request",
                "loss_stage": "unknown",
                "tickets": [],
                "expectations": [
                    {"text": "Keep subtitles.", "source_pointers": ["/sections/events/0/content"]}
                ],
            }
            assert (
                client.post(
                    path + "/review", headers={**headers, "Origin": "http://other.test"}, json=body
                ).status_code
                == 403
            )
            saved = client.post(path + "/review", headers=headers, json=body)
            assert saved.status_code == 200, saved.text
            assert client.post(path + "/review", headers=headers, json=body).status_code == 409
            packet = client.get(path + "/export", headers=headers).json()
            assert packet["candidate"]["review"] is None
            assert packet["eligibility"]["source_sha256"] == source_hash
            candidate = json.loads((store / "candidates/private-review.json").read_text())
            assert candidate["intent_review"]["decision"] == "approved"
            assert candidate["review"] is None
            # Neither a changed receipt nor a held-out split can expose the body.
            receipt_file = store / "eligibility" / f"{source_hash}.json"
            bad = {**receipt, "creator_id": str(uuid.uuid4())}
            receipt_file.write_text(json.dumps(bad))
            assert client.get(path, headers=headers).status_code == 404
            expired = {**receipt, "expires_at": (now - timedelta(seconds=1)).isoformat()}
            receipt_file.write_text(json.dumps(expired))
            assert client.get(path, headers=headers).status_code == 404
            receipt_file.unlink()
            assert client.get(path, headers=headers).status_code == 404
            receipt_file.write_text(json.dumps(receipt))
            index = json.loads((store / "index.json").read_text())
            imported_source = Path(index[session]["path"])
            original_source = imported_source.read_text()
            changed_source = json.loads(original_source)
            changed_source["sections"]["events"][0]["content"] = "Changed outside import"
            imported_source.write_text(json.dumps(changed_source))
            assert client.get(path, headers=headers).status_code == 404
            imported_source.write_text(original_source)
            index[session]["split"] = "holdout"
            (store / "index.json").write_text(json.dumps(index))
            assert client.get("/api/cases", headers=headers).json()["items"] == []
    finally:
        process.terminate()
        process.wait(timeout=10)
