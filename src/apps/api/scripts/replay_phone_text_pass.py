"""Replay a failed phone montage's planning and text pass from its admin debug capture (KRI-1376).

``scripts/replay_creator.py`` re-plans a narrated job from source bytes through Main Creator and a
Celery worker. A ``creation_text_failed`` montage failed later, inside the worker, after the
creator approved: this driver runs that exact step in-process, from the job's own approved brief,
strategy, request and clip analysis, with the real text composer model. Storage, the job row and
the fences are stand-ins; nothing is dispatched and nothing writes to production.

    python scripts/admin.py --prod GET /admin/jobs/<id>/debug > /tmp/<id>.json
    DATABASE_URL=$(cat ../../../.dev/test-db-url) \\
        .venv/bin/python scripts/replay_phone_text_pass.py /tmp/<id>.json --out receipt.json \
        [--no-degrade]

``--no-degrade`` runs the text gates as before KRI-1376 (the negative control). The receipt names
the outcome, the notes the creator would read and the drawn text layers; each model call and its
cost is in the ``agent_run`` log lines.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _bindings(assignments: list[dict]) -> list[dict]:
    from app.kria.media_sources import OriginalMediaDescriptor  # noqa: PLC0415
    from app.services.phone_sources import PhoneSourceBinding  # noqa: PLC0415

    rows = []
    for row in assignments:
        original = ((row.get("upload_contract") or {}).get("proxy") or {}).get("original") or {}
        rows.append(
            PhoneSourceBinding(
                media_id=row["media_id"],
                proxy_path=row["gcs_path"],
                generation=str(row.get("storage_generation") or "1"),
                original=OriginalMediaDescriptor.model_validate(
                    {key: value for key, value in original.items() if key != "capture"}
                ),
            ).model_dump(mode="json")
        )
    return rows


def replay(capture: dict, *, degrade: bool) -> dict[str, Any]:
    from app.config import settings  # noqa: PLC0415
    from app.kria.brief import CreativeBrief  # noqa: PLC0415
    from app.kria.recipes import MediaCapability  # noqa: PLC0415
    from app.pipeline.guided_story import compile_execution_plan  # noqa: PLC0415
    from app.services import clip_facts  # noqa: PLC0415
    from app.services import creation_text_composition as composition  # noqa: PLC0415
    from app.services.creation_text_degrade import Degraded  # noqa: PLC0415
    from app.services.device_render import device_status  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD  # noqa: PLC0415
    from app.tasks import generative_build as gb  # noqa: PLC0415

    source = capture["job"]
    assembly = copy.deepcopy(source["assembly_plan"])
    binding = assembly["creator_brief_binding"]
    assignments = [dict(row) for row in binding["media_snapshot"]["clip_assignments"]]
    brief = CreativeBrief.model_validate(binding["brief"])
    assembly[PHONE_SOURCES_FIELD] = _bindings(assignments)
    job = SimpleNamespace(
        id=uuid.UUID(source["id"]),
        user_id=uuid.UUID(source["user_id"]),
        assembly_plan=assembly,
        status="queued",
        all_candidates=copy.deepcopy(source["all_candidates"]),
        error_detail=None,
        failure_reason=None,
        content_plan_item_id=uuid.UUID(source["content_plan_item_id"]),
    )
    failures: list[dict] = []
    events: list[tuple[str, str, dict]] = []

    @contextmanager
    def session():
        yield mock.Mock()

    def guided_plan(_job_id, guided):
        plan = compile_execution_plan(guided, track=None)
        job.assembly_plan["guided_story_execution_plan"] = plan
        return plan, None

    def fail(_job_id, detail, **kwargs):
        failures.append({"detail": detail, **{k: v for k, v in kwargs.items() if k != "decline"}})
        return True

    patches = [
        mock.patch.object(gb, "_sync_session", session),
        mock.patch.object(gb, "_lock_owned_entry_job", lambda *_a: (job, 1)),
        mock.patch.object(
            gb, "_load_unified_montage_inputs", lambda _id: (job.user_id, assignments, brief)
        ),
        mock.patch.object(gb, "_load_unified_montage_visuals", lambda *_a, **_k: []),
        mock.patch.object(gb, "_guided_execution_plan", guided_plan),
        mock.patch.object(gb, "_fail_job", fail),
        mock.patch.object(gb, "mark_failed_phase", lambda *_a, **_k: None),
        mock.patch.object(clip_facts, "enrich_clip_facts", lambda results, **_k: list(results)),
        mock.patch.object(settings, "phone_rendering_enabled", True),
        mock.patch.object(settings, "phone_font_qualification_strict", False),
        mock.patch.object(
            settings,
            "phone_render_verified_features",
            list(MediaCapability.__args__),  # type: ignore[attr-defined]
        ),
        mock.patch(
            "app.services.pipeline_trace.record_pipeline_event",
            lambda stage, event, data=None: events.append((stage, event, data or {})),
        ),
    ]
    if not degrade:
        patches += [
            mock.patch.object(
                composition,
                "degrade_text_ops",
                lambda ops, attempt, **_k: Degraded(attempt(ops), ops, [], None),
            ),
            mock.patch.object(composition, "shrink", lambda error: (_ for _ in ()).throw(error)),
        ]
    for patch in patches:
        patch.start()
    try:
        gb._run_generative_job(str(job.id))
    finally:
        mock.patch.stopall()
    variant = (job.assembly_plan.get("variants") or [{}])[0]
    drawn: list[str] = []
    if job.status == "awaiting_device":
        recipe = device_status(job, "guided_story").request.recipe
        drawn = [" ".join(run.text for run in layer.runs) for layer in recipe.text_layers]
    return {
        "job_id": source["id"],
        "degrade": degrade,
        "status": job.status,
        "failure": failures[-1] if failures else None,
        "phone_repair_notes": variant.get("phone_repair_notes") or [],
        "text_recoveries": [
            data
            for _stage, event, data in events
            if event == "requirement_recovery" and data.get("lane") == "text"
        ],
        "drawn_text_layers": drawn,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="admin /jobs/<id>/debug JSON")
    parser.add_argument("--out", type=Path, required=True, help="where the receipt is written")
    parser.add_argument("--no-degrade", action="store_true", help="run the pre-KRI-1376 gates")
    args = parser.parse_args(argv)
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    receipt = replay(capture, degrade=not args.no_degrade)
    args.out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
