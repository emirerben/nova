"""KRI-1340 on real Postgres: S3 Yorum1's setup (take A footage, takes B and C in Visuals)
ends with the Yorum2 edit.

Prod thread 855274e8 (2026-11-08): "There are 3 videos, two at home and one in the car, join
them in that order." Take A was footage; takes B (IMG_7245) and C (IMG_7252) were Visuals. The
turn held every ask on "I couldn't tell which clip "two at home" means". Here the real adoption
task runs on real files (ffmpeg-made stand-ins with the takes' own lengths, real ffprobe and
proxy encode, local storage) and the item's real rows; the takes' stored analysis is the one
Gemini wrote for the same three files (`s3_yorum2_talking_order.json`). No model is called.

Failure modes pinned here (full list: `docs/reviews/kri-1340-visual-talking-takes.md`):
 2  a video where no one talks to the camera stays a Visual;   3  a copy of a footage take is
 not adopted twice;   4  photos never;   9  the proxy keeps the original's timeline;
 10  switch off / not Talking = nothing changes;   12  a second run adds nothing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from app import storage
from app.config import settings
from app.kria.approval_readiness import assess_readiness
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import snapshot_media
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.services.phone_rollout import PHONE_TALKING_HEAD_FEATURES
from app.services.phone_sources import bind_phone_sources
from app.services.visual_talking_takes import adopted_asset_ids
from app.tasks import visual_talking_takes as task

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "s3_yorum2_talking_order.json").read_text()
)
ANALYSIS = {row["media_id"]: row for row in CASE["snapshot"]["clip_assignments"]}
A_ID, B_ID, C_ID = (CASE["clips"][k] for k in "ABC")
SECONDS = {"A": 20.1, "B": 6.99, "C": 7.15}


def _video(path: Path, seconds: float, *, audio: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size=540x960:rate=30:duration={seconds}",
            *(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"] if audio else []),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            *(["-c:a", "aac", "-shortest"] if audio else []),
            str(path),
        ],
        check=True,
    )  # fmt: skip


@pytest.fixture
def env(monkeypatch, tmp_path, prod_profile):
    from app.database import sync_session
    from app.models import ContentPlan, CreationThread, Persona, PlanItem, PlanItemAsset, User

    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path / "bucket"))
    monkeypatch.setattr(settings, "local_storage_base_url", "http://local.test")
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)
    monkeypatch.setattr(settings, "visual_talking_takes_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(
            dict.fromkeys([*settings.phone_render_verified_features, *PHONE_TALKING_HEAD_FEATURES])
        ),
    )
    monkeypatch.setattr(task, "_retire_pending_approvals", lambda *a, **k: None)
    from app.tasks import kria_clip_understanding

    # Whisper's timed sentences are footage's own best-effort pass; no network here.
    monkeypatch.setattr(kria_clip_understanding, "_with_speech_segments", lambda row, **_k: row)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)

    user_id, persona_id, plan_id, item_id, thread_id = (uuid.uuid4() for _ in range(5))
    pool = f"users/{user_id}/plan/{item_id}/pool"
    thread_dir = f"users/{user_id}/creation-threads/{thread_id}"

    def put(object_path: str, seconds: float, *, audio: bool = True) -> tuple[str, str]:
        _video(storage.local_object_path(object_path), seconds, audio=audio)
        meta = storage.object_metadata(object_path)
        return meta.generation, hashlib.sha256(
            storage.local_object_path(object_path).read_bytes()
        ).hexdigest()

    # Take A: footage, as the phone attached it (proxy + original receipt).
    a_proxy = f"{thread_dir}/{A_ID}"
    a_generation, _ = put(a_proxy, SECONDS["A"])
    a_original_sha = "d6788e0ab2ff8bd0f35bac5c64489d5276ac5d8f8c2fa151493e94eacfd139a5"
    take_a = {
        "gcs_path": a_proxy,
        "media_id": A_ID,
        "kind": "video",
        "shot_id": None,
        "storage_generation": a_generation,
        "duration_s": SECONDS["A"],
        "has_audio": True,
        "manifest_identity": A_ID,
        "capture": ANALYSIS[A_ID]["capture"],
        "source_filename": ANALYSIS[A_ID]["source_filename"],
        "analysis": copy.deepcopy(ANALYSIS[A_ID]["analysis"]),
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "timing_version": 1,
                "duration_s": SECONDS["A"],
                "width": 320,
                "height": 568,
                "frame_rate": 30.0,
                "orientation_degrees": 0,
                "original": {
                    "kind": "video",
                    "sha256": a_original_sha,
                    "byte_count": 8811753,
                    "duration_s": SECONDS["A"],
                    "width": 1080,
                    "height": 1920,
                    "orientation_degrees": 0,
                    "has_audio": True,
                },
            },
        },
    }
    # Visuals, in Yorum1's upload order: take C, take B, then the photos; plus two controls.
    visuals: dict[str, uuid.UUID] = {}
    rows = []
    for name, media_id, seconds in (("C", C_ID, SECONDS["C"]), ("B", B_ID, SECONDS["B"])):
        asset_id = uuid.uuid4()
        path = f"{pool}/{asset_id}.mov"
        generation, _ = put(path, seconds)
        source = ANALYSIS[media_id]
        rows.append(
            dict(
                id=asset_id,
                kind="video",
                gcs_path=path,
                gcs_generation=generation,
                duration_s=seconds,
                analysis=copy.deepcopy(source["analysis"]),
                capture=source["capture"],
                source_filename=source["source_filename"],
            )
        )
        visuals[name] = asset_id
    broll_id = uuid.uuid4()  # a street shot: speech in the background, nobody to camera
    broll_path = f"{pool}/{broll_id}.mov"
    generation, _ = put(broll_path, 5.0)
    rows.append(
        dict(
            id=broll_id,
            kind="video",
            gcs_path=broll_path,
            gcs_generation=generation,
            duration_s=5.0,
            analysis={
                "transcript": "people chatting at the next table about the weather today",
                "understanding": {
                    "speech": {"has_speech": True, "to_camera": False, "transcript": "chatting"}
                },
            },
            source_filename="IMG_7300.MOV",
        )
    )
    visuals["broll"] = broll_id
    photo_id = uuid.uuid4()
    rows.append(
        dict(
            id=photo_id,
            kind="image",
            gcs_path=f"{pool}/{photo_id}.jpg",
            gcs_generation="1",
            analysis={"understanding": {"speech": {"to_camera": True, "transcript": "x " * 9}}},
            source_filename="IMG_7150.JPG",
        )
    )
    visuals["photo"] = photo_id

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
                idea="Yorumla episode 10",
                item_status="awaiting_clips",
                edit_format="subtitled",
                clip_assignments=[take_a],
                clip_gcs_paths=[a_proxy],
            )
        )
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                runtime_version=2,
                state={"edit_format": "subtitled"},
            )
        )
        for row in rows:
            db.add(PlanItemAsset(plan_item_id=item_id, user_id=user_id, status="ready", **row))
        db.commit()
    return {"item_id": item_id, "visuals": visuals}


def _item(item_id):
    from app.database import sync_session
    from app.models import PlanItem

    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        db.expunge(item)
        return item


def _yorum1_brief() -> CreativeBrief:
    data = copy.deepcopy(CASE["brief"])
    data["requirements"][0]["facts"] = {
        "key": "capture_time",
        "first_clip": "two at home",
        "last_clip": "one in the car",
    }
    data["requirements"][0]["description"] = (
        "There are 3 videos, two at home and one in the car, join them in that order."
    )
    return CreativeBrief.model_validate(data)


@pytest.mark.usefixtures("env")
def test_yorum1_takes_in_visuals_end_as_the_yorum2_edit(env) -> None:
    visuals = env["visuals"]
    outcomes = {name: task._adopt(asset_id) for name, asset_id in visuals.items()}

    assert outcomes == {
        "C": "adopted",
        "B": "adopted",
        "broll": "not_to_camera",
        "photo": "not_video",
    }
    item = _item(env["item_id"])
    rows = item.clip_assignments
    assert adopted_asset_ids(rows) == {str(visuals["C"]), str(visuals["B"])}
    assert [row["media_id"] for row in rows][0] == A_ID
    assert len(item.clip_gcs_paths) == 3

    # The phone job binds every take: footage from the camera roll, B and C from the pool.
    bindings = bind_phone_sources(rows, [row["gcs_path"] for row in rows])
    by_asset = {b.pool_source.asset_id: b for b in bindings if b.pool_source}
    for name in ("B", "C"):
        binding = by_asset[str(visuals[name])]
        pool_bytes = storage.local_object_path(binding.pool_source.gcs_path).read_bytes()
        assert binding.original.sha256 == hashlib.sha256(pool_bytes).hexdigest()
        assert binding.render_asset().kind == "visual"
        proxy = next(r for r in rows if r["media_id"] == binding.media_id)["upload_contract"]
        assert max(proxy["proxy"]["width"], proxy["proxy"]["height"]) <= 568
        assert abs(proxy["proxy"]["duration_s"] - SECONDS[name]) <= 0.1

    # Yorum1's own words now seat the takes: the two home takes, then the car take.
    snapshot = snapshot_media(item)
    # Yorum2's approved strategy, over this item's own takes.
    strategy = {
        **CASE["strategy"],
        "edit_format": "subtitled",
        "selected_media_ids": [row["media_id"] for row in rows],
    }
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=_yorum1_brief(),
        media_snapshot=snapshot,
    )
    b_take = by_asset[str(visuals["B"])].media_id
    c_take = by_asset[str(visuals["C"])].media_id
    assert contract.unresolved == ()
    assert contract.order_ids == (A_ID, b_take, c_take)

    manifest = resolve_creator_manifest(
        item_id=str(item.id),
        edit_format="subtitled",
        media=[
            {"media_id": r["media_id"], "kind": "video", "duration_s": r["duration_s"]}
            for r in rows
        ],
    )
    report = assess_readiness(_yorum1_brief(), strategy, media=snapshot, manifest=manifest)
    assert report["policy_problem"] is None
    assert not any(
        "couldn't tell which clip" in str(r.get("reason")) for r in report["requirements"]
    )
    # "café" is take C's word: the take the phone now hears.
    heard = " ".join(
        str(r["analysis"]["understanding"]["speech"]["transcript"]) for r in rows
    ).lower()
    assert "cafes" in heard

    # Run again (a redelivered task): nothing more is added.
    assert task._adopt(visuals["B"]) == "already_adopted"
    assert len(_item(env["item_id"]).clip_assignments) == 3


@pytest.mark.usefixtures("env")
def test_switch_off_or_not_talking_changes_nothing(env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "visual_talking_takes_enabled", False)
    assert task._adopt(env["visuals"]["B"]) == "switch_off"

    monkeypatch.setattr(settings, "visual_talking_takes_enabled", True)
    from app.database import sync_session
    from app.models import PlanItem

    with sync_session() as db:
        db.get(PlanItem, env["item_id"]).edit_format = "montage"
        db.commit()
    assert task._adopt(env["visuals"]["B"]) == "not_talking_format"
    assert len(_item(env["item_id"]).clip_assignments) == 1


@pytest.mark.usefixtures("env")
def test_a_copy_of_a_footage_take_is_not_adopted_twice(env, monkeypatch) -> None:
    from app.database import sync_session
    from app.models import PlanItem

    asset_id = env["visuals"]["B"]
    with sync_session() as db:
        from app.models import PlanItemAsset

        asset = db.get(PlanItemAsset, asset_id)
        sha = hashlib.sha256(storage.local_object_path(asset.gcs_path).read_bytes()).hexdigest()
        item = db.get(PlanItem, env["item_id"])
        rows = copy.deepcopy(item.clip_assignments)
        rows[0]["upload_contract"]["proxy"]["original"]["sha256"] = sha
        item.clip_assignments = rows
        db.commit()

    assert task._adopt(asset_id) == "duplicate"
    assert len(_item(env["item_id"]).clip_assignments) == 1
