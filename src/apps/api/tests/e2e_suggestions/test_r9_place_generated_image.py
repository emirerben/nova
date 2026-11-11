"""R9-P (KRI-1253): the generated image the creator accepted is placed on a phone Talking edit.

The card's tap makes the image and the creator accepts it in the native review. Placement is the
other half: chat may place that image, and a Talking Save accepts it. Nothing else is placeable.

Failure catalogue (written before the code; each row is a test below):

  P1  the accepted image is the only photo chat is offered: ``overlays.asset_pool`` lists it, the
      parser takes ``add_overlay`` for its id and refuses any other id (a pool photo the creator
      uploaded, a generated image that was never accepted, an unknown id).
  P2  the card's path is the server's, never the model's: the compiled card points at the
      accepted image, whatever the op carries.
  P3  the Save accepts the placed image: the real prepare-commit pins it first
      (``pin_generated_overlays_for_commit_sync``), the recompile puts it on the recipe's overlay
      track, and the receipt is the exact stored generation.
  P4  a Save that posts any other new photo is still refused in words (an uploaded pool photo, a
      generated image that was not accepted).
  P5  the kill switch: with the card flag off nothing is offered, nothing is pinned, and the same
      Save is refused as it always was.
  P6  a Save of cards the job already pinned is untouched by the new pin step.
  P8  the model is told how to place an image: the prompt it is rendered lists each accepted
      image under the creator's title and teaches add_overlay instead of refusing it.
  P9  the op's own fields are honoured: "bottom" lands at the bottom, an exact spot beats a
      preset, and the effect bundle id reaches the card.
  P10 more accepted images than the pool holds drops the OLDEST, so the newest is still placeable
      and a Save that places it pins it.

Stand-ins: the image provider (the operation is completed with a real PNG through the real
admission path), the bucket (an in-memory object store), and the render worker.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app import storage
from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
from app.config import settings
from app.database import sync_session
from app.models import GeneratedMediaOperation, Job, PlanItemAsset
from app.routes import generative_jobs as gj
from app.services import generated_media, generated_overlay_pool
from app.services.device_render import device_status
from app.services.generated_asset_contracts import GeneratedAsset
from app.services.generated_media_admission import admit_generated_asset, persist_generated_asset
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    derived_phone_overlays,
)
from app.services.phone_sources import PHONE_VISUALS_FIELD
from tests.routes.test_phone_subtitled_editor_commit import phone_job
from tests.tasks import kria_pinned_suggestion_harness as h

STORED_GENERATION = "1700000000000077"


def _png(colour: str) -> GeneratedAsset:
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), colour).save(out, format="PNG")
    return GeneratedAsset(
        out.getvalue(), "image/png", "offline", {}, editable_title=f"{colour} card"
    )


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload(self, local, path, mime):  # noqa: ANN001, ANN201
        self.objects[path] = Path(local).read_bytes()
        return storage.ObjectMetadata(path, STORED_GENERATION, None, len(self.objects[path]), mime)

    def download(self, path, local, *, generation):  # noqa: ANN001, ANN201
        assert generation == STORED_GENERATION, (path, generation)
        Path(local).write_bytes(self.objects[path])

    def metadata(self, path):  # noqa: ANN001, ANN201
        return storage.ObjectMetadata(
            path, STORED_GENERATION, None, len(self.objects[path]), "image/png"
        )


@pytest.fixture
def bucket(monkeypatch):  # noqa: ANN001, ANN201
    store = Bucket()
    monkeypatch.setattr(storage, "upload_local_file_immutable", store.upload)
    monkeypatch.setattr(storage, "download_generation_to_file", store.download)
    monkeypatch.setattr(storage, "object_metadata", store.metadata)
    return store


@pytest.fixture
def talking(monkeypatch, bucket):  # noqa: ANN001, ANN201, ARG001
    """A rendered phone Talking edit on a real thread, with the card's flag on."""
    phone = phone_job(monkeypatch)
    plan = json.loads(json.dumps(phone.assembly_plan))
    plan["variants"][0]["video_path"] = "generative-jobs/talking/output.mp4"
    seeded = h.seed_ready_thread(plan["variants"][0])
    plan = json.loads(
        json.dumps(plan)
        .replace("users/owner/plan/item/", f"users/{seeded.user_id}/plan/{seeded.item_id}/")
        .replace(str(phone.id), str(seeded.job_id))
    )
    with sync_session() as db:
        job = db.get(Job, seeded.job_id)
        job.assembly_plan = plan
        job.status = "awaiting_device"
        db.commit()
    monkeypatch.setattr(settings, "kria_suggestion_generated_overlay_enabled", True)
    return seeded


def make_image(seeded, colour: str, *, accepted: bool) -> uuid.UUID:  # noqa: ANN001
    """One generated image through the real admission path, accepted by the real accept call."""
    with sync_session() as db:
        operation = GeneratedMediaOperation(
            user_id=seeded.user_id,
            plan_item_id=seeded.item_id,
            client_event_id=str(uuid.uuid4()),
            request_digest=uuid.uuid4().hex * 2,
            capability="image",
            state="submitting",
        )
        db.add(operation)
        db.flush()
        owner = {"owner_id": seeded.user_id, "plan_item_id": seeded.item_id}
        persist_generated_asset(db, operation, _png(colour), **owner)
        admit_generated_asset(db, operation, **owner)
        db.commit()
        asset_id, operation_id = operation.asset_id, operation.id
    if accepted:
        with sync_session() as db:
            generated_media.accept_generated_media(
                db,
                operation_id,
                owner_id=seeded.user_id,
                item_id=seeded.item_id,
                body=generated_media.GeneratedMediaAccept(),
            )
    return asset_id


def card(asset_id, path, **extra):  # noqa: ANN001, ANN201, ANN003
    return {
        "id": uuid.uuid4().hex,
        "kind": "image",
        "src_gcs_path": path,
        "display_mode": "pip",
        "x_frac": 0.74,
        "y_frac": 0.22,
        "scale": 0.36,
        "start_s": 4.0,
        "end_s": 6.0,
        **extra,
    }


def path_of(asset_id) -> str:  # noqa: ANN001
    with sync_session() as db:
        return db.get(PlanItemAsset, asset_id).gcs_path


def load_job(seeded):  # noqa: ANN001, ANN201
    with sync_session() as db:
        job = db.get(Job, seeded.job_id)
        db.expunge(job)
    return job


def projected_variant(job):  # noqa: ANN001, ANN201
    variant = dict(job.assembly_plan["variants"][0])
    variant["media_overlays"] = derived_phone_overlays(job, variant) or []
    return variant


def pool_rows(seeded):  # noqa: ANN001, ANN201
    with sync_session() as db:
        return generated_overlay_pool.load_pool_sync(db, seeded.item_id, seeded.user_id)


def snapshot_with(job, rows):  # noqa: ANN001, ANN201
    return build_editor_snapshot(
        job, projected_variant(job), clip_context={"generated_overlay_pool": rows}
    )


def parse(op, snapshot):  # noqa: ANN001, ANN201
    return parse_editor_operation(dict(op), dict(snapshot), EditorOperationParseState(1.0))


def add_op(asset_id, **extra):  # noqa: ANN001, ANN201, ANN003
    return {"op": "add_overlay", "asset_id": str(asset_id), "start_s": 4.0, "end_s": 6.0, **extra}


def commit(job, seeded, **sections):  # noqa: ANN001, ANN201, ANN003
    request = gj.EditorCommitRequest(base_generation="first", **sections)
    with sync_session() as db:
        from app.services.phone_speaker_background import (  # noqa: PLC0415
            pin_background_for_commit_sync,
        )

        pin_background_for_commit_sync(db, job, "subtitled", request, plan_item_id=seeded.item_id)
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        request,
        user_id=str(seeded.user_id),
        plan_item_id=str(seeded.item_id),
    )


def overlay_clips(job) -> int:  # noqa: ANN001
    request = device_status(job, "subtitled").request
    track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    return len(track.clips)


def manifest_text(job) -> str:  # noqa: ANN001
    recipe = device_status(job, "subtitled").request.recipe
    return json.dumps(recipe.asset_manifest.model_dump(mode="json"))


# ── P1: the accepted image is the only photo chat is offered ─────────────────────────


def test_p1_only_an_accepted_generated_image_is_offered_and_placeable(talking):  # noqa: ANN001
    accepted = make_image(talking, "red", accepted=True)
    unaccepted = make_image(talking, "blue", accepted=False)
    job = load_job(talking)

    rows = pool_rows(talking)
    assert [row["id"] for row in rows] == [str(accepted)], "an image nobody accepted is not offered"
    snapshot = snapshot_with(job, rows)
    assert snapshot["overlays"]["asset_pool"] == [
        {"id": str(accepted), "kind": "image", "subject": "red card"}
    ]
    assert "gcs_path" not in json.dumps(snapshot), "the model sees ids, never storage paths"

    assert parse(add_op(accepted), snapshot) is not None
    for refused in (unaccepted, uuid.uuid4(), "photo-1"):
        assert parse(add_op(refused), snapshot) is None, refused


# ── P2/P3: placed by the server's path, accepted by a real Save ──────────────────────


def test_p2_p3_chat_places_the_accepted_image_and_the_save_accepts_it(talking):  # noqa: ANN001
    accepted = make_image(talking, "red", accepted=True)
    job = load_job(talking)
    rows = pool_rows(talking)
    parsed = parse(
        add_op(accepted, src_gcs_path="users/evil/plan/x/pool/other.png"), snapshot_with(job, rows)
    )
    assert parsed is not None and "src_gcs_path" not in parsed, "the model has no say in the path"

    compiled = compile_editor_ops(
        job,
        projected_variant(job),
        [parsed],
        pool_assets=generated_overlay_pool.paths_by_asset_id(rows),
    )
    placed = [c for c in compiled.payload.media_overlays if c["src_gcs_path"] == path_of(accepted)]
    assert len(placed) == 1, "the card points at the accepted image"
    before = overlay_clips(job)

    assert pinned_ids(job) == {"photo-1"}, "the take's own photo is the only pin before the Save"
    prep = commit(job, talking, media_overlays=list(compiled.payload.media_overlays))
    assert prep["render_destination"] == "device"

    assert str(accepted) in pinned_ids(job), "the Save pinned the accepted image first"
    receipt = next(
        r for r in job.assembly_plan[PHONE_VISUALS_FIELD] if r["media_id"] == str(accepted)
    )
    assert receipt["generation"] == STORED_GENERATION
    assert overlay_clips(job) == before + 1, "the recompiled recipe carries the new picture card"
    assert str(accepted) in manifest_text(job), "and its pinned bytes are in the render manifest"


def pinned_ids(job) -> set[str]:  # noqa: ANN001
    return {row["media_id"] for row in job.assembly_plan.get(PHONE_VISUALS_FIELD) or []}


# ── P4: any other new photo is still refused ─────────────────────────────────────────


def test_p4_a_save_of_any_other_new_photo_is_still_refused(talking):  # noqa: ANN001
    unaccepted = make_image(talking, "blue", accepted=False)
    job = load_job(talking)
    for path in (
        path_of(unaccepted),
        f"users/{talking.user_id}/plan/{talking.item_id}/pool/up.jpg",
    ):
        with pytest.raises(HTTPException) as error:
            commit(job, talking, media_overlays=[card(None, path)])
        assert error.value.status_code == 422
        assert "isn't a photo Kria pinned" in error.value.detail["reason"]
        assert pinned_ids(job) == {"photo-1"}, "a refused Save pins nothing"


# ── P5: the kill switch ───────────────────────────────────────────────────────────────


def test_p5_flag_off_offers_nothing_pins_nothing_and_refuses_the_save(talking, monkeypatch):  # noqa: ANN001
    accepted = make_image(talking, "red", accepted=True)
    monkeypatch.setattr(settings, "kria_suggestion_generated_overlay_enabled", False)
    job = load_job(talking)
    assert not generated_overlay_pool.enabled_for(job.assembly_plan["variants"][0])
    snapshot = snapshot_with(job, [])
    assert snapshot["overlays"]["asset_pool"] == []
    assert parse(add_op(accepted), snapshot) is None
    with pytest.raises(HTTPException) as error:
        commit(job, talking, media_overlays=[card(accepted, path_of(accepted))])
    assert "isn't a photo Kria pinned" in error.value.detail["reason"]
    assert pinned_ids(job) == {"photo-1"}


# ── P6: a Save of already-pinned cards does not touch the pool ───────────────────────


def test_p6_a_save_of_pinned_cards_never_reads_the_pool(talking, monkeypatch):  # noqa: ANN001
    make_image(talking, "red", accepted=True)
    job = load_job(talking)
    monkeypatch.setattr(
        generated_overlay_pool,
        "load_pool_sync",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the pool was read")),
    )
    pinned_path = job.assembly_plan[PHONE_VISUALS_FIELD][0]["gcs_path"]
    commit(job, talking, media_overlays=[card(None, pinned_path)])
    assert pinned_ids(job) == {"photo-1"}


def test_the_accepted_image_row_is_the_one_the_pool_lists(talking):  # noqa: ANN001
    accepted = make_image(talking, "red", accepted=True)
    with sync_session() as db:
        operation = db.scalar(
            select(GeneratedMediaOperation).where(GeneratedMediaOperation.asset_id == accepted)
        )
        assert operation.request_metadata.get("accepted") is True


# ── P8: the prompt the model reads ──────────────────────────────────────────────────


def test_p8_the_rendered_prompt_names_each_image_and_teaches_add_overlay(talking):  # noqa: ANN001
    from app.agents import edit_copilot
    from app.agents.editor_ops_v2 import family_fragments

    make_image(talking, "red", accepted=True)
    make_image(talking, "blue", accepted=True)
    snapshot = snapshot_with(load_job(talking), pool_rows(talking))

    text = edit_copilot._format_snapshot(snapshot)
    pool_lines = [
        line for line in text.splitlines() if line.startswith("- id=") and "kind='image'" in line
    ]
    assert sorted(line.split("subject=")[1] for line in pool_lines) == ["'blue card'", "'red card'"]
    assert not any("unknown" in line for line in pool_lines)

    lane = family_fragments(snapshot)
    assert '"op":"add_overlay"' in lane
    assert "(no ASSET POOL)" not in lane, "the lane must not tell the model to refuse a pool row"


# ── P9: add_overlay honours position and the bundle id ───────────────────────────────


def placed_card(talking, op):  # noqa: ANN001, ANN201
    accepted = make_image(talking, "red", accepted=True)
    job = load_job(talking)
    rows = pool_rows(talking)
    parsed = parse(add_op(accepted, **op), snapshot_with(job, rows))
    assert parsed is not None
    compiled = compile_editor_ops(
        job,
        projected_variant(job),
        [parsed],
        pool_assets=generated_overlay_pool.paths_by_asset_id(rows),
    )
    return next(
        c for c in compiled.payload.media_overlays if c["src_gcs_path"] == path_of(accepted)
    )


def test_p9_position_preset_effect_bundle_and_exact_spot(talking):  # noqa: ANN001
    bottom = placed_card(talking, {"position": "bottom", "effect_bundle_id": "reveal_1"})
    assert (bottom["x_frac"], bottom["y_frac"]) == (0.5, 0.82)
    assert bottom["effect_group_id"] == "reveal_1"

    exact = placed_card(talking, {"position": "bottom", "y_frac": 0.3})
    assert exact["y_frac"] == 0.3, "an exact spot beats the preset"

    default = placed_card(talking, {})
    assert (default["x_frac"], default["y_frac"]) == (0.74, 0.22)
    assert "effect_group_id" not in default


# ── P10: the newest image survives the pool's size limit ─────────────────────────────


def test_p10_the_oldest_image_is_the_one_the_pool_drops(talking):  # noqa: ANN001
    made = [
        make_image(talking, f"#{n:02x}{n:02x}{n:02x}", accepted=True)
        for n in range(1, generated_overlay_pool.MAX_POOL_ROWS + 2)
    ]
    ids = [row["id"] for row in pool_rows(talking)]
    assert len(ids) == generated_overlay_pool.MAX_POOL_ROWS
    assert str(made[-1]) in ids and str(made[0]) not in ids

    job = load_job(talking)
    card_row = card(made[-1], path_of(made[-1]))
    prep = commit(job, talking, media_overlays=[card_row])
    assert prep["render_destination"] == "device"
    assert str(made[-1]) in pinned_ids(job)


# ── P7: the real chat turn compiles the card from the server's rows ──────────────────


@pytest.mark.asyncio
async def test_p7_a_chat_turn_stages_the_accepted_image_as_a_draft_card(
    world, talking, monkeypatch
):  # noqa: ANN001
    from unittest.mock import AsyncMock

    from app.kria import planner
    from app.kria.planner import PlannedKriaTurn

    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities", gj._editor_capabilities
    )
    accepted = make_image(talking, "red", accepted=True)
    job = load_job(talking)
    parsed = parse(add_op(accepted), snapshot_with(job, pool_rows(talking)))
    assert parsed is not None
    plan = planner.adapt_editor_action(reply="Placed the picture.", ops=[parsed])
    stub = AsyncMock(
        return_value=PlannedKriaTurn(plan=plan, manifest_hash="b" * 64, context_hash="a" * 64)
    )
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", stub)

    accepted_turn, _publish = await h.submit(talking, "Put the generated picture on screen")
    await h.run(accepted_turn.turn_id)

    stored = h.drafts(talking.thread_id)
    assert stored, "the turn staged a draft"
    cards = (stored[-1]["snap"].get("editor_payload") or {}).get("media_overlays") or []
    assert [c["src_gcs_path"] for c in cards if c["src_gcs_path"] == path_of(accepted)], cards
