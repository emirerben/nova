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


# East-Run-shaped: two Arnavutköy places, one repeated on a NON-adjacent clip so
# it keeps its own label (consecutive repeats dedupe) => 3 labels contain
# "Arnavutköy". Real place names, invented footage (no prod data).
_GUIDED_PLACES = [
    "Arnavutköy Sahili",
    "Arnavutköy Çarşı",
    "Arnavutköy Sahili",
    "Galata Bridge",
    "Dolmabahçe Palace",
    "Galata Tower",
]
_GUIDED_CLIPS = len(_GUIDED_PLACES)
# Filming times (UTC hour:minute) for the seeded clips: deliberately NOT in clip order,
# with one clip (index 2) carrying none, so "order by filming time" / "add the hour"
# asks exercise reordering, an untimed clip and the Istanbul zone (place = Türkiye).
_GUIDED_CAPTURE_UTC = ["11:20", "09:05", None, "14:45", "10:30", "13:10"]
# Matched song is reference-only on every current (compiler v6+) guided plan:
# it is added when posting, so music level/swap/remove are NOT editable and the
# clips' own audio is what plays. Mirrors prod; see docs in the battery file.
_GUIDED_TRACK = {
    "track_id": "dev-track-1",
    "title": "Dev Song",
    "artist": "Kria Dev",
    "catalog_duration_s": 180.0,
    "start_s": 0.0,
}


def _guided_fixture(user_id: uuid.UUID) -> tuple[dict, dict, list[str], list[dict]]:
    """Synthetic East-Run-shaped guided variant with `clip-label-*` bars.

    Same construction as tests/services/test_kria_editor_clip_context.py: the
    unified montage planner + guided compile, over invented clip facts. Paths
    are fake (`users/dev/...`): the editor snapshot, copilot and compiler work;
    playback does not.
    """
    from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: PLC0415
    from app.pipeline.guided_story import (  # noqa: PLC0415
        compile_execution_plan,
        song_reference_variant_fields,
    )
    from app.pipeline.unified_montage import (  # noqa: PLC0415
        UnifiedClip,
        brief_view,
        plan_unified_montage,
    )

    paths = [f"users/dev/{user_id}/proxy-{i}.mp4" for i in range(_GUIDED_CLIPS)]
    clips, assignments = [], []
    for i, path in enumerate(paths):
        facts = [{"kind": "landmark", "value": _GUIDED_PLACES[i], "provenance": "inferred"}]
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
        capture: dict = {"place": {"locality": "Istanbul", "country": "T\u00fcrkiye"}}
        moment = _GUIDED_CAPTURE_UTC[i] if i < len(_GUIDED_CAPTURE_UTC) else None
        if moment:
            capture["capture_time"] = f"2026-09-20T{moment}:00Z"
        assignments.append(
            {
                "gcs_path": path,
                "capture": capture,
                "analysis": {"clip_facts": [{**facts[0], "confidence": 0.8}]},
            }
        )
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="the place")
        ],
    )
    guided = plan_unified_montage(clips, brief_view(brief)).guided_edit()
    execution_plan = compile_execution_plan(guided, track=_GUIDED_TRACK)
    variant = {
        **song_reference_variant_fields(execution_plan),
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
    # Persist the editor revision the way a first Save would, so the variant is
    # guided-native for `_guided_v2_revision` (needs GUIDED_STORY_EDITOR_V2_ENABLED
    # for caps.timeline / clips.* to be advertised).
    from types import SimpleNamespace  # noqa: PLC0415

    from app.routes.generative_jobs import _guided_v2_revision  # noqa: PLC0415

    stub = SimpleNamespace(id=uuid.uuid4(), assembly_plan=plan)
    revision = _guided_v2_revision(stub, variant)  # type: ignore[arg-type]
    if revision is not None:
        variant["guided_edit_revision"] = revision
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
        # Protected PlanItem media fields may only be assigned by the
        # plan_item_media facade (test_protected_plan_item_media_fields_have_one_writer),
        # so the guided fixture is built first and passed to the constructor.
        guided_fixture = _guided_fixture(user.id) if guided else None
        item = PlanItem(
            content_plan_id=plan.id,
            position=1,
            idea="Kria local matcha update",
            item_status="awaiting_clips",
            edit_format="montage",
            **(
                {"clip_gcs_paths": guided_fixture[2], "clip_assignments": guided_fixture[3]}
                if guided_fixture is not None
                else {}
            ),
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
        media_state: list[dict] = []
        if guided_fixture is not None:
            plan_json, variant, paths, _assignments = guided_fixture
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
            item.item_status = "ready"
            item.current_job_id = job.id
            session.target_job_id = job.id
            session.target_variant_id = variant["variant_id"]
            session.target_generation_id = variant["render_generation_id"]
            job_id = str(job.id)
            # A real attached project mirrors its clips as opaque media entries on
            # thread.state (paths stay on the PlanItem). Without them runtime v2's
            # project.inspect reports "needs footage" and never reaches the planner.
            for i, path in enumerate(paths):
                mid = f"seed-media-{i + 1}"
                media_state.append(
                    {
                        "media_id": mid,
                        "kind": "video",
                        "filename": f"clip-{i + 1}.mp4",
                        "content_type": "video/mp4",
                        "size_bytes": 1,
                        "duration_s": 5.0,
                    }
                )
        thread = CreationThread(
            creator_id=user.id,
            runtime_version=2,
            content_plan_id=plan.id,
            active_plan_item_id=item.id,
            active_creator_agent_session_id=session.id,
            title="Kria local guided story" if guided else "Kria local matcha update",
            revision=1,
            state={
                "edit_format": "montage",
                "media": media_state,
                "media_count": len(media_state),
            },
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
