from app.pipeline.canvas import LANDSCAPE, PORTRAIT, canvas_for_orientation


def test_canvas_for_orientation_defaults_to_portrait() -> None:
    assert canvas_for_orientation(None) == PORTRAIT
    assert canvas_for_orientation("portrait") == PORTRAIT
    assert canvas_for_orientation("bad-value") == PORTRAIT


def test_canvas_for_orientation_landscape() -> None:
    assert canvas_for_orientation("landscape") == LANDSCAPE
