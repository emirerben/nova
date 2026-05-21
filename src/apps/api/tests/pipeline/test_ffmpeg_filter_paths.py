from app.pipeline._ffmpeg_filter_paths import escape_ffmpeg_filter_path


def test_plain_path_unchanged() -> None:
    assert escape_ffmpeg_filter_path("/tmp/file.ass") == "/tmp/file.ass"


def test_colon_escaped() -> None:
    assert escape_ffmpeg_filter_path("/tmp/a:b.ass") == "/tmp/a\\:b.ass"


def test_quote_escaped() -> None:
    assert escape_ffmpeg_filter_path("/tmp/a'b.ass") == "/tmp/a\\'b.ass"


def test_backslash_escaped() -> None:
    assert escape_ffmpeg_filter_path(r"C:\tmp\a.ass") == "C\\:\\\\tmp\\\\a.ass"


def test_multiple_specials_escaped() -> None:
    assert escape_ffmpeg_filter_path(r"C:\tmp\a'b,c.ass") == "C\\:\\\\tmp\\\\a\\'b\\,c.ass"


def test_spaces_unchanged() -> None:
    assert escape_ffmpeg_filter_path("/tmp/a b.ass") == "/tmp/a b.ass"
