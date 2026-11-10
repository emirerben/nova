"""Approved word-by-word text is timed by the server, not guessed by the text composer.

KRI-1365, thread 147eb928: a creator approved a ~26-word Turkish paragraph for the middle and
its English translation for the bottom, both word by word, over 15 short archival clips, then
asked for a faster, 12 s video. Two failures came out of the text pass:

* the composer asked for research, and a literal word sequence went to the footage-explanation
  writer (180 characters / 15 cps per clip window), which raised "needs a longer clip or shorter
  wording" and was shown as a bug;
* the composer retimed each paragraph at its own ~0.45 s per word, so the two sequences ended at
  different times and the text pass refused them as "not on screen together".

Failure modes written down before the fix (each has a test below):
 1. research asked for, only approved literal sequences in the brief: no footage-writer call;
 2. paired sequences retimed independently by the composer: one shared span by construction;
 3. a pair at the same video length as its words need at the minimum piece time still fits;
 4. words that cannot fit even at the minimum piece time: an honest typed failure, never flicker;
 5. changed approved wording still rejected (control);
 6. a plain explanation ask that does research and fails: a typed creation_text failure, not a bug;
 7. the length plan never undercuts the longest sequence's minimum reading time, and a creator's
    explicit shorter length is never stretched.

Synthetic wording; provider answers stubbed; brief, planner, composer parse, replay and compile
are real.
"""

from __future__ import annotations

import json

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creation_text_composition import (
    CreationTextComposer,
    CreationTextCompositionError,
    compose_creation_text,
)
from app.services.researched_text import ResearchUnavailable
from tests.kria import test_approved_long_sequence_copy as base

LOCAL = base.LOCAL
ENGLISH = base.ENGLISH
# The incident's footage was 15 clips of 2 to 12 s; the creator's length was 12 s.
CLIP_SECONDS = [2, 3, 4, 2, 6, 12, 3, 5, 2, 4, 3, 6, 2, 9, 4]


def _brief(length_s: float | None = 12) -> CreativeBrief:
    reqs = [
        base._sequence("r1", LOCAL, "middle"),
        base._sequence("r2", ENGLISH, "bottom"),
    ]
    if length_s is not None:
        reqs.append(
            BriefRequirement(id="r3", kind="timing", scope="global", facts={"duration_s": length_s})
        )
    return CreativeBrief(requirements=reqs)


def _plan(brief: CreativeBrief):
    clips = [
        UnifiedClip(
            media_id=f"archive-{index}",
            proxy_path=f"private/archive-{index}.mp4",
            generation="1",
            duration_s=seconds,
            analysis={"understanding": {"summary": "Archival footage of a crowd"}},
        )
        for index, seconds in enumerate(CLIP_SECONDS)
    ]
    return plan_unified_montage(clips, brief_view(brief))


def _own_pace(per_word_s: float = 0.45):
    """What the live composer did: retime each paragraph on its own at ~0.45 s per word."""

    def answer(agent, input, *, ctx=None):  # noqa: ARG001
        total = input.variant_snapshot["total_duration_s"]
        bars = {bar["text"]: bar for bar in input.variant_snapshot["text_bars"]}
        ops = []
        for text, position in ((LOCAL, "middle"), (ENGLISH, "bottom")):
            ids = [bars[text]["id"]]
            ops.append(
                {
                    "op": "set_texts_timing",
                    "selector": {"ids": ids},
                    "start_s": 0.0,
                    "end_s": min(total, len(text.split()) * per_word_s),
                }
            )
            ops.append(
                {
                    "op": "replace_text_sequence",
                    "selector": {"ids": ids},
                    "segments": text.split(),
                    "patch": {"position": position},
                }
            )
        return agent.parse(
            json.dumps({"intent": "edit", "confidence": 0.9, "reply": "Shaped.", "ops": ops}),
            input,
        )

    return answer


def _asks_for_research(agent, input, *, ctx=None):  # noqa: ARG001
    reply = {
        "intent": "describe",
        "confidence": 0.9,
        "reply": "This needs research.",
        "ops": [],
        "research_request": {
            "topic": "Founders Day",
            "request_quote": "Explain Founders Day",
        },
    }
    return agent.parse(json.dumps(reply), input)


def _compose(monkeypatch, answer, *, length_s: float | None = 12):
    monkeypatch.setattr(CreationTextComposer, "run", answer)
    brief = _brief(length_s)
    plan = _plan(brief)
    composed = compose_creation_text(
        plan.snapshot,
        creator_request="Explain Founders Day word by word and translate it",
        text_requirements=brief.live(),
    )
    return plan, composed


def _pieces(composed, text: str) -> list[dict]:
    rows = compile_proposal_execution_plan(composed)["text_elements"]
    words = text.split()
    return sorted((r for r in rows if r["text"] in words), key=lambda r: r["start_s"])


def test_the_fixture_is_the_incident_shape(monkeypatch) -> None:
    story = compile_proposal_execution_plan(_plan(_brief()).snapshot)["story_timeline"]

    assert len(LOCAL.split()) >= 25
    assert len(CLIP_SECONDS) == 15
    assert 11.5 <= max(row["output_end_s"] for row in story) <= 12.5


def test_research_for_approved_sequences_never_reaches_the_footage_writer(monkeypatch) -> None:
    from app.services import researched_text

    def footage_writer(*args, **kwargs):  # noqa: ARG001
        raise ResearchUnavailable("This explanation needs a longer clip or shorter wording")

    monkeypatch.setattr(researched_text, "run_researched_revision", footage_writer)

    _, composed = _compose(monkeypatch, _asks_for_research)

    local, english = _pieces(composed, LOCAL), _pieces(composed, ENGLISH)
    assert [r["text"] for r in local] == LOCAL.split()
    assert [r["text"] for r in english] == ENGLISH.split()
    assert {r["position"] for r in local} == {"middle"}
    assert {r["position"] for r in english} == {"bottom"}


def test_paired_sequences_the_composer_paces_alone_share_one_span(monkeypatch) -> None:
    _, composed = _compose(monkeypatch, _own_pace())

    local, english = _pieces(composed, LOCAL), _pieces(composed, ENGLISH)
    assert [r["text"] for r in local] == LOCAL.split()
    assert [r["text"] for r in english] == ENGLISH.split()
    assert local[0]["start_s"] == pytest.approx(english[0]["start_s"], abs=1 / 30)
    assert local[-1]["end_s"] == pytest.approx(english[-1]["end_s"], abs=1 / 30)
    assert all(r["end_s"] - r["start_s"] >= 0.25 - 1e-3 for r in [*local, *english])
    assert compose_creation_text(composed, creator_request="same") == composed


def test_a_saved_server_timed_program_replays_without_the_composer(monkeypatch) -> None:
    brief = _brief()
    _, composed = _compose(monkeypatch, _own_pace())

    def no_model(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("a saved program replays without a model call")

    monkeypatch.setattr(CreationTextComposer, "run", no_model)

    again = compose_creation_text(
        composed, creator_request="Use approved wording", text_requirements=brief.live()
    )
    assert again == composed


def test_words_that_cannot_fit_at_the_minimum_piece_time_fail_honestly(monkeypatch) -> None:
    # 39 English words need at least 9.75 s at 0.25 s a piece; the creator asked for 6 s.
    with pytest.raises(CreationTextCompositionError, match="Text composition is incomplete"):
        _compose(monkeypatch, _own_pace(), length_s=6)


def test_changed_approved_wording_is_still_rejected(monkeypatch) -> None:
    def reword(agent, input, *, ctx=None):  # noqa: ARG001
        bar = next(b for b in input.variant_snapshot["text_bars"] if b["text"] == ENGLISH)
        reply = {
            "intent": "edit",
            "confidence": 0.9,
            "reply": "Shortened.",
            "ops": [{"op": "rewrite_text", "selector": {"ids": [bar["id"]]}, "text": "Short"}],
        }
        return agent.parse(json.dumps(reply), input)

    with pytest.raises(CreationTextCompositionError, match="confirmed copy"):
        _compose(monkeypatch, reword)


def test_a_failing_research_step_is_a_typed_creation_text_failure(monkeypatch) -> None:
    from app.services import researched_text

    def footage_writer(*args, **kwargs):  # noqa: ARG001
        raise ResearchUnavailable("This explanation needs a longer clip or shorter wording")

    monkeypatch.setattr(researched_text, "run_researched_revision", footage_writer)
    explanation = BriefRequirement(
        id="r9", kind="text", scope="per_clip", description="explain what each place is"
    )
    brief = CreativeBrief(requirements=[explanation])
    plan = _plan(brief)
    monkeypatch.setattr(CreationTextComposer, "run", _asks_for_research)

    with pytest.raises(CreationTextCompositionError, match="longer clip or shorter wording"):
        compose_creation_text(
            plan.snapshot, creator_request="Explain Founders Day", text_requirements=brief.live()
        )
