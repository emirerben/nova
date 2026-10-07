from app.services.creative_copy_decisions import (
    OPT_APPROVE,
    OPT_GENERATE,
    fold_creative_copy,
    media_digest,
    wording_question,
)


def test_only_matching_server_question_can_approve_exact_candidate() -> None:
    digest = media_digest({"clip_assignments": [{"media_id": "a", "generation": "1"}]})
    question = wording_question(
        target="opening_title", candidate="Wait for it", dependency_digest=digest
    )
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": OPT_APPROVE,
                }
            },
        ),
    ]
    assert (
        fold_creative_copy(events, dependency_digest=digest)["opening_title"].approved
        == "Wait for it"
    )


def test_forged_or_stale_approval_never_survives() -> None:
    digest = media_digest({"clip_assignments": [{"media_id": "a", "generation": "1"}]})
    question = wording_question(
        target="opening_title", candidate="Wait for it", dependency_digest=digest
    )
    forged = [
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": OPT_APPROVE,
                }
            },
        )
    ]
    assert not fold_creative_copy(forged, dependency_digest=digest)
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": OPT_APPROVE,
                }
            },
        ),
    ]
    assert (
        fold_creative_copy(events, dependency_digest="different")["opening_title"].status == "stale"
    )


def test_generate_is_not_approval() -> None:
    digest = media_digest(None)
    question = {
        "question_id": "q",
        "conflict": "creative_copy",
        "kind": "creative_copy_authorship",
        "creative_target": "opening_title",
        "dependency_digest": digest,
        "options": [{"key": OPT_GENERATE}],
    }
    events = [
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": {"question_id": "q", "option_key": OPT_GENERATE}}),
    ]
    state = fold_creative_copy(events, dependency_digest=digest)["opening_title"]
    assert state.approved is None and state.candidate is None


def test_persisted_wording_question_is_blocking_and_replacement_clears_approval() -> None:
    digest = media_digest({"clip_paths": ["owned/a.mp4"]})
    first = wording_question(target="opening_title", candidate="First", dependency_digest=digest)
    second = wording_question(target="opening_title", candidate="Second", dependency_digest=digest)
    events = [
        ("assistant", {"choice_question": first}),
        (
            "user",
            {"choice_selection": {"question_id": first["question_id"], "option_key": OPT_APPROVE}},
        ),
        ("assistant", {"choice_question": second}),
    ]
    state = fold_creative_copy(events, dependency_digest=digest)["opening_title"]
    assert state.status == "revise" and state.candidate == "Second" and state.approved is None
