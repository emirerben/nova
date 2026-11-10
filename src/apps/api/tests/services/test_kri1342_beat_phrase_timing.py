"""KRI-1342 item 8: a picture cued by what the talk is about starts with the phrase naming it.

Real grounding (`ground_phone_reaction_beats`) on timed words shaped like the yorum3 take
(punctuated, a restarted sentence, a comma inside a two-word cue) and a Turkish take; only the
pool/sfx/geometry DB seams are stubbed. Failure modes: docs/reviews/kri-1342-beat-phrase-timing.md.
"""

from __future__ import annotations

from dataclasses import replace

import app.services.phone_reaction_grounding as grounding
from app.pipeline.phrase_window import phrase_window, sentence_cut_inside


def _open_session():
    raise AssertionError("database seams must be mocked")


def _asset(asset_id: str, kind: str = "image", duration_s: float | None = None) -> dict:
    return {
        "id": asset_id,
        "gcs_path": f"users/owner/pool/{asset_id}.jpg",
        "gcs_generation": "7",
        "kind": kind,
        "source_filename": f"{asset_id}.jpg",
        "duration_s": duration_s,
        "aspect": 1.0,
        "user_context": "",
        "analysis": {},
    }


def _timed(spec: str) -> list[dict]:
    """``"word@start-end word@start-end ..."`` -> timed words."""
    out = []
    for item in spec.split():
        text, times = item.rsplit("@", 1)
        start, end = times.split("-")
        out.append({"text": text.replace("_", " "), "start_s": float(start), "end_s": float(end)})
    return out


# The yorum3 shape: a sentence whose last word is the cue, a restarted line, "Welcome, Lumo."
# (a cue across a comma) followed by its own quote, a cue after "Then,".
EN_TAKE = _timed(
    "Ten@2.16-2.28 minutes@2.28-2.62 after@2.62-2.98 Zorb@2.98-3.18 launched@3.18-3.58 "
    "its@3.58-3.82 first@3.82-4.24 folding@4.24-4.56 phone,@4.56-4.98 Vex@5.54-5.58 "
    "posted@5.58-5.94 online.@5.94-6.40 First,@9.08-9.10 Welcome,@9.82-9.82 Zorb.@10.02-10.30 "
    "We've@10.48-10.66 waited@10.66-11.02 for@11.02-11.26 you@11.26-11.48 for@11.48-11.72 "
    "years.@11.72-12.54 Then,@12.94-13.38 A@13.72-13.92 hinge?@13.92-14.22 We@14.58-14.78 "
    "call@14.78-14.94 that@14.94-15.16 an@15.16-15.32 old@15.32-15.56 friend.@15.56-15.96 "
    "Like@19.02-19.30 car@19.30-19.50 brands@19.50-19.78 fighting@19.78-20.08 on@20.08-20.52 "
    "bill...@20.52-20.88 Sorry,@20.88-21.22 again.@21.22-21.84 Like@25.06-25.52 car@25.52-25.88 "
    "brands@25.88-26.14 fighting@26.14-26.46 on@26.46-26.76 billboards.@26.76-27.10 "
    "So,@27.24-27.60 how@28.28-28.36 do@28.36-28.46 I@28.46-28.64 read@28.64-28.84 "
    "this?@28.84-29.20"
)
BILLBOARDS_CUE = (26.76, 27.10)
EN_PHRASE_STARTS = {"launch": 2.16, "post1": 9.82, "post2": 13.72, "billboards": 25.06}
EN_BEATS = [
    {"beat_id": "launch", "trigger": "launched", "visual_id": "p-launch"},
    {"beat_id": "post1", "trigger": "welcome Zorb", "visual_id": "p-post1"},
    {"beat_id": "post2", "trigger": "a hinge", "visual_id": "p-post2"},
    {"beat_id": "billboards", "trigger": "billboards", "visual_id": "p-bill"},
]

# Turkish: the cue noun sits mid-clause, after a comma-led opener; the next sentence follows.
TR_TAKE = _timed(
    "Geçen@0.40-0.70 hafta@0.70-1.00 Kadıköy'de,@1.00-1.60 sahilde@1.80-2.20 "
    "yürürken@2.20-2.70 kocaman@2.70-3.10 bir@3.10-3.25 martı@3.25-3.60 simidimi@3.60-4.10 "
    "kaptı.@4.10-4.60 Sonra@5.20-5.50 ne@5.50-5.65 oldu@5.65-5.95 biliyor@5.95-6.30 "
    "musunuz?@6.30-6.80"
)


def _ground(
    monkeypatch,
    beats: list[dict],
    words: list[dict],
    *,
    timing: grounding.PhraseTiming | None = grounding.TALKING_PHRASE_TIMING,
    fullscreen: bool = True,
    assets: list[dict] | None = None,
    duration_s: float = 40.0,
):
    beats = [
        {"visual_role": "photo", **({"display_mode": "fullscreen"} if fullscreen else {}), **b}
        for b in beats
    ]
    pool = assets or [_asset(b["visual_id"]) for b in beats if b.get("visual_id")]
    monkeypatch.setattr(grounding, "_load_ready_pool_assets", lambda *a, **k: pool)
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *a, **k: [])

    def geometry(overlays, **kwargs):
        return {overlay["id"]: overlay for overlay in overlays}, {}, "stubbed"

    monkeypatch.setattr(grounding, "resolve_phone_card_geometry", geometry)
    return grounding.ground_phone_reaction_beats(
        _open_session,
        job_id="job-1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=duration_s,
        clip_path=None,
        allow_fullscreen=True,
        video_supported=True,
        fullscreen_hold_s_default=grounding.TALKING_FULLSCREEN_HOLD_S_DEFAULT,
        **({"phrase_timing": timing} if timing is not None else {}),
    )


def _windows(result) -> dict[str, tuple[float, float]]:
    return {e["beat_id"]: (e["at_s"], e["end_s"]) for e in result.receipt["placed"]}


def _cue_ends(words: list[dict], beats: list[dict]) -> dict[str, float]:
    stream = grounding._token_stream(words)  # noqa: SLF001
    out = {}
    for beat in beats:
        match = grounding._match_trigger(stream, beat["trigger"], None, "first", words=words)  # noqa: SLF001
        out[beat["beat_id"]] = match.windows[0][1]
    return out


def _phrase_ok(words, beats, windows, starts) -> dict[str, bool]:
    """The pass check: starts within 0.3 s of the phrase start and never cuts away inside a
    sentence that began after the cue."""
    cue_ends = _cue_ends(words, beats)
    return {
        beat_id: abs(start - starts[beat_id]) <= 0.3
        and not sentence_cut_inside(words, start, end, cue_end_s=cue_ends[beat_id])
        for beat_id, (start, end) in windows.items()
    }


def test_yorum3_shape_every_photo_starts_with_its_phrase_and_stops_before_the_next(monkeypatch):
    result = _ground(monkeypatch, EN_BEATS, EN_TAKE)

    windows = _windows(result)
    assert set(windows) == set(EN_PHRASE_STARTS)
    assert all(_phrase_ok(EN_TAKE, EN_BEATS, windows, EN_PHRASE_STARTS).values()), windows
    # "billboards" is the last word of its phrase: the photo now covers the whole phrase and
    # is gone before "So, how do I read this?".
    assert windows["billboards"][0] < 25.06 <= windows["billboards"][0] + 0.1
    assert windows["billboards"][1] <= 27.24
    # Every full-screen hold stays within the Talking bounds.
    assert all(1.5 <= end - start <= 3.0 + 1e-6 for start, end in windows.values())
    bill = next(e for e in result.receipt["placed"] if e["beat_id"] == "billboards")
    assert (bill["timing"], bill["cue_at_s"]) == ("phrase", 26.76)


def test_negative_control_word_timing_fails_the_phrase_check(monkeypatch):
    """Without phrase timing (the yorum3 render), the same check rejects the windows."""
    result = _ground(monkeypatch, EN_BEATS, EN_TAKE, timing=None)

    ok = _phrase_ok(EN_TAKE, EN_BEATS, _windows(result), EN_PHRASE_STARTS)
    assert ok == {"launch": False, "post1": False, "post2": False, "billboards": False}
    assert "timing" not in result.receipt["placed"][0]


def test_a_word_the_creator_named_keeps_its_exact_word_timing(monkeypatch):
    timing = replace(grounding.TALKING_PHRASE_TIMING, word_cued=frozenset({"billboards"}))
    result = _ground(monkeypatch, EN_BEATS, EN_TAKE, timing=timing)

    windows = _windows(result)
    assert windows["billboards"] == (26.76, 28.26)
    assert windows["post2"][0] < 13.72  # the others still follow their phrase


def test_creator_named_cues_reads_picture_and_sound_asks_not_text_asks():
    class Req:
        def __init__(self, kind, facts):
            self.kind, self.facts = kind, facts

    cues = grounding.creator_named_cues(
        [
            Req("style", {"triggers": ["Billboards", "kahve"]}),
            Req("timing", {"trigger": "kano"}),  # the cabin T2 brief's singular key
            Req("text", {"triggers": ["SpecKit"]}),  # Qendresa Q1's list label
            Req("style", {"display_mode": "fullscreen"}),
        ]
    )
    assert cues == frozenset({"billboards", "kahve", "kano"})


def test_after_cue_sticker_and_sound_stay_on_the_word(monkeypatch):
    beats = [
        {"beat_id": "after", "trigger": "billboards", "after": "brands", "visual_id": "p1"},
        {"beat_id": "sticker", "trigger": "hinge", "visual_id": "s1", "visual_role": "sticker"},
    ]
    result = _ground(monkeypatch, beats, EN_TAKE, fullscreen=False)

    windows = _windows(result)
    assert windows["after"][0] == 26.76
    assert windows["sticker"][0] == 13.92


def test_explicit_hold_keeps_its_length_and_moves_its_start(monkeypatch):
    beats = [{"beat_id": "b", "trigger": "billboards", "visual_id": "p", "hold_s": 2.5}]
    result = _ground(monkeypatch, beats, EN_TAKE)

    start, end = _windows(result)["b"]
    assert abs(start - 25.06) <= 0.1
    assert round(end - start, 3) == 2.5


def test_two_pictures_in_one_phrase_both_stay(monkeypatch):
    words = _timed(
        "We@1.00-1.20 saw@1.20-1.50 the@1.50-1.60 launch@1.60-2.00 and@2.00-2.20 "
        "the@2.20-2.30 billboards@2.30-2.90 together.@2.90-3.40 Next@4.20-4.50 up.@4.50-4.90"
    )
    beats = [
        {"beat_id": "a", "trigger": "launch", "visual_id": "pa"},
        {"beat_id": "b", "trigger": "billboards", "visual_id": "pb"},
    ]
    result = _ground(monkeypatch, beats, words)

    windows = _windows(result)
    assert set(windows) == {"a", "b"}
    assert windows["a"][0] <= 1.0
    # The second picture waits for the first one's own word, then takes over.
    assert windows["b"][0] >= 2.0
    assert windows["a"][1] == windows["b"][0]


def test_a_run_on_without_punctuation_keeps_the_cue_start():
    words = _timed(" ".join(f"w{i}@{1 + i * 0.3:.2f}-{1.25 + i * 0.3:.2f}" for i in range(20)))
    cue = words[15]
    window = phrase_window(words, cue["start_s"], cue["end_s"], min_s=1.5, max_s=3.0)

    assert window is not None and window.basis == "trigger"
    assert window.start_s >= cue["start_s"] - 0.1


def test_turkish_cue_mid_clause_starts_at_the_clause(monkeypatch):
    beats = [{"beat_id": "marti", "trigger": "martı", "visual_id": "p"}]
    result = _ground(monkeypatch, beats, TR_TAKE)

    start, end = _windows(result)["marti"]
    assert abs(start - 1.80) <= 0.3
    assert end <= 5.20  # never into "Sonra ne oldu ..."


def test_photo_then_clip_sequence_keeps_its_length(monkeypatch):
    beats = [
        {
            "beat_id": "seq",
            "trigger": "billboards",
            "visual_id": "p",
            "following_clip_id": "clip-1",
        }
    ]
    result = _ground(monkeypatch, beats, EN_TAKE, timing=grounding.VOICEOVER_PHRASE_TIMING)

    start, end = _windows(result)["seq"]
    assert round(end - start, 3) == 1.5
    # Too short to reach back to "Like": it still covers the whole cue word, ending as
    # "billboards." ends (the following clip takes over there), as early as that allows.
    assert start <= BILLBOARDS_CUE[0] and end >= BILLBOARDS_CUE[1]
    assert end == BILLBOARDS_CUE[1]


def test_a_video_beat_keeps_its_length_rules(monkeypatch):
    beats = [{"beat_id": "v", "trigger": "billboards", "visual_id": "vid"}]
    result = _ground(
        monkeypatch,
        beats,
        EN_TAKE,
        assets=[_asset("vid", kind="video", duration_s=1.2)],
    )

    start, end = _windows(result)["v"]
    assert round(end - start, 3) == 1.2
    assert start <= BILLBOARDS_CUE[0] and end >= BILLBOARDS_CUE[1]
    assert end == BILLBOARDS_CUE[1]


def test_every_window_is_on_screen_while_its_cue_is_said(monkeypatch):
    """Emir's review of #2257: an earlier start must never leave a visual ending before its
    own word. Fixed short lengths (explicit holds, a short video) and phrase-sized ones alike."""
    beats = [
        {"beat_id": "short", "trigger": "billboards", "visual_id": "p1", "hold_s": 0.5},
        {"beat_id": "launch", "trigger": "launched", "visual_id": "p2"},
        {"beat_id": "post1", "trigger": "welcome Zorb", "visual_id": "p3", "hold_s": 0.6},
    ]
    result = _ground(monkeypatch, beats, EN_TAKE)
    cues = {"short": BILLBOARDS_CUE, "launch": (3.18, 3.58), "post1": (9.82, 10.30)}

    windows = _windows(result)
    for beat_id, (start, end) in windows.items():
        cue_start, cue_end = cues[beat_id]
        # On screen at the cue's first word, and through its last word when long enough.
        assert start <= cue_start < end, beat_id
        assert end >= min(cue_end, cue_start + (end - start)), beat_id
    assert windows["short"] == (26.6, 27.1)  # ends as "billboards." ends


def test_a_silent_footage_cutaway_starts_with_its_mention_phrase():
    """Talking own-footage cutaways (parent KRI-1151) follow the same rule when asked."""
    from types import SimpleNamespace

    from app.pipeline.phone_talking_mentions import (
        FootageClip,
        clip_keys,
        schedule_mention_cutaways,
    )

    words = [
        SimpleNamespace(text=w["text"], start_s=w["start_s"], end_s=w["end_s"])
        for w in _timed(
            "We@1.00-1.20 kept@1.20-1.50 going.@1.50-2.00 Then@5.00-5.30 we@5.30-5.50 "
            "walked@5.50-5.90 into@5.90-6.10 the@6.10-6.20 Maltepe@6.20-6.70 "
            "stadium@6.70-7.20 tonight.@7.20-7.70 Wow.@9.00-9.40"
        )
    ]
    clip = FootageClip("a", 10.0, clip_keys(["Maltepe stadium"]), max_window_s=4.0)

    before, _u, _a = schedule_mention_cutaways([clip], words=words, duration_s=20.0)
    after, _u, _a = schedule_mention_cutaways(
        [clip], words=words, duration_s=20.0, phrase_starts=True
    )

    assert before[0].start_s == 6.2
    assert abs(after[0].start_s - 5.0) <= 0.1


def test_short_footage_stays_on_screen_through_its_mention():
    """Review #2257: a cutaway shorter than the phrase keeps its own length, so moving it to
    the phrase start must not make it disappear before "Maltepe stadium" is said. Every
    footage length stays on screen while its mention starts and through its end when it can."""
    from types import SimpleNamespace

    from app.pipeline.phone_talking_mentions import (
        FootageClip,
        clip_keys,
        schedule_mention_cutaways,
    )

    words = [
        SimpleNamespace(text=w["text"], start_s=w["start_s"], end_s=w["end_s"])
        for w in _timed(
            "We@1.00-1.20 kept@1.20-1.50 going.@1.50-2.00 Then@5.00-5.30 we@5.30-5.50 "
            "walked@5.50-5.90 into@5.90-6.10 the@6.10-6.20 Maltepe@6.20-6.70 "
            "stadium@6.70-7.20 tonight.@7.20-7.70 Wow.@9.00-9.40"
        )
    ]
    mention_start, mention_end = 6.2, 7.2

    one_second = FootageClip("a", 1.0, clip_keys(["Maltepe stadium"]), max_window_s=4.0)
    after, _u, _a = schedule_mention_cutaways(
        [one_second], words=words, duration_s=20.0, phrase_starts=True
    )
    assert (after[0].start_s, after[0].end_s) == (6.2, 7.2)  # not (4.9, 5.9)

    for footage_s in (0.6, 1.0, 1.5, 2.0, 2.5, 10.0):
        clip = FootageClip("a", footage_s, clip_keys(["Maltepe stadium"]), max_window_s=4.0)
        (window,), _u, _a = schedule_mention_cutaways(
            [clip], words=words, duration_s=20.0, phrase_starts=True
        )
        length = window.end_s - window.start_s
        assert window.start_s <= mention_start < window.end_s, footage_s
        assert window.end_s >= min(mention_end, window.start_s + length) - 1e-6, footage_s


def test_two_clips_named_in_one_phrase_each_stay_on_their_own_mention():
    """Two cutaways named in one sentence: neither starts before the other's words, neither
    loses its mention anchor, and each is on screen as its own name is said."""
    from types import SimpleNamespace

    from app.pipeline.phone_talking_mentions import (
        BASIS_MENTION,
        FootageClip,
        clip_keys,
        schedule_mention_cutaways,
    )

    words = [
        SimpleNamespace(text=w["text"], start_s=w["start_s"], end_s=w["end_s"])
        for w in _timed(
            "We@1.00-1.20 kept@1.20-1.50 going.@1.50-2.00 Then@5.00-5.30 we@5.30-5.50 "
            "saw@5.50-5.90 the@5.90-6.00 Kadikoy@6.00-6.40 ferry@6.40-6.80 and@6.80-7.00 "
            "the@7.00-7.10 Maltepe@7.20-7.70 stadium@7.70-8.20 tonight.@8.20-8.70 "
            "Wow.@11.00-11.40 Okay@13.00-13.40 bye.@13.40-13.80"
        )
    ]
    said = {"ferry": (6.0, 6.8), "stad": (7.2, 8.2)}
    for footage_s in (1.0, 2.0, 10.0):
        clips = [
            FootageClip("ferry", footage_s, clip_keys(["Kadikoy ferry"]), max_window_s=4.0),
            FootageClip("stad", footage_s, clip_keys(["Maltepe stadium"]), max_window_s=4.0),
        ]
        windows, _u, anchors = schedule_mention_cutaways(
            clips, words=words, duration_s=20.0, phrase_starts=True
        )
        assert [a.basis for a in anchors] == [BASIS_MENTION, BASIS_MENTION], footage_s
        for window in windows:
            start, end = said[window.clip_id]
            assert window.start_s <= start < window.end_s, (footage_s, window)
            assert window.end_s >= end - 1e-6 or window.end_s - window.start_s >= 1.5, (
                footage_s,
                window,
            )


def test_worker_passes_nothing_while_the_flag_is_off(monkeypatch):
    """Flag off: the grounding call gets no ``phrase_timing`` (byte-identical windows)."""
    from app.tasks import generative_build

    class Req:
        facts = {"triggers": ["billboards"]}

    monkeypatch.setattr(generative_build.settings, "beat_phrase_timing_enabled", False)
    assert (
        generative_build._beat_phrase_timing_kwargs(grounding.TALKING_PHRASE_TIMING, [Req()]) == {}
    )  # noqa: SLF001

    monkeypatch.setattr(generative_build.settings, "beat_phrase_timing_enabled", True)
    kwargs = generative_build._beat_phrase_timing_kwargs(grounding.TALKING_PHRASE_TIMING, [Req()])  # noqa: SLF001
    assert kwargs["phrase_timing"].word_cued == frozenset({"billboards"})
    assert kwargs["phrase_timing"].max_s == grounding.TALKING_PHRASE_TIMING.max_s


def test_a_corner_screenshot_stays_through_the_explanation_after_its_name(monkeypatch):
    """Qendresa Q1 shape: "Number one, Spec Kit. You write ... from it." The corner screenshot
    used to leave 3 s in, mid-way through the explanation; it now stays until it ends."""
    words = _timed(
        "Number@7.52-7.72 one,@7.72-8.08 Spec@8.48-8.54 Kit.@8.54-8.80 You@9.26-9.42 "
        "write@9.42-9.62 the@9.62-9.94 spec@9.94-9.94 first,@9.94-10.44 and@10.46-10.64 "
        "your@10.64-11.08 agent@11.08-11.08 builds@11.08-11.38 it.@11.38-12.84 "
        "Number@14.48-14.62 two.@14.62-15.04"
    )
    beats = [{"beat_id": "repo", "trigger": "Spec Kit", "visual_id": "shot"}]
    before = _windows(_ground(monkeypatch, beats, words, fullscreen=False, timing=None))["repo"]
    after = _windows(_ground(monkeypatch, beats, words, fullscreen=False))["repo"]

    assert before == (8.48, 11.48)  # leaves inside "and your agent builds it."
    assert abs(after[0] - 8.48) <= 0.1
    assert 12.84 <= after[1] <= 14.48
