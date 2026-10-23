"""Which genre did the creator ask the song to be, and do a song's labels say it (KRI-848).

The music matcher ranks by mood; it never checks a genre, and ``MusicLabels.genre`` is a
7-value enum that cannot hold "tango" (it is coerced to ``other``). Without this check any
track could be presented as the tango song the creator asked for. Pure text and label
reading, no model call: the genre words come from the creator's own ask, and a track
fits only when its labels say so: the enum value, or the genre as a whole word in
``vibe_tags`` / ``mood`` / ``ideal_content_profile``. Title and artist never count.

A named genre becomes a hard filter on the candidates, so reading the ask wrongly in the
"asked" direction would force a genre the creator did not want ("no rap" must never mean
"only rap"). Every doubt therefore leans to "not asked", which is exactly today's
behaviour (no filter, no notice): a genre is asked only when it is named plainly, an
everyday word ("house", "rock") only beside a music word, and never after a cue that rules
it out ("no", "without", "sin", "anything but", Turkish "olmasın" / "istemiyorum" after it).

Music stays recommend-only: nothing here changes the audio of a video, only what the
reply says about the song Kria suggests.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, NamedTuple, get_args

from app.agents._schemas.music_labels import _GENRE_ALIASES, Genre, MusicLabels
from app.kria.reply_language import say
from app.text_fold import loose_words

# The two fields a frozen song section carries when the creator asked for a genre.
ASKED_GENRE = "asked_genre"
GENRE_FIT = "genre_fit"


class AskedGenre(NamedTuple):
    name: str  # as the creator reads it: "tango", "hip hop", "EDM"
    genre: str | None  # the ``MusicLabels.genre`` value it stands for, if the enum has one
    spellings: tuple[str, ...]  # folded word runs a label may spell it with

    @property
    def everyday(self) -> bool:
        """Also an everyday word ("house", "rock"), so it needs more than the word itself."""
        return any(spelling in _EVERYDAY for spelling in self.spellings)


# Genres the enum cannot hold. The first spelling is how the creator is told about it.
_OUTSIDE_THE_ENUM: tuple[tuple[str, ...], ...] = (
    ("tango",),
    ("salsa",),
    ("bachata",),
    ("merengue",),
    ("cumbia",),
    ("reggaeton",),
    ("flamenco",),
    ("samba",),
    ("bossa nova", "bossanova"),
    ("jazz", "caz"),
    ("blues",),
    ("rock",),
    ("metal",),
    ("punk",),
    ("indie",),
    ("funk",),
    ("soul",),
    ("disco",),
    ("reggae",),
    ("ska",),
    ("lo-fi", "lofi"),
    ("R&B", "rnb"),
    ("afrobeat", "afrobeats"),
    ("k-pop", "kpop"),
    ("latin",),
    ("gospel",),
    ("opera",),
    ("swing",),
    ("arabesk",),
)
# Said as the enum value or one of its aliases, shown the way an acronym is written.
_DISPLAY = {"edm": "EDM"}
# Genre words that are everyday words too: "the house tour", "the pop-in sound", "a power
# drill". The music ask is every live audio ask of the edit joined together, so these count
# only in a part of it that is about music, and only in a label that describes the song.
_EVERYDAY = frozenset(
    """
    pop house trap drill dance score country soul metal rock indie swing latin ambient
    classical folk cinematic comedy funk punk acoustic electronic opera salsa
    """.split()
)
# Folded words that make a part of the ask about music (Turkish forms by prefix).
_MUSIC_WORDS = frozenset(
    """
    song songs music track tracks tune tunes soundtrack playlist parca parcasi cancion
    canciones musica tema
    """.split()
)
_MUSIC_PREFIXES = ("sarki", "muzi")
# A genre the creator rules out ("no rap", "anything but reggaeton", "caz istemiyorum") is
# not one they asked for. Every miss leans to "not asked" (no filter, no notice), never the
# reverse, because a named genre becomes a hard filter. English and Spanish: a cue rules out
# every genre after it in the same ";" part, across commas and "or" / "ni", until a word
# that turns back ("but", "pero"). Turkish puts the cue just after the genre (2 words).
# Folded, apostrophes dropped ("don't" = "dont"); a bare "but" turns back, never rules out.
_RULES_OUT = frozenset("no not without except avoid never dont sin".split())
_RULES_OUT_PHRASES = frozenset({"anything but", "everything but", "instead of", "rather than"})
_TURNS_BACK = frozenset("but ama fakat pero sino".split())
_RULED_OUT_AFTER = frozenset("olmasin istemiyorum istemem degil haric disinda yerine".split())
# An alias of the enum that names a kind of audio, not a genre ("a soundtrack").
_NOT_A_GENRE = frozenset({"soundtrack"})


def _spelling(text: str) -> str:
    return " ".join(loose_words(text))


def _build_lexicon() -> dict[str, AskedGenre]:
    lexicon: dict[str, AskedGenre] = {}
    for group in _OUTSIDE_THE_ENUM:
        spellings = tuple(dict.fromkeys(_spelling(word) for word in group))
        for spelling in spellings:
            lexicon[spelling] = AskedGenre(group[0], None, spellings)
    enum_words = {genre: genre for genre in get_args(Genre) if genre != "other"}
    for word, genre in {**enum_words, **_GENRE_ALIASES}.items():
        if word in _NOT_A_GENRE:
            continue
        spelling = _spelling(word)
        lexicon[spelling] = AskedGenre(_DISPLAY.get(spelling, spelling), genre, (spelling,))
    return lexicon


_LEXICON = _build_lexicon()
_LONGEST_NAME = max(len(spelling.split()) for spelling in _LEXICON)


def _words(text: str) -> list[str]:
    return loose_words(re.sub(r"['’]", "", text))


def _about_music(words: Sequence[str]) -> bool:
    return any(word in _MUSIC_WORDS or word.startswith(_MUSIC_PREFIXES) for word in words)


def _mentions(words: Sequence[str]) -> Iterator[tuple[AskedGenre, bool]]:
    """Each genre named in ``words`` (longest name first), and whether it was ruled out."""
    ruling_out = False
    i = 0
    while i < len(words):
        if " ".join(words[i : i + 2]) in _RULES_OUT_PHRASES:
            ruling_out, i = True, i + 2
        elif words[i] in _RULES_OUT:
            ruling_out, i = True, i + 1
        elif words[i] in _TURNS_BACK:
            ruling_out, i = False, i + 1
        else:
            for size in range(min(_LONGEST_NAME, len(words) - i), 0, -1):
                genre = _LEXICON.get(" ".join(words[i : i + size]))
                if genre is not None:
                    after = words[i + size : i + size + 2]
                    yield genre, ruling_out or any(word in _RULED_OUT_AFTER for word in after)
                    i += size
                    break
            else:
                i += 1


def asked_genres(ask: str) -> list[AskedGenre]:
    """The genres the creator's words name, in order of mention, each once.

    ``ask`` is the requirement texts joined by ";", so each part is read on its own: an
    everyday genre word counts only in a part that also names music, and a genre the
    creator rules out ("no rap, reggaeton or rock") is not asked for.
    """
    found: dict[str, AskedGenre] = {}
    for part in ask.split(";"):
        words = _words(part)
        about_music = _about_music(words)
        for genre, ruled_out in _mentions(words):
            if not ruled_out and (about_music or not genre.everyday):
                found.setdefault(genre.name, genre)
    return list(found.values())


def _track_labels(track: Any) -> MusicLabels | None:
    """The track's labels, read the way the matcher's candidate loader reads them."""
    raw = getattr(track, "ai_labels", None) or {}
    try:
        return MusicLabels(**(raw.get("labels") or raw))
    except (AttributeError, TypeError, ValueError):
        return None


def track_genre_fits(track: Any, genres: Sequence[AskedGenre]) -> list[AskedGenre]:
    """The asked genres this track's labels say; none when the labels cannot be read.

    ``other`` is the enum's catch-all, so it never stands for a genre.
    """
    labels = _track_labels(track)
    if labels is None:
        return []
    song = f" {_spelling(' '.join([*labels.vibe_tags, labels.mood]))} "
    # The profile describes the clips the song suits ("rock climbing b-roll"), not the song.
    clips = f"{song}{_spelling(labels.ideal_content_profile)} "
    return [
        genre
        for genre in genres
        if (genre.genre is not None and labels.genre == genre.genre)
        or any(
            f" {spelling} " in (song if genre.everyday else clips) for spelling in genre.spellings
        )
    ]


def genre_record(genres: Sequence[AskedGenre], track: Any | None) -> dict[str, Any]:
    """What a frozen song section records about the genre; empty when none was asked."""
    if not genres:
        return {}
    fits = track_genre_fits(track, genres) if track is not None else []
    return {ASKED_GENRE: (fits or genres)[0].name, GENRE_FIT: bool(fits)}


def music_genre_notice(section: Mapping[str, Any] | None) -> str | None:
    """What the reply must say about the suggested song when a genre was asked for.

    Names the song, calls it the asked genre only when its labels say so, and otherwise
    says there is none and offers the closest. A section frozen before the genre was
    checked (no fit field) says nothing.
    """
    if not isinstance(section, Mapping) or not isinstance(section.get(GENRE_FIT), bool):
        return None
    genre = str(section.get(ASKED_GENRE) or "").strip()
    if not genre:
        return None
    title = str(section.get("title") or "").strip()
    artist = str(section.get("artist") or "").strip()
    if section.get("status") != "selected" or not title:
        return say(
            en=f"I couldn't find any {genre} tracks to suggest.",
            tr=f"Önerebileceğim bir {genre} şarkısı bulamadım.",
        )
    song_en = f"“{title}” by {artist}" if artist else f"“{title}”"
    song_tr = f"“{title}” ({artist})" if artist else f"“{title}”"
    if section[GENRE_FIT]:
        return say(
            en=f"For the {genre} song you asked for, I suggest {song_en}. Add it when you post.",
            tr=f"{song_tr}, istediğin {genre} şarkısı için önerim. Paylaşırken ekleyebilirsin.",
        )
    return say(
        en=(
            f"I don't have any {genre} tracks. The closest I have is {song_en}. "
            "Add it when you post if you like it."
        ),
        tr=(
            f"Elimde {genre} şarkısı yok. En yakını {song_tr}. "
            "Beğenirsen paylaşırken ekleyebilirsin."
        ),
    )
