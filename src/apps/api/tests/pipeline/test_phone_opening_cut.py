"""KRI-628: "cut the intro, start the video at '...'" on a phone Talking take (Tigre T1).

End to end through the real pieces, with a T1-shaped take (intro chatter, a long gap,
then the quoted question): the brief's own words -> `brief_opening_cut` -> a real
speech-cleanup `CutPlan` -> `apply_opening_cut` -> `compile_phone_subtitled_plan`, and the
receipt the brief check judges.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.opening_cut_ask import brief_opening_cut, opening_quote
from app.pipeline.phone_opening_cut import apply_opening_cut
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.silence_cut import build_cut_plan
from app.services.phone_sources import PhoneSourceBinding

T1_R1 = (
    "Baştaki “Hazırlıklara başladın mı” kısmını ve uzun duraksamaları kes; video "
    "“Abonelerimize bir şey demek ister misin” sorusuyla başlasın."
)
DURATION_S = 30.0


def _words() -> list[dict]:
    spoken = [
        ("Evet", 0.2, 0.6),
        ("reis.", 0.6, 1.0),
        ("Hazırlıklara", 1.4, 2.2),
        ("başladın", 2.2, 2.7),
        ("mı?", 2.7, 3.0),
        ("Ne", 3.4, 3.6),
        ("hazırlığı?", 3.6, 4.2),
        ("Hadi", 4.6, 4.9),
        ("çıkacağız", 4.9, 5.6),
        ("az", 5.6, 5.8),
        ("sonra.", 5.8, 6.6),
        ("Tamam,", 7.0, 7.5),
        ("çıkarız.", 7.5, 8.4),
        ("He?", 8.6, 8.9),
        ("Evet.", 9.8, 10.4),
        # whisper heard "etmek" where the creator quotes "demek".
        ("Abonelerimize", 14.6, 15.4),
        ("bir", 15.4, 15.6),
        ("şey", 15.6, 15.9),
        ("etmek", 15.9, 16.3),
        ("ister", 16.3, 16.7),
        ("misin?", 16.7, 17.4),
        ("Takipte", 17.8, 18.4),
        ("kalın.", 18.4, 19.0),
        ("Biz", 20.0, 20.4),
        ("bir", 20.4, 20.6),
        ("community", 20.6, 21.4),
        ("yaratmaya", 21.4, 22.2),
        ("çalışıyoruz.", 22.2, 23.4),
    ]
    return [{"text": t, "start_s": s, "end_s": e, "confidence": 0.95} for t, s, e in spoken]


def _binding() -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=DURATION_S,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=True,
        ),
    )


def _cleanup_plan():
    # The pauses a real cleanup of this take cuts: the 4.2 s gap and the long tail.
    return build_cut_plan(_words(), [(10.4, 14.6), (23.4, 30.0), (6.6, 7.0)], DURATION_S)


def test_t1_brief_line_picks_the_question_not_the_intro():
    assert opening_quote(T1_R1) == "Abonelerimize bir şey demek ister misin"


def test_t1_render_starts_at_the_quoted_question():
    requirement = BriefRequirement(id="r1", kind="select", scope="global", description=T1_R1)
    ask = brief_opening_cut(type("B", (), {"live": lambda self: [requirement]})())
    assert ask is not None and ask.requirement_ids == ("r1",)

    base = _cleanup_plan()
    # Today's behaviour (the bug): the pauses go, the intro stays.
    assert base.keep_segments[0][0] < 1.0

    plan, receipt = apply_opening_cut(
        base, _words(), ask.quote, duration_s=DURATION_S, requirement_ids=ask.requirement_ids
    )
    assert receipt["status"] == "applied"
    # Nothing before the question survives; it opens a hair before "Abonelerimize".
    assert 14.4 <= plan.keep_segments[0][0] < 14.6
    assert plan.removed[0].reason == "opening_cut"
    assert sum(e - s for s, e in plan.keep_segments) + plan.time_saved_s == pytest.approx(
        DURATION_S, abs=0.01
    )

    recipe = compile_phone_subtitled_plan((_binding(),), caption_cues=[], cut_plan=plan)
    video = next(t for t in recipe.tracks if t.kind == "video")
    assert video.clips[0].source_start == pytest.approx(plan.keep_segments[0][0])
    assert recipe.duration == pytest.approx(sum(e - s for s, e in plan.keep_segments), abs=0.01)


def test_a_line_the_take_never_says_cuts_nothing_and_the_review_says_so():
    base = _cleanup_plan()
    plan, receipt = apply_opening_cut(
        base, _words(), "Bu cümle hiç söylenmedi burada", duration_s=DURATION_S
    )
    assert plan is base
    assert (receipt["status"], receipt["reason"]) == ("not_applied", "not_found")

    requirement = BriefRequirement(id="r1", kind="select", scope="global", description=T1_R1)
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_cues": [],
        "text_elements": [],
        "opening_cut": receipt,
    }
    [judged] = build_receipts(
        [requirement], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert judged.status == "partial"
    assert "bulamadım" in (judged.reason or "") or "couldn't find" in (judged.reason or "")


def test_the_review_calls_the_start_cut_done_only_when_it_happened():
    requirement = BriefRequirement(id="r1", kind="select", scope="global", description=T1_R1)
    _, receipt = apply_opening_cut(
        _cleanup_plan(), _words(), "Abonelerimize bir şey demek ister misin", duration_s=DURATION_S
    )
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_cues": [],
        "text_elements": [],
        "opening_cut": receipt,
        "silence_cut_outcome": "applied",
    }
    [judged] = build_receipts(
        [requirement], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert judged.status == "met", judged
