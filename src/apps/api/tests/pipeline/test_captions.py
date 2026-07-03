from __future__ import annotations

from app.pipeline.captions import (
    _ACTIVE_WORD_ASS_COLOR,
    _ASS_HEADER,
    _SENTENCE_POP_TAGS,
    SUBTITLED_CAPTION_MARGIN_V,
    build_plain_cues,
    build_word_cues,
    generate_ass,
    generate_ass_from_cues,
    generate_word_pop_ass,
    resplit_cues_into_sentences,
)
from app.pipeline.transcribe import Transcript, Word


def test_resplit_multi_sentence_cue_proportional():
    """A corrected 4-sentence mega-cue splits into one cue per sentence, timing
    distributed across the original window, monotonic and span-preserving."""
    cues = [
        {
            "text": "Bu bizim zaten dünkü hareket. Kaçar adet yapıyoruz? "
            "Bu nereyi çalıştırıyor? Kalça bölgesini.",
            "start_s": 0.96,
            "end_s": 11.14,
        }
    ]
    out = resplit_cues_into_sentences(cues)
    assert [c["text"] for c in out] == [
        "Bu bizim zaten dünkü hareket.",
        "Kaçar adet yapıyoruz?",
        "Bu nereyi çalıştırıyor?",
        "Kalça bölgesini.",
    ]
    assert out[0]["start_s"] == 0.96 and out[-1]["end_s"] == 11.14  # span preserved
    for a, b in zip(out, out[1:]):
        assert a["end_s"] <= b["start_s"] + 1e-6  # monotonic, non-overlapping
        assert a["end_s"] > a["start_s"]


def test_resplit_uses_word_timings_when_aligned():
    """When the cue's words still spell its text, sentence boundaries land on the real
    word times (audio-locked), not proportional estimates."""
    cues = [
        {
            "text": "Merhaba dünya. Nasılsın bugün.",
            "start_s": 0.0,
            "end_s": 10.0,
            "words": [
                {"text": "Merhaba", "start_s": 0.0, "end_s": 0.8},
                {"text": "dünya.", "start_s": 0.8, "end_s": 1.2},
                {"text": "Nasılsın", "start_s": 7.0, "end_s": 7.8},  # long silence gap
                {"text": "bugün.", "start_s": 7.8, "end_s": 8.4},
            ],
        }
    ]
    out = resplit_cues_into_sentences(cues)
    assert len(out) == 2
    # Sentence 1 ends at its last word's end; sentence 2 starts at ITS first word — the
    # 6s silence between them stays caption-free (proportional would put it mid-split).
    assert out[0]["end_s"] == 1.2
    assert out[1]["start_s"] == 7.0


def test_resplit_merges_dangling_fragment_into_next_cue():
    """whisper split 'Ona / onar adet yapıyoruz.' across cues — the unpunctuated
    ≤2-word tail merges into the next cue so the sentence displays whole."""
    cues = [
        {"text": "Kalça bölgesini. Ona", "start_s": 0.0, "end_s": 11.0},
        {"text": "onar adet yapıyoruz.", "start_s": 11.0, "end_s": 12.1},
    ]
    out = resplit_cues_into_sentences(cues)
    assert [c["text"] for c in out] == ["Kalça bölgesini.", "Ona onar adet yapıyoruz."]
    assert out[1]["end_s"] == 12.1


def test_resplit_single_sentence_cue_is_unchanged():
    cues = [{"text": "Onar adet yapıyoruz.", "start_s": 1.0, "end_s": 2.0}]
    assert resplit_cues_into_sentences(cues) == [
        {"text": "Onar adet yapıyoruz.", "start_s": 1.0, "end_s": 2.0}
    ]


def test_resplit_preserves_words_on_unsplit_cues():
    """REGRESSION (specialist-found): the merge/monotonic passes used to rebuild bare
    {text,start_s,end_s} cues, stripping `words` — so word-pop NEVER got real timings
    in production and always fell back to the even split."""
    cue = {
        "text": "bugün size çok.",
        "start_s": 0.0,
        "end_s": 1.5,
        "words": [
            {"text": "bugün", "start_s": 0.0, "end_s": 0.5},
            {"text": "size", "start_s": 0.5, "end_s": 0.9},
            {"text": "çok.", "start_s": 0.9, "end_s": 1.5},
        ],
    }
    out = resplit_cues_into_sentences([cue])
    assert out[0].get("words"), "unsplit cue must keep real per-word timings for word-pop"
    assert [w["text"] for w in out[0]["words"]] == ["bugün", "size", "çok."]


def test_resplit_aligned_split_slices_words_per_sentence():
    """A word-aligned multi-sentence cue slices its real word timings onto each piece,
    so word-pop stays audio-locked after the sentence re-split."""
    cue = {
        "text": "Merhaba dünya. Nasılsın bugün.",
        "start_s": 0.0,
        "end_s": 10.0,
        "words": [
            {"text": "Merhaba", "start_s": 0.0, "end_s": 0.8},
            {"text": "dünya.", "start_s": 0.8, "end_s": 1.2},
            {"text": "Nasılsın", "start_s": 7.0, "end_s": 7.8},
            {"text": "bugün.", "start_s": 7.8, "end_s": 8.4},
        ],
    }
    out = resplit_cues_into_sentences([cue])
    assert len(out) == 2
    assert [w["text"] for w in out[0]["words"]] == ["Merhaba", "dünya."]
    assert [w["text"] for w in out[1]["words"]] == ["Nasılsın", "bugün."]
    assert out[1]["words"][0]["start_s"] == 7.0  # real audio time, not proportional


def test_resplit_fragment_merge_concats_words_when_both_sides_have_them():
    cues = [
        {
            "text": "Ona",
            "start_s": 10.0,
            "end_s": 11.0,
            "words": [{"text": "Ona", "start_s": 10.0, "end_s": 11.0}],
        },
        {
            "text": "onar adet yapıyoruz.",
            "start_s": 11.0,
            "end_s": 12.1,
            "words": [
                {"text": "onar", "start_s": 11.0, "end_s": 11.4},
                {"text": "adet", "start_s": 11.4, "end_s": 11.7},
                {"text": "yapıyoruz.", "start_s": 11.7, "end_s": 12.1},
            ],
        },
    ]
    out = resplit_cues_into_sentences(cues)
    assert len(out) == 1
    assert out[0]["text"] == "Ona onar adet yapıyoruz."
    assert [w["text"] for w in out[0]["words"]] == ["Ona", "onar", "adet", "yapıyoruz."]


def test_resplit_boundaries():
    assert resplit_cues_into_sentences([]) == []
    # empty-text cue dropped
    assert resplit_cues_into_sentences([{"text": "  ", "start_s": 0.0, "end_s": 1.0}]) == []
    # a final-position ≤2-word unpunctuated fragment has no next cue → stays standalone
    out = resplit_cues_into_sentences([{"text": "Ona", "start_s": 0.0, "end_s": 1.0}])
    assert [c["text"] for c in out] == ["Ona"]


def test_resplit_does_not_split_after_turkish_ordinals():
    """"3. gün böyle geçti." must NOT split into a dangling "3." caption (the
    fragment-merge can't rescue it — "3." carries terminal punctuation)."""
    cues = [{"text": "Bu 3. gün böyle geçti. Devam ediyoruz.", "start_s": 0.0, "end_s": 4.0}]
    out = resplit_cues_into_sentences(cues)
    assert [c["text"] for c in out] == ["Bu 3. gün böyle geçti.", "Devam ediyoruz."]


def test_attach_words_tokenization_matches_text_with_stray_punct():
    """A punctuation-only whisper token coalesces into the previous word in BOTH the
    cue text and the attached words — otherwise the aligned check silently fails and
    word-pop degrades to synthesized timing."""
    words = [
        Word(text="hello", start_s=0.0, end_s=0.5, confidence=1.0),
        Word(text=".", start_s=0.5, end_s=0.6, confidence=1.0),  # stray token
        Word(text="World", start_s=0.6, end_s=1.0, confidence=1.0),
        Word(text="two.", start_s=1.0, end_s=1.4, confidence=1.0),
    ]
    cues = build_plain_cues(words, attach_words=True)
    for cue in cues:
        toks = cue["text"].split()
        assert [w["text"] for w in cue["words"]] == toks  # 1:1, spellings match


def test_sanitize_strips_carriage_returns():
    from app.pipeline.ass_utils import sanitize_ass_text

    assert sanitize_ass_text("a\r\nb") == "a\\Nb"
    assert sanitize_ass_text("a\rb") == "a\\Nb"


def test_build_plain_cues_attach_words_offset_and_default():
    words = [
        Word(text="hello", start_s=2.0, end_s=2.5, confidence=1.0),
        Word(text="world.", start_s=2.5, end_s=3.0, confidence=1.0),
    ]
    attached = build_plain_cues(words, offset_s=2.0, attach_words=True)
    assert attached[0]["words"][0] == {"text": "hello", "start_s": 0.0, "end_s": 0.5}
    assert attached[0]["words"][1]["start_s"] == 0.5  # offset-shifted
    # default stays byte-identical: no words key
    assert "words" not in build_plain_cues(words, offset_s=2.0)[0]


def test_pop_in_prepends_animation_tags_only_when_asked(tmp_path):
    cues = [{"text": "Kaçar adet yapıyoruz?", "start_s": 0.0, "end_s": 1.5}]
    pop = tmp_path / "pop.ass"
    generate_ass_from_cues(
        cues, str(pop), style="plain", margin_v=SUBTITLED_CAPTION_MARGIN_V, pop_in=True
    )
    pop_lines = pop.read_text(encoding="utf-8").splitlines()
    (line,) = [ln for ln in pop_lines if ln.startswith("Dialogue:")]
    assert _SENTENCE_POP_TAGS in line and "\\fad(120,0)" in line and "\\t(0,140" in line
    # narrated default stays byte-identical: no tags without pop_in
    plain = tmp_path / "plain.ass"
    generate_ass_from_cues(cues, str(plain), style="plain")
    (pline,) = [
        ln for ln in plain.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")
    ]
    assert "\\fad" not in pline and "\\t(" not in pline


def _events(ass_path) -> list[str]:
    return [
        ln for ln in ass_path.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")
    ]


def _style_line(ass_path) -> str:
    lines = ass_path.read_text(encoding="utf-8").splitlines()
    return next(ln for ln in lines if ln.startswith("Style: Default"))


def test_word_pop_highlights_one_word_per_event_with_real_timings(tmp_path):
    """Unedited cue: one event per spoken word, full line visible each time, the active
    word popped lime, using the real per-word timings (locked to audio)."""
    cue = {
        "text": "bugün size çok",
        "start_s": 0.0,
        "end_s": 1.5,
        "words": [
            {"text": "bugün", "start_s": 0.0, "end_s": 0.5},
            {"text": "size", "start_s": 0.5, "end_s": 0.9},
            {"text": "çok", "start_s": 0.9, "end_s": 1.5},
        ],
    }
    out = tmp_path / "pop.ass"
    generate_word_pop_ass([cue], str(out), font_name="TikTok Sans")
    events = _events(out)
    tokens = ["bugün", "size", "çok"]
    assert len(events) == 3  # one event per word
    for i, ev in enumerate(events):
        # every event shows the FULL line (no baseline jitter)
        for t in tokens:
            assert t in ev
        # exactly the i-th word is popped lime, exactly once
        assert ev.count(_ACTIVE_WORD_ASS_COLOR) == 1
        assert f"{{\\c{_ACTIVE_WORD_ASS_COLOR}&}}{tokens[i]}" in ev
    # Real word timings drive the event boundaries (an even split over [0,1.5]
    # would put event 2 at 0.50→1.00 — assert the true 0.50→0.90 window instead).
    assert "Dialogue: 0,0:00:00.50,0:00:00.90" in events[1]
    assert events[2].startswith("Dialogue: 0,0:00:00.90")
    # line-visible look (plain 78px at the safe margin) — NOT the one-big-word 120px
    style = _style_line(out)
    assert ",78," in style
    assert style.endswith(f"2,80,80,{SUBTITLED_CAPTION_MARGIN_V},1")


def test_word_pop_synthesizes_for_an_edited_cue(tmp_path):
    """Edited cue: stored words no longer spell the text, so timings are synthesized
    across the cue window (E3 — never even-splits still-trusted real times)."""
    cue = {
        "text": "bugün size az",  # user changed "çok" → "az"
        "start_s": 0.0,
        "end_s": 1.5,
        "words": [
            {"text": "bugün", "start_s": 0.0, "end_s": 0.5},
            {"text": "size", "start_s": 0.5, "end_s": 0.9},
            {"text": "çok", "start_s": 0.9, "end_s": 1.5},
        ],
    }
    out = tmp_path / "pop_edit.ass"
    generate_word_pop_ass([cue], str(out), font_name="TikTok Sans")
    events = _events(out)
    assert len(events) == 3  # three edited tokens
    assert f"{{\\c{_ACTIVE_WORD_ASS_COLOR}&}}az" in events[2]  # new word is captioned


def test_word_pop_clamps_out_of_order_word_times(tmp_path):
    """Whisper word timestamps can regress at segment boundaries; every word-pop event
    draws the FULL line, so an overlap would stack two complete captions (the
    lyric-injector stacked-text incident class). Events must be monotonic."""
    cue = {
        "text": "bir iki üç",
        "start_s": 0.0,
        "end_s": 2.0,
        "words": [
            {"text": "bir", "start_s": 0.0, "end_s": 0.8},
            {"text": "iki", "start_s": 0.5, "end_s": 1.2},  # regressed start (< prev end)
            {"text": "üç", "start_s": 1.2, "end_s": 2.0},
        ],
    }
    out = tmp_path / "clamp.ass"
    generate_word_pop_ass([cue], str(out))
    events = _events(out)
    # Parse start/end times back out and assert non-overlap.
    import re as _re

    times = [_re.match(r"Dialogue: 0,([\d:.]+),([\d:.]+),", ev).groups() for ev in events]

    def _s(t: str) -> float:
        h, m, rest = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    for (s1, e1), (s2, _e2) in zip(times, times[1:]):
        assert _s(e1) <= _s(s2) + 1e-6, f"overlapping events: {e1} > {s2}"


def test_word_pop_empty_cue_writes_valid_empty_ass(tmp_path):
    out = tmp_path / "pop_empty.ass"
    generate_word_pop_ass([{"text": "   ", "start_s": 0.0, "end_s": 1.0}], str(out))
    assert "[Events]" in out.read_text(encoding="utf-8")
    assert _events(out) == []


def test_subtitled_margin_v_clears_platform_ui(tmp_path):
    """The subtitled style burns plain captions at the safe-zone MarginV so they
    clear the TikTok/Reels UI; the narrated default (180) must stay unchanged."""
    cues = [{"text": "merhaba arkadaslar", "start_s": 0.0, "end_s": 1.5}]
    out = tmp_path / "safe.ass"
    generate_ass_from_cues(
        cues, str(out), font_name="TikTok Sans", style="plain", margin_v=SUBTITLED_CAPTION_MARGIN_V
    )
    style_line = next(
        ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith("Style: Default")
    )
    # Format tail is ...,Alignment,MarginL,MarginR,MarginV,Encoding → 2,80,80,<mv>,1
    assert style_line.endswith(f"2,80,80,{SUBTITLED_CAPTION_MARGIN_V},1")
    assert SUBTITLED_CAPTION_MARGIN_V >= 360  # comfortably above the ~15-20% UI band

    default_out = tmp_path / "default.ass"
    generate_ass_from_cues(cues, str(default_out), font_name="TikTok Sans", style="plain")
    default_line = next(
        ln
        for ln in default_out.read_text(encoding="utf-8").splitlines()
        if ln.startswith("Style: Default")
    )
    assert default_line.endswith("2,80,80,180,1")  # narrated default untouched


def _transcript() -> Transcript:
    return Transcript(
        words=[
            Word(text="the", start_s=0.0, end_s=0.3, confidence=1.0),
            Word(text="energy", start_s=0.3, end_s=0.8, confidence=1.0),
            Word(text="here", start_s=0.8, end_s=1.2, confidence=1.0),
            Word(text="is", start_s=1.2, end_s=1.4, confidence=1.0),
            Word(text="different", start_s=1.4, end_s=2.0, confidence=1.0),
        ]
    )


def test_default_is_byte_identical_karaoke(tmp_path) -> None:
    """No kwargs → legacy Arial karaoke header + per-word \\k tags (orchestrate.py)."""
    out = tmp_path / "cap.ass"
    generate_ass(_transcript(), 0.0, 5.0, str(out))
    content = out.read_text(encoding="utf-8")
    assert content.startswith(_ASS_HEADER)
    assert "Arial" in content
    assert "{\\k" in content  # karaoke word-highlight preserved
    assert "different" in content


def test_plain_style_drops_karaoke_and_sets_font(tmp_path) -> None:
    out = tmp_path / "cap.ass"
    generate_ass(_transcript(), 0.0, 5.0, str(out), style="plain", font_name="TikTok Sans")
    content = out.read_text(encoding="utf-8")
    assert "TikTok Sans" in content
    assert "Arial" not in content
    assert "{\\k" not in content  # plain line captions, no per-word sweep
    # the spoken words are present as plain text
    assert "the energy here is different" in content


def test_plain_caption_window_offset(tmp_path) -> None:
    """start_s offset shifts cue times so the first cue starts near 0."""
    out = tmp_path / "cap.ass"
    # words live at 0..2s; ask for window starting at 0 → first cue ~0:00
    generate_ass(_transcript(), 0.0, 5.0, str(out), style="plain", font_name="TikTok Sans")
    content = out.read_text(encoding="utf-8")
    dialogue = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    assert dialogue, "expected at least one caption cue"
    assert dialogue[0].split(",")[1].startswith("0:00:00")


def test_empty_words_writes_header_only(tmp_path) -> None:
    out = tmp_path / "cap.ass"
    generate_ass(Transcript(words=[]), 0.0, 5.0, str(out), style="plain", font_name="TikTok Sans")
    content = out.read_text(encoding="utf-8")
    assert "TikTok Sans" in content
    assert "Dialogue:" not in content


# ── editable cues (on-video caption editor) ──────────────────────────────────


def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start_s=start, end_s=end, confidence=1.0)


def test_build_plain_cues_splits_on_sentences() -> None:
    """Each sentence is its own cue — two sentences are NEVER merged into one line."""
    words = [
        _w("the", 0.0, 0.3),
        _w("energy", 0.3, 0.8),
        _w("is", 0.8, 1.0),
        _w("different.", 1.0, 1.6),  # sentence 1 ends here
        _w("Could", 2.0, 2.4),
        _w("not", 2.4, 2.6),
        _w("be", 2.6, 2.8),
        _w("better!", 2.8, 3.4),  # sentence 2 ends here
    ]
    cues = build_plain_cues(words, offset_s=0.0)
    assert len(cues) == 2
    assert cues[0]["text"] == "the energy is different."
    assert cues[1]["text"] == "Could not be better!"


def test_build_plain_cues_preserves_word_timing_exactly() -> None:
    """Each cue spans its own words' [first.start, last.end] — no drift, no re-timing."""
    words = [
        _w("Hello", 1.10, 1.40),
        _w("there.", 1.40, 1.95),  # cue 0: [1.10, 1.95]
        _w("How", 3.00, 3.20),
        _w("are", 3.20, 3.35),
        _w("you?", 3.35, 3.90),  # cue 1: [3.00, 3.90]
    ]
    cues = build_plain_cues(words, offset_s=0.0)
    assert (cues[0]["start_s"], cues[0]["end_s"]) == (1.1, 1.95)
    assert (cues[1]["start_s"], cues[1]["end_s"]) == (3.0, 3.9)


def test_build_plain_cues_offset_shifts_uniformly() -> None:
    """An assembled-time offset shifts every cue identically (alignment unchanged)."""
    words = [_w("One.", 2.24, 2.8), _w("Two.", 3.0, 3.5)]
    cues = build_plain_cues(words, offset_s=2.24)
    assert cues[0]["start_s"] == 0.0
    assert abs(cues[1]["start_s"] - 0.76) < 1e-6  # 3.0 - 2.24


def test_build_plain_cues_long_sentence_subsplits_without_merging() -> None:
    """A >14-word sentence (no clause break) hard-splits into multiple cues, each
    from the SAME sentence — and the within-sentence gap is CLOSED (no flicker)."""
    words = [_w(f"w{i}", float(i), float(i) + 0.4) for i in range(20)]
    words[-1] = _w("end.", 19.0, 19.4)
    cues = build_plain_cues(words, offset_s=0.0)
    assert len(cues) == 2  # 14 + 6
    assert cues[0]["start_s"] == 0.0
    assert cues[1]["start_s"] == 14.0
    # block 1 holds until block 2 begins — no mid-sentence caption flicker
    assert cues[0]["end_s"] == cues[1]["start_s"] == 14.0
    assert cues[1]["text"].endswith("end.")


def test_build_plain_cues_splits_long_sentence_at_comma() -> None:
    """A long sentence breaks at the LAST clause boundary before the cap, not mid-phrase."""
    words = [_w(f"w{i}", float(i), float(i) + 0.4) for i in range(18)]
    words[9] = _w("nine,", 9.0, 9.4)  # comma in [soft_min=7, cap=14)
    words[-1] = _w("end.", 17.0, 17.4)
    cues = build_plain_cues(words, offset_s=0.0)
    assert len(cues) == 2
    assert cues[0]["text"].endswith("nine,")  # split AT the comma, not at word 14
    assert cues[1]["start_s"] == 10.0  # next block starts at w10
    assert cues[0]["end_s"] == cues[1]["start_s"]  # gap closed within the sentence


def test_build_plain_cues_inter_sentence_gap_preserved() -> None:
    """Between sentences the caption clears during the pause (ends on the last word)."""
    words = [_w("Hi.", 0.0, 0.5), _w("Bye.", 2.0, 2.5)]
    cues = build_plain_cues(words, offset_s=0.0)
    assert cues[0]["end_s"] == 0.5  # ends when spoken, NOT held to the next sentence (2.0)
    assert cues[1]["start_s"] == 2.0


def test_build_plain_cues_attaches_standalone_punctuation() -> None:
    """A bare '.' word attaches to the previous word — no stray space, no empty cue."""
    words = [
        _w("the", 0.0, 0.3),
        _w("energy", 0.3, 0.8),
        _w(".", 0.8, 0.85),  # standalone terminal punctuation
        _w("Wow", 1.2, 1.6),
        _w("!", 1.6, 1.65),
    ]
    cues = build_plain_cues(words, offset_s=0.0)
    assert cues[0]["text"] == "the energy."
    assert cues[1]["text"] == "Wow!"


def test_build_plain_cues_no_cue_merges_two_sentences() -> None:
    """Core invariant: a terminal . ! ? may only sit at the END of a cue — never
    mid-cue with another sentence glued on. AND times stay monotonic + non-overlapping."""
    import re

    words = [
        _w("Hello.", 0.0, 0.5),
        _w("This", 1.0, 1.2),
        _w("is", 1.2, 1.4),
        _w("two.", 1.4, 1.9),
        _w("And", 2.5, 2.7),
        _w("three!", 2.7, 3.2),
    ]
    cues = build_plain_cues(words)
    assert [c["text"] for c in cues] == ["Hello.", "This is two.", "And three!"]
    for c in cues:
        assert not re.search(r"[.!?]\s+\S", c["text"]), c["text"]
    for a, b in zip(cues, cues[1:]):
        assert a["end_s"] <= b["start_s"] + 1e-6  # monotonic, non-overlapping


def test_build_plain_cues_no_punctuation_degrades_to_chunks() -> None:
    """No terminal punctuation anywhere → safe fixed chunks of _MAX_CUE_WORDS (no giant cue)."""
    words = [_w(f"w{i}", float(i), float(i) + 0.5) for i in range(30)]
    cues = build_plain_cues(words, offset_s=0.0)
    assert len(cues) == 3  # 14 + 14 + 2
    assert all(len(c["text"].split()) <= 14 for c in cues)


def test_generate_ass_from_edited_cues(tmp_path) -> None:
    """Reburn path: explicit (hand-edited) cue text is what burns — incl. a word fix."""
    out = tmp_path / "cap.ass"
    cues = [
        {"text": "the energy here is", "start_s": 0.0, "end_s": 1.2},
        {"text": "could not be better", "start_s": 1.2, "end_s": 2.4},  # was 'cooled down'
    ]
    generate_ass_from_cues(cues, str(out), font_name="TikTok Sans")
    content = out.read_text(encoding="utf-8")
    assert "TikTok Sans" in content
    assert "could not be better" in content
    assert "cooled down" not in content
    # two cues → two Dialogue lines
    assert content.count("Dialogue:") == 2


def test_generate_ass_from_cues_skips_blank_text(tmp_path) -> None:
    out = tmp_path / "cap.ass"
    generate_ass_from_cues(
        [
            {"text": "  ", "start_s": 0.0, "end_s": 1.0},
            {"text": "kept", "start_s": 1.0, "end_s": 2.0},
        ],
        str(out),
        font_name="TikTok Sans",
    )
    content = out.read_text(encoding="utf-8")
    assert content.count("Dialogue:") == 1
    assert "kept" in content


# ── word-by-word cues (qbuilder caption style) ───────────────────────────────


def test_build_word_cues_one_cue_per_word() -> None:
    """Each spoken word is its own cue spanning its [start, end] — no merging."""
    words = [_w("the", 0.0, 0.3), _w("energy", 0.3, 0.9), _w("here.", 0.9, 1.5)]
    cues = build_word_cues(words)
    assert [c["text"] for c in cues] == ["the", "energy", "here."]
    assert (cues[1]["start_s"], cues[1]["end_s"]) == (0.3, 0.9)


def test_build_word_cues_attaches_standalone_punctuation() -> None:
    """A stray '.' word attaches to the prior word instead of flashing on its own."""
    words = [_w("wow", 0.0, 0.5), _w(".", 0.5, 0.55), _w("Next", 1.0, 1.5)]
    cues = build_word_cues(words)
    assert [c["text"] for c in cues] == ["wow.", "Next"]


def test_build_word_cues_floors_ultrashort_word_clamped_to_next() -> None:
    """An ultra-short word gets a min on-screen floor — but never past the next start."""
    # next word is close: floor would overrun, so clamp to the next word's start.
    near = build_word_cues([_w("I", 1.0, 1.03), _w("am", 1.10, 1.5)])
    assert near[0]["start_s"] == 1.0
    assert near[0]["end_s"] == 1.10
    # next word is far: the full 0.18s floor applies.
    far = build_word_cues([_w("I", 1.0, 1.03), _w("am", 2.0, 2.5)])
    assert abs(far[0]["end_s"] - 1.18) < 1e-6


def test_build_word_cues_offset_and_monotonic() -> None:
    """offset_s shifts every cue; times stay monotonic + non-overlapping."""
    cues = build_word_cues([_w("one", 2.24, 2.6), _w("two", 2.7, 3.1)], offset_s=2.24)
    assert cues[0]["start_s"] == 0.0
    for a, b in zip(cues, cues[1:]):
        assert a["end_s"] <= b["start_s"] + 1e-6


def test_build_word_cues_empty() -> None:
    assert build_word_cues([]) == []


def test_generate_ass_from_cues_word_style_uses_big_centered_header(tmp_path) -> None:
    """style="word" burns the big (120px) centered word header, not the 78px plain one."""
    out = tmp_path / "w.ass"
    generate_ass_from_cues(
        [{"text": "most", "start_s": 0.0, "end_s": 0.4}],
        str(out),
        font_name="TikTok Sans",
        style="word",
    )
    content = out.read_text(encoding="utf-8")
    assert "TikTok Sans" in content
    assert ",120," in content  # big word font (plain is 78)
    assert content.count("Dialogue:") == 1


def test_generate_ass_from_cues_default_style_is_plain(tmp_path) -> None:
    """Byte-identity guard: no style kwarg keeps the 78px plain header (unchanged)."""
    out = tmp_path / "p.ass"
    generate_ass_from_cues(
        [{"text": "hi", "start_s": 0.0, "end_s": 1.0}], str(out), font_name="TikTok Sans"
    )
    content = out.read_text(encoding="utf-8")
    assert ",78," in content
    assert ",120," not in content
