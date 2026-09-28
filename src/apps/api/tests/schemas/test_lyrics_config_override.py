from pydantic import ValidationError

from app.schemas.lyrics_config_override import LyricsConfigOverride


def test_out_of_range_post_dwell_rejected() -> None:
    try:
        LyricsConfigOverride(post_dwell_s=10.0)
    except ValidationError as exc:
        assert "less than or equal to 5" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_out_of_range_sync_offset_rejected() -> None:
    try:
        LyricsConfigOverride(sync_offset_s=6.0)
    except ValidationError as exc:
        assert "less than or equal to 5" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
