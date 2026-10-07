"""Rewrite EVERY string a creator or model wrote in an approved plan (KRI-470 PR-D tests).

The route resolver must not branch on any of them.  ``prose`` maps each distinct original
string to a different, non-empty, deterministic string, so relationships between strings
(a shot label that equals a brief literal) survive the rewrite while the words change.
Blank strings stay blank (an empty transcript means "no speech").
"""

from __future__ import annotations

import hashlib
import typing
from typing import Any

from app.agents._schemas.creator_agent import CreativeStrategy

# CreativeStrategy fields holding words (not enumerated values, not opaque ids).
FREE_TEXT_STRATEGY_FIELDS = (
    "rationale",
    "intro_hook",
    "story_structure",
    "opening_title",
    "closing_title",
    "shot_labels",
    "font_family",
    "text_color",
)
# Opaque media identifiers: route-relevant only by presence, never by content.
IDENTIFIER_STRATEGY_FIELDS = ("hero_media_id", "selected_media_ids")
# Structured sub-models the dispatchers never read for routing (matrix: upstream_resolved /
# preference_only); their strings are not reachable from the resolver's typed projection.
STRUCTURED_STRATEGY_FIELDS = (
    "clip_intents",
    "resolved_clip_intents",
    "choice_answers",
    "reaction_beats",
    "closing_media",
    "resolved_song_takes",
    "licensed_sfx",
    "mixed_media_timing",
    "montage_audio",
    "montage_cadence",
)


def prose(value: str, salt: str = "reworded") -> str:
    if not value or not value.strip():
        return value
    digest = hashlib.sha1(value.encode()).hexdigest()[:8]  # noqa: S324 -- not security
    return f"{salt} {digest}".strip()[:190]


def _has_plain_str(annotation: Any) -> bool:
    if annotation is str:
        return True
    if typing.get_origin(annotation) is typing.Literal:
        return False
    return any(_has_plain_str(arg) for arg in typing.get_args(annotation))


def unclassified_string_fields() -> list[str]:
    """Strategy fields that can hold a free string but are not classified above."""
    known = {
        *FREE_TEXT_STRATEGY_FIELDS,
        *IDENTIFIER_STRATEGY_FIELDS,
        *STRUCTURED_STRATEGY_FIELDS,
    }
    return [
        name
        for name, field in CreativeStrategy.model_fields.items()
        if _has_plain_str(field.annotation) and name not in known
    ]


# Words the contract never projects: a variant may ADD them (a route that read them would
# diverge), unlike titles / shot labels, whose presence is itself a contract requirement.
FILLABLE_STRATEGY_FIELDS = ("rationale", "intro_hook", "story_structure")


def reword_strategy(
    strategy: dict[str, Any] | None, salt: str = "reworded"
) -> dict[str, Any] | None:
    if strategy is None:
        return None
    out = dict(strategy)
    for key in FILLABLE_STRATEGY_FIELDS:
        if not out.get(key):
            out[key] = [prose(key, salt)] if key == "story_structure" else prose(key, salt)
    for key in FREE_TEXT_STRATEGY_FIELDS:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = prose(value, salt)
        elif isinstance(value, list):
            out[key] = [prose(item, salt) if isinstance(item, str) else item for item in value]
    return out


def reword_contract(contract: Any, salt: str = "reworded") -> Any:
    """The same contract with every exact-text string and unresolved message rewritten."""
    from app.services.creator_render_contract import TextRequirement

    return contract.rebind(
        exact_texts=tuple(
            TextRequirement(**{**text.model_dump(), "text": prose(text.text, salt)})
            for text in contract.exact_texts
        ),
        unresolved=tuple(prose(item, salt) for item in contract.unresolved),
    )


def reword_job(assembly: dict[str, Any], candidates: dict[str, Any], text: str):
    """(assembly, candidates) with every carrier of words rewritten to ``text``-flavoured prose.

    Covers: the request/brief/message carriers a job persists today, the strategy's prose, the
    contract's exact texts and unresolved messages, and per-clip transcripts.
    """
    import copy

    from app.services.creator_render_contract import CONTRACT_FIELD, read_render_contract

    a, c = copy.deepcopy(assembly), copy.deepcopy(candidates)
    c["creator_request"] = text
    c["brief"] = {"creator_request": text, "description": text}
    c["creator_strategy"] = reword_strategy(c.get("creator_strategy"), text)
    a["creator_brief_binding"] = {
        **(a.get("creator_brief_binding") or {}),
        "creator_request": text,
        "latest_message": text,
    }
    rows = (a["creator_brief_binding"].get("media_snapshot") or {}).get("clip_assignments") or []
    for row in rows:
        speech = ((row.get("analysis") or {}).get("understanding") or {}).get("speech")
        if isinstance(speech, dict) and isinstance(speech.get("transcript"), str):
            speech["transcript"] = prose(speech["transcript"], text)
    contract = read_render_contract(a)
    if contract is not None:
        a[CONTRACT_FIELD] = reword_contract(contract, text).model_dump(mode="json")
    return a, c
