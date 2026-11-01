"""Failure modes of the show-when-mentioned decisions (parent KRI-1151), written before the code.

Roles (`demote_speakers_to_cutaways`), written as the ways a wrong demotion would cost the
creator his video:

1. A clip whose analysis failed or was degraded reads `speaks_to_camera=False` by default.
   That is no evidence, so it is never demoted.
2. Every speaker saying "not to camera" (the creator's own take mis-flagged) must not leave the
   edit with no creator: nothing moves.
3. A clip the ask NAMES moves even when it speaks to camera, but never the last speaker.
4. The creator's ending clip keeps its own rule.
5. A clip the analysis says speaks to camera is never moved by an ask about no clip in
   particular.

Placement (`schedule_mention_cutaways`):

6. One coincidental shared word is not a mention (two distinct words, rarity-weighted).
7. A word the creator says all through the talk is not a mention.
8. A mention in the hook or the last second never puts a window there.
9. Two clips mentioned at once do not overlap; the later one moves a little or is spread.
10. A window ends where the next clip's mention starts.
11. No words (or none in common): every clip is spread, with the reason.
12. A window never runs over a blocked span (a full-screen Visual).
13. A clip with its own sound may run longer than a silent one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement
from app.kria.cutaway_show_ask import full_screen_show_ask
from app.pipeline.phone_talking_cutaways import TalkingClipRoles
from app.pipeline.phone_talking_mentions import (
    BASIS_MENTION,
    BASIS_SPREAD,
    DEMOTED_NAMED,
    DEMOTED_NOT_TO_CAMERA,
    KEPT_NO_CREATOR_TAKE,
    SPREAD_NO_MENTION,
    SPREAD_NO_ROOM,
    ClipRecord,
    FootageClip,
    clip_keys,
    demote_speakers_to_cutaways,
    match_media_id,
    schedule_mention_cutaways,
)


def _roles(speakers, footage=()):
    order = [*speakers, *footage]
    return TalkingClipRoles(
        speakers=tuple(speakers),
        footage=tuple(footage),
        receipt=tuple(
            {"clip_id": c, "role": "speaker" if c in speakers else "footage"} for c in order
        ),
    )


def _records(**speaks):
    return [ClipRecord(clip_id=c, media_id=f"m-{c}", speaks_to_camera=v) for c, v in speaks.items()]


def _demote(roles, records, *, named=(), whole=True, protect=()):
    return demote_speakers_to_cutaways(
        roles,
        records,
        named_media_ids=named,
        global_ask=whole,
        protect_media_ids=protect,
    )


def test_a_clip_not_to_camera_becomes_footage_and_the_creator_stays_the_spine():
    roles, rows, kept = _demote(_roles(["talk", "me"]), _records(talk=False, me=True), whole=True)
    assert (roles.speakers, roles.footage, kept) == (("me",), ("talk",), None)
    assert rows == [{"clip_id": "talk", "media_id": "m-talk", "reason": DEMOTED_NOT_TO_CAMERA}]
    assert roles.receipt[0] == {
        "clip_id": "talk",
        "role": "footage",
        "was": "speaker",
        "reason": DEMOTED_NOT_TO_CAMERA,
    }


def test_unknown_analysis_is_never_evidence():
    roles, rows, kept = _demote(_roles(["talk", "me"]), _records(talk=None, me=True))
    assert (roles.speakers, rows, kept) == (("talk", "me"), [], None)


def test_when_every_speaker_reads_not_to_camera_nothing_moves():
    roles, rows, kept = _demote(_roles(["a", "b"]), _records(a=False, b=False))
    assert (roles.speakers, rows, kept) == (("a", "b"), [], KEPT_NO_CREATOR_TAKE)


def test_a_global_ask_needs_a_remaining_clip_that_really_speaks_to_camera():
    roles, rows, kept = _demote(_roles(["talk", "me"]), _records(talk=False, me=None))
    assert (roles.speakers, rows, kept) == (("talk", "me"), [], KEPT_NO_CREATOR_TAKE)


def test_a_clip_the_ask_names_moves_even_when_it_speaks_to_camera():
    roles, rows, _kept = _demote(
        _roles(["a", "me"]), _records(a=True, me=True), named={"m-a"}, whole=False
    )
    assert (roles.speakers, roles.footage) == (("me",), ("a",))
    assert rows[0]["reason"] == DEMOTED_NAMED


def test_the_last_speaker_is_never_moved():
    roles, rows, kept = _demote(_roles(["me"]), _records(me=True), named={"m-me"}, whole=False)
    assert (roles.speakers, rows, kept) == (("me",), [], KEPT_NO_CREATOR_TAKE)


def test_the_ending_clip_is_left_to_its_own_rule():
    roles, rows, _ = _demote(
        _roles(["talk", "end", "me"]),
        _records(talk=False, end=False, me=True),
        protect={"m-end"},
    )
    assert roles.speakers == ("me", "end")
    assert [r["clip_id"] for r in rows] == ["talk"]


def test_the_protected_ending_clip_is_not_the_take_that_must_remain():
    roles, rows, kept = _demote(
        _roles(["me", "end"]),
        _records(me=True, end=True),
        named={"m-me"},
        whole=False,
        protect={"m-end"},
    )
    assert (roles.speakers, rows, kept) == (("me", "end"), [], KEPT_NO_CREATOR_TAKE)


def test_naming_the_creators_own_take_never_demotes_it():
    roles, rows, kept = _demote(
        _roles(["me", "talk"]), _records(me=True, talk=False), named={"m-me"}, whole=False
    )
    assert (roles.speakers, rows, kept) == (("me", "talk"), [], KEPT_NO_CREATOR_TAKE)


def test_the_kept_take_leads_the_main_track():
    roles, _rows, _ = _demote(_roles(["x", "talk", "me"]), _records(x=None, talk=False, me=True))
    assert roles.speakers == ("me", "x")


def test_an_ask_about_no_clip_in_particular_leaves_a_clip_that_speaks_to_camera():
    roles, _rows, _ = _demote(_roles(["a", "me"]), _records(a=True, me=True))
    assert roles.speakers == ("a", "me")


def _words(text: str, start: float = 0.0, step: float = 0.5):
    return [
        SimpleNamespace(text=w, start_s=start + i * step, end_s=start + i * step + step * 0.8)
        for i, w in enumerate(text.split())
    ]


def _clip(clip_id, text, footage_s=10.0, cap=4.0):
    return FootageClip(clip_id, footage_s, clip_keys([text]), max_window_s=cap)


TALK = "Maltepe stadium capacity seventy thousand people Euro2032 final"
FILLER = "and so on and so forth we keep going with the story here today friends"


def _creator(*parts: tuple[float, str]):
    words = []
    for start, text in parts:
        words += _words(text, start)
    return words


def test_a_clip_is_placed_where_the_creator_talks_about_what_it_shows():
    words = _creator((0.0, FILLER), (12.0, "the Euro2032 final in Maltepe stadium"))
    windows, unplaced, anchors = schedule_mention_cutaways(
        [_clip("a", TALK)], words=words, duration_s=40.0
    )
    [anchor] = anchors
    assert (anchor.basis, anchor.reason) == (BASIS_MENTION, None)
    assert anchor.at_s == pytest.approx(12.0 + 0.5, abs=0.6)
    assert windows[0].start_s == pytest.approx(anchor.start_s)
    assert unplaced == []


def test_one_coincidental_word_is_not_a_mention():
    words = _creator((0.0, FILLER), (12.0, "the final score"))
    _w, _u, [anchor] = schedule_mention_cutaways([_clip("a", TALK)], words=words, duration_s=40.0)
    assert (anchor.basis, anchor.reason) == (BASIS_SPREAD, SPREAD_NO_MENTION)


def test_a_word_said_all_through_the_talk_is_not_a_mention():
    words = _creator(
        (0.0, "stadium stadium capacity stadium stadium stadium stadium stadium"),
        (8.0, "stadium stadium stadium stadium capacity stadium"),
    )
    _w, _u, [anchor] = schedule_mention_cutaways([_clip("a", TALK)], words=words, duration_s=40.0)
    assert anchor.basis == BASIS_SPREAD


def test_no_words_means_every_clip_is_spread_and_says_why():
    windows, unplaced, anchors = schedule_mention_cutaways(
        [_clip("a", TALK), _clip("b", TALK)], words=[], duration_s=40.0
    )
    assert [a.reason for a in anchors] == [SPREAD_NO_MENTION] * 2
    assert len(windows) == 2 and unplaced == []


def test_a_mention_in_the_hook_never_puts_a_window_in_the_hook():
    words = _creator((0.0, "Euro2032 final Maltepe stadium"), (6.0, FILLER))
    windows, _u, [anchor] = schedule_mention_cutaways(
        [_clip("a", TALK)], words=words, duration_s=40.0
    )
    assert anchor.basis == BASIS_MENTION
    assert windows[0].start_s >= 2.5 - 1e-9


def test_a_mention_in_the_last_second_finds_no_room_and_is_spread():
    words = _creator((0.0, FILLER), (38.2, "Euro2032 final Maltepe"))
    windows, _u, [anchor] = schedule_mention_cutaways(
        [_clip("a", TALK)], words=words, duration_s=39.5
    )
    assert (anchor.basis, anchor.reason) == (BASIS_SPREAD, SPREAD_NO_ROOM)
    assert windows[0].end_s <= 38.5 + 1e-9


def test_two_clips_mentioned_at_once_do_not_overlap():
    words = _creator((0.0, FILLER), (12.0, "the Euro2032 final Maltepe stadium capacity people"))
    windows, _u, anchors = schedule_mention_cutaways(
        [_clip("a", TALK), _clip("b", TALK)], words=words, duration_s=40.0
    )
    spans = sorted((w.start_s, w.end_s) for w in windows)
    assert len(spans) == 2
    assert spans[0][1] <= spans[1][0] + 1e-9
    assert {a.clip_id for a in anchors} == {"a", "b"}


def test_a_window_ends_where_the_next_clips_mention_starts():
    other = "Besiktas ticket prices increased supporters protest"
    words = _creator(
        (0.0, FILLER),
        (10.0, "the Euro2032 final Maltepe stadium capacity"),
        (14.0, "Besiktas ticket prices supporters protest"),
    )
    windows, _u, _a = schedule_mention_cutaways(
        [_clip("a", TALK, cap=6.0), _clip("b", other, cap=6.0)],
        words=words,
        duration_s=40.0,
    )
    first, second = sorted(windows, key=lambda w: w.start_s)
    assert first.clip_id == "a"
    assert first.end_s <= second.start_s + 1e-9
    assert first.end_s - first.start_s < 6.0


def test_a_blocked_span_is_left_free():
    words = _creator((0.0, FILLER), (12.0, "the Euro2032 final Maltepe stadium capacity"))
    windows, _u, _a = schedule_mention_cutaways(
        [_clip("a", TALK)], words=words, duration_s=40.0, blocked=[(11.0, 15.0)]
    )
    assert windows[0].start_s >= 15.0 - 1e-9 or windows[0].end_s <= 11.0 + 1e-9


def test_a_talk_with_its_own_sound_may_run_longer_than_a_silent_cutaway():
    words = _creator((0.0, FILLER), (12.0, "the Euro2032 final Maltepe stadium capacity"))
    [silent], _u, _a = schedule_mention_cutaways([_clip("a", TALK)], words=words, duration_s=40.0)
    [sounding], _u, _a = schedule_mention_cutaways(
        [_clip("a", TALK, cap=6.0)], words=words, duration_s=40.0
    )
    assert silent.end_s - silent.start_s == pytest.approx(4.0)
    assert sounding.end_s - sounding.start_s == pytest.approx(6.0)


def _req(rid, kind, scope, **facts):
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, status="open", description="x", facts=facts
    )


def test_the_ask_is_read_from_the_recorded_display_fact_only():
    ask = full_screen_show_ask(
        [
            _req("r1", "style", "global", display_mode="Full Screen"),
            _req("r2", "style", "clip:abc-def.mp4", display_mode="fullscreen"),
            _req("r3", "style", "title", display_mode="fullscreen"),
            _req("r4", "style", "global", display_mode="pip"),
        ]
    )
    assert ask is not None
    assert (ask.whole_video, ask.media_refs, ask.order_asked) == (True, ("abc-def.mp4",), False)
    assert ask.requirement_ids == ("r1", "r2", "r3")


def test_no_fact_no_ask_and_a_superseded_ask_is_no_ask():
    gone = _req("r1", "style", "global", display_mode="fullscreen").model_copy(
        update={"status": "superseded"}
    )
    assert full_screen_show_ask([gone, _req("r2", "style", "global")]) is None


def test_a_live_order_ask_is_flagged():
    ask = full_screen_show_ask(
        [_req("r1", "style", "global", display_mode="fullscreen"), _req("r2", "order", "global")]
    )
    assert ask is not None and ask.order_asked


def test_a_clip_is_named_by_the_phone_file_name_or_a_unique_id_part():
    ids = [
        "analysis-proxy-ios-134CDBD9-2781-4407-921B-57D663D2B33B.mp4",
        "analysis-proxy-ios-AF8BF2A5-B06D-4089-A8CA-C55BF924F243.mp4",
    ]
    assert match_media_id("134CDBD9-2781-4407-921B-57D663D2B33B.mp4", ids) == ids[0]
    assert match_media_id("AF8BF2A5", ids) == ids[1]
    assert match_media_id("analysis-proxy-ios-AF8BF2A5-B06D-4089-A8CA-C55BF924F243.mp4", ids)
    assert match_media_id("2781", ids) is None
    assert match_media_id("zzzzzz", ids) is None


def test_ordinary_shared_words_are_not_a_mention():
    # "bizim" and "uzeri" are everyday words: two shared, none distinctive.
    words = _creator((0.0, FILLER), (12.0, "bizim kagit uzeri"))
    clip = _clip("a", "bizim kagit uzeri getirirsiniz")
    _w, _u, [anchor] = schedule_mention_cutaways([clip], words=words, duration_s=40.0)
    assert anchor.basis == BASIS_SPREAD


def test_a_photo_pop_in_ask_is_not_a_show_the_talks_ask():
    ask = full_screen_show_ask(
        [_req("r1", "style", "global", display_mode="fullscreen", triggers=["grill"])]
    )
    assert ask is None


def test_a_talk_with_its_own_sound_starts_after_the_creator_has_said_it():
    words = _creator((0.0, FILLER), (12.0, "the Euro2032 final Maltepe stadium capacity"))
    [silent], _u, _a = schedule_mention_cutaways([_clip("a", TALK)], words=words, duration_s=40.0)
    [sounding], _u, _a = schedule_mention_cutaways(
        [_clip("a", TALK, cap=6.0)], words=words, duration_s=40.0
    )
    assert sounding.start_s > silent.start_s
    assert sounding.start_s >= 14.5
