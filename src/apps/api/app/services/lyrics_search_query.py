"""Shared title/artist hygiene for any lyric-lookup backend.

yt-dlp passes the raw YouTube title through to track metadata, so titles
routinely arrive as 'Artist Name - Song Title (Official Video) [HD]'. A
naive lookup using that as `track_name` and the stored artist as
`artist_name` would produce three copies of the artist and a parenthetical
noise tag — Genius's relevance ranking degraded sharply on these (PR #251),
and LRCLIB matches even more strictly because it does exact-string
indexing rather than full-text search.

This helper produces a `(clean_title, clean_artist)` pair suitable for
separate-parameter APIs (LRCLIB) or string concatenation by the caller
(if any future Genius-style backend returns).
"""

from __future__ import annotations

import re

# YouTube titles arrive as "Artist - Title (Official Video)" / "[Official Audio]"
# / "(HD)" / "[Lyrics]" etc. Strip these BEFORE the lookup query — they
# poison both Genius's full-text search and LRCLIB's exact-string indexing.
_YOUTUBE_NOISE_TAGS = re.compile(
    r"[\(\[\{][^\)\]\}]*?(?:official|lyric|audio|video|hd|hq|4k|mv|"
    r"music|edit|remix|live|version|explicit|clean|color\s*coded|"
    r"extended|radio|album|single|cover|karaoke|instrumental|"
    r"reupload|reaction|tribute|demo)[^\)\]\}]*?[\)\]\}]",
    flags=re.IGNORECASE,
)


def build_lyrics_search_query(title: str, artist: str) -> tuple[str, str]:
    """Return cleaned `(title, artist)` pair for lyric lookups.

    Two operations, in order:

    1. **Artist deduplication.** When the title field starts with the artist
       name plus a separator ('Artist - ', 'Artist -', case-insensitive),
       strip the prefix. This handles the very common yt-dlp output
       "The Weeknd - Can't Feel My Face" + artist="The Weeknd" case.

    2. **Noise tag stripping.** Drop parenthetical / bracketed segments
       that match the YouTube noise vocabulary ("Official Video", "HD",
       "Lyrics", "Remix", etc.). Preserves legitimate parens like
       "(feat. Ariana Grande)" — those don't match the vocabulary.

    Returns:
        (clean_title, clean_artist). Either field may be empty; callers
        should validate before issuing a lookup.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()

    # Strip "Artist - " prefix when it duplicates the artist field.
    if artist and title.lower().startswith(f"{artist.lower()} - "):
        title = title[len(artist) + 3 :].lstrip()
    # Strip "Artist -" with no following space too (rare but seen).
    elif artist and title.lower().startswith(f"{artist.lower()}-"):
        title = title[len(artist) + 1 :].lstrip()

    # Drop parenthetical / bracketed noise like "(Official Video)".
    title = _YOUTUBE_NOISE_TAGS.sub("", title)
    # Collapse repeated whitespace + leading/trailing hyphens left behind.
    title = re.sub(r"\s+", " ", title).strip(" -–—")

    return title, artist
