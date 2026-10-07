"""Pure lifecycle replays for KRI-506 creative-copy state."""

from app.services.creative_copy_decisions import (
    OPT_APPROVE,
    OPT_GENERATE,
    OPT_REVISE,
    authorship_question,
    fold_creative_copy,
    wording_question,
)

DIGEST = "d" * 24


def _selection(question: dict, option: str, *, delegated: bool = False) -> tuple[str, dict]:
    payload = {"choice_selection": {"question_id": question["question_id"], "option_key": option}}
    if delegated:
        payload["choice_selection"]["delegated"] = True
    return "user", payload


def test_long_history_keeps_latest_approval() -> None:
    question = wording_question(
        target="opening_title", candidate="After dark", dependency_digest=DIGEST
    )
    events = [("user", {"message": f"old turn {index}"}) for index in range(150)]
    events += [("assistant", {"choice_question": question}), _selection(question, OPT_APPROVE)]

    state = fold_creative_copy(events, dependency_digest=DIGEST)["opening_title"]

    assert state.status == "approved"
    assert state.approved == "After dark"


def test_replacement_question_invalidates_old_approval_id() -> None:
    first = wording_question(
        target="opening_title", candidate="First title", dependency_digest=DIGEST
    )
    replacement = wording_question(
        target="opening_title", candidate="Replacement title", dependency_digest=DIGEST
    )
    events = [
        ("assistant", {"choice_question": first}),
        (
            "user",
            {"choice_selection": {"question_id": first["question_id"], "option_key": OPT_REVISE}},
        ),
        ("assistant", {"choice_question": replacement}),
        _selection(first, OPT_APPROVE),
    ]

    state = fold_creative_copy(events, dependency_digest=DIGEST)["opening_title"]

    assert state.status == "revise"
    assert state.approved is None
    assert state.candidate == "Replacement title"


def test_cancellation_survives_media_change() -> None:
    events = [
        (
            "assistant",
            {
                "creative_copy_resolution": {
                    "target": "closing_title",
                    "dependency_digest": DIGEST,
                    "status": "cancelled",
                }
            },
        ),
    ]

    state = fold_creative_copy(events, dependency_digest="e" * 24)["closing_title"]

    assert state.status == "cancelled"
    assert state.cancelled is True


def test_fabricated_selection_is_ignored_and_not_offered() -> None:
    fabricated = {
        "user": {"choice_selection": {"question_id": "made-up", "option_key": OPT_APPROVE}}
    }

    state = fold_creative_copy([("user", fabricated["user"])], dependency_digest=DIGEST)

    assert state == {}


def test_delegated_generation_does_not_approve_copy() -> None:
    question = authorship_question(target="opening_title", dependency_digest=DIGEST)

    state = fold_creative_copy(
        [
            ("assistant", {"choice_question": question}),
            _selection(question, OPT_GENERATE, delegated=True),
        ],
        dependency_digest=DIGEST,
    )["opening_title"]

    assert state.status == "authorship"
    assert state.approved is None


def test_stale_selection_from_previous_candidate_cannot_approve_revised_candidate() -> None:
    first = wording_question(target="opening_title", candidate="First", dependency_digest=DIGEST)
    second = wording_question(target="opening_title", candidate="Second", dependency_digest=DIGEST)
    state = fold_creative_copy(
        [
            ("assistant", {"choice_question": first}),
            ("assistant", {"choice_question": second}),
            _selection(first, OPT_APPROVE),
            _selection(second, OPT_REVISE),
        ],
        dependency_digest=DIGEST,
    )["opening_title"]

    assert state.status == "revise"
    assert state.approved is None


def test_creator_supplied_unicode_spacing_is_preserved_exactly() -> None:
    text = "  Gün  batımında  İstanbul · 夜  "
    state = fold_creative_copy(
        [
            (
                "assistant",
                {
                    "creative_copy_resolution": {
                        "target": "opening_title",
                        "dependency_digest": DIGEST,
                        "status": "creator_supplied",
                        "text": text,
                    }
                },
            )
        ],
        dependency_digest=DIGEST,
    )["opening_title"]

    assert state.status == "approved"
    assert state.approved == text
    assert state.provenance == "creator"


def test_malformed_candidate_digest_fails_closed() -> None:
    question = wording_question(
        target="opening_title", candidate="Safe title", dependency_digest=DIGEST
    )
    question["candidate_digest"] = "0" * 24

    state = fold_creative_copy(
        [("assistant", {"choice_question": question}), _selection(question, OPT_APPROVE)],
        dependency_digest=DIGEST,
    )["opening_title"]

    assert state.status == "revise"
    assert state.approved is None
    assert state.candidate == "Safe title"
