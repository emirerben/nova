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


def test_legacy_is_unchanged_and_grouping_keeps_all_147_identities():
    rows = [cue(index, "word", index * 0.3, index * 0.3 + 0.28) for index in range(147)]
    before = deepcopy(rows)
    assert project_guided_caption_overlays(rows, None) is rows
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert len(result) == 147
    assert {row["element_id"] for row in result} == {str(index) for index in range(147)}
    assert result[0]["text"] == "word word word word word word"
    assert result[0]["end_s"] == 0.3
    assert rows == before


def test_punctuation_pause_and_length_end_groups():
    rows = [
        cue(1, "Hello.", 0, 0.2),
        cue(2, "Next", 0.2, 0.4),
        cue(3, "pause", 1, 1.2),
        cue(4, "ThisIsAVeryLongWordThatExceedsTheLineBudget", 1.2, 1.4),
    ]
    result = project_guided_caption_overlays(rows, {"style": "sentence"})
    assert [row["text"] for row in result] == [row["text"] for row in rows]


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
