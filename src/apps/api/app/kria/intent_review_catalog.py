"""Server-built catalog of what an intent review may ask about and what could be repaired.

The model sees issued requirement aliases and frame aliases, never this catalog's candidates.
Every candidate is built from frozen inputs of the approved render only and is emitted only when
its own deterministic precondition holds: an authority other than the draft (the creator's exact
text, the rendered duration) disagrees with the draft. A candidate that restores the draft's own
value would compile to an empty diff, so it is never emitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents._schemas.intent_review import ISSUABLE_KINDS, MAX_FINDINGS, DefectKind
from app.kria.brief import BriefRequirement
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument
from app.services.creator_render_contract import CreatorRenderContract
from app.services.kria_editor_ops import is_caption_text_bar
from app.services.kria_editor_ops_text import bars_from_snapshot, classify, contains_find, fold

MIN_VISIBLE_S = 1.0
END_MARGIN_S = 0.3
MAX_LABEL_CHARS = 300
_TIMING_FACTS = frozenset(
    {"segmentation", "sequence", "timing", "overlap", "animation_phases", "duration_s"}
)
_PLACEMENT_FACTS = frozenset({"position", "below_text", "stack_id"})
_ANCHOR_AFTER_MOVE = {"bottom": "top", "top": "bottom", "middle": "top", "center": "top"}
_PIN_PREFIX = "guided-pinned-"

Role = Literal["title", "caption", "text", "pin"]
CandidateKind = Literal["restore_exact_text", "fit_text_to_duration", "move_text_to_safe_anchor"]
RequirementKind = Literal["text", "text_sequence", "select", "order", "timing", "style"]
WithheldReason = Literal["audio", "cap", "superseded", "removed", "not_possible"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrameRef(_Model):
    frame_id: str
    t_ms: int = Field(ge=0)
    sha256: str
    w: int
    h: int
    anchor_ref: str | None = None


class IssuedRequirement(_Model):
    requirement_id: str
    brief_id: str
    kind: RequirementKind
    label: str
    literal: str | None = None
    element_refs: list[str] = Field(default_factory=list)
    draft_matches_authority: bool | None = None


class WithheldRequirement(_Model):
    brief_id: str
    reason: WithheldReason


class Candidate(_Model):
    candidate_id: str
    kind: CandidateKind
    requirement_ids: list[str]
    defect_kinds: list[DefectKind]
    element_ref: str
    role: Role
    allowed_paths: list[str]
    target: dict[str, Any]
    auto: bool


class ReviewCatalog(_Model):
    digest: str
    frames: list[FrameRef]
    requirements: list[IssuedRequirement]
    withheld: list[WithheldRequirement] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)

    @property
    def requirement_ids(self) -> list[str]:
        return [item.requirement_id for item in self.requirements]

    @property
    def frame_ids(self) -> list[str]:
        return [frame.frame_id for frame in self.frames]

    def requirement(self, requirement_id: str) -> IssuedRequirement | None:
        return next((r for r in self.requirements if r.requirement_id == requirement_id), None)

    def candidate(self, candidate_id: str) -> Candidate | None:
        return next((c for c in self.candidates if c.candidate_id == candidate_id), None)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def strict_fold(value: str) -> str:
    """Casefold with collapsed whitespace. Diacritics stay: a missing Turkish dot is a drift."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def contains_exact_words(text: str, wanted: str) -> bool:
    needle = strict_fold(wanted)
    return bool(needle) and bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", strict_fold(text)))


def _seal(**parts: Any) -> ReviewCatalog:
    body = {key: [item.model_dump(mode="json") for item in value] for key, value in parts.items()}
    return ReviewCatalog(digest=_sha(body), **parts)


def _label(requirement: BriefRequirement) -> str:
    text = requirement.text().strip()
    return (text or f"{requirement.kind} ({requirement.scope})")[:MAX_LABEL_CHARS]


def _role(bar: dict[str, Any], kind: str) -> Role:
    if bar["caption"]:
        return "caption"
    if str(bar["id"]).startswith(_PIN_PREFIX):
        return "pin"
    return "title" if kind == "title" else "text"


def _issue(
    binding: BriefBinding,
) -> tuple[list[BriefRequirement], list[WithheldRequirement]]:
    """Requirements the review may judge, in priority order, plus every one it declined."""
    brief = binding.brief
    withheld: list[WithheldRequirement] = []
    eligible: list[BriefRequirement] = []
    for requirement in brief.requirements if brief else []:
        if requirement.status == "superseded":
            removed = requirement.facts.get("superseded_by") is not None
            withheld.append(
                WithheldRequirement(
                    brief_id=requirement.id, reason="removed" if removed else "superseded"
                )
            )
        elif requirement.status == "not_possible":
            withheld.append(WithheldRequirement(brief_id=requirement.id, reason="not_possible"))
        elif requirement.kind == "audio":
            withheld.append(WithheldRequirement(brief_id=requirement.id, reason="audio"))
        elif requirement.kind in ISSUABLE_KINDS:
            eligible.append(requirement)
    eligible.sort(key=lambda r: ISSUABLE_KINDS.index(r.kind))
    for requirement in eligible[MAX_FINDINGS:]:
        withheld.append(WithheldRequirement(brief_id=requirement.id, reason="cap"))
    return eligible[:MAX_FINDINGS], withheld


def _mapped(
    requirement: BriefRequirement, bars: list[dict[str, Any]], kinds: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The on-screen text rows a requirement is about, by the editor's own scope mapping.

    `title` is whatever the editor resolves as the title, `clip:<id>` is that clip's text, and
    anything else is found by the creator's words. Captions and lyric lines are never mapped.
    """
    if requirement.kind not in {"text", "text_sequence"}:
        return []
    live = [
        bar
        for bar in bars
        if not bar["removed"]
        and bar["text"].strip()
        and not bar["caption"]
        and bar["role"] != "lyric_line"
    ]
    scope = requirement.scope
    if scope == "title":
        picked = [bar for bar in live if kinds[bar["id"]] == "title"]
    elif scope.startswith("clip:"):
        picked = [bar for bar in live if bar["clip_id"] == scope.partition(":")[2]]
    else:
        picked = []
    if not picked and requirement.literal:
        picked = [bar for bar in live if contains_find(bar["text"], requirement.literal)]
    return picked


class _Guards:
    """What the approved brief forbids touching, read once from the frozen binding."""

    def __init__(self, binding: BriefBinding):
        live = binding.brief.live() if binding.brief else []
        self.frozen_all = bool(binding.protected_regions) or any(
            isinstance(r.facts.get("sequence_timing"), dict)
            and r.facts["sequence_timing"].get("mode") == "absolute"
            for r in live
        )
        self.preserve_text = any(r.facts.get("preserve_editor_text") is True for r in live)
        self.footage = any(r.kind in {"select", "order"} for r in live)
        self.placement = any(
            set(r.facts) & _PLACEMENT_FACTS for r in live if r.kind in {"text", "style"}
        )
        self.timing_scopes = {r.scope for r in live if r.kind == "timing"}

    def explicit_timing(
        self, bar: dict[str, Any], kind: str, mapped: Sequence[BriefRequirement]
    ) -> bool:
        scopes = {"global", "per_clip"}
        if kind == "title":
            scopes.add("title")
        if bar["clip_id"]:
            scopes.add(f"clip:{bar['clip_id']}")
        return bool(self.timing_scopes & scopes) or any(
            set(r.facts) & _TIMING_FACTS for r in mapped
        )


def _candidate(
    generation: str, kind: CandidateKind, element: str, target: dict[str, Any], **fields: Any
) -> Candidate:
    identity = f"{generation}|{kind}|{element}|{_sha(target)}"
    return Candidate(
        candidate_id=hashlib.sha256(identity.encode()).hexdigest()[:16],
        kind=kind,
        element_ref=element,
        target=target,
        **fields,
    )


def _exact_text_for(contract: CreatorRenderContract, requirement: BriefRequirement) -> str | None:
    if not requirement.literal:
        return None
    wanted = fold(requirement.literal)
    return next((row.text for row in contract.exact_texts if fold(row.text) == wanted), None)


def build_review_catalog(
    packet: Mapping[str, Any],
    frozen_draft: KriaDraftDocument,
    contract: CreatorRenderContract,
    binding: BriefBinding,
    frames: Sequence[FrameRef],
    rendered_duration_s: float | None,
) -> ReviewCatalog:
    requirements, withheld = _issue(binding)
    raw_rows = (
        (frozen_draft.editor_payload or {}).get("text_elements")
        if frozen_draft.kind == "editor"
        else None
    )
    rows = [row if isinstance(row, dict) else {} for row in raw_rows or []]
    bars = bars_from_snapshot({"text_bars": rows})
    for bar in bars:
        bar["caption"] = bar["caption"] or is_caption_text_bar(rows[bar["index"]])
    kinds = classify(bars)
    by_id = {bar["id"]: (bar, rows[bar["index"]]) for bar in bars}
    guards = _Guards(binding)
    generation = str(packet.get("requirement_generation") or "")

    issued: list[IssuedRequirement] = []
    mapped_by_alias: dict[str, tuple[BriefRequirement, list[dict[str, Any]]]] = {}
    for position, requirement in enumerate(requirements, start=1):
        alias = f"R{position}"
        picked = _mapped(requirement, bars, kinds)
        authority = _exact_text_for(contract, requirement) or requirement.literal
        matches: bool | None = None
        if requirement.kind == "text" and authority and picked:
            matches = any(contains_exact_words(bar["text"], authority) for bar in picked)
        mapped_by_alias[alias] = (requirement, picked)
        issued.append(
            IssuedRequirement(
                requirement_id=alias,
                brief_id=requirement.id,
                kind=requirement.kind,
                label=_label(requirement),
                literal=requirement.literal,
                element_refs=[bar["id"] for bar in picked],
                draft_matches_authority=matches,
            )
        )

    candidates: list[Candidate] = []
    if not guards.frozen_all:
        elements: dict[str, list[tuple[str, BriefRequirement]]] = {}
        for alias, (requirement, picked) in mapped_by_alias.items():
            for bar in picked:
                elements.setdefault(bar["id"], []).append((alias, requirement))
        for element_id, owners in elements.items():
            bar, row = by_id[element_id]
            kind = kinds[element_id]
            role = _role(bar, kind)
            if (
                role == "pin"
                or bar["sequence_source_id"]
                or (bar["clip_id"] and guards.footage)
                or (
                    isinstance(row.get("source_params"), dict)
                    and "sequence_timing" in row["source_params"]
                )
            ):
                continue
            path = f"editor_payload.text_elements[{bar['index']}]"
            text_owners = [(a, r) for a, r in owners if r.kind == "text"]
            targets = {
                exact
                for _, r in text_owners
                if (exact := _exact_text_for(contract, r)) is not None
                and not contains_exact_words(bar["text"], exact)
            }
            if len(targets) == 1:
                restoring = [
                    a for a, r in text_owners if (exact := _exact_text_for(contract, r)) in targets
                ]
                candidates.append(
                    _candidate(
                        generation,
                        "restore_exact_text",
                        element_id,
                        {"text": next(iter(targets))},
                        requirement_ids=restoring,
                        defect_kinds=["copy_mismatch", "text_missing"],
                        role=role,
                        allowed_paths=[f"{path}.text"],
                        auto=True,
                    )
                )
            if guards.preserve_text or guards.explicit_timing(bar, kind, [r for _, r in owners]):
                continue
            ids = [a for a, _ in owners]
            start, end = bar["start_s"], row.get("end_s")
            if (
                rendered_duration_s is not None
                and start is not None
                and isinstance(end, (int, float))
                and not isinstance(end, bool)
                and float(end) > rendered_duration_s - END_MARGIN_S
            ):
                fitted_end = round(min(float(end), rendered_duration_s - END_MARGIN_S), 3)
                if fitted_end - start >= MIN_VISIBLE_S:
                    candidates.append(
                        _candidate(
                            generation,
                            "fit_text_to_duration",
                            element_id,
                            {"start_s": round(start, 3), "end_s": fitted_end},
                            requirement_ids=ids,
                            defect_kinds=["text_cut_off", "text_missing"],
                            role=role,
                            allowed_paths=[f"{path}.start_s", f"{path}.end_s"],
                            auto=True,
                        )
                    )
            anchor = _ANCHOR_AFTER_MOVE.get(str(row.get("position") or ""))
            if anchor is not None and not guards.placement:
                candidates.append(
                    _candidate(
                        generation,
                        "move_text_to_safe_anchor",
                        element_id,
                        {"anchor": anchor},
                        requirement_ids=ids,
                        defect_kinds=["text_obstructed"],
                        role=role,
                        allowed_paths=[
                            f"{path}.position",
                            f"{path}.x_frac",
                            f"{path}.y_frac",
                        ],
                        auto=False,
                    )
                )
    return _seal(
        frames=list(frames),
        requirements=issued,
        withheld=withheld,
        candidates=sorted(candidates, key=lambda c: (c.kind, c.element_ref)),
    )
