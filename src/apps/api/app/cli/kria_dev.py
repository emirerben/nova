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
# What the (stored) vision analysis says each seeded clip shows: `analysis["understanding"]`.
# The LAST clip has none, so descriptive-caption asks exercise "leave it out and say so".
# Real-footage-shaped understanding that does NOT match a "wedding" framing (--seen-mismatch).
_GUIDED_SEEN_MISMATCH = [
    (
        "Playing football on an outdoor artificial turf pitch at night",
        "outdoor turf pitch",
        "playing football",
    ),
    (
        "Playing football on an outdoor artificial turf pitch at night",
        "outdoor turf pitch",
        "playing football",
    ),
    (
        "Running up stairs and celebrating in a rustic bar",
        "rustic bar interior",
        "running up stairs and celebrating",
    ),
    ("Working on a laptop in a home office", "home office", "working on a laptop"),
    ("Taking a selfie video in an outdoor square", "outdoor square", "taking a selfie video"),
]
_GUIDED_SEEN = [
    ("Guests hugging at an airport arrivals hall", "airport terminal", "welcoming arrivals"),
    ("Family walking along a seaside promenade", "waterfront promenade", "walking and chatting"),
    ("Bride getting ready with friends", "decorated hotel room", "hair and makeup"),
    ("Groom and friends laughing in a bar", "evening bar", "pre-wedding celebration"),
    ("People dancing in a decorated hall", "wedding hall", "dancing"),
]
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


def _guided_fixture(
    user_id: uuid.UUID, *, seen: bool = True, mismatch: bool = False
) -> tuple[dict, dict, list[str], list[dict]]:
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
                "analysis": {
                    "clip_facts": [{**facts[0], "confidence": 0.8}],
                    **(
                        {
                            "understanding": {
                                "kind": "video",
                                "summary": (_GUIDED_SEEN_MISMATCH if mismatch else _GUIDED_SEEN)[i][
                                    0
                                ],
                                "setting": (_GUIDED_SEEN_MISMATCH if mismatch else _GUIDED_SEEN)[i][
                                    1
                                ],
                                "activity": (_GUIDED_SEEN_MISMATCH if mismatch else _GUIDED_SEEN)[
                                    i
                                ][2],
                            }
                        }
                        if seen and i < len(_GUIDED_SEEN)
                        else {}
                    ),
                },
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


def seed(
    email: str, *, guided: bool = False, seen: bool = True, mismatch: bool = False
) -> dict[str, str]:
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
        guided_fixture = _guided_fixture(user.id, seen=seen, mismatch=mismatch) if guided else None
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


def analyze(thread_id: uuid.UUID, *, apply: bool) -> dict[str, object]:
    """LOCAL ONLY: store vision `understanding` on a thread's clips (the missing producer).

    Runtime-v2 threads never run the clip analyzer on their main footage (only the guided
    proposal flow and Visuals autoplace do), so `analysis["understanding"]` stays empty.
    This runs `analyze_clip_assignment` (one Gemini vision call per clip, roughly
    $0.01-0.03 each; needs GEMINI_API_KEY and readable storage objects) and merges the
    result into `clip_assignments[].analysis` through the media-mutation facade.
    Without --apply it only lists what would be analyzed.
    """
    require_local_database(settings.database_url)
    from app.models import PlanItem  # noqa: PLC0415
    from app.services.clip_understanding import clip_record  # noqa: PLC0415
    from app.services.creator_clip_analysis import analyze_clip_assignment  # noqa: PLC0415
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_sync,
    )

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id) if thread else None
        if item is None:
            raise SystemExit("Thread/item not found")
        todo = [
            dict(row)
            for row in (item.clip_assignments or [])
            if isinstance(row, dict)
            and row.get("gcs_path")
            and row.get("media_id")
            and clip_record(row.get("analysis"), kind=str(row.get("kind") or "video")).is_empty()
        ]
        summary: dict[str, object] = {"clips_without_understanding": len(todo)}
        if not apply or not todo:
            return summary
        analyzed: dict[str, dict] = {}
        for raw in todo:
            entry, _ref = analyze_clip_assignment(raw, {}, require_semantic=True)
            analyzed[str(raw["media_id"])] = entry
        rows = []
        for row in item.clip_assignments or []:
            entry = analyzed.get(str(row.get("media_id"))) if isinstance(row, dict) else None
            if entry is None:
                rows.append(row)
                continue
            old = dict(row.get("analysis") or {})
            rows.append(
                {
                    **row,
                    **{
                        k: entry[k]
                        for k in ("generation", "kind", "duration_s", "aspect")
                        if k in entry
                    },
                    "analysis": {**old, **(entry.get("analysis") or {})},
                }
            )
        mutate_plan_item_media(
            item,
            detector_policy=current_detector_policy(),
            clip_assignments=rows,
            current_analysis=mutation_current_analysis_sync(db, item.id, for_update=True),
        )
        db.commit()
        summary["analyzed"] = len(analyzed)
        return summary


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
    seed_parser.add_argument(
        "--no-seen",
        action="store_true",
        help="with --guided: seed NO stored clip understanding (descriptive-caption clarify path)",
    )
    seed_parser.add_argument(
        "--seen-mismatch",
        action="store_true",
        help="with --guided: stored understanding that does not match a wedding framing",
    )
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--thread-id", required=True, type=uuid.UUID)
    analyze_parser.add_argument("--apply", action="store_true")
    reset_parser = commands.add_parser("reset")
    reset_parser.add_argument("--thread-id", required=True, type=uuid.UUID)
    reset_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "seed":
        result = seed(
            args.email,
            guided=args.guided,
            seen=not args.no_seen,
            mismatch=args.seen_mismatch,
        )
    elif args.command == "analyze":
        result = analyze(args.thread_id, apply=args.apply)
    else:
        result = reset(args.thread_id, apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
