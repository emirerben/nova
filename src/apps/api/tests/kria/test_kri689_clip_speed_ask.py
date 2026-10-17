"""KRI-689: a clip-speed ask a phone montage cannot honour is said before approval.

Prod thread 1390E686 (2026-10-15, stress kit M2 "night lights"): "Speed the river shots up
2x" was kept in the brief as `r4 [style/global]` with no facts. The phone montage lays every
clip out at 1x, yet the draft reply and the approval card never said so; only the render-ready
review listed it, under "Have a look at these in the video".

Failure modes pinned here (written before the code):
- the draft is approved without a word about speed (no notice, or one that misses the card);
- the notice appears where it is false: a cloud render, a voiceover montage, an account the
  phone gate excludes, a format that is not a montage, a requirement the creator replaced;
- a pacing / text / music ask is reported as a dropped speed ask ("fast pacing", "speed it
  up", "faster transitions", "slow title animation", "zoom 2x"), or a real one is missed
  (Turkish "2 kat hızlandır", "ağır çekim", "time-lapse", "slow motion");
- a recorded `playback_rate` fact is ignored, "1" is read as a change, or a bool / NaN / text
  value crashes or flags;
- the receipt turns the first draft into the "simpler version?" question or blocks an
  approved render (it is a limit of the phone montage, not a simplification);
- the Turkish chat gets English copy.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    clip_speed_notice,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_unified_montage,
)
from app.kria.reply_language import reply_language_for
from app.kria.speed_ask import asks_clip_speed

USER = uuid.uuid4()
PROXY = "users/u/creation-threads/t/analysis-proxy-ios-A.mp4"
CLOUD = "users/u/creation-threads/t/original-clip.mp4"
EN_NOTICE = "I can't change clip speed on your iPhone yet, so the clips play at normal speed."
TR_NOTICE = "iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor."


def _req(description: str, kind: str = "style", scope: str = "global", **facts):
    return BriefRequirement(id="r4", kind=kind, scope=scope, description=description, facts=facts)


# -- which words are a clip-speed ask --------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Speed the river shots up 2x",  # the prod wording
        "speed up the footage",
        "Sped up clips",
        "Speed it up 2x",
        "Slow down the last clip",
        "slow the boat footage down",
        "Slow motion on the finish",
        "slow-mo for the jump",
        "make it a time-lapse",
        "timelapse of the sunset",
        "hyperlapse the ride",
        "2x speed",
        "play the video at 2x faster",
        "double speed for the walking parts",
        "half speed on the drone shot",
        "playback speed 1.5x",
        "add a speed ramp on the jump",
        "fast forward through the boring parts",
        # Turkish
        "Nehir çekimlerini 2 kat hızlandır",
        "Klipleri hızlandır",
        "Videoyu yavaşlat",
        "Son çekimi ağır çekim yap",
        "yavaş çekim olsun",
        "Zaman atlamalı bir görüntü istiyorum",
        "iki kat hızlı oynat",
        "Nehir görüntülerini 2x hızlandır",
        "Son klibi yavaşlat",
    ],
)
def test_a_clip_speed_ask_is_recognised(description):
    assert asks_clip_speed(_req(description)), description


@pytest.mark.parametrize(
    "description",
    [
        "Fast pacing",
        "Speed it up",
        "Make the cuts faster",
        "Speed up the cuts",
        "Faster transitions",
        "Slow down the title animation",
        "Slow the music down",
        "Slow down the transitions",
        "A slow fade at the end",
        "Keep a slow-paced, calm vibe",
        "Zoom in 2x on her face",
        "Make the text 2x bigger",
        "Add captions",
        "Warm tones",
        "Hızlı tempo olsun",
        "Geçişler hızlı olsun",
        "Başlık animasyonunu yavaşlat",
        "Montajı hızlandır",
        "Müziği yavaşlat",
        "Hızlı çekimler olsun",
        "2 kat büyüt",
    ],
)
def test_pacing_text_and_music_asks_are_not_clip_speed(description):
    assert not asks_clip_speed(_req(description)), description


def test_only_the_asks_a_speed_checker_owns_count():
    ask = "Speed the river shots up 2x"
    assert asks_clip_speed(_req(ask, kind="style"))
    assert asks_clip_speed(_req(ask, kind="timing"))
    assert asks_clip_speed(_req(ask, kind="select"))
    for kind in ("text", "order", "audio"):
        assert not asks_clip_speed(_req(ask, kind=kind)), kind
    assert not asks_clip_speed(_req(ask, scope="title"))
    # The creator's exact words are text to show, never an ask.
    assert not asks_clip_speed(
        BriefRequirement(
            id="r1", kind="style", scope="global", description="big title", literal="Slow Motion"
        )
    )


@pytest.mark.parametrize("key", ["playback_rate", "speed"])
def test_a_recorded_rate_decides_before_the_words(key):
    # Turkish wording nobody wrote a pattern for, but the model recorded what it meant.
    assert asks_clip_speed(_req("Nehirde akış hissi olsun", **{key: 2}))
    assert asks_clip_speed(_req("Dramatik an olsun", **{key: 0.5}))
    # An explicit "no change" beats words that look like a change.
    assert not asks_clip_speed(_req("Speed the river shots up 2x", **{key: 1}))
    assert not asks_clip_speed(_req("Speed the river shots up 2x", **{key: 1.0}))


@pytest.mark.parametrize(
    "bad", [True, False, None, "2x", "fast", float("nan"), float("inf"), 0, -2]
)
def test_a_malformed_rate_falls_back_to_the_words(bad):
    assert asks_clip_speed(_req("Speed the river shots up 2x", playback_rate=bad))
    assert not asks_clip_speed(_req("Warm tones", playback_rate=bad))


# -- the heads-up before approval ------------------------------------------------------


@pytest.fixture
def phone_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])


def _notice(reqs, *, strategy=None, clips=(PROXY,), item_format="montage", user=USER):
    return clip_speed_notice(
        reqs,
        creator_id=user,
        strategy={"edit_format": "montage", "audio_strategy": "original_audio"}
        if strategy is None
        else strategy,
        item_edit_format=item_format,
        clip_paths=clips,
    )


def test_a_phone_montage_draft_with_a_speed_ask_says_so_before_approval(phone_on):
    reqs = [_req("Warm tones"), _req("Speed the river shots up 2x")]

    assert _notice(reqs) == EN_NOTICE
    with reply_language_for("tr"):
        assert _notice(reqs) == TR_NOTICE


def test_two_speed_asks_get_one_notice(phone_on):
    reqs = [_req("Speed the river shots up 2x"), _req("slow motion on the finish", kind="timing")]

    assert _notice(reqs) == EN_NOTICE


@pytest.mark.parametrize("fmt", ["montage", "day_vlog", "single_hero"])
@pytest.mark.parametrize("audio", ["original_audio", "licensed_music"])
def test_every_phone_montage_family_draft_gets_it(phone_on, fmt, audio):
    strategy = {"edit_format": fmt, "audio_strategy": audio}
    assert _notice([_req("speed it up 2x")], strategy=strategy) == EN_NOTICE


def test_no_speed_ask_no_notice(phone_on):
    assert _notice([_req("Fast pacing"), _req("Warm tones")]) is None
    assert _notice([]) is None


def test_a_replaced_requirement_is_not_announced(phone_on):
    gone = _req("Speed the river shots up 2x").model_copy(update={"status": "superseded"})

    assert _notice([gone]) is None


@pytest.mark.parametrize("fmt", ["subtitled", "narrated", "slides"])
def test_formats_the_unified_montage_does_not_render_get_none(phone_on, fmt):
    strategy = {"edit_format": fmt, "audio_strategy": "original_audio"}
    assert _notice([_req("speed it up 2x")], strategy=strategy, item_format=fmt) is None


@pytest.mark.parametrize("audio", ["voiceover", "anything_else", None, ""])
def test_the_voiceover_lane_is_not_the_unified_montage(phone_on, audio):
    strategy = {"edit_format": "montage", "audio_strategy": audio}
    assert _notice([_req("speed it up 2x")], strategy=strategy) is None


def test_a_cloud_render_is_not_told_about_the_iphone(phone_on):
    assert _notice([_req("speed it up 2x")], clips=(CLOUD,)) is None
    assert _notice([_req("speed it up 2x")], clips=()) is None


def test_an_account_the_phone_gate_excludes_is_not_told_about_the_iphone(phone_on, monkeypatch):
    monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    assert _notice([_req("speed it up 2x")]) is None
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    assert _notice([_req("speed it up 2x")]) is None


def test_no_strategy_no_notice(phone_on):
    assert (
        clip_speed_notice(
            [_req("speed it up 2x")],
            creator_id=USER,
            strategy=None,
            item_edit_format="montage",
            clip_paths=(PROXY,),
        )
        is None
    )


# -- the receipt after the render ------------------------------------------------------

_MONTAGE = {"clip_ids": ["a", "b"], "title": "T", "duration_s": 20.0}


def test_a_unified_montage_record_cannot_retime_its_clips():
    assert plan_facts_from_unified_montage(_MONTAGE).speed_unsupported
    # A draft, an editor payload or a cloud plan claims nothing about speed.
    assert not PlanFacts().speed_unsupported
    assert not PlanFacts(editor=True).speed_unsupported


def test_the_rendered_montage_says_couldnt_instead_of_have_a_look():
    req = _req("Speed the river shots up 2x")
    facts = plan_facts_from_unified_montage(_MONTAGE)

    receipt = check_requirement(req, facts)

    assert receipt.status == "not_possible"
    assert receipt.reason == EN_NOTICE.rstrip(".")
    (built,) = build_receipts([req], facts, include_unchecked=True)
    assert (built.status, built.verification, built.stage) == ("not_possible", "checked", "checked")
    assert is_judged(req, built)


def test_the_receipt_is_a_format_limit_so_it_never_asks_or_blocks():
    req = _req("Speed the river shots up 2x")
    (built,) = build_receipts([req], plan_facts_from_unified_montage(_MONTAGE))

    assert is_format_limit(built.reason)
    assert not needs_creator_choice(built)
    with reply_language_for("tr"):
        (turkish,) = build_receipts([req], plan_facts_from_unified_montage(_MONTAGE))
    assert turkish.reason == TR_NOTICE.rstrip(".")
    assert is_format_limit(turkish.reason)
    assert not needs_creator_choice(turkish)


def test_a_speed_ask_nobody_can_judge_stays_unjudged():
    """Cloud / draft / editor facts do not claim the phone limit: unchanged behaviour."""
    req = _req("Speed the river shots up 2x")
    receipt = check_requirement(req, PlanFacts())

    assert receipt.status == "partial"
    assert not is_judged(req, receipt)
    assert build_receipts([req], PlanFacts()) == []
