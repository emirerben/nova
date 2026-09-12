"""Reject renderer numeric overflow without limiting off-canvas authoring."""

import pytest
from pydantic import ValidationError

from app.agents._schemas.text_element import TextElement


def test_font_size_outside_renderer_numeric_domain_is_rejected_before_rendering():
    with pytest.raises(ValidationError, match="cannot be represented by the text renderer"):
        TextElement(text="A", start_s=0, end_s=1, size_px=1e300, wrap_lines=False)


@pytest.mark.parametrize("size", [1200, 4000])
@pytest.mark.parametrize("wrap_lines", [False, True])
def test_off_canvas_authored_font_size_is_preserved(size, wrap_lines):
    element = TextElement(text="A", start_s=0, end_s=1, size_px=size, wrap_lines=wrap_lines)
    assert element.size_px == size
    assert element.model_dump()["size_px"] == size
