"""Deterministic production phone preflight over transactional actor-owned sources."""

import copy
import uuid

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import CreativeBrief
from app.kria.order_replacement import (
    CheckedOrderMoment,
    OrderReplacementProposal,
    phone_binding_digest,
    source_fence_digest,
    validate_confirmation,
    verify_compiled_order,
)
from app.kria.order_replacement_receipts import compiled_order_preflight_sync
from app.kria.order_replacement_sources import load_order_replacement_source_context_sync
from app.kria.prepared_execution import SourceIdentity, load_source_identities_sync
from app.models import PlanItemAsset
from app.pipeline.guided_story import compile_execution_plan
from app.services.phone_montage_preflight import _ready_visuals, preflight_montage_plan
from app.services.phone_sources import bind_phone_sources
from tests.kria.test_kri775_merope_m2_replay import prod_flags
from tests.kria.test_order_replacement import context, proposal, question
from tests.kria.test_order_replacement_receipts import requirement
from tests.kria.test_order_replacement_sources import stored as stored
from tests.services.test_phone_sources import receipt


@pytest.fixture
def phone_preflight(stored, monkeypatch):
    prod_flags(monkeypatch)
    db, owner, item = stored
    assignment = {**receipt("take"), "kind": "video"}
    item.clip_assignments = [assignment]
    item.clip_gcs_paths = [assignment["gcs_path"]]
    item.edit_format = "montage"
    db.flush()
    media = {
        "clip_paths": list(item.clip_gcs_paths),
        "clip_assignments": list(item.clip_assignments),
    }
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        audio_strategy="original_audio",
        media_scope="all",
    ).model_dump(mode="json")
    brief = CreativeBrief(version=3, requirements=[requirement(req_id="r2")])
    planned = preflight_montage_plan(
        creator_id=owner.id,
        edit_format=item.edit_format,
        brief=brief,
        strategy=strategy,
        media_snapshot=media,
        visuals=lambda: _ready_visuals(db, item=item, user_id=owner.id, strategy=strategy),
    )
    assert isinstance(planned, tuple), planned
    offered = compile_execution_plan(planned[0].guided_edit(), track=None)
    sources = load_source_identities_sync(db, item, owner.id)
    bindings = bind_phone_sources(item.clip_assignments, item.clip_gcs_paths)
    assert len(sources) == 1
    source = sources[0]
    moments = tuple(
        CheckedOrderMoment(
            occurrence_id=row["moment_id"],
            intent_id=f"remaining-{i}",
            label="Checked remaining moment",
            source_slot=source.slot,
            source_fence_digest=source_fence_digest(source),
            phone_binding_digest=phone_binding_digest(bindings[0]),
            media_id=row["media_id"],
            kind=row["kind"],
            gcs_path=row["gcs_path"],
            generation=row["generation"],
            source_start_s=row["source_start_s"],
            source_end_s=row["source_end_s"],
            evidence_origin="creator_answer",
            evidence_ref=f"checked-{i}",
            output_start_s=row["output_start_s"],
            output_end_s=row["output_end_s"],
            duration_s=row["duration_s"],
            playback_rate=row.get("playback_rate") or 1.0,
            freeze_hold_s=row.get("freeze_hold_s") or 0.0,
        )
        for i, row in enumerate(offered["story_timeline"])
    )
    p = OrderReplacementProposal.model_validate(
        {**proposal().model_dump(mode="python"), "moments": moments}
    )
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **{**context(p), "sources": sources, "phone_bindings": bindings},
    )
    strategy["confirmed_order_replacement"] = proof.model_dump(mode="json")
    brief = CreativeBrief(version=4, requirements=[requirement(proof)])
    return (
        db,
        {
            "item": item,
            "creator_id": owner.id,
            "brief": brief,
            "strategy": strategy,
            "media_snapshot": media,
            "client_protocol": None,
        },
        offered,
    )


def test_real_phone_preflight_returns_same_full_compiler_artifact(phone_preflight):
    db, args, offered = phone_preflight
    actual = compiled_order_preflight_sync(db, **args)
    assert actual is not None
    assert actual["story_timeline"] == offered["story_timeline"]


@pytest.mark.parametrize(
    "change", ["missing_media", "conflict", "other_writer", "missing_proof", "source_generation"]
)
def test_unknown_or_conflicting_inputs_return_no_artifact(phone_preflight, change):
    db, original, _ = phone_preflight
    args = dict(original)
    if change == "missing_media":
        args["media_snapshot"] = None
    elif change in {"conflict", "other_writer"}:
        args["media_snapshot"] = copy.deepcopy(args["media_snapshot"])
        args["media_snapshot"].update(
            {"length_plan": {"conflicts": ["open"]}}
            if change == "conflict"
            else {"voiceover_path": "voice.wav"}
        )
    elif change == "missing_proof":
        args["strategy"] = dict(args["strategy"])
        args["strategy"].pop("confirmed_order_replacement")
    else:
        assignments = copy.deepcopy(args["item"].clip_assignments)
        assignments[0]["storage_generation"] = "changed-generation"
        args["item"].clip_assignments = assignments
        db.flush()
    assert compiled_order_preflight_sync(db, **args) is None


def test_absent_canonical_proof_never_reads_database():
    assert (
        compiled_order_preflight_sync(
            object(),
            item=object(),
            creator_id=None,
            brief=None,
            strategy={},
            media_snapshot=None,
            client_protocol=None,
        )
        is None
    )


def test_unexpected_database_programming_error_is_not_swallowed(phone_preflight):
    _, args, _ = phone_preflight
    with pytest.raises(AttributeError):
        compiled_order_preflight_sync(object(), **args)


def test_expected_too_short_worker_plan_leaves_replacement_unresolved(phone_preflight):
    db, original, _ = phone_preflight
    args = dict(original)
    args["media_snapshot"] = copy.deepcopy(original["media_snapshot"])
    assignment = args["media_snapshot"]["clip_assignments"][0]
    assignment["duration_s"] = 0.1
    assignment["upload_contract"]["proxy"]["duration_s"] = 0.1
    assignment["upload_contract"]["proxy"]["original"]["duration_s"] = 0.1
    assert compiled_order_preflight_sync(db, **args) is None


@pytest.fixture
def mixed_visual_preflight(phone_preflight):
    db, initial, _ = phone_preflight
    item, owner_id = initial["item"], initial["creator_id"]
    asset = PlanItemAsset(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        user_id=owner_id,
        gcs_path=f"visuals/{uuid.uuid4()}.jpg",
        gcs_generation="photo-generation",
        kind="image",
        status="ready",
        media_status="ready",
    )
    db.add(asset)
    db.flush()
    strategy = dict(initial["strategy"])
    strategy.pop("confirmed_order_replacement")
    strategy.update(media_scope="selected", selected_media_ids=["take", f"asset-{asset.id}"])
    brief = CreativeBrief(version=3, requirements=[requirement(req_id="r2")])
    visuals = _ready_visuals(db, item=item, user_id=owner_id, strategy=strategy)
    assert [row.media_id for row in visuals] == [str(asset.id)]
    planned = preflight_montage_plan(
        creator_id=owner_id,
        edit_format=item.edit_format,
        brief=brief,
        strategy=strategy,
        media_snapshot=initial["media_snapshot"],
        visuals=visuals,
    )
    assert isinstance(planned, tuple), planned
    offered = compile_execution_plan(planned[0].guided_edit(), track=None)
    assert {row["kind"] for row in offered["story_timeline"]} == {"video", "image"}
    sources = load_source_identities_sync(db, item, owner_id)
    visual_source = SourceIdentity(
        slot=f"visual:{asset.id}",
        media_id=f"asset-{asset.id}",
        path=asset.gcs_path,
        generation=asset.gcs_generation,
        role="original",
    )
    bindings = bind_phone_sources(item.clip_assignments, item.clip_gcs_paths)
    moments = []
    for index, row in enumerate(offered["story_timeline"]):
        is_photo = row["kind"] == "image"
        source = visual_source if is_photo else sources[0]
        if is_photo:
            assert row["media_id"] == str(asset.id)
            assert row["lane"] == "asset"
        moments.append(
            CheckedOrderMoment(
                occurrence_id=row["moment_id"],
                intent_id=f"remaining-{index}",
                label="Selected photo" if is_photo else "Checked video",
                source_slot=source.slot,
                source_fence_digest=source_fence_digest(source),
                phone_binding_digest=None if is_photo else phone_binding_digest(bindings[0]),
                media_id=source.media_id,
                kind=row["kind"],
                gcs_path=row["gcs_path"],
                generation=row["generation"],
                source_start_s=None if is_photo else row["source_start_s"],
                source_end_s=None if is_photo else row["source_end_s"],
                hold_duration_s=row["duration_s"] if is_photo else None,
                evidence_origin="selected_image" if is_photo else "creator_answer",
                evidence_ref=f"checked-{index}",
                output_start_s=row["output_start_s"],
                output_end_s=row["output_end_s"],
                duration_s=row["duration_s"],
                playback_rate=row.get("playback_rate") or 1.0,
                freeze_hold_s=row.get("freeze_hold_s") or 0.0,
            )
        )
    p = OrderReplacementProposal.model_validate(
        {**proposal().model_dump(mode="python"), "moments": tuple(moments)}
    )
    current = load_order_replacement_source_context_sync(
        db,
        item_id=item.id,
        creator_id=owner_id,
        proposal=p,
    )
    assert visual_source in current.sources
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **{**context(p), "sources": current.sources, "phone_bindings": current.phone_bindings},
    )
    strategy["confirmed_order_replacement"] = proof.model_dump(mode="json")
    args = {
        **initial,
        "strategy": strategy,
        "brief": CreativeBrief(version=4, requirements=[requirement(proof)]),
    }
    return db, args, offered, proof, current, asset


def test_mixed_selected_photo_accepts_actual_native_compiler_identity(mixed_visual_preflight):
    db, args, offered, proof, current, asset = mixed_visual_preflight
    original = copy.deepcopy(offered)
    native_photo = next(row for row in offered["story_timeline"] if row["kind"] == "image")
    confirmed_photo = next(moment for moment in proof.proposal.moments if moment.kind == "image")
    assert native_photo["media_id"] == str(asset.id)
    assert confirmed_photo.media_id == f"asset-{asset.id}"
    assert confirmed_photo.source_slot == f"visual:{asset.id}"
    actual = compiled_order_preflight_sync(db, **args)
    assert actual is not None, {
        "native_media_id": native_photo["media_id"],
        "confirmed_media_id": confirmed_photo.media_id,
        "source_slot": confirmed_photo.source_slot,
    }
    verify_compiled_order(
        proof, offered, sources=current.sources, phone_bindings=current.phone_bindings
    )
    assert actual["story_timeline"] == offered["story_timeline"]
    assert offered == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("media_id", "00000000-0000-0000-0000-000000000001"),
        ("gcs_path", "visuals/different-photo.jpg"),
        ("generation", "different-generation"),
        ("lane", "clip"),
    ],
)
def test_native_visual_identity_adapter_cannot_accept_a_different_asset(
    mixed_visual_preflight,
    field,
    value,
):
    _, _, offered, proof, current, _ = mixed_visual_preflight
    changed = copy.deepcopy(offered)
    row = next(row for row in changed["story_timeline"] if row["kind"] == "image")
    row[field] = value
    with pytest.raises(ValueError):
        verify_compiled_order(
            proof, changed, sources=current.sources, phone_bindings=current.phone_bindings
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("gcs_path", "visuals/replaced-photo.jpg"),
        ("gcs_generation", "new-generation"),
    ],
)
def test_mixed_preflight_rechecks_current_visual_source(mixed_visual_preflight, field, value):
    db, args, _, _, _, asset = mixed_visual_preflight
    setattr(asset, field, value)
    db.flush()
    assert compiled_order_preflight_sync(db, **args) is None


def test_visual_manifest_alias_is_not_an_alternative_native_id(mixed_visual_preflight):
    _, _, offered, proof, current, asset = mixed_visual_preflight
    changed = copy.deepcopy(offered)
    row = next(row for row in changed["story_timeline"] if row["kind"] == "image")
    row["media_id"] = f"asset-{asset.id}"
    with pytest.raises(ValueError, match="occurrence or source changed"):
        verify_compiled_order(
            proof, changed, sources=current.sources, phone_bindings=current.phone_bindings
        )
