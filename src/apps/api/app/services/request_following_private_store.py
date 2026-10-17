"""Local view of authorized downloaded copies, using the existing candidate store.

No cloud credentials, new eligibility grants, provider calls, or fixture promotion.
Receipts describe eligibility when checked; this mode cannot claim live consent.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.services.request_following_cases import (
    build_candidate,
    diagnostics,
    digest,
    validate_eligibility_receipt,
    validate_intent,
)


class StoreError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


def read(path):
    if path.stat().st_size > 20_000_000:
        raise ValueError("private_file_too_large")
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PrivateReviewStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_dir() or self.path.stat().st_mode & 0o077:
            raise ValueError("Private review store must be an existing directory with mode 700")

    def contained(self, path):
        path = Path(path).resolve()
        if self.path not in path.parents:
            raise ValueError("Source must stay inside the private store")
        return path

    def records(self):
        index = read(self.path / "index.json") if (self.path / "index.json").exists() else {}
        records = []
        for path in sorted((self.path / "candidates").glob("*.json")):
            try:
                path = self.contained(path)
                candidate = read(path)
                session = index[candidate["session"]]
                if session["split"] != "development" or candidate["split"] != "development":
                    continue
                source_hash = session["source_sha256"]
                if candidate["source_sha256"] != source_hash:
                    continue
                source = read(self.contained(session["path"]))
                if digest({k: v for k, v in source.items() if k != "captured_at"}) != source_hash:
                    continue
                receipt = read(self.contained(self.path / "eligibility" / f"{source_hash}.json"))
                validate_eligibility_receipt(source, source_hash, receipt)
                case_id = str(uuid.uuid5(uuid.NAMESPACE_URL, candidate["id"] + ":" + source_hash))
                records.append((case_id, path, candidate, source, receipt))
            except (ValueError, KeyError, TypeError, OSError):
                continue  # Missing/expired/wrong-owner evidence never enters the UI.
        return sorted(records, key=lambda row: row[0])

    def summary(self, record):
        case_id, _, candidate, source, receipt = record
        review = candidate.get("intent_review") or {}
        return {
            "id": case_id,
            "thread_id": source["thread_id"],
            "source_sha256": candidate["source_sha256"],
            "created_at": receipt.get("captured_at") or source["captured_at"],
            "expires_at": receipt["expires_at"],
            "eligibility_checked_at": receipt["checked_at"],
            "review_state": review.get("decision", "unreviewed"),
            "review_version": review.get("version", 0),
            "diagnostics": diagnostics(source),
            "request_preview": next(
                (
                    e.get("content", "")[:160]
                    for e in source.get("sections", {}).get("events", [])
                    if e.get("kind") == "user_message"
                    and e.get("actor") == "user"
                    and e.get("content")
                ),
                "",
            ),
        }

    def handle(self, method, path, query, body=None):
        # Serializes local reviewers. CLI imports are atomic and a changed index
        # invalidates the older candidate on the next access; nothing is promoted.
        with (self.path / ".review.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.records()
            parts = path.removeprefix("/api/cases").strip("/").split("/")
            if parts == [""] and method == "GET":
                limit = int(query.get("limit", [25])[0])
                if not 1 <= limit <= 50:
                    raise StoreError(422, "invalid_page_size")
                state = query.get("review_state", ["all"])[0]
                if state not in {
                    "all",
                    "unreviewed",
                    "approved",
                    "needs_clarification",
                    "rejected",
                }:
                    raise StoreError(422, "invalid_review_state")
                cursor = query.get("cursor", [""])[0]
                filtered = [
                    r
                    for r in records
                    if r[0] > cursor
                    and (state == "all" or self.summary(r)["review_state"] == state)
                ]
                page = filtered[:limit]
                return {
                    "items": [self.summary(r) for r in page],
                    "holdout_count": 0,
                    "next_cursor": page[-1][0] if len(filtered) > limit else None,
                    "collector": {
                        "mode": "private_store",
                        "enabled": False,
                        "all_users": False,
                        "scoped_users": len({r[4]["creator_id"] for r in records}),
                        "scoped_threads": len({r[3]["thread_id"] for r in records}),
                        "last_run_at": None,
                        "last_run": None,
                        "interval_minutes": 15,
                    },
                    "coverage_note": (
                        "Private downloaded copies. Eligibility was checked at capture; "
                        "current server revocation is not refreshed here. Expired, "
                        "unreceipted and held-out copies are withheld."
                    ),
                    "regression_runs_url": "https://github.com/emirerben/nova/actions/workflows/request-following-regressions.yml",
                }
            record = next((r for r in records if r[0] == parts[0]), None)
            if record is None:
                raise StoreError(404, "case_unavailable")
            _, candidate_path, candidate, source, receipt = record
            if len(parts) == 1 and method == "GET":
                return {
                    **self.summary(record),
                    "capture": source,
                    "review": candidate.get("intent_review"),
                    "source_mode": "private_store",
                    "evidence": {
                        "understanding": "unassessed",
                        "persisted_execution": "unassessed",
                        "rendered_output": "missing",
                    },
                }
            if parts[-1] == "review" and method == "POST":
                if not isinstance(body, dict):
                    raise StoreError(422, "invalid_review")
                current = self.summary(record)
                if (
                    body.get("source_sha256") != candidate["source_sha256"]
                    or body.get("review_version") != current["review_version"]
                ):
                    raise StoreError(409, "review_changed_reload")
                try:
                    intent = validate_intent(source, body)
                except ValueError as error:
                    raise StoreError(422, str(error)) from error
                intent.update(
                    source_sha256=candidate["source_sha256"],
                    review_version=current["review_version"],
                    version=current["review_version"] + 1,
                    reviewed_at=datetime.now(UTC).isoformat(),
                )
                write(self.path / "reviews" / f"intent-before-{digest(candidate)}.json", candidate)
                candidate.update(
                    intent_review=intent,
                    expectations=intent["expectations"],
                    loss_stage=intent["loss_stage"],
                    tickets=intent["tickets"],
                    review=None,
                    ambiguities=[] if intent["decision"] == "approved" else ["Intent needs review"],
                )
                write(candidate_path, candidate)
                return {**self.summary(record), "review": intent}
            if parts[-1] == "export" and method == "GET":
                intent = candidate.get("intent_review") or {}
                if (
                    intent.get("decision") != "approved"
                    or intent.get("source_sha256") != candidate["source_sha256"]
                ):
                    raise StoreError(409, "intent_review_required")
                validate_intent(source, intent)
                exported = build_candidate(
                    source,
                    case_id="queue-" + record[0],
                    session_id=candidate["session"],
                    source_sha256=candidate["source_sha256"],
                    tickets=intent["tickets"],
                )
                exported.update(
                    intent_review=intent,
                    expectations=intent["expectations"],
                    loss_stage=intent["loss_stage"],
                    ambiguities=[],
                )
                return {
                    "schema_version": 1,
                    "capture": source,
                    "candidate": exported,
                    "eligibility": receipt,
                }
            raise StoreError(404, "Not found")
