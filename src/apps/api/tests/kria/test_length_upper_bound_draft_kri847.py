"""KRI-847: the draft strategy plans under a ceiling instead of exactly to it.

``_sanitize_strategy_duration_provenance`` overwrites the model's ``target_duration_s`` with the
creator's seconds. For "under 10 seconds" that made the planner aim at exactly 10 s even when
the Creator model chose 7 s from the footage. Failure modes written first:

1. The model's shorter pick (7 s) is thrown away for a ceiling.
2. The model's longer pick (the 24 s default) survives and plans past the ceiling.
3. An exact length stops overwriting the model's pick (guard).
4. The stamp that proves a creator length (``creator_duration_s``) changes for a ceiling.
"""

from __future__ import annotations

import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import _sanitize_strategy_duration_provenance


def _binding(**facts) -> BriefBinding:
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Under 10 seconds",
                facts={"duration_s": 10, **facts},
            )
        ],
    )
    return BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message="Under 10 seconds",
        full_creator_request="Under 10 seconds",
        media_snapshot={},
        choice_answers=None,
    )


def _sanitized(binding: BriefBinding | None, **strategy) -> dict:
    raw = {"edit_format": "montage", "audio_strategy": "licensed_music", **strategy}
    return _sanitize_strategy_duration_provenance(raw, brief_binding=binding)


@pytest.mark.parametrize(
    ("model_target", "planned"),
    [
        pytest.param(7, 7, id="the-models-shorter-pick-is-kept"),
        pytest.param(10, 10, id="at-the-ceiling"),
        pytest.param(24, 10, id="the-24s-default-is-capped"),
        pytest.param(45, 10, id="a-longer-pick-is-capped"),
    ],
)
def test_a_ceiling_caps_the_models_target_instead_of_replacing_it(model_target, planned):
    out = _sanitized(_binding(duration_bound="at_most"), target_duration_s=model_target)

    assert out["target_duration_s"] == planned
    assert out["creator_duration_s"] == 10
    assert out["target_duration_requested"] is True


def test_a_ceiling_without_a_model_target_plans_to_the_ceiling():
    out = _sanitized(_binding(duration_bound="at_most"))

    assert out["target_duration_s"] == 10
    assert out["creator_duration_s"] == 10


@pytest.mark.parametrize("model_target", [7, 10, 24, 45])
def test_guard_an_exact_length_still_overwrites_the_models_target(model_target):
    out = _sanitized(_binding(), target_duration_s=model_target)

    assert out["target_duration_s"] == 10
    assert out["creator_duration_s"] == 10


def test_guard_no_binding_and_no_stamp_proves_no_creator_length():
    out = _sanitized(None, target_duration_s=7)

    assert "creator_duration_s" not in out
    assert "target_duration_requested" not in out
