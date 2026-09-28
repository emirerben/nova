"""Tests for UTM capture on POST /api/waitlist."""

from app.routes.waitlist import _truncate_utm


def test_truncate_utm_normal():
    """Normal UTM value is returned as-is."""
    assert _truncate_utm("tiktok") == "tiktok"


def test_truncate_utm_none():
    """None → None."""
    assert _truncate_utm(None) is None


def test_truncate_utm_empty():
    """Empty string → None."""
    assert _truncate_utm("") is None


def test_truncate_utm_long_string():
    """String > 256 chars is truncated."""
    long = "x" * 300
    result = _truncate_utm(long)
    assert result is not None
    assert len(result) == 256
