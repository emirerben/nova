"""Seed and safely reset one local Kria runtime-v2 thread.

This utility refuses non-local database URLs. Reset is a status repair, not a
delete: creator events and receipts remain available for debugging.
"""

from __future__ import annotations

import argparse
import json
import uuid

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import sync_session
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    Persona,
    PlanItem,
    User,
)

_LOCAL_HOSTS = {None, "", "127.0.0.1", "localhost", "postgres", "db"}


def require_local_database(url: str) -> None:
    parsed = make_url(url)
    database = parsed.database or ""
    if parsed.host not in _LOCAL_HOSTS or database in {"nova", "nova_prod", "production"}:
        raise SystemExit(f"Refusing Kria dev mutation against {parsed.host}/{database}")


_GUIDED_PLACES = ["Alpha Quay", "Beta Bridge", "Gamma Tower", "Delta Palace"]
_GUIDED_CLIPS = 6


def _guided_fixture(user_id: uuid.UUID) -> tuple[dict, dict, list[str], list[dict]]:
    """Synthetic East-Run-shaped guided variant with `clip-label-*` bars.

    Same construction as tests/services/test_kria_editor_clip_context.py: the
    unified montage planner + guided compile, over invented clip facts. Paths
    are fake (`users/dev/...`): the editor snapshot, copilot and compiler work;
    playback does not.
    """
    from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: PLC0415
    from app.pipeline.guided_story import compile_execution_plan  # noqa: PLC0415
    from app.pipeline.unified_montage import (  # noqa: PLC0415
        UnifiedClip,
        brief_view,
        plan_unified_montage,
    )

    paths = [f"users/dev/{user_id}/proxy-{i}.mp4" for i in range(_GUIDED_CLIPS)]
    clips, assignments = [], []
    for i, path in enumerate(paths):
        facts = []
        if i % 3 != 2:
            facts.append(
                {
                    "kind": "landmark",
                    "value": _GUIDED_PLACES[i % 4],
                    "provenance": "inferred",
                }
            )
        clips.append(
            UnifiedClip(
                media_id=f"clip-{i}",
                proxy_path=path,
                generation="7",
                duration_s=4.0,
                width=1080,
                height=1920,
                facts=tuple(facts),
            )
        )
        row: dict = {"gcs_path": path}
        if facts:
            row["analysis"] = {"clip_facts": [{**facts[0], "confidence": 0.8}]}
        assignments.append(row)
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="the place")
        ],
    )
    guided = plan_unified_montage(clips, brief_view(brief)).guided_edit()
    execution_plan = compile_execution_plan(guided, track=None)
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "render_destination": "device",
        "text_elements": execution_plan["text_elements"],
    }
    plan = {
        "guided_edit": guided,
        "guided_story_execution_plan": execution_plan,
        "variants": [variant],
    }
    return plan, variant, paths, assignments


def seed(email: str, *, guided: bool = False) -> dict[str, str]:
    require_local_database(settings.database_url)
    with sync_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(email=email, name="Kria Dev", auth_provider="dev")
            db.add(user)
            db.flush()
        persona = db.execute(select(Persona).where(Persona.user_id == user.id)).scalar_one_or_none()
        if persona is None:
            persona = Persona(
                user_id=user.id,
                persona_status="ready",
                persona={"content_mode": "lifestyle", "tone": "direct"},
            )
            db.add(persona)
            db.flush()
        plan = ContentPlan(
            user_id=user.id,
            persona_id=persona.id,
            plan_status="ready",
        )
        db.add(plan)
        db.flush()
        item = PlanItem(
            content_plan_id=plan.id,
            position=1,
            idea="Kria local matcha update",
            item_status="awaiting_clips",
            edit_format="montage",
        )
        db.add(item)
        db.flush()
        session = CreatorAgentSession(
            creator_id=user.id,
            plan_item_id=item.id,
            status="awaiting_feedback",
        )
        db.add(session)
        db.flush()
        job_id: str | None = None
        if guided:
            plan_json, variant, paths, assignments = _guided_fixture(user.id)
            job = Job(
                user_id=user.id,
                status="variants_ready",
                job_type="default",
                mode="generative",
                raw_storage_path=paths[0],
                assembly_plan=plan_json,
                all_candidates={"clip_paths": paths},
            )
            db.add(job)
            db.flush()
            item.edit_format = "montage"
            item.item_status = "ready"
            item.current_job_id = job.id
            item.clip_gcs_paths = paths
            item.clip_assignments = assignments
            session.target_job_id = job.id
            session.target_variant_id = variant["variant_id"]
            session.target_generation_id = variant["render_generation_id"]
            job_id = str(job.id)
        thread = CreationThread(
            creator_id=user.id,
            runtime_version=2,
            content_plan_id=plan.id,
            active_plan_item_id=item.id,
            active_creator_agent_session_id=session.id,
            title="Kria local guided story" if guided else "Kria local matcha update",
            revision=1,
            state={"edit_format": "montage", "media": [], "media_count": 0},
        )
        db.add(thread)
        db.flush()
        db.add(
            CreationThreadEvent(
                thread_id=thread.id,
                sequence=0,
                revision=1,
                role="system",
                event_type="thread_created",
                content=None,
                payload={"runtime_version": 2, "seeded": True},
            )
        )
        db.commit()
        out = {
            "creator_id": str(user.id),
            "item_id": str(item.id),
            "thread_id": str(thread.id),
        }
        if job_id is not None:
            out["job_id"] = job_id
        return out


def reset(thread_id: uuid.UUID, *, apply: bool) -> dict[str, object]:
    require_local_database(settings.database_url)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        if thread is None or int(thread.runtime_version) != 2:
            raise SystemExit("Runtime-v2 thread not found")
        turns = list(
            db.execute(select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread.id))
            .scalars()
            .all()
        )
        approvals = list(
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread.id)
            )
            .scalars()
            .all()
        )
        executions = list(
            db.execute(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.target_thread_id == thread.id
                )
            )
            .scalars()
            .all()
        )
        summary = {
            "thread_id": str(thread.id),
            "dry_run": not apply,
            "turns_to_cancel": sum(
                row.status not in {"completed", "failed", "cancelled", "superseded"}
                for row in turns
            ),
            "approvals_to_cancel": sum(row.status in {"pending", "approved"} for row in approvals),
            "executions_to_cancel": sum(
                row.status in {"pending", "running", "awaiting_approval"} for row in executions
            ),
        }
        if not apply:
            return summary
        for row in turns:
            if row.status not in {"completed", "failed", "cancelled", "superseded"}:
                row.status = "cancelled"
                row.lease_owner = None
                row.lease_expires_at = None
        for row in approvals:
            if row.status in {"pending", "approved"}:
                row.status = "cancelled"
        for row in executions:
            if row.status in {"pending", "running", "awaiting_approval"}:
                row.status = "cancelled"
        if thread.active_creator_agent_session_id is not None:
            session = db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
            if session is not None:
                session.status = "awaiting_feedback"
        db.commit()
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--email", default="kria-dev@local.test")
    seed_parser.add_argument(
        "--guided",
        action="store_true",
        help="also seed a rendered guided-native variant with clip-label-* bars",
    )
    reset_parser = commands.add_parser("reset")
    reset_parser.add_argument("--thread-id", required=True, type=uuid.UUID)
    reset_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "seed":
        result = seed(args.email, guided=args.guided)
    else:
        result = reset(args.thread_id, apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
