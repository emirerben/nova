from copy import deepcopy

from app.pipeline.guided_caption_presentation import project_guided_caption_overlays


def cue(index, text, start, end):
    return {
        "element_id": str(index),
        "role": "generative_narration_caption",
        "text": text,
        "start_s": start,
        "end_s": end,
        "effect": "static",
        "font_family": "Inter",
    }


def test_legacy_is_unchanged_and_sentence_projection_keeps_all_147_identities():
    rows = [cue(index, "word", index * 0.3, index * 0.3 + 0.28) for index in range(147)]
    before = deepcopy(rows)
    assert project_guided_caption_overlays(rows, None) is rows
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert len(result) == 147
    assert {row["element_id"] for row in result} == {str(index) for index in range(147)}
    assert result[0]["text"] == " ".join(["word"] * 147)
    assert result[0]["end_s"] == 0.3
    assert rows == before


def test_sentence_projection_uses_full_sentence_and_terminal_punctuation():
    rows = [
        cue(1, "This", 0, 0.2),
        cue(2, "is", 0.2, 0.4),
        cue(3, "a", 0.4, 0.6),
        cue(4, "long", 0.6, 0.8),
        cue(5, "sentence.", 0.8, 1),
        cue(6, "Next", 1.5, 1.7),
        cue(7, "one!", 1.7, 1.9),
    ]
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert [row["text"] for row in result] == [
        "This is a long sentence.",
        "This is a long sentence.",
        "This is a long sentence.",
        "This is a long sentence.",
        "This is a long sentence.",
        "Next one!",
        "Next one!",
    ]
    assert [(row["start_s"], row["end_s"]) for row in result] == [
        (0, 0.2),
        (0.2, 0.4),
        (0.4, 0.6),
        (0.6, 0.8),
        (0.8, 1),
        (1.5, 1.7),
        (1.7, 1.9),
    ]


def test_sentence_projection_does_not_split_decimals_and_accepts_closing_quotes():
    rows = [
        cue(1, "It", 0, 0.2),
        cue(2, "costs", 0.2, 0.4),
        cue(3, "172.5", 0.4, 0.6),
        cue(4, "dollars.”", 0.6, 0.8),
        cue(5, "Done", 0.8, 1),
    ]
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert [row["text"] for row in result] == [
        "It costs 172.5 dollars.”",
        "It costs 172.5 dollars.”",
        "It costs 172.5 dollars.”",
        "It costs 172.5 dollars.”",
        "Done",
    ]


def test_highlight_only_current_spoken_word_and_preserve_receipt_identity():
    rows = [cue(1, "we go", 0, 0.4), cue(2, "home", 0.4, 0.6)]
    meta = {"style": "sentence", "appearance": {"highlight_spoken_word": True}}
    result = project_guided_caption_overlays(rows, meta)
    assert [row["element_id"] for row in result] == ["1", "1", "2"]
    assert all(row["text"] == "we go home" for row in result)
    for index, row in enumerate(result):
        assert [w["start_s"] == 0 for w in row["word_timings"]] == [i == index for i in range(3)]
    assert [(row["start_s"], row["end_s"]) for row in result] == [(0, 0.2), (0.2, 0.4), (0.4, 0.6)]


def test_word_style_and_authored_text_are_independent():
    title = {"role": "generative_intro", "text": "Title"}
    rows = [title, cue(1, "we go", 0, 0.4)]
    result = project_guided_caption_overlays(
        rows,
        {
            "style": "word",
            "appearance": {"highlight_spoken_word": True},
            "highlight_color": "#123456",
        },
    )
    assert result[0] == title
    assert [row["text"] for row in result[1:]] == ["we", "go"]
    assert all(row["text_color"] == "#123456" for row in result[1:])


def lyric(index, text, start, end, *, line_end=False):
    return {**cue(index, text, start, end), **({"caption_line_end": True} if line_end else {})}


def test_a_lyric_line_ends_without_punctuation_and_the_marker_never_leaves():
    rows = [
        lyric(1, "hold", 0, 0.2),
        lyric(2, "me", 0.2, 0.4),
        lyric(3, "close", 0.4, 0.6, line_end=True),
        lyric(4, "dance", 0.8, 1),
        lyric(5, "tonight", 1, 1.2, line_end=True),
    ]
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert [row["text"] for row in result] == ["hold me close"] * 3 + ["dance tonight"] * 2
    assert not any("caption_line_end" in row for row in result)
    plain = project_guided_caption_overlays(
        [lyric(1, "a", 0, 0.2, line_end=True)], {"style": "word"}
    )
    assert not any("caption_line_end" in row for row in plain)
    bare = project_guided_caption_overlays([lyric(1, "a", 0, 0.2, line_end=True)], None)
    assert not any("caption_line_end" in row for row in bare)


def test_a_highlighted_line_restarts_after_a_lyric_line_end():
    rows = [
        lyric(1, "hold", 0, 0.2),
        lyric(2, "me", 0.2, 0.4, line_end=True),
        lyric(3, "dance", 0.4, 0.6),
    ]
    result = project_guided_caption_overlays(rows, {"appearance": {"highlight_spoken_word": True}})
    # Without the marker these three words (no gap, no punctuation) would share one line.
    assert [row["text"] for row in result] == ["hold me", "hold me", "dance"]


def test_word_display_animates_every_word_and_sentence_display_animates_the_line(monkeypatch):
    """Display Word shows one word at a time, so each word enters, exits and loops on its own."""
    from app.services import phone_rollout

    monkeypatch.setattr(phone_rollout, "caption_animation_renderable", lambda: True)
    pop_fade = {"entrance": "pop", "exit": "fade", "loop": "float", "speed": 1.0}
    rows = [cue(1, "we go", 0, 0.4), cue(2, "home.", 0.4, 0.6)]

    def edges(meta):
        result = project_guided_caption_overlays(deepcopy(rows), meta)
        return [
            (row["text"], row["animation_phases"]["entrance"], row["animation_phases"]["exit"])
            for row in result
        ]

    for highlight in (False, True):
        word = edges(
            {"style": "word", "animation_phases": pop_fade,
             "appearance": {"highlight_spoken_word": highlight}}
        )
        assert word == [("we", "pop", "fade"), ("go", "pop", "fade"), ("home.", "pop", "fade")]
    line = edges({"style": "sentence", "animation_phases": pop_fade})
    assert line == [("we go home.", "pop", "none"), ("we go home.", "none", "fade")]
    unset = {"style": "word", "appearance": {"highlight_spoken_word": False}}
    assert project_guided_caption_overlays(deepcopy(rows), unset) == project_guided_caption_overlays(
        deepcopy(rows), {**unset, "animation_phases": {"entrance": "none"}}
    )
