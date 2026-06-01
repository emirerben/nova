"""Unit tests for the strict LRCLIB ID/URL parser.

Security-critical: this module gates whether an admin-supplied string is
treated as a trusted LRCLIB row pointer or rejected. A leaky parser would
let an admin paste a hostile URL whose digit fragment happens to collide
with a valid LRCLIB row ID, silently retargeting the next extraction at
whatever recording that ID points to.
"""

from __future__ import annotations

import pytest

from app.services.lrclib_id_parse import LrclibIdParseError, parse_lrclib_id

# ── Accepts ────────────────────────────────────────────────────────────────────


def test_accepts_naked_numeric_id() -> None:
    assert parse_lrclib_id("12345") == 12345


def test_accepts_long_numeric_id() -> None:
    # LRCLIB IDs aren't bounded by any documented limit; accept large.
    assert parse_lrclib_id("9876543210") == 9876543210


def test_accepts_https_lyrics_url() -> None:
    assert parse_lrclib_id("https://lrclib.net/lyrics/12345") == 12345


def test_accepts_https_api_get_url() -> None:
    assert parse_lrclib_id("https://lrclib.net/api/get/12345") == 12345


def test_accepts_www_subdomain() -> None:
    assert parse_lrclib_id("https://www.lrclib.net/lyrics/12345") == 12345


def test_accepts_http_scheme() -> None:
    # The parser only extracts the ID; the transport scheme isn't its
    # business. Reject only on host or path mismatch.
    assert parse_lrclib_id("http://lrclib.net/lyrics/12345") == 12345


def test_accepts_trailing_slash() -> None:
    assert parse_lrclib_id("https://lrclib.net/lyrics/12345/") == 12345


def test_strips_surrounding_whitespace() -> None:
    assert parse_lrclib_id("  12345  ") == 12345
    assert parse_lrclib_id("\nhttps://lrclib.net/lyrics/12345\n") == 12345


# ── Rejects: invalid input shape ──────────────────────────────────────────────


def test_rejects_empty_string() -> None:
    with pytest.raises(LrclibIdParseError, match="empty"):
        parse_lrclib_id("")


def test_rejects_whitespace_only() -> None:
    with pytest.raises(LrclibIdParseError, match="empty"):
        parse_lrclib_id("   ")


def test_rejects_zero() -> None:
    with pytest.raises(LrclibIdParseError, match="positive integer"):
        parse_lrclib_id("0")


def test_rejects_negative_via_url_path_match() -> None:
    # The path regex requires \d+, so "-5" never enters the URL path branch
    # — it'd be parsed as a naked-input path with a "-", which is invalid.
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("-5")


def test_rejects_naked_with_trailing_text() -> None:
    """`12345 (Beauty And A Beat)` would match a naive `\\d+` regex but is
    clearly not a clean ID. Forces the URL branch, which then fails on
    the missing host."""
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("12345 (Beauty And A Beat)")


def test_rejects_alphanumeric() -> None:
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("12abc")


def test_rejects_word_only() -> None:
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("hello")


def test_rejects_decimal_number() -> None:
    # `12345.5` has a `.`, so it goes to URL parsing → fails on no scheme.
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("12345.5")


# ── Rejects: host allowlist (SECURITY-CRITICAL) ───────────────────────────────


def test_rejects_non_lrclib_host() -> None:
    """The headline failure mode: admin pastes a URL from somewhere else."""
    with pytest.raises(LrclibIdParseError, match="not an LRCLIB host"):
        parse_lrclib_id("https://evil.com/lyrics/12345")


def test_rejects_substring_host_spoof() -> None:
    """`lrclib.net.evil.com` looks similar but is owned by evil.com.
    `urlsplit().hostname` returns `lrclib.net.evil.com` (the actual host),
    which must NOT match the allowlist."""
    with pytest.raises(LrclibIdParseError, match="not an LRCLIB host"):
        parse_lrclib_id("https://lrclib.net.evil.com/lyrics/12345")


def test_rejects_userinfo_spoof() -> None:
    """`https://lrclib.net@evil.com/lyrics/12345` — the @ delimits userinfo.
    The actual host is `evil.com`. Must be rejected."""
    with pytest.raises(LrclibIdParseError, match="not an LRCLIB host"):
        parse_lrclib_id("https://lrclib.net@evil.com/lyrics/12345")


def test_rejects_uppercase_lrclib_net_subpath() -> None:
    """A subdomain we don't allowlist must still be rejected, regardless of
    case. (urlsplit lowercases the hostname for us, but exercise the case.)"""
    with pytest.raises(LrclibIdParseError, match="not an LRCLIB host"):
        parse_lrclib_id("https://api.lrclib.net/lyrics/12345")


# ── Rejects: path shape ───────────────────────────────────────────────────────


def test_rejects_non_id_path() -> None:
    with pytest.raises(LrclibIdParseError, match="row path"):
        parse_lrclib_id("https://lrclib.net/about")


def test_rejects_search_path() -> None:
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("https://lrclib.net/search?q=Beauty")


def test_rejects_query_string() -> None:
    """Even if the path is /lyrics/<id>, refuse if there's a query string —
    LRCLIB row pages don't use them, and accepting them invites future
    ambiguity (e.g. tracking params we'd then misread)."""
    with pytest.raises(LrclibIdParseError, match="query string or fragment"):
        parse_lrclib_id("https://lrclib.net/lyrics/12345?ref=cheat")


def test_rejects_fragment() -> None:
    with pytest.raises(LrclibIdParseError, match="query string or fragment"):
        parse_lrclib_id("https://lrclib.net/lyrics/12345#section")


def test_rejects_url_without_scheme() -> None:
    """`lrclib.net/lyrics/12345` (no scheme) parses with hostname=None.
    Must be rejected, otherwise admin could accidentally fetch a path-
    looking string that doesn't even resolve."""
    with pytest.raises(LrclibIdParseError):
        parse_lrclib_id("lrclib.net/lyrics/12345")
