"""Offline order replacement readers; no named-action or incident replay claim."""

import copy

import pytest
from pydantic import ValidationError

from app.kria.order_replacement import (
    CheckedOrderMoment,
    OrderReplacementProof,
    OrderReplacementProposal,
    phone_binding_digest,
    source_fence_digest,
    validate_confirmation,
    validate_proposal,
    verify_compiled_order,
)
from app.kria.prepared_execution import SourceIdentity


def source():
    return SourceIdentity(
        slot="clip:0",
        media_id="take",
        path="clips/take.mp4",
        role="original",
        generation="g1",
        duration_s=40.0,
    )


def moment(occurrence="first", start=2.0):
    return CheckedOrderMoment(
        occurrence_id=occurrence,
        intent_id=occurrence,
        label=occurrence,
        source_slot="clip:0",
        source_fence_digest=source_fence_digest(source()),
        media_id="take",
        kind="video",
        gcs_path="clips/take.mp4",
        generation="g1",
        source_start_s=start,
        source_end_s=start + 2,
        output_start_s=0.0 if occurrence == "first" else 2.0,
        output_end_s=2.0 if occurrence == "first" else 4.0,
        duration_s=2.0,
        evidence_origin="creator_answer",
        evidence_ref=f"point:{occurrence}",
    )


def proposal():
    return OrderReplacementProposal(
        predecessor_requirement_id="r2",
        predecessor_brief_version=3,
        predecessor_requirement_digest="a" * 64,
        rejection_question_id="clips-question",
        rejection_category_key="interior",
        rejection_selection_sequence=7,
        rejected_intent_id="interior",
        moments=(moment(), moment("second", 12.0)),
    )


def context(p):
    return dict(
        predecessor_requirement_id="r2",
        predecessor_brief_version=3,
        predecessor_requirement_digest="a" * 64,
        checked_moments=p.moments,
        sources=(source(),),
        rejection_question_id=p.rejection_question_id,
        rejection_category_key=p.rejection_category_key,
        rejection_selection_sequence=p.rejection_selection_sequence,
        rejected_intent_id=p.rejected_intent_id,
    )


def question(p):
    return {
        "question_id": "replace-question",
        "kind": "order_replacement",
        "input_digest": p.input_digest,
        "options": [{"key": "accept_sequence"}, {"key": "revise_sequence"}],
    }


def confirmation(p, **updates):
    args = dict(
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
    )
    args.update(updates)
    return validate_confirmation(p, **args, **context(p))


def execution(p):
    return {
        "story_timeline": [
            {
                "moment_id": m.occurrence_id,
                "media_id": m.media_id,
                "kind": m.kind,
                "gcs_path": m.gcs_path,
                "generation": m.generation,
                "source_start_s": m.source_start_s,
                "source_end_s": m.source_end_s,
                "output_start_s": m.output_start_s,
                "output_end_s": m.output_end_s,
                "duration_s": m.duration_s,
                "playback_rate": m.playback_rate,
                "freeze_hold_s": m.freeze_hold_s,
            }
            for m in p.moments
        ]
    }


def test_confirmed_same_source_occurrences_roundtrip_and_verify():
    p = proposal()
    validate_proposal(p, **context(p))
    proof = confirmation(p)
    restored = OrderReplacementProof.model_validate_json(proof.model_dump_json())
    assert restored == proof
    assert proof.proposal.input_digest == p.input_digest
    verify_compiled_order(proof, execution(p), sources=(source(),))


@pytest.mark.parametrize(
    "change", ["version", "requirement", "digest", "media", "interval", "omitted", "reordered"]
)
def test_stale_proposal_rejected(change):
    p = proposal()
    args = context(p)
    if change == "version":
        args["predecessor_brief_version"] = 4
    elif change == "requirement":
        args["predecessor_requirement_id"] = "r3"
    elif change == "digest":
        args["predecessor_requirement_digest"] = "b" * 64
    elif change == "media":
        args["sources"] = (source().model_copy(update={"generation": "g2"}),)
    elif change == "interval":
        args["checked_moments"] = (moment(start=3.0), p.moments[1])
    elif change == "omitted":
        args["checked_moments"] = p.moments[:1]
    else:
        args["checked_moments"] = tuple(reversed(p.moments))
    with pytest.raises(ValueError):
        validate_proposal(p, **args)


@pytest.mark.parametrize(
    "change",
    [
        "question",
        "digest",
        "decline",
        "delegated",
        "sequence",
        "before_rejection",
        "missing_option",
    ],
)
def test_confirmation_requires_exact_explicit_current_choice(change):
    p = proposal()
    q = question(p)
    selection = {"question_id": "replace-question", "option_key": "accept_sequence"}
    updates = {"question": q, "selection": selection}
    if change == "question":
        selection["question_id"] = "other-question"
    elif change == "digest":
        q["input_digest"] = "b" * 64
    elif change == "decline":
        selection["option_key"] = "revise_sequence"
    elif change == "delegated":
        selection["delegated"] = True
    elif change == "sequence":
        updates["selection_sequence"] = 8
    elif change == "before_rejection":
        updates["question_sequence"] = 6
    else:
        q["options"] = [{"key": "revise_sequence"}]
    with pytest.raises(ValueError):
        confirmation(p, **updates)


@pytest.mark.parametrize(
    "change",
    [
        "window",
        "wrong_content",
        "generation",
        "order",
        "missing",
        "duplicate",
        "extra",
        "output_order",
    ],
)
def test_actual_compiled_sequence_must_match_every_occurrence(change):
    p = proposal()
    compiled = execution(p)
    rows = compiled["story_timeline"]
    if change == "window":
        rows[0]["source_end_s"] += 0.5
    elif change == "wrong_content":
        rows[0]["source_start_s"] += 8
        rows[0]["source_end_s"] += 8
    elif change == "generation":
        rows[0]["generation"] = "g2"
    elif change == "order":
        rows.reverse()
    elif change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif change == "extra":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[1]["output_start_s"] = 0.0
    with pytest.raises(ValueError):
        verify_compiled_order(confirmation(p), compiled, sources=(source(),))


@pytest.mark.parametrize(
    "updates",
    [
        {"occurrence_id": ""},
        {"source_start_s": float("nan")},
        {"source_end_s": 1.0},
        {"generation": ""},
        {"evidence_origin": "unlocated"},
        {"checked": True},
        {"source_start_s": True},
    ],
)
def test_invalid_moment_cannot_supply_authority(updates):
    raw = moment().model_dump(mode="json")
    raw.update(updates)
    with pytest.raises(ValidationError):
        CheckedOrderMoment.model_validate(raw)


def test_duplicate_occurrence_and_rejected_retained_intent_fail():
    raw = proposal().model_dump(mode="json")
    raw["moments"][1] = raw["moments"][0]
    with pytest.raises(ValidationError):
        OrderReplacementProposal.model_validate(raw)
    raw = proposal().model_dump(mode="json")
    raw["rejected_intent_id"] = "first"
    with pytest.raises(ValidationError):
        OrderReplacementProposal.model_validate(raw)


def test_database_json_arrays_roundtrip():
    proof = confirmation(proposal())
    raw = proof.model_dump(mode="json")
    assert isinstance(raw["proposal"]["moments"], list)
    assert OrderReplacementProof.model_validate(raw) == proof
    raw["proposal"]["moments"][0]["source_end_s"] += 1
    with pytest.raises(ValidationError):
        OrderReplacementProof.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rejection_question_id", "other"),
        ("rejection_category_key", "other"),
        ("rejection_selection_sequence", 10),
        ("rejected_intent_id", "other"),
    ],
)
def test_later_or_different_rejection_invalidates_offer(field, value):
    p = proposal()
    args = {**context(p), field: value}
    with pytest.raises(ValueError):
        validate_proposal(p, **args)


def test_reader_checks_real_recompiled_timeline_with_photos():
    from app.pipeline.guided_story import compile_execution_plan
    from tests.pipeline.test_guided_story import _guided_snapshot

    snapshot = _guided_snapshot()
    offered = compile_execution_plan(snapshot, track=None)
    sources = tuple(
        SourceIdentity(
            slot=f"clip:{i}",
            media_id=row["media_id"],
            path=row["gcs_path"],
            role="original",
            generation=row["generation"],
            duration_s=30.0,
        )
        for i, row in enumerate(offered["story_timeline"])
    )
    moments = tuple(
        CheckedOrderMoment(
            occurrence_id=row["moment_id"],
            intent_id=f"intent-{i}",
            label=row["topic"],
            source_slot=src.slot,
            source_fence_digest=source_fence_digest(src),
            media_id=row["media_id"],
            kind=row["kind"],
            gcs_path=row["gcs_path"],
            generation=row["generation"],
            source_start_s=row["source_start_s"] if row["kind"] == "video" else None,
            source_end_s=row["source_end_s"] if row["kind"] == "video" else None,
            hold_duration_s=row["duration_s"] if row["kind"] == "image" else None,
            evidence_origin="creator_answer" if row["kind"] == "video" else "selected_image",
            evidence_ref=f"synthetic-confirmed-window-{i}",
            output_start_s=row["output_start_s"],
            output_end_s=row["output_end_s"],
            duration_s=row["duration_s"],
            playback_rate=row.get("playback_rate") or 1.0,
            freeze_hold_s=row.get("freeze_hold_s") or 0.0,
        )
        for i, (row, src) in enumerate(zip(offered["story_timeline"], sources, strict=True))
    )
    raw = proposal().model_dump(mode="python")
    raw["moments"] = moments
    p = OrderReplacementProposal.model_validate(raw)
    args = {**context(p), "sources": sources}
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **args,
    )
    actual = compile_execution_plan(snapshot, track=None)
    verify_compiled_order(proof, actual, sources=sources)
    assert any(row["kind"] == "image" for row in actual["story_timeline"])
    changed = copy.deepcopy(actual)
    video = next(row for row in changed["story_timeline"] if row["kind"] == "video")
    video["source_start_s"] += 1
    video["source_end_s"] += 1
    with pytest.raises(ValueError, match="source window"):
        verify_compiled_order(proof, changed, sources=sources)
    changed = copy.deepcopy(actual)
    photo = next(row for row in changed["story_timeline"] if row["kind"] == "image")
    photo["duration_s"] += 1
    with pytest.raises(ValueError, match="timing"):
        verify_compiled_order(proof, changed, sources=sources)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_versions_require_exact_supported_integer(version):
    raw = proposal().model_dump(mode="json")
    raw["version"] = version
    with pytest.raises(ValidationError):
        OrderReplacementProposal.model_validate(raw)


def test_nonfinite_source_duration_is_not_window_evidence():
    p = proposal()
    args = {**context(p), "sources": (source().model_copy(update={"duration_s": float("nan")}),)}
    with pytest.raises(ValueError):
        validate_proposal(p, **args)


@pytest.mark.parametrize("change", ["overlap", "gap", "retime", "duration", "rate", "freeze"])
def test_source_preserving_output_mutations_fail(change):
    p = proposal()
    compiled = execution(p)
    row = compiled["story_timeline"][1]
    if change == "overlap":
        row["output_start_s"] -= 0.5
    elif change == "gap":
        row["output_start_s"] += 0.5
        row["output_end_s"] += 0.5
    elif change == "retime":
        row["output_end_s"] += 1.0
        row["duration_s"] += 1.0
    elif change == "duration":
        row["duration_s"] += 1.0
    elif change == "rate":
        row["playback_rate"] = 2.0
    else:
        row["freeze_hold_s"] = 1.0
    with pytest.raises(ValueError, match="timing"):
        verify_compiled_order(confirmation(p), compiled, sources=(source(),))


def test_confirmed_speed_uses_actual_output_clock():
    raw = proposal().model_dump(mode="json")
    for i, row in enumerate(raw["moments"]):
        row.update(
            output_start_s=float(i), output_end_s=float(i + 1), duration_s=1.0, playback_rate=2.0
        )
    p = OrderReplacementProposal.model_validate(raw)
    verify_compiled_order(confirmation(p), execution(p), sources=(source(),))


def test_analysis_proxy_requires_unavailable_original_lineage_contract():
    proxy = source().model_copy(
        update={"role": "analysis_proxy", "path": "users/u/analysis-proxy-ios-take.mp4"}
    )
    raw = proposal().model_dump(mode="json")
    for row in raw["moments"]:
        row.update(gcs_path=proxy.path, source_fence_digest=source_fence_digest(proxy))
    p = OrderReplacementProposal.model_validate(raw)
    with pytest.raises(ValueError, match="verified phone binding"):
        validate_proposal(p, **{**context(p), "sources": (proxy,)})


def phone_case():
    from app.services.phone_sources import bind_phone_sources
    from tests.services.test_phone_sources import receipt

    assignment = receipt("take")
    assignment["duration_s"] = 40.0
    assignment["upload_contract"]["proxy"]["duration_s"] = 40.0
    assignment["upload_contract"]["proxy"]["original"]["duration_s"] = 40.0
    bindings = bind_phone_sources([assignment], [assignment["gcs_path"]])
    proxy = source().model_copy(
        update={
            "path": assignment["gcs_path"],
            "role": "analysis_proxy",
            "generation": assignment["storage_generation"],
        }
    )
    raw = proposal().model_dump(mode="json")
    for row in raw["moments"]:
        row.update(
            gcs_path=proxy.path,
            generation=proxy.generation,
            source_fence_digest=source_fence_digest(proxy),
            phone_binding_digest=phone_binding_digest(bindings[0]),
        )
    p = OrderReplacementProposal.model_validate(raw)
    args = {**context(p), "sources": (proxy,), "phone_bindings": bindings}
    return assignment, bindings, proxy, p, args


def test_verified_phone_receipt_pins_original_fingerprint_and_clock():
    assignment, bindings, proxy, p, args = phone_case()
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **args,
    )
    verify_compiled_order(proof, execution(p), sources=(proxy,), phone_bindings=bindings)
    asset = bindings[0].render_asset()
    original = assignment["upload_contract"]["proxy"]["original"]
    assert asset.fingerprint.sha256 == original["sha256"]
    assert asset.fingerprint.byte_count == original["byte_count"]
    assert "analysis-proxy" not in asset.model_dump_json()
    assert proof.proposal.moments[1].source_start_s == 12.0
    assert OrderReplacementProof.model_validate(proof.model_dump(mode="json")) == proof


@pytest.mark.parametrize(
    "change",
    [
        "sha256",
        "bytes",
        "original_duration",
        "generation",
        "media_id",
        "timing_version",
        "registered_duration",
        "proxy_clock",
        "manifest",
        "audio",
    ],
)
def test_current_phone_receipt_changes_cannot_reuse_confirmation(change):
    from app.services.phone_sources import bind_phone_sources

    assignment, bindings, proxy, p, args = phone_case()
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **args,
    )
    contract = assignment["upload_contract"]["proxy"]
    if change == "sha256":
        contract["original"]["sha256"] = "b" * 64
    elif change == "bytes":
        contract["original"]["byte_count"] += 1
    elif change == "original_duration":
        contract["original"]["duration_s"] = 41.0
        contract["duration_s"] = assignment["duration_s"] = 41.0
    elif change == "generation":
        assignment["storage_generation"] = "new-generation"
    elif change == "media_id":
        assignment["media_id"] = assignment["manifest_identity"] = "other"
    elif change == "timing_version":
        contract["timing_version"] = 2
    elif change == "registered_duration":
        assignment["duration_s"] = 30.0
    elif change == "proxy_clock":
        contract["duration_s"] = assignment["duration_s"] = 20.0
    elif change == "manifest":
        assignment["manifest_identity"] = "other"
    else:
        assignment["has_audio"] = False
    with pytest.raises(ValueError):
        current = bind_phone_sources([assignment], [assignment["gcs_path"]])
        verify_compiled_order(proof, execution(p), sources=(proxy,), phone_bindings=current)


def test_proxy_window_cannot_use_proxy_rounding_past_original_end():
    from app.services.phone_sources import bind_phone_sources

    assignment, bindings, proxy, p, args = phone_case()
    assignment["duration_s"] = 40.05
    assignment["upload_contract"]["proxy"]["duration_s"] = 40.05
    bindings = bind_phone_sources([assignment], [assignment["gcs_path"]])
    proxy = proxy.model_copy(update={"duration_s": 40.05})
    raw = p.model_dump(mode="json")
    raw["moments"][1]["source_start_s"] = 39.0
    raw["moments"][1]["source_end_s"] = 40.04
    p = OrderReplacementProposal.model_validate(raw)
    with pytest.raises(ValueError, match="exceeds"):
        validate_proposal(p, **{**context(p), "sources": (proxy,), "phone_bindings": bindings})
