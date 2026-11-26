"""KRI-1558: a simple topic list for a Talking take, and one speaker layout per topic.

A Talking edit used to draw the speaker one way for the whole take: full screen, or (once pictures
were added) small in one corner from the first second to the last. Creators expect her place and
size to follow what she says. This module gives the take its sections and picks a layout for each.

Sections. The spoken words are cut into sentences (`split_sentences`, the same rules topic placement
uses), then grouped into a handful of passages: one per `TARGET_SECTION_S` of talk, at most
`MAX_SECTIONS`, never shorter than `MIN_SECTION_S`. A boundary goes between two sentences, chosen by
how long she pauses there and how much the words change across it (a TextTiling-style lexical shift
over word stems, so it works the same in Turkish and English and needs no model call). Sections
tile the take: the first starts at 0, each ends where the next starts, the last ends at the take's
end; a switch lands just before the first word of the next section.

Layouts. `own` (her own take, untouched), `full` (her cut-out at full size over the picture),
`small` (her cut-out small in a corner over the picture) and `picture` (the picture alone, her voice
on). `arrange_layouts` opens and closes on `own` (her face carries the hook and the ending) and lays
the pictures, in order, on the sections between, alternating `small` (alternating corners) and
`picture`. Every function here is pure and deterministic.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.pipeline.phone_talking_insert_topics import Sentence, split_sentences

__all__ = [
    "CORNER_PRESETS",
    "LAYOUTS",
    "MAX_SECTIONS",
    "MIN_SECTION_S",
    "SectionLayout",
    "TalkingSection",
    "arrange_layouts",
    "drawn_layout",
    "layout_mismatches",
    "section_at",
    "sections_from_cues",
    "talking_sections",
]

SpeakerLayout = Literal["own", "full", "small", "picture"]
LAYOUTS: tuple[SpeakerLayout, ...] = ("own", "full", "small", "picture")
# The corners a `small` section alternates between (named presets of `SPEAKER_FRAME_PRESETS`).
CORNER_PRESETS = ("small_bottom_right", "small_bottom_left")

# One section per this many seconds of talk.
TARGET_SECTION_S = 8.0
# No section is shorter than this (a layout that flashes by reads as a glitch).
MIN_SECTION_S = 3.0
MAX_SECTIONS = 6
# A pause this long counts fully toward a boundary.
_FULL_PAUSE_S = 1.2
# A switch lands this long before the first word of the next section (at most half the pause).
_SWITCH_LEAD_S = 0.12
# Sentences on each side compared for the lexical shift.
_SHIFT_SPAN = 2
# Word stems: the first five letters of words of at least three letters (Turkish endings fall away),
# minus the commonest function words of the app's languages (EN/DE/TR).
_STEM_LETTERS = 5
_MIN_WORD_LETTERS = 3
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_FUNCTION_WORDS = frozenset(
    # English
    "the and for you but are was this that with have not all can get your our they them what "
    "just like from will about there then when how its it's also very really so "
    # German
    "und der die das ich ist nicht sie mit den dem ein eine auch auf für aber wie wir was "
    "noch nur dann wenn mal schon sehr hier jetzt "
    # Turkish
    "bir ama için çok gibi daha olan olarak kadar sonra şimdi bunu şunu ben sen biz siz onlar "
    "değil var yok ile veya hem her şey nasıl neden çünkü yani evet hayır ki mi mı mu mü "
    "bu şu da de".split()
)
# Phrases that open a new point when a sentence starts with them (cue phrases).
_CUE_PHRASES = (
    # English
    "now",
    "next",
    "then",
    "finally",
    "lastly",
    "first",
    "firstly",
    "second",
    "secondly",
    "third",
    "thirdly",
    "another",
    "also",
    "moving on",
    "okay",
    "ok",
    "alright",
    "so now",
    "last but not least",
    # German
    "jetzt",
    "dann",
    "danach",
    "zuerst",
    "erstens",
    "zweitens",
    "drittens",
    "schließlich",
    "zum schluss",
    "außerdem",
    "als nächstes",
    "zuletzt",
    # Turkish
    "şimdi",
    "sonra",
    "daha sonra",
    "son olarak",
    "ilk olarak",
    "ilk önce",
    "öncelikle",
    "ikinci",
    "ikincisi",
    "üçüncü",
    "üçüncüsü",
    "bir de",
    "peki",
    "gelelim",
    "ayrıca",
    "en son",
    "bir diğer",
)


@dataclass(frozen=True)
class TalkingSection:
    """One passage of the take; ``index`` is 1-based. ``start_s``/``end_s`` tile the take."""

    index: int
    start_s: float
    end_s: float
    text: str
    sentence_first: int
    sentence_last: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "text": self.text,
            "sentence_first": self.sentence_first,
            "sentence_last": self.sentence_last,
        }


@dataclass(frozen=True)
class SectionLayout:
    """The layout of one section. ``preset`` is the corner of a `small` section; ``media_id`` the
    picture shown behind (`small`, `full`) or alone (`picture`), None for `own`."""

    index: int
    start_s: float
    end_s: float
    layout: SpeakerLayout
    preset: str | None = None
    media_id: str | None = None


def _stems(text: str) -> Counter[str]:
    return Counter(
        word.casefold()[:_STEM_LETTERS]
        for word in _WORD.findall(text)
        if len(word) >= _MIN_WORD_LETTERS and word.casefold() not in _FUNCTION_WORDS
    )


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(count * b.get(stem, 0) for stem, count in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def _opens_with_cue(text: str) -> bool:
    folded = " ".join(_WORD.findall(text.casefold()))
    return any(folded == cue or folded.startswith(cue + " ") for cue in _CUE_PHRASES)


def _boundary_scores(sentences: Sequence[Sentence]) -> list[float]:
    """Score of a boundary after sentence ``i`` (0-based), for ``i`` in ``0..len-2``: how long she
    pauses there, how deep the dip in word overlap is (TextTiling depth: how far the similarity
    across this gap falls below the highest similarity on each side), and whether the next sentence
    opens with a turn phrase ("now", "finally", "şimdi", "son olarak", "jetzt", ...)."""
    similarity: list[float] = []
    for i in range(len(sentences) - 1):
        before = _stems(" ".join(s.text for s in sentences[max(0, i + 1 - _SHIFT_SPAN) : i + 1]))
        after = _stems(" ".join(s.text for s in sentences[i + 1 : i + 1 + _SHIFT_SPAN]))
        similarity.append(_cosine(before, after))
    scores: list[float] = []
    for i, sim in enumerate(similarity):
        pause = max(0.0, sentences[i + 1].start_s - sentences[i].end_s)
        depth = (max(similarity[: i + 1]) - sim) + (max(similarity[i:]) - sim)
        cue = 1.0 if _opens_with_cue(sentences[i + 1].text) else 0.0
        scores.append(0.35 * min(pause / _FULL_PAUSE_S, 1.0) + 0.35 * min(depth, 1.0) + 0.3 * cue)
    return scores


def _switch_time(before: Sentence, after: Sentence) -> float:
    pause = max(0.0, after.start_s - before.end_s)
    return round(after.start_s - min(_SWITCH_LEAD_S, pause / 2), 3)


def talking_sections(
    words: Sequence[Any], take_s: float, *, max_sections: int = MAX_SECTIONS
) -> list[TalkingSection]:
    """The take's passages, in order, tiling ``0..take_s``.

    ``words`` are the creator's spoken words on one clock (``text``/``start_s``/``end_s``, any
    order); the sections are on the same clock. No speech, or a talk too short for two sections,
    is one section over the whole take. Deterministic: the same words give the same sections.
    """
    take_s = float(take_s)
    if take_s <= 0:
        return []
    sentences = split_sentences(words, take_s)
    if not sentences:
        return [TalkingSection(1, 0.0, round(take_s, 3), "", 0, 0)]
    talk_s = max(0.0, sentences[-1].end_s - sentences[0].start_s)
    wanted = max(1, min(max_sections, round(talk_s / TARGET_SECTION_S), len(sentences)))
    scores = _boundary_scores(sentences)
    # Boundary candidates by score (ties: the earlier one), accepted while every section keeps at
    # least `MIN_SECTION_S`.
    chosen: list[int] = []
    for i in sorted(range(len(scores)), key=lambda i: (-scores[i], i)):
        if len(chosen) >= wanted - 1:
            break
        trial = sorted([*chosen, i])
        cuts = [0.0, *(_switch_time(sentences[j], sentences[j + 1]) for j in trial), take_s]
        if all(b - a >= MIN_SECTION_S - 1e-9 for a, b in zip(cuts, cuts[1:], strict=False)):
            chosen = trial
    out: list[TalkingSection] = []
    first = 0
    for number, last in enumerate([*chosen, len(sentences) - 1], start=1):
        start = 0.0 if first == 0 else _switch_time(sentences[first - 1], sentences[first])
        end = (
            round(take_s, 3)
            if last == len(sentences) - 1
            else _switch_time(sentences[last], sentences[last + 1])
        )
        out.append(
            TalkingSection(
                index=number,
                start_s=round(start, 3),
                end_s=end,
                text=" ".join(s.text for s in sentences[first : last + 1]),
                sentence_first=sentences[first].index,
                sentence_last=sentences[last].index,
            )
        )
        first = last + 1
    return out


def sections_from_cues(cues: Sequence[Any], take_s: float) -> list[TalkingSection]:
    """`talking_sections` over caption cues' timed words (output timeline). A cue without words
    stands as one word spanning the cue."""
    words: list[dict[str, Any]] = []
    for cue in cues or ():
        cue_words = cue.get("words") if isinstance(cue, dict) else None
        if cue_words:
            for word in cue_words:
                if not isinstance(word, dict):
                    continue
                words.append(
                    {
                        "text": word.get("text") or word.get("word") or "",
                        "start_s": word.get("start_s", word.get("start", 0.0)),
                        "end_s": word.get("end_s", word.get("end", 0.0)),
                    }
                )
        elif isinstance(cue, dict):
            words.append(
                {
                    "text": cue.get("text") or "",
                    "start_s": cue.get("start_s", cue.get("start", 0.0)),
                    "end_s": cue.get("end_s", cue.get("end", 0.0)),
                }
            )
    return talking_sections(words, take_s)


def section_at(sections: Sequence[TalkingSection], t: float) -> TalkingSection | None:
    """The section drawing ``t`` (``start_s <= t < end_s``; the take's end belongs to the last)."""
    for section in sections:
        if section.start_s - 1e-6 <= t < section.end_s - 1e-6:
            return section
    return sections[-1] if sections and abs(t - sections[-1].end_s) <= 1e-6 else None


def arrange_layouts(
    sections: Sequence[TalkingSection], pictures: Sequence[str]
) -> list[SectionLayout]:
    """One layout per section, given the pictures to use (pool media ids, in the order to show).

    The first and the last section are `own` (her face opens and closes). The pictures go, in order,
    on the sections between, alternating `small` (alternating corners) and `picture`; a middle
    section left without a picture is `own`. With two sections the second takes the first picture
    as `small`. One section, or no picture, arranges nothing (an empty list: the edit keeps one
    layout). Pictures beyond the middle sections are not used.
    """
    if len(sections) < 2 or not pictures:
        return []
    out: list[SectionLayout] = []
    middle = list(sections[1:-1]) if len(sections) > 2 else [sections[1]]
    queue = list(pictures)
    slot = 0
    for section in sections:
        is_middle = section in middle
        if is_middle and queue:
            media_id = queue.pop(0)
            if slot % 2 == 0:
                preset = CORNER_PRESETS[(slot // 2) % len(CORNER_PRESETS)]
                out.append(
                    SectionLayout(
                        section.index, section.start_s, section.end_s, "small", preset, media_id
                    )
                )
            else:
                out.append(
                    SectionLayout(
                        section.index, section.start_s, section.end_s, "picture", None, media_id
                    )
                )
            slot += 1
        else:
            out.append(SectionLayout(section.index, section.start_s, section.end_s, "own"))
    return out


def _span(row: Any) -> tuple[float, float, str, Any]:
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    start = get("start_s", None)
    start = get("start", 0.0) if start is None else start
    end = get("end_s", None)
    end = get("end", 0.0) if end is None else end
    frame = get("speaker_frame", None) or get("frame", None)
    if frame is not None and not isinstance(frame, dict):
        frame = frame.model_dump() if hasattr(frame, "model_dump") else dict(frame)
    return float(start), float(end), str(get("layout", "")), frame


def drawn_layout(layout_windows: Sequence[Any], t: float) -> tuple[str, Any]:
    """(layout, frame) the layout windows draw at ``t``; ("", None) where none covers it."""
    for row in layout_windows or ():
        start, end, layout, frame = _span(row)
        if start - 1e-6 <= t < end - 1e-6:
            return layout, frame
    return "", None


def layout_mismatches(
    sections: Sequence[TalkingSection],
    layout_windows: Sequence[Any],
    planned: Sequence[SectionLayout],
    *,
    tolerance_s: float = 0.25,
) -> list[str]:
    """Where the drawn layouts disagree with the plan, checked against the section list.

    Each planned section must be drawn, at its middle, with its planned layout (and a ``small``
    one at its planned corner), and every place the drawn layout changes must be a section
    boundary (within ``tolerance_s``). Empty when the edit draws the plan. ``layout_windows`` are
    document rows (``start_s``/``end_s``/``speaker_frame``) or recipe rows (``start``/``end``/
    ``frame``) on the same clock as ``sections``.
    """
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    problems: list[str] = []
    by_index = {section.index: section for section in sections}
    for plan in planned:
        section = by_index.get(plan.index)
        if section is None:
            problems.append(f"section {plan.index} is not in the section list")
            continue
        middle = (section.start_s + section.end_s) / 2
        layout, frame = drawn_layout(layout_windows, middle)
        if layout != plan.layout:
            problems.append(
                f"section {plan.index} draws {layout or 'no layout'}, planned {plan.layout}"
            )
        elif plan.layout == "small" and plan.preset is not None:
            want = SPEAKER_FRAME_PRESETS.get(plan.preset)
            if not isinstance(frame, dict) or any(
                abs(float(frame.get(key, -1)) - value) > 1e-6 for key, value in (want or {}).items()
            ):
                problems.append(f"section {plan.index} is small in the wrong corner")
    boundaries = [section.start_s for section in sections[1:]]
    rows = sorted((_span(row) for row in layout_windows or ()), key=lambda span: span[0])
    edges = {round(start, 3) for start, _, _, _ in rows} | {round(end, 3) for _, end, _, _ in rows}
    take_end = sections[-1].end_s if sections else 0.0
    for edge in sorted(edges):
        if edge <= tolerance_s or abs(edge - take_end) <= tolerance_s:
            continue
        before, _ = drawn_layout(layout_windows, edge - 1e-3)
        after, _ = drawn_layout(layout_windows, edge + 1e-3)
        if before == after:
            continue
        if not any(abs(edge - boundary) <= tolerance_s for boundary in boundaries):
            problems.append(f"the layout changes at {edge:g}s, which is not a section boundary")
    return problems
