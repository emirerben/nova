"""KRI-777: a freeze-frame ask is a plain "Couldn't", and its seconds are never the video's length.

Café kit M2 (prod 6b689783a, thread EE7B4DCA, 2026-10-21): "Freeze the frame for one second on
Atakan's bite." became brief `r3 [timing/global] {"duration_s": 1, "freeze_frame": true}`. No
edit can freeze a frame (the strategy has no field for it; neither the phone recipe nor the
cloud renderer holds a creator-chosen frame), yet the draft and the approval card said "...
freeze the frame on the bite ..." with no notice. After approval the phone montage's receipt for
r3 read "This draft is about 4.7s; you asked for 1s.": the global timing check took the freeze's
one second for the whole video's length.

Ways this could go wrong, written down before the code (each has a case below or in
`test_kri777_freeze_frame_postgres.py` / `tests/tasks/test_kri777_freeze_render.py`):

1. A freeze ask gets no receipt before approval (the phone montage defers every `timing`
   requirement to the render), so the draft and the approval card keep a summary that
   promises the freeze.
2. It gets a receipt, but not a format limit, so the first draft turns into the "simpler
   version?" question, or the approved phone render is blocked by it.
3. The freeze's seconds are still compared against the total ("you asked for 1s"), or still
   counted as the video's length by the readers that decide the length: the creator-length
   proof (`length_policy.creator_duration_s`), the duration-conflict questions, the render
   contract and the phone planner's target (`unified_montage.brief_view`).
4. The same for a speed / slow-motion ask or "N seconds on <a moment>" filed as global timing.
5. A real length stops being checked: "About 14 seconds", "under 60 seconds", "15 seconds at
   most", "30 seconds on TikTok", "toplamda 14 saniye".
6. Something that is not a freeze is read as one: "the frozen lake shot", "a freezing cold
   morning", Turkish "dondurma" (ice cream), "hold on the sunset for 2 seconds" (a clip length
   the phone can do), "pause the music", "cut the long pauses", a text / order / audio ask, a
   title, the creator's own on-screen words.
7. A real one is missed: "freeze frame", "freeze-frame", "still frame", "hold the frame",
   Turkish "kareyi dondur", "görüntüyü dondur", "donmuş kare", "videoyu durdur", or a recorded
   `freeze_frame` fact with wording nobody wrote a rule for; `freeze_frame: false` must win.
8. The Turkish chat gets English copy, or the Turkish reason stops counting as a format limit.
9. An editor turn (receipts from the before/after diff) is rerouted.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    keeps_model_summary,
    needs_creator_choice,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.freeze_ask import asks_freeze_frame, times_one_moment
from app.kria.reply_language import reply_language_for
from app.pipeline.unified_montage import brief_view
from app.services.choice_questions import _requested_durations
from app.services.length_policy import creator_duration_s

EN_REASON = "I can't freeze a frame in this edit yet, so that moment plays normally"
TR_REASON = "Bu düzenlemede henüz bir kareyi donduramıyorum, o yüzden o an normal akıyor"
PHOTO = "clip:asset-6ae61e1a-96ff-4776-8f95-54ace669db23"
# The M2 draft's summary, verbatim (events #8-#9).
M2_SUMMARY = (
    "I will cut the 6 exact moments in order, insert the photo with text, freeze the frame on "
    "the bite, and keep the original cafe sound."
)


def _m2_brief() -> CreativeBrief:
    """The stored M2 brief (version 1), verbatim (job_debug.json)."""
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description=(
                    "Caro arriving with tray, her hello wave, Cotopaxi photo, plate pushed close,"
                    " Atakan's bite, his thumbs-up, Caro's bye-bye wave"
                ),
            ),
            BriefRequirement(
                id="r2", kind="timing", scope=PHOTO, description="1 second", facts={"duration_s": 1}
            ),
            BriefRequirement(
                id="r3",
                kind="timing",
                scope="global",
                description="Freeze the frame for one second on Atakan's bite",
                facts={"duration_s": 1, "freeze_frame": True},
            ),
            BriefRequirement(
                id="r4",
                kind="audio",
                scope="global",
                description="Keep the original sound, no music",
                facts={"audio_intent": "source_unchanged"},
            ),
            BriefRequirement(
                id="r5", kind="text", scope="title", literal="Colombian dessert, Ecuadorian hands"
            ),
            BriefRequirement(id="r6", kind="text", scope=PHOTO, literal="From Ecuador 🇪🇨"),
            BriefRequirement(
                id="r7",
                kind="timing",
                scope="global",
                description="About 14 seconds",
                facts={"duration_s": 14},
            ),
        ],
    )


def _req(description: str, kind: str = "timing", scope: str = "global", **facts):
    return BriefRequirement(id="r3", kind=kind, scope=scope, description=description, facts=facts)


def _r(brief: CreativeBrief, rid: str) -> BriefRequirement:
    return next(req for req in brief.requirements if req.id == rid)


# -- which asks are a freeze --------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Freeze the frame for one second on Atakan's bite",  # the prod wording
        "freeze frame on the bite",
        "a freeze-frame at the end",
        "Freezeframe when she laughs",
        "freeze on the jump for a second",
        "Freeze it right when the ball hits the net",
        "hold the frame on the medal for 2 seconds",
        "a still frame of the sunset",
        "pause the frame when he smiles",
        # Turkish
        "Atakan'ın ısırığında kareyi bir saniye dondur",
        "Görüntüyü 1 saniye dondur",
        "Son karede dondurarak bitir",
        "donmuş kare efekti olsun",
        "Kareyi ısırıkta durdur",
        "videoyu bir saniye durdur",
    ],
)
def test_a_freeze_ask_is_recognised(description):
    assert asks_freeze_frame(_req(description)), description
    assert asks_freeze_frame(_req(description, kind="style")), description


@pytest.mark.parametrize(
    "description",
    [
        "the frozen lake shot for 2 seconds",
        "a freezing cold morning at the beach",
        "hold on the sunset for 2 seconds",  # a clip length the phone can do
        "Hold the last clip longer",
        "pause the music when I talk",
        "cut the long pauses",
        "keep the camera still",
        "use the still photos",
        "About 14 seconds",
        "Fast pacing",
        # Turkish: "dondurma" is ice cream; "müziği durdur" stops the music.
        "Dondurma çekimini 2 saniye göster",
        "dondurmalı tatlı en sonda",
        "Müziği durdur",
        "Uzun duraklamaları kes",
    ],
)
def test_what_is_not_a_freeze_stays_unflagged(description):
    assert not asks_freeze_frame(_req(description)), description


def test_only_the_kinds_a_freeze_lives_in_count():
    ask = "Freeze the frame for one second on Atakan's bite"
    for kind in ("timing", "style", "select"):
        assert asks_freeze_frame(_req(ask, kind=kind)), kind
    for kind in ("text", "order", "audio"):
        assert not asks_freeze_frame(_req(ask, kind=kind)), kind
    assert not asks_freeze_frame(_req(ask, scope="title"))
    # The creator's own words to print are text, never an ask.
    assert not asks_freeze_frame(
        BriefRequirement(
            id="r1", kind="style", scope="global", description="big title", literal="Freeze Frame"
        )
    )


def test_a_recorded_fact_decides_before_the_words():
    # Wording nobody wrote a rule for, but the model recorded what it meant.
    assert asks_freeze_frame(_req("Isırık anı bir saniye asılı kalsın", freeze_frame=True))
    assert asks_freeze_frame(_req("make the bite linger", freeze=True))
    # An explicit "no freeze" beats words that look like one.
    assert not asks_freeze_frame(_req("Freeze the frame on the bite", freeze_frame=False))


@pytest.mark.parametrize("bad", [None, "true", "yes", 1, 0, [], {}])
def test_a_malformed_fact_falls_back_to_the_words(bad):
    assert asks_freeze_frame(_req("Freeze the frame on the bite", freeze_frame=bad))
    assert not asks_freeze_frame(_req("About 14 seconds", freeze_frame=bad))


# -- the receipt: before approval and after the render --------------------------------------


def test_m2_freeze_is_a_couldnt_on_the_draft_not_a_simpler_version_question():
    brief = _m2_brief()
    r3 = _r(brief, "r3")
    facts = plan_facts_from_strategy(
        {"edit_format": "montage", "audio_strategy": "original_audio", "target_duration_s": 14}
    )

    # The bound writer's path (prod): every judged receipt is stored as `checked`.
    (receipt,) = build_receipts([r3], facts, include_unchecked=True)

    assert (receipt.status, receipt.verification, receipt.reason) == (
        "not_possible",
        "checked",
        EN_REASON,
    )
    assert is_judged(r3, receipt)
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)
    # The reply is what was checked, so the model's "freeze the frame on the bite" is gone,
    # and everything else the draft says about itself follows the reply.
    reply = reply_from_receipts(brief, [receipt], summary=M2_SUMMARY)
    assert "freeze the frame on the bite" not in reply
    assert reply == (
        "Not everything you asked for made it in:\n"
        f"- Couldn't: Freeze the frame for one second on Atakan's bite ({EN_REASON})"
    )
    assert not keeps_model_summary(brief, [receipt])


def test_the_phone_montage_draft_checks_the_freeze_it_defers_every_other_timing_for(monkeypatch):
    """The unified planner settles `timing` at render time, but it cannot freeze a frame."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    checked = requirements_to_check_at_draft(
        _m2_brief().live(),
        creator_id="u",
        strategy={"edit_format": "montage", "audio_strategy": "original_audio"},
        item_edit_format="montage",
        clip_paths=["users/u/creation-threads/t/analysis-proxy-ios-11CF148F.mp4"],
    )

    assert [req.id for req in checked] == ["r3", "r4"]


def test_the_rendered_phone_montage_says_couldnt_and_never_you_asked_for_1s():
    brief = _m2_brief()
    # The M2 record's shape: one take cut once plus the photo, 4.7 s in all.
    facts = plan_facts_from_unified_montage(
        {"clip_ids": ["video", "photo"], "title": "T", "duration_s": 4.7}
    )

    receipts = {r.requirement_id: r for r in build_receipts(brief.live(), facts)}

    assert receipts["r3"].status == "not_possible"
    assert receipts["r3"].reason == EN_REASON
    assert not needs_creator_choice(receipts["r3"])
    assert all("you asked for 1s" not in (r.reason or "") for r in receipts.values())
    # The creator's real length is still judged against the total.
    assert receipts["r7"].reason == "This draft is about 4.7s; you asked for 14s."


def test_a_turkish_chat_gets_the_turkish_reason():
    r3 = _req("Atakan'ın ısırığında kareyi bir saniye dondur", duration_s=1, freeze_frame=True)
    with reply_language_for("tr"):
        (receipt,) = build_receipts([r3], PlanFacts())
        reply = reply_from_receipts(CreativeBrief(version=1, requirements=[r3]), [receipt])

    assert receipt.reason == TR_REASON
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)
    assert reply == (
        "İstediklerinin hepsi videoya giremedi:\n"
        f"- Yapamadım: Atakan'ın ısırığında kareyi bir saniye dondur ({TR_REASON})"
    )
    assert EN_REASON not in reply


def test_an_editor_turn_keeps_its_own_path_and_never_compares_the_total():
    r3 = _req("Freeze the frame for one second on Atakan's bite", duration_s=1, freeze_frame=True)

    receipt = check_requirement(r3, PlanFacts(editor=True, duration_s=4.7))

    assert receipt.status != "not_possible"
    assert "you asked for" not in (receipt.reason or "")
    assert not is_judged(r3, receipt)
    # A rendered phone variant is a finished render: "Couldn't", like the montage record.
    rendered = check_requirement(r3, PlanFacts(editor=True, rendered_variant=True))
    assert (rendered.status, rendered.reason) == ("not_possible", EN_REASON)


# -- a moment's or an effect's seconds are never the video's length ------------------------


@pytest.mark.parametrize(
    "req",
    [
        _req("Freeze the frame for one second on Atakan's bite", duration_s=1, freeze_frame=True),
        _req("Freeze the frame for one second on Atakan's bite", duration_s=1),
        _req("slow motion for 3 seconds on the jump", duration_s=3),
        _req("hızlandırılmış", duration_s=3, playback_rate=2),
        _req("Stay 2 seconds on the medal", duration_s=2),
        _req("show it for one second on Atakan's bite", duration_s=1),
        _req("3 seconds at the toast", duration_s=3),
        _req("2s during the jump", duration_s=2),
        _req("Isırık anında 1 saniye kalsın", duration_s=1),
    ],
)
def test_a_moment_or_effect_timing_is_never_compared_to_the_total(req):
    assert times_one_moment(req), req.description
    receipt = check_requirement(req, PlanFacts(duration_s=4.7, edit_format="montage"))

    assert "you asked for" not in (receipt.reason or ""), receipt.reason


@pytest.mark.parametrize(
    ("description", "seconds"),
    [
        ("About 14 seconds", 14),
        ("under 60 seconds", 60),
        ("15 seconds at most", 15),
        ("60 seconds at least", 60),
        ("30 seconds on TikTok", 30),
        ("Make it 20 seconds on Instagram reels", 20),
        ("a 20 second video of my trip", 20),
        ("About 14 seconds, it's at a café", 14),  # "it's" folds to "it s at"
        ("toplamda 14 saniye", 14),
        ("Videoda 14 saniye olsun", 14),
        ("yaklaşık 14 saniye", 14),
    ],
)
def test_a_real_length_is_still_the_videos_length(description, seconds):
    req = _req(description, duration_s=seconds)

    assert not times_one_moment(req), description
    receipt = check_requirement(req, PlanFacts(duration_s=4.7, edit_format="montage"))
    assert receipt.reason == f"This draft is about 4.7s; you asked for {seconds}s."


def test_the_length_readers_never_take_the_freeze_for_the_video_length():
    length = _req("About 14 seconds", duration_s=14).model_copy(update={"id": "r7"})
    freeze = _req(
        "Freeze the frame for one second on Atakan's bite", duration_s=1, freeze_frame=True
    )
    # The freeze listed last: the planner's target used to be the LAST timing's seconds.
    brief = CreativeBrief(version=1, requirements=[length, freeze])

    assert creator_duration_s(brief=brief) == 14.0
    assert brief_view(brief).target_duration_s == 14.0
    durations, requirement_ids, _quoted = _requested_durations(brief)
    assert (durations, requirement_ids) == ([14.0], ["r7"])
    # With nothing but the freeze there is no creator length at all.
    only = CreativeBrief(version=1, requirements=[freeze])
    assert creator_duration_s(brief=only) is None
    assert brief_view(only).target_duration_s is None
