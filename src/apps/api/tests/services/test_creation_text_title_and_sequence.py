"""KRI-1057 / KRI-1058: a sentence shown piece by piece always has a bar to split.

Failure modes written down before the fix:
1. A `text` title and a DIFFERENT `text_sequence` sentence in one brief (either order, title or
   whole-video scope): the opening title bar went to the title text, the sentence had no bar,
   and an honest composer answered "unmet" after approval (`creation_text_failed`).
2. A sequence scoped to a clip seeded nothing, so it hung on the composer adding text first.
3. A sequence that repeats the title's own words must still be ONE bar (no duplicate).
4. A sequence alone, or a title alone, must plan exactly as before.
Every model answer is faked by reading the bars the composer is actually shown.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import ModelInvocation
from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creation_text_composition import compose_creation_text, snapshot_copy_constraints

TITLE = "Weekend in Assos"
SENTENCE = "Slow mornings and salty air"


def _clips():
    return [
        UnifiedClip(
            media_id=f"c{i}",
            proxy_path=f"users/test/c{i}.mp4",
            generation="1",
            duration_s=12,
            width=1080,
            height=1920,
        )
        for i in range(2)
    ]


def _title_text(literal=TITLE):
    return BriefRequirement(
        id="r-title", kind="text", scope="title", literal=literal, description="the title"
    )


def _sequence(scope="title", literal=SENTENCE):
    return BriefRequirement(
        id="r-seq",
        kind="text_sequence",
        scope=scope,
        literal=literal,
        description="show it word by word",
        facts={"unit": "word", "mode": "replace"},
    )


def _plan(*requirements, opening_title=None):
    brief = CreativeBrief(version=2, requirements=list(requirements))
    strategy = {"font_family": "Inter", "text_color": "#FFFFFF"}
    if opening_title:
        strategy["opening_title"] = opening_title
    return plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, font_covers=lambda *_: True
    )


class _Transport:
    """A composer that splits the bar holding SENTENCE, or says it cannot when none does.

    ``bars`` are the draft's own text rows, which is what the composer is shown.
    """

    def __init__(self, bars):
        self.bars = bars

    def invoke(self, **kwargs):
        target = next((b for b in self.bars if b["text"] == SENTENCE), None)
        if target is None:
            body = {
                "intent": "edit",
                "confidence": 0.9,
                "reply": "No bar holds that sentence.",
                "ops": [],
                "unmet_requests": [{"request": "sequence", "reason": "no text to split"}],
            }
        else:
            body = {
                "intent": "edit",
                "confidence": 0.95,
                "reply": "Split the sentence.",
                "ops": [
                    {
                        "op": "replace_text_sequence",
                        "selector": {"ids": [target["id"]]},
                        "segments": SENTENCE.split(),
                    }
                ],
            }
        return ModelInvocation(raw_text=json.dumps(body))


def _compose(monkeypatch, plan, requirements):
    transport = _Transport(_rows(plan.snapshot))
    monkeypatch.setattr("app.services.creation_text_composition.default_client", lambda: transport)
    return compose_creation_text(
        plan.snapshot,
        creator_request=f'Title "{TITLE}". Then show "{SENTENCE}" one word at a time.',
        copy_constraints=snapshot_copy_constraints(plan.snapshot),
        text_requirements=requirements,
    )


def _rows(snapshot):
    return compile_proposal_execution_plan(snapshot)["text_elements"]


@pytest.mark.parametrize("scope", ["title", "global"])
@pytest.mark.parametrize("title_first", [True, False])
def test_a_title_and_a_different_sentence_both_compose_in_either_order(
    monkeypatch, scope, title_first
):
    requirements = [_title_text(), _sequence(scope)]
    if not title_first:
        requirements.reverse()
    result = _compose(monkeypatch, _plan(*requirements, opening_title=TITLE), requirements)
    rows = _rows(result)
    assert [r["text"] for r in rows if "::sequence-" in r["id"]] == SENTENCE.split()
    assert [r["text"] for r in rows].count(TITLE) == 1


@pytest.mark.parametrize("scope", ["clip:c1", "clip:c0"])
def test_a_clip_scoped_sequence_seeds_its_own_bar(monkeypatch, scope):
    requirements = [_sequence(scope)]
    plan = _plan(*requirements)
    assert SENTENCE in [r["text"] for r in _rows(plan.snapshot)]
    result = _compose(monkeypatch, plan, requirements)
    assert [r["text"] for r in _rows(result) if "::sequence-" in r["id"]] == SENTENCE.split()


def test_a_sequence_that_repeats_the_title_words_is_still_one_bar(monkeypatch):
    requirements = [_title_text(SENTENCE), _sequence("title", SENTENCE)]
    plan = _plan(*requirements)
    assert [r["text"] for r in _rows(plan.snapshot)].count(SENTENCE) == 1


def test_a_title_alone_and_a_sequence_alone_plan_as_before():
    only_title = _rows(_plan(_title_text()).snapshot)
    only_seq = _rows(_plan(_sequence("title")).snapshot)
    assert [r["text"] for r in only_title].count(TITLE) == 1
    assert SENTENCE not in [r["text"] for r in only_title]
    assert [r["text"] for r in only_seq].count(SENTENCE) == 1
