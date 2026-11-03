"""What the creator reads after an intent review: receipts, one question, repair settlement.

Failure modes, written before the copy and its composition (matrix row in parentheses). Each has
a test.

  A "checked" claim nothing checked (4, 38):
    every line that says the video was checked needs the transport's own proof that the model
    received the frames; without it the composer raises and there is no fallback sentence. A
    settled repair's "I checked the new video" needs the FOLLOW-UP review's proof, not the
    parent's.
  A claim about what stills cannot show (5, 38):
    an audio ask (withheld) and an ask the model could not judge get no row and turn the
    headline from "everything I could see matches" into the version that names the limit. A
    low-confidence reading, "unmet" or "met", is a guess and not a verdict: it gets no row and
    does not support a clean headline. A "met" on a timing ask is never a check, whatever the
    stored finding says, because stills cannot show how long anything stays up. A review whose
    only passes are guesses has nothing to say. No sentence says "I listened" or "I heard".
  Wrong footage or a doubtful reading sold as a fix (22, 34):
    a suggestion says nothing changed and offers no question; the optional fix is named only
    when the repair was held back for a reason that leaves it sensible (not once the creator's
    own edits moved on); at most one question, never beside a staged repair; the question carries
    two fixed options and no wording from the model.
  A repair described as more than it is (31, 32):
    "the fix worked" only for a repair the follow-up met; an unconfirmed repair never says
    checked; a revert says the DRAFT was undone and that the video still shows the fix; a revert
    over newer edits claims no undo; the two past-tense fix lines are listed only where the fix
    is in the new video.
  Words the app cannot show (39):
    event types stay inside what old app builds render; every receipt row parses by the app's own
    rules (non-empty id, status met/partial/not_possible); the event always has text.
  One language per chat (13):
    every sentence has a Turkish form that differs from the English one, with no placeholder left
    over, and an unknown language reads English.
  Receipts that outlive their video (12):
    each row is stamped with the brief version and the render generation it judged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.kria.contracts import RequirementReceipt
from app.kria.intent_review_catalog import (
    Candidate,
    FrameRef,
    IssuedRequirement,
    ReviewCatalog,
    WithheldRequirement,
)
from app.kria.intent_review_copy import UncheckedReview, visual_inspection_proven
from app.kria.intent_review_posts import POSTABLE_STATUSES, compose_posts

REPO = Path(__file__).resolve().parents[5]
SWIFT_CHAT = REPO / "src/apps/ios/Kria/Features/ChatWorkspaceView.swift"
SWIFT_RECEIPTS = REPO / "src/apps/ios/Kria/Features/RequirementChips.swift"
GENERATION = "gen-7"
PROVEN = {
    "visual_input_received": True,
    "frames_sent": 3,
    "provider_outcome": "ok",
    "response_status": "completed",
}
UNPROVEN = {**PROVEN, "visual_input_received": False}
RESTORE = Candidate(
    candidate_id="c-restore",
    kind="restore_exact_text",
    requirement_ids=["R1"],
    defect_kinds=["copy_mismatch", "text_missing"],
    element_ref="title",
    role="title",
    allowed_paths=["/text_elements/title/text"],
    target={"text": "Before"},
    auto=True,
)
FIT = Candidate(
    candidate_id="c-fit",
    kind="fit_text_to_duration",
    requirement_ids=["R2"],
    defect_kinds=["text_cut_off"],
    element_ref="caption-1",
    role="caption",
    allowed_paths=["/text_elements/caption-1/end_s"],
    target={"start_s": 0.0, "end_s": 1.3},
    auto=True,
)
MOVE = Candidate(
    candidate_id="c-move",
    kind="move_text_to_safe_anchor",
    requirement_ids=["R3"],
    defect_kinds=["text_obstructed"],
    element_ref="pin-1",
    role="pin",
    allowed_paths=["/text_elements/pin-1/position"],
    target={"anchor": "top"},
    auto=False,
)


def requirement(alias, brief_id, kind="text"):
    return IssuedRequirement(
        requirement_id=alias, brief_id=brief_id, kind=kind, label=f"ask {brief_id}"
    )


def catalog(*requirements, withheld=(), candidates=()):
    return ReviewCatalog(
        digest="d" * 8,
        frames=[FrameRef(frame_id="f0", t_ms=0, sha256="a" * 64, w=360, h=640)],
        requirements=list(requirements),
        withheld=[WithheldRequirement(brief_id=b, reason=r) for b, r in withheld],
        candidates=list(candidates),
    )


def finding(alias, verdict="met", defect=None, confidence="high"):
    return {
        "requirement_id": alias,
        "verdict": verdict,
        "defect_kind": defect,
        "evidence_frame_ids": ["f0"] if verdict != "unverifiable" else [],
        "confidence": confidence,
    }


def review(cat, findings, disposition=None, *, provenance=PROVEN, repair=None, comparison=None):
    return {
        "catalog": cat.model_dump(mode="json"),
        "findings": findings,
        "disposition": disposition
        or {
            "outcome": "no_findings",
            "repair_candidate_ids": [],
            "suggestions": [],
            "question": None,
            "ignored": [],
        },
        "provenance": provenance,
        **({"repair": repair} if repair else {}),
        **({"comparison": comparison} if comparison else {}),
    }


def disposition(outcome, *, repairs=(), suggestions=(), question=None, ignored=()):
    return {
        "outcome": outcome,
        "repair_candidate_ids": list(repairs),
        "suggestions": [
            {"requirement_id": a, "candidate_id": c, "why": w} for a, c, w in suggestions
        ],
        "question": {"requirement_id": question} if question else None,
        "ignored": list(ignored),
    }


def posts(status, reason, rev, *, language=None, child=None, brief_version=3):
    return compose_posts(
        review_id="rev-1",
        status=status,
        reason=reason,
        review=rev,
        brief_version=brief_version,
        generation_id=GENERATION,
        child_review=child,
        language=language,
    )


CLEAN = catalog(requirement("R1", "r1"), requirement("R2", "r2", "select"))
ALL_MET = review(CLEAN, [finding("R1"), finding("R2")])


def lead(post):
    return post.payload.get("reply_lead") or post.content


def rows(post):
    return {r["requirement_id"]: r for r in post.payload.get("requirement_receipts", [])}


def every_text(post):
    out = [post.content, post.payload.get("reply_lead") or "", *post.payload.get("options", [])]
    out += post.payload.get("also_changed", [])
    out += [r.get("reason") or "" for r in post.payload.get("requirement_receipts", [])]
    return [t for t in out if t]


# --- the honesty key (4, 38) ------------------------------------------------------------


def test_the_proof_is_the_transports_flag_and_nothing_inferred():
    assert visual_inspection_proven({"provenance": PROVEN})
    assert not visual_inspection_proven({"provenance": UNPROVEN})
    assert not visual_inspection_proven({"provenance": {"frames_sent": 3}})
    assert not visual_inspection_proven({"provenance": {**PROVEN, "visual_input_received": "yes"}})
    assert not visual_inspection_proven({})
    assert not visual_inspection_proven({"provenance": None})


@pytest.mark.parametrize(
    "status,rev",
    [
        ("no_findings", review(CLEAN, [finding("R1"), finding("R2")], provenance=UNPROVEN)),
        (
            "suggestion",
            review(
                CLEAN,
                [finding("R1"), finding("R2", "unmet", "wrong_content")],
                disposition("suggestion", suggestions=[("R2", None, "no_candidate")]),
                provenance=UNPROVEN,
            ),
        ),
        (
            "needs_confirmation",
            review(
                CLEAN,
                [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
                disposition("needs_confirmation", question="R1"),
                provenance={},
            ),
        ),
        (
            "repair_staged",
            review(
                catalog(requirement("R1", "r1"), candidates=[RESTORE]),
                [finding("R1", "unmet", "copy_mismatch")],
                disposition("repair", repairs=["c-restore"]),
                provenance=UNPROVEN,
                repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore"]},
            ),
        ),
    ],
)
def test_no_sentence_says_checked_without_the_proof_that_frames_were_received(status, rev):
    with pytest.raises(UncheckedReview):
        posts(status, "reviewed", rev)


@pytest.mark.parametrize(
    "status,reason",
    [
        ("repair_verified", "fixed"),
        ("repair_reverted", "worse"),
        ("repair_reverted", "worse_head_moved"),
    ],
)
def test_a_settled_repair_that_claims_a_check_needs_the_follow_ups_proof_not_the_parents(
    status, reason
):
    parent = review(
        catalog(requirement("R1", "r1"), candidates=[RESTORE]),
        [finding("R1", "unmet", "copy_mismatch")],
        repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore"]},
    )
    follow_up = review(CLEAN, [finding("R1")])
    assert posts(status, reason, parent, child=follow_up)
    with pytest.raises(UncheckedReview):
        posts(status, reason, parent, child={**follow_up, "provenance": UNPROVEN})
    with pytest.raises(UncheckedReview):
        posts(status, reason, parent, child=None)


def test_an_unconfirmed_repair_says_so_and_never_claims_a_check():
    parent = review(
        catalog(requirement("R1", "r1"), candidates=[RESTORE]),
        [finding("R1", "unmet", "copy_mismatch")],
        repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore"]},
    )
    (post,) = posts("repair_unverified", "rereview_unavailable", parent, child=None)
    assert "couldn't confirm" in lead(post)
    assert "checked" not in " ".join(every_text(post)).lower().replace("not checked", "")
    assert rows(post)["r1"]["status"] == "partial"
    assert rows(post)["r1"]["reason"] == "Fixed, but not confirmed."


# --- what stills cannot show (5, 38) ------------------------------------------------------


def test_everything_judged_gets_the_plain_headline_and_a_row_per_met_ask():
    (post,) = posts("no_findings", "reviewed", ALL_MET)
    assert post.event_type == "assistant_response" and post.kind == "receipt"
    assert lead(post) == (
        "I checked the finished video against your request. Everything I could see matches."
    )
    assert set(rows(post)) == {"r1", "r2"}
    assert {r["status"] for r in rows(post).values()} == {"met"}
    assert {r["reason"] for r in rows(post).values()} == {"Checked in the video."}


@pytest.mark.parametrize(
    "cat,findings",
    [
        pytest.param(
            catalog(requirement("R1", "r1"), withheld=[("r9", "audio")]),
            [finding("R1")],
            id="an_audio_ask_was_withheld",
        ),
        pytest.param(
            catalog(requirement("R1", "r1"), requirement("R2", "r2", "timing")),
            [finding("R1"), finding("R2", "unverifiable")],
            id="the_model_could_not_judge_one",
        ),
        pytest.param(
            catalog(requirement("R1", "r1"), requirement("R2", "r2", "select")),
            [finding("R1"), finding("R2", confidence="low")],
            id="a_low_confidence_met_is_a_guess_not_a_pass",
        ),
        pytest.param(
            catalog(requirement("R1", "r1"), requirement("R2", "r2", "timing")),
            [finding("R1"), finding("R2")],
            id="a_met_on_a_timing_ask_is_not_a_check",
        ),
    ],
)
def test_anything_stills_cannot_judge_names_the_limit_and_gets_no_row(cat, findings):
    (post,) = posts("no_findings", "reviewed", review(cat, findings))
    assert lead(post) == (
        "I checked frames from the finished video against your request. Everything I could "
        "see matches. I can't judge sound or motion from still frames."
    )
    assert set(rows(post)) == {"r1"}


@pytest.mark.parametrize("language", [None, "tr"])
def test_a_low_confidence_unmet_is_a_doubt_the_creator_hears_about_not_an_all_clear(language):
    cat = catalog(requirement("R1", "r1"), requirement("R2", "r2", "select"))
    rev = review(
        cat,
        [finding("R1"), finding("R2", "unmet", "wrong_content", "low")],
        disposition("no_findings", ignored=["R2"]),
    )
    (post,) = posts("no_findings", "reviewed", rev, language=language)
    assert "Everything I could see matches" not in lead(post)
    assert "Görebildiğim her şey uyuyor" not in lead(post)
    assert rows(post)["r1"]["status"] == "met"
    assert rows(post)["r2"]["status"] == "partial"
    assert rows(post)["r2"]["reason"] == (
        "Videoda uyuşmuyor olabilir. Hiçbir şey değişmedi."
        if language == "tr"
        else "May not match in the video. Nothing changed."
    )


def test_a_medium_confidence_pass_is_still_a_checked_pass_only_low_is_ignored():
    medium = review(CLEAN, [finding("R1", confidence="medium"), finding("R2")])
    (post,) = posts("no_findings", "reviewed", medium)
    assert set(rows(post)) == {"r1", "r2"} and "frames" not in lead(post)


@pytest.mark.parametrize(
    "cat,findings",
    [
        pytest.param(
            CLEAN,
            [finding("R1", confidence="low"), finding("R2", confidence="low")],
            id="every_pass_is_a_guess",
        ),
        pytest.param(
            catalog(requirement("R1", "r1", "timing")),
            [finding("R1")],
            id="the_only_pass_is_on_a_timing_ask",
        ),
    ],
)
def test_a_review_whose_only_passes_cannot_be_counted_has_nothing_to_say(cat, findings):
    assert posts("no_findings", "reviewed", review(cat, findings)) == []


def test_a_guessed_pass_beside_a_real_doubt_gets_no_checked_row():
    rev = review(
        CLEAN,
        [finding("R1", confidence="low"), finding("R2", "unmet", "wrong_content")],
        disposition("suggestion", suggestions=[("R2", None, "no_candidate")]),
    )
    (post,) = posts("suggestion", "reviewed", rev)
    assert set(rows(post)) == {"r2"} and rows(post)["r2"]["status"] == "partial"


def test_no_sentence_claims_to_have_listened_in_either_language():
    cases = [
        ("no_findings", "reviewed", ALL_MET),
        (
            "suggestion",
            "reviewed",
            review(
                CLEAN,
                [finding("R1"), finding("R2", "unmet", "wrong_content")],
                disposition("suggestion", suggestions=[("R2", None, "no_candidate")]),
            ),
        ),
    ]
    for language in ("en", "tr"):
        for status, reason, rev in cases:
            for post in posts(status, reason, rev, language=language):
                text = " ".join(every_text(post)).lower()
                assert not re.search(r"listen|heard|hear\b|dinle|duy", text), text


# --- wrong footage and doubtful readings (22, 34) -----------------------------------------


def test_wrong_footage_is_a_suggestion_with_no_question_and_no_fix_on_offer():
    rev = review(
        CLEAN,
        [finding("R1"), finding("R2", "unmet", "wrong_content")],
        disposition("suggestion", suggestions=[("R2", None, "no_candidate")]),
    )
    (post,) = posts("suggestion", "reviewed", rev)
    assert lead(post) == (
        "I checked the finished video. Something may not match your request. "
        "I didn't change anything."
    )
    assert rows(post)["r1"]["status"] == "met"
    assert rows(post)["r2"]["status"] == "partial"
    assert rows(post)["r2"]["reason"] == "May not match in the video. Nothing changed."
    assert "options" not in post.payload and post.kind == "receipt"


@pytest.mark.parametrize(
    "why,offered",
    [
        ("medium_confidence", True),
        ("tier_review", True),
        ("candidate_not_auto", True),
        ("repair_cap", True),
        ("head_moved", False),
        ("thread_busy", False),
        ("protected", False),
    ],
)
def test_the_optional_fix_is_named_only_when_it_is_still_a_sensible_offer(why, offered):
    cat = catalog(requirement("R1", "r1"), candidates=[RESTORE])
    rev = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch", "medium")],
        disposition("suggestion", suggestions=[("R1", "c-restore", why)]),
    )
    (post,) = posts("suggestion", "reviewed", rev)
    reason = rows(post)["r1"]["reason"]
    assert reason.startswith("May not match in the video. Nothing changed.")
    assert ("Optional: put your exact wording back on the title." in reason) is offered


def test_every_optional_fix_has_its_own_sentence_in_both_languages():
    cat = catalog(
        requirement("R1", "r1"),
        requirement("R2", "r2"),
        requirement("R3", "r3", "style"),
        candidates=[RESTORE, FIT, MOVE],
    )
    rev = review(
        cat,
        [
            finding("R1", "unmet", "copy_mismatch", "medium"),
            finding("R2", "unmet", "text_cut_off", "medium"),
            finding("R3", "unmet", "text_obstructed", "high"),
        ],
        disposition(
            "suggestion",
            suggestions=[
                ("R1", "c-restore", "medium_confidence"),
                ("R2", "c-fit", "medium_confidence"),
                ("R3", "c-move", "candidate_not_auto"),
            ],
        ),
    )
    (en,) = posts("suggestion", "reviewed", rev, language="en")
    (tr,) = posts("suggestion", "reviewed", rev, language="tr")
    assert rows(en)["r2"]["reason"].endswith(
        "Optional: shorten the caption so it ends before the video does."
    )
    assert rows(en)["r3"]["reason"].endswith(
        "Optional: move the pinned text to the top so it's easier to read."
    )
    assert rows(tr)["r1"]["reason"].endswith("İstersen başlık tam yazdığın hâline döndürülebilir.")
    assert rows(tr)["r2"]["reason"].endswith(
        "İstersen altyazı video bitmeden bitecek şekilde kısaltılabilir."
    )
    assert rows(tr)["r3"]["reason"].endswith(
        "İstersen sabit yazı daha kolay okunsun diye üste taşınabilir."
    )


def test_a_wording_doubt_with_no_authority_is_a_receipt_then_one_two_option_question():
    rev = review(
        CLEAN,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition("needs_confirmation", question="R1"),
    )
    receipt, question = posts("needs_confirmation", "reviewed", rev)
    assert (receipt.kind, receipt.event_type) == ("receipt", "assistant_response")
    assert rows(receipt)["r1"]["reason"] == "May not match in the video. Nothing changed."
    assert (question.kind, question.event_type) == ("question", "assistant_question")
    assert question.content == (
        "The text on screen doesn't match what you asked for. Want me to change it?"
    )
    assert question.payload["options"] == ["Change it", "Keep it"]
    assert question.payload["recommended_option"] == "Change it"
    assert question.payload["requirement_id"] == "r1"
    assert "requirement_receipts" not in question.payload


@pytest.mark.parametrize(
    ("language", "text", "change"),
    [
        (
            None,
            "The text on screen doesn't match what you asked for. "
            'Want me to change it to "İstanbul"?',
            'Change it to "İstanbul"',
        ),
        (
            "tr",
            'Ekrandaki yazı istediğinle uyuşmuyor. "İstanbul" olarak değiştireyim mi?',
            '"İstanbul" olarak değiştir',
        ),
    ],
)
def test_the_question_names_the_words_so_a_bare_tap_authorises_exactly_that(language, text, change):
    cat = catalog(
        IssuedRequirement(
            requirement_id="R1", brief_id="r1", kind="text", label="title", literal="İstanbul"
        ),
        requirement("R2", "r2", "select"),
    )
    rev = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition("needs_confirmation", question="R1"),
    )
    _, question = posts("needs_confirmation", "reviewed", rev, language=language)
    assert question.content == text
    assert question.payload["options"][0] == change
    assert question.payload["recommended_option"] == change


def test_a_staged_repair_posts_a_receipt_and_never_a_question():
    cat = catalog(requirement("R1", "r1"), requirement("R2", "r2", "select"), candidates=[RESTORE])
    rev = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition(
            "repair", repairs=["c-restore"], question="R1"
        ),  # even a corrupt disposition cannot add a question beside a repair
        repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore"]},
    )
    (post,) = posts("repair_staged", "awaiting_approval", rev)
    assert post.kind == "receipt"
    assert lead(post) == (
        "I checked the finished video against your request. One thing didn't match, and a fix "
        "is waiting for your approval."
    )
    assert rows(post)["r1"]["status"] == "partial"
    assert rows(post)["r1"]["reason"] == "Fix waiting for your approval."
    assert rows(post)["r2"]["reason"] == "Checked in the video."


# --- a settled repair (31, 32) ------------------------------------------------------------

PARENT = review(
    catalog(requirement("R1", "r1"), requirement("R2", "r2", "select"), candidates=[RESTORE, FIT]),
    [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
    repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore", "c-fit"]},
)
FOLLOW_UP = review(CLEAN, [finding("R1"), finding("R2")])


def test_a_verified_repair_says_so_names_the_fix_once_and_keeps_the_undo():
    (post,) = posts("repair_verified", "fixed", PARENT, child=FOLLOW_UP)
    assert lead(post) == "I checked the new video: the fix worked. You can still undo it."
    assert set(rows(post)) == {"r1"}
    assert rows(post)["r1"]["status"] == "met"
    assert rows(post)["r1"]["reason"] == "Fixed and checked again."
    assert post.payload["also_changed"] == [
        "Restored your exact wording on the title.",
        "Shortened the caption so it ends before the video does.",
    ]


def test_an_unconfirmed_repair_keeps_the_fix_lines_because_the_fix_is_in_the_new_video():
    (post,) = posts("repair_unverified", "not_confirmed", PARENT, child=FOLLOW_UP)
    assert lead(post) == (
        "The fix is in the new video, but I couldn't confirm it worked. You can still undo it."
    )
    assert post.payload["also_changed"][0] == "Restored your exact wording on the title."


def test_a_reverted_repair_says_the_draft_was_undone_and_the_video_still_shows_the_fix():
    (post,) = posts("repair_reverted", "worse", PARENT, child=FOLLOW_UP)
    assert post.kind == "receipt"
    assert lead(post) == (
        "The fix made the video worse, so I undid it in your draft. The video still shows the "
        "fixed version. Ask me to render your previous version if you want it."
    )
    assert rows(post)["r1"]["status"] == "partial"
    assert rows(post)["r1"]["reason"] == "Fix undone in your draft."
    assert "also_changed" not in post.payload
    text = " ".join(every_text(post)).lower()
    assert "is back" not in text and "restored the video" not in text


def test_a_revert_over_newer_edits_claims_no_undo_and_no_rows():
    (post,) = posts("repair_reverted", "worse_head_moved", PARENT, child=FOLLOW_UP)
    assert post.content == (
        "The fix may have made the video worse. Your newer edits are untouched; you can undo "
        "the fix from its card."
    )
    assert "requirement_receipts" not in post.payload and "also_changed" not in post.payload
    assert "undid" not in post.content


def test_the_two_voices_of_one_fix_line_follow_the_candidate_kind_and_role():
    (en,) = posts("repair_verified", "fixed", PARENT, child=FOLLOW_UP, language="en")
    (tr,) = posts("repair_verified", "fixed", PARENT, child=FOLLOW_UP, language="tr")
    assert tr.payload["also_changed"] == [
        "Başlık tam yazdığın hâline döndü.",
        "Altyazı video bitmeden bitecek şekilde kısaldı.",
    ]
    assert len(en.payload["also_changed"]) == len(tr.payload["also_changed"]) == 2


# --- the surface old app builds render (39) ----------------------------------------------


def _swift_allowlist():
    block = re.search(
        r"conversationalAssistantEvents: Set<String> = \[(.*?)\]",
        SWIFT_CHAT.read_text(encoding="utf-8"),
        re.S,
    )
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def _swift_outcomes():
    source = SWIFT_RECEIPTS.read_text(encoding="utf-8")
    block = re.search(r"enum RequirementOutcome: String.*?\{(.*?)/// Plain words", source, re.S)
    return {raw or name for name, raw in re.findall(r'case (\w+)(?: = "(\w+)")?', block.group(1))}


def _every_scenario():
    cat = catalog(requirement("R1", "r1"), requirement("R2", "r2", "select"), candidates=[RESTORE])
    suggestion = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition("suggestion", suggestions=[("R1", "c-restore", "medium_confidence")]),
    )
    question = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition("needs_confirmation", question="R1"),
    )
    staged = review(
        cat,
        [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
        disposition("repair", repairs=["c-restore"]),
        repair={"targeted_brief_ids": ["r1"], "candidate_ids": ["c-restore"]},
    )
    return [
        ("no_findings", "reviewed", ALL_MET, None),
        ("suggestion", "reviewed", suggestion, None),
        ("needs_confirmation", "reviewed", question, None),
        ("repair_staged", "awaiting_approval", staged, None),
        ("repair_verified", "fixed", staged, FOLLOW_UP),
        ("repair_unverified", "not_confirmed", staged, FOLLOW_UP),
        ("repair_reverted", "worse", staged, FOLLOW_UP),
        ("repair_reverted", "worse_head_moved", staged, FOLLOW_UP),
    ]


def test_every_posted_event_is_a_type_old_app_builds_render_and_every_row_parses():
    allowed, outcomes = _swift_allowlist(), _swift_outcomes()
    assert {"assistant_response", "assistant_question"} <= allowed
    assert outcomes == {"met", "partial", "not_possible"}
    seen = set()
    for status, reason, rev, child in _every_scenario():
        for post in posts(status, reason, rev, child=child):
            seen.add(post.event_type)
            assert post.event_type in allowed
            assert post.content.strip()
            assert post.payload["turn_value"] == "intent_review"
            assert post.payload["schema_version"] == 1
            assert post.payload["render_generation_id"] == GENERATION
            for row in post.payload.get("requirement_receipts", []):
                assert row["requirement_id"].strip() and row["status"] in outcomes
                RequirementReceipt.model_validate(row)
                assert len(row["reason"]) <= 300
                assert row["verification"] == "checked" and row["stage"] == "checked"
                assert row["brief_version"] == 3 and row["generation_id"] == GENERATION
    assert seen == {"assistant_response", "assistant_question"}


def test_a_reply_lead_is_only_ever_sent_with_rows_it_can_sit_above():
    for status, reason, rev, child in _every_scenario():
        for post in posts(status, reason, rev, child=child):
            if "reply_lead" in post.payload:
                assert post.payload["requirement_receipts"]
                assert post.payload["reply_lead"] == post.content


def test_only_the_postable_outcomes_have_anything_to_say():
    cat = catalog(requirement("R1", "r1"))
    rev = review(cat, [finding("R1")])
    for status in (
        "unavailable",
        "stale",
        "exhausted",
        "review_unknown",
        "repair_abandoned",
        "pending",
        "evidence_ready",
        "reviewing",
    ):
        assert status not in POSTABLE_STATUSES
        assert posts(status, "x", rev) == []
    assert POSTABLE_STATUSES == {
        "no_findings",
        "suggestion",
        "needs_confirmation",
        "repair_staged",
        "repair_verified",
        "repair_unverified",
        "repair_reverted",
    }


# --- one language per chat (13) ----------------------------------------------------------


def test_every_sentence_has_a_turkish_form_with_no_placeholder_left_over():
    for status, reason, rev, child in _every_scenario():
        en = posts(status, reason, rev, child=child, language="en")
        tr = posts(status, reason, rev, child=child, language="tr")
        assert [p.kind for p in en] == [p.kind for p in tr]
        for english, turkish in zip(en, tr, strict=True):
            assert english.payload.keys() == turkish.payload.keys()
            assert [
                r["requirement_id"] for r in english.payload.get("requirement_receipts", [])
            ] == [r["requirement_id"] for r in turkish.payload.get("requirement_receipts", [])]
            for a, b in zip(every_text(english), every_text(turkish), strict=True):
                assert a != b and b.strip()
                assert not re.search(r"[{}]", a + b)


def test_an_unknown_language_reads_english_and_turkish_uses_dotted_capitals():
    assert posts("no_findings", "reviewed", ALL_MET, language=None) == posts(
        "no_findings", "reviewed", ALL_MET, language="de"
    )
    (tr,) = posts("no_findings", "reviewed", ALL_MET, language="tr")
    assert lead(tr) == "Bitmiş videoyu isteğinle karşılaştırdım. Görebildiğim her şey uyuyor."
    assert {r["reason"] for r in rows(tr).values()} == {"Videoda kontrol edildi."}
    question = posts(
        "needs_confirmation",
        "reviewed",
        review(
            CLEAN,
            [finding("R1", "unmet", "copy_mismatch"), finding("R2")],
            disposition("needs_confirmation", question="R1"),
        ),
        language="tr",
    )[1]
    assert question.payload["options"] == ["Değiştir", "Böyle kalsın"]


# --- a review the server cannot read posts nothing --------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("catalog"),
        lambda r: r.pop("findings"),
        lambda r: r.update(findings=[{"requirement_id": "R1"}]),
        lambda r: r.update(disposition={"outcome": "nonsense"}),
    ],
)
def test_a_review_that_cannot_be_read_back_raises_instead_of_guessing(mutate):
    rev = {**ALL_MET}
    mutate(rev)
    with pytest.raises(UncheckedReview):
        posts("no_findings", "reviewed", rev)
