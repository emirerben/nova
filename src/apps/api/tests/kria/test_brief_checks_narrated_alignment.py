"""KRI-533: a phone Voiceover edit judges its order anchor, clip timing and caption language.

Prod thread d76b79bf (Cappadocia): all four receipts read "Couldn't verify" at draft time
and the render never re-checked them. The worker now records every clip's window, labels and
spoken words (`assembly_plan["narrated_alignment"]`), and these checkers read only that.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    check_requirement,
    defers_to_narrated_render,
    plan_facts_from_narrated_alignment,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

PROXY = "users/u/creation-threads/t/analysis-proxy-ios-A.mp4"
USER = uuid.uuid4()

R1 = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description=(
        "Provide English subtitles translated from the Turkish voiceover, spelling the "
        "specific place names exactly: Göreme, Paşabağ, Avanos, Kızılçukur"
    ),
)
R2 = BriefRequirement(
    id="r2",
    kind="timing",
    scope="global",
    description="Show the balloons while talking about the balloons",
)
R3 = BriefRequirement(
    id="r3",
    kind="order",
    scope="global",
    description="End on the sunset valley",
    facts={"last_clip": "the sunset valley"},
)
R4 = BriefRequirement(id="r4", kind="select", scope="global", description="Skip the quad bike clip")
ALL = [R1, R2, R3, R4]

LONG_TR = (
    "Kapadokya'da tek bir günüm olsaydı sabah erkenden kalkardım gökyüzü yüzlerce balonla doluyor."
)


def _step(media_id, labels, start, end, text="", placed=()):
    return {
        "media_id": media_id,
        "labels": list(labels),
        "placed": [{"spot": spot, "name": name} for spot, name in placed],
        "start_s": start,
        "end_s": end,
        "text": text,
    }


def _record(steps=None, **extra):
    base = {
        "ordering_basis": "spoken_word_alignment",
        "caption_language": "en",
        "spoken_language": "tr",
        "steps": steps
        if steps is not None
        else [
            _step("m1", ["the balloons"], 0.0, 4.5, LONG_TR[:60]),
            _step("m2", ["the balloons"], 4.5, 9.0, LONG_TR[60:]),
            _step("m3", [], 9.0, 14.0, "sonra Göreme'ye geçerdim"),
            _step(
                "m4",
                ["the sunset valley"],
                14.0,
                20.0,
                "akşam gün batımı vadisi",
                placed=[("last", "the sunset valley")],
            ),
        ],
    }
    return {**base, **extra}


def _receipts(record, reqs=ALL, **kwargs):
    facts = plan_facts_from_narrated_alignment(record)
    return {
        r.requirement_id: r for r in build_receipts(reqs, facts, include_unchecked=True, **kwargs)
    }


def test_the_cappadocia_asks_are_judged_from_the_render_record():
    receipts = _receipts(_record())
    assert {rid: (r.status, r.verification) for rid, r in receipts.items()} == {
        "r1": ("met", "checked"),
        "r2": ("met", "checked"),
        "r3": ("met", "checked"),
        "r4": ("partial", "unchecked"),  # no exclude op yet (KRI-511)
    }
    assert receipts["r1"].reason.startswith(
        "The captions are in English, translated from the Turkish voiceover."
    )
    assert "haven't checked how the names are spelled" in receipts["r1"].reason
    assert receipts["r2"].reason.startswith("The balloons clips play from 0.0 s to 9.0 s, under: ")
    assert LONG_TR[:60] in receipts["r2"].reason
    assert receipts["r3"].reason is None


def test_the_render_ready_reply_lists_done_lines():
    receipts = list(_receipts(_record()).values())
    text = reply_from_receipts(
        CreativeBrief(version=1, requirements=ALL), receipts, summary="Ready."
    )
    assert "Done: End on the sunset valley" in text
    assert "Done: Show the balloons while talking about the balloons (The balloons clips" in text
    assert "Done: Provide English subtitles" in text
    assert "Couldn't verify: Skip the quad bike clip" in text
    assert text.count("Couldn't verify") == 1


def test_a_last_clip_that_is_not_last_is_not_possible_and_names_where_it_ended_up():
    steps = _record()["steps"]
    steps[0], steps[3] = steps[3], steps[0]
    receipt = _receipts(_record(steps))["r3"]
    assert (receipt.status, receipt.verification) == ("not_possible", "checked")
    assert receipt.reason == "The sunset valley is clip 1 of 4, not the last one."


def test_a_preference_order_that_misses_is_partial_not_a_blocker():
    soft = R3.model_copy(
        update={"facts": {"last_clip": "the sunset valley", "strength": "preference"}}
    )
    steps = _record()["steps"]
    steps.reverse()
    assert _receipts(_record(steps), [soft])["r3"].status == "partial"


def test_first_clip_anchor_and_unknown_group():
    first = BriefRequirement(
        id="f", kind="order", scope="global", facts={"first_clip": "the balloons"}
    )
    assert _receipts(_record(), [first])["f"].status == "met"
    ghost = BriefRequirement(
        id="g", kind="order", scope="global", facts={"last_clip": "the quad bikes"}
    )
    receipt = _receipts(_record(), [ghost])["g"]
    # No clip carries that name: nothing to judge, so it stays unchecked, never blocks.
    assert (receipt.status, receipt.verification) == ("partial", "unchecked")


def test_an_order_intent_position_implies_the_anchor_when_the_brief_has_no_fact():
    plain = BriefRequirement(
        id="p", kind="order", scope="global", description="End on the sunset valley"
    )
    assert _receipts(_record(), [plain])["p"].status == "met"
    steps = _record()["steps"]
    steps.reverse()
    assert _receipts(_record(steps), [plain])["p"].status == "not_possible"


def test_an_order_with_no_seat_to_judge_stays_unchecked():
    other = BriefRequirement(id="o", kind="order", scope="global", facts={"key": "capture_time"})
    receipt = _receipts(_record(), [other])["o"]
    assert (receipt.status, receipt.verification) == ("partial", "unchecked")


def test_timing_is_only_judged_when_the_words_placed_the_clips_in_one_run():
    legacy = _receipts(_record(ordering_basis="attachment"))["r2"]
    assert legacy.verification == "unchecked"
    steps = _record()["steps"]
    steps[1], steps[2] = steps[2], steps[1]  # balloons clips now split apart
    split = _receipts(_record(steps))["r2"]
    assert split.verification == "unchecked"
    silent = _record()["steps"]
    for row in silent:
        row["text"] = ""
    assert _receipts(_record(silent))["r2"].verification == "unchecked"
    assert (
        _receipts(_record(), [R2.model_copy(update={"description": "make it dramatic"})])[
            "r2"
        ].verification
        == "unchecked"
    )


def test_a_numeric_or_whole_take_timing_is_not_hijacked():
    ten = BriefRequirement(
        id="t", kind="timing", scope="global", description="about 10 s", facts={"duration_s": 10}
    )
    whole = BriefRequirement(
        id="w", kind="timing", scope="global", description="keep my whole take"
    )
    facts = plan_facts_from_narrated_alignment(_record())
    # Same verdicts as before the render recorded anything: neither reads the clip record.
    assert check_requirement(ten, facts).reason == "I can't confirm this draft's length yet."
    assert "balloons" not in (check_requirement(whole, facts).reason or "")


def test_a_long_narration_is_quoted_by_its_first_and_last_words():
    spoken = " ".join(f"word{i}" for i in range(60))
    steps = [_step("m1", ["the balloons"], 0.0, 9.0, spoken)]
    reason = _receipts(_record(steps), [R2])["r2"].reason
    assert 'under: "word0 word1' in reason and "…" in reason and 'word59"' in reason
    assert len(reason) < 300


@pytest.mark.parametrize(
    ("language", "status", "needle"),
    [
        ("en", "met", "The captions are in English, translated"),
        ("tr", "partial", "The captions are in Turkish, not English."),
    ],
)
def test_caption_language_met_or_partial(language, status, needle):
    receipt = _receipts(_record(caption_language=language), [R1])["r1"]
    assert (receipt.status, receipt.verification) == (status, "checked")
    assert needle in receipt.reason


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("English subtitles translated from the Turkish voiceover", "en"),
        ("add captions in Turkish", "tr"),
        ("translate the captions to English", "en"),
        ("İngilizce altyazı ekle", "en"),
        ("Türkçe altyazılar olsun", "tr"),
    ],
)
def test_the_requested_language_is_read_from_the_ask(text, code):
    req = BriefRequirement(id="c", kind="style", scope="global", description=text)
    rendered = "de" if code != "de" else "en"
    receipt = _receipts(_record(caption_language=rendered, spoken_language=None), [req])["c"]
    assert f"not {'English' if code == 'en' else 'Turkish'}" in receipt.reason


def test_a_captions_ask_without_a_language_is_left_to_the_style_checker():
    req = BriefRequirement(id="c", kind="style", scope="global", description="add captions")
    assert _receipts(_record(), [req])["c"].verification == "unchecked"


def test_turkish_copy():
    token = bind_reply_language("tr")
    try:
        receipts = _receipts(_record())
    finally:
        release_reply_language(token)
    assert "Altyazılar İngilizce, Türkçe seslendirmeden çevrildi." in receipts["r1"].reason
    assert "klipleri 0.0 sn ile 9.0 sn arasında" in receipts["r2"].reason


def test_receipts_do_not_exist_for_a_record_that_is_not_a_voiceover_render():
    from app.kria.brief_checks import PlanFacts

    receipts = build_receipts(ALL, PlanFacts(), include_unchecked=True)
    assert all(r.verification == "unchecked" for r in receipts)


# --------------------------------------------------------------------- draft time


@pytest.fixture
def phone_on(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "narrationAudio", "audioMix"],
    )


def _draft(strategy, *, clips=(PROXY,), item_format="narrated_planned"):
    return [
        r.id
        for r in requirements_to_check_at_draft(
            ALL,
            creator_id=USER,
            strategy=strategy,
            item_edit_format=item_format,
            clip_paths=clips,
        )
    ]


def test_a_phone_voiceover_draft_defers_order_timing_and_the_caption_language(phone_on):
    strategy = {"edit_format": "narrated_planned", "audio_strategy": "voiceover"}
    assert _draft(strategy) == ["r4"]


def test_a_strategy_that_names_its_ordering_basis_still_judges_order_at_draft(phone_on):
    strategy = {
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "ordering_basis": "capture_time",
    }
    assert _draft(strategy) == ["r3", "r4"]


@pytest.mark.parametrize(
    ("strategy", "clips", "item_format"),
    [
        ({"edit_format": "narrated_planned", "audio_strategy": "voiceover"}, ("cloud.mp4",), "x"),
        ({"edit_format": "narrated_planned", "audio_strategy": "original_audio"}, (PROXY,), "x"),
        ({"edit_format": "subtitled", "audio_strategy": "voiceover"}, (PROXY,), "subtitled"),
        ({"edit_format": "montage", "audio_strategy": "voiceover"}, (PROXY,), "montage"),
    ],
)
def test_every_other_draft_is_judged_in_full(phone_on, strategy, clips, item_format):
    assert _draft(strategy, clips=clips, item_format=item_format) == ["r1", "r2", "r3", "r4"]


def test_the_phone_deployment_gates_the_deferral(monkeypatch, phone_on):
    strategy = {"edit_format": "narrated_planned", "audio_strategy": "voiceover"}
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", False)
    assert _draft(strategy) == ["r1", "r2", "r3", "r4"]
    assert not defers_to_narrated_render(
        creator_id=USER,
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        clip_paths=(PROXY,),
    )


def test_a_duration_timing_ask_is_not_deferred(phone_on):
    ten = BriefRequirement(
        id="t", kind="timing", scope="global", description="about 10 s", facts={"duration_s": 10}
    )
    kept = requirements_to_check_at_draft(
        [ten],
        creator_id=USER,
        strategy={"edit_format": "narrated_planned", "audio_strategy": "voiceover"},
        item_edit_format="narrated_planned",
        clip_paths=(PROXY,),
    )
    assert [r.id for r in kept] == ["t"]
