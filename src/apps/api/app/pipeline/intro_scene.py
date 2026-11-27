"""KRI-1610: `IntroScene`, the closed shape an AI fills in to compose a creator-style intro.

The scene is made only of ids the server issued for this footage and title: one voice
(``solid`` / ``accent``) per word of the creator's approved title, and an option id each for font
pair, ink, title zone, entrance and sound, or an explicit decline. It has no field that reaches
the screen as text and none that holds a coordinate, a size or a time (the structural test walks
the JSON schema and fails on any free string / integer / number). The engine owns the rest: sizes,
wrapping, fit-to-width, glyph coverage, and where the block sits clear of the face and captions
(`phone_title_look.place_title_look`). The same stance as `intro_cluster`: LLMs are not trusted
with coordinates.

`compile_intro_scene` turns a scene into today's title rows (the `title_look_rows` shape, one
`TextElement` row per voice run), so the iPhone renderer, the editor and the approval check need
no change. `intro_scene_pick` (KRI-1611, flagged off) calls it. Every failure returns
``rows=None`` with
a reason, which is "use today's title" for the caller.

Ways this could go wrong, and the guard for each (tests/pipeline/test_intro_scene*.py):

* S1: the model writes text or numbers that reach the screen. The schema has none.
* S2: the split drops, adds or reorders a word. `check_scene` needs one voice per word; rows are
  her exact words in order (caps come from ``text_case``, never from rewriting a word).
* S3: an id the server never issued, or a face that cannot draw her title, is accepted.
  `issue_menu` filters by glyph coverage; `check_scene` refuses anything outside the menu.
  ``pen`` (the handwriting effect) draws Patrick Hand strokes whatever the pair says, so it is
  issued only with a pair whose accent face is Patrick Hand.
* S4: a half-filled decline or an unfinished compose. The model validator refuses both.
* S5: a scene the stacked-title reader would read differently (one run, a run under two
  characters or without a letter, more than five runs). The rows must round-trip through
  `split_title_lines(title_look_text(rows))`.
* S6: a failure breaks the render. Everything is caught; the caller gets today's title.
* S7: Turkish caps (``text_language`` rides on the row so "trendi" caps to "TRENDİ").
* S8: more than one ink. Every row takes the scene's single ink.
* S11: rows that compile but cannot sit clear: a long accent run at 250px, a long caps row that
  rises above the title safe zone, a row whose ink box covers the face box. `preflight_rows`
  places the rows on a typical speaker (the same `place_title_look`), renders them, and measures
  the ink, so `compile_intro_scene` declines them (`placement_unclean`, `ink_off_zone`). The
  typical face is a stand-in: the caller still holds the real placement receipt and MUST gate on
  `placement_is_clean(receipt)` before using the rows.

KRI-1618 adds two moves, each an option id the engine maps through a table (`STAGGERS`,
`STACKS`): the accent rows entering after the solid rows, and a script row tucked onto the serif
row under it. Rows declare them in their ``title_look`` param; times and gaps are derived from
that declaration, never stored as free numbers.

* M1: the stagger holds back the solid rows, or the accent outlives them. Accent rows start at the
  issued delay, solid rows at the title's start, and every row ends together.
* M2: a delay nobody issued reaches the screen or passes approval. The delay is an id;
  `stacked_title_matches` accepts a late row only at an issued delay (`title_family`).
* M3: a stack whose accent leads (ref 5) fails the opening-title check. Approval reads the
  stack's earliest start.
* M4: a hold too short for the delay. The compiler declines (`hold_too_short`); a later retime
  to a short window shows the rows together.
* M5: a chat retime, the offer's window or a Range cut bends the delay. Every retime goes through
  `title_family.retime_title_rows`, which derives starts from the declaration (idempotent).
* M6: window code reads line 1 and misses the late row. `title_family.title_window`.
* M7: the tuck with a pair that has no script face, or with no script row directly above a serif
  row. `check_scene` refuses both (an inert declaration is refused too).
* M8: the tuck buries the serif's letters or Turkish marks. The geometry is the approved taste
  board's (B2): the script baseline sits `tuck_caps` serif cap heights above the serif baseline,
  so only script descenders cross; checked by `make verify-intro-scenes` in English and Turkish.
* M9: an undeclared overlap passes the render check. The verifier allows ink on ink only for a
  declared script-over-serif pair (negative control).
* M10: the phone clips or reorders overlapping rows. Observed not to
  (`IntroRowMotionRenderTests`, on layers the compiler made).
* M11: old pins, cassettes and goldens change. The defaults are today's look and add no key.

KRI-1613 gives the scene its own sound: ``sound`` (a slug of the seeded creator library) and
``sound_cue``, which says where it plays, through `SOUND_CUES`: ``none``, ``accent`` (once, as
the accent lines enter) or ``each`` (once per entrance; lines that enter on the same frame share
one). Rows declare both in their ``title_look`` param; `title_family.intro_sound_cues` derives
every sound from that declaration: an ``introsfx-<line id>`` row at the line's ``start_s``, the
entrance's first frame. The worker resolves the slug to the library row
(`services.sfx_library`), an editor Save re-pins the rows to the lines and drops them with the
declaration. Guards: tests/pipeline/test_intro_scene_sound.py, tests/tasks/
test_intro_scene_sound_{worker,save,phone_fixture}.py, tests/services/test_sfx_library.py and
the KriaMediaEngine export test `IntroSceneSoundExportTests`.

* M12: a sound plays that nobody issued, or a cue with nothing to play. ``sound_cue`` is an id
  of the menu like the others; a cue needs a sound (``sound_cue_needs_sound``) and a line it can
  sit on (``sound_cue_has_no_line``: ``accent`` with no accent line). A slug without a cue is
  silent, as every scene before KRI-1613 was (teardowns and intake replies carry slugs).
* M13: a sound when a gate is closed: ``INTRO_SCENE_SOUND_ENABLED``, the Talking sound-effect
  lane (`sfx_capability.subtitled_sfx_limit`), or her brief says "no sound effects". The worker
  then compiles the scene silent, so its rows declare nothing and no row is written.
* M14: the cue misses the entrance (a stagger, a retime, a Range cut, a speech cut). It is
  derived from the line's own ``start_s`` on the cut timeline, after every lane was remapped,
  and re-derived on every Save; `phone_talking_range` never moves it through source time.
* M15: two sounds stacked on one entrance (louder, phasey). Lines that enter together share one
  cue; a sound she named (or any lane sound) within `sfx_autoplace.MIN_SPACING_S` of a cue wins
  and the cue is skipped.
* M16: a library row that is missing, unpublished or unplayable fails the render. The cue is
  skipped and the ``intro_scene_sound`` trace event says why.
* M17: her edit loses. A Save never adds an intro sound; one she deleted stays deleted, one
  whose line is gone or no longer declares it (another look, a whole-title rewrite) is dropped.
  Sounds already saved stay when the switch is later turned off (the colour-moment precedent).
* M18: old pins, cassettes and goldens change. ``sound_cue`` defaults to ``none`` and adds no
  key; a silent scene's rows and dumps are byte for byte as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError, model_serializer, model_validator

if TYPE_CHECKING:
    from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

INTRO_SCENE_VERSION = 1

Voice = Literal["solid", "accent"]
DeclineReason = Literal["no_good_fit", "footage_unsuited", "title_unsuited"]
FontPairId = Literal[
    "grotesk_hand", "serif_italic", "grotesk_loop", "script_serif", "young_serif", "grotesk_pen"
]
InkId = Literal["oxblood", "chartreuse", "cream", "butter", "white"]
ZoneId = Literal["above_head", "top_left", "mid_frame"]
EntranceId = Literal["fade", "rise", "pop", "ink", "pen"]
SoundId = Literal[
    "none", "accent-pop", "soft-pop", "click", "sparkle", "whoosh-fast", "camera-shutter"
]
StaggerId = Literal["together", "accent_after"]
StackId = Literal["gap", "script_over_serif"]
SoundCueId = Literal["none", "accent", "each"]

_MAX_RUNS = 5
_MIN_RUN_CHARS = 2
PEN_FAMILY = "Patrick Hand"  # the only face the `handwriting` effect ever draws


@dataclass(frozen=True)
class Face:
    family: str
    size_px: int
    text_case: Literal["upper"] | None = None
    line_spacing: float | None = None


@dataclass(frozen=True)
class FontPair:
    solid: Face
    accent: Face


# Sizes are px on the 1080x1920 canvas, from the approved taste board (2026-11-24); the renderer
# shrinks a row that does not fit its width and the placement shrinks the block again.
FONT_PAIRS: dict[str, FontPair] = {
    "grotesk_hand": FontPair(
        Face("Hanken Grotesk", 124, "upper", 1.02), Face("Zeyada", 150, None, 1.0)
    ),
    "serif_italic": FontPair(
        Face("Instrument Serif", 150, None, 1.0), Face("Instrument Serif Italic", 140, None, 1.0)
    ),
    "grotesk_loop": FontPair(
        Face("Hanken Grotesk", 100, None, 1.02), Face("Over the Rainbow", 250, None, 1.0)
    ),
    "script_serif": FontPair(
        Face("Newsreader", 140, None, 1.0), Face("Great Vibes", 190, None, 1.0)
    ),
    "young_serif": FontPair(
        Face("Young Serif", 92, None, 1.05), Face("Young Serif", 92, None, 1.05)
    ),
    "grotesk_pen": FontPair(Face("Hanken Grotesk", 110, "upper", 1.02), Face(PEN_FAMILY, 105)),
}
INKS: dict[str, str] = {
    "oxblood": "#8A1C1F",
    "chartreuse": "#E6FF00",
    "cream": "#FBF1D8",
    "butter": "#F6E7A8",
    "white": "#FFFFFF",
}
# Zone id -> the `place_title_look` position it maps to (None = its default: the band above the
# head, then below or above the face). Zones it cannot honour (under the chin, between cards)
# are not offered: see `prompts/intro_teardowns.json`.
ZONES: dict[str, str | None] = {"above_head": None, "top_left": "top_left", "mid_frame": "middle"}
# Entrance id -> (solid-row effect, accent-row effect), TextElement effects both renderers draw.
ENTRANCES: dict[str, tuple[str, str]] = {
    "fade": ("fade-in", "fade-in"),
    "rise": ("slide-up", "fade-in"),
    "pop": ("pop-in", "pop-in"),
    "ink": ("fade-in", "ink-reveal"),
    "pen": ("fade-in", "handwriting"),
}
# Slugs of the seeded creator sound library (`scripts/sfx_library/catalog.py`); "none" = silent.
SILENT = "none"
SOUNDS: tuple[str, ...] = (
    SILENT,
    "accent-pop",
    "soft-pop",
    "click",
    "sparkle",
    "whoosh-fast",
    "camera-shutter",
)
# Sound cue id -> the line styles (`title_look.style`) whose entrance it sounds on.
SOUND_CUES: dict[str, frozenset[str]] = {
    SILENT: frozenset(),
    "accent": frozenset({"intro_accent"}),
    "each": frozenset({"intro_solid", "intro_accent"}),
}
# Stagger id -> seconds the accent rows wait after the solid rows. 0.6 s is ref 1 on the approved
# taste board (A1: the caps at 0.15 s, the handwritten accent at 0.75 s).
TOGETHER = "together"
STAGGERS: dict[str, float] = {TOGETHER: 0.0, "accent_after": 0.6}


@dataclass(frozen=True)
class Stack:
    pairs: tuple[str, ...]  # the font pairs it may be used with
    # A script (accent) row directly above a serif (solid) row: its baseline sits this many of
    # the serif's cap heights above the serif baseline. None: every row keeps the plain gap.
    tuck_caps: float | None = None


GAP = "gap"
# 1.28: the taste board's B2 (ref 5), Great Vibes 190 over Newsreader 140; only the script's
# descenders reach into the serif's caps.
STACKS: dict[str, Stack] = {
    GAP: Stack(tuple(FONT_PAIRS)),
    "script_over_serif": Stack(("script_serif",), tuck_caps=1.28),
}


class IntroScene(BaseModel):
    """One composed intro, or a decline. Ids and voices only (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["compose", "decline"]
    decline_reason: DeclineReason | None = None
    voices: tuple[Voice, ...] = ()
    font_pair: FontPairId | None = None
    ink: InkId | None = None
    zone: ZoneId | None = None
    entrance: EntranceId | None = None
    sound: SoundId | None = None
    # KRI-1618 / KRI-1613. The defaults are today's look, so an answer or pin from before reads
    # unchanged.
    stagger: StaggerId = TOGETHER
    stack: StackId = GAP
    sound_cue: SoundCueId = SILENT

    @model_validator(mode="after")
    def _shape(self) -> IntroScene:
        choices = (self.font_pair, self.ink, self.zone, self.entrance, self.sound)
        if self.decision == "decline":
            if self.decline_reason is None:
                raise ValueError("a decline names its reason")
            if (
                self.voices
                or any(choice is not None for choice in choices)
                or (self.stagger, self.stack, self.sound_cue) != (TOGETHER, GAP, SILENT)
            ):
                raise ValueError("a decline carries no choices")
            return self
        if self.decline_reason is not None:
            raise ValueError("a compose has no decline reason")
        if not self.voices or any(choice is None for choice in choices):
            raise ValueError("a compose needs voices and one option id for every choice")
        return self

    @model_serializer(mode="wrap")
    def _omit_todays_look(self, handler: Any) -> dict[str, Any]:
        # A scene from before KRI-1618 dumps byte for byte as it did (teardowns, pins, cassettes).
        data = handler(self)
        return {
            k: v for k, v in data.items() if k not in ("stagger", "stack", "sound_cue")
        } | self.moves()

    def moves(self) -> dict[str, str]:
        """What this scene adds to today's look, as rows and pins store it: the KRI-1618 moves
        and, when a cue plays it, the KRI-1613 sound. Empty for today's look."""
        return {
            **({"stagger": self.stagger} if self.stagger != TOGETHER else {}),
            **({"stack": self.stack} if self.stack != GAP else {}),
            **(
                {"sound": self.sound, "sound_cue": self.sound_cue}
                if self.sound_cue != SILENT
                else {}
            ),
        }


# --- her words ----------------------------------------------------------------------------


@dataclass(frozen=True)
class IntroWord:
    text: str
    segment: int  # the line she typed it on (a newline or a spaced slash starts a new one)


_BREAK = re.compile(r"\n+|\s+/\s+")


def title_words(title: str | None) -> list[IntroWord]:
    if not title or not title.strip():
        return []
    words: list[IntroWord] = []
    for segment, part in enumerate(_BREAK.split(title.strip())):
        words.extend(IntroWord(text, segment) for text in part.split())
    return words


# --- the issued menu ----------------------------------------------------------------------

_TURKISH_LETTERS = set("ğĞşŞıİçÇöÖüÜ")


def face_draws(family: str, text: str) -> bool:
    """True when ``family`` draws ``text`` (and its caps form) without `drawable_family`
    swapping in another face."""
    from app.pipeline.phone_text_style import drawable_family  # noqa: PLC0415

    needed = f"{text} {text.upper()}"
    if _TURKISH_LETTERS & set(text):
        needed += " İı"
    return drawable_family(family, [needed]) == family


@dataclass(frozen=True)
class IntroMenu:
    """The ids the server issues for this footage and title. The AI picks from these only."""

    font_pairs: tuple[str, ...]
    inks: tuple[str, ...]
    zones: tuple[str, ...]
    entrances: tuple[str, ...]
    sounds: tuple[str, ...]
    staggers: tuple[str, ...] = (TOGETHER,)
    stacks: tuple[str, ...] = (GAP,)
    sound_cues: tuple[str, ...] = (SILENT,)

    def entrances_for(self, font_pair: str) -> tuple[str, ...]:
        """``pen`` draws Patrick Hand strokes, so only a pair whose accent face is Patrick Hand
        may use it (any other pair would silently show a different face than it names)."""
        accent = FONT_PAIRS[font_pair].accent.family
        return tuple(e for e in self.entrances if e != "pen" or accent == PEN_FAMILY)

    def stacks_for(self, font_pair: str) -> tuple[str, ...]:
        return tuple(s for s in self.stacks if font_pair in STACKS[s].pairs)


def issue_menu(title: str) -> IntroMenu:
    text = " ".join(word.text for word in title_words(title))
    pairs = tuple(
        pair_id
        for pair_id, pair in FONT_PAIRS.items()
        if face_draws(pair.solid.family, text) and face_draws(pair.accent.family, text)
    )
    return IntroMenu(
        font_pairs=pairs,
        inks=tuple(INKS),
        zones=tuple(ZONES),
        entrances=tuple(ENTRANCES),
        sounds=SOUNDS,
        staggers=tuple(STAGGERS),
        stacks=tuple(STACKS),
        sound_cues=tuple(SOUND_CUES),
    )


def check_scene(scene: IntroScene, title: str, menu: IntroMenu) -> str | None:
    """The reason a composed scene cannot be used for ``title``, or ``None`` when it can."""
    if scene.decision != "compose":
        return "declined"
    words = title_words(title)
    if not words:
        return "empty_title"
    if len(scene.voices) != len(words):
        return "voices_not_one_per_word"
    if (
        scene.font_pair not in menu.font_pairs
        or scene.ink not in menu.inks
        or scene.zone not in menu.zones
        or scene.entrance not in menu.entrances_for(scene.font_pair)
        or scene.sound not in menu.sounds
        or scene.stagger not in menu.staggers
        or scene.stack not in menu.stacks_for(scene.font_pair)
        or scene.sound_cue not in menu.sound_cues
    ):
        return "option_not_issued"
    runs = _runs(words, scene.voices)
    if scene.stagger != TOGETHER and {run.voice for run in runs} != {"solid", "accent"}:
        return "stagger_needs_both_voices"
    if STACKS[scene.stack].tuck_caps is not None and not any(
        upper.voice == "accent" and lower.voice == "solid"
        for upper, lower in zip(runs, runs[1:], strict=False)
    ):
        return "stack_needs_script_above_serif"
    if scene.sound_cue != SILENT:
        if scene.sound == SILENT:
            return "sound_cue_needs_sound"
        if not any(f"intro_{run.voice}" in SOUND_CUES[scene.sound_cue] for run in runs):
            return "sound_cue_has_no_line"
    return None


# --- the placement preflight --------------------------------------------------------------

# A typical speaker (head with clear wall above it) and the captions under it, normalized 0-1.
# Stand-ins: compile has no footage, so it asks "could these rows sit clear of a typical head?".
# Built lazily (module ``__getattr__`` below): `render_geometry` imports Skia, and the schema
# half of this module must import without it (structural-evals CI has no libEGL, KRI-1612).
_REGIONS: dict[str, tuple[float, float, float, float]] = {
    "TYPICAL_FACE": (0.27, 0.29, 0.74, 0.73),
    "TYPICAL_CAPTIONS": (0.12, 0.81, 0.88, 0.87),
    "SAFE_ZONE": (0.03, 0.06, 0.97, 0.95),  # the title safe zone `phone_title_look` places inside
}


def __getattr__(name: str) -> Any:
    if name == "NormalizedBox" or name in _REGIONS:
        from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

        return NormalizedBox if name == "NormalizedBox" else NormalizedBox(*_REGIONS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


CLEAN_STATUSES = frozenset({"preset", "moved", "moved_below_face", "shrunk", "squeezed"})
MAX_INK_COVERAGE = 0.05  # the title placement's own face-coverage limit, here on the ink box


def placement_is_clean(receipt: dict[str, Any]) -> bool:
    """True when a `place_title_look` receipt is a clean spot: not ``best_effort`` / ``error``
    and no face or text collision. The caller MUST check its real receipt with this."""
    return (
        str(receipt.get("status")) in CLEAN_STATUSES
        and not receipt.get("face_collision")
        and not receipt.get("text_overlap")
    )


def ink_box_problems(
    box: NormalizedBox, *, face: NormalizedBox, captions: NormalizedBox
) -> list[str]:
    """Why a row's measured ink box is not acceptable (empty when it is)."""
    problems = []
    safe = __getattr__("SAFE_ZONE")
    if (
        box.left < safe.left - 1e-6
        or box.right > safe.right + 1e-6
        or box.top < safe.top - 1e-6
        or box.bottom > safe.bottom + 1e-6
    ):
        problems.append(f"ink outside the title safe zone {box.as_dict()}")
    if box.coverage_by(face) > MAX_INK_COVERAGE:
        problems.append(f"ink covers the face ({box.coverage_by(face):.2f})")
    if box.coverage_by(captions) > MAX_INK_COVERAGE:
        problems.append(f"ink covers the captions ({box.coverage_by(captions):.2f})")
    return problems


def overlay_for_row(row: dict[str, Any]) -> dict[str, Any]:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415

    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)],
        video_duration_s=max(float(row["end_s"]), 0.1),
        independent_box_alignment=True,
    )
    return overlay


def place_on_typical_face(
    rows: list[dict[str, Any]], position: str | None, canvas: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.pipeline.phone_title_look import place_title_look  # noqa: PLC0415
    from app.pipeline.render_geometry import ProtectedRegion  # noqa: PLC0415

    region = ProtectedRegion(0.0, 1e6, __getattr__("TYPICAL_FACE"), kind="face")

    def sample(_path, anchors, **_kwargs):  # noqa: ANN001, ANN202 - a fixed stand-in speaker
        return [region], {"attempted": len(anchors), "decoded": 1, "detected": 1}

    return place_title_look(
        rows,
        clip_path="typical-speaker.mp4",
        keep_segments=[(0.0, 1e6)],
        display_width=canvas.width,
        display_height=canvas.height,
        canvas=canvas,
        sample=sample,
        position=position,
        avoid=[__getattr__("TYPICAL_CAPTIONS")],
    )


def preflight_rows(rows: list[dict[str, Any]], position: str | None, canvas: Any) -> str | None:
    """Place ``rows`` on a typical speaker, draw them, and measure the ink. Returns the decline
    reason, or ``None`` when they sit clear."""
    from app.pipeline import overlay_verify as ov  # noqa: PLC0415
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    placed, receipt = place_on_typical_face(rows, position, canvas)
    if not placement_is_clean(receipt):
        return "placement_unclean"
    for row in placed:
        bare = ov.render_overlay_frame({**overlay_for_row(row), "shadow_enabled": False})
        bbox = ov.text_bbox(bare)
        if bbox is None:
            return "ink_off_zone"
        box = NormalizedBox(
            bbox[0] / bare.width, bbox[1] / bare.height, bbox[2] / bare.width, bbox[3] / bare.height
        )
        if ink_box_problems(
            box, face=__getattr__("TYPICAL_FACE"), captions=__getattr__("TYPICAL_CAPTIONS")
        ):
            return "ink_off_zone"
    return None


# --- compile ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    voice: str
    text: str


@dataclass(frozen=True)
class IntroCompile:
    """``rows`` is ``None`` for every failure (``reason`` says why): use today's title."""

    rows: list[dict[str, Any]] | None
    reason: str
    typed_title: str = ""  # her words with " / " between rows, what `stacked_title_matches` reads
    sound: str = "none"
    position: str | None = None  # for `place_title_look(position=...)`


class _Decline(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _runs(words: list[IntroWord], voices: tuple[str, ...]) -> list[_Run]:
    """One run per stretch of the same voice on the same typed line."""
    groups: list[tuple[str, int, list[str]]] = []
    for word, voice in zip(words, voices, strict=True):
        if groups and groups[-1][0] == voice and groups[-1][1] == word.segment:
            groups[-1][2].append(word.text)
        else:
            groups.append((voice, word.segment, [word.text]))
    return [_Run(voice, " ".join(parts)) for voice, _segment, parts in groups]


def _run_reads_as_a_line(text: str) -> bool:
    """`phone_title_look.split_title_lines`' rule for one line."""
    return len(text.replace(" ", "")) >= _MIN_RUN_CHARS and any(ch.isalpha() for ch in text)


def _build_rows(
    runs: list[_Run],
    scene: IntroScene,
    *,
    canvas: Any,
    language: str | None,
    duration_s: float | None,
    timeline_duration_s: float,
) -> list[dict[str, Any]]:
    """One `TextElement` row per run, in the `phone_title_look.title_look_rows` shape."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline import phone_subtitled_title as title_mod  # noqa: PLC0415
    from app.pipeline.narrated_title import narrated_title_placement  # noqa: PLC0415
    from app.pipeline.phone_title_look import (  # noqa: PLC0415
        _HOLD_S,
        _LEFT_X_FRAC,
        _MAX_WIDTH_FRAC,
        TITLE_LOOK_PARAM,
    )
    from app.pipeline.title_family import MIN_LINE_S, retime_title_rows  # noqa: PLC0415

    assert scene.font_pair and scene.ink and scene.zone and scene.entrance  # checked upstream
    pair = FONT_PAIRS[scene.font_pair]
    solid_effect, accent_effect = ENTRANCES[scene.entrance]
    hold = float(duration_s) if duration_s is not None else _HOLD_S
    end_s = round(min(hold, float(timeline_duration_s)), 3)
    if end_s - STAGGERS[scene.stagger] < MIN_LINE_S:
        raise _Decline("hold_too_short")
    left = scene.zone == "top_left"
    rows: list[dict[str, Any]] = []
    for index, run in enumerate(runs):
        accent = run.voice == "accent"
        face = pair.accent if accent else pair.solid
        if not face_draws(face.family, run.text):
            raise _Decline("face_cannot_draw_title")
        base = narrated_title_placement(run.text, canvas=canvas, explicit=True)
        element = TextElement(
            id=title_mod.TALKING_TITLE_ELEMENT_ID
            if index == 0
            else f"{title_mod.TALKING_TITLE_ELEMENT_ID}-line-{index + 1}",
            text=run.text,
            start_s=0.0,
            end_s=end_s,
            role="generative_intro",
            effect=accent_effect if accent else solid_effect,
            **{
                **base,
                "font_family": face.family,
                "size_px": face.size_px,
                "color": INKS[scene.ink],
                "alignment": "left" if left else "center",
                "x_frac": _LEFT_X_FRAC if left else 0.5,
                "y_frac": 0.15,
                "max_width_frac": _MAX_WIDTH_FRAC,
                "shadow_style": "high_visibility",
                **({"text_case": face.text_case} if face.text_case else {}),
                **({"line_spacing": face.line_spacing} if face.line_spacing else {}),
                **({"text_language": language} if language else {}),
            },
            source_params={
                "source": title_mod.TALKING_TITLE_SOURCE,
                TITLE_LOOK_PARAM: {
                    "line": index + 1,
                    "lines": len(runs),
                    "style": f"intro_{run.voice}",
                    **scene.moves(),
                },
            },
        )
        rows.append(element.model_dump(mode="json", exclude_none=True))
    return retime_title_rows(rows, 0.0, end_s)


def compile_intro_scene(
    scene: IntroScene | dict[str, Any],
    title: str,
    *,
    canvas: Any,
    timeline_duration_s: float,
    language: str | None = None,
    duration_s: float | None = None,
    menu: IntroMenu | None = None,
) -> IntroCompile:
    """Today's title rows for ``scene`` over ``title``, or ``rows=None`` and the reason. Never
    raises: a decline, a bad scene, an unissued id or any error all mean "use today's title"."""
    from app.pipeline.phone_title_look import (  # noqa: PLC0415
        split_title_lines,
        title_look_text,
    )

    try:
        if not isinstance(scene, IntroScene):
            try:
                scene = IntroScene.model_validate(scene)
            except ValidationError:
                return IntroCompile(None, "invalid_scene")
        reason = check_scene(scene, title, menu or issue_menu(title))
        if reason:
            return IntroCompile(None, reason)
        runs = _runs(title_words(title), scene.voices)
        if len(runs) == 1:
            return IntroCompile(None, "single_run")  # a lone row is the plain title path
        if len(runs) > _MAX_RUNS:
            return IntroCompile(None, "too_many_runs")
        if not all(_run_reads_as_a_line(run.text) for run in runs):
            return IntroCompile(None, "run_too_short")
        rows = _build_rows(
            runs,
            scene,
            canvas=canvas,
            language=language,
            duration_s=duration_s,
            timeline_duration_s=timeline_duration_s,
        )
        typed = title_look_text(rows)
        if typed is None or split_title_lines(typed) != [run.text for run in runs]:
            return IntroCompile(None, "stack_mismatch")
        assert scene.zone and scene.sound
        unsat = preflight_rows(rows, ZONES[scene.zone], canvas)
        if unsat:
            return IntroCompile(None, unsat)
        return IntroCompile(
            rows, "ok", typed_title=typed, sound=scene.sound, position=ZONES[scene.zone]
        )
    except _Decline as decline:
        return IntroCompile(None, decline.reason)
    except Exception as exc:  # noqa: BLE001 - a styled intro never blocks a render
        log.warning("intro_scene.compile_failed", error=str(exc)[:200])
        return IntroCompile(None, "compile_error")
