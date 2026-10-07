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
    _guided_source_audio_evidence,
    _verify_receipt,
    compile_execution_plan,
    guided_text_evidence,
)
from app.services.cloud_render_contract import (
    CloudRenderContractError,
    preflight_cloud_contract,
    verify_cloud_variant,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
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


def _receipt(plan: dict, *, visible: bool = True, **evidence) -> dict:
    moments = [
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
    media = [
        {"media_id": mid, "gcs_path": "x", "generation": "1", "kind": "image"}
        for mid in plan["selected_media_ids"]
    ]
    texts = [{"element_id": e["id"], "visible": visible} for e in plan["text_elements"]]
    return _verify_receipt(
        {k: v for k, v in plan.items() if k != "_final"},
        media,
        moments,
        texts,
        plan["_final"],
        music_applied=False,
        text_elements=[TextElement.model_validate(e) for e in plan["text_elements"]],
        **evidence,
    )


def _audio(plan: dict, ids: list[str], **kw) -> dict:
    return _guided_source_audio_evidence(
        plan,
        ids,
        narration_applied=kw.get("narration", False),
        music_applied=kw.get("music", False),
    )


def test_receipt_reports_the_rendered_picture_order_and_text_layers(plan):
    receipt = _receipt(plan, source_audio=_audio(plan, ["coast-video"]))

    assert receipt["actual_clip_order"] == _ORDER
    assert [s["media_id"] for s in receipt["picture_timeline"]] == _ORDER
    roles = {row["role"]: row for row in receipt["text_evidence"]}
    assert roles["opening"]["text"] == "Corfu in a day" and roles["opening"]["start_s"] == 0.0
    assert roles["closing"]["text"] == "See you next summer"
    assert roles["closing"]["end_s"] == pytest.approx(plan["resolved_duration_s"], abs=0.05)
    assert receipt["source_audio_ids"] == ["coast-video"]
    assert receipt["source_audio_state"] == "audible"
    # and the typed receipt (what is persisted) accepts it
    GuidedStoryRenderReceipt.model_validate(receipt)


def test_guided_receipt_satisfies_text_order_and_audio_contracts_end_to_end(plan):
    receipt = _receipt(plan, source_audio=_audio(plan, ["coast-video"]))
    strategy = {
        "opening_title": "Corfu in a day",
        "closing_title": "See you next summer",
        "audio_strategy": "original_audio",
    }
    contract = build_render_contract(
        strategy,
        generation_id="g",
        clip_order=_ORDER,
    )
    assert contract is not None
    # exact requirement of order needs capture times; pin the order directly
    contract = contract.rebind(
        order_ids=tuple(_ORDER), order_required=True, order_basis="confirmed"
    )
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter="cloud_guided_story")
    variant = {
        "ok": True,
        "render_status": "ready",
        "video_path": "x",
        "resolved_archetype": "guided_story",
        "render_receipt": receipt,
    }
    assert verify_cloud_variant(assembly, variant, candidates=_CANDIDATES)

    # the same receipt against a contract that wants another order fails visibly
    reversed_contract = contract.rebind(order_ids=tuple(reversed(_ORDER)))
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            {CONTRACT_FIELD: reversed_contract.model_dump(mode="json")},
            variant,
            candidates=_CANDIDATES,
        )
    assert exc.value.decline_reason == "evidence_missing"
    # and against camera audio that must be forbidden
    forbid = build_render_contract(
        {"montage_audio": {"source_media_ids": [], "preserve_source_audio": False}},
        generation_id="g",
    )
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            {CONTRACT_FIELD: forbid.model_dump(mode="json")}, variant, candidates=_CANDIDATES
        )
    assert exc.value.decline_reason == "evidence_missing"


def test_a_text_layer_the_pixel_check_did_not_find_is_never_reported_as_shown(plan):
    with pytest.raises(GuidedStoryError):
        _receipt(plan, visible=False)
    elements = [TextElement.model_validate(e) for e in plan["text_elements"]]
    rows = guided_text_evidence(elements, set(), [])
    assert rows == []


def test_narration_or_a_song_replaces_camera_audio_in_the_evidence(plan):
    assert _audio(plan, ["coast-video"], narration=True) == {
        "source_audio_ids": [],
        "source_audio_state": "muted",
        "source_audio_reason": "replaced_by_narration",
    }
    assert _audio(plan, ["coast-video"], music=True)["source_audio_reason"] == "replaced_by_music"
    # nothing restored and no choice to keep it => muted with a reason, never a guess
    assert _audio(plan, [])["source_audio_state"] == "muted"


def test_evidence_is_only_emitted_when_asked_for(plan):
    legacy = _receipt(plan)
    for key in ("actual_clip_order", "picture_timeline", "source_audio_ids", "text_evidence"):
        assert key not in legacy
    # stored receipts keep their exact shape after a model round-trip
    dumped = GuidedStoryRenderReceipt.model_validate(legacy).model_dump(mode="json")
    assert "text_evidence" not in dumped and "picture_timeline" not in dumped


def test_text_reburn_rederives_the_words_that_are_now_on_screen(plan, monkeypatch):
    receipt = _receipt(plan, source_audio=_audio(plan, ["coast-video"]))
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
    texts = {row["text"] for row in updated["text_evidence"]}
    assert "A different opening" in texts and "Corfu in a day" not in texts
    # the picture and camera-audio evidence of the untouched base is carried forward
    assert updated["actual_clip_order"] == receipt["actual_clip_order"]
    assert updated["source_audio_ids"] == receipt["source_audio_ids"]


def test_text_reburn_of_a_legacy_receipt_stays_evidence_free(plan, monkeypatch):
    legacy = _receipt(plan)
    monkeypatch.setattr(
        guided_story, "_sha256", lambda path: "c" * 64 if "base" in str(path) else "d" * 64
    )
    elements = [TextElement.model_validate(e) for e in plan["text_elements"]]
    updated = guided_story.verify_guided_text_reburn(
        legacy,
        [e.model_dump(mode="json") for e in elements],
        [{"element_id": e.id, "visible": True} for e in elements],
        plan["_final"],
        "clean_base.mp4",
    )
    assert "text_evidence" not in updated
