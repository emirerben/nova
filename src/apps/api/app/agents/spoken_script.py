"""Which creator-quoted lines are voiceover script rather than on-screen copy.

Shared by the semantic planner (`semantic_edit_proposal`) and the legacy
planner (`edit_proposal`). A narrated edit already draws every spoken word as
timed voiceover captions, so a line the creator marks as voiceover ("VO: ...")
or that the recorded narration speaks must never also become a chapter
thought. An explicit display instruction ('"..." on screen', 'Put "..."')
still keeps a quote as creator copy.

Depends only on `app.schemas.edit_proposal`, so both planners can import it
without an import cycle.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from app.schemas.edit_proposal import creator_copy_match_key

QUOTED_TEXT_RE = re.compile(
    r"[\"\u201c\u201d]([^\"\u201c\u201d]{1,120})[\"\u201c\u201d]|(?<!\w)'([^']{1,120})'(?!\w)"
)
_SPEECH_ATTRIBUTION_RE = re.compile(
    r"\b(?:said|says|wrote|writes|replied|replies|remarked|remarks|stated|states|asked|asks"
    r"|told\s+(?:me|us|them|him|her))\s*[:,]?\s*$",
    re.IGNORECASE,
)
_DISPLAY_COPY_CUE_RE = re.compile(
    r"\b(?:captions?|subtitles?|overlays?|labels?|titles?|show|display)\b"
    r"|\bon[\s-]?screen\b",
    re.IGNORECASE,
)
_DISPLAY_QUOTE_TARGET = (
    r"(?:on[\s-]?screen\b"
    r"|(?:as|in|for)\s+(?:(?:a|an|the)\s+)?(?:caption|subtitle|overlay|label|title)\b)"
)
_QUOTE_MEDIA_TARGET = (
    r"on\s+(?:(?:a|an|the)\s+)?(?:(?:opening|first|last|closing)\s+)?"
    r"(?:clip|shot|image|frame|video)\b"
)
_QUOTE_PLACEMENT_RE = re.compile(
    r"\b(?:write|print|render|put|add|use)\s+"
    r"(?:(?:the|these|those|his|her|their|a|an|this|that)\s+)?(?:words?|quotes?|text|lines?)\b",
    re.IGNORECASE,
)
_DISPLAY_QUOTE_SUFFIX_RE = re.compile(
    rf"^\s*(?:[,;:\u2014-]\s*)?(?:{_DISPLAY_QUOTE_TARGET}"
    r"|(?:put|show|display|use)\s+(?:that|this|those|these)\s+"
    rf"(?:quotes?|text|words?|lines?|captions?)\s+(?:{_DISPLAY_QUOTE_TARGET}|{_QUOTE_MEDIA_TARGET})"
    rf"|(?:put|show|display|use)\s+(?:that|this|it)\s+{_DISPLAY_QUOTE_TARGET})",
    re.IGNORECASE,
)
# The voiceover-script exclusion must never swallow explicit on-screen copy.
# A short quote is far likelier a label than a spoken sentence, and a
# placement verb before the quote or a where-to-show phrase after it keeps
# it as creator copy even when the voiceover also says those words.
_SPOKEN_SCRIPT_MIN_WORDS = 4
_SPOKEN_QUOTE_PLACEMENT_RE = re.compile(
    r"\b(?:put|add|write|place|overlay|type|print|text)\b", re.IGNORECASE
)
_SPOKEN_QUOTE_WHERE_RE = re.compile(
    r"\s*(?:[,;:—-]\s*)?(?:as\s+(?:on[\s-]?screen\s+)?text\b"
    r"|(?:on|over|across)\s+(?:(?:the|this|that|each|every|a|an)\s+)?(?:[\w-]+\s+){0,3}"
    r"(?:clips?|shots?|photos?|images?|pictures?|videos?|frames?|chapters?|screen)\b)",
    re.IGNORECASE,
)
# A creator who explicitly tags a quote as voiceover/script ("VO:", "I say
# ...") means it regardless of what the ASR transcript happened to catch;
# that cue alone excludes the quote, even with an empty transcript.
_SPOKEN_SCRIPT_CUE_RE = re.compile(
    r"(?:"
    r"\bVO\b"
    r"|\bV\.O\."
    r"|\bvoice[\s-]?over\b"
    r"|\bnarration\b"
    r"|\bnarrator\b"
    r"|\bscript\b"
    r"|\bspoken\b"
    r"|\bI\s+say\b"
    r"|\bI'?m\s+saying\b"
    r"|\bI\s+am\s+saying\b"
    r"|\bI\s+will\s+say\b"
    r"|\bI'?ll\s+say\b"
    r"|\bwhat\s+I\s+say\b"
    r"|\bmy\s+voiceover\s+says\b"
    r"|\bthe\s+voiceover\s+says\b"
    r"|\bnarration\s+says\b"
    r"|\b(?:voiceover|VO|spoken)\s+line\b"
    r")\s*[:\-–—]?\s*$",
    re.IGNORECASE,
)
# A long quote (>= 6 words) that fails an exact transcript match is retried
# with a bounded number of tolerated word errors — ASR occasionally mis-hears
# a word or two of an otherwise-correct voiceover line; a short quote never
# gets this leniency since one differing word out of a handful is more likely
# a genuine on-screen label than a transcription slip.
_SPOKEN_SCRIPT_FUZZY_MIN_WORDS = 6
_QUOTE_LIST_JOIN_RE = re.compile(r"\s*,?\s*(?:(?:and|or|&)\s*)?", re.IGNORECASE)


def is_reported_speech_quote(request: str, match: re.Match[str]) -> bool:
    """Do not turn attributed story dialogue into a required text overlay.

    Keep the quote fallback for unclassified copy, including non-English
    captions. Only an adjacent speech attribution establishes this exclusion;
    an explicit display instruction attached to that quote overrides it.
    """
    prefix = _quote_clause_prefix(request, match)
    if not _SPEECH_ATTRIBUTION_RE.search(prefix):
        return False
    return not _quote_has_display_instruction(request, match, prefix)


def _quote_clause_prefix(request: str, match: re.Match[str]) -> str:
    before_quote = request[: match.start()].rstrip().removesuffix(",")
    return re.split(r"[.!?\n,;]|\b(?:then|and|but)\b", before_quote, flags=re.I)[-1]


def _quote_has_display_instruction(request: str, match: re.Match[str], prefix: str) -> bool:
    # A period can sit inside the quoted speech. Do not borrow an instruction
    # from the next sentence (e.g. 'He said "... ." Put "real copy" on screen').
    # Only a postfix that directly assigns this quote to a display lane counts.
    return bool(
        _DISPLAY_COPY_CUE_RE.search(prefix)
        or _DISPLAY_QUOTE_SUFFIX_RE.search(request[match.end() :])
        or (
            _QUOTE_PLACEMENT_RE.search(prefix)
            and re.match(rf"\s*{_QUOTE_MEDIA_TARGET}", request[match.end() :], re.I)
        )
    )


def _quote_list_ends(request: str, match: re.Match[str]) -> tuple[re.Match[str], re.Match[str]]:
    """First and last quote of a list such as 'Label them "A", "B" and "C"'.

    A display cue before the list or a placement after it covers every item,
    not only the adjacent one.
    """
    quotes = list(QUOTED_TEXT_RE.finditer(request))
    at = next(index for index, quote in enumerate(quotes) if quote.start() == match.start())
    first = last = at
    while first > 0 and _QUOTE_LIST_JOIN_RE.fullmatch(
        request[quotes[first - 1].end() : quotes[first].start()]
    ):
        first -= 1
    while last + 1 < len(quotes) and _QUOTE_LIST_JOIN_RE.fullmatch(
        request[quotes[last].end() : quotes[last + 1].start()]
    ):
        last += 1
    return quotes[first], quotes[last]


def transcript_speaks(words: list[str], spoken: list[str]) -> bool:
    """True when the transcript word-key stream `spoken` speaks quote `words`.

    An exact contiguous match is tried first. A quote long enough (>= 6 words)
    to survive a couple of wrong words is then retried allowing up to
    ``len(words) // 4`` substitution/insertion/deletion errors, found via a
    sliding `difflib.SequenceMatcher` over transcript windows near the quote's
    length — ASR mis-hears an occasional word (a homophone, a merged phrase)
    in an otherwise-correct voiceover line, and that must not turn a spoken
    line into a fabricated on-screen caption.
    """
    n = len(words)
    if n < _SPOKEN_SCRIPT_MIN_WORDS:
        return False
    if any(spoken[start : start + n] == words for start in range(len(spoken) - n + 1)):
        return True
    if n < _SPOKEN_SCRIPT_FUZZY_MIN_WORDS:
        return False
    k = n // 4
    min_len = max(1, n - k)
    max_len = min(len(spoken), n + k)
    for length in range(min_len, max_len + 1):
        for start in range(0, len(spoken) - length + 1):
            window = spoken[start : start + length]
            matcher = difflib.SequenceMatcher(None, words, window, autojunk=False)
            matched = sum(block.size for block in matcher.get_matching_blocks())
            if matched >= n - k:
                return True
    return False


def is_spoken_script_quote(request: str, match: re.Match[str], spoken: list[str]) -> bool:
    """A quoted sentence the recorded voiceover speaks is its script, not a text overlay.

    Two rules exclude a quote. First, an explicit voiceover/script cue right
    before it ("VO: ...", "I say ...") means it regardless of the transcript,
    including an empty one. Otherwise the quote must appear in the transcript,
    tolerating a bounded number of ASR word errors on long quotes (see
    `transcript_speaks`) since a speech model occasionally mis-hears a word
    or two of a correctly spoken line. Either way, a short phrase, a placement
    verb ('Put "..."'), a where-to-show phrase ('"..." over the photo') or any
    other display instruction keeps the quote as creator copy.
    """
    text = match.group(1) or match.group(2) or ""
    words = [key for key in (creator_copy_match_key(word) for word in text.split()) if key]
    head, tail = _quote_list_ends(request, match)
    prefix = _quote_clause_prefix(request, head)
    # The short-quote gate guards the transcript rule only: an explicit cue
    # says the creator meant a short line as speech, not as a label.
    if not _SPOKEN_SCRIPT_CUE_RE.search(prefix) and not transcript_speaks(words, spoken):
        return False
    if _SPOKEN_QUOTE_PLACEMENT_RE.search(prefix) or _SPOKEN_QUOTE_WHERE_RE.match(
        request[tail.end() :]
    ):
        return False
    return not _quote_has_display_instruction(request, tail, prefix)


def quote_text(match: re.Match[str]) -> str:
    return match.group(1) or match.group(2) or ""


def word_keys(text: str) -> list[str]:
    """Per-word match keys, the unit the transcript comparison works in."""
    return [key for key in (creator_copy_match_key(word) for word in text.split()) if key]


def narration_word_keys(narration_words: Iterable[dict]) -> list[str]:
    """Match-key stream of the recorded voiceover's ASR words."""
    words = (str(word.get("text") or "") for word in narration_words)
    return [key for key in map(creator_copy_match_key, words) if key]


def _contains_words(quote: list[str], words: list[str]) -> bool:
    """`words` run contiguously inside `quote`; the last may be cut mid-word.

    Intent copy is truncated to 60 characters (`ClipIntent.creator_text`), so
    a long voiceover line can arrive ending in a partial word.
    """
    n = len(words)
    return any(
        quote[start : start + n - 1] == words[:-1] and quote[start + n - 1].startswith(words[-1])
        for start in range(len(quote) - n + 1)
    )


def is_spoken_script_copy(text: str, request: str, spoken: list[str]) -> bool:
    """Resolved caption/group intent copy that the voiceover already says.

    The chat resolver can turn a creator's "VO: ..." line into a caption or
    group intent. When a creator quote with the same words (or, for copy of
    at least `_SPOKEN_SCRIPT_MIN_WORDS` words, a longer quote containing it)
    is voiceover script per `is_spoken_script_quote`, the copy is script, not
    a caption. A same-words quote that is on-screen copy wins. Copy the
    creator never quoted counts as script only when the transcript speaks it
    (same bounded ASR tolerance as a quote).
    """
    words = word_keys(text)
    if not words:
        return False
    key = creator_copy_match_key(text)
    quoted = False
    script = False
    for match in QUOTED_TEXT_RE.finditer(request):
        quote = quote_text(match)
        same = creator_copy_match_key(quote) == key
        if not same and not (
            len(words) >= _SPOKEN_SCRIPT_MIN_WORDS and _contains_words(word_keys(quote), words)
        ):
            continue
        quoted = True
        if is_spoken_script_quote(request, match, spoken):
            script = True
        elif same:
            return False
    return script if quoted else transcript_speaks(words, spoken)
