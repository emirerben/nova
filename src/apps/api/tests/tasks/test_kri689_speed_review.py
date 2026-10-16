"""KRI-689: the render-ready review of a phone montage says "Couldn't" for a speed ask.

Prod thread 1390E686 (2026-10-15, stress kit M2): "Speed the river shots up 2x" was brief
requirement r4 [style/global]. The finished phone video played every clip at 1x, and the
review listed r4 under "Have a look at these in the video" next to asks nobody could judge,
as if the creator should find the speed in the video. It must be a plain "Couldn't" with the
reason, in the chat's language, and it must not turn the review into a failure notice or
change what the other asks say.

Two stored shapes reach the review: a record written by the fixed worker (r4 already
`not_possible`) and one written before the fix (r4 `unchecked`). Both must read the same.
No database: `_approved_generation_review` only reads the thread id, the binding, the job's
plan and the variant.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.reply_language import reply_language_for
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime

THREAD = uuid.UUID("1390e686-2702-476d-995c-50ab657196de")
GENERATION = "6c3e26292db04e29a32a7446495ce494"  # assembly_plan.creator_generation_id
ATTEMPT = "a71d75c2-6e92-45f8-9ed6-133197de103b"  # the phone's published upload attempt
DEFAULT = "The confirmed edit is ready. Watch it and tell me what feels right or what you want."
SPEED = "Speed the river shots up 2x"
EN_REASON = "I can't change clip speed on your iPhone yet, so the clips play at normal speed"
TR_REASON = "iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor"

CLIPS = [
    "analysis-proxy-ios-E860F8C1-2BBA-4D8C-93F1-1787AE56DF68.mp4",  # the short arrival clip
    "analysis-proxy-ios-88F0B87C-8687-4D9C-A7F6-C5EA8143DB8A.mp4",
    "analysis-proxy-ios-3A2C36E1-A46A-4060-8217-862653FE6F60.mp4",
    "analysis-proxy-ios-7A793029-DF50-4DBB-8D9F-D8E2D66F527E.mp4",
]
ARRIVAL, *RIVER = CLIPS


def _brief(speed: str = SPEED) -> CreativeBrief:
    """The prod brief (version 2), verbatim except the speed wording."""
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="title",
                description=None,
                literal="20 minutes in the dark",
            ),
            BriefRequirement(
                id="r2",
                kind="timing",
                scope="global",
                description="20 seconds",
                facts={"duration_s": 20},
            ),
            BriefRequirement(
                id="r3",
                kind="select",
                scope="global",
                description=(
                    "pick the moments where a lit house, the ferry or the amusement park passes "
                    "— eight of them, about 2 seconds each, no black-sky stretches"
                ),
            ),
            BriefRequirement(id="r4", kind="style", scope="global", description=speed),
            BriefRequirement(
                id="r5",
                kind="audio",
                scope="global",
                description="Keep every clip's own sound without any music",
            ),
            BriefRequirement(
                id="r6",
                kind="order",
                scope="global",
                description="End on the short arrival clip where I say tam 20 dakika sürdü",
                facts={"last_clip": "the short arrival clip where I say tam 20 dakika sürdü"},
            ),
        ],
    )


def _record() -> dict:
    """`assembly_plan.unified_montage` as the phone montage worker stored it."""
    return {
        "version": 1,
        "brief_version": 2,
        "generation_id": GENERATION,
        "clip_ids": [*RIVER, ARRIVAL],
        "title": "20 minutes in the dark",
        "title_source": "creator",
        "duration_s": 20.0,
        "ordering_basis": "attachment",
        "ordering_fallback_clip_ids": [],
        "clip_durations_s": {clip: 90.0 for clip in RIVER} | {ARRIVAL: 16.0},
        "best_parts": [
            {"media_id": clip, "source_start_s": 0.0, "source_end_s": 5.0, "score": 0.0}
            for clip in (*RIVER, ARRIVAL)
        ],
        "intent_outcomes": [
            {
                "op": "order",
                "code": "landed",
                "name": "last: the short arrival clip where I say tam 20 dakika sürdü",
                "reason": None,
                "status": "met",
                "position": "last",
            }
        ],
    }


def _stamped(receipts) -> list[dict]:  # noqa: ANN001
    return [
        {**receipt.model_dump(mode="json"), "brief_version": 2, "generation_id": GENERATION}
        for receipt in receipts
    ]


def _fixed_worker_record(brief: CreativeBrief) -> dict:
    """The record the render worker writes now: receipts built from the plan it made."""
    record = _record()
    record["requirement_receipts"] = _stamped(
        build_receipts(
            brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
        )
    )
    return record


def _old_worker_record(brief: CreativeBrief) -> dict:
    """A record from before the fix: r4 (like r3 and r5) was stored as unchecked."""
    record = _fixed_worker_record(brief)
    for row in record["requirement_receipts"]:
        if row["requirement_id"] == "r4":
            row.update(
                status="partial",
                verification="unchecked",
                stage="understood",
                reason="I can't verify this one automatically yet.",
            )
    return record


def _variant() -> dict:
    timeline, at = [], 0.0
    for clip in (*RIVER, ARRIVAL):
        timeline.append(
            {
                "kind": "video",
                "lane": "clip",
                "media_id": clip,
                "source_start_s": 0.0,
                "source_end_s": 5.0,
                "output_start_s": at,
                "output_end_s": at + 5.0,
            }
        )
        at += 5.0
    return {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_destination": "device",
        "render_generation_id": ATTEMPT,
        "resolved_archetype": "guided_story",
        "edit_duration_s": 20.0,
        "duration_s": 21.6,
        "brand_tail_s": 1.6,
        "source_audio_preserved": True,
        "text_elements": [
            {
                "id": "guided-title",
                "role": "generative_intro",
                "text": "20 minutes in the dark",
                "start_s": 0.0,
                "end_s": 2.2,
            }
        ],
        "story_timeline": timeline,
    }


def _review(record: dict, brief: CreativeBrief, *, lang: str = "en"):
    plan = {
        "creator_generation_id": GENERATION,
        "unified_montage": record,
        DEVICE_RENDER_FIELD: {
            "guided_story": {
                "published_attempt": ATTEMPT,
                "base_generation": ATTEMPT,
                "requirement_generation": GENERATION,
            }
        },
    }
    binding = BriefBinding.create(THREAD, brief).model_dump(mode="json")
    thread = SimpleNamespace(id=THREAD, creator_id=uuid.uuid4())
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=plan, all_candidates={})
    execution = SimpleNamespace(result={"brief_binding": binding})
    with reply_language_for(lang):
        text, payload = kria_runtime._approved_generation_review(
            None, thread, job, _variant(), execution, DEFAULT
        )
    return text, {row["requirement_id"]: row for row in payload}


def _look_section(text: str) -> str:
    return text.split("Have a look at these in the video:")[1]


@pytest.mark.parametrize("make_record", [_fixed_worker_record, _old_worker_record])
def test_the_speed_ask_is_a_couldnt_with_the_reason_not_a_have_a_look(make_record):
    brief = _brief()
    text, receipts = _review(make_record(brief), brief)

    assert f"- Couldn't: {SPEED} ({EN_REASON})" in text
    # The asks nobody can judge keep today's calm list; speed is no longer in it.
    assert "Have a look at these in the video:" in text
    assert SPEED not in _look_section(text)
    assert receipts["r4"]["status"] == "not_possible"
    assert receipts["r4"]["verification"] == "checked"
    assert receipts["r4"]["reason"] == EN_REASON
    assert receipts["r4"]["generation_id"] == ATTEMPT
    assert "Not everything you asked for made it in" not in text


@pytest.mark.parametrize("make_record", [_fixed_worker_record, _old_worker_record])
def test_the_turkish_review_says_it_in_turkish(make_record):
    brief = _brief("Nehir çekimlerini 2 kat hızlandır")
    text, receipts = _review(make_record(brief), brief, lang="tr")

    assert f"- Yapamadım: Nehir çekimlerini 2 kat hızlandır ({TR_REASON})" in text
    assert EN_REASON not in text
    assert receipts["r4"]["status"] == "not_possible"


def test_the_other_asks_read_exactly_as_before():
    brief = _brief()
    text, receipts = _review(_fixed_worker_record(brief), brief)

    assert '- Done: "20 minutes in the dark"' in text
    assert "- Done: 20 seconds" in text
    assert receipts["r1"]["status"] == "met"
    assert receipts["r2"]["status"] == "met"
    # r3 asks for eight moments: since KRI-686 that count is checked against the video (it has
    # four), so it is `checked` and partial. r5 (clip sound) is still not judged here.
    assert receipts["r3"]["verification"] == "checked"
    assert receipts["r3"]["status"] == "partial"
    assert receipts["r5"]["verification"] == "unchecked"
    # The eight-moments ask is judged now (partial), so it reads as "Partly:", not "have a look".
    assert "- Partly: pick the moments where a lit house" in text
    assert "Keep every clip's own sound without any music" in _look_section(text)


def test_a_pacing_ask_is_not_reported_as_a_dropped_speed_ask():
    brief = _brief("Fast pacing")
    text, receipts = _review(_fixed_worker_record(brief), brief)

    assert "clip speed" not in text
    assert receipts["r4"]["verification"] == "unchecked"
