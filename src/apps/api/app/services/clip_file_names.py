"""Clips the creator names by the file name the app shows ("first IMG_7382 then ...") (KRI-614).

The iOS app lists every upload by its file name (``IMG_7382.MOV``), so creators order and
pick clips by it. The attach route keeps that name on the clip assignment
(``source_filename``); this module turns it into the name the creator saw, finds those
names in creator text, and settles clip intents that name clips this way before the
resolver runs (the resolver's vision records never carry file names).

Matching is deliberately narrow, because a match pins a clip with no model check:

* only camera/phone-style names that mix letters and digits (``IMG_7382``, ``B964DD9A-...``)
  match. A word ("beach.mov") or a bare number ("2026") is left to the resolver;
* the full name matches, or a leading part of it at a ``_``/``-``/``.`` boundary that is
  long enough to identify a clip ("B964DD9A" for ``B964DD9A-A978-...``, never "IMG");
* case and the Turkish dotless/dotted I are folded ("ımg_7382" == "IMG_7382");
* a name two clips share matches neither.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.services.media_filenames import safe_media_basename

# iOS stages a pick as "<36-char upload UUID>-<name Photos gives>" and shows the name without
# that prefix (`BackgroundUploadCoordinator.displayFilename`).
_UPLOAD_PREFIX = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}-(?=.)")
_MEDIA_SUFFIXES = frozenset(
    "mov mp4 m4v hevc avi mkv webm 3gp jpg jpeg png heic heif webp gif tif tiff dng".split()
)
DISPLAY_NAME_MAX_CHARS = 80
_SEGMENT_SPLIT = re.compile(r"[\s_.\-]+")
# KRI-763: a generic last word some exports append ("M1_08_footage"); creators type the name
# without it ("M1_08"). The shorter spelling is one more way to name the same clip.
_GENERIC_NAME_SUFFIXES = frozenset({"footage"})
# A leading part of a name identifies a clip only with this many folded characters.
_MIN_PREFIX_CHARS = 6
# Creator text splits into words on whitespace and punctuation, but not on the "_", "-" and
# "." inside a file name ("IMG_7382.MOV").
_TEXT_TOKEN = re.compile(r"[^\s,;:!?()\[\]{}<>\"'`“”‘’«»/\\|]+")
# "img 7382" is one name typed as two words; three covers "PXL 20261011 183000".
_MAX_WORDS_PER_NAME = 3
# Words that may surround file names in an intent's attribute without describing anything
# else ("the IMG_7382 clip", "ımg_7382 then ımg_7820"). Folded (accent/case free).
_FILLER_WORDS = frozenset(
    (
        "a an the this that it one and then after next finally followed by first last start "
        "end beginning ending opening closing at with in to of put use show play order clip "
        "clips video videos file files footage shot shots ve sonra ardindan ilk son sonda "
        "sonunda en basta once klip klibi klipler videosu dosya dosyasi ile bu s "
        # KRI-763: the inflected Turkish nouns ("M1_05 klibini", "M1_05 videosunu").
        "klibini klibine klibin klipi klipleri kliplerini videoyu videosunu dosyayi dosyasini "
        "klibinden klipten"
    ).split()
)


def clip_display_name(filename: object) -> str | None:
    """The name the app shows for an upload, without its extension ("IMG_7382").

    Sanitized like every other filename that reaches a prompt (``safe_media_basename``).
    """
    name = safe_media_basename(filename)
    if name is None:
        return None
    name = _UPLOAD_PREFIX.sub("", name, count=1)
    stem, dot, suffix = name.rpartition(".")
    if dot and stem.strip() and suffix.casefold() in _MEDIA_SUFFIXES:
        name = stem
    name = name.strip()[:DISPLAY_NAME_MAX_CHARS].strip()
    return name or None


def _fold(text: str) -> str:
    # KRI-520: Turkish dotless ı lower-cases I; İ decomposes to I plus a combining dot.
    decomposed = unicodedata.normalize("NFKD", text.replace("ı", "i")).casefold()
    return "".join(c for c in decomposed if c.isalnum() and not unicodedata.combining(c))


def _identifying(form: str) -> bool:
    return len(form) >= 4 and any(c.isdigit() for c in form) and any(c.isalpha() for c in form)


def _strip_media_suffix(word: str) -> str:
    stem, dot, suffix = word.rpartition(".")
    return stem if dot and stem and suffix.casefold() in _MEDIA_SUFFIXES else word


@dataclass(frozen=True)
class NameReference:
    """A file name found in creator text: the words it spans, and the ONE clip it names.

    ``media_id`` is None when the name fits more than one clip (never guessed).
    """

    start: int
    end: int
    surface: str
    media_id: str | None


class ClipNameIndex:
    """Which clip each identifying name (or leading part of one) points at."""

    def __init__(self, names: Mapping[str, str | None]) -> None:
        self._full: dict[str, set[str]] = {}
        self._prefix: dict[str, set[str]] = {}
        for media_id, name in names.items():
            if not name:
                continue
            full = _fold(name)
            if _identifying(full):
                self._full.setdefault(full, set()).add(media_id)
            segments = [s for s in _SEGMENT_SPLIT.split(name) if s]
            if len(segments) > 1 and _fold(segments[-1]) in _GENERIC_NAME_SUFFIXES:
                # "M1_08_footage" is also named "M1_08". Two clips that share the short
                # spelling match neither (`_lookup`), so this never picks one silently.
                short = _fold("".join(segments[:-1]))
                if _identifying(short):
                    self._full.setdefault(short, set()).add(media_id)
            for count in range(1, len(segments)):
                prefix = _fold("".join(segments[:count]))
                if len(prefix) >= _MIN_PREFIX_CHARS and _identifying(prefix) and prefix != full:
                    self._prefix.setdefault(prefix, set()).add(media_id)

    def __bool__(self) -> bool:
        return bool(self._full or self._prefix)

    def _lookup(self, form: str) -> tuple[bool, str | None]:
        """(is a clip name, the one clip it names). A full name beats a leading part."""
        for table in (self._full, self._prefix):
            ids = table.get(form)
            if ids:
                return True, next(iter(ids)) if len(ids) == 1 else None
        return False, None

    def references(self, text: str) -> tuple[list[NameReference], list[str]]:
        """The file names in ``text`` in the order written, plus every other word (folded)."""
        # A typed name may carry the iOS upload prefix ("52250013-...-M1_08_footage.mp4").
        words = [
            _UPLOAD_PREFIX.sub("", m.group(0), count=1).strip("._-*")
            for m in _TEXT_TOKEN.finditer(text or "")
        ]
        words = [w for w in words if w]
        folded = [_fold(_strip_media_suffix(w)) for w in words]
        refs: list[NameReference] = []
        others: list[str] = []
        index = 0
        while index < len(words):
            for size in range(min(_MAX_WORDS_PER_NAME, len(words) - index), 0, -1):
                form = "".join(folded[index : index + size])
                found, media_id = self._lookup(form) if form else (False, None)
                if found:
                    surface = " ".join(words[index : index + size])
                    refs.append(NameReference(index, index + size, surface, media_id))
                    index += size
                    break
            else:
                if folded[index]:
                    others.append(folded[index])
                index += 1
        return refs, others


def names_only(others: Iterable[str]) -> bool:
    """True when the words besides the file names describe nothing ("then", "the clip")."""
    return all(word in _FILLER_WORDS for word in others)


__all__ = [
    "DISPLAY_NAME_MAX_CHARS",
    "ClipNameIndex",
    "NameReference",
    "clip_display_name",
    "names_only",
]
