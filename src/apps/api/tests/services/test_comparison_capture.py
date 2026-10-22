"""Capture privacy and retention boundaries, specified before capture implementation."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services.creator_comparison_capture import private_root, sanitize_input, save_capture


def test_private_store_rejects_repo_and_open_permissions(tmp_path):
    with pytest.raises(ValueError):
        private_root(".")
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        private_root(tmp_path)


def test_signed_capabilities_are_removed_and_capture_marked_incomplete(tmp_path):
    tmp_path.chmod(0o700)
    safe, complete = sanitize_input(
        {"url": "https://host/file?X-Goog-Signature=private", "utterance": "keep these exact words"}
    )
    assert not complete and "private" not in json.dumps(safe)
    assert safe["utterance"] == "keep these exact words"
    record = {
        "id": "capture-test",
        "input": safe,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    path = save_capture(private_root(tmp_path), record)
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        save_capture(private_root(tmp_path), record)


def test_expired_capture_cannot_be_saved(tmp_path):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="expired"):
        save_capture(
            private_root(tmp_path), {"id": "old", "expires_at": datetime.now(UTC).isoformat()}
        )


def test_actual_invocation_capture_rechecks_grant_and_deduplicates(tmp_path, monkeypatch):
    """Real PostgreSQL authority plus private file publication; no provider calls."""
    import uuid
    from pathlib import Path

    from sqlalchemy import delete

    from app.agents._runtime import RunContext
    from app.agents.main_creator import MainCreatorAgent
    from app.config import settings
    from app.database import sync_session
    from app.models import CreationThread, InternalAccountGrant, RequestFollowingCase, User
    from app.services.creator_comparison_capture import capture_invocation

    owner, thread, grant, case_id = [uuid.uuid4() for _ in range(4)]
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, "creator_comparison_capture_enabled", True)
    monkeypatch.setattr(settings, "creator_comparison_capture_user_ids", [str(owner)])
    monkeypatch.setattr(settings, "creator_comparison_capture_dir", str(tmp_path))
    monkeypatch.setattr(settings, "request_following_all_users", False)
    monkeypatch.setattr(settings, "request_following_user_ids", [str(owner)])
    with sync_session() as db:
        db.add(User(id=owner, email=f"{owner}@comparison.test"))
        db.flush()
        db.add(CreationThread(id=thread, creator_id=owner))
        db.flush()
        db.add(
            InternalAccountGrant(
                id=grant, creator_id=owner, granted_by="test", idempotency_key=str(grant)
            )
        )
        db.add(
            RequestFollowingCase(
                id=case_id,
                creator_id=owner,
                thread_id=thread,
                source_sha256="a" * 64,
                eligibility_key=f"internal_grant:{grant}",
                split="development",
                capture={},
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.commit()
    try:
        fixture = json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures/agent_evals/main_creator/montage_strategy.json"
            ).read_text()
        )
        agent = MainCreatorAgent(None)
        data = agent.Input.model_validate(fixture["input"])
        ctx = RunContext(request_id=str(thread), creator_id=str(owner))
        capture_invocation(agent, data, ctx)
        capture_invocation(agent, data, ctx)
        paths = list(tmp_path.glob("*.json"))
        assert len(paths) == 1
        captured = json.loads(paths[0].read_text())
        assert captured["complete"] and captured["eligibility_key"] == f"internal_grant:{grant}"
        assert captured["prompt_sha256"] and captured["source_kind"] == "exact_invocation"
        with sync_session() as db:
            db.get(InternalAccountGrant, grant).status = "revoked"
            db.commit()
        data.user_message = "A later request after revocation"
        capture_invocation(agent, data, ctx)
        assert len(list(tmp_path.glob("*.json"))) == 1
    finally:
        with sync_session() as db:
            db.execute(delete(User).where(User.id == owner))
            db.commit()
