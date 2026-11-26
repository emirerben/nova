"""Scoring for the story sequencer's held-out set (KRI-1593).

One number per case: the share of hand-written precedence pairs ("this clip plays before that
one") the played order satisfies. The played order is what the SERVER makes of a ranking
(`story_order.apply_ranking` over the case's own spans), so a score is about the video the creator
would get, not the raw model text. The upload order (the clips as listed) is the baseline every
answer is compared with.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.agents.story_sequencer import StorySequencerInput
from app.services import story_order

HELDOUT = Path(__file__).with_name("heldout.json")
# A recorded live run of the held-out set (`test_story_sequencer_evals.py::test_live_heldout_run`).
RECORDED_RUN = Path(__file__).parent.parent / "baselines" / "story_sequencer" / "heldout_run.json"
# "Beats upload order": the mean score must clear the baseline's by this much.
REQUIRED_MARGIN = 0.25


def load_heldout() -> dict[str, Any]:
    return json.loads(HELDOUT.read_text(encoding="utf-8"))


def case_input(case: dict[str, Any]) -> StorySequencerInput:
    return StorySequencerInput.model_validate(case["input"])


def upload_order(case: dict[str, Any]) -> list[str]:
    return [str(clip["id"]) for clip in case["input"]["clips"]]


def spans(case: dict[str, Any]) -> list[tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for clip in case["input"]["clips"]:
        if clip.get("span"):
            grouped.setdefault(str(clip["span"]), []).append(str(clip["id"]))
    return [tuple(ids) for ids in grouped.values() if len(ids) > 1]


def played_order(case: dict[str, Any], ranking: Sequence[str] | None) -> list[str]:
    """The order the creator would get for ``ranking`` (None = the model could not decide)."""
    before = upload_order(case)
    return story_order.apply_ranking(before, spans(case), ranking) if ranking else before


def precedence_score(case: dict[str, Any], order: Sequence[str]) -> float:
    at = {clip: index for index, clip in enumerate(order)}
    pairs = case["precedence"]
    return sum(1 for earlier, later in pairs if at[earlier] < at[later]) / len(pairs)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
