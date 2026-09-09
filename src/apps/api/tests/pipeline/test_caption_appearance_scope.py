"""KRI-13: explicit cue appearance survives both caption burn modes."""

import pytest
from pydantic import ValidationError

from app.pipeline.captions import generate_ass_from_cues, generate_word_pop_ass
from app.routes.generative_jobs import CaptionCue


@pytest.mark.parametrize("renderer", [generate_ass_from_cues, generate_word_pop_ass])
def test_scoped_appearance_overrides_survive_burn_without_changing_siblings(tmp_path, renderer):
    output = tmp_path / "captions.ass"
    renderer(
        [
            {
                "text": "target words",
                "start_s": 0,
                "end_s": 1,
                "stroke_width": 0,
                "shadow_enabled": False,
            },
            {"text": "other words", "start_s": 1, "end_s": 2},
        ],
        str(output),
        appearance={"stroke_width": 4, "shadow_enabled": True},
    )
    lines = [line for line in output.read_text().splitlines() if line.startswith("Dialogue:")]
    target = [line for line in lines if "target" in line]
    other = [line for line in lines if "other" in line]
    assert target and other
    assert all(r"\bord0" in line and r"\shad0" in line for line in target)
    assert all(r"\bord" not in line and r"\shad" not in line for line in other)
    if renderer is generate_word_pop_ass:
        assert all(r"{\r}{\bord0}{\shad0}" in line for line in target)


@pytest.mark.parametrize("renderer", [generate_ass_from_cues, generate_word_pop_ass])
def test_absent_and_null_appearance_keep_legacy_output(tmp_path, renderer):
    base = {"text": "unchanged words", "start_s": 0, "end_s": 1}
    before, after = tmp_path / "before.ass", tmp_path / "after.ass"
    renderer([base], str(before))
    renderer([{**base, "stroke_width": None, "shadow_enabled": None}], str(after))
    assert before.read_bytes() == after.read_bytes()


def test_caption_cue_round_trip_preserves_zero_false():
    cue = CaptionCue(text="target", start_s=0, end_s=1, stroke_width=0, shadow_enabled=False)
    value = cue.model_dump(exclude_none=True)
    assert value["stroke_width"] == 0
    assert value["shadow_enabled"] is False
    assert "stroke_width" not in CaptionCue(text="other", start_s=1, end_s=2).model_dump(
        exclude_none=True
    )


@pytest.mark.parametrize(
    "patch", [{"stroke_width": -1}, {"stroke_width": 13}, {"shadow_enabled": "false"}]
)
def test_caption_cue_rejects_invalid_appearance(patch):
    with pytest.raises(ValidationError):
        CaptionCue(text="target", start_s=0, end_s=1, **patch)
