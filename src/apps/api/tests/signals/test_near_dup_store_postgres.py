"""The backfill's write path on a real PlanItem row (real Postgres, real fenced store).

Mocked sessions cannot show the two things that matter here: that the write goes through
``mutate_plan_item_media`` without disturbing footage identity or other analysis keys, and that
a second run finds nothing left to do.
"""

from __future__ import annotations

import shutil
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.cli import backfill_clip_signals as cli
from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, Persona, PlanItem, User
from app.services import clip_signals
from app.services.clip_signals import near_dup_hash as nd

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("test Postgres not reachable", allow_module_level=True)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")

UNDERSTANDING = {"understanding": {"summary": "a dog on a beach"}, "exposure": {"version": 1}}


def _seed(clips: list[dict]) -> uuid.UUID:
    user_id, item_id = uuid.uuid4(), uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        persona = Persona(user_id=user_id, persona_status="ready", persona={"tone": "direct"})
        db.add(persona)
        db.flush()
        plan = ContentPlan(user_id=user_id, persona_id=persona.id)
        db.add(plan)
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan.id,
                position=1,
                idea="beach day",
                item_status="awaiting_clips",
                clip_assignments=clips,
            )
        )
        db.commit()
    return item_id


def _rows(item_id: uuid.UUID) -> list[dict]:
    with sync_session() as db:
        return [dict(r) for r in db.get(PlanItem, item_id).clip_assignments]


class _FakeStorage:
    """GCS stand-in: one local file per path, generation 5 unless told otherwise."""

    def __init__(self, files: dict[str, str], generation: str = "5") -> None:
        self.files, self.generation = files, generation

    def object_metadata(self, path: str):
        size = 1000
        return type("M", (), {"generation": self.generation, "size": size})()

    def download_generation_to_file(self, path: str, local: str, *, generation: str) -> None:
        assert generation == self.generation
        shutil.copyfile(self.files[path], local)


@pytest.fixture
def media(tmp_path, monkeypatch):
    from tests.fixtures.clip_signals.dups import build  # noqa: PLC0415

    made = build.build(tmp_path / "media")
    by_id = {i["id"]: i["path"] for i in made["items"]}
    files = {"u/a.mp4": by_id["v_a"], "u/b.jpg": by_id["p_a"]}
    fake = _FakeStorage(files)
    monkeypatch.setattr("app.storage.object_metadata", fake.object_metadata)
    monkeypatch.setattr("app.storage.download_generation_to_file", fake.download_generation_to_file)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    return fake


def _clips() -> list[dict]:
    base = {"analysis": dict(UNDERSTANDING), "storage_generation": "5"}
    return [
        {"media_id": "m-video", "gcs_path": "u/a.mp4", "kind": "video", **base},
        {"media_id": "m-photo", "gcs_path": "u/b.jpg", "kind": "image", **base},
    ]


def test_backfill_writes_signals_beside_understanding_and_a_rerun_is_a_noop(media):
    item_id = _seed(_clips())
    before = _rows(item_id)

    report = cli.run(item_id=item_id, write=True)
    assert report["outcomes"] == {"stored": 2}, report

    after = _rows(item_id)
    for old, new in zip(before, after, strict=True):
        assert {k: v for k, v in new.items() if k != "analysis"} == {
            k: v for k, v in old.items() if k != "analysis"
        }
        assert new["analysis"]["understanding"] == old["analysis"]["understanding"]
        assert new["analysis"]["exposure"] == old["analysis"]["exposure"]
        assert nd.NAME in clip_signals.read_signals(new["analysis"])
    video = nd.dup_clip("m-video", "video", after[0]["analysis"])
    assert video is not None and len(video.hashes) == 3

    again = cli.run(item_id=item_id, write=True)
    assert again["clips_needing_signals"] == 0 and again["outcomes"] == {}


def test_object_replaced_while_measuring_is_not_stored(media):
    item_id = _seed(_clips())
    media.generation = "6"  # the object moved on after the row registered generation 5
    report = cli.run(item_id=item_id, write=True)
    assert report["outcomes"] == {"generation_changed": 2}
    assert all("signals" not in r["analysis"] for r in _rows(item_id))


def test_stale_store_after_measuring_never_overwrites_the_newer_clip(media):
    item_id = _seed(_clips())
    computed = {nd.NAME: {"hashes": ["00ff00ff00ff00ff"], "frames": 1}}
    # The clip at this path was replaced by generation 9 while the worker measured generation 5.
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        rows = [dict(r) for r in item.clip_assignments]
        rows[0]["storage_generation"] = "9"
        item.clip_assignments = rows
        db.commit()
    stored = cli.store_signals(
        item_id, media_id="m-video", gcs_path="u/a.mp4", generation="5", computed=computed
    )
    assert stored is False
    assert "signals" not in _rows(item_id)[0]["analysis"]


def test_dry_run_leaves_the_row_untouched(media):
    item_id = _seed(_clips())
    before = _rows(item_id)
    report = cli.run(item_id=item_id)
    assert report["mode"] == "dry_run" and report["clips_needing_signals"] == 2
    assert _rows(item_id) == before
