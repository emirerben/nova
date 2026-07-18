from app.pipeline.canvas import LANDSCAPE, PORTRAIT
from app.pipeline.reframe import _build_video_filter, _encoding_args


def test_encoding_args_default_matches_explicit_portrait() -> None:
    assert _encoding_args("/tmp/out.mp4") == _encoding_args("/tmp/out.mp4", canvas=PORTRAIT)


def test_encoding_args_landscape_sets_output_size() -> None:
    args = _encoding_args("/tmp/out.mp4", canvas=LANDSCAPE)

    assert args[args.index("-s") + 1] == "1920x1080"


def test_build_video_filter_default_matches_explicit_portrait() -> None:
    assert _build_video_filter("16:9", None) == _build_video_filter(
        "16:9",
        None,
        canvas=PORTRAIT,
    )


def test_build_video_filter_landscape_threads_canvas_dimensions() -> None:
    filters = _build_video_filter("16:9", None, canvas=LANDSCAPE)

    assert "scale=-2:1080" in filters
    assert "crop=1920:1080" in filters


def test_build_video_filter_landscape_letterbox_threads_canvas_dimensions() -> None:
    filters = _build_video_filter("9:16", None, output_fit="letterbox_black", canvas=LANDSCAPE)

    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in filters
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black" in filters
