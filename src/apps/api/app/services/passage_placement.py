"""One placement decision per passage of speech (KRI-1556).

Which picture, if any, goes with a passage of the creator's speech, and why. "No picture here" is
a decision: the speaker stays on screen and the passage can be named as one that needs a picture.

Modes:

* ``meaning``: the card matcher (`phone_overlay_grounding._match_placements`, the Gemini
  `OverlayPlacementAgent`, with `heuristic_match` as the no-model fallback) decides by what the
  words say. The creator's request is passed to it. Candidates reach it in one stable order, so
  re-ordering the pool never changes the result.
* ``slideshow``: the creator asked for an order or a pace, so her order and pace win over the
  matcher. It is the only mode that rotates, spaces evenly or shows every picture; it is built only
  from an explicit ask (`PlacementRequest.slideshow`).
* ``phrase``: "when I say X show Y". Declared here; path 4 (`phone_reaction_grounding`) moves in
  with unchanged behaviour in a later step.

`PlacementResult.placements` and `.wishlist` carry the matcher's own output (timing, slot, tier)
for callers that still build cards from it. `needs` stays empty until the matcher's wishlist is
typed with times; a free-text line cannot be tied to a passage without a keyword rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.services.generated_image_grounding import GENERATION_PROMPT_KEY, is_generated

if TYPE_CHECKING:
    from app.agents.overlay_placement import RawPlacement

PlacementMode = Literal["meaning", "phrase", "slideshow"]
Rule = Literal["meaning", "keyword_overlap", "slideshow", "none"]

NO_SPOKEN_MATCH = "no_spoken_match"
PAST_END_OF_TAKE = "past_end_of_take"
NOT_IN_ORDER = "not_in_order"

MIN_WINDOW_S = 0.5
PASSAGE_GAP_S = 0.7
PASSAGE_MAX_S = 8.0
_SENTENCE_END = re.compile(r"[.?!…]+[\"')\]]*$")
_MAX_REASON_CHARS = 160


@dataclass(frozen=True, slots=True)
class PassageWord:
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class Passage:
    start_s: float
    end_s: float
    words: tuple[PassageWord, ...]


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    asset_id: str
    kind: str = "image"
    origin: str = ""
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    brands: tuple[str, ...] = ()
    user_context: str = ""
    generation_prompt: str = ""
    # The pool row the matchers read (`heuristic_match` also reads the file name).
    row: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_pool_row(cls, row: Mapping[str, Any]) -> PlacementCandidate:
        analysis = row.get("analysis") or {}
        return cls(
            asset_id=str(row["id"]),
            kind="video" if row.get("kind") == "video" else "image",
            origin="generated" if is_generated(analysis) else "",
            subject=str(analysis.get("subject", "")),
            description=str(analysis.get("description", "")),
            on_screen_text=str(analysis.get("on_screen_text", "")),
            brands=tuple(str(b) for b in analysis.get("brands") or ()),
            user_context=str(row.get("user_context") or ""),
            generation_prompt=str(analysis.get(GENERATION_PROMPT_KEY) or ""),
            row=row,
        )


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    """What the creator asked for. ``slideshow`` exists only for an explicit order or pace."""

    text: str = ""
    mode: PlacementMode = "meaning"
    spoken_language: str = ""
    order: tuple[str, ...] = ()
    pace_s: float | None = None
    loop: bool = False

    def __post_init__(self) -> None:
        asked = bool(self.order) or self.pace_s is not None
        if self.mode == "slideshow" and not asked:
            raise ValueError("a slideshow needs an explicit order or pace")
        if self.mode != "slideshow" and (asked or self.loop):
            raise ValueError("order, pace and loop belong to a slideshow request")
        if self.pace_s is not None and not self.pace_s > 0:
            raise ValueError("pace_s must be positive")

    @classmethod
    def meaning(cls, text: str = "", spoken_language: str = "") -> PlacementRequest:
        return cls(text=text, spoken_language=spoken_language)

    @classmethod
    def slideshow(
        cls,
        order: Sequence[str] = (),
        pace_s: float | None = None,
        loop: bool = False,
        text: str = "",
    ) -> PlacementRequest:
        return cls(
            text=text,
            mode="slideshow",
            order=tuple(order),
            pace_s=None if pace_s is None else float(pace_s),
            loop=loop,
        )


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    passage: Passage
    asset_id: str | None
    rule: Rule
    reason: str = ""
    anchor_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlacementNeed:
    start_s: float
    end_s: float
    words: tuple[str, ...]
    description: str


@dataclass(frozen=True, slots=True)
class PlacementResult:
    decisions: tuple[PlacementDecision, ...] = ()
    needs: tuple[PlacementNeed, ...] = ()
    unused: tuple[tuple[str, str], ...] = ()
    placements: tuple[RawPlacement, ...] = ()
    wishlist: tuple[str, ...] = ()
    matcher: str = "none"

    def receipt(self, dropped: Mapping[str, str] | None = None) -> dict[str, Any]:
        """JSON-safe record for `phone_overlay_receipt`: the why per passage, without the words.

        ``dropped`` maps a picture the matcher chose to why the card never reached the screen
        (too short, overlap, no safe spot); that passage then shows the speaker.
        """
        dropped = dropped or {}
        decisions = [
            {
                "start_s": round(d.passage.start_s, 3),
                "end_s": round(d.passage.end_s, 3),
                "asset_id": None if d.asset_id in dropped else d.asset_id,
                "rule": "none" if d.asset_id in dropped else d.rule,
                "reason": (
                    f"Not shown: {dropped[d.asset_id]}."
                    if d.asset_id in dropped
                    else d.reason[:_MAX_REASON_CHARS]
                ),
                "anchor_words": [] if d.asset_id in dropped else list(d.anchor_words),
            }
            for d in self.decisions
        ]
        decided = {d.asset_id for d in self.decisions if d.asset_id in dropped}
        return {
            "decisions": decisions,
            "needs": [
                {"start_s": n.start_s, "end_s": n.end_s, "description": n.description}
                for n in self.needs
            ],
            "unused": [{"media_id": a, "reason": r} for a, r in self.unused]
            + [{"media_id": a, "reason": dropped[a]} for a in sorted(decided)],
        }


def passages_from_words(words: Iterable[Mapping[str, Any]]) -> list[Passage]:
    """Split a transcript into passages at a pause, a sentence end, or after eight seconds.

    ``words`` are ``{"word" | "text", "start_s", "end_s"}`` records in transcript order.
    """
    passages: list[Passage] = []
    current: list[PassageWord] = []

    def close() -> None:
        if current:
            passages.append(Passage(current[0].start_s, current[-1].end_s, tuple(current)))
            current.clear()

    for w in words:
        word = PassageWord(
            str(w.get("word", w.get("text", ""))), float(w["start_s"]), float(w["end_s"])
        )
        if current and (
            word.start_s - current[-1].end_s >= PASSAGE_GAP_S
            or _SENTENCE_END.search(current[-1].text)
            or word.start_s - current[0].start_s >= PASSAGE_MAX_S
        ):
            close()
        current.append(word)
    close()
    return passages


def place_passages(
    passages: Sequence[Passage],
    candidates: Sequence[PlacementCandidate],
    request: PlacementRequest,
    *,
    job_id: str,
    duration_s: float,
    occupied: Sequence[tuple[float, float]] = (),
) -> PlacementResult:
    if request.mode == "phrase":
        raise NotImplementedError("phrase placement moves in with path 4")
    if request.mode == "slideshow":
        return _place_slideshow(passages, candidates, request, duration_s)
    return _place_by_meaning(passages, candidates, request, job_id, duration_s, occupied)


def _place_by_meaning(
    passages: Sequence[Passage],
    candidates: Sequence[PlacementCandidate],
    request: PlacementRequest,
    job_id: str,
    duration_s: float,
    occupied: Sequence[tuple[float, float]],
) -> PlacementResult:
    # One stable order: the prompt, the matcher's ties and the arbitration after it must not
    # depend on which picture was uploaded first.
    ordered = sorted({c.asset_id: c for c in candidates}.values(), key=lambda c: c.asset_id)
    words = [
        {"word": w.text, "start_s": w.start_s, "end_s": w.end_s} for p in passages for w in p.words
    ]
    if not ordered or not words:
        return PlacementResult(
            decisions=tuple(_none(p) for p in passages),
            unused=tuple((c.asset_id, NO_SPOKEN_MATCH) for c in ordered),
        )
    from app.services.phone_overlay_grounding import _match_placements  # noqa: PLC0415

    raw, wishlist, matcher = _match_placements(
        job_id=job_id,
        words=words,
        candidate_assets=[dict(c.row) for c in ordered],
        duration_s=duration_s,
        occupied=[(float(s), float(e)) for s, e in occupied],
        spoken_language=request.spoken_language,
        creator_request=request.text,
    )
    rule: Rule = "meaning" if matcher == "agent" else "keyword_overlap"
    placed_on: dict[int, list[PlacementDecision]] = {}
    for p in sorted(raw, key=lambda r: r.start_s):
        idx = _passage_index(passages, p.start_s)
        placed_on.setdefault(idx, []).append(
            PlacementDecision(
                passage=passages[idx],
                asset_id=str(p.asset_id),
                rule=rule,
                reason=p.reason,
                anchor_words=tuple(p.transcript_anchor.split()),
            )
        )
    decisions = tuple(
        d for i, passage in enumerate(passages) for d in (placed_on.get(i) or [_none(passage)])
    )
    placed_ids = {str(p.asset_id) for p in raw}
    return PlacementResult(
        decisions=decisions,
        unused=tuple(
            (c.asset_id, NO_SPOKEN_MATCH) for c in ordered if c.asset_id not in placed_ids
        ),
        placements=tuple(raw),
        wishlist=tuple(wishlist),
        matcher=matcher,
    )


def _place_slideshow(
    passages: Sequence[Passage],
    candidates: Sequence[PlacementCandidate],
    request: PlacementRequest,
    duration_s: float,
) -> PlacementResult:
    known = {c.asset_id for c in candidates}
    order = (
        [a for a in request.order if a in known]
        if request.order
        else [c.asset_id for c in candidates]
    )
    if not order:
        return PlacementResult(unused=tuple((c.asset_id, NOT_IN_ORDER) for c in candidates))
    slot_s = request.pace_s or duration_s / len(order)
    words = [w for p in passages for w in p.words]
    decisions: list[PlacementDecision] = []
    start_s, idx = 0.0, 0
    while duration_s - start_s >= MIN_WINDOW_S:
        if idx == len(order):
            if not request.loop:
                break
            idx = 0
        end_s = min(start_s + slot_s, duration_s)
        if end_s - start_s < MIN_WINDOW_S:
            break
        inside = tuple(w for w in words if start_s <= w.start_s < end_s)
        decisions.append(
            PlacementDecision(
                passage=Passage(start_s, end_s, inside),
                asset_id=order[idx],
                rule="slideshow",
                reason="the creator asked for this order and pace",
            )
        )
        start_s, idx = end_s, idx + 1
    shown = {d.asset_id for d in decisions}
    unused = tuple(
        (c.asset_id, PAST_END_OF_TAKE if c.asset_id in order else NOT_IN_ORDER)
        for c in candidates
        if c.asset_id not in shown
    )
    return PlacementResult(decisions=tuple(decisions), unused=unused, matcher="slideshow")


def _none(passage: Passage) -> PlacementDecision:
    return PlacementDecision(
        passage=passage,
        asset_id=None,
        rule="none",
        reason="No picture in the pool is about this passage.",
    )


def _passage_index(passages: Sequence[Passage], start_s: float) -> int:
    """The passage a placement opens on: the last one that starts at or before it."""
    index = 0
    for i, passage in enumerate(passages):
        if passage.start_s <= start_s + 1e-6:
            index = i
    return index
