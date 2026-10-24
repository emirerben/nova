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

- Said as music. The music ask is every live audio/global requirement joined with ";", so
  it also holds source-sound asks ("Keep the tango band audible") and place or class nouns
  ("music for the jazz club visit"). Each ";" part is read on its own:
  - A part that names music (song, music, track, playlist, şarkı, müzik, canción...) asks a
    genre only when the genre is said as music: a music word right after it, or after a bare
    list it starts ("a tango, a salsa or a jazz song"; "-style" and articles bridge), or,
    in Spanish only, right before it ("música rock"). English "Make the music pop" is a
    verb, "house tour music" and "jazz club music" are other nouns.
  - A part with no music word asks a genre only if it is not an everyday word ("house",
    "rock", "pop" never) and the part has no sound word (audible, hear, mute, noise,
    voice, loud, ses*, duyul*, sonido...): "SUGGEST A TANGO", "Tango please" ask; "Keep the
    tango band audible" does not.
- A cue rules out the genres after it ("no", "without", "sin", "anything but", "other
  than", "hate", "tired of", "leave out"...; not "cut" or "drop", which are editing verbs)
  in its own clause. It reaches the next clause only when the words it rules out are
  themselves a bare list of genres, and that clause is one too ("No rap, reggaeton or
  rock"); never across a sentence end. Any other word ends it, and so does "but" / "pero" /
  "ama". "No vocals" / "not too slow" rule out nothing, and "No sad stuff, tango please"
  still asks for tango. Turkish puts the cue after the genre ("rap olmasın", "caz
  istemiyorum").

Fitting a track. The labels say a genre when ``MusicLabels.genre`` IS that genre (the
enum's own word, or a true synonym such as "rap" or "EDM"), or when its label text says the
word. A narrow alias ("house", "folk", "orchestral") is only a kind of an enum family, so
the family alone is not a fit: it is the closest the library has, offered as the closest.
Which text counts depends on what it describes:

- ``vibe_tags`` and ``mood`` describe the song: any genre word counts.
- The classifier ``rationale`` also says what clips the song wants ("wants rock climbing
  footage"): an everyday word counts there only right before a song noun ("rock anthem").
- ``ideal_content_profile`` describes the clips: only a word that is not also an everyday
  one counts there ("tango"), never "rock" or "house".
- In any of them, a genre word right after a negation ("not a tango", "non-tango", "rather
  than a tango", two words back, three for instead / rather) does not count.

Known limits, all on the "not asked" / "not a fit" side: "pop love song", "a song with a
tango feel", "Without rock, tango" and a bare genre in a part that names music elsewhere
("No sad songs, tango please", "Calm music, tango please") are not read as asks. A
rationale that spells the genre unusually ("rock-and-roll banger") is not a fit. Label
text that uses a genre word as an adjective ("dance-floor drama", "rock-solid beat") is
still trusted as the song's own words: only a negation or, for an everyday word in the
rationale, a missing song noun, keeps it from counting.

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
# Words that make a part of the ask about a SOUND from the footage ("keep the tango band
# audible"), not about the song: a genre word there names the band, the club, the class.
_SOURCE_SOUND_WORDS = frozenset(
    """
    audible hear heard hearing listen listening mute muted sound sounds noise noises volume
    loud louder quiet quieter voice voices sonido sonidos ruido ruidos escuchar escuche
    escuchen
    """.split()
)
_SOURCE_SOUND_PREFIXES = ("duyul",)  # Turkish "duyulsun" (let it be heard)
# Between a genre word and the music word that makes it a music ask: more genres, the
# connectors of a list, articles, and "-style" ("a tango or a jazz song", "tango tarzı bir şarkı").
_BRIDGE_WORDS = frozenset(
    """
    or and nor ni y o veya ve ya da a an the some any one another bir el la un una please por
    favor lutfen either style styled ish tarzi tarzinda turu turunde
    """.split()
)
# Spanish puts the music word first ("música rock"); in English "the music pop" is a verb.
_MUSIC_WORDS_BEFORE_THE_GENRE = frozenset("musica cancion canciones tema".split())
# In a song's label text: a genre word right after one of these (within 2 words) is what the
# song is NOT. "instead of a tango" / "rather than a tango" reach one word further.
_NEGATES_LABEL = frozenset("not no non without instead rather".split())
_NEGATES_LABEL_FAR = frozenset("instead rather".split())
# In the classifier's rationale an everyday genre word names the song only before one of these.
_SONG_NOUNS = frozenset("anthem track song ballad cue groove beat tune number bed piece".split())
# Ruling a genre out. Folded, apostrophes dropped ("don't" = "dont"). A bare "but" turns back
# and never rules out; "no vocals" / "not too slow" rule out nothing the song could be.
_RULES_OUT = frozenset(
    """
    no not without except avoid never dont sin hate dislike skip ditch remove ban unlike kill
    """.split()
)
_RULES_OUT_PHRASES = (
    ("anything", "but"),
    ("everything", "but"),
    ("instead", "of"),
    ("rather", "than"),
    ("other", "than"),
    ("doesnt", "have", "to", "be"),
    ("neednt", "be"),
    ("tired", "of"),
    ("sick", "of"),
    ("bored", "of"),
    ("rid", "of"),
    ("free", "of"),
    ("leave", "out"),
    ("away", "from"),
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
_GENRE_WORDS = frozenset(word for spelling in _LEXICON for word in spelling.split())
_BETWEEN_GENRE_AND_MUSIC = _GENRE_WORDS | _BRIDGE_WORDS


def _words(text: str) -> list[str]:
    return loose_words(re.sub(r"['’]", "", text))


def _is_music_word(word: str) -> bool:
    return word in _MUSIC_WORDS or word.startswith(_MUSIC_PREFIXES)


def _is_source_sound_word(word: str) -> bool:
    if word in _SOURCE_SOUND_WORDS or word.startswith(_SOURCE_SOUND_PREFIXES):
        return True
    # Turkish "ses" (sound), "sesi", "sesini", "sesli", not English "session" or "sesame".
    return word.startswith("ses") and not word.startswith(("sess", "sesa", "sest", "sesq"))


def _said_as_music(words: Sequence[str], start: int, end: int) -> bool:
    """Is the genre at ``words[start:end]`` said as music?

    A music word comes right after it, or after a bare list of genres and connectors that
    it starts ("a tango or a jazz song"); or, in Spanish only, one comes right before it
    ("música rock"). In English "Make the music pop" is a verb, "jazz club music" a place.
    """
    after = end
    while after < len(words) and words[after] in _BETWEEN_GENRE_AND_MUSIC:
        after += 1
    if after < len(words) and _is_music_word(words[after]):
        return True
    before = start - 1
    while before >= 0 and words[before] in _BETWEEN_GENRE_AND_MUSIC:
        before -= 1
    return before >= 0 and words[before] in _MUSIC_WORDS_BEFORE_THE_GENRE


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
    """Each genre named in the clauses of one sentence: (genre, ruled out, said as music).

    A cue rules out the genres after it in its clause. It carries into the next clause only
    when what it rules out here is a bare genre list and that clause is one too. "Said as
    music": see ``_said_as_music`` (read over the whole sentence, across its commas).
    """
    sentence = [word for words in clauses for word in words]
    offset = 0  # where this clause starts in ``sentence``
    ruling_out = False
    for words in clauses:
        if ruling_out and not _is_genre_list(words):
            ruling_out = False
        rules_out_from = 0  # where the words the active cue rules out begin
        i = 0
        while i < len(words):
            cue = _cue_length(words, i)
            if cue:
                i += cue
                if i < len(words) and words[i] in _NOT_A_GENRE_AFTER_CUE:
                    i += 1
                else:
                    ruling_out, rules_out_from = True, i
            elif words[i] in _TURNS_BACK:
                ruling_out = False
                i += 1
            elif (found := _genre_at(words, i)) is not None:
                genre, size = found
                end = i + size
                ruled_out = ruling_out or any(w in _RULED_OUT_AFTER for w in words[end : end + 2])
                yield genre, ruled_out, _said_as_music(sentence, offset + i, offset + end)
                i = end
            else:
                i += 1
        if ruling_out and not _is_genre_list(words[rules_out_from:]):
            ruling_out = False
        offset += len(words)


def asked_genres(ask: str) -> list[AskedGenre]:
    """The genres the creator's words name, in order of mention, each once.

    ``ask`` is the requirement texts joined by ";", so each part is read on its own, sentence
    by sentence and clause by clause. A genre the creator rules out ("no rap, reggaeton or
    rock") is not asked for, and an everyday word ("house") only when said as music ("a house
    track").
    """
    found: dict[str, AskedGenre] = {}
    for part in ask.split(";"):
        part_words = _words(part)
        # A part with no music word and no sound word is a bare ask ("Tango please").
        bare = not any(_is_music_word(w) or _is_source_sound_word(w) for w in part_words)
        for sentence in re.split(r"[.!?]", part):
            clauses = [_words(clause) for clause in re.split(r"[,:]", sentence)]
            for genre, ruled_out, as_music in _mentions(clauses):
                if not ruled_out and (as_music or (bare and not genre.everyday)):
                    found.setdefault(genre.name, genre)
    return list(found.values())


class _TrackText(NamedTuple):
    """A track's genre enum value and its label text, one folded word list per field."""

    genre: str  # ``MusicLabels.genre``
    song: list[list[str]]  # vibe tags and mood: what the song is
    rationale: list[str]  # the classifier's sentence: the song, and the clips it wants
    clips: list[str]  # ``ideal_content_profile``: the clips the song suits


def _read_track(track: Any) -> _TrackText | None:
    """The track's labels, read the way the matcher's candidate loader reads them.

    ``ai_labels`` is ``{"labels": ..., "rationale": ...}``: the classifier is told to keep
    genre words out of ``vibe_tags``, so its one-sentence rationale ("Driving rock anthem")
    says the genre too. ``None`` when the labels cannot be read.
    """
    raw = getattr(track, "ai_labels", None) or {}
    try:
        labels = MusicLabels(**(raw.get("labels") or raw))
    except (AttributeError, TypeError, ValueError):
        return None
    rationale = raw.get("rationale")
    return _TrackText(
        labels.genre,
        [loose_words(text) for text in [*labels.vibe_tags, labels.mood]],
        loose_words(rationale) if isinstance(rationale, str) else [],
        loose_words(labels.ideal_content_profile),
    )


def _is_negated(words: Sequence[str], i: int) -> bool:
    """Is the genre word at ``words[i]`` what the label says the song is NOT?"""
    return bool(
        _NEGATES_LABEL.intersection(words[max(0, i - 2) : i])
        or _NEGATES_LABEL_FAR.intersection(words[max(0, i - 3) : i])
    )


def _says(
    genre: AskedGenre, fields: Sequence[Sequence[str]], *, before_song_noun: bool = False
) -> bool:
    """Does one of these word lists name the genre, and not as what the song is not?

    ``before_song_noun``: the genre word must be followed by a song noun ("rock anthem").
    """
    for words in fields:
        for spelling in genre.spellings:
            name = spelling.split()
            for i in range(len(words) - len(name) + 1):
                end = i + len(name)
                if list(words[i:end]) != name or _is_negated(words, i):
                    continue
                if not before_song_noun or (end < len(words) and words[end] in _SONG_NOUNS):
                    return True
    return False


def _label_says(read: _TrackText, genre: AskedGenre) -> bool:
    """Do the track's label texts say this genre? An everyday word needs the song's own text."""
    if genre.everyday:
        return _says(genre, read.song) or _says(genre, [read.rationale], before_song_noun=True)
    return _says(genre, [*read.song, read.rationale, read.clips])


def track_genre_fits(track: Any, genres: Sequence[AskedGenre]) -> list[AskedGenre]:
    """The asked genres this track's labels say; none when the labels cannot be read.

    The enum value counts only for the genre it IS (``other`` is the catch-all, so never).
    Otherwise the genre must be named in the song's own label text.
    """
    read = _read_track(track)
    if read is None:
        return []
    return [
        genre
        for genre in genres
        if (genre.genre is not None and read.genre == genre.genre) or _label_says(read, genre)
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
