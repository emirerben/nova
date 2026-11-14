"""Replay a failed phone montage's planning and text pass from its admin debug capture (KRI-1376).

``scripts/replay_creator.py`` re-plans a narrated job from source bytes through Main Creator and a
Celery worker. A ``creation_text_failed`` montage failed later, inside the worker, after the
creator approved: this driver runs that exact step in-process, from the job's own approved brief,
strategy, request and clip analysis, with the real text composer model. Storage, the job row and
the fences are stand-ins; nothing is dispatched and nothing writes to production.

    python scripts/admin.py --prod GET /admin/jobs/<id>/debug > /tmp/<id>.json
    DATABASE_URL=$(cat ../../../.dev/test-db-url) \\
        .venv/bin/python scripts/replay_phone_text_pass.py /tmp/<id>.json --out receipt.json \
        [--no-degrade] [--record answers.json | --answers answers.json]

``--no-degrade`` runs the text gates as before KRI-1376 (the negative control). The receipt names
the outcome, the notes the creator would read and the drawn text layers; each model call and its
cost is in the ``agent_run`` log lines.

``--record`` writes every model agent's raw answer of a live run, in call order, as
``[[agent class, raw text or null], ...]`` (null: the call failed). ``--answers`` replays such a
file at $0: each model agent gets its next recorded answer and no model is called. An agent with
no recorded answer fails its call, and the receipt's ``not_replayed`` names it: the phone
typography call falls back to Inter on a failure, so a replay without its answer can render
where prod's font choice would not.

Clip facts run for real (the capture copies onto each clip's analysis) except the landmark guess,
which makes no model call. Visuals cannot be rebuilt: no read-only admin route returns their
stored path, generation and analysis. The receipt's ``not_replayed`` lists every such gap for
this job; a non-empty list means the plan, and every outcome after it, may differ from prod.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))


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


Answers = list[list[str | None]]


@contextmanager
def _model_answers(answers: Answers | None, recorded: Answers, unanswered: list[str]):
    """Answer every model agent from ``answers`` (no model call), or record each live answer."""
    from app.agents import _runtime  # noqa: PLC0415

    run = _runtime.Agent.run
    queue = [list(row) for row in answers or ()]

    def answered(self, input, *, ctx=None):  # noqa: A002
        name = type(self).__name__
        for index, (agent, raw) in enumerate(queue):
            if agent == name:
                del queue[index]
                if raw is None:
                    raise _runtime.TerminalError(f"{name}: the recorded call failed")
                return self.parse(raw, self._validate_input(input))
        unanswered.append(name)
        raise _runtime.TerminalError(f"{name}: no recorded answer")

    def recording(self, input, *, ctx=None):  # noqa: A002
        raw: list[str] = []
        parse = self.parse

        def keep(text, validated):
            output = parse(text, validated)
            raw[:] = [text]
            return output

        self.parse = keep
        try:
            return run(self, input, ctx=ctx)
        finally:
            del self.parse
            recorded.append([type(self).__name__, raw[0] if raw else None])

    def routed(self, input, *, ctx=None):  # noqa: A002
        if self.spec.model == "rule_based":
            return run(self, input, ctx=ctx)
        if answers is not None:
            return answered(self, input, ctx=ctx)
        return recording(self, input, ctx=ctx)

    with mock.patch.object(_runtime.Agent, "run", routed):
        yield


def _not_replayed(snapshot: dict, landmark_asks: int, unanswered: list[str]) -> list[str]:
    gaps = [f"{name}: no recorded answer, so its call failed" for name in unanswered]
    visuals = snapshot.get("visuals") or []
    if visuals:
        gaps.append(
            f"visuals: the approved draft placed {len(visuals)} Visuals; this replay plans "
            "without them"
        )
    if landmark_asks:
        gaps.append(
            f"landmark guesses: {landmark_asks} clips would ask the landmark model; this replay "
            "answers none"
        )
    return gaps


def replay(
    capture: dict,
    *,
    degrade: bool,
    answers: Answers | None = None,
    recorded: Answers | None = None,
) -> dict[str, Any]:
    from app.config import settings  # noqa: PLC0415
    from app.kria.brief import CreativeBrief  # noqa: PLC0415
    from app.kria.recipes import MediaCapability  # noqa: PLC0415
    from app.pipeline.guided_story import compile_execution_plan  # noqa: PLC0415
    from app.services import clip_facts  # noqa: PLC0415
    from app.services import creation_text_composition as composition  # noqa: PLC0415
    from app.services.creation_text_degrade import (  # noqa: PLC0415
        Degraded,
        finished_render_notes,
        text_drop_notes,
    )
    from app.services.device_render import device_status  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD  # noqa: PLC0415
    from app.tasks import generative_build as gb  # noqa: PLC0415
    from app.tasks.kria_runtime import _plan_record_generations  # noqa: PLC0415

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
    text_drops: list = []
    landmark_asks: list[str] = []
    unanswered: list[str] = []
    recorded = recorded if recorded is not None else []
    compose = composition.compose_creation_text

    def composed(*args, drops=None, **kwargs):
        result = compose(*args, drops=drops, **kwargs)
        text_drops[:] = list(drops or [])
        return result

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
        mock.patch.object(
            clip_facts,
            "_guess_landmark",
            lambda entry, **_k: landmark_asks.append(str(entry.get("media_id"))),
        ),
        mock.patch.object(gb, "_checkpoint_unified_facts", lambda *_a: None),
        # Prod runs with CLIP_FACTS_ENABLED on.
        mock.patch.object(settings, "clip_facts_enabled", True),
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
        mock.patch.object(composition, "compose_creation_text", composed),
    ]
    if not degrade:
        patches += [
            mock.patch.object(
                composition,
                "degrade_text_ops",
                lambda ops, attempt, **_k: Degraded(attempt(ops), ops, [], None),
            ),
            mock.patch.object(
                composition, "shrink", lambda error, **_k: (_ for _ in ()).throw(error)
            ),
        ]
    for patch in patches:
        patch.start()
    try:
        with _model_answers(answers, recorded, unanswered):
            gb._run_generative_job(str(job.id))
    finally:
        mock.patch.stopall()
    variant = (job.assembly_plan.get("variants") or [{}])[0]
    drawn: list[str] = []
    fonts: list[str] = []
    if job.status == "awaiting_device":
        recipe = device_status(job, "guided_story").request.recipe
        drawn = [" ".join(run.text for run in layer.runs) for layer in recipe.text_layers]
        fonts = sorted({run.font_asset_id for layer in recipe.text_layers for run in layer.runs})
    return {
        "job_id": source["id"],
        "degrade": degrade,
        "not_replayed": _not_replayed(binding["media_snapshot"], len(landmark_asks), unanswered),
        "status": job.status,
        "failure": failures[-1] if failures else None,
        "text_drops": [drop.record() for drop in text_drops],
        "text_pass_notes": text_drop_notes(text_drops),
        "phone_repair_notes": variant.get("phone_repair_notes") or [],
        "finished_reply_notes": (
            finished_render_notes(job.assembly_plan, _plan_record_generations(job, variant))
            if variant
            else []
        ),
        "text_recoveries": [
            data
            for _stage, event, data in events
            if event == "requirement_recovery" and data.get("lane") == "text"
        ],
        "drawn_text_layers": drawn,
        "drawn_fonts": fonts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="admin /jobs/<id>/debug JSON")
    parser.add_argument("--out", type=Path, required=True, help="where the receipt is written")
    parser.add_argument("--no-degrade", action="store_true", help="run the pre-KRI-1376 gates")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--record", type=Path, help="write the live run's model answers here")
    source.add_argument("--answers", type=Path, help="replay these recorded model answers ($0)")
    args = parser.parse_args(argv)
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    answers = json.loads(args.answers.read_text(encoding="utf-8")) if args.answers else None
    out, record = args.out.resolve(), args.record.resolve() if args.record else None
    import app.config  # noqa: F401, PLC0415 - settings read the caller's .env before the chdir

    # The image runs from this layout (/app). From the repo root the planner sized e7bf7099's
    # pinned lines 52/52/46 px instead of 52/36/50, and the contract fit decline went away.
    os.chdir(API_DIR)
    recorded: Answers = []
    receipt = replay(capture, degrade=not args.no_degrade, answers=answers, recorded=recorded)
    if record is not None:
        record.write_text(json.dumps(recorded, ensure_ascii=False, indent=2) + "\n")
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, default=str) + "\n")
    for gap in receipt["not_replayed"]:
        print(f"not replayed: {gap}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
