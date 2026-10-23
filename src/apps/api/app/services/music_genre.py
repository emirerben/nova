"""Which genre did the creator ask the song to be, and do a song's labels say it (KRI-848).

The music matcher ranks by mood; it never checks a genre, and ``MusicLabels.genre`` is a
7-value enum that cannot hold "tango" (it is coerced to ``other``). Without this check any
track could be presented as the tango song the creator asked for. Pure text and label
reading, no model call: the genre words come from the creator's own ask, and a track
fits only when its labels say so. Title and artist never count.

Reading the ask. A named genre becomes a hard filter on the candidates, so reading the ask
wrongly in the "asked" direction would force a genre the creator did not want ("no rap"
must never mean "only rap"). Every doubt therefore leans to "not asked", which is exactly
today's behaviour (no filter, no notice):

- An everyday word ("house", "rock", "pop") is a genre only when a music word touches it:
  right after ("a house track", "rock song") or right before ("música rock"). A word in
  between makes it another noun ("house tour music", "rock climbing music"), and so does
  being elsewhere in the ask ("music for the house tour").
- A cue rules out the genres after it ("no", "without", "sin", "anything but", "other
  than", "hate"...) in its own clause, and in the following clauses only while they are
  bare lists of genres ("No rap, reggaeton or rock"). Any other word ends it, and so does
  "but" / "pero" / "ama". "No vocals" / "not too slow" rule out nothing. Turkish puts the
  cue after the genre ("rap olmasın", "caz istemiyorum").

Fitting a track. The labels say a genre when ``MusicLabels.genre`` IS that genre (the
enum's own word, or a true synonym such as "rap" or "EDM"), or when the genre appears as a
whole word in the song's ``vibe_tags`` / ``mood`` / classifier ``rationale`` (and, for a
word that is not also an everyday one, in ``ideal_content_profile``). A narrow alias
("house", "folk", "orchestral") is only a kind of an enum family, so the family alone is
not a fit: it is the closest the library has, offered as the closest.

Known limits, all on the "not asked" side: "pop love song", "a rock feel" and "Without
rock, tango" are not read as asks.

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

# What a frozen song section carries when the creator asked for a genre: the genre that fits
# (or the first asked), every genre asked, and whether the suggested track fits any of them.
ASKED_GENRE = "asked_genre"
ASKED_GENRES = "asked_genres"
GENRE_FIT = "genre_fit"


class AskedGenre(NamedTuple):
    name: str  # as the creator reads it: "tango", "hip hop", "EDM"
    genre: str | None  # the ``MusicLabels.genre`` value that IS this genre, if the enum has it
    family: str | None  # the enum value this genre is a kind of ("house" -> "electronic")
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
    ("K-pop", "kpop"),
    ("Latin",),
    ("gospel",),
    ("opera",),
    ("swing",),
    ("arabesk",),
)
# Aliases of an enum value that are the same genre, not a narrower kind of it.
_SYNONYMS = frozenset({"hiphop", "rap", "edm"})
# Said as the enum value or one of its aliases, shown the way an acronym is written.
_DISPLAY = {"edm": "EDM"}
# An alias of the enum that names a kind of audio, not a genre ("a soundtrack").
_NOT_A_GENRE = frozenset({"soundtrack"})
# Genre words that are everyday words too: "the house tour", "the pop-up shop", "a power
# drill". They count only next to a music word, and only in a label that describes the song.
_EVERYDAY = frozenset(
    """
    pop house trap drill dance score country soul metal rock indie swing latin ambient
    classical folk cinematic comedy funk punk acoustic electronic opera salsa
    """.split()
)
# Folded words that make a genre word a music ask ("rock song"; Turkish forms by prefix).
_MUSIC_WORDS = frozenset(
    """
    song songs music track tracks tune tunes soundtrack playlist parca parcasi cancion
    canciones musica tema
    """.split()
)
_MUSIC_PREFIXES = ("sarki", "muzi")
# Ruling a genre out. Folded, apostrophes dropped ("don't" = "dont"). A bare "but" turns back
# and never rules out; "no vocals" / "not too slow" rule out nothing the song could be.
_RULES_OUT = frozenset("no not without except avoid never dont sin hate dislike skip".split())
_RULES_OUT_PHRASES = (
    ("anything", "but"),
    ("everything", "but"),
    ("instead", "of"),
    ("rather", "than"),
    ("other", "than"),
    ("doesnt", "have", "to", "be"),
    ("neednt", "be"),
)
_TURNS_BACK = frozenset("but ama fakat pero sino".split())
_NOT_A_GENRE_AFTER_CUE = frozenset(
    "vocals vocal lyrics words singing voice voices too very".split()
)
_LIST_WORDS = frozenset("or and nor ni y o veya ve ya da please por favor lutfen either".split())
# Turkish says it after the genre, within two words.
_RULED_OUT_AFTER = frozenset(
    "olmasin olmadan istemiyorum istemem degil haric disinda yerine".split()
)


def _spelling(text: str) -> str:
    return " ".join(loose_words(text))


def _build_lexicon() -> dict[str, AskedGenre]:
    lexicon: dict[str, AskedGenre] = {}
    for group in _OUTSIDE_THE_ENUM:
        spellings = tuple(dict.fromkeys(_spelling(word) for word in group))
        for spelling in spellings:
            lexicon[spelling] = AskedGenre(group[0], None, None, spellings)
    enum_words = {genre: genre for genre in get_args(Genre) if genre != "other"}
    for word, genre in {**enum_words, **_GENRE_ALIASES}.items():
        if word in _NOT_A_GENRE:
            continue
        spelling = _spelling(word)
        is_it = word == genre or word in _SYNONYMS
        name = _DISPLAY.get(spelling, spelling)
        lexicon[spelling] = AskedGenre(name, genre if is_it else None, genre, (spelling,))
    return lexicon


_LEXICON = _build_lexicon()
_LONGEST_NAME = max(len(spelling.split()) for spelling in _LEXICON)


def _words(text: str) -> list[str]:
    return loose_words(re.sub(r"['’]", "", text))


def _is_music_word(word: str) -> bool:
    return word in _MUSIC_WORDS or word.startswith(_MUSIC_PREFIXES)


def _genre_at(words: Sequence[str], i: int) -> tuple[AskedGenre, int] | None:
    """The genre (longest name first) that starts at ``words[i]`` and how many words it takes."""
    for size in range(min(_LONGEST_NAME, len(words) - i), 0, -1):
        genre = _LEXICON.get(" ".join(words[i : i + size]))
        if genre is not None:
            return genre, size
    return None


def _cue_length(words: Sequence[str], i: int) -> int:
    """How many words of a ruling-out cue start at ``words[i]`` (0 = none)."""
    for phrase in _RULES_OUT_PHRASES:
        if tuple(words[i : i + len(phrase)]) == phrase:
            return len(phrase)
    return 1 if words[i] in _RULES_OUT else 0


def _is_genre_list(words: Sequence[str]) -> bool:
    """Is this clause only genres, connectors and fillers ("reggaeton or rock please")?"""
    i = 0
    while i < len(words):
        found = _genre_at(words, i)
        if found is not None:
            i += found[1]
        elif words[i] in _LIST_WORDS:
            i += 1
        else:
            return False
    return True


def _mentions(clauses: Sequence[Sequence[str]]) -> Iterator[tuple[AskedGenre, bool, bool]]:
    """Each genre named in the clauses of one part of the ask: (genre, ruled out, said as music).

    A cue rules out the genres after it in its clause, and in the next clauses while they
    are bare genre lists. "Said as music" = a music word is the word right before it or
    right after it, in the same clause.
    """
    ruling_out = False
    for words in clauses:
        if ruling_out and not _is_genre_list(words):
            ruling_out = False
        i = 0
        while i < len(words):
            cue = _cue_length(words, i)
            if cue:
                i += cue
                if i < len(words) and words[i] in _NOT_A_GENRE_AFTER_CUE:
                    i += 1
                else:
                    ruling_out = True
            elif words[i] in _TURNS_BACK:
                ruling_out = False
                i += 1
            elif (found := _genre_at(words, i)) is not None:
                genre, size = found
                end = i + size
                ruled_out = ruling_out or any(w in _RULED_OUT_AFTER for w in words[end : end + 2])
                as_music = (i > 0 and _is_music_word(words[i - 1])) or (
                    end < len(words) and _is_music_word(words[end])
                )
                yield genre, ruled_out, as_music
                i = end
            else:
                i += 1


def asked_genres(ask: str) -> list[AskedGenre]:
    """The genres the creator's words name, in order of mention, each once.

    ``ask`` is the requirement texts joined by ";", so each part is read on its own, clause
    by clause. A genre the creator rules out ("no rap, reggaeton or rock") is not asked for,
    and an everyday word ("house") only when said as music ("a house track").
    """
    found: dict[str, AskedGenre] = {}
    for part in ask.split(";"):
        clauses = [_words(clause) for clause in re.split(r"[,.!?:]", part)]
        for genre, ruled_out, as_music in _mentions(clauses):
            if not ruled_out and (as_music or not genre.everyday):
                found.setdefault(genre.name, genre)
    return list(found.values())


class _TrackText(NamedTuple):
    genre: str  # ``MusicLabels.genre``
    song: str  # folded words describing the song, padded with spaces for whole-word search
    clips: str  # ``song`` plus the clips the song suits (``ideal_content_profile``)


def _read_track(track: Any) -> _TrackText | None:
    """The track's labels, read the way the matcher's candidate loader reads them.

    ``ai_labels`` is ``{"labels": ..., "rationale": ...}``: the classifier is told to keep
    genre words out of ``vibe_tags``, so its one-sentence rationale ("Driving rock anthem")
    describes the song too. ``None`` when the labels cannot be read.
    """
    raw = getattr(track, "ai_labels", None) or {}
    try:
        labels = MusicLabels(**(raw.get("labels") or raw))
    except (AttributeError, TypeError, ValueError):
        return None
    rationale = raw.get("rationale")
    song = _spelling(
        " ".join([*labels.vibe_tags, labels.mood, rationale if isinstance(rationale, str) else ""])
    )
    # The profile describes the clips a song suits ("rock climbing b-roll"), not the song.
    clips = f"{song} {_spelling(labels.ideal_content_profile)}"
    return _TrackText(labels.genre, f" {song} ", f" {clips} ")


def track_genre_fits(track: Any, genres: Sequence[AskedGenre]) -> list[AskedGenre]:
    """The asked genres this track's labels say; none when the labels cannot be read.

    The enum value counts only for the genre it IS (``other`` is the catch-all, so never).
    Otherwise the genre must be a whole word in the song's own label text.
    """
    read = _read_track(track)
    if read is None:
        return []
    return [
        genre
        for genre in genres
        if (genre.genre is not None and read.genre == genre.genre)
        or any(
            f" {spelling} " in (read.song if genre.everyday else read.clips)
            for spelling in genre.spellings
        )
    ]


def genre_shortlist(tracks: Sequence[Any], genres: Sequence[AskedGenre]) -> list[Any]:
    """The tracks the matcher should rank for an ask that names ``genres``.

    The tracks whose labels say a genre; else the closest the library has (tracks of the
    enum family of a narrow genre: electronic for "house"); else all of them.
    """
    everything = list(tracks)
    if not genres:
        return everything
    exact = [track for track in everything if track_genre_fits(track, genres)]
    if exact:
        return exact
    family = []
    for track in everything:
        read = _read_track(track)
        if read is not None and any(genre.family == read.genre for genre in genres):
            family.append(track)
    return family or everything


def genre_record(genres: Sequence[AskedGenre], track: Any | None) -> dict[str, Any]:
    """What a frozen song section records about the genre; empty when none was asked."""
    if not genres:
        return {}
    fits = track_genre_fits(track, genres) if track is not None else []
    return {
        ASKED_GENRE: (fits or genres)[0].name,
        ASKED_GENRES: [genre.name for genre in genres],
        GENRE_FIT: bool(fits),
    }


def _join(names: Sequence[str], last: str) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


def _a(name: str) -> str:
    return "an" if name[:1].lower() in "aeiou" or name.startswith("R&B") else "a"


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
    listed = section.get(ASKED_GENRES)
    names = (
        [str(name) for name in listed if name] if isinstance(listed, list) and listed else [genre]
    )
    title = str(section.get("title") or "").strip()
    artist = str(section.get("artist") or "").strip()
    if section.get("status") != "selected" or not title:
        # No song came back: the matcher may simply have failed, so don't blame the library.
        return say(
            en=f"I couldn't suggest {_a(names[0])} {_join(names, 'or')} track this time.",
            tr=f"Bu sefer bir {_join(names, 'ya da')} şarkısı öneremedim.",
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
            f"I don't have any {_join(names, 'or')} tracks. The closest I have is {song_en}. "
            "Add it when you post if you like it."
        ),
        tr=(
            f"Elimde {_join(names, 'ya da')} şarkısı yok. En yakını {song_tr}. "
            "Beğenirsen paylaşırken ekleyebilirsin."
        ),
    )
