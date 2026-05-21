from pydantic import ValidationError

from app.schemas.lyrics_config_override import LyricsConfigOverride


def test_valid_partial_payload_parses() -> None:
    parsed = LyricsConfigOverride(post_dwell_s=1.25, fade_in_ms=100)
    assert parsed.post_dwell_s == 1.25
    assert parsed.fade_in_ms == 100


def test_unknown_field_rejected() -> None:
    try:
        LyricsConfigOverride(not_real=True)
    except ValidationError as exc:
        assert "extra_forbidden" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_out_of_range_post_dwell_rejected() -> None:
    try:
        LyricsConfigOverride(post_dwell_s=10.0)
    except ValidationError as exc:
        assert "less than or equal to 5" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_font_family_passes_through() -> None:
    parsed = LyricsConfigOverride(font_family="Inter Tight")
    assert parsed.font_family == "Inter Tight"
