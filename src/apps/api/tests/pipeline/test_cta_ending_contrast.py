"""KRI-1306: CTA ending over a light page -- outlined text, captions stop at the ending."""

from app.pipeline.phone_cta_ending import _row
from app.tasks.generative_build import _cues_clear_of_cta


def _ending_row(part: str, color: str) -> dict:
    return _row(
        element_id=f"cta-{part}",
        text="comment" if part == "lead" else "REPO",
        part=part,
        font="Inter",
        color=color,
        size_px=70,
        start_s=39.5,
        end_s=42.0,
        x_frac=0.5,
        y_frac=0.65,
    )


def test_ending_text_has_dark_outline_for_light_backgrounds():
    for part, color in (("lead", "#FFFFFF"), ("keyword", "#FCF3C3")):
        row = _ending_row(part, color)
        assert row["stroke_width"] >= 4
        assert row["stroke_color"] == "#000000"


def test_caption_over_ending_stops_when_ending_could_only_touch_captions():
    # Q2 run 1: the running caption "all five" was still on screen as the ending began.
    cues = [
        {"start_s": 36.0, "end_s": 38.0, "words": [{"text": "so", "start_s": 36.0}]},
        {
            "start_s": 38.5,
            "end_s": 41.0,
            "words": [
                {"text": "all", "start_s": 38.5},
                {"text": "five", "start_s": 40.0},
            ],
        },
        {"start_s": 40.5, "end_s": 41.5, "words": [{"text": "x", "start_s": 40.5}]},
    ]
    rows = [_ending_row("lead", "#FFFFFF")]
    out = _cues_clear_of_cta(cues, rows, {"text_overlap": True})
    assert all(c["end_s"] <= 39.5 for c in out)
    assert [c["start_s"] for c in out] == [36.0, 38.5]
    assert [w["text"] for w in out[1]["words"]] == ["all"]


def test_captions_untouched_when_ending_is_clear_of_them():
    cues = [{"start_s": 38.5, "end_s": 41.0, "words": []}]
    rows = [_ending_row("lead", "#FFFFFF")]
    assert _cues_clear_of_cta(cues, rows, {"text_overlap": False}) == cues
