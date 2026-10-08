"""KRI-479: composition commitments beside the render contract.

Failure modes pinned (before the code):

* the voice clip stays in `order_ids`, so the speech job refuses the confirmed order;
* a new field sneaks into the strict contract model (older workers read stamped jobs);
* an UNSTAMPED job gets commitments (legacy behaviour must be byte-identical);
* a commitment written for one contract is applied to a rebuilt/edited one (stale digest);
* the route / commitments are derived from request text instead of typed plan fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services.creator_render_contract import (
    COMPOSITION_FIELD,
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    CompositionCommitments,
    CreatorRenderContract,
    build_render_contract,
    commitments_from_strategy,
    read_composition,
    stamp_composition,
)

T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
VOICE = "talk"
PICTURE = ["a", "b", "c", "d"]


def _snapshot(*, shuffled: bool = True) -> dict:
    order = ["c", VOICE, "a", "d", "b"] if shuffled else [*PICTURE, VOICE]
    minutes = {"a": 1, "b": 2, "talk": 3, "c": 0, "d": 9}  # c, a, b, talk, d
    rows = []
    for media_id in order:
        stamp = (T0 + timedelta(minutes=minutes[media_id])).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"media_id": media_id, "kind": "video", "capture": {"capture_time": stamp}})
    return {"clip_assignments": rows}


def _strategy(**update) -> dict:
    base = {
        "audio_strategy": "original_audio",
        "voice_mode": "continuous",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": [VOICE]},
        "ordering_choice": "chronological",
        "target_duration_s": 30,
        "target_duration_requested": True,
    }
    return CreativeStrategy.model_validate({**base, **update}).model_dump(
        mode="json", exclude_none=True
    )


def test_the_voice_clip_is_left_out_of_the_order_when_its_picture_is_hidden():
    strategy = _strategy()
    commitments = commitments_from_strategy(strategy)
    assert commitments == CompositionCommitments(voice_picture="hidden")
    contract = build_render_contract(
        strategy, generation_id="g", media_snapshot=_snapshot(), composition=commitments
    )
    assert contract is not None and contract.unresolved == ()
    assert contract.order_ids == ("c", "a", "b", "d")  # capture time, voice clip excluded
    assert contract.audio_source_ids == (VOICE,)  # the voice is still the required audio
    assert contract.order_required and contract.order_basis == "capture_time"


def test_without_commitments_the_contract_is_exactly_what_it_was():
    strategy = _strategy()
    plain = build_render_contract(strategy, generation_id="g", media_snapshot=_snapshot())
    assert plain is not None
    assert plain.order_ids == ("c", "a", "b", "talk", "d")  # unchanged legacy projection
    explicit_none = build_render_contract(
        strategy, generation_id="g", media_snapshot=_snapshot(), composition=None
    )
    assert explicit_none == plain
    # A no-voice-mode strategy yields no commitments at all.
    assert commitments_from_strategy(_strategy(voice_mode=None)) is None


def test_the_hidden_voice_is_also_dropped_from_a_selected_set_and_attachment_order():
    selected = _strategy(selected_media_ids=["a", "b", "c", "d", VOICE], media_scope="selected")
    contract = build_render_contract(
        selected,
        generation_id="g",
        media_snapshot=_snapshot(),
        composition=commitments_from_strategy(selected),
    )
    assert contract is not None and contract.order_ids == ("c", "a", "b", "d")
    explicit = build_render_contract(
        _strategy(),
        generation_id="g",
        media_snapshot=_snapshot(shuffled=False),
        clip_order=["d", "talk", "a"],
        composition=CompositionCommitments(voice_picture="hidden"),
    )
    assert explicit is not None and "talk" not in explicit.order_ids


def test_the_contract_model_did_not_grow_a_field():
    """Older workers read stamped jobs with `extra="forbid"`: no new contract field."""
    assert set(CreatorRenderContract.model_fields) == {
        "version",
        "generation_id",
        "strategy_digest",
        "brief_digest",
        "duration_s",
        "audio_source_ids",
        "original_audio",
        "require_voiceover",
        "exact_texts",
        "order_ids",
        "order_required",
        "order_basis",
        "unresolved",
        "digest",
    }


# --- the sibling key -----------------------------------------------------------------------


def _job(strategy: dict, *, stamped: bool = True, route: str | None = "voice_behind_footage"):
    contract = build_render_contract(
        strategy,
        generation_id="g",
        media_snapshot=_snapshot(),
        composition=commitments_from_strategy(strategy),
    )
    assert contract is not None
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    if route:
        assembly["creator_route"] = {
            "route": route,
            "platform": "phone",
            "contract_digest": contract.digest,
        }
    candidates = {"creator_strategy": strategy}
    if stamped:
        candidates[PLAN_AUTHORITY_FIELD] = 1
    return assembly, candidates, contract


def test_stamp_writes_the_commitments_keyed_by_the_contract_digest():
    assembly, candidates, contract = _job(_strategy())
    stamped = stamp_composition(assembly, candidates)
    assert stamped[COMPOSITION_FIELD] == {
        "contract_digest": contract.digest,
        "route": "voice_behind_footage",
        "voice_picture": "hidden",
        "voice_span_s": None,
        "min_shot_s": None,
    }
    json.dumps(stamped)  # plain JSON, no models
    assert read_composition(stamped, contract.digest) == CompositionCommitments(
        voice_picture="hidden"
    )


def test_an_unstamped_job_never_gets_commitments_and_keeps_the_same_object():
    assembly, candidates, _ = _job(_strategy(), stamped=False)
    assert stamp_composition(assembly, candidates) is assembly
    assert COMPOSITION_FIELD not in assembly


def test_a_strategy_without_a_voice_mode_is_left_untouched():
    assembly, candidates, _ = _job(_strategy(voice_mode=None), route=None)
    assert stamp_composition(assembly, candidates) is assembly


def test_a_stale_digest_reads_as_absent():
    assembly, candidates, contract = _job(_strategy())
    stamped = stamp_composition(assembly, candidates)
    assert read_composition(stamped, "someone-elses-digest") is None
    assert read_composition(stamped, "") is None
    assert read_composition({}, contract.digest) is None
    mangled = {**stamped, COMPOSITION_FIELD: {**stamped[COMPOSITION_FIELD], "voice_picture": "x"}}
    assert read_composition(mangled, contract.digest) is None


def test_restamping_after_the_strategy_loses_voice_mode_removes_the_old_key():
    assembly, candidates, _ = _job(_strategy())
    stamped = stamp_composition(assembly, candidates)
    plain_candidates = {**candidates, "creator_strategy": _strategy(voice_mode=None)}
    cleared = stamp_composition(stamped, plain_candidates)
    assert COMPOSITION_FIELD not in cleared


def test_the_silent_tail_answer_commits_the_voice_span():
    strategy = _strategy(
        choice_answers=[
            {
                "conflict": "voice_vs_duration",
                "kind": "voice_vs_duration",
                "option": "silent_tail",
                "input_digest": "d1",
            }
        ]
    )
    commitments = commitments_from_strategy(strategy, voice_duration_s=20.0)
    assert commitments is not None and commitments.voice_picture == "hidden"
    assert commitments.voice_span_s == pytest.approx(19.95)
    # Without the answer (or without a known voice length) no span is promised.
    assert commitments_from_strategy(_strategy(), voice_duration_s=20.0).voice_span_s is None
    assert commitments_from_strategy(strategy).voice_span_s is None


def test_several_named_voices_do_not_hide_anything():
    several = _strategy(
        montage_audio={"preserve_source_audio": True, "source_media_ids": [VOICE, "a"]}
    )
    assert commitments_from_strategy(several) is None
