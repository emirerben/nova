"""KRI-470 PR-E: cloud renderers emit evidence; the verifier checks it; declines lift per adapter.

Failure modes this file is written against (before the code):
  * a declined requirement is lifted for an adapter that does not emit the evidence
    (desired metadata passes for proof);
  * evidence is emitted but the verifier accepts the wrong thing: reversed order,
    opening text that is really an end card, camera audio on a "forbid" edit,
    a recording that never reached the output file;
  * a receipt written before this change (no evidence keys) is retroactively
    failed, or a job without the contract marker grows receipts;
  * a renderer that emits no receipt (talking head / subtitled) renders at full
    spend and is then rejected at publication.

Every adapter x lifted-field pair drives the real entry points
(`preflight_cloud_contract`, `verify_cloud_variant`) with a supported fixture
(passes both) and a wrong-evidence fixture (fails visibly with a typed reason).
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from app.pipeline.cloud_render_evidence import (
    classic_slot_evidence,
    collapse_adjacent,
    media_ids_by_gcs_path,
)
from app.services.cloud_render_contract import (
    CLASSIC_EVIDENCE_ARCHETYPES,
    CLOUD_ADAPTER_DECLARATIONS,
    CLOUD_EVIDENCES,
    CloudRenderContractError,
    check_classic_archetype,
    cloud_adapter_for_job,
    cloud_adapter_for_variant,
    preflight_cloud_contract,
    verify_cloud_variant,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)

GUIDED = "cloud_guided_story"
CLASSIC = "cloud_classic"
SLIDES = "cloud_slides"
_ARCHETYPE = {GUIDED: "guided_story", CLASSIC: "montage", SLIDES: "slides"}
_CANDIDATES = {REQUIREMENT_VERSION_FIELD: 1}

# Clips in capture order: a, b, c.
_SNAPSHOT = {
    "clip_assignments": [
        {"media_id": "c", "capture": {"capture_time": "2026-10-06T12:00:00Z"}},
        {"media_id": "a", "capture": {"capture_time": "2026-10-06T10:00:00Z"}},
        {"media_id": "b", "capture": {"capture_time": "2026-10-06T11:00:00Z"}},
    ]
}


def _assembly(strategy: dict[str, Any], *, media_snapshot: dict | None = None) -> dict[str, Any]:
    contract = build_render_contract(
        strategy, generation_id="generation-1", media_snapshot=media_snapshot
    )
    assert contract is not None
    assert not contract.unresolved, contract.unresolved
    return {CONTRACT_FIELD: contract.model_dump(mode="json")}


_EVIDENCE_KEYS = {
    "actual_clip_order",
    "picture_timeline",
    "source_audio_ids",
    "source_audio_state",
    "source_audio_reason",
    "text_evidence",
}


def _variant(adapter: str, receipt: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    """A published variant: evidence keys live in the sibling ``cloud_evidence``, never in
    the strict guided receipt (KRI-470 P2-1); the rest stays on ``render_receipt``."""
    evidence = {k: v for k, v in (receipt or {}).items() if k in _EVIDENCE_KEYS}
    plain = {k: v for k, v in (receipt or {}).items() if k not in _EVIDENCE_KEYS}
    return {
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/output.mp4",
        "resolved_archetype": _ARCHETYPE[adapter],
        **({"render_receipt": plain} if receipt is not None else {}),
        **({"cloud_evidence": {"schema_version": 1, **evidence}} if evidence else {}),
        **extra,
    }


def _receipt(**fields: Any) -> dict[str, Any]:
    return {"verified": True, "actual_duration_s": 12.0, **fields}


def _seg(media_id: str, start: float, end: float) -> dict[str, Any]:
    return {"media_id": media_id, "start_s": start, "end_s": end}


def _text(role: str, text: str, start: float, end: float, media_id: str | None = None) -> dict:
    return {
        "role": role,
        "text": text,
        "start_s": start,
        "end_s": end,
        **({"media_id": media_id} if media_id else {}),
    }


_THREE_SEGMENTS = [_seg("a", 0, 4), _seg("b", 4, 8), _seg("c", 8, 12)]


def _passes(adapter: str, assembly: dict, receipt: dict | None, **extra: Any) -> None:
    preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=adapter)
    assert verify_cloud_variant(
        assembly, _variant(adapter, receipt, **extra), candidates=_CANDIDATES
    )


def _fails(
    adapter: str,
    assembly: dict,
    receipt: dict | None,
    reason: str = "evidence_missing",
    path: str | None = None,
) -> CloudRenderContractError:
    # Preflight cannot see the output, so it lets a consumed requirement through...
    preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=adapter)
    # ...and publication refuses it, visibly and typed.
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(assembly, _variant(adapter, receipt), candidates=_CANDIDATES)
    assert exc.value.decline_reason == reason
    if path is not None:
        assert exc.value.field_path == path
    return exc.value


# ── declarations: what each adapter consumes, before/after ───────────────────────


def test_declarations_lift_only_what_each_adapter_evidences():
    assert CLOUD_ADAPTER_DECLARATIONS[GUIDED].consumes == frozenset(
        {"duration_s", "require_voiceover", "original_audio", "exact_texts", "order_required"}
    )
    # Classic HONOURS only the recorded voice (by archetype): its matcher never reads
    # the contract's order and its song variants replace camera audio, so those decline
    # up front even though its receipts can report them.
    assert CLOUD_ADAPTER_DECLARATIONS[CLASSIC].consumes == frozenset(
        {"duration_s", "require_voiceover"}
    )
    for adapter, declaration in CLOUD_ADAPTER_DECLARATIONS.items():
        assert declaration.consumes <= CLOUD_EVIDENCES[adapter], adapter
    assert CLOUD_ADAPTER_DECLARATIONS[SLIDES].consumes == frozenset()
    # Named camera-audio sources cannot be isolated by any cloud renderer.
    for declaration in CLOUD_ADAPTER_DECLARATIONS.values():
        assert declaration.declines["audio_source_ids"].reason == "capability_unavailable"
    assert CLOUD_ADAPTER_DECLARATIONS[CLASSIC].declines["exact_texts"].reason == (
        "capability_unavailable"
    )


def test_adapter_is_identified_from_the_variant_and_the_job():
    assert cloud_adapter_for_variant({"resolved_archetype": "guided_story"}) == GUIDED
    assert cloud_adapter_for_variant({"resolved_archetype": "slides"}) == SLIDES
    assert cloud_adapter_for_variant({"resolved_archetype": "montage"}) == CLASSIC
    assert cloud_adapter_for_variant({}) == CLASSIC
    assert cloud_adapter_for_job({}, {"edit_format": "montage"}) == CLASSIC
    assert cloud_adapter_for_job({}, {"declared_edit_format": "slides"}) == SLIDES
    # A guided snapshot that applies to the intent is the guided adapter; with a
    # recorded voiceover the intent is native, so the dispatcher renders classic.
    snapshot = {"guided_edit": {"proposal_version": 1}}
    assert cloud_adapter_for_job(snapshot, {"edit_format": "montage"}) == GUIDED
    assert (
        cloud_adapter_for_job(
            snapshot, {"edit_format": "montage", "voiceover_gcs_path": "voice/a.m4a"}
        )
        == CLASSIC
    )


# ── order_required ───────────────────────────────────────────────────────────────

_ORDER = {"ordering_choice": "chronological", "selected_media_ids": ["a", "b", "c"]}


def test_guided_order_is_proven_by_the_rendered_picture_sequence():
    assembly = _assembly(_ORDER, media_snapshot=_SNAPSHOT)
    _passes(GUIDED, assembly, _receipt(actual_clip_order=["a", "b", "c"]))
    # a subset that keeps the relative order is a coverage choice, not an order failure
    _passes(GUIDED, assembly, _receipt(actual_clip_order=["a", "c"]))
    # reversed, interleaved repeats, foreign clips and absent evidence all fail visibly
    for wrong in (["c", "b", "a"], ["a", "b", "a", "c"], ["a", "x", "b"], None):
        receipt = _receipt(**({"actual_clip_order": wrong} if wrong is not None else {}))
        error = _fails(GUIDED, assembly, receipt, path="ordering_choice")
        assert error.alternative


def test_classic_declines_order_up_front_even_with_matching_evidence():
    """The classic matcher never reads the contract's order, so a default contracted job
    must not render everything and then fail publication."""
    assembly = _assembly(_ORDER, media_snapshot=_SNAPSHOT)
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=CLASSIC)
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "ordering_choice",
    )
    assert exc.value.alternative
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            assembly,
            _variant(CLASSIC, _receipt(actual_clip_order=["a", "b", "c"])),
            candidates=_CANDIDATES,
        )
    assert exc.value.decline_reason == "capability_unavailable"


def test_slides_still_decline_order_before_any_work():
    assembly = _assembly(_ORDER, media_snapshot=_SNAPSHOT)
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=SLIDES)
    assert exc.value.decline_reason == "capability_unavailable"
    with pytest.raises(CloudRenderContractError):
        verify_cloud_variant(
            assembly,
            _variant(SLIDES, _receipt(actual_clip_order=["a", "b", "c"])),
            candidates=_CANDIDATES,
        )


def test_adapter_less_preflight_never_lifts_a_decline():
    """Without a named adapter nothing is proven, so the pre-PR refusal stands."""
    for strategy, media in (
        (_ORDER, _SNAPSHOT),
        ({"audio_strategy": "original_audio"}, None),
        ({"opening_title": "Hello"}, None),
    ):
        with pytest.raises(CloudRenderContractError) as exc:
            preflight_cloud_contract(
                _assembly(strategy, media_snapshot=media), candidates=_CANDIDATES
            )
        assert exc.value.decline_reason == "capability_unavailable"


# ── original_audio (require / forbid) ────────────────────────────────────────────

_REQUIRE_AUDIO = {"audio_strategy": "original_audio"}
_FORBID_AUDIO = {"montage_audio": {"source_media_ids": [], "preserve_source_audio": False}}


def test_guided_original_audio_required_needs_audible_sources_in_the_output():
    assembly = _assembly(_REQUIRE_AUDIO)
    _passes(
        GUIDED,
        assembly,
        _receipt(source_audio_ids=["a", "b"], source_audio_state="audible"),
    )
    # muted output, no evidence, and a copied intent flag are not proof
    _fails(GUIDED, assembly, _receipt(source_audio_ids=[], source_audio_state="muted"))
    _fails(GUIDED, assembly, _receipt())
    _fails(GUIDED, assembly, _receipt(source_audio_preserved=True))


def test_guided_original_audio_forbidden_needs_a_muted_output():
    assembly = _assembly(_FORBID_AUDIO)
    _passes(
        GUIDED,
        assembly,
        _receipt(
            source_audio_ids=[],
            source_audio_state="muted",
            source_audio_reason="replaced_by_music",
        ),
    )
    _fails(GUIDED, assembly, _receipt(source_audio_ids=["a"], source_audio_state="audible"))
    _fails(GUIDED, assembly, _receipt())


@pytest.mark.parametrize("strategy", [_REQUIRE_AUDIO, _FORBID_AUDIO])
def test_classic_declines_camera_audio_up_front(strategy):
    """Classic never reads audio_strategy/montage_audio; song variants replace camera
    audio and track-less ones keep it, so one variant always disagrees."""
    assembly = _assembly(strategy)
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=CLASSIC)
    assert exc.value.decline_reason == "capability_unavailable"
    assert exc.value.alternative
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            assembly,
            _variant(CLASSIC, _receipt(source_audio_ids=[], source_audio_state="muted")),
            candidates=_CANDIDATES,
        )
    assert exc.value.decline_reason == "capability_unavailable"


def test_slides_decline_camera_audio():
    for strategy in (_REQUIRE_AUDIO, _FORBID_AUDIO):
        with pytest.raises(CloudRenderContractError) as exc:
            preflight_cloud_contract(_assembly(strategy), candidates=_CANDIDATES, adapter=SLIDES)
        assert exc.value.decline_reason == "capability_unavailable"


# ── named camera-audio sources stay declined for every cloud adapter ─────────────


@pytest.mark.parametrize("adapter", [GUIDED, CLASSIC, SLIDES])
def test_named_audio_sources_are_declined_before_spend_everywhere(adapter):
    assembly = _assembly(
        {
            "audio_strategy": "original_audio",
            "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["a"]},
        }
    )
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=adapter)
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "montage_audio.source_media_ids[]",
    )
    assert "iPhone" in exc.value.alternative
    # and a receipt claiming exactly that source set cannot lift it at publication
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            assembly,
            _variant(adapter, _receipt(source_audio_ids=["a"], source_audio_state="audible")),
            candidates=_CANDIDATES,
        )
    assert exc.value.decline_reason == "capability_unavailable"


# ── require_voiceover ────────────────────────────────────────────────────────────

_VOICE = {"audio_strategy": "voiceover"}


@pytest.mark.parametrize("adapter", [GUIDED, CLASSIC])
def test_voiceover_needs_the_recording_in_the_output(adapter):
    assembly = _assembly(_VOICE)
    _passes(adapter, assembly, _receipt(narration_applied=True))
    _fails(adapter, assembly, _receipt(narration_applied=False))
    _fails(adapter, assembly, None)


def test_slides_decline_voiceover_before_any_work():
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(_assembly(_VOICE), candidates=_CANDIDATES, adapter=SLIDES)
    assert exc.value.decline_reason == "capability_unavailable"


# ── duration_s ───────────────────────────────────────────────────────────────────

_DURATION = {"target_duration_s": 12, "target_duration_requested": True}


@pytest.mark.parametrize("adapter", [GUIDED, CLASSIC])
def test_duration_is_the_measured_output_length(adapter):
    assembly = _assembly(_DURATION)
    _passes(adapter, assembly, _receipt(actual_duration_s=12.4))
    _fails(adapter, assembly, _receipt(actual_duration_s=5.0), path="target_duration_s")


def test_slides_cannot_prove_a_length():
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(_assembly(_DURATION), candidates=_CANDIDATES, adapter=SLIDES)
    assert exc.value.decline_reason == "capability_unavailable"


# ── exact_texts: guided proves role + window; classic and slides decline ─────────


def test_guided_proves_opening_text_by_role_and_window():
    assembly = _assembly({"opening_title": "Eren in Athens", "opening_title_duration_s": 2.0})
    good = _receipt(
        text_evidence=[_text("opening", "Eren in Athens", 0, 2.5)],
        picture_timeline=_THREE_SEGMENTS,
    )
    _passes(GUIDED, assembly, good)
    # the same words as an end card are not an opening title
    late = _receipt(text_evidence=[_text("opening", "Eren in Athens", 9, 12)])
    _fails(GUIDED, assembly, late, path="opening_title")
    wrong_role = _receipt(text_evidence=[_text("closing", "Eren in Athens", 0, 2.5)])
    _fails(GUIDED, assembly, wrong_role, path="opening_title")
    # too brief for the requested hold
    brief = _receipt(text_evidence=[_text("opening", "Eren in Athens", 0, 0.5)])
    _fails(GUIDED, assembly, brief, path="opening_title_duration_s")
    # different words, absent text, absent evidence
    _fails(GUIDED, assembly, _receipt(text_evidence=[_text("opening", "Something else", 0, 3)]))
    _fails(GUIDED, assembly, _receipt(text_evidence=[]))
    _fails(GUIDED, assembly, _receipt())


def test_guided_text_wrapping_is_layout_not_a_different_title():
    assembly = _assembly({"opening_title": "Eren in Athens"})
    _passes(
        GUIDED,
        assembly,
        _receipt(text_evidence=[_text("opening", "Eren in\nAthens", 0, 2)]),
    )


def test_guided_proves_closing_text_sits_at_the_end():
    assembly = _assembly({"closing_title": "See you soon"})
    _passes(
        GUIDED, assembly, _receipt(text_evidence=[_text("closing", "See you soon", 10.5, 12.0)])
    )
    _fails(
        GUIDED,
        assembly,
        _receipt(text_evidence=[_text("closing", "See you soon", 0, 1.5)]),
        path="closing_title",
    )


def test_guided_proves_per_shot_text_lands_on_its_own_shot():
    assembly = _assembly({"shot_labels": ["Start", "Middle", "End"]})
    good = _receipt(
        picture_timeline=_THREE_SEGMENTS,
        text_evidence=[
            _text("clip", "Start", 0.2, 3.8, "a"),
            _text("clip", "Middle", 4.2, 7.8, "b"),
            _text("clip", "End", 8.2, 11.8, "c"),
        ],
    )
    _passes(GUIDED, assembly, good)
    # "Middle" parked on the first shot
    swapped = _receipt(
        picture_timeline=_THREE_SEGMENTS,
        text_evidence=[
            _text("clip", "Start", 0.2, 3.8, "a"),
            _text("clip", "Middle", 0.2, 3.8, "a"),
            _text("clip", "End", 8.2, 11.8, "c"),
        ],
    )
    _fails(GUIDED, assembly, swapped, path="shot_labels[]")
    # no picture timeline => the shot cannot be located
    _fails(GUIDED, assembly, _receipt(text_evidence=good["text_evidence"]), path="shot_labels[]")


@pytest.mark.parametrize("adapter", [CLASSIC, SLIDES])
def test_adapters_without_text_evidence_decline_exact_text_before_work(adapter):
    assembly = _assembly({"opening_title": "Eren in Athens"})
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract(assembly, candidates=_CANDIDATES, adapter=adapter)
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "opening_title",
    )
    # a receipt that lists the very words cannot lift it at publication either
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            assembly,
            _variant(adapter, _receipt(text_evidence=[_text("opening", "Eren in Athens", 0, 2)])),
            candidates=_CANDIDATES,
        )
    assert exc.value.decline_reason == "capability_unavailable"


# ── legacy receipts and jobs ─────────────────────────────────────────────────────


def test_a_receipt_stored_before_the_evidence_fields_still_validates_unchanged():
    from app.pipeline.guided_story import GuidedStoryRenderReceipt

    stored = {
        "schema_version": 1,
        "verified": True,
        "proposal_version": 1,
        "media_digest": "d" * 64,
        "expected_beat_ids": ["b"],
        "actual_beat_ids": ["b"],
        "expected_moment_ids": ["m"],
        "actual_moment_ids": ["m"],
        "expected_media_ids": ["a"],
        "actual_media_ids": ["a"],
        "expected_text_ids": [],
        "actual_text_ids": [],
        "media_count": 1,
        "image_count": 0,
        "video_count": 1,
        "expected_duration_s": 5.0,
        "actual_duration_s": 5.0,
        "music_applied": False,
        "music": None,
        "output": {
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "sha256": "e" * 64,
        },
        "media_stages": [],
        "moment_stages": [],
        "text_stages": [],
    }
    dumped = GuidedStoryRenderReceipt.model_validate(stored).model_dump(mode="json")
    for key in (
        "actual_clip_order",
        "picture_timeline",
        "source_audio_ids",
        "source_audio_state",
        "source_audio_reason",
        "text_evidence",
    ):
        assert key not in dumped


def test_a_marked_job_rendered_before_the_evidence_keeps_its_duration_behaviour():
    """Duration-only contracts never needed a receipt; they must not start to."""
    assembly = _assembly(_DURATION)
    for adapter in (GUIDED, CLASSIC):
        assert verify_cloud_variant(
            assembly,
            {**_variant(adapter, None), "duration_s": 12},
            candidates=_CANDIDATES,
        )


def test_a_legacy_job_without_the_marker_is_never_asked_for_evidence():
    assert verify_cloud_variant({}, _variant(CLASSIC, None), candidates={}) is None
    preflight_cloud_contract({}, candidates={}, adapter=CLASSIC)


def test_old_guided_receipt_cannot_satisfy_evidence_requirements_retroactively():
    """A receipt without evidence is `evidence_missing`, never a silent pass."""
    for strategy, media in ((_ORDER, _SNAPSHOT), ({"opening_title": "Hi"}, None)):
        assembly = _assembly(strategy, media_snapshot=media)
        with pytest.raises(CloudRenderContractError) as exc:
            verify_cloud_variant(assembly, _variant(GUIDED, _receipt()), candidates=_CANDIDATES)
        assert exc.value.decline_reason == "evidence_missing"


# ── classic archetype gate: renderers that emit no receipt refuse before spend ───


@pytest.mark.parametrize("archetype", ["talking_head", "subtitled"])
@pytest.mark.parametrize("strategy", [_VOICE])
def test_receiptless_archetypes_refuse_receipt_only_requirements_before_rendering(
    archetype, strategy
):
    assembly = _assembly(strategy)
    with pytest.raises(CloudRenderContractError) as exc:
        check_classic_archetype(assembly, candidates=_CANDIDATES, archetype=archetype)
    assert exc.value.decline_reason == "capability_unavailable"
    assert exc.value.alternative


def test_evidence_capable_archetypes_and_duration_only_contracts_pass_the_gate():
    for archetype in CLASSIC_EVIDENCE_ARCHETYPES:
        check_classic_archetype(_assembly(_VOICE), candidates=_CANDIDATES, archetype=archetype)
    check_classic_archetype(_assembly(_DURATION), candidates=_CANDIDATES, archetype="talking_head")
    check_classic_archetype({}, candidates={}, archetype="talking_head")


# ── receipts produced by the real renderers ──────────────────────────────────────


def test_pure_evidence_helpers():
    assert collapse_adjacent(["a", "a", "b", "a"]) == ["a", "b", "a"]
    assert media_ids_by_gcs_path(
        {
            "creator_brief_binding": {
                "media_snapshot": {
                    "clip_assignments": [{"gcs_path": "u/a.mp4", "media_id": "a"}, {"x": 1}]
                }
            }
        }
    ) == {"u/a.mp4": "a"}
    assert media_ids_by_gcs_path({}) == {}


def test_slot_evidence_withholds_everything_it_cannot_attribute():
    kwargs = {
        "clip_id_to_gcs": {"c1": "u/a.mp4", "c2": "u/b.mp4"},
        "clip_id_to_local": {"c1": "/a", "c2": "/b"},
        "probe_map": {
            "/a": types.SimpleNamespace(has_audio=True),
            "/b": types.SimpleNamespace(has_audio=False),
        },
        "media_ids_by_gcs": {"u/a.mp4": "a", "u/b.mp4": "b"},
    }
    ok = classic_slot_evidence([("c1", 2.0), ("c2", 3.0)], **kwargs)
    assert ok["actual_clip_order"] == ["a", "b"]
    assert ok["picture_timeline"] == [_seg("a", 0, 2), _seg("b", 2, 5)]
    assert ok["slot_audio_media_ids"] == ["a"]
    # a spliced synthetic clip, or one the approved snapshot does not know, voids the claim
    for slots in ([("c1", 2.0), ("__carousel_x", 1.0)], [("c1", 2.0), ("c3", 1.0)], []):
        assert classic_slot_evidence(slots, **kwargs)["actual_clip_order"] is None
    # unknown audio probe => camera audio unknown, picture still known
    unknown = classic_slot_evidence(
        [("c1", 2.0)], **{**kwargs, "probe_map": {"/a": types.SimpleNamespace()}}
    )
    assert unknown["slot_audio_media_ids"] is None and unknown["actual_clip_order"] == ["a"]


# ── closing text uses the renderer's own duration tolerance (P2-3) ───────────────


def test_closing_text_tolerance_matches_the_guided_duration_check():
    """Guided accepts max(0.2, 0.04 * moments) of duration drift; a valid closing title
    that ends inside that drift must not be false-declined."""
    from app.pipeline.cloud_render_evidence import duration_tolerance_s

    assert duration_tolerance_s(1) == pytest.approx(0.2)
    assert duration_tolerance_s(12) == pytest.approx(0.48)
    assembly = _assembly({"closing_title": "See you soon"})
    twelve = [_seg("a", float(i), float(i + 1)) for i in range(12)]
    row = [_text("closing", "See you soon", 10.0, 11.7)]  # 0.3s short of the 12.0 output
    _passes(GUIDED, assembly, _receipt(picture_timeline=twelve, text_evidence=row))
    _fails(
        GUIDED,
        assembly,
        _receipt(picture_timeline=[_seg("a", 0.0, 12.0)], text_evidence=row),
        path="closing_title",
    )
