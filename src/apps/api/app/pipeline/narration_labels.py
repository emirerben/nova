"""Grounded text annotations for voiceover-led edits.

This module is deliberately independent of the creator planner and renderer.  A
planner or an LLM may suggest semantic annotations, but this boundary owns the
rules that make them safe to render:

* score text is copied from an exact timed-word span;
* numbers that are not a score shape are ignored;
* participant placeholders require typed single-subject evidence for the final
  shot's source asset;
* repeated appearances reuse an asset-local identity while keeping their own
  final-timeline window; and
* every accepted element carries source word, timeline, and asset provenance.

The public function accepts the small mapping-shaped records already used by
the pipeline.  It also accepts equivalent objects with attributes, which lets
the guided compiler pass its existing timeline and media-analysis objects
without coupling this helper to parent-owned schemas.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.agents._schemas.text_element import TextElement

_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "nil": 0,
    "love": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCORE_WORDS = frozenset(_NUMBER_WORDS) | {"all", "a-piece", "apiece"}
_HYBRID_SCORE_RE = re.compile(
    r"^(?P<left>\d{1,3}|[a-z]+)[-–:]\s*"
    r"(?P<right>\d{1,3}|[a-z]+)[.!?,;:!?]*$",
    re.I,
)
_PUNCTUATION_RE = re.compile(r"[^\w\s'-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class NarrationLabelRequirements:
    """Typed label intent consumed by the materializer.

    ``context_labels`` is intentionally generic.  A caller may request
    ``("sport",)`` for the current creator feature, or any other grounded
    context vocabulary in a future feature.  The materializer never contains a
    sports-only vocabulary or turns a free-text request into a requirement.
    """

    participant_labels: Literal["none", "single_subject"] = "none"
    score_labels: bool = False
    context_labels: tuple[str, ...] = ()
    intro: bool = False
    negative_kinds: frozenset[str] = frozenset()

    @classmethod
    def from_value(
        cls,
        value: NarrationLabelRequirements | Mapping[str, Any] | None,
    ) -> NarrationLabelRequirements:
        if isinstance(value, cls):
            return value
        raw = value if isinstance(value, Mapping) else {}

        participant = raw.get("participant_labels", raw.get("participant_label_mode", "none"))
        if participant is True:
            participant = "single_subject"
        participant = str(participant or "none").strip().casefold()
        if participant not in {"none", "single_subject"}:
            participant = "none"

        context: list[str] = []
        raw_context = raw.get("context_labels", raw.get("label_kinds", ()))
        if isinstance(raw_context, str):
            context.append(raw_context)
        elif isinstance(raw_context, Iterable) and not isinstance(raw_context, (bytes, Mapping)):
            context.extend(str(item) for item in raw_context)
        # The current creator contract names its context request sport_labels;
        # normalize it at this boundary while retaining a generic internal API.
        if raw.get("sport_labels") is True:
            context.append("sport")
        context = list(dict.fromkeys(item.strip() for item in context if item.strip()))

        negative_raw = raw.get("negative_kinds", raw.get("disabled_kinds", ()))
        if isinstance(negative_raw, str):
            negative = frozenset({negative_raw.strip().casefold()})
        elif isinstance(negative_raw, Iterable) and not isinstance(negative_raw, (bytes, Mapping)):
            negative = frozenset(
                str(item).strip().casefold() for item in negative_raw if str(item).strip()
            )
        else:
            negative = frozenset()
        negative = frozenset(_normalize_kind(item) for item in negative)

        return cls(
            participant_labels=("single_subject" if participant == "single_subject" else "none"),
            score_labels=bool(raw.get("score_labels", False)),
            context_labels=tuple(context),
            intro=bool(raw.get("intro", raw.get("intro_labels", False))),
            negative_kinds=negative,
        )

    def requests(self, kind: str) -> bool:
        normalized = _normalize_kind(kind)
        if normalized in self.negative_kinds or kind.casefold() in self.negative_kinds:
            return False
        if normalized == "participant":
            return self.participant_labels == "single_subject"
        if normalized == "score":
            return self.score_labels
        if normalized == "intro":
            return self.intro
        if normalized == "topic":
            return bool(self.context_labels)
        return False


@dataclass(frozen=True, slots=True)
class GroundedNarrationAnnotation:
    """An annotation after its source IDs have been validated."""

    kind: str
    text: str
    start_word_id: str | None
    end_word_id: str | None
    timeline_id: str | None = None
    asset_id: str | None = None
    source_word_ids: tuple[str, ...] = ()
    confidence: str = "medium"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RejectedNarrationAnnotation:
    kind: str
    reason: str
    start_word_id: str | None = None
    end_word_id: str | None = None
    timeline_id: str | None = None
    asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class NarrationLabelResult:
    """Materialized output plus audit information for the parent task."""

    elements: tuple[dict[str, Any], ...] = ()
    accepted: tuple[GroundedNarrationAnnotation, ...] = ()
    rejected: tuple[RejectedNarrationAnnotation, ...] = ()


@dataclass(frozen=True, slots=True)
class _Word:
    word_id: str
    text: str
    start_s: float
    end_s: float
    index: int


@dataclass(frozen=True, slots=True)
class _Timeline:
    timeline_id: str
    start_s: float
    end_s: float
    asset_id: str
    identity_key: str
    single_subject: bool | None


@dataclass(frozen=True, slots=True)
class _Analysis:
    asset_id: str
    identity_key: str
    single_subject: bool | None


def materialize_narration_labels(
    transcript: Any,
    final_timeline: Sequence[Any] | Mapping[str, Any] | None,
    media_analysis: Sequence[Any] | Mapping[str, Any] | None,
    requirements: NarrationLabelRequirements | Mapping[str, Any] | None,
    semantic_annotations: Iterable[Any] = (),
    *,
    explicit_intro_text: str | None = None,
) -> NarrationLabelResult:
    """Materialize grounded narration text on the final visual timeline.

    ``semantic_annotations`` may be empty.  In that case hybrid score tokens
    such as ``1-0`` are still safely discoverable from the transcript, while
    ambiguous spoken number pairs require an explicit semantic annotation.
    This is the key distinction between a grounded score and an incidental
    number such as ``10 minutes``.
    """

    intent = NarrationLabelRequirements.from_value(requirements)
    words = _normalize_words(transcript)
    timeline = _normalize_timeline(final_timeline)
    analyses = _normalize_analysis(media_analysis)
    if not words or not timeline:
        return NarrationLabelResult()

    words_by_id = {word.word_id: word for word in words}
    word_index = {word.word_id: word.index for word in words}
    analysis_by_identity: dict[tuple[str, str], _Analysis] = {}
    for item in analyses:
        if not item.asset_id:
            continue
        analysis_by_identity[(item.asset_id, item.identity_key)] = item
    annotations = [_normalize_annotation(item) for item in semantic_annotations]
    annotations = [item for item in annotations if item is not None]

    accepted: list[GroundedNarrationAnnotation] = []
    rejected: list[RejectedNarrationAnnotation] = []
    elements: list[dict[str, Any]] = []
    seen_element_sources: set[str] = set()

    def accept(annotation: GroundedNarrationAnnotation) -> None:
        accepted.append(annotation)

    def reject(annotation: Any, reason: str) -> None:
        rejected.append(
            RejectedNarrationAnnotation(
                kind=_normalize_kind(str(_get(annotation, "kind", "unknown"))),
                reason=reason,
                start_word_id=_optional_text(
                    _get(annotation, "start_word_id", _get(annotation, "anchor_word_id"))
                ),
                end_word_id=_optional_text(
                    _get(annotation, "end_word_id", _get(annotation, "end_anchor_word_id"))
                ),
                timeline_id=_optional_text(
                    _get(annotation, "timeline_id", _get(annotation, "shot_id"))
                ),
                asset_id=_optional_text(_get(annotation, "asset_id", _get(annotation, "media_id"))),
            )
        )

    if explicit_intro_text and intent.requests("intro"):
        text = " ".join(str(explicit_intro_text).split())[:500]
        if text:
            _append_element(
                elements,
                seen_element_sources,
                kind="intro",
                text=text,
                start_s=0.0,
                end_s=max(0.5, min(3.0, words[0].end_s + 1.0)),
                source={"source_kind": "explicit_intro"},
            )
    elif intent.requests("intro"):
        for annotation in annotations:
            if _normalize_kind(annotation.kind) != "intro" or not annotation.text:
                continue
            if annotation.start_word_id:
                bounds = _annotation_bounds(annotation, word_index, len(words))
                if bounds is None:
                    reject(annotation, "intro annotation has unknown word anchors")
                    continue
                start_index, end_index = bounds
                start_word = words[start_index]
                end_word = words[end_index]
                source_words = tuple(word.word_id for word in words[start_index : end_index + 1])
                start_s = start_word.start_s
                end_s = max(end_word.end_s, start_s + 0.5)
            else:
                source_words = ()
                start_s = 0.0
                end_s = max(0.5, min(3.0, words[0].end_s + 1.0))
            grounded = GroundedNarrationAnnotation(
                kind="intro",
                text=" ".join(annotation.text.split())[:500],
                start_word_id=annotation.start_word_id,
                end_word_id=annotation.end_word_id,
                source_word_ids=source_words,
                confidence=annotation.confidence,
                reason=annotation.reason,
            )
            accept(grounded)
            _append_element(
                elements,
                seen_element_sources,
                kind="intro",
                text=grounded.text,
                start_s=start_s,
                end_s=end_s,
                source={
                    "source_kind": "narration_annotation",
                    "source_word_ids": list(source_words),
                },
            )

    # Hybrid score tokens are unambiguous enough to discover without an LLM.
    # Spoken pairs are intentionally discovered only inside an agent annotation
    # (or a caller-provided typed annotation), where its exact span supplies the
    # semantic grounding that a bare number pair lacks.
    score_candidates: list[GroundedNarrationAnnotation] = []
    if intent.requests("score"):
        for word in words:
            if _is_hybrid_score(word.text):
                score_candidates.append(
                    GroundedNarrationAnnotation(
                        kind="score",
                        text=word.text.strip(),
                        start_word_id=word.word_id,
                        end_word_id=word.word_id,
                        source_word_ids=(word.word_id,),
                        confidence="high",
                        reason="transcript_hybrid_score",
                    )
                )
        for annotation in annotations:
            if _normalize_kind(annotation.kind) != "score":
                continue
            candidate = _ground_score_annotation(annotation, words, word_index)
            if candidate is None:
                reject(annotation, "annotation does not contain an exact score phrase")
            else:
                score_candidates.append(candidate)

        score_seen: set[tuple[str, str]] = set()
        for candidate in score_candidates:
            key = (candidate.start_word_id or "", candidate.end_word_id or "")
            if key in score_seen:
                continue
            score_seen.add(key)
            start = words_by_id[candidate.start_word_id or ""]
            end = words_by_id[candidate.end_word_id or ""]
            grounded = GroundedNarrationAnnotation(
                kind=candidate.kind,
                text=_words_text(words, start.index, end.index),
                start_word_id=candidate.start_word_id,
                end_word_id=candidate.end_word_id,
                timeline_id=candidate.timeline_id,
                asset_id=candidate.asset_id,
                source_word_ids=candidate.source_word_ids,
                confidence=candidate.confidence,
                reason=candidate.reason,
            )
            accept(grounded)
            _append_element(
                elements,
                seen_element_sources,
                kind="score",
                text=grounded.text,
                start_s=start.start_s,
                end_s=max(end.end_s, start.start_s + 0.5),
                source={
                    "source_kind": "narration_annotation",
                    "transcript_grounded": True,
                    "source_word_ids": list(grounded.source_word_ids),
                },
            )

    if intent.requests("topic"):
        for annotation in annotations:
            if _normalize_kind(annotation.kind) != "topic":
                continue
            candidate = _ground_topic_annotation(annotation, words, word_index)
            if candidate is None:
                reject(annotation, "topic text is not copied from its transcript anchor")
                continue
            start = words_by_id[candidate.start_word_id or ""]
            end = words_by_id[candidate.end_word_id or ""]
            accept(candidate)
            _append_element(
                elements,
                seen_element_sources,
                kind="topic",
                text=candidate.text,
                start_s=start.start_s,
                end_s=max(end.end_s, start.start_s + 0.5),
                source={
                    "source_kind": "narration_annotation",
                    "transcript_grounded": True,
                    "source_word_ids": list(candidate.source_word_ids),
                },
            )

    # Build the asset-local ordinal from canonical analysis order.  The same
    # source asset gets the same placeholder on repeated timeline appearances;
    # no subject description or visual similarity ever joins two assets.
    identity_order: list[str] = []
    for item in analyses:
        if item.identity_key and item.identity_key not in identity_order:
            identity_order.append(item.identity_key)
    for row in timeline:
        if row.identity_key and row.identity_key not in identity_order:
            identity_order.append(row.identity_key)
    player_number = {identity: index + 1 for index, identity in enumerate(identity_order)}

    if intent.requests("participant"):
        for row in timeline:
            analysis = analysis_by_identity.get((row.asset_id, row.identity_key))
            if analysis is None and row.identity_key == row.asset_id:
                # An old timeline may omit source_instance_id.  Fall back only
                # when the asset has one unambiguous analysis record; otherwise
                # guessing would merge source-local identities.
                candidates = [item for item in analyses if item.asset_id == row.asset_id]
                analysis = candidates[0] if len(candidates) == 1 else None
            eligible = (
                row.single_subject
                if row.single_subject is not None
                else (analysis.single_subject if analysis is not None else None)
            )
            if eligible is not True:
                # False means known multi-subject; None means the analysis did
                # not make the required typed claim. Both are label-free.
                rejected.append(
                    RejectedNarrationAnnotation(
                        kind="participant",
                        reason=(
                            "typed single-subject evidence unavailable"
                            if eligible is None
                            else "shot is typed as multi-subject"
                        ),
                        timeline_id=row.timeline_id,
                        asset_id=row.asset_id,
                    )
                )
                continue
            number = player_number.get(row.identity_key)
            if number is None:
                continue
            text = f"PLAYER {number}"
            participant = GroundedNarrationAnnotation(
                kind="participant",
                text=text,
                start_word_id=None,
                end_word_id=None,
                timeline_id=row.timeline_id,
                asset_id=row.asset_id,
                confidence="high",
                reason="typed_single_subject_asset_analysis",
            )
            accept(participant)
            _append_element(
                elements,
                seen_element_sources,
                kind="participant",
                text=text,
                start_s=row.start_s,
                end_s=row.end_s,
                source={
                    "source_kind": "typed_media_analysis",
                    "editable_placeholder": True,
                    "participant_key": f"asset:{row.identity_key}",
                    "source_asset_id": row.asset_id,
                    "source_timeline_id": row.timeline_id,
                },
            )

    return NarrationLabelResult(
        elements=tuple(
            sorted(
                elements,
                key=lambda item: (
                    float(item.get("start_s", 0.0)),
                    float(item.get("end_s", 0.0)),
                    str(item.get("id", "")),
                ),
            )
        ),
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def _append_element(
    elements: list[dict[str, Any]],
    seen: set[str],
    *,
    kind: str,
    text: str,
    start_s: float,
    end_s: float,
    source: dict[str, Any],
) -> None:
    if not text or not math.isfinite(start_s) or not math.isfinite(end_s) or end_s <= start_s:
        return
    source_key = hashlib.sha256(
        (kind + ":" + repr(sorted(source.items())) + f":{start_s:.6f}:{end_s:.6f}").encode()
    ).hexdigest()[:32]
    if source_key in seen:
        return
    seen.add(source_key)
    role = "generative_intro" if kind == "intro" else "generative_sequence"
    if kind == "participant":
        position = "custom"
        x_frac = 0.08
        y_frac = 0.76
        alignment = "left"
        effect = "static"
    elif kind == "score":
        position = "custom"
        x_frac = 0.92
        y_frac = 0.12
        alignment = "right"
        effect = "pop-in"
    elif kind == "topic":
        position = "custom"
        x_frac = 0.08
        y_frac = 0.12
        alignment = "left"
        effect = "pop-in"
    else:
        position = "top"
        x_frac = None
        y_frac = None
        alignment = "center"
        effect = "fade-in"
    element = TextElement(
        id=source_key,
        text=text,
        start_s=max(0.0, start_s),
        end_s=end_s,
        role=role,
        position=position,
        x_frac=x_frac,
        y_frac=y_frac,
        size_class="large" if kind in {"intro", "score"} else "small",
        alignment=alignment,
        effect=effect,
        source_params={"narration_label_kind": kind, **source},
    )
    elements.append(element.model_dump(mode="json", exclude_none=True))


def _ground_score_annotation(
    annotation: GroundedNarrationAnnotation,
    words: list[_Word],
    word_index: Mapping[str, int],
) -> GroundedNarrationAnnotation | None:
    bounds = _annotation_bounds(annotation, word_index, len(words))
    if bounds is None:
        return None
    start_index, end_index = bounds
    # Prefer the smallest exact hybrid token inside a broad model anchor.
    for index in range(start_index, end_index + 1):
        if _is_hybrid_score(words[index].text):
            word = words[index]
            return GroundedNarrationAnnotation(
                kind="score",
                text=word.text.strip(),
                start_word_id=word.word_id,
                end_word_id=word.word_id,
                source_word_ids=(word.word_id,),
                confidence=annotation.confidence,
                reason=annotation.reason,
            )
    for candidate_start in range(start_index, end_index + 1):
        for candidate_end in range(candidate_start, min(end_index, candidate_start + 3) + 1):
            if _is_spoken_score_span(words, candidate_start, candidate_end):
                return GroundedNarrationAnnotation(
                    kind="score",
                    text=_words_text(words, candidate_start, candidate_end),
                    start_word_id=words[candidate_start].word_id,
                    end_word_id=words[candidate_end].word_id,
                    source_word_ids=tuple(
                        word.word_id for word in words[candidate_start : candidate_end + 1]
                    ),
                    confidence=annotation.confidence,
                    reason=annotation.reason,
                )
    return None


def _ground_topic_annotation(
    annotation: GroundedNarrationAnnotation,
    words: list[_Word],
    word_index: Mapping[str, int],
) -> GroundedNarrationAnnotation | None:
    bounds = _annotation_bounds(annotation, word_index, len(words))
    text_tokens = _normalized_tokens(annotation.text)
    if bounds is None or not text_tokens:
        return None
    start_index, end_index = bounds
    for index in range(start_index, end_index - len(text_tokens) + 2):
        candidate = words[index : index + len(text_tokens)]
        if [_normalized_token(word.text) for word in candidate] == text_tokens:
            return GroundedNarrationAnnotation(
                kind="topic",
                text=_words_text(words, index, index + len(text_tokens) - 1),
                start_word_id=words[index].word_id,
                end_word_id=words[index + len(text_tokens) - 1].word_id,
                source_word_ids=tuple(word.word_id for word in candidate),
                confidence=annotation.confidence,
                reason=annotation.reason,
            )
    return None


def _annotation_bounds(
    annotation: GroundedNarrationAnnotation,
    word_index: Mapping[str, int],
    word_count: int,
) -> tuple[int, int] | None:
    if not annotation.start_word_id or annotation.start_word_id not in word_index:
        return None
    end_id = annotation.end_word_id or annotation.start_word_id
    if end_id not in word_index:
        return None
    start_index = word_index[annotation.start_word_id]
    end_index = word_index[end_id]
    if start_index > end_index or end_index >= word_count or end_index - start_index > 12:
        return None
    return start_index, end_index


def _is_spoken_score_span(words: list[_Word], start: int, end: int) -> bool:
    tokens = [_score_value(word.text) for word in words[start : end + 1]]
    if end - start == 1:
        return tokens[0] is not None and tokens[1] is not None
    if end - start == 2:
        return (
            tokens[0] is not None
            and _normalized_token(words[start + 1].text) == "to"
            and tokens[2] is not None
        )
    return False


def _is_hybrid_score(text: Any) -> bool:
    match = _HYBRID_SCORE_RE.fullmatch(_normalized_token(str(text)))
    if match is None:
        return False
    left = _score_value(match.group("left"))
    right = _score_value(match.group("right"))
    return (
        left is not None
        and right is not None
        and (str(match.group("left")).isdigit() or str(match.group("right")).isdigit())
    )


def _score_value(text: Any) -> int | None:
    token = _normalized_token(str(text))
    if token.isdigit() and len(token) <= 3:
        return int(token)
    if token in _SCORE_WORDS:
        return _NUMBER_WORDS.get(token, 0)
    return None


def _normalize_words(transcript: Any) -> list[_Word]:
    raw_words = _get(transcript, "words", transcript if isinstance(transcript, Sequence) else ())
    if isinstance(raw_words, (str, bytes)) or not isinstance(raw_words, Iterable):
        return []
    words: list[_Word] = []
    for index, raw in enumerate(raw_words):
        text = str(_get(raw, "text", _get(raw, "word", "")) or "").strip()
        if not text:
            continue
        word_id = _optional_text(_get(raw, "word_id", _get(raw, "id"))) or f"w{index:06d}"
        try:
            start_s = float(_get(raw, "start_s", _get(raw, "start", 0.0)) or 0.0)
            end_s = float(_get(raw, "end_s", _get(raw, "end", start_s)) or start_s)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s < start_s:
            continue
        if any(item.word_id == word_id for item in words):
            word_id = f"w{index:06d}"
        words.append(_Word(word_id, text, max(0.0, start_s), max(start_s, end_s), len(words)))
    return words


def _normalize_timeline(value: Sequence[Any] | Mapping[str, Any] | None) -> list[_Timeline]:
    rows = _records(value, ("story_timeline", "timeline", "shots", "items"))
    out: list[_Timeline] = []
    for index, raw in enumerate(rows):
        timeline_id = (
            _optional_text(
                _get(
                    raw, "timeline_id", _get(raw, "shot_id", _get(raw, "step_id", _get(raw, "id")))
                )
            )
            or f"timeline_{index}"
        )
        asset_id = _optional_text(
            _get(
                raw,
                "asset_id",
                _get(raw, "media_id", _get(raw, "clip_id", _get(raw, "source_asset_id"))),
            )
        )
        if not asset_id:
            continue
        identity_key = _optional_text(_get(raw, "source_instance_id")) or asset_id
        try:
            start_s = float(_get(raw, "start_s", _get(raw, "output_start_s", 0.0)) or 0.0)
            end_s = float(_get(raw, "end_s", _get(raw, "output_end_s", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s <= start_s:
            continue
        single = _typed_single_subject(raw)
        out.append(_Timeline(timeline_id, max(0.0, start_s), end_s, asset_id, identity_key, single))
    return out


def _normalize_analysis(value: Sequence[Any] | Mapping[str, Any] | None) -> list[_Analysis]:
    rows = _records(value, ("assets", "media_analysis", "clip_metas", "items"))
    out: list[_Analysis] = []
    for raw in rows:
        asset_id = _optional_text(
            _get(raw, "asset_id", _get(raw, "media_id", _get(raw, "clip_id", _get(raw, "id"))))
        )
        if not asset_id:
            continue
        identity_key = _optional_text(_get(raw, "source_instance_id")) or asset_id
        out.append(_Analysis(asset_id, identity_key, _typed_single_subject(raw)))
    return out


def _typed_single_subject(raw: Any) -> bool | None:
    """Read only explicit typed subject-focus evidence.

    Free-form fields such as ``detected_subject='young man'`` are deliberately
    ignored.  They are useful creative descriptions but do not establish that
    the final shot visibly focuses on exactly one participant.
    """

    # MediaRef.analysis is currently flattened by the caller, but accepting a
    # nested ``analysis`` object keeps this boundary compatible with callers
    # that pass the schema object directly.  Both paths read the same small
    # typed vocabulary; neither path inspects prose such as subject/description.
    sources: list[Any] = [raw]
    nested = _get(raw, "analysis", None)
    if isinstance(nested, Mapping):
        sources.append(nested)
    for source in sources:
        if bool(_get(source, "analysis_degraded", False)):
            continue
        explicit = _get(source, "single_subject", None)
        if isinstance(explicit, bool):
            return explicit
        focus = _get(
            source,
            "subject_focus",
            _get(
                source,
                "participant_focus",
                _get(source, "focus_mode", _get(source, "focus", None)),
            ),
        )
        if isinstance(focus, str):
            normalized = focus.strip().casefold()
            if normalized in {"single", "single_subject", "one", "one_subject"}:
                return True
            if normalized in {"multiple", "group", "multi_subject", "many", "none"}:
                return False
        for key in (
            "primary_subject_count",
            "visible_subject_count",
            "subject_count",
            "participant_count",
        ):
            count = _get(source, key, None)
            if isinstance(count, bool):
                continue
            try:
                if count is not None:
                    count_int = int(count)
                    if count_int == 1:
                        return True
                    if count_int > 1:
                        return False
            except (TypeError, ValueError):
                continue
    return None


def _normalize_annotation(raw: Any) -> GroundedNarrationAnnotation | None:
    kind = _normalize_kind(str(_get(raw, "kind", "")))
    if kind not in {"intro", "participant", "score", "topic"}:
        return None
    return GroundedNarrationAnnotation(
        kind=kind,
        text=str(_get(raw, "text", _get(raw, "label", "")) or "").strip(),
        start_word_id=_optional_text(_get(raw, "start_word_id", _get(raw, "anchor_word_id"))),
        end_word_id=_optional_text(_get(raw, "end_word_id", _get(raw, "end_anchor_word_id"))),
        timeline_id=_optional_text(_get(raw, "timeline_id", _get(raw, "shot_id"))),
        asset_id=_optional_text(_get(raw, "asset_id", _get(raw, "media_id"))),
        confidence=str(_get(raw, "confidence", "medium") or "medium"),
        reason=str(_get(raw, "reason", "") or "")[:240],
    )


def _normalize_kind(kind: str) -> str:
    normalized = kind.strip().casefold().replace("-", "_")
    return {
        "player": "participant",
        "players": "participant",
        "sport": "topic",
        "context": "topic",
        "context_label": "topic",
    }.get(normalized, normalized)


def _records(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, Iterable) and not isinstance(nested, (str, bytes, Mapping)):
                return list(nested)
        # A mapping keyed by source ID is a useful media-analysis shape.
        if value and all(isinstance(item, Mapping) for item in value.values()):
            rows = []
            for key, item in value.items():
                row = dict(item)
                row.setdefault("asset_id", str(key))
                rows.append(row)
            return rows
        return []
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalized_token(text: str) -> str:
    return _PUNCTUATION_RE.sub("", text.casefold()).strip()


def _normalized_tokens(text: str) -> list[str]:
    return [_normalized_token(token) for token in text.split() if _normalized_token(token)]


def _words_text(words: list[_Word], start: int, end: int) -> str:
    return " ".join(word.text for word in words[start : end + 1]).strip()
