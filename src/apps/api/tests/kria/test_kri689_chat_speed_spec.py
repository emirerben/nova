"""KRI-689: a chat speed ask at creation is read into a rate and a target, then judged.

Failure modes pinned here (docs/reviews/kri-689-chat-speed-failure-modes.md, sections A and D):
- A1 a recorded ``playback_rate`` fact decides before the words; 1.0 is no ask;
- A2 "slow down 2x" is 0.5x, never 2x; "half speed" / "0.5x" are not inverted;
- A3 "slow motion" / "ağır çekim" with no number = 0.5x, "speed up" / "time-lapse" = 2x, and
  the receipt says the creator named no number;
- A4 10x / 0.1x are held at 4x / 0.25x and the receipt says so (partial, never "met");
- A5 "1.5x" / "1,5x" stay one number;
- A7 a speed ramp is an honest limit, never a constant rate;
- A8 the target words for the prod wording are exactly ("river",);
- A9/A11 Turkish targets and positions; only live requirements are planned;
- D1/D2 the heads-up before approval is kept only where the plan cannot carry the speed;
- D4/D7 the receipt reads the plan record: applied rows, locked rows, a legacy record.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    clip_speed_notice,
    clip_speed_plannable,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_rendered_montage,
    plan_facts_from_unified_montage,
)
from app.kria.reply_language import reply_language_for
from app.kria.speed_ask import (
    DEFAULT_FAST_RATE,
    DEFAULT_SLOW_RATE,
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    asks_clip_speed,
    live_speeds,
    speed_spec,
)

USER = uuid.uuid4()
PROXY = "users/u/creation-threads/t/analysis-proxy-ios-A.mp4"
LEGACY_NOTICE = "I can't change clip speed on your iPhone yet, so the clips play at normal speed."
SONG_NOTICE = (
    "I can't change clip speed on this edit because the clips follow your song, "
    "so they play at normal speed."
)


def _req(description: str, kind: str = "style", scope: str = "global", **facts):
    return BriefRequirement(id="r4", kind=kind, scope=scope, description=description, facts=facts)


# -- A. reading the ask ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "rate", "stated"),
    [
        ("Speed the river shots up 2x", 2.0, True),
        ("make the walking clip 2x", 2.0, True),
        ("play the video at 2x faster", 2.0, True),
        ("playback speed 1.5x", 1.5, True),
        ("1,5x hızlı oynat", 1.5, True),
        ("double speed for the walking parts", 2.0, True),
        ("half speed on the drone shot", 0.5, True),
        ("quarter speed on the jump", 0.25, True),
        ("slow down the boat footage 2x", 0.5, True),  # A2: a multiplier on a slow ask divides
        ("slow the clips down to 0.5x", 0.5, True),
        ("three times faster", 3.0, True),
        ("Nehir çekimlerini 2 kat hızlandır", 2.0, True),
        ("iki kat hızlı oynat", 2.0, True),
        ("Son klibi 2 kat yavaşlat", 0.5, True),
        # A3: no number
        ("Slow motion on the finish", DEFAULT_SLOW_RATE, False),
        ("slow-mo for the jump", DEFAULT_SLOW_RATE, False),
        ("Slow down the last clip", DEFAULT_SLOW_RATE, False),
        ("speed up the footage", DEFAULT_FAST_RATE, False),
        ("make it a time-lapse", DEFAULT_FAST_RATE, False),
        ("hyperlapse the ride", DEFAULT_FAST_RATE, False),
        ("fast forward through the boring parts", DEFAULT_FAST_RATE, False),
        ("Klipleri hızlandır", DEFAULT_FAST_RATE, False),
        ("Videoyu yavaşlat", DEFAULT_SLOW_RATE, False),
        ("Son çekimi ağır çekim yap", DEFAULT_SLOW_RATE, False),
        ("yavaş çekim olsun", DEFAULT_SLOW_RATE, False),
        ("Zaman atlamalı bir görüntü istiyorum", DEFAULT_FAST_RATE, False),
    ],
)
def test_the_rate_is_read_from_the_creators_words(description, rate, stated):
    spec = speed_spec(_req(description))
    assert spec is not None, description
    assert spec.rate == pytest.approx(rate), description
    assert spec.rate_stated is stated, description
    assert not spec.ramp


@pytest.mark.parametrize("key", ["playback_rate", "speed"])
def test_a_recorded_rate_decides_before_the_words(key):
    spec = speed_spec(_req("Nehirde akış hissi olsun", **{key: 3}))
    assert spec is not None and spec.rate == 3.0 and spec.rate_stated
    # The words say 2x but the model recorded what it meant.
    assert speed_spec(_req("Speed the river shots up 2x", **{key: 0.5})).rate == 0.5
    # 1.0 is "no change": not an ask at all.
    assert speed_spec(_req("Speed the river shots up 2x", **{key: 1})) is None


@pytest.mark.parametrize(
    ("description", "rate", "requested"),
    [
        ("speed it up 10x", MAX_PLAYBACK_RATE, 10.0),
        ("slow it down to 0.1x", MIN_PLAYBACK_RATE, 0.1),
        ("slow motion 8x", MIN_PLAYBACK_RATE, 0.125),
    ],
)
def test_a_rate_outside_the_native_range_is_held_at_the_bound(description, rate, requested):
    spec = speed_spec(_req(description))
    assert spec.rate == pytest.approx(rate)
    assert spec.rate_requested == pytest.approx(requested)


def test_a_recorded_rate_out_of_range_is_held_too():
    spec = speed_spec(_req("Faster", playback_rate=12))
    assert (spec.rate, spec.rate_requested) == (MAX_PLAYBACK_RATE, 12.0)


def test_a_speed_ramp_is_recognised_and_never_a_constant_rate():
    spec = speed_spec(_req("add a speed ramp on the jump"))
    assert spec is not None and spec.ramp


@pytest.mark.parametrize(
    ("description", "words"),
    [
        ("Speed the river shots up 2x", ("river",)),
        ("make the walking clip 2x", ("walking",)),
        ("slow motion on the pour", ("pour",)),
        ("half speed on the drone shot", ("drone",)),
        ("Nehir çekimlerini 2 kat hızlandır", ("nehir",)),
        ("Nehir görüntülerini 2x hızlandır", ("nehir",)),
        # Whole-edit asks name no footage.
        ("speed up the footage", ()),
        ("Speed it up 2x", ()),
        ("Klipleri hızlandır", ()),
        ("Videoyu yavaşlat", ()),
        ("slow the clips down to 0.5x", ()),
    ],
)
def test_the_target_words_are_the_creators_words_for_the_footage(description, words):
    assert speed_spec(_req(description)).target_words == words
    assert speed_spec(_req(description)).position is None


@pytest.mark.parametrize(
    "description",
    [
        "Slow down the last clip",
        "Son klibi yavaşlat",
        "Son çekimi ağır çekim yap",
        "slow down the final shot",
    ],
)
def test_the_last_clip_is_a_cut_position_not_a_moment(description):
    spec = speed_spec(_req(description))
    assert spec.position == "last"
    assert spec.target_words == ()


def test_a_clip_scope_names_the_clip_directly():
    spec = speed_spec(_req("speed it up 2x", scope="clip:clip-7"))
    assert spec.media_id == "clip-7"
    assert spec.rate == 2.0


@pytest.mark.parametrize(
    "description",
    ["Fast pacing", "Speed up the cuts", "Slow down the title animation", "Müziği yavaşlat"],
)
def test_pacing_text_and_music_asks_have_no_spec(description):
    assert speed_spec(_req(description)) is None
    assert not asks_clip_speed(_req(description))


def test_live_speeds_keeps_brief_order_and_skips_replaced_requirements():
    brief = CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(id="r1", kind="style", scope="global", description="Warm tones"),
            BriefRequirement(
                id="r2", kind="style", scope="global", description="speed up the footage"
            ),
            BriefRequirement(
                id="r3",
                kind="style",
                scope="global",
                description="slow motion on the pour",
                status="superseded",
            ),
            BriefRequirement(
                id="r4", kind="timing", scope="global", description="Son klibi yavaşlat"
            ),
        ],
    )
    specs = live_speeds(brief.live())
    assert [spec.requirement_id for spec in specs] == ["r2", "r4"]
    assert specs[1].position == "last"


# -- D. the heads-up before approval -----------------------------------------------------


@pytest.fixture
def phone_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_render_verified_features", [])


def _notice(reqs, *, strategy=None, judged_ids=()):
    return clip_speed_notice(
        reqs,
        creator_id=USER,
        strategy={"edit_format": "montage", "audio_strategy": "original_audio"}
        if strategy is None
        else strategy,
        item_edit_format="montage",
        clip_paths=(PROXY,),
        judged_ids=judged_ids,
    )


def test_without_a_verified_speed_renderer_the_old_heads_up_stays(phone_on):
    assert not clip_speed_plannable({"edit_format": "montage", "audio_strategy": "original_audio"})
    assert _notice([_req("Speed the river shots up 2x")]) == LEGACY_NOTICE


def test_with_a_verified_speed_renderer_the_plan_speaks_not_the_notice(phone_on, monkeypatch):
    monkeypatch.setattr(settings, "phone_render_verified_features", ["variableSpeed"])
    assert clip_speed_plannable({"edit_format": "montage", "audio_strategy": "original_audio"})
    assert _notice([_req("Speed the river shots up 2x")]) is None


def test_a_song_montage_says_the_clips_follow_the_song(phone_on, monkeypatch):
    monkeypatch.setattr(settings, "phone_render_verified_features", ["variableSpeed"])
    strategy = {"edit_format": "montage", "audio_strategy": "user_song"}
    assert not clip_speed_plannable(strategy)
    assert _notice([_req("Speed the river shots up 2x")], strategy=strategy) == SONG_NOTICE
    with reply_language_for("tr"):
        assert _notice([_req("Speed the river shots up 2x")], strategy=strategy) == (
            "Bu düzenlemede klip hızını değiştiremiyorum çünkü klipler şarkını takip ediyor, "
            "o yüzden normal hızda oynuyorlar."
        )


def test_an_ask_the_preflight_plan_already_judged_gets_no_second_sentence(phone_on):
    assert _notice([_req("Speed the river shots up 2x")], judged_ids={"r4"}) is None


# -- D. the receipt off the plan record -------------------------------------------------

_MONTAGE = {"clip_ids": ["a", "b", "c"], "title": "T", "duration_s": 6.0}


def _row(**overrides):
    row = {
        "requirement_id": "r4",
        "status": "applied",
        "rate": 2.0,
        "rate_requested": 2.0,
        "rate_stated": True,
        "cut_ids": ["unified-cut-2"],
        "cut_positions": [2],
        "media_ids": ["b"],
        "grounding": "analysed_moment",
    }
    row.update(overrides)
    return row


def _facts(*rows):
    return plan_facts_from_unified_montage({**_MONTAGE, "clip_speeds": list(rows)})


def test_a_legacy_record_without_speed_rows_keeps_the_old_couldnt():
    facts = plan_facts_from_unified_montage(_MONTAGE)
    assert facts.speed_unsupported and facts.clip_speeds is None
    receipt = check_requirement(_req("Speed the river shots up 2x"), facts)
    assert (receipt.status, receipt.reason) == ("not_possible", LEGACY_NOTICE.rstrip("."))


def test_an_applied_row_is_done_and_names_the_clip():
    facts = _facts(_row())
    assert not facts.speed_unsupported
    receipt = check_requirement(_req("make the walking clip 2x"), facts)
    assert (receipt.status, receipt.reason) == ("met", "Clip 2 plays at 2x")
    (built,) = build_receipts([_req("make the walking clip 2x")], facts)
    assert built.verification == "checked" and is_judged(_req("make the walking clip 2x"), built)
    with reply_language_for("tr"):
        assert check_requirement(_req("yürüme klibini 2 kat hızlandır"), facts).reason == (
            "2. klip 2x hızda oynuyor"
        )


def test_several_clips_and_the_whole_edit_read_naturally():
    many = _facts(_row(cut_ids=["unified-cut-1", "unified-cut-2", "unified-cut-3"],
                       cut_positions=[1, 2, 3], media_ids=["a", "b", "c"]))  # fmt: skip
    assert check_requirement(_req("Speed the river shots up 2x"), many).reason == (
        "Clips 1, 2 and 3 play at 2x"
    )
    whole = _facts(_row(cut_ids=["unified-cut-1", "unified-cut-2", "unified-cut-3"],
                        cut_positions=[1, 2, 3], media_ids=["a", "b", "c"],
                        grounding="all_clips", rate=0.5, rate_requested=0.5))  # fmt: skip
    assert check_requirement(_req("slow the clips down to 0.5x"), whole).reason == (
        "Every video clip plays at 0.5x"
    )
    with reply_language_for("tr"):
        assert check_requirement(_req("Klipleri yavaşlat"), whole).reason == (
            "Her video klibi 0.5x hızda oynuyor"
        )


def test_a_default_rate_says_the_creator_named_none():
    facts = _facts(_row(rate_stated=False))
    receipt = check_requirement(_req("speed up the walking clip"), facts)
    assert receipt.status == "met"
    assert receipt.reason == "Clip 2 plays at 2x; you didn't say how fast, so I used 2x"


def test_a_held_rate_is_partial_and_names_both_numbers():
    facts = _facts(_row(rate=4.0, rate_requested=10.0))
    receipt = check_requirement(_req("speed the walking clip up 10x"), facts)
    assert receipt.status == "partial"
    assert (
        receipt.reason == "Clip 2 plays at 4x, not the 10x you asked for: a clip plays at most 4x"
    )
    slow = _facts(_row(rate=0.25, rate_requested=0.1))
    assert check_requirement(_req("slow the clips down to 0.1x"), slow).reason == (
        "Clip 2 plays at 0.25x, not the 0.1x you asked for: a clip plays at least 0.25x"
    )


def test_footage_that_ran_out_is_partial_and_names_both_rates():
    receipt = check_requirement(
        _req("speed the walking clip up 4x"),
        _facts(_row(rate=2.458, rate_requested=4.0, footage_limited=True)),
    )
    assert receipt.status == "partial"
    assert receipt.reason == (
        "Clip 2 plays at 2.458x, not the 4x you asked for: the clip ran out of footage"
    )
    with reply_language_for("tr"):
        receipt = check_requirement(
            _req("yürüme klibini 4 kat hızlandır"),
            _facts(_row(rate=2.458, rate_requested=4.0, footage_limited=True)),
        )
    assert receipt.reason == (
        "2. klip 2.458x hızda oynuyor, istediğin 4x değil: klibin görüntüsü yetmedi"
    )


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("locked", "renderer", LEGACY_NOTICE.rstrip(".")),
        (
            "locked",
            "user_song",
            "I can't change clip speed on this edit because the clips follow your song, "
            "so they play at normal speed",
        ),
        (
            "locked",
            "frozen",
            "I can't change the speed of a clip that holds a freeze frame, "
            "so it plays at normal speed",
        ),
        ("ramp", None, "I can't ramp speed inside a clip yet, so it plays at normal speed"),
        ("photo", None, "A photo has no speed to change, so it stays as it is"),
        (
            "no_footage",
            None,
            "I can't speed these clips up: the video already shows each one whole, so there's "
            "no footage left to speed through",
        ),
    ],
)
def test_the_locked_cases_are_honest_format_limits(status, reason, expected):
    facts = _facts(_row(status=status, reason=reason, cut_ids=[], cut_positions=[], media_ids=[]))
    receipt = check_requirement(_req("Speed the river shots up 2x"), facts)
    assert (receipt.status, receipt.reason) == ("not_possible", expected)
    (built,) = build_receipts([_req("Speed the river shots up 2x")], facts)
    assert is_format_limit(built.reason) and not needs_creator_choice(built)


def test_a_clip_nobody_could_name_is_the_creators_call():
    facts = _facts(_row(status="not_grounded", cut_ids=[], cut_positions=[], media_ids=[]))
    receipt = check_requirement(_req("Speed the river shots up 2x"), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == (
        "I couldn't tell which clip you meant for the speed change, so the clips play at "
        "normal speed. Tell me which clip"
    )
    # A bound writer (the preflight, a bound render) stamps `checked`: this one asks the creator.
    (built,) = build_receipts([_req("Speed the river shots up 2x")], facts, include_unchecked=True)
    assert needs_creator_choice(built) and not is_format_limit(built.reason)


def test_a_later_ask_on_the_same_clip_wins_and_the_earlier_one_says_so():
    facts = _facts(
        _row(status="replaced", replaced_by="r9", rate=0.5, rate_requested=0.5),
        _row(requirement_id="r9", rate=0.5, rate_requested=0.5),
    )
    receipt = check_requirement(_req("make the walking clip 2x"), facts)
    assert (receipt.status, receipt.reason) == ("met", "Clip 2 plays at 0.5x, as you asked later")


def test_a_speed_ask_on_a_draft_or_editor_payload_stays_unjudged():
    receipt = check_requirement(_req("Speed the river shots up 2x"), PlanFacts())
    assert receipt.status == "partial" and not is_judged(
        _req("Speed the river shots up 2x"), receipt
    )
    assert build_receipts([_req("Speed the river shots up 2x")], PlanFacts()) == []


# -- D4. the finished video ------------------------------------------------------------


def _variant(rows):
    return {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_destination": "device",
        "resolved_archetype": "guided_story",
        "edit_duration_s": 6.0,
        "story_timeline": rows,
    }


def _timeline(rate_on_second):
    rows = []
    for index, media_id in enumerate(("a", "b", "c")):
        row = {
            "moment_id": f"unified-cut-{index + 1}",
            "kind": "video",
            "lane": "clip",
            "media_id": media_id,
            "source_start_s": 0.0,
            "source_end_s": 2.0 if index != 1 else 4.0,
            "output_start_s": index * 2.0,
            "output_end_s": index * 2.0 + 2.0,
        }
        if index == 1 and rate_on_second is not None:
            row["playback_rate"] = rate_on_second
        rows.append(row)
    return rows


def test_the_finished_video_confirms_the_rate_on_its_timeline():
    record = {**_MONTAGE, "clip_speeds": [_row()]}
    facts = plan_facts_from_rendered_montage(_variant(_timeline(2.0)), record)
    receipt = check_requirement(_req("make the walking clip 2x"), facts)
    assert (receipt.status, receipt.reason) == ("met", "Clip 2 plays at 2x")


def test_a_finished_video_whose_timeline_lost_the_rate_says_so():
    record = {**_MONTAGE, "clip_speeds": [_row()]}
    facts = plan_facts_from_rendered_montage(_variant(_timeline(None)), record)
    receipt = check_requirement(_req("make the walking clip 2x"), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == "The finished video plays this clip at normal speed"
    assert is_format_limit(receipt.reason)


def test_a_timeline_without_moment_ids_cannot_contradict_the_plan():
    """Older variants carry no ``moment_id``: nothing is claimed beyond the plan record."""
    rows = [{k: v for k, v in row.items() if k != "moment_id"} for row in _timeline(None)]
    record = {**_MONTAGE, "clip_speeds": [_row()]}
    facts = plan_facts_from_rendered_montage(_variant(rows), record)
    assert check_requirement(_req("make the walking clip 2x"), facts).status == "met"
