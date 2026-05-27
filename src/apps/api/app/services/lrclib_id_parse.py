"""Strict LRCLIB ID / URL parser for the admin manual-override input.

The admin Lyrics tab lets an operator paste either a raw numeric LRCLIB row
ID (`12345`) or an lrclib.net URL copied from their browser. This module is
the SINGLE source of truth for extracting the numeric ID, and it deliberately
refuses anything that could be a security footgun.

Why this is its own module (not a regex inline in the route):
  * A naive `re.search(r"(\\d+)", input)` would match digits in arbitrary
    URLs (`https://evil.com/12345` → 12345), which is exactly the kind of
    quiet trust-store pollution that the second-pass review of this PR
    flagged. Routing the input through a hostname allowlist before
    extracting digits forces every caller to go through the gate.
  * Keeping it independently testable means the parser's security-critical
    cases (substring spoofs like `lrclib.net.evil.com`) get unit tests
    pinned at this layer, independent of the endpoint that consumes them.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_LRCLIB_HOST_ALLOWLIST: frozenset[str] = frozenset({"lrclib.net", "www.lrclib.net"})

# /lyrics/12345 or /api/get/12345, with optional trailing slash. No query
# string, no fragment — the LRCLIB row URLs don't use them and accepting
# them would invite future ambiguity.
_LRCLIB_PATH_RE = re.compile(r"^/(?:lyrics|api/get)/(\d+)/?$")

# A "naked ID" is a string of digits with no URL-ish characters anywhere.
# This blocks `12345 (some other text)` and `12345.5` from sneaking through
# under the digit-only branch.
_NAKED_DIGITS_RE = re.compile(r"^\d+$")


class LrclibIdParseError(ValueError):
    """Raised when the admin-supplied input isn't a valid LRCLIB ID or URL.

    Inherits ValueError so FastAPI's Pydantic validation surfaces a 422
    with the same shape as built-in type errors.
    """


def parse_lrclib_id(raw: str) -> int:
    """Return the positive integer LRCLIB row ID from a raw admin input.

    Accepts:
      * naked numeric string, e.g. `"12345"`
      * https://lrclib.net/lyrics/12345
      * https://lrclib.net/api/get/12345
      * https://www.lrclib.net/lyrics/12345  (www. subdomain)
      * http://lrclib.net/lyrics/12345        (http accepted; the parser
        is for ID extraction, not for an outbound fetch, so transport
        scheme is irrelevant)

    Rejects (raises LrclibIdParseError with a human-readable message):
      * non-allowlisted hosts (https://evil.com/12345)
      * substring host spoofs (https://lrclib.net.evil.com/12345)
      * non-id paths (https://lrclib.net/about, https://lrclib.net/search?q=foo)
      * URL with extra query / fragment beyond the row path
      * empty / whitespace-only input
      * zero or negative numeric ID
      * naked input containing whitespace, periods, slashes, or colons
        ("12345 (Beauty And A Beat)" — would be a digit-extraction trap)
    """
    if not isinstance(raw, str):  # defensive — Pydantic should have done this already
        raise LrclibIdParseError("LRCLIB ID input must be a string")

    cleaned = raw.strip()
    if not cleaned:
        raise LrclibIdParseError("LRCLIB ID input is empty")

    # Naked digits path. Reject anything that has URL-ish characters or
    # whitespace mixed in — those go through the URL parser.
    if _NAKED_DIGITS_RE.match(cleaned):
        value = int(cleaned)
        if value <= 0:
            raise LrclibIdParseError("LRCLIB ID must be a positive integer")
        return value

    # If the input has any of `:`, `/`, or `.`, treat it as a URL and run
    # through the strict parser. Anything else (e.g. `"12345 (Beauty)"`,
    # `"hello"`, `"12abc"`) is invalid by construction.
    if not any(ch in cleaned for ch in ":/"):
        raise LrclibIdParseError(
            "LRCLIB ID must be a positive integer or a https://lrclib.net/... URL"
        )

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise LrclibIdParseError(f"could not parse URL: {exc}") from exc

    # `hostname` is the host with no port and lowercased. `urlsplit` returns
    # None for schemes it doesn't recognize; guard against both.
    host = (parsed.hostname or "").lower()
    if host not in _LRCLIB_HOST_ALLOWLIST:
        raise LrclibIdParseError(
            f"URL host {host!r} is not an LRCLIB host (allowed: {sorted(_LRCLIB_HOST_ALLOWLIST)})"
        )

    if parsed.query or parsed.fragment:
        raise LrclibIdParseError("LRCLIB URL must not include a query string or fragment")

    match = _LRCLIB_PATH_RE.match(parsed.path)
    if not match:
        raise LrclibIdParseError(
            f"URL path {parsed.path!r} is not an LRCLIB row path "
            "(expected /lyrics/<id> or /api/get/<id>)"
        )

    value = int(match.group(1))
    if value <= 0:
        raise LrclibIdParseError("LRCLIB ID must be a positive integer")
    return value
