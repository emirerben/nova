"""KRI-1057 / KRI-1058: a sentence shown piece by piece always has a bar to split.

Failure modes written down before the fix:
1. A `text` title and a DIFFERENT `text_sequence` sentence in one brief (either order, title or
   whole-video scope): the opening title bar went to the title text, the sentence had no bar,
   and an honest composer answered "unmet" after approval (`creation_text_failed`).
2. A sequence scoped to a clip seeded nothing, so it hung on the composer adding text first.
3. A sequence that repeats the title's own words must still be ONE bar (no duplicate).
4. A sequence alone, or a title alone, must plan exactly as before.
5. Seeding the bar must not turn the brief into one that asked for clip text: no place or
   landmark labels, no strategy shot labels, no model calls for clip facts, and the story
   layout stays possible.
6. The bar never replaces a label the creator wrote for a clip (a described shot, a chapter
   caption, a positional label): the sentence takes a clip that has none.
7. Every distinct title or whole-video sentence has its own bar, not only the first.
Every model answer is faked by reading the bars the composer is actually shown.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.agents._runtime import ModelInvocation
from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.story_direction import story_blocker
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creation_text_composition import compose_creation_text, snapshot_copy_constraints

TITLE = "Weekend in Assos"
SENTENCE = "Slow mornings and salty air"
SECOND = "Golden hours by the sea"


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


def _sequence(scope="title", literal=SENTENCE, req_id="r-seq"):
    return BriefRequirement(
        id=req_id,
        kind="text_sequence",
        scope=scope,
        literal=literal,
        description="show it word by word",
        facts={"unit": "word", "mode": "replace"},
    )


def _plan(*requirements, opening_title=None, clips=None, strategy=None, **kwargs):
    brief = CreativeBrief(version=2, requirements=list(requirements))
    strategy = {"font_family": "Inter", "text_color": "#FFFFFF", **(strategy or {})}
    if opening_title:
        strategy["opening_title"] = opening_title
    return plan_unified_montage(
        clips or _clips(),
        brief_view(brief),
        strategy=strategy,
        font_covers=lambda *_: True,
        **kwargs,
    )


class _Transport:
    """A composer that splits the bar holding each sentence, or says it cannot when none does.

    ``bars`` are the draft's own text rows, which is what the composer is shown.
    """

    def __init__(self, bars, sentences=(SENTENCE,)):
        self.bars = bars
        self.sentences = sentences

    def invoke(self, **kwargs):
        ops, unmet = [], []
        for sentence in self.sentences:
            target = next((b for b in self.bars if b["text"] == sentence), None)
            if target is None:
                unmet.append(
                    {"request": "sequence", "reason": f"no text to split for {sentence!r}"}
                )
            else:
                ops.append(
                    {
                        "op": "replace_text_sequence",
                        "selector": {"ids": [target["id"]]},
                        "segments": sentence.split(),
                    }
                )
        body = {"intent": "edit", "confidence": 0.95, "reply": "Split the sentence.", "ops": ops}
        if unmet:
            body["confidence"] = 0.9
            body["unmet_requests"] = unmet
        return ModelInvocation(raw_text=json.dumps(body))


def _compose(monkeypatch, plan, requirements, sentences=(SENTENCE,)):
    transport = _Transport(_rows(plan.snapshot), sentences)
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


def _clips_with_facts(count=3):
    return [
        dataclasses.replace(
            clip,
            facts=(
                {"kind": "place", "value": "Behramkale, Ayvacik, Canakkale", "provenance": "gps"},
                {"kind": "landmark", "value": f"Temple of Athena {i}", "provenance": "inferred"},
            ),
        )
        for i, clip in enumerate(
            UnifiedClip(
                media_id=f"c{i}",
                proxy_path=f"users/test/c{i}.mp4",
                generation="1",
                duration_s=12,
                width=1080,
                height=1920,
            )
            for i in range(count)
        )
    ]


def _labels(plan):
    return {label.media_id: label.text for label in plan.snapshot.clip_labels or []}


@pytest.mark.parametrize("scope", ["title", "global"])
def test_a_sentence_beside_a_title_asks_for_no_clip_text(scope):
    requirements = [_title_text(), _sequence(scope)]
    view = brief_view(CreativeBrief(version=2, requirements=requirements))
    assert view.wants_per_clip_text is False
    assert story_blocker({}, view) is None
    plan = _plan(*requirements, opening_title=TITLE, clips=_clips_with_facts())
    assert _labels(plan) == {"c0": SENTENCE}
    assert not plan.dropped_label_clip_ids


def test_strategy_shot_labels_stay_stripped_beside_a_title_and_a_sentence():
    plan = _plan(
        _title_text(),
        _sequence("title"),
        opening_title=TITLE,
        clips=_clips_with_facts(),
        strategy={"shot_labels": ["hook context A", "hook context B", "hook context C"]},
    )
    assert _labels(plan) == {"c0": SENTENCE}


_BEACH_INTENT = {
    "op": "caption",
    "status": "resolved",
    "intent_id": "cap1",
    "attribute": "the beach shot",
    "caption_attribute": "Beach day",
    "caption_text": "Beach day",
    "creator_text": "Beach day",
    "caption_grounding": "creator_text",
    "assignments": [{"media_id": "c0"}],
}


def test_a_sentence_never_replaces_the_creators_text_for_the_first_clip(monkeypatch):
    requirements = [
        _title_text(),
        _sequence("title"),
        BriefRequirement(
            id="r-beach",
            kind="text",
            scope="per_clip",
            literal="Beach day",
            description="on the beach shot write Beach day",
        ),
    ]
    plan = _plan(
        *requirements,
        opening_title=TITLE,
        clips=_clips() + [dataclasses.replace(_clips()[0], media_id="c2")],
        strategy={"resolved_clip_intents": [_BEACH_INTENT]},
        clip_intents_enabled=True,
    )
    labels = _labels(plan)
    assert labels["c0"] == "Beach day"
    assert SENTENCE in labels.values()
    assert not plan.dropped_label_clip_ids
    result = _compose(monkeypatch, plan, requirements)
    assert [r["text"] for r in _rows(result) if "::sequence-" in r["id"]] == SENTENCE.split()


def test_a_positional_label_for_the_first_clip_is_not_replaced_by_a_sentence():
    plan = _plan(
        _title_text(),
        _sequence("title"),
        BriefRequirement(
            id="r-cap", kind="text", scope="per_clip", literal=None, description="label each clip"
        ),
        opening_title=TITLE,
        strategy={"shot_labels": ["Arrival"]},
    )
    labels = _labels(plan)
    assert labels["c0"] == "Arrival"
    assert SENTENCE in labels.values()


@pytest.mark.parametrize(
    "extra",
    [
        [_sequence("global", SECOND)],
        [_title_text(), _sequence("global", SECOND)],
    ],
)
def test_two_sentences_each_get_their_own_bar(monkeypatch, extra):
    first = _sequence("title", req_id="r-seq-1")
    second = _sequence("global", SECOND, req_id="r-seq-2")
    requirements = [first, second]
    if len(extra) == 2:
        requirements = [_title_text(), first, second]
    plan = _plan(*requirements, opening_title=TITLE if len(extra) == 2 else None, clips=_clips())
    bars = [r["text"] for r in _rows(plan.snapshot)]
    assert SENTENCE in bars and SECOND in bars
    result = _compose(monkeypatch, plan, requirements, sentences=(SENTENCE, SECOND))
    split = [r["text"] for r in _rows(result) if "::sequence-" in r["id"]]
    assert split == SENTENCE.split() + SECOND.split()


def test_the_same_sentence_asked_twice_is_one_bar():
    first = _sequence("title", req_id="r-seq-1")
    again = _sequence("global", req_id="r-seq-2")
    bars = [r["text"] for r in _rows(_plan(first, again).snapshot)]
    assert bars.count(SENTENCE) == 1
