"""Default-off exact invocation capture using the existing training authority.

No media read, provider call, new consent grant or fixture promotion. Missing
ownership, deleted threads and missing queue eligibility fail closed. Capture
failures never affect the creator turn. Enable only after operator deployment.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from app.services.request_following_cases import digest


def private_root(path):
    root = Path(path).resolve()
    repo = Path(__file__).resolve().parents[5]
    if root == repo or repo in root.parents or (root / ".git").exists():
        raise ValueError("private store must be outside Git")
    # Also forbid other worktrees and nested repositories.
    if any((parent / ".git").exists() for parent in root.parents):
        raise ValueError("private store must be outside Git")
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError("private store must exist with mode 700")
    return root


def sanitize_input(value):
    complete = True

    def scrub(v):
        nonlocal complete
        if isinstance(v, dict):
            out = {}
            for k, item in v.items():
                if any(
                    s in str(k).lower()
                    for s in ("api_key", "authorization", "access_token", "password")
                ):
                    out[k] = "[credential removed]"
                    complete = False
                else:
                    out[k] = scrub(item)
            return out
        if isinstance(v, list):
            return [scrub(i) for i in v]
        if isinstance(v, str):
            # Prompts should use opaque ids. Preserve ordinary creator text;
            # capability URLs and bearer secrets must never become fixtures.
            def url(match):
                nonlocal complete
                raw = match.group()
                if urlsplit(raw).query or "storage.googleapis.com" in raw:
                    complete = False
                    return "[capability URL removed]"
                return raw

            return re.sub(r'https?://[^\s"<>]+', url, v)
        return v

    result = scrub(value)
    return result, complete


def save_capture(root, record):
    expires = datetime.fromisoformat(record["expires_at"])
    if expires <= datetime.now(UTC):
        raise ValueError("capture expired")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", record["id"]):
        raise ValueError("invalid capture identity")
    content = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    if len(content) > 2_000_000:
        raise ValueError("capture exceeds bound")
    path = root / (record["id"] + ".json")
    # Exclusive publication prevents a retry overwriting point-in-time context.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    return path


def capture_invocation(agent, input_model, ctx):
    from app.config import settings

    if not settings.creator_comparison_capture_enabled or ctx.extra.get("skip_comparison_capture"):
        return
    if agent.spec.name not in ("nova.creator.main", "nova.edit.copilot"):
        return
    try:
        from sqlalchemy import select

        from app.database import sync_session
        from app.models import CreationThread, CreationThreadDeletion, Job, RequestFollowingCase
        from app.services.request_following_collection import eligibility_key, in_scope
        from app.services.training_eligibility import evaluate_training_eligibility

        with sync_session() as db:
            creator = uuid.UUID(ctx.creator_id) if ctx.creator_id else None
            if ctx.job_id:
                job = db.get(Job, uuid.UUID(ctx.job_id))
                if job is None or (creator and creator != job.user_id):
                    return
                creator = job.user_id
            if creator is None or str(creator) not in settings.creator_comparison_capture_user_ids:
                return
            query = select(CreationThread).where(CreationThread.creator_id == creator)
            if ctx.job_id:
                query = query.where(CreationThread.active_job_id == uuid.UUID(ctx.job_id))
            elif ctx.request_id:
                query = query.where(CreationThread.id == uuid.UUID(ctx.request_id))
            else:
                return
            thread = db.scalar(query)
            if (
                thread is None
                or not in_scope(creator, thread.id)
                or db.get(CreationThreadDeletion, thread.id)
            ):
                return
            decision = evaluate_training_eligibility(db, creator)
            key = eligibility_key(decision)
            if key is None:
                return
            from app.services.request_following_collection import scope

            held_out = thread.id in scope()[2] or db.scalar(
                select(RequestFollowingCase.id)
                .where(
                    RequestFollowingCase.thread_id == thread.id,
                    RequestFollowingCase.split == "holdout",
                )
                .limit(1)
            )
            directory = (
                settings.creator_comparison_holdout_capture_dir
                if held_out
                else settings.creator_comparison_capture_dir
            )
            if not directory:
                return
            root = private_root(directory)
            safe, complete = sanitize_input(input_model.model_dump(mode="json"))
            now = datetime.now(UTC)
            agent_name = (
                "main_creator" if agent.spec.name == "nova.creator.main" else "edit_copilot"
            )
            identity = digest([agent_name, str(thread.id), ctx.request_id, safe])
            prompt = agent.render_prompt(agent.Input.model_validate(safe)) if complete else None
            record = {
                "id": identity,
                "agent": agent_name,
                "source_kind": "exact_invocation",
                "split": "holdout" if held_out else "development",
                "input": safe,
                "input_sha256": digest(safe),
                "complete": complete,
                "rendered_prompt": prompt,
                "prompt_sha256": digest(prompt) if prompt else None,
                "prompt_version": agent.spec.prompt_version,
                "spec": asdict(agent.spec),
                "captured_at": now.isoformat(),
                "expires_at": (now + timedelta(days=30)).isoformat(),
                "eligibility_key": key,
                "creator_id": str(creator),
                "thread_id": str(thread.id),
                "source_sha256": digest([str(thread.id), ctx.request_id, safe]),
            }
            save_capture(root, record)
    except Exception as exc:
        # Neither text nor exception messages (which can contain SQL parameters).
        import structlog

        structlog.get_logger().warning(
            "creator_comparison_capture_skipped", error_class=type(exc).__name__
        )
