"""KRI-1515: "Make a montage from these clips." listed twice under "Have a look" on a montage.

Cusco DJI T5 (2026-11-17, thread 6b52ecd6, job 2207172d): the brief extractor filed the first
sentence as two asks with the same words, r1 ``style {edit_format: montage}`` and r2
``select {all: true}``. Neither had a checker, so the draft, the approval card and the finished
video's review each printed the sentence twice under "Have a look at these in the video",
although the plan was a montage of all 8 clips and the video played all 8.

The review tests drive the real ``_approved_generation_requirements_review`` with the recorded
brief, plan record, finished variant and strategy (`tests/fixtures/kri1515_cusco_t5_review.json`);
the draft tests run the real strategy facts, receipts and reply.

Ways this can go wrong (written before the code):

1. The format ask stays "can't verify" on a montage draft or video that IS a montage.
2. The format ask reads "Done" on a video of another format (a Talking edit asked to be a
   montage), or when the format is unknown.
3. The format check swallows another fact filed on the same row (``landscape_fit``) or answers
   for an editor turn, which names its changes from the edit diff.
4. "Use all the clips" reads "Done" when the plan or the finished video leaves a clip out
   (negative control: drop one clip from the recorded timeline; same sentence, same count of
   asks).
5. "Use all the clips" reads "Done" when the clips the creator attached are unknown, or when
   their ids do not match the timeline's at all (a different id format must not read as
   "0 of 8").
6. A Talking edit's "use all clips" (KRI-1405) changes.
7. Two receipts for one sentence still print twice, in "Done" lines or "Have a look" lines.
8. Merging hides a miss: a sentence with one part done and one part not reads "Done".
9. Merging joins different sentences.
10. A sentence with one part done and one part unchecked is listed both as "Done" and under
    "Have a look".
11. Turkish replies print the English label or the sentence twice.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import ResolvedCreatorManifest
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1515_cusco_t5_review.json"
_THREAD = uuid.UUID("6b52ecd6-657e-4cd7-bec3-1af22b996161")
_SENTENCE = "Make a montage from these clips."


def _load() -> dict:
    return json.loads(_FIXTURE.read_text())


def _media_ids(data: dict) -> list[str]:
    return [path.rsplit("/", 1)[-1] for path in data["clip_paths"]]


def _review(
    data: dict | None = None,
    *,
    snapshot: bool = True,
    language: str | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, dict[str, dict]]:
    """The real review text + receipts the chat would post for the recorded finished video."""
    data = data or _load()
    brief = CreativeBrief.model_validate(data["brief"])
    variant = copy.deepcopy(data["variant"])
    record = copy.deepcopy(data["unified_montage"])
    generation = str(record["generation_id"])
    job = SimpleNamespace(
        id=uuid.UUID("2207172d-78ca-4ee2-9a87-efb88da63db7"),
        assembly_plan={
            "unified_montage": record,
            "variants": [variant],
            DEVICE_RENDER_FIELD: {
                variant["variant_id"]: {
                    "published_attempt": variant["render_generation_id"],
                    "requirement_generation": generation,
                }
            },
        },
        all_candidates={"creator_strategy": copy.deepcopy(data["creator_strategy"])},
    )
    binding = BriefBinding.create(
        _THREAD,
        brief,
        media_snapshot={"clip_paths": list(data["clip_paths"])} if snapshot else None,
    )
    execution = SimpleNamespace(
        result={"brief_binding": binding.model_dump(mode="json")}, target_draft_id=None
    )
    monkeypatch.setattr(kria_runtime, "_source_fingerprints", lambda _job: {})
    monkeypatch.setattr(kria_runtime, "_with_clip_sound_toggles", lambda _j, _v, facts, **_: facts)
    with reply_language_for(language):
        text, receipts = kria_runtime._approved_generation_requirements_review(
            None, SimpleNamespace(id=_THREAD), job, variant, execution, "The edit is ready."
        )
    return text, {row["requirement_id"]: row for row in receipts}


def _without_clip(data: dict, media_id: str) -> dict:
    """The recorded video with every cut of ``media_id`` taken out (the negative control)."""
    data = copy.deepcopy(data)
    data["variant"]["story_timeline"] = [
        row for row in data["variant"]["story_timeline"] if row.get("media_id") != media_id
    ]
    data["unified_montage"]["clip_ids"] = [
        c for c in data["unified_montage"]["clip_ids"] if c != media_id
    ]
    return data


def _lines_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


# ── the finished video's review ──────────────────────────────────────────────────────


def test_recorded_review_says_the_montage_sentence_once_and_done(monkeypatch):
    data = _load()
    assert data["recorded_review"].count(_SENTENCE) == 2  # the incident

    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert (receipts["r1"]["status"], receipts["r1"]["verification"]) == ("met", "checked")
    assert (receipts["r2"]["status"], receipts["r2"]["verification"]) == ("met", "checked")
    assert _lines_with(text, _SENTENCE) == [f"- Done: {_SENTENCE}"]
    assert "Have a look" not in text


def test_a_clip_missing_from_the_video_is_said_once_and_not_done(monkeypatch):
    data = _load()
    dropped = _media_ids(data)[5]  # the hand on the wall
    text, receipts = _review(_without_clip(data, dropped), monkeypatch=monkeypatch)

    assert (receipts["r1"]["status"], receipts["r1"]["verification"]) == ("met", "checked")
    assert (receipts["r2"]["status"], receipts["r2"]["verification"]) == ("partial", "checked")
    assert receipts["r2"]["reason"] == "Left out 1 of your 8 clips"
    assert _lines_with(text, _SENTENCE) == [f"- Partly: {_SENTENCE} (Left out 1 of your 8 clips)"]


def test_unknown_attached_clips_claim_nothing_and_list_the_sentence_once(monkeypatch):
    text, receipts = _review(snapshot=False, monkeypatch=monkeypatch)

    assert receipts["r1"]["status"] == "met"
    assert receipts["r2"]["verification"] == "unchecked"
    # Part of the sentence is unchecked: it is listed once, to look at, never also as Done.
    assert _lines_with(text, _SENTENCE) == [f"- {_SENTENCE}"]
    assert text.index("Have a look") < text.index(_SENTENCE)


def test_attached_ids_that_match_nothing_in_the_video_claim_nothing(monkeypatch):
    data = _load()
    data["clip_paths"] = [f"users/x/other-format-{i}.mov" for i in range(8)]

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r2"]["verification"] == "unchecked"


def test_turkish_review_says_the_sentence_once(monkeypatch):
    text, receipts = _review(language="tr", monkeypatch=monkeypatch)

    assert receipts["r2"]["status"] == "met"
    assert _lines_with(text, _SENTENCE) == [f"- Yapıldı: {_SENTENCE}"]


# ── the draft and the approval card ──────────────────────────────────────────────────


def _manifest(media_ids: list[str]) -> ResolvedCreatorManifest:
    return ResolvedCreatorManifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        render_program="guided",
        media=[{"media_id": media_id, "kind": "video"} for media_id in media_ids],
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _draft(data: dict, *, strategy: dict | None = None, language: str | None = None):
    brief = CreativeBrief.model_validate(data["brief"])
    ids = _media_ids(data)
    facts = plan_facts_from_strategy(
        strategy or data["creator_strategy"], clip_ids=ids, manifest=_manifest(ids)
    )
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    with reply_language_for(language):
        return reply_from_receipts(brief, receipts), {r.requirement_id: r for r in receipts}


def test_draft_says_the_montage_sentence_once_and_done():
    data = _load()
    assert data["recorded_draft"].count(_SENTENCE) == 2  # the incident

    text, receipts = _draft(data)

    assert receipts["r1"].status == receipts["r2"].status == "met"
    assert _lines_with(text, _SENTENCE) == [f"- Done: {_SENTENCE}"]


def test_draft_that_leaves_clips_out_says_so():
    data = _load()
    strategy = copy.deepcopy(data["creator_strategy"])
    strategy.update(media_scope="selected", selected_media_ids=_media_ids(data)[:6])

    text, receipts = _draft(data, strategy=strategy)

    assert receipts["r2"].status == "partial"
    assert _lines_with(text, _SENTENCE) == [f"- Partly: {_SENTENCE} (Left out 2 of your 8 clips)"]


# ── the format ask ───────────────────────────────────────────────────────────────────


def _r1(**facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description=_SENTENCE,
        facts={"edit_format": "montage", **facts},
    )


def test_format_ask_is_done_only_on_that_format():
    assert check_requirement(_r1(), PlanFacts(edit_format="montage")).status == "met"
    assert check_requirement(_r1(), PlanFacts(rendered_montage=True)).status == "met"
    other = check_requirement(_r1(), PlanFacts(edit_format="subtitled", phone_talking=True))
    assert other.status != "met"
    unknown = check_requirement(_r1(), PlanFacts())
    assert unknown.status != "met"


def test_format_ask_never_swallows_another_fact_or_an_editor_turn():
    both = check_requirement(_r1(landscape_fit="fill"), PlanFacts(edit_format="montage"))
    assert both.status != "met"
    assert both.reason is not None and "crop" in both.reason
    editor = check_requirement(_r1(), PlanFacts(edit_format="montage", editor=True))
    assert editor.status != "met"


def test_talking_use_all_clips_keeps_its_own_answer():
    """KRI-1405: a Talking edit plays every clip as a speaker or a cutaway."""
    req = BriefRequirement(
        id="r2",
        kind="select",
        scope="global",
        description="use all five clips",
        facts={"all": True},
    )
    for count in (1, 5):
        talking = PlanFacts(edit_format="subtitled", phone_talking=True, video_clip_count=count)
        known = dataclasses.replace(talking, attached_media_count=count, unused_media_ids=("x",))
        before = build_receipts([req], talking, include_unchecked=True)
        assert build_receipts([req], known, include_unchecked=True) == before


# ── one row per sentence ──────────────────────────────────────────────────────────────


def _row(rid: str, text: str, kind: str = "style") -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=text)


def _receipt(rid: str, status: str, reason: str | None = None, *, checked: bool = True):
    return RequirementReceipt(
        requirement_id=rid,
        status=status,
        verification="checked" if checked else "unchecked",
        stage="checked" if checked else "understood",
    ).with_reason(reason)


def test_one_sentence_filed_twice_and_unchecked_is_listed_once():
    brief = CreativeBrief(
        version=1, requirements=[_row("r1", "Make it pop."), _row("r2", "Make it pop.")]
    )
    text = reply_from_receipts(
        brief, [_receipt("r1", "partial", checked=False), _receipt("r2", "partial", checked=False)]
    )
    assert _lines_with(text, "Make it pop.") == ["- Make it pop."]


def test_a_part_not_done_wins_and_different_sentences_stay_apart():
    brief = CreativeBrief(
        version=1,
        requirements=[
            _row("r1", "Use the clips in order.", "order"),
            _row("r2", "Use the clips in order.", "order"),
            _row("r3", "Use the clips in order!", "order"),
        ],
    )
    text = reply_from_receipts(
        brief,
        [
            _receipt("r1", "met"),
            _receipt("r2", "partial", "The last two clips swapped places"),
            _receipt("r3", "met"),
        ],
    )
    assert _lines_with(text, "Use the clips in order") == [
        "- Partly: Use the clips in order. (The last two clips swapped places)",
        "- Done: Use the clips in order!",
    ]
