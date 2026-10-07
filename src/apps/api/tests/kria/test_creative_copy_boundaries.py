"""Approval boundary failures for KRI-506, specified before the gate implementation.

A model can propose a strategy while a question is open, keep an old candidate after
revision, or put unapproved text in an editor draft. Neither draft completion nor
render approval/dispatch may authorize those plans. Media changes invalidate consent.
"""

import pytest

from app.services.creative_copy_decisions import authorship_question, media_digest, wording_question
from app.services.creative_copy_gate import creative_copy_problem

MEDIA = {"clip_paths": ["owner/clip-a.mp4"], "clip_assignments": []}


def _events(*, approved=False):
    question = wording_question(
        target="opening_title",
        candidate="A day worth remembering",
        dependency_digest=media_digest(MEDIA),
    )
    events = [("assistant", {"choice_question": question})]
    if approved:
        events.append(
            (
                "user",
                {
                    "choice_selection": {
                        "question_id": question["question_id"],
                        "option_key": "approve",
                    }
                },
            )
        )
    return events


def test_existing_threads_without_copy_decisions_keep_working():
    assert (
        creative_copy_problem([], MEDIA, strategy={"opening_title": "Creator's exact words"})
        is None
    )


@pytest.mark.parametrize("strategy", [None, {}, {"opening_title": "Unapproved"}])
def test_unapproved_wording_blocks_strategy_and_editor(strategy):
    assert creative_copy_problem(_events(), MEDIA, strategy=strategy)


def test_authorship_question_blocks_even_with_no_candidate():
    question = authorship_question(target="opening_title", dependency_digest=media_digest(MEDIA))
    assert creative_copy_problem([("assistant", {"choice_question": question})], MEDIA, strategy={})


def test_approved_exact_copy_passes_but_model_rewrite_or_omission_does_not():
    events = _events(approved=True)
    assert (
        creative_copy_problem(events, MEDIA, strategy={"opening_title": "A day worth remembering"})
        is None
    )
    assert creative_copy_problem(events, MEDIA, strategy={"opening_title": "A different day"})
    assert creative_copy_problem(events, MEDIA, strategy={})


def test_editor_cannot_rewrite_resolved_wording_without_a_reviewed_strategy():
    assert creative_copy_problem(_events(approved=True), MEDIA, strategy=None)


def test_changed_media_requires_new_wording_decision():
    changed = {"clip_paths": ["owner/replacement.mp4"], "clip_assignments": []}
    assert creative_copy_problem(
        _events(approved=True), changed, strategy={"opening_title": "A day worth remembering"}
    )
