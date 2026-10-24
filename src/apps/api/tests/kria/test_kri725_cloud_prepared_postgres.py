"""KRI-725 acceptance: cloud adapters honour the prepared execution snapshot.

Failure matrix written before implementation: docs/reviews/kri-725-prepared-coverage.md.
Real HTTP (ASGI) approval, real Postgres, real ``decide_approval`` ->
``execute_kria_approval`` (claim) -> ``dispatch_item_render_for`` ->
``_run_generative_job``. Substituted: the provider transport, the broker, object
storage (a local directory) and, for classic/guided, everything after the worker
start fence (those renders need paid models; their output proof is KRI-734's).
The slide post renders through the real ``_build_slide_post_result`` (ffmpeg
stills, no model) and the test reads the slide order back out of the MP4.

``KRI725_ARTIFACT=<dir>`` writes ``report.json``, ``slides_positive.mp4`` and
``frames.png`` (one frame per slide, in order) there.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app import storage
from app.config import settings
from app.database import sync_session
from app.kria import prepared_execution as prepared
from app.kria.drafts import KriaDraftDocument
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorEditDraft,
    Job,
    PlanItem,
    PlanItemAsset,
)
from app.schemas.slide_post import SlidePostDraft, SlideRef
from app.tasks import generative_build as gb
from tests.kria.test_approval_readiness_postgres import readiness_flags  # noqa: F401
from tests.kria.test_prepared_execution_postgres import (  # noqa: F401
    _approved,
    _dispatch,
    _snapshot_row,
)

ARTIFACT = os.environ.get("KRI725_ARTIFACT")
_report: dict[str, object] = {}


def _record(case: str, **fields: object) -> None:
    _report[case] = {
        k: v if isinstance(v, (bool, int, float, list)) else (str(v) if v is not None else None)
        for k, v in fields.items()
    }
    if ARTIFACT:
        out = Path(ARTIFACT)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(_report, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def broker(monkeypatch):
    """The render broker: record publishes instead of reaching Redis (as KRI-722's)."""
    published: list[str] = []
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync",
        lambda _task, job_id, **_kw: published.append(job_id),
    )
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda _job: None)
    return published


class _PassedFence(Exception):
    """Raised by the first step after the worker start fence (no render spend)."""


@pytest.fixture
def past_the_fence(monkeypatch):
    """Stop the cloud worker at its first post-fence step and capture the attempt."""
    seen: list[dict | None] = []

    def _stop(_assembly):
        seen.append(gb._PREPARED_ATTEMPT.get())
        raise _PassedFence

    monkeypatch.setattr(gb, "_pinned_creator_direction_typed_overrides", _stop)
    return seen


def _run_worker(job_id: str) -> bool:
    """True when the worker passed the start fence."""
    try:
        gb._run_generative_job(job_id)
    except _PassedFence:
        return True
    return False


_TIMING = {
    "kind": "timing",
    "scope": "global",
    "description": "6 seconds",
    "facts": {"duration_s": 6},
}


_LIGHT = {
    "kind": "style",
    "scope": "global",
    "description": "Lift the shadows in the footage",
    "facts": {"footage_light": "brighter"},
}


async def _approved_cloud(monkeypatch, *, timed: bool = True):
    """A cloud approval the cloud adapter accepts at preflight (a 6-second ask).

    An exact on-screen title would be declined before the start fence by
    ``preflight_cloud_contract`` on the adapter this edit routes to.
    """
    return await _approved(
        monkeypatch,
        updates_override=[_TIMING if timed else _LIGHT],
        message="Make the edit 6 seconds." if timed else "Lift the shadows in the footage.",
        action={
            "kind": "propose_strategy",
            "summary": "A six-second edit.",
            "strategy": {
                "direction": "guided_story",
                "edit_format": "day_vlog",
                "audio_strategy": "licensed_music",
                "pacing": "balanced",
                "render_program": "guided",
                "target_duration_s": 6,
                "selected_media_ids": ["clip-1"],
                "rationale": "Use the stadium flag.",
            },
        },
    )


async def _stamped_cloud_job(
    monkeypatch, *, timed: bool = True
) -> tuple[uuid.UUID, uuid.UUID, Job]:
    _, thread_id, approval = await _approved_cloud(monkeypatch, timed=timed)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched", result
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(result["job_id"]))
        assert prepared.read_stamp(job.assembly_plan) is not None
        db.expunge(job)
    return approval.id, thread_id, job


def _tamper(db, job, row):
    row.snapshot = {**row.snapshot, "route": "something_else"}


def _invalidate(db, job, row):
    row.invalidated_at = db.scalar(select(__import__("sqlalchemy").func.now()))
    row.invalidation_code = prepared.STALE


def _newer_compiler(db, job, row):
    row.snapshot = {**row.snapshot, "compiler_version": prepared.COMPILER_VERSION + 1}


def _other_generation(db, job, row):
    job.assembly_plan = {**job.assembly_plan, "creator_generation_id": uuid.uuid4().hex}


def _foreign_stamp(db, job, row):
    stamp = dict(job.assembly_plan[prepared.STAMP_FIELD])
    stamp["digest"] = "0" * 64
    job.assembly_plan = {**job.assembly_plan, prepared.STAMP_FIELD: stamp}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,code",
    [
        (None, None),
        ("capability", None),
        (_tamper, prepared.STALE),
        (_invalidate, prepared.STALE),
        (_newer_compiler, prepared.UNSUPPORTED),
        (_other_generation, prepared.STALE),
        (_foreign_stamp, prepared.STALE),
    ],
    ids=[
        "positive",
        "F12-deploy-after-accept",
        "F4-snapshot-tampered",
        "F5-snapshot-invalidated",
        "F6-newer-compiler",
        "F8-generation-mismatch",
        "stamp-digest-mismatch",
    ],
)
async def test_cloud_worker_starts_only_the_stamped_attempt(
    monkeypatch,
    broker,
    past_the_fence,
    mutate,
    code,
):
    approval_id, _thread, job = await _stamped_cloud_job(monkeypatch)
    with sync_session() as db:
        row = _snapshot_row(db, approval_id)
        live = db.get(Job, job.id)
        if mutate == "capability":
            # A deploy after the attempt was accepted must not cancel it (KRI-722 F10).
            monkeypatch.setattr(
                settings,
                "phone_render_verified_features",
                [*settings.phone_render_verified_features, "kri725Probe"],
            )
        elif mutate is not None:
            mutate(db, live, row)
        db.commit()
    passed = _run_worker(str(job.id))
    with sync_session() as db:
        after = db.get(Job, job.id)
        stamp = prepared.read_stamp(after.assembly_plan)
        if code is None:
            assert passed, (after.status, after.failure_reason, after.error_detail)
            assert [{k: a[k] for k in ("id", "attempt_id")} for a in past_the_fence] == [
                {"id": stamp["id"], "attempt_id": stamp["attempt_id"]}
            ]
        else:
            assert not passed and past_the_fence == []
            assert after.status == "processing_failed"
            assert after.failure_reason == code
            assert not after.assembly_plan.get("variants")
        label = mutate if isinstance(mutate, str) else getattr(mutate, "__name__", "none")
        _record(
            f"worker_start[{label}]",
            job=after.id,
            snapshot=stamp["id"] if stamp else None,
            attempt=stamp.get("attempt_id") if stamp else None,
            passed_fence=passed,
            failure_reason=after.failure_reason,
        )


@pytest.mark.asyncio
async def test_legacy_unstamped_cloud_job_is_untouched(monkeypatch, broker, past_the_fence):
    """F10: flag off -> no stamp -> no attempt recorded, publication unchanged."""
    monkeypatch.setattr(settings, "kria_prepared_execution_enabled", False)
    _, _thread, approval = await _approved_cloud(monkeypatch)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched", result
    job_id = result["job_id"]
    with sync_session() as db:
        before = copy.deepcopy(db.get(Job, uuid.UUID(job_id)).assembly_plan)
    assert _run_worker(job_id)
    assert past_the_fence == [None]
    with sync_session() as db:
        after = db.get(Job, uuid.UUID(job_id)).assembly_plan
        assert prepared.STAMP_FIELD not in after
        assert prepared.APPROVED_SLIDE_POST_FIELD not in after
        # The worker preamble may only add what it always added for legacy jobs.
        assert {k: v for k, v in after.items() if k in before} == {
            k: v for k, v in before.items() if k in after
        }
    candidate = {"variant_id": "original_text", "render_status": "ready", "video_path": "x.mp4"}
    assert gb._prepared_attempt_superseded(after, candidate) is None
    _record("legacy_unstamped", job=job_id, stamped=False)


def _seed_variant(job_id: uuid.UUID) -> None:
    with sync_session() as db:
        job = db.get(Job, job_id)
        job.assembly_plan = {
            **job.assembly_plan,
            "variants": [
                {
                    "variant_id": "original_text",
                    "ok": True,
                    "render_status": "ready",
                    "video_path": "last-good.mp4",
                }
            ],
        }
        db.commit()


def _repin_variant(job_id: uuid.UUID, attempt_id: str) -> None:
    """A newer editor approval re-pins one variant (KRI-722 stamp_job with variant_id)."""
    with sync_session() as db:
        job = db.get(Job, job_id)
        stamp = dict(job.assembly_plan[prepared.STAMP_FIELD])
        prepared.stamp_job(job, {**stamp, "attempt_id": attempt_id, "variant_id": "original_text"})
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["upsert", "update"])
async def test_late_publication_of_a_superseded_attempt_keeps_last_good(
    monkeypatch,
    broker,
    boundary,
):
    """F7/F15: inside a prepared attempt, only the variant's current attempt publishes."""
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda result, job_id: (result, []))
    # No objective contract requirement, so only the prepared fence decides here.
    _approval, _thread, job = await _stamped_cloud_job(monkeypatch, timed=False)
    _seed_variant(job.id)
    stamp = prepared.read_stamp(job.assembly_plan)
    first = {"id": stamp["id"], "attempt_id": stamp["attempt_id"]}

    def publish(video: str) -> bool:
        result = {
            "variant_id": "original_text",
            "ok": True,
            "render_status": "ready",
            "video_path": video,
        }
        if boundary == "upsert":
            return gb._upsert_variant_entry(str(job.id), result)
        return gb._update_variant_entry(
            str(job.id), "original_text", result, cleanup_followup="none"
        )

    def variant() -> dict:
        with sync_session() as db:
            return next(
                v
                for v in db.get(Job, job.id).assembly_plan["variants"]
                if v["variant_id"] == "original_text"
            )

    token = gb._PREPARED_ATTEMPT.set(first)
    try:
        # The renderer attached no record itself: the boundary still fences it.
        _repin_variant(job.id, "newer-editor-attempt")
        publish("superseded.mp4")
        refused = variant()
        assert refused["video_path"] == "last-good.mp4"
        assert refused["render_status"] == "failed"
        assert refused["prepared_execution_code"] == prepared.SUPERSEDED
        # The current attempt publishes.
        gb._PREPARED_ATTEMPT.set({"id": stamp["id"], "attempt_id": "newer-editor-attempt"})
        publish("current.mp4")
        accepted = variant()
        assert accepted["video_path"] == "current.mp4", accepted
        assert accepted["render_status"] == "ready"
        assert "prepared_execution_code" not in accepted or boundary == "upsert"
    finally:
        gb._PREPARED_ATTEMPT.reset(token)
    _record(
        f"publication[{boundary}]",
        job=job.id,
        snapshot=stamp["id"],
        superseded_attempt=first["attempt_id"],
        current_attempt="newer-editor-attempt",
        kept_last_good=refused["video_path"] == "last-good.mp4",
        published=accepted["video_path"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["upsert", "update"])
async def test_full_job_rerender_of_an_editor_approved_variant_publishes(
    monkeypatch, broker, past_the_fence, boundary
):
    """F16: a creator's full-job rerender (speech timing / speech cut) re-enters the
    worker with the Job-level stamp. A variant an editor approval already re-pinned is
    fenced against its own stamp, so the creator's edit publishes; only an approval
    that lands after the run started supersedes it."""
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda result, job_id: (result, []))
    _approval, _thread, job = await _stamped_cloud_job(monkeypatch, timed=False)
    _seed_variant(job.id)
    _repin_variant(job.id, "editor-attempt")

    # The real worker start fence, as rerender_speech_timing re-enters it.
    assert _run_worker(str(job.id))
    attempt = past_the_fence[-1]
    assert attempt is not None

    def publish(video: str) -> dict:
        result = {
            "variant_id": "original_text",
            "ok": True,
            "render_status": "ready",
            "video_path": video,
        }
        token = gb._PREPARED_ATTEMPT.set(attempt)
        try:
            if boundary == "upsert":
                gb._upsert_variant_entry(str(job.id), result)
            else:
                gb._update_variant_entry(
                    str(job.id), "original_text", result, cleanup_followup="none"
                )
        finally:
            gb._PREPARED_ATTEMPT.reset(token)
        with sync_session() as db:
            return next(
                v
                for v in db.get(Job, job.id).assembly_plan["variants"]
                if v["variant_id"] == "original_text"
            )

    accepted = publish("creator-rerender.mp4")
    assert accepted["video_path"] == "creator-rerender.mp4", accepted
    assert accepted["render_status"] == "ready"
    # Negative control: a newer editor approval mid-run still supersedes this run.
    _repin_variant(job.id, "newer-editor-attempt")
    refused = publish("late.mp4")
    assert refused["video_path"] == "creator-rerender.mp4"
    assert refused["prepared_execution_code"] == prepared.SUPERSEDED
    _record(
        f"full_job_rerender_editor_variant[{boundary}]",
        job=job.id,
        published=accepted["video_path"],
        superseded_by_newer_approval=refused["render_status"] == "failed",
    )


# --- Slides --------------------------------------------------------------------

_COLORS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255)}


@pytest.fixture
def local_storage(monkeypatch, tmp_path):
    root = tmp_path / "bucket"
    root.mkdir()

    def _path(key: str) -> Path:
        return root / key

    def _download(key, local, **_kw):
        shutil.copyfile(_path(key), local)

    def _upload(local, key, _content_type=None, **_kw):
        _path(key).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, _path(key))

    monkeypatch.setattr(storage, "object_exists", lambda key: _path(key).exists())
    monkeypatch.setattr(storage, "download_to_file", _download)
    monkeypatch.setattr(storage, "download_generation_to_file", _download)
    monkeypatch.setattr(storage, "upload_local_file", _upload)
    return root


def _jpeg(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1080x1350",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )


def _seed_slides(local_storage: Path, thread_id: uuid.UUID) -> tuple[uuid.UUID, dict[str, str]]:
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        ids: dict[str, str] = {}
        refs = []
        for n, color in enumerate(_COLORS):
            path = f"users/{item.content_plan.user_id}/plan/{item.id}/pool/{color}.jpg"
            _jpeg(local_storage / path, color)
            asset = PlanItemAsset(
                plan_item_id=item.id,
                user_id=item.content_plan.user_id,
                gcs_path=path,
                gcs_generation="1",
                kind="image",
                status="ready",
                media_status="ready",
            )
            db.add(asset)
            db.flush()
            ids[color] = str(asset.id)
            refs.append(SlideRef(id=f"s{n}", asset_id=asset.id, kind="image"))
        item.edit_format = "slides"
        item.slide_post = SlidePostDraft(
            platform_profile="instagram_carousel", slides=refs, caption="Approved caption."
        ).model_dump(mode="json")
        db.commit()
        return item.id, ids


def _mint_slides_snapshot(approval_id: uuid.UUID, thread_id: uuid.UUID) -> dict:
    """The snapshot ``decide_approval`` mints for a slide-post approval, stored for real.

    Kria's strategy policy does not offer ``slides`` today (format_availability), so
    no planner draft can target it; the approval/draft rows are a real approved
    Kria approval and only the format is switched before the snapshot is minted.
    """
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        draft = db.get(CreatorEditDraft, approval.draft_id)
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        document = KriaDraftDocument.model_validate(draft.snapshot_json)
        old = _snapshot_row(db, approval_id)
        snapshot = prepared.build_snapshot(
            creator_id=approval.creator_id,
            thread_id=thread_id,
            approval_id=approval.id,
            draft=draft,
            document=document,
            sources=prepared.load_source_identities_sync(
                db, item, approval.creator_id, slides=True
            ),
            slide_post=(prepared.slide_post_draft(item),),
        )
        old.snapshot = snapshot.model_dump(mode="json")
        old.snapshot_digest = snapshot.digest
        db.commit()
        return {
            "row_id": old.id,
            "record": prepared.attempt_record(old, snapshot.digest, prepared.new_attempt_id()),
            "slots": [s.slot for s in snapshot.sources],
        }


def _claim_check(approval_id, thread_id):
    """The claim's own fence (``_verify_prepared_execution`` -> ``prepared.verify``)."""
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        draft = db.get(CreatorEditDraft, approval.draft_id)
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        snapshot = prepared.parse(_snapshot_row(db, approval_id))
        try:
            prepared.verify(
                snapshot,
                draft=draft,
                document=KriaDraftDocument.model_validate(draft.snapshot_json),
                thread_id=thread_id,
                sources=prepared.load_source_identities_sync(
                    db, item, approval.creator_id, slides=True
                ),
                item=item,
            )
        except prepared.PreparedExecutionError as exc:
            return exc
        return None


def _edit_live_draft(item_id, *, reorder=True, caption="Edited after approval."):
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        draft = SlidePostDraft.model_validate(item.slide_post)
        item.slide_post = draft.model_copy(
            update={
                "slides": list(reversed(draft.slides)) if reorder else draft.slides,
                "caption": caption,
                "version": draft.version + 1,
            }
        ).model_dump(mode="json")
        db.commit()


def _replace_photo(item_id, asset_id):
    with sync_session() as db:
        db.get(PlanItemAsset, uuid.UUID(asset_id)).gcs_generation = "2"
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,changed",
    [("caption", "slide_post"), ("reorder", "sources"), ("replace_photo", "sources")],
    ids=[
        "F1-caption-edited-before-claim",
        "F1-slides-reordered-before-claim",
        "F2-photo-replaced-before-claim",
    ],
)
async def test_slide_post_claim_refuses_a_changed_approval(
    monkeypatch,
    broker,
    local_storage,
    mutate,
    changed,
):
    _, thread_id, approval = await _approved(monkeypatch)
    item_id, ids = _seed_slides(local_storage, thread_id)
    minted = _mint_slides_snapshot(approval.id, thread_id)
    assert minted["slots"][-3:] == ["slide:0", "slide:1", "slide:2"]
    assert _claim_check(approval.id, thread_id) is None
    if mutate == "caption":
        _edit_live_draft(item_id, reorder=False)
    elif mutate == "reorder":
        _edit_live_draft(item_id)
    else:
        _replace_photo(item_id, ids["green"])
    refused = _claim_check(approval.id, thread_id)
    assert refused is not None and refused.code == prepared.STALE
    assert refused.changed == [changed]
    _record(f"slides_claim[{mutate}]", snapshot=minted["row_id"], code=refused.code)


def _dispatch_slides(item_id, record) -> str:
    from app.tasks.content_plan_build import dispatch_item_render_for

    result = dispatch_item_render_for(
        str(item_id), None, bypass_guided_edit_gate=True, prepared_execution=record
    )
    assert result.outcome == "dispatched", result
    return str(result.job_id)


def _frame_colors(mp4: Path, count: int, out_dir: Path | None) -> list[str]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probe.stdout.strip())
    colors = []
    frames = []
    for index in range(count):
        at = duration * (index + 0.5) / count
        raw = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-ss",
                f"{at:.3f}",
                "-i",
                str(mp4),
                "-frames:v",
                "1",
                "-vf",
                "crop=8:8:(iw-8)/2:(ih-8)/2,scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        rgb = tuple(raw[:3])
        colors.append(
            min(_COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(_COLORS[c], rgb)))
        )
        if out_dir is not None:
            frame = out_dir / f"frame_{index}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(mp4),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=270:-2",
                    str(frame),
                ],
                check=True,
            )
            frames.append(frame)
    if out_dir is not None and frames:
        inputs = [arg for frame in frames for arg in ("-i", str(frame))]
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                *inputs,
                "-filter_complex",
                f"hstack=inputs={len(frames)}",
                str(out_dir / "frames.png"),
            ],
            check=True,
        )
        for frame in frames:
            frame.unlink()
    return colors


@pytest.mark.asyncio
@pytest.mark.parametrize("red", [False, True], ids=["fenced", "red-control-live-read"])
async def test_slide_post_renders_the_approved_draft_not_a_later_edit(
    monkeypatch,
    broker,
    local_storage,
    red,
):
    """F3: the draft is edited after dispatch; the output keeps the approved order."""
    _, thread_id, approval = await _approved(monkeypatch)
    item_id, _ids = _seed_slides(local_storage, thread_id)
    minted = _mint_slides_snapshot(approval.id, thread_id)
    job_id = _dispatch_slides(item_id, minted["record"])
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        pin = job.assembly_plan[prepared.APPROVED_SLIDE_POST_FIELD]
        assert (
            pin["digest"]
            == prepared.parse(_snapshot_row(db, approval.id)).compiled_refs[prepared.SLIDE_POST_REF]
        )
        if red:
            # Red control: without the Job pin the worker falls back to the live read.
            job.assembly_plan = {
                k: v
                for k, v in job.assembly_plan.items()
                if k != prepared.APPROVED_SLIDE_POST_FIELD
            }
            db.commit()
    _edit_live_draft(item_id)
    gb._run_generative_job(job_id)
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        assert job.status in {"variants_ready", "done"}, (job.status, job.error_detail)
        variant = job.assembly_plan["variants"][0]
        assert variant["variant_id"] == "slides"
        preview = local_storage / variant["video_path"]
    out = Path(ARTIFACT) if ARTIFACT and not red else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(preview, out / "slides_positive.mp4")
    order = _frame_colors(preview, 3, out)
    expected = ["blue", "green", "red"] if red else ["red", "green", "blue"]
    assert order == expected
    caption = variant["slide_post"]["caption"]
    assert caption == ("Edited after approval." if red else "Approved caption.")
    _record(
        "slides_positive" if not red else "slides_red_control",
        job=job_id,
        snapshot=minted["row_id"],
        attempt=minted["record"]["attempt_id"],
        output_order=order,
        caption=caption,
        output_sha256=__import__("hashlib").sha256(preview.read_bytes()).hexdigest(),
    )


def _unready_photo(item_id, asset_id):
    with sync_session() as db:
        db.get(PlanItemAsset, uuid.UUID(asset_id)).media_status = "processing"
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [_replace_photo, _unready_photo], ids=["moved", "unready"])
async def test_slide_photo_replaced_after_dispatch_is_refused(
    monkeypatch, broker, local_storage, mutate
):
    """F2/F13 at the worker: a pinned asset that moved or left 'ready' does not render."""
    _, thread_id, approval = await _approved(monkeypatch)
    item_id, ids = _seed_slides(local_storage, thread_id)
    minted = _mint_slides_snapshot(approval.id, thread_id)
    job_id = _dispatch_slides(item_id, minted["record"])
    mutate(item_id, ids["blue"])
    # The Celery task itself, so its typed slide-policy handler terminalizes the Job.
    gb.orchestrate_generative_job.run(job_id)
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        assert job.status == "processing_failed"
        assert job.failure_reason == prepared.STALE
        assert not job.assembly_plan.get("variants")
    _record(f"slides_worker_{mutate.__name__}", job=job_id, failure_reason=prepared.STALE)


@pytest.mark.asyncio
async def test_guided_dispatch_binds_the_approved_compiled_story(monkeypatch, broker):
    """F9: a story replaced between approve and dispatch is not checked against itself."""
    from app.kria.approval_readiness import digest
    from app.tasks.content_plan_build import _story_matches_prepared_execution

    approval_id, _thread, job = await _stamped_cloud_job(monkeypatch)
    approved_story = {"story_id": "approved", "compiled_digest": "a" * 64}
    with sync_session() as db:
        row = _snapshot_row(db, approval_id)
        snapshot = prepared.parse(row).model_copy(
            update={"compiled_refs": {"prepared_collection_story": digest(approved_story)}}
        )
        row.snapshot = snapshot.model_dump(mode="json")
        row.snapshot_digest = snapshot.digest
        live = db.get(Job, job.id)
        stamp = dict(live.assembly_plan[prepared.STAMP_FIELD])
        live.assembly_plan = {
            **live.assembly_plan,
            prepared.STAMP_FIELD: {**stamp, "digest": snapshot.digest},
        }
        db.commit()
        live = db.get(Job, job.id)
        assert _story_matches_prepared_execution(db, live, approved_story)
        replaced = {**approved_story, "story_id": "replaced"}
        assert not _story_matches_prepared_execution(db, live, replaced)
        legacy = type("Legacy", (), {"assembly_plan": {}})()
        assert _story_matches_prepared_execution(db, legacy, replaced)
    _record("guided_story_binding", job=job.id, approved=True, replaced_refused=True)
