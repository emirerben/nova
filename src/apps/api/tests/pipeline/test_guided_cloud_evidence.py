"""KRI-470 PR-E: the guided-story receipt reports what the renderer actually produced.

Failure modes written first: roles taken from the plan instead of the burned layers
(an invisible title reported as shown); the picture order copied from the approved
list instead of the rendered moments; camera audio claimed for an edit whose
narration or song replaced it; evidence emitted for jobs that did not ask for it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.pipeline.guided_story as guided_story
from app.agents._schemas.text_element import TextElement
from app.pipeline.guided_story import (
    GuidedStoryError,
    GuidedStoryRenderReceipt,
    _verify_receipt,
    compile_execution_plan,
    guided_cloud_evidence,
    guided_text_evidence,
    rederive_guided_text_evidence,
)
from app.services.cloud_render_contract import (
    CloudRenderContractError,
    check_guided_plan,
    check_guided_plan_audio,
    check_guided_plan_order,
    check_guided_plan_text,
    preflight_cloud_contract,
    verify_cloud_variant,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    TextRequirement,
    build_render_contract,
)
from tests.pipeline.test_guided_story import _guided_snapshot

_CANDIDATES = {REQUIREMENT_VERSION_FIELD: 1}
# `_guided_snapshot` selects these, in this order.
_ORDER = ["food-photo", "town-photo", "coast-video"]


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    raw = _guided_snapshot()
    raw["approved_proposal"]["title"] = "Corfu in a day"
    raw["approved_proposal"]["closing_title"] = "See you next summer"
    runtime = compile_execution_plan(raw, track=None)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"output")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _p: SimpleNamespace(
            duration_s=runtime["resolved_duration_s"],
            video_stream_duration_s=runtime["resolved_duration_s"],
            width=1080,
            height=1920,
            codec="h264",
        ),
    )
    monkeypatch.setattr(
        guided_story, "_story_canvas", lambda _o: SimpleNamespace(width=1080, height=1920)
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _p: "aac")
    monkeypatch.setattr(guided_story, "_sha256", lambda _p: "b" * 64)
    runtime["_final"] = str(final)
    return runtime


def _moments(plan: dict) -> list[dict]:
    return [
        {
            "moment_id": m["moment_id"],
            "beat_id": m["beat_id"],
            "media_id": m["media_id"],
            "generation": m["generation"],
            "kind": m["kind"],
            "layout": m["layout"],
            "image_motion": m.get("image_motion"),
        }
        for m in plan["story_timeline"]
    ]


def _plain(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k != "_final"}


def _receipt(plan: dict, *, visible: bool = True) -> dict:
    media = [
        {"media_id": mid, "gcs_path": "x", "generation": "1", "kind": "image"}
        for mid in plan["selected_media_ids"]
    ]
    texts = [{"element_id": e["id"], "visible": visible} for e in plan["text_elements"]]
    return _verify_receipt(
        _plain(plan), media, _moments(plan), texts, plan["_final"], music_applied=False
    )


def _evidence(plan: dict, ids: list[str], *, narration=False, music=False) -> dict:
    texts = [{"element_id": e["id"], "visible": True} for e in plan["text_elements"]]
    return guided_cloud_evidence(
        _plain(plan),
        _moments(plan),
        texts,
        [TextElement.model_validate(e) for e in plan["text_elements"]],
        ids,
        narration_applied=narration,
        music_applied=music,
        actual_duration_s=plan["resolved_duration_s"],
    )


def _variant(receipt: dict, evidence: dict) -> dict:
    return {
        "ok": True,
        "render_status": "ready",
        "video_path": "x",
        "resolved_archetype": "guided_story",
        "render_receipt": receipt,
        "cloud_evidence": evidence,
    }


# `GuidedStoryRenderReceipt` as it is on main (adb8ede00): old workers validate this model,
# so evidence must never widen it (rolling deploys / rollbacks, KRI-470 P2-1).
_MAIN_RECEIPT_FIELDS = {
    "schema_version", "verified", "proposal_version", "media_digest", "expected_beat_ids",
    "actual_beat_ids", "expected_moment_ids", "actual_moment_ids", "expected_media_ids",
    "actual_media_ids", "expected_text_ids", "actual_text_ids", "expected_context_label_ids",
    "actual_context_label_ids", "expected_narration_label_ids", "actual_narration_label_ids",
    "approved_text_ids", "text_edited_after_approval", "media_count", "image_count",
    "video_count", "expected_duration_s", "actual_duration_s", "output_orientation",
    "output_orientation_reason", "music_applied", "music", "song_reference",
    "music_window_applied", "output", "base_storage", "output_storage", "media_stages",
    "moment_stages", "text_stages", "revision_number", "revision_hash", "lane_hashes",
    "tombstones", "source_pool", "segment_order", "music_removed", "base_render_generation",
    "renderer_version", "effect_schema_version", "source_audio_options",
    "source_audio_preserved", "narration", "narration_applied", "narration_label_receipt",
}  # fmt: skip


def test_the_strict_receipt_is_unchanged_and_evidence_rides_beside_it(plan):
    assert set(GuidedStoryRenderReceipt.model_fields) == _MAIN_RECEIPT_FIELDS
    receipt = _receipt(plan)
    assert set(receipt) <= _MAIN_RECEIPT_FIELDS
    # a receipt written by new code validates under the unchanged model...
    GuidedStoryRenderReceipt.model_validate(receipt)
    # ...and the evidence is a plain sibling dict, not part of it
    evidence = _evidence(plan, ["coast-video"])
    assert set(evidence) & set(receipt) <= {
        "schema_version",
        "actual_duration_s",
        "narration_applied",
    }
    assert evidence["adapter"] == "cloud_guided_story"


def test_evidence_reports_the_rendered_picture_order_and_text_layers(plan):
    evidence = _evidence(plan, ["coast-video"])

    assert evidence["actual_clip_order"] == _ORDER
    assert [s["media_id"] for s in evidence["picture_timeline"]] == _ORDER
    roles = {row["role"]: row for row in evidence["text_evidence"]}
    assert roles["opening"]["text"] == "Corfu in a day" and roles["opening"]["start_s"] == 0.0
    assert roles["closing"]["text"] == "See you next summer"
    assert roles["closing"]["end_s"] == pytest.approx(plan["resolved_duration_s"], abs=0.05)
    assert evidence["source_audio_ids"] == ["coast-video"]
    assert evidence["source_audio_state"] == "audible"


def test_guided_evidence_satisfies_text_order_and_audio_contracts_end_to_end(plan):
    variant = _variant(_receipt(plan), _evidence(plan, ["coast-video"]))
    strategy = {
        "opening_title": "Corfu in a day",
        "closing_title": "See you next summer",
        "audio_strategy": "original_audio",
    }
    contract = build_render_contract(strategy, generation_id="g", clip_order=_ORDER)
    assert contract is not None
    contract = contract.rebind(
        order_ids=tuple(_ORDER), order_required=True, order_basis="confirmed"
    )
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter="cloud_guided_story")
    check_guided_plan_order(assembly, candidates=_CANDIDATES, plan=_plain(plan))
    assert verify_cloud_variant(assembly, variant, candidates=_CANDIDATES)

    reversed_contract = contract.rebind(order_ids=tuple(reversed(_ORDER)))
    reversed_assembly = {CONTRACT_FIELD: reversed_contract.model_dump(mode="json")}
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(reversed_assembly, variant, candidates=_CANDIDATES)
    assert exc.value.decline_reason == "evidence_missing"
    forbid = build_render_contract(
        {"montage_audio": {"source_media_ids": [], "preserve_source_audio": False}},
        generation_id="g",
    )
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            {CONTRACT_FIELD: forbid.model_dump(mode="json")}, variant, candidates=_CANDIDATES
        )
    assert exc.value.decline_reason == "evidence_missing"


# ── the pre-render plan gate ─────────────────────────────────────────────────────


def _order_assembly(order_ids) -> dict:
    contract = build_render_contract({"opening_title": "x"}, generation_id="g")
    contract = contract.rebind(
        order_ids=tuple(order_ids), order_required=True, order_basis="capture_time"
    )
    return {CONTRACT_FIELD: contract.model_dump(mode="json")}


def test_plan_gate_passes_a_plan_that_already_follows_the_order(plan):
    check_guided_plan_order(_order_assembly(_ORDER), candidates=_CANDIDATES, plan=_plain(plan))


def test_plan_gate_restricts_the_contract_order_to_the_media_the_plan_uses(plan):
    wider = ["extra-clip", *_ORDER[:1], "another", *_ORDER[1:]]
    check_guided_plan_order(_order_assembly(wider), candidates=_CANDIDATES, plan=_plain(plan))


def test_plan_gate_declines_before_render_when_the_plan_is_not_in_order(plan):
    swapped = [_ORDER[1], _ORDER[0], _ORDER[2]]
    with pytest.raises(CloudRenderContractError) as exc:
        check_guided_plan_order(_order_assembly(swapped), candidates=_CANDIDATES, plan=_plain(plan))
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "ordering_choice",
    )
    assert exc.value.alternative and "iPhone" in exc.value.alternative


def test_plan_gate_ignores_contracts_without_an_order_requirement(plan):
    contract = build_render_contract({"opening_title": "x"}, generation_id="g")
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    check_guided_plan_order(assembly, candidates=_CANDIDATES, plan=_plain(plan))
    check_guided_plan_order({}, candidates={}, plan=_plain(plan))


def test_plan_gate_merges_adjacent_repeats_but_not_later_returns(plan):
    twice = {**_plain(plan)}
    first = twice["story_timeline"][0]
    twice["story_timeline"] = [first, dict(first, moment_id="dup"), *twice["story_timeline"][1:]]
    check_guided_plan_order(_order_assembly(_ORDER), candidates=_CANDIDATES, plan=twice)
    back = {**_plain(plan)}
    back["story_timeline"] = [*back["story_timeline"], dict(first, moment_id="again")]
    with pytest.raises(CloudRenderContractError):
        check_guided_plan_order(_order_assembly(_ORDER), candidates=_CANDIDATES, plan=back)


# ── evidence semantics ───────────────────────────────────────────────────────────


def test_a_text_layer_the_pixel_check_did_not_find_is_never_reported_as_shown(plan):
    with pytest.raises(GuidedStoryError):
        _receipt(plan, visible=False)
    elements = [TextElement.model_validate(e) for e in plan["text_elements"]]
    assert guided_text_evidence(elements, set(), []) == []


def test_narration_or_a_song_replaces_camera_audio_in_the_evidence(plan):
    narrated = _evidence(plan, ["coast-video"], narration=True)
    assert narrated["source_audio_ids"] == []
    assert narrated["source_audio_reason"] == "replaced_by_narration"
    assert narrated["narration_applied"] is True
    song = _evidence(plan, ["coast-video"], music=True)
    assert song["source_audio_reason"] == "replaced_by_music"
    assert _evidence(plan, [])["source_audio_state"] == "muted"


def test_receipt_building_adds_no_evidence_keys(plan):
    receipt = _receipt(plan)
    for key in ("actual_clip_order", "picture_timeline", "source_audio_ids", "text_evidence"):
        assert key not in receipt


def test_text_reburn_rederives_the_words_that_are_now_on_screen(plan, monkeypatch):
    receipt = _receipt(plan)
    evidence = _evidence(plan, ["coast-video"])
    monkeypatch.setattr(
        guided_story, "_sha256", lambda path: "c" * 64 if "base" in str(path) else "d" * 64
    )
    edited = [TextElement.model_validate(e) for e in plan["text_elements"]]
    edited[0] = edited[0].model_copy(update={"text": "A different opening"})
    updated = guided_story.verify_guided_text_reburn(
        receipt,
        [e.model_dump(mode="json") for e in edited],
        [{"element_id": e.id, "visible": True} for e in edited],
        plan["_final"],
        "clean_base.mp4",
    )
    GuidedStoryRenderReceipt.model_validate(updated)  # still the unchanged model
    fresh = rederive_guided_text_evidence(evidence, edited, {e.id for e in edited})
    texts = {row["text"] for row in fresh["text_evidence"]}
    assert "A different opening" in texts and "Corfu in a day" not in texts
    # the untouched picture and camera-audio evidence is carried forward
    assert fresh["actual_clip_order"] == evidence["actual_clip_order"]
    assert fresh["source_audio_ids"] == evidence["source_audio_ids"]
    # a variant with no evidence stays without it
    assert rederive_guided_text_evidence(None, edited, {e.id for e in edited}) is None


# ── the pre-render camera-audio gate ─────────────────────────────────────────────


def _audio_assembly(strategy: dict) -> dict:
    contract = build_render_contract(strategy, generation_id="g")
    return {CONTRACT_FIELD: contract.model_dump(mode="json")}


_NARRATION = {"gcs_path": "voice/a.m4a", "generation": "1", "duration_s": 8.0}


def test_audio_gate_passes_a_plan_that_keeps_camera_audio_when_it_is_required(plan):
    keeps = {**_plain(plan), "compiler_version": 7, "narration": None, "montage_audio": None}
    check_guided_plan_audio(
        _audio_assembly({"audio_strategy": "original_audio"}), candidates=_CANDIDATES, plan=keeps
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"narration": _NARRATION},  # a recording replaces camera audio
        {"user_song": {"gcs_path": "song/a.m4a"}},  # a creator song is the whole soundtrack
        {"montage_audio": {"preserve_source_audio": False}},  # the plan itself mutes it
        {"compiler_version": 5},  # pre-v6 plans never restore source audio
    ],
)
def test_audio_gate_declines_before_render_when_required_audio_cannot_survive(plan, changes):
    muted = {**_plain(plan), "narration": None, "montage_audio": None, **changes}
    with pytest.raises(CloudRenderContractError) as exc:
        check_guided_plan_audio(
            _audio_assembly({"audio_strategy": "original_audio"}),
            candidates=_CANDIDATES,
            plan=muted,
        )
    assert exc.value.decline_reason == "capability_unavailable"
    assert exc.value.field_path == "montage_audio.preserve_source_audio"
    assert exc.value.alternative


def test_audio_gate_names_the_voice_versus_camera_audio_conflict(plan):
    both = _audio_assembly(
        {
            "audio_strategy": "voiceover",
            "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        }
    )
    narrated = {**_plain(plan), "narration": _NARRATION}
    with pytest.raises(CloudRenderContractError) as exc:
        check_guided_plan_audio(both, candidates=_CANDIDATES, plan=narrated)
    assert exc.value.decline_reason == "requirement_conflict"
    assert "voice" in exc.value.alternative


def test_audio_gate_declines_a_forbid_the_plan_would_break(plan):
    forbid = _audio_assembly(
        {"montage_audio": {"preserve_source_audio": False, "source_media_ids": []}}
    )
    keeps = {**_plain(plan), "compiler_version": 7, "narration": None, "montage_audio": None}
    with pytest.raises(CloudRenderContractError):
        check_guided_plan_audio(forbid, candidates=_CANDIDATES, plan=keeps)
    mutes = {**keeps, "montage_audio": {"preserve_source_audio": False}}
    check_guided_plan_audio(forbid, candidates=_CANDIDATES, plan=mutes)
    narrated = {**keeps, "narration": _NARRATION}
    check_guided_plan_audio(forbid, candidates=_CANDIDATES, plan=narrated)


def test_audio_gate_ignores_contracts_without_a_camera_audio_requirement(plan):
    check_guided_plan_audio(
        _audio_assembly({"opening_title": "x"}), candidates=_CANDIDATES, plan=_plain(plan)
    )
    check_guided_plan_audio({}, candidates={}, plan=_plain(plan))


# ── the pre-render text gate ─────────────────────────────────────────────────────


def _text_assembly(*texts: TextRequirement) -> dict:
    contract = build_render_contract({"pacing": "fast"}, generation_id="g")
    return {CONTRACT_FIELD: contract.rebind(exact_texts=tuple(texts)).model_dump(mode="json")}


def test_text_gate_passes_text_the_plan_will_burn(plan):
    assembly = _text_assembly(
        TextRequirement(role="opening", text="Corfu in a day"),
        TextRequirement(role="closing", text="See you next summer"),
        TextRequirement(role="any", text="Corfu in a day"),
    )
    check_guided_plan_text(assembly, candidates=_CANDIDATES, plan=_plain(plan))


@pytest.mark.parametrize(
    "requirement",
    [
        # brief-derived copy that no snapshot field carries
        TextRequirement(role="any", text="Words the snapshot never received"),
        # the right words in the wrong place: an end card is not the opening
        TextRequirement(role="closing", text="Corfu in a day"),
        # a per-shot label on a shot that has none
        TextRequirement(role="clip", text="Start line", shot_index=0),
    ],
)
def test_text_gate_declines_before_render_when_the_plan_cannot_show_the_text(plan, requirement):
    with pytest.raises(CloudRenderContractError) as exc:
        check_guided_plan_text(
            _text_assembly(requirement), candidates=_CANDIDATES, plan=_plain(plan)
        )
    assert exc.value.decline_reason == "capability_unavailable"
    assert exc.value.field_path
    assert exc.value.alternative


def test_combined_gate_runs_order_audio_and_text(plan):
    contract = build_render_contract(
        {"audio_strategy": "original_audio", "opening_title": "Corfu in a day"}, generation_id="g"
    ).rebind(order_ids=tuple(_ORDER), order_required=True, order_basis="capture_time")
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    check_guided_plan(assembly, candidates=_CANDIDATES, plan=_plain(plan))
    with pytest.raises(CloudRenderContractError):
        check_guided_plan(
            assembly, candidates=_CANDIDATES, plan={**_plain(plan), "narration": _NARRATION}
        )
