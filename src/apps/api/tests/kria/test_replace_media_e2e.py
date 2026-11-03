"""End-to-end: re-record a take through the real `POST /creation-threads/{id}/media/replace`.

Real FastAPI app over ASGI, real Postgres (the scratch `*_test` database), real ffprobe on real
MP4 files, the real upload verifier, the real `mutate_plan_item_media` facade and the real
runtime-v2 approval locks. Only the object store is replaced: it serves local files, since the
bytes are not what is under test. Transcription and the planner agent are not involved;
what the next turn does with the marker is checked at the planner's own seam.

Failure modes this file answers (each is a test or a parametrized case):
  zero speakers      swap never leaves the item without its clip (upload not finished, no audio)
  two speakers       count is preserved; a cutaway or an unknown speaker is refused (multi-clip)
  stale approval     a pending approval for the old take is closed; a later one is untouched
  render in flight   409 on a running Job, a rerendering variant, and a phone export in flight
  wrong target       audio / voiceover / Visuals ids, a missing id, a non-speaking format
  concurrent attach  stale revision, and a second swap after the first
  retry / double tap same key replays, same key different body 409s, a second key is a no-op
  no-audio take      refused with the old take intact
"""

from __future__ import annotations

import copy
import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app import storage
from app.auth import get_current_user
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria import planner
from app.kria.brief import plan_shape_from_editor_snapshot, route_requirements
from app.main import app
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreationThreadUploadReservation,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
    User,
)
from app.services import phone_rollout
from app.services.plan_item_media import current_detector_policy, resolve_item_narration

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

REPORT = Path(__file__).resolve().parents[2] / ".replace-media-e2e" / "report.json"
GENERATION = "1790858000000000"
PROXY = "analysis-proxy-"


def _encode(path: Path, *, audio: bool, seconds: float = 2.0) -> None:
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i"]
    args += [f"testsrc=size=320x568:rate=30:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac"] if audio else ["-an"]
    subprocess.run([*args, str(path)], check=True)


def _contract(path: Path, *, audio: bool = True) -> dict:
    size = path.stat().st_size
    return {
        "purpose": "analysis_proxy",
        "proxy": {
            "original": {
                "kind": "video",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "byte_count": size,
                "duration_s": 2.0,
                "width": 1080,
                "height": 1920,
                "has_audio": audio,
            },
            "duration_s": 2.0,
            "width": 320,
            "height": 568,
            "frame_rate": 30.0,
        },
    }


@pytest.fixture(scope="module")
def takes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("takes")
    files = {name: root / f"{name}.mp4" for name in ("a", "b", "c", "silent")}
    for name, path in files.items():
        _encode(path, audio=name != "silent")
    return files


@dataclass
class World:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    item_id: uuid.UUID
    session_id: uuid.UUID
    job_id: uuid.UUID | None
    clips: dict[str, str]  # label -> media_id
    paths: dict[str, str]  # media_id -> object path
    files: dict[str, Path]  # object path -> local file

    def reserve(self, label: str, file: Path) -> str:
        """What a completed `upload-urls` + PUT leaves behind."""
        media_id = f"{PROXY}ios-{label}-{uuid.uuid4().hex[:8]}.mp4"
        path = f"users/{self.user_id}/creation-threads/{self.thread_id}/{media_id}"
        self.paths[media_id] = path
        self.files[path] = file
        with sync_session() as db:
            db.add(
                CreationThreadUploadReservation(
                    thread_id=self.thread_id,
                    creator_id=self.user_id,
                    media_id=media_id,
                    object_path=path,
                    upload_contract=_contract(file, audio="silent" not in file.name),
                    expires_at=func.now() + text("interval '1 day'"),
                )
            )
            db.commit()
        return media_id


def seed_world(
    takes: dict[str, Path],
    *,
    labels: tuple[str, ...] = ("a",),
    edit_format: str = "subtitled",
    job: str | None = "ready",
    speakers: tuple[str, ...] | None = None,
    voiceover: bool = False,
) -> World:
    user_id, persona_id, plan_id, item_id, session_id, thread_id = (uuid.uuid4() for _ in range(6))
    job_id = uuid.uuid4() if job else None
    clips, paths, files, assignments, media = {}, {}, {}, [], []
    for index, label in enumerate(labels):
        media_id = f"{PROXY}ios-{label}-{uuid.uuid4().hex[:8]}.mp4"
        path = f"users/{user_id}/creation-threads/{thread_id}/{media_id}"
        clips[label], paths[media_id], files[path] = media_id, path, takes[label]
        assignments.append(
            {
                "media_id": media_id,
                "manifest_identity": media_id,
                "gcs_path": path,
                "kind": "video",
                "shot_id": f"shot-{index}",
                "duration_s": 2.0,
                "has_audio": True,
                "storage_generation": GENERATION,
                "trim_start_s": 0.25,
                "trim_end_s": 1.5,
                "speech_coverage": 0.9,
                "upload_contract": _contract(takes[label]),
            }
        )
        media.append({"media_id": media_id, "kind": "video", "filename": f"{label}.mp4"})
    if voiceover:
        media.append({"media_id": "voiceover.m4a", "kind": "audio", "filename": "vo.m4a"})
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "render_status": "ready",
        "render_generation_id": "generation-1",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "text_elements": [],
    }
    if speakers is not None:
        variant["phone_talking_multi_clip"] = {
            "version": 1,
            "speaker_media_ids": [clips[label] for label in speakers],
        }
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="",
                item_status="awaiting_clips",
                edit_format=edit_format,
                content_mode="existing_footage",
                clip_gcs_paths=[row["gcs_path"] for row in assignments],
                clip_assignments=assignments,
                **(
                    {
                        "voiceover_gcs_path": "users/x/vo.m4a",
                        "voiceover_generation": "5",
                        "voiceover_duration_s": 3.0,
                        "audio_mode": "voiceover",
                    }
                    if voiceover
                    else {}
                ),
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Talking",
                revision=2,
                state={
                    "media": media,
                    "media_count": len(media),
                    "native_device_only": True,
                    "render_destination_intent": "device",
                },
            )
        )
        db.flush()
        for sequence, kind in enumerate(("thread_created", "media_added")):
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=sequence + 1,
                    role="system",
                    event_type=kind,
                )
            )
        if job_id is not None:
            db.add(
                Job(
                    id=job_id,
                    user_id=user_id,
                    status="variants_ready" if job == "ready" else "processing",
                    mode="generative",
                    raw_storage_path="",
                    selected_platforms=["tiktok"],
                    content_plan_item_id=item_id,
                    content_plan_ownership_epoch=0,
                    all_candidates={
                        "clip_paths": [row["gcs_path"] for row in assignments],
                        "clip_source_instance_ids": [uuid.uuid4().hex for _ in assignments],
                    },
                    assembly_plan={"variants": [variant]},
                )
            )
            db.flush()
            db_item = db.get(PlanItem, item_id)
            db_item.current_job_id = job_id
            db_session = db.get(CreatorAgentSession, session_id)
            db_session.target_job_id = job_id
            db_session.target_variant_id = "original_text"
            db_session.target_generation_id = "generation-1"
        db.commit()
    return World(user_id, thread_id, item_id, session_id, job_id, clips, paths, files)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("kria_runtime_v2_enabled", "media_replace_enabled"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "media_library_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def object_store(monkeypatch: pytest.MonkeyPatch):
    """Serves local files for the object store and records every delete."""
    registry: dict[str, Path] = {}
    deleted: list[str] = []

    def metadata(path: str):
        if path not in registry:
            raise FileNotFoundError(path)
        return SimpleNamespace(
            content_type="video/mp4", size=registry[path].stat().st_size, generation=GENERATION
        )

    monkeypatch.setattr(storage, "object_metadata", metadata)
    monkeypatch.setattr(
        storage, "signed_get_url_for_generation", lambda path, generation: str(registry[path])
    )
    monkeypatch.setattr(storage, "signed_put_url", lambda *a, **k: "https://store.test/put")
    monkeypatch.setattr(storage, "signed_get_url", lambda *a, **k: "https://store.test/get")
    for name in (
        "delete_object_best_effort",
        "delete_object_generation",
        "delete_object_generation_best_effort",
        "delete_object_once",
    ):
        monkeypatch.setattr(storage, name, lambda path, *a, **k: deleted.append(path) or True)
    for name in ("delete_prefix_best_effort", "delete_prefix_verified", "delete_prefix_once"):
        monkeypatch.setattr(storage, name, lambda *a, **k: deleted.append("prefix") or 0)
    return SimpleNamespace(registry=registry, deleted=deleted)


class Api:
    def __init__(self, world: World, store) -> None:  # noqa: ANN001
        self.world, self.store = world, store
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=world.user_id, email="qa@test.local"
        )

    def url(self, tail: str = "") -> str:
        return f"/creation-threads/{self.world.thread_id}{tail}"

    async def post(self, tail: str, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(self.url(tail), json=body)

    def revision(self) -> int:
        with sync_session() as db:
            return int(db.get(CreationThread, self.world.thread_id).revision)

    def upload(self, label: str, file: Path) -> str:
        media_id = self.world.reserve(label, file)
        self.store.registry[self.world.paths[media_id]] = file
        return media_id

    async def replace(
        self, replaced: str, new: str, *, key: str | None = None, revision: int | None = None
    ) -> httpx.Response:
        return await self.post(
            "/media/replace",
            {
                "replaced_media_id": replaced,
                "media": {"media_id": new, "kind": "video", "filename": "retake.mp4"},
                "reason": "retake",
                "client_event_id": key or f"ios-replace-{uuid.uuid4().hex[:10]}",
                "expected_revision": self.revision() if revision is None else revision,
            },
        )


def _api(world: World, store) -> Api:  # noqa: ANN001
    for path, file in world.files.items():
        store.registry[path] = file
    return Api(world, store)


def snapshot(world: World) -> dict:
    """Everything a refused replace must leave exactly as it was."""
    with sync_session() as db:
        item = db.get(PlanItem, world.item_id)
        thread = db.get(CreationThread, world.thread_id)
        job = db.get(Job, world.job_id) if world.job_id else None
        return {
            "paths": list(item.clip_gcs_paths),
            "assignments": copy.deepcopy(item.clip_assignments),
            "state": copy.deepcopy(thread.state),
            "revision": thread.revision,
            "events": db.scalar(
                select(func.count())
                .select_from(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == world.thread_id)
            ),
            "job": (job.status, copy.deepcopy(job.assembly_plan), copy.deepcopy(job.all_candidates))
            if job
            else None,
        }


def code(response: httpx.Response) -> str | None:
    return (response.json().get("problem") or {}).get("code")


async def pending_approval(world: World, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """A real runtime-v2 strategy approval, prepared against the take as it is now."""
    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
    from app.kria.api_schemas import SubmitTurnBody
    from app.kria.planner import PlannedKriaTurn, adapt_creator_action
    from app.kria.runtime import submit_turn
    from app.tasks.kria_runtime import run_kria_turn

    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                render_program="native",
                selected_media_ids=[],
                rationale="One take, captioned.",
            ),
            summary="Captions on your take.",
        )
    )

    async def planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    from app.database import AsyncSessionLocal

    with sync_session() as db:
        revision = db.get(CreationThread, world.thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=world.thread_id,
            creator_id=world.user_id,
            body=SubmitTurnBody(
                message="Add captions",
                client_event_id=f"turn-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    import asyncio

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval", result
    with sync_session() as db:
        return db.execute(
            select(CreatorAgentApproval.id).where(
                CreatorAgentApproval.thread_id == world.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()


def approval_state(approval_id: uuid.UUID) -> tuple[str, str | None]:
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        turn = db.get(CreatorAgentTurn, approval.turn_id)
        return approval.status, (turn.error or {}).get("code")


def write_report(name: str, payload: dict) -> None:
    import json

    REPORT.parent.mkdir(exist_ok=True)
    existing = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    existing[name] = payload
    REPORT.write_text(json.dumps(existing, indent=2, sort_keys=True, default=str))


# --- the happy path -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retake_swaps_the_speaker_and_the_next_turn_plans_fresh(
    takes, object_store, monkeypatch
) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    old_id = world.clips["a"]
    old_path = world.paths[old_id]
    before = snapshot(world)
    approval_id = await pending_approval(world, monkeypatch)

    new_id = api.upload("b", takes["b"])
    reserve = await api.post(
        "/upload-urls",
        {
            "files": [
                {
                    "filename": "retake.mp4",
                    "content_type": "video/mp4",
                    "file_size_bytes": takes["b"].stat().st_size,
                    "client_upload_id": "ios-retake-1",
                    "upload_contract": _contract(takes["b"]),
                    "replaces_media_id": old_id,
                }
            ]
        },
    )
    assert reserve.status_code == 200, reserve.text
    # A first Talking take is capped at one clip: the same body without the field is refused.
    capped = await api.post(
        "/upload-urls",
        {
            "files": [
                {
                    "filename": "retake.mp4",
                    "content_type": "video/mp4",
                    "file_size_bytes": takes["b"].stat().st_size,
                    "client_upload_id": "ios-retake-2",
                    "upload_contract": _contract(takes["b"]),
                }
            ]
        },
    )
    assert capped.status_code == 400, capped.text

    revision = api.revision()
    key = f"ios-replace-{uuid.uuid4().hex[:10]}"
    response = await api.replace(old_id, new_id, key=key)
    assert response.status_code == 200, response.text
    body = response.json()

    with sync_session() as db:
        item = db.get(PlanItem, world.item_id)
        thread = db.get(CreationThread, world.thread_id)
        events = (
            db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == world.thread_id,
                    CreationThreadEvent.event_type == "media_replaced",
                )
            )
            .scalars()
            .all()
        )
        new_path = world.paths[new_id]
        row = item.clip_assignments[0]
        assert item.clip_gcs_paths == [new_path]
        assert len(item.clip_assignments) == 1
        assert (row["media_id"], row["gcs_path"], row["manifest_identity"]) == (
            new_id,
            new_path,
            new_id,
        )
        assert row["shot_id"] == "shot-0"
        # The old take's timeline never carries over.
        assert not {"trim_start_s", "trim_end_s", "speech_coverage"} & row.keys()
        assert [entry["media_id"] for entry in thread.state["media"]] == [new_id]
        assert thread.state["media_count"] == 1
        assert len(events) == 1
        swap = thread.state["source_replacement"]
        assert swap == {
            "replaced_media_id": old_id,
            "media_id": new_id,
            "superseded_job_id": str(world.job_id),
            "revision": events[0].revision,
            "speaker_media_ids": [new_id],
        }
        # The swap is one event; closing the stale approval afterwards is a second.
        assert events[0].revision == revision + 1
        assert thread.revision == revision + 2
        speaker = resolve_item_narration(item, detector_policy=current_detector_policy()).source
        assert speaker is not None and speaker.media_id == new_id
        # The old Job is not rewritten, and the old take is not deleted from storage.
        assert snapshot(world)["job"] == before["job"]
    assert object_store.deleted == []
    assert old_path in object_store.registry
    assert body["state"]["media"][0]["media_id"] == new_id
    assert body["state"]["source_replacement"]["media_id"] == new_id
    assert approval_state(approval_id) == ("cancelled", "source_replaced")
    with sync_session() as db:
        assert (
            db.execute(
                select(CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == world.thread_id,
                    CreationThreadEvent.event_type == "assistant_error",
                )
            )
            .scalars()
            .all()[-1]["code"]
            == "source_replaced"
        )

    # The planner: the old Job is no longer an editor target, so a follow-up routes to a
    # fresh strategy (a new Job) instead of editing the old take's cuts and captions.
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, world.item_id)
        target = await planner._load_editor_target(db, thread_id=world.thread_id, item=item)
        assert target is None
        assert planner._editor_target_miss.get() == "source_replaced"
        assert planner._editor_target_miss.get() not in planner._GUARDED_MISSES
        shape = plan_shape_from_editor_snapshot(None)
        assert route_requirements([], shape, message="make it again") == "replan"

    # Retry and double tap.
    after = snapshot(world)
    replay = await api.replace(old_id, new_id, key=key, revision=revision)
    assert replay.status_code == 200, replay.text
    assert snapshot(world) == after
    other = api.upload("c", takes["c"])
    reused = await api.replace(old_id, other, key=key, revision=revision)
    assert (reused.status_code, code(reused)) == (409, "idempotency_key_reused")
    double_tap = await api.replace(old_id, new_id, revision=revision)
    assert double_tap.status_code == 200, double_tap.text
    assert snapshot(world) == after
    stale = await api.replace(old_id, other, revision=revision)
    assert (stale.status_code, code(stale)) == (409, "revision_mismatch")
    gone = await api.replace(old_id, other)
    assert (gone.status_code, code(gone)) == (409, "retake_target_missing")
    assert snapshot(world) == after

    write_report(
        "retake",
        {
            "clip_media_ids_after": [r["media_id"] for r in snapshot(world)["assignments"]],
            "speaker_clips_before": 1,
            "speaker_clips_after": 1,
            "superseded_job": str(world.job_id),
            "approval": approval_state(approval_id),
            "storage_deletes": object_store.deleted,
        },
    )


@pytest.mark.asyncio
async def test_the_marker_is_what_sends_the_follow_up_to_a_fresh_plan(takes) -> None:
    """Negative control for the routing: only a marker naming the CURRENT Job flips the target."""
    from app.database import AsyncSessionLocal

    world = seed_world(takes)

    async def load():  # noqa: ANN202
        async with AsyncSessionLocal() as db:
            item = await db.get(PlanItem, world.item_id)
            target = await planner._load_editor_target(db, thread_id=world.thread_id, item=item)
            return target, planner._editor_target_miss.get()

    def mark(job_id: uuid.UUID) -> None:
        with sync_session() as db:
            thread = db.get(CreationThread, world.thread_id)
            thread.state = {
                **thread.state,
                "source_replacement": {
                    "replaced_media_id": "x",
                    "media_id": "y",
                    "superseded_job_id": str(job_id),
                    "revision": 3,
                },
            }
            db.commit()

    target, miss = await load()
    assert target is not None and miss is None, miss
    mark(uuid.uuid4())
    target, miss = await load()
    assert target is not None and miss is None, miss
    mark(world.job_id)
    target, miss = await load()
    assert target is None and miss == "source_replaced"


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True], ids=["unbound_draft", "brief_bound_draft"])
async def test_a_replay_never_closes_an_approval_made_after_the_swap(
    takes, object_store, monkeypatch, bound
) -> None:
    """The retired-approval replay is scoped: it closes the old take's card, not the new plan's."""
    if bound:
        for name in ("kria_creative_brief_enabled", "kria_brief_binding_enabled"):
            monkeypatch.setattr(settings, name, True)
        for name in ("kria_creative_brief_user_ids", "kria_brief_binding_user_ids"):
            monkeypatch.setattr(settings, name, [])
    world = seed_world(takes)
    api = _api(world, object_store)
    old_id = world.clips["a"]
    stale_approval = await pending_approval(world, monkeypatch)
    with sync_session() as db:
        draft = db.get(CreatorEditDraft, db.get(CreatorAgentApproval, stale_approval).draft_id)
        assert bool((draft.snapshot_json or {}).get("brief_binding")) is bound
    new_id = api.upload("b", takes["b"])
    key = f"ios-replace-{uuid.uuid4().hex[:10]}"
    revision = api.revision()
    assert (await api.replace(old_id, new_id, key=key)).status_code == 200
    assert approval_state(stale_approval) == ("cancelled", "source_replaced")

    fresh_approval = await pending_approval(world, monkeypatch)
    assert fresh_approval != stale_approval
    replay = await api.replace(old_id, new_id, key=key, revision=revision)
    assert replay.status_code == 200, replay.text
    assert approval_state(fresh_approval)[0] == "pending"


# --- render in flight: the negative control ---------------------------------------------------


def _set_job(world: World, *, status=None, variant_status=None, device_phase=None) -> None:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, world.job_id)
        plan = copy.deepcopy(job.assembly_plan)
        if variant_status is not None:
            plan["variants"][0]["render_status"] = variant_status
        if device_phase is not None:
            plan["_device_render_v1"] = {"original_text": {"status": {"phase": device_phase}}}
        job.assembly_plan = plan
        if status is not None:
            job.status = status
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("job_processing", {"status": "processing"}),
        ("job_awaiting_device", {"status": "awaiting_device"}),
        ("variant_reburning_on_a_ready_job", {"variant_status": "rendering"}),
        ("variant_pending_on_a_ready_job", {"variant_status": "pending"}),
        ("phone_export_syncing", {"device_phase": "syncing"}),
        ("phone_export_awaiting_device", {"device_phase": "awaiting_device"}),
    ],
)
async def test_a_render_in_flight_blocks_the_swap_and_changes_nothing(
    takes, object_store, label, change
) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    _set_job(world, **change)
    before = snapshot(world)

    response = await api.replace(world.clips["a"], new_id)

    assert (response.status_code, code(response)) == (409, "render_in_progress"), label
    assert snapshot(world) == before
    assert object_store.deleted == []


@pytest.mark.asyncio
async def test_the_same_swap_succeeds_once_the_render_has_settled(takes, object_store) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    _set_job(world, status="processing")
    assert (await api.replace(world.clips["a"], new_id)).status_code == 409
    _set_job(world, status="variants_ready")
    assert (await api.replace(world.clips["a"], new_id)).status_code == 200


# --- zero speakers ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_upload_that_never_finished_leaves_the_old_take_in_place(
    takes, object_store
) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    new_id = world.reserve("b", takes["b"])  # reserved, never PUT
    before = snapshot(world)

    response = await api.replace(world.clips["a"], new_id)

    assert (response.status_code, code(response)) == (409, "upload_not_finished")
    assert response.json()["problem"]["retryable"] is True
    assert snapshot(world) == before
    assert len(before["assignments"]) == 1


@pytest.mark.asyncio
async def test_a_take_with_no_audio_is_refused_and_the_old_take_stays(takes, object_store) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    new_id = api.upload("silent", takes["silent"])
    before = snapshot(world)

    response = await api.replace(world.clips["a"], new_id)

    assert (response.status_code, code(response)) == (422, "retake_has_no_audio")
    assert snapshot(world) == before


# --- wrong target / wrong item ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_targets_that_are_not_a_speaker_clip_are_refused(takes, object_store) -> None:
    world = seed_world(takes, voiceover=True, edit_format="narrated")
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    before = snapshot(world)

    # The recorded voiceover owns the speech, so no embedded clip is "the speaker".
    unsupported = await api.replace(world.clips["a"], new_id)
    assert (unsupported.status_code, code(unsupported)) == (422, "retake_unsupported_format")
    assert snapshot(world) == before

    talking = seed_world(takes, voiceover=False)
    api2 = _api(talking, object_store)
    new_two = api2.upload("b", takes["b"])
    with sync_session() as db:
        thread = db.get(CreationThread, talking.thread_id)
        thread.state = {
            **thread.state,
            "media": [
                *thread.state["media"],
                {"media_id": "voiceover.m4a", "kind": "audio"},
                {"media_id": "song.m4a", "kind": "audio", "role": "song"},
            ],
        }
        db.commit()
    for target in ("voiceover.m4a", "song.m4a", f"asset-{uuid.uuid4()}"):
        refused = await api2.replace(target, new_two)
        assert (refused.status_code, code(refused)) == (422, "retake_target_not_clip"), target
    missing = await api2.replace("analysis-proxy-never-attached.mp4", new_two)
    assert (missing.status_code, code(missing)) == (409, "retake_target_missing")


@pytest.mark.asyncio
async def test_a_montage_item_has_no_speaker_to_re_record(takes, object_store) -> None:
    world = seed_world(takes, edit_format="montage")
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    response = await api.replace(world.clips["a"], new_id)
    assert (response.status_code, code(response)) == (422, "retake_unsupported_format")


@pytest.mark.asyncio
async def test_an_image_or_library_pick_is_not_a_retake(takes, object_store) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    body = {
        "replaced_media_id": world.clips["a"],
        "media": {"media_id": "photo.jpg", "kind": "image"},
        "client_event_id": "k-image",
        "expected_revision": api.revision(),
    }
    refused = await api.post("/media/replace", body)
    assert (refused.status_code, code(refused)) == (422, "retake_kind_mismatch")
    body["media"] = {
        "media_id": "lib.mp4",
        "kind": "video",
        "library_asset_id": str(uuid.uuid4()),
    }
    body["client_event_id"] = "k-library"
    refused = await api.post("/media/replace", body)
    assert (refused.status_code, code(refused)) == (422, "retake_library_unsupported")


# --- two speakers (multi-clip Talking) -------------------------------------------------------


@pytest.fixture
def multi_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_rollout, "phone_talking_multi_clip_supported", lambda: True)


@pytest.mark.asyncio
async def test_multi_clip_swap_keeps_the_count_and_only_the_speaker_may_change(
    takes, object_store, multi_clip
) -> None:
    world = seed_world(takes, labels=("a", "c"), speakers=("a",))
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    before = snapshot(world)

    cutaway = await api.replace(world.clips["c"], new_id)
    assert (cutaway.status_code, code(cutaway)) == (409, "retake_target_not_speaker")
    assert snapshot(world) == before

    swapped = await api.replace(world.clips["a"], new_id)
    assert swapped.status_code == 200, swapped.text
    with sync_session() as db:
        item = db.get(PlanItem, world.item_id)
        assert [row["media_id"] for row in item.clip_assignments] == [new_id, world.clips["c"]]
        assert [row["shot_id"] for row in item.clip_assignments] == ["shot-0", "shot-1"]
        assert len(item.clip_gcs_paths) == 2
    write_report(
        "multi_clip",
        {
            "clips_before": 2,
            "clips_after": 2,
            "swapped_slot": 0,
            "refused": "retake_target_not_speaker",
        },
    )


@pytest.mark.asyncio
async def test_multi_clip_speaker_can_be_re_recorded_again_before_a_new_plan(
    takes, object_store, multi_clip
) -> None:
    """The Job's receipt still names the ORIGINAL speaker until a new plan renders, so the
    second and third swaps must follow the take the creator just recorded, not the receipt."""
    world = seed_world(takes, labels=("a", "c"), speakers=("a",))
    api = _api(world, object_store)
    second, third = api.upload("b", takes["b"]), api.upload("c2", takes["c"])
    fourth = api.upload("a2", takes["a"])

    first_swap = await api.replace(world.clips["a"], second)
    assert first_swap.status_code == 200, first_swap.text
    again = await api.replace(second, third)
    assert again.status_code == 200, again.text
    once_more = await api.replace(third, fourth)
    assert once_more.status_code == 200, once_more.text

    # Neither the replaced original nor the cutaway became the speaker by the swaps.
    stale = await api.replace(world.clips["c"], api.upload("b3", takes["b"]))
    assert (stale.status_code, code(stale)) == (409, "retake_target_not_speaker")
    with sync_session() as db:
        item = db.get(PlanItem, world.item_id)
        assert [row["media_id"] for row in item.clip_assignments] == [fourth, world.clips["c"]]


@pytest.mark.asyncio
async def test_multi_clip_without_a_speaker_receipt_is_refused(
    takes, object_store, multi_clip
) -> None:
    world = seed_world(takes, labels=("a", "c"), speakers=None)
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    before = snapshot(world)
    response = await api.replace(world.clips["a"], new_id)
    assert (response.status_code, code(response)) == (409, "retake_speaker_unknown")
    assert snapshot(world) == before


# --- the flag ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_hides_the_route_the_field_and_the_capability(
    takes, object_store, monkeypatch
) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    new_id = api.upload("b", takes["b"])
    thread = await _thread_json(api)
    assert thread["media_capabilities"]["clips"]["replace"] is True

    monkeypatch.setattr(settings, "media_replace_enabled", False)
    before = snapshot(world)
    response = await api.replace(world.clips["a"], new_id)
    assert (response.status_code, code(response)) == (404, "media_replace_unavailable")
    reserve = await api.post(
        "/upload-urls",
        {
            "files": [
                {
                    "filename": "x.mp4",
                    "content_type": "video/mp4",
                    "file_size_bytes": 10,
                    "client_upload_id": "ios-flag-off",
                    "upload_contract": _contract(takes["b"]),
                    "replaces_media_id": world.clips["a"],
                }
            ]
        },
    )
    assert (reserve.status_code, code(reserve)) == (404, "media_replace_unavailable")
    assert snapshot(world) == before
    # Reading the thread stamps a `generation` into its state on its own, so look last.
    assert "replace" not in (await _thread_json(api))["media_capabilities"]["clips"]


async def _thread_json(api: Api) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(api.url("?projection=full"))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_a_non_attached_id_earns_no_cap_exemption(takes, object_store) -> None:
    world = seed_world(takes)
    api = _api(world, object_store)
    body = {
        "files": [
            {
                "filename": "x.mp4",
                "content_type": "video/mp4",
                "file_size_bytes": 10,
                "client_upload_id": "ios-no-exemption",
                "upload_contract": _contract(takes["b"]),
                "replaces_media_id": "analysis-proxy-not-attached.mp4",
            }
        ]
    }
    refused = await api.post("/upload-urls", body)
    assert refused.status_code == 400, refused.text
