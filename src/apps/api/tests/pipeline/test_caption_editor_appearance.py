from app.pipeline.captions import generate_ass_from_cues


def render(tmp_path, **appearance):
    output = tmp_path / "captions.ass"
    generate_ass_from_cues(
        [{"text": "Hello world", "start_s": 1, "end_s": 3}],
        str(output),
        appearance={"highlight_spoken_word": False, **appearance},
    )
    return output.read_text()


def test_word_display_is_independent_of_highlight(tmp_path):
    plain = render(tmp_path, display_style="word")
    lines = [line for line in plain.splitlines() if line.startswith("Dialogue:")]
    assert len(lines) == 2
    assert lines[0].endswith("Hello") and lines[1].endswith("world")
    assert "0:00:01.00" in lines[0] and "0:00:03.00" in lines[1]
    highlighted = render(tmp_path, display_style="word", highlight_spoken_word=True)
    assert "\\c" in highlighted
    sentence = render(tmp_path, display_style="sentence")
    assert sentence.count("Dialogue:") == 1 and "Hello world" in sentence


def test_outline_shadow_alignment_are_independently_written(tmp_path):
    result = render(
        tmp_path,
        stroke_color="#123456",
        shadow_color="#654321",
        shadow_opacity=0.25,
        alignment="right",
    )
    fields = next(line for line in result.splitlines() if line.startswith("Style: Default,")).split(
        ","
    )
    assert fields[5] == "&H00563412"
    assert fields[6] == "&HBF214365"
    assert fields[18] == "3"
