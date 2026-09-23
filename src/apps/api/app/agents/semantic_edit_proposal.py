"""Semantic-only edit proposal agent.

This pass chooses the editorial grouping and source intent.  The scheduler owns
all timestamps, durations, source windows, and frame arithmetic.
"""

from __future__ import annotations

import json
import math
import re
from typing import ClassVar

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.edit_proposal import (
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
    _media_prompt_dict,
    _required_media_ids,
)
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import creator_copy_match_key
from app.schemas.semantic_edit import (
    SemanticChapter,
    SemanticEditPlan,
    SemanticSource,
    SemanticTextBinding,
)

_QUOTED_TEXT_RE = re.compile(
    r"[\"\u201c\u201d]([^\"\u201c\u201d]{1,120})[\"\u201c\u201d]|(?<!\w)'([^']{1,120})'(?!\w)"
)
_SPEECH_ATTRIBUTION_RE = re.compile(
    r"\b(?:said|says|wrote|writes|replied|replies|remarked|remarks|stated|states|asked|asks"
    r"|told\s+(?:me|us|them|him|her))\s*[:,]?\s*$",
    re.IGNORECASE,
)
_DISPLAY_COPY_CUE_RE = re.compile(
    r"\b(?:captions?|subtitles?|overlays?|labels?|titles?|show|display)\b"
    r"|\bon[\s-]?screen\b",
    re.IGNORECASE,
)
_DISPLAY_QUOTE_TARGET = (
    r"(?:on[\s-]?screen\b"
    r"|(?:as|in|for)\s+(?:(?:a|an|the)\s+)?(?:caption|subtitle|overlay|label|title)\b)"
)
_QUOTE_MEDIA_TARGET = (
    r"on\s+(?:(?:a|an|the)\s+)?(?:(?:opening|first|last|closing)\s+)?"
    r"(?:clip|shot|image|frame|video)\b"
)
_QUOTE_PLACEMENT_RE = re.compile(
    r"\b(?:write|print|render|put|add|use)\s+"
    r"(?:(?:the|these|those|his|her|their|a|an|this|that)\s+)?(?:words?|quotes?|text|lines?)\b",
    re.IGNORECASE,
)
_DISPLAY_QUOTE_SUFFIX_RE = re.compile(
    rf"^\s*(?:[,;:\u2014-]\s*)?(?:{_DISPLAY_QUOTE_TARGET}"
    r"|(?:put|show|display|use)\s+(?:that|this|those|these)\s+"
    rf"(?:quotes?|text|words?|lines?|captions?)\s+(?:{_DISPLAY_QUOTE_TARGET}|{_QUOTE_MEDIA_TARGET})"
    rf"|(?:put|show|display|use)\s+(?:that|this|it)\s+{_DISPLAY_QUOTE_TARGET})",
    re.IGNORECASE,
)
# The voiceover-script exclusion must never swallow explicit on-screen copy.
# A short quote is far likelier a label than a spoken sentence, and a
# placement verb before the quote or a where-to-show phrase after it keeps
# it as creator copy even when the voiceover also says those words.
_SPOKEN_SCRIPT_MIN_WORDS = 4
_SPOKEN_QUOTE_PLACEMENT_RE = re.compile(
    r"\b(?:put|add|write|place|overlay|type|print|text)\b", re.IGNORECASE
)
_SPOKEN_QUOTE_WHERE_RE = re.compile(
    r"\s*(?:[,;:—-]\s*)?(?:as\s+(?:on[\s-]?screen\s+)?text\b"
    r"|(?:on|over|across)\s+(?:(?:the|this|that|each|every|a|an)\s+)?(?:[\w-]+\s+){0,3}"
    r"(?:clips?|shots?|photos?|images?|pictures?|videos?|frames?|chapters?|screen)\b)",
    re.IGNORECASE,
)
_QUOTE_LIST_JOIN_RE = re.compile(r"\s*,?\s*(?:(?:and|or|&)\s*)?", re.IGNORECASE)
_FORBIDDEN_TIMING_KEYS = frozenset(
    {"duration_s", "start_s", "end_s", "source_start_s", "source_end_s", "output_duration_s"}
)
_GROUP_SPLIT_ERROR = "semantic_edit_proposal: clip intent group was split"
_GROUP_UNRELATED_ERROR = "semantic_edit_proposal: clip intent group has unrelated sources"
_GROUP_RETRY_HINT = (
    "Correction: every resolved GROUP or CAPTION is an exclusive contiguous block. Use exactly its "
    "assigned aliases in its block; keep every other alias outside that block, even when "
    "the setting is similar."
)
_CAPTION_RETRY_HINT = (
    "Correction: each quoted caption must be the thought of its own chapter, and that "
    "chapter must contain the caption's source, outside any resolved GROUP or CAPTION block."
)


def _unknown_media_retry_hint(media_count: int) -> str:
    """Server-derived alias range only; never echoes the rejected output."""
    last = f"m{media_count:03d}"
    return (
        f"Correction: the only valid media aliases are m001 through {last} "
        f"({media_count} sources, numbered from m001; there is no m000). Every "
        "chapters[].sources[].media_id must be one of them. Never invent an alias "
        "or add a chapter without a real source."
    )


def _validate_reuse_once(plan: SemanticEditPlan, input: EditProposalAgentInput) -> None:  # noqa: A002
    """Reject duplicate video choices before the scheduler has to repair them."""
    if input.video_reuse_policy != "once":
        return
    video_ids = {media.media_id for media in input.media if media.kind == "video"}
    seen: set[str] = set()
    for chapter in plan.chapters:
        for source in chapter.sources:
            if source.media_id in video_ids and source.media_id in seen:
                raise SchemaError(
                    "semantic_edit_proposal: a video appears more than once under once reuse"
                )
            seen.add(source.media_id)


def _canonicalize_text_bindings(
    plan: SemanticEditPlan, input: EditProposalAgentInput, repairs: list[str]
) -> None:  # noqa: A002
    """Keep server-owned and unrequested copy out of the generic text lane."""
    server_owned = {text for text in (input.opening_title, input.closing_title) if text}
    server_owned_keys = {creator_copy_match_key(text) for text in server_owned}
    if input.direction != "fast_montage":
        server_owned.update(input.shot_labels or [])
        server_owned_keys.update(creator_copy_match_key(label) for label in input.shot_labels or [])
    label_keys = _resolved_label_keys(input)
    label_texts = _resolved_label_texts(input)
    captions = _creator_captions(input)
    caption_texts = _caption_texts(captions)
    retained: list[SemanticTextBinding] = []
    for index, binding in enumerate(plan.text_bindings):
        key = creator_copy_match_key(binding.text)
        if binding.text in label_texts or (key in label_keys and binding.text not in caption_texts):
            repairs.append(f"dropped_grounded_label_text_binding:{index}")
        elif binding.text in server_owned or (key in server_owned_keys and key not in captions):
            repairs.append(f"dropped_server_owned_text_binding:{index}")
        elif captions and key not in captions:
            repairs.append(f"dropped_unrequested_text_binding:{index}")
        else:
            exact_creator_copy = (
                _resolve_creator_caption(binding.text, captions) if captions else None
            )
            if captions and key in captions and exact_creator_copy is None:
                raise SchemaError("semantic_edit_proposal: creator caption variants are ambiguous")
            if exact_creator_copy and binding.text != exact_creator_copy:
                binding = binding.model_copy(update={"text": exact_creator_copy})
                repairs.append(f"canonicalized_creator_caption_binding:{index}")
            retained.append(binding)
    plan.text_bindings = retained


def _semantic_prompt_media(
    input: EditProposalAgentInput,  # noqa: A002
) -> tuple[list[EditProposalMedia], dict[str, str], dict[str, str]]:
    """Alias the full semantic manifest in stable input order.

    The legacy proposal prompt uses a 32-source shortlist.  A semantic plan
    can be rejected for omitting a resolved intent or required source, so its
    model must see every source that the preflight/scheduler may require.
    """
    alias_to_id = {f"m{index + 1:03d}": media.media_id for index, media in enumerate(input.media)}
    id_to_alias = {media_id: alias for alias, media_id in alias_to_id.items()}
    return (
        [
            media.model_copy(update={"media_id": id_to_alias[media.media_id]})
            for media in input.media
        ],
        alias_to_id,
        id_to_alias,
    )


def _semantic_required_media_ids(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    """Include every source a semantic constraint can make mandatory."""
    required = set(_required_media_ids(input))
    if input.montage_audio:
        required.update(input.montage_audio.source_media_ids)
    if input.montage_cadence:
        required.update(input.montage_cadence.source_media_ids)
    for intent in input.clip_intents or []:
        if intent.status == "resolved":
            required.update(assignment.media_id for assignment in intent.assignments)
    return required


def _is_reported_speech_quote(request: str, match: re.Match[str]) -> bool:
    """Do not turn attributed story dialogue into a required text overlay.

    Keep the quote fallback for unclassified copy, including non-English
    captions. Only an adjacent speech attribution establishes this exclusion;
    an explicit display instruction attached to that quote overrides it.
    """
    prefix = _quote_clause_prefix(request, match)
    if not _SPEECH_ATTRIBUTION_RE.search(prefix):
        return False
    return not _quote_has_display_instruction(request, match, prefix)


def _quote_clause_prefix(request: str, match: re.Match[str]) -> str:
    before_quote = request[: match.start()].rstrip().removesuffix(",")
    return re.split(r"[.!?\n,;]|\b(?:then|and|but)\b", before_quote, flags=re.I)[-1]


def _quote_has_display_instruction(request: str, match: re.Match[str], prefix: str) -> bool:
    # A period can sit inside the quoted speech. Do not borrow an instruction
    # from the next sentence (e.g. 'He said "... ." Put "real copy" on screen').
    # Only a postfix that directly assigns this quote to a display lane counts.
    return bool(
        _DISPLAY_COPY_CUE_RE.search(prefix)
        or _DISPLAY_QUOTE_SUFFIX_RE.search(request[match.end() :])
        or (
            _QUOTE_PLACEMENT_RE.search(prefix)
            and re.match(rf"\s*{_QUOTE_MEDIA_TARGET}", request[match.end() :], re.I)
        )
    )


def _quote_list_ends(request: str, match: re.Match[str]) -> tuple[re.Match[str], re.Match[str]]:
    """First and last quote of a list such as 'Label them "A", "B" and "C"'.

    A display cue before the list or a placement after it covers every item,
    not only the adjacent one.
    """
    quotes = list(_QUOTED_TEXT_RE.finditer(request))
    at = next(index for index, quote in enumerate(quotes) if quote.start() == match.start())
    first = last = at
    while first > 0 and _QUOTE_LIST_JOIN_RE.fullmatch(
        request[quotes[first - 1].end() : quotes[first].start()]
    ):
        first -= 1
    while last + 1 < len(quotes) and _QUOTE_LIST_JOIN_RE.fullmatch(
        request[quotes[last].end() : quotes[last + 1].start()]
    ):
        last += 1
    return quotes[first], quotes[last]


def _is_spoken_script_quote(request: str, match: re.Match[str], spoken: list[str]) -> bool:
    """A quoted sentence the recorded voiceover speaks is its script, not a text overlay.

    Timed narration captions already draw those words, whether the attribution
    follows the quote ('"...," she said.') or is absent. A short phrase, a
    placement verb ('Put "..."'), a where-to-show phrase ('"..." over the
    photo') or any other display instruction keeps the quote as creator copy.
    """
    text = match.group(1) or match.group(2) or ""
    words = [key for key in (creator_copy_match_key(word) for word in text.split()) if key]
    if len(words) < _SPOKEN_SCRIPT_MIN_WORDS or not any(
        spoken[start : start + len(words)] == words for start in range(len(spoken) - len(words) + 1)
    ):
        return False
    head, tail = _quote_list_ends(request, match)
    prefix = _quote_clause_prefix(request, head)
    if _SPOKEN_QUOTE_PLACEMENT_RE.search(prefix) or _SPOKEN_QUOTE_WHERE_RE.match(
        request[tail.end() :]
    ):
        return False
    return not _quote_has_display_instruction(request, tail, prefix)


def _creator_captions(input: EditProposalAgentInput) -> dict[str, list[str]]:  # noqa: A002
    """Creator-quoted captions are a complete allowlist (KRI-129)."""
    if input.shot_labels:
        return {}
    resolved = [
        intent.caption_text
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "caption" and intent.caption_text
    ]
    if resolved:
        resolved.extend(
            intent.creator_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "group" and intent.creator_text
        )
        phrases: dict[str, list[str]] = {}
        for text in resolved:
            key = creator_copy_match_key(text)
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
        return phrases
    phrases: dict[str, list[str]] = {}
    words = (str(word.get("text") or "") for word in input.narration_words)
    spoken = [key for key in map(creator_copy_match_key, words) if key]
    for match in _QUOTED_TEXT_RE.finditer(input.creator_request):
        if _is_reported_speech_quote(input.creator_request, match):
            continue
        if spoken and _is_spoken_script_quote(input.creator_request, match, spoken):
            continue
        text = (match.group(1) or match.group(2) or "").strip()
        key = creator_copy_match_key(text)
        if key:
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
    for intent in input.clip_intents or []:
        if intent.status != "resolved":
            continue
        text = (
            intent.caption_text
            if intent.op == "caption"
            else intent.creator_text
            if intent.op == "group"
            else None
        )
        if text:
            key = creator_copy_match_key(text)
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
    for title in (input.opening_title, input.closing_title):
        if title:
            key = creator_copy_match_key(title)
            phrases[key] = [text for text in phrases.get(key, []) if text != title]
            if not phrases[key]:
                phrases.pop(key)
    return phrases


def _resolve_creator_caption(text: str, captions: dict[str, list[str]]) -> str | None:
    """Return exact creator copy, or None when normalized variants are ambiguous."""
    variants = captions.get(creator_copy_match_key(text), [])
    if text in variants:
        return text
    return variants[0] if len(variants) == 1 else None


def _caption_texts(captions: dict[str, list[str]]) -> set[str]:
    return {text for variants in captions.values() for text in variants}


def _resolved_label_keys(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    """Exact copy already owned by the worker's grounded-label render lane."""
    return {
        creator_copy_match_key(assignment.value)
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "label"
        for assignment in intent.assignments
        if assignment.value
    }


def _resolved_label_texts(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    return {
        assignment.value
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "label"
        for assignment in intent.assignments
        if assignment.value
    }


def _blank_grounded_label_thoughts(
    plan: SemanticEditPlan, input: EditProposalAgentInput, repairs: list[str]
) -> None:
    """Never let a model duplicate a resolved label through the generic text lane."""
    if input.shot_labels:
        return
    label_keys = _resolved_label_keys(input)
    label_texts = _resolved_label_texts(input)
    caption_texts = _caption_texts(_creator_captions(input))
    for index, chapter in enumerate(plan.chapters):
        if chapter.thought in label_texts or (
            creator_copy_match_key(chapter.thought) in label_keys
            and chapter.thought not in caption_texts
        ):
            chapter.thought = ""
            repairs.append(f"blanked_grounded_label_thought:{index}")


def _restore_dropped_creator_captions(
    plan: SemanticEditPlan,
    input: EditProposalAgentInput,  # noqa: A002
    dropped: list[tuple[int, dict]],
    alias_to_id: dict[str, str],
    allowed: set[str],
    repairs: list[str],
) -> set[str]:
    """Keep allowlisted creator copy from a dropped non-montage binding renderable.

    Guided stories render chapter thoughts, not montage bindings. Move exact
    creator copy to the first empty thought among its valid targets so the
    allowlist check below validates text that will actually be drawn.

    Returns the captions proven unplaceable: bound to real chapters, but each
    one already shows creator copy and cannot split, or is owned by a resolved
    group/caption intent. A retry cannot fix that (e.g. two captions on one
    shot under once reuse), so the story renders without that caption and
    records ``unplaceable_creator_caption:<binding index>``.
    """
    if input.shot_labels or not dropped:
        return set()
    captions = _creator_captions(input)
    if not captions:
        return set()
    # Resolved caption/group copy is placed by _apply_resolved_caption_intents
    # and resolved labels by the grounded label lane. Moving either here would
    # duplicate it.
    server_placed = _resolved_label_texts(input) | {
        intent.caption_text if intent.op == "caption" else intent.creator_text
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op in {"caption", "group"}
    }
    # Those intents also own the thought of every chapter holding their
    # sources, so a moved caption would be overwritten there.
    server_media = {
        alias_to_id.get(assignment.media_id, assignment.media_id)
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op in {"caption", "group"}
        for assignment in intent.assignments
    }
    unplaceable: dict[str, int] = {}
    for binding_index, raw in dropped:
        exact = _resolve_creator_caption(raw["text"].strip(), captions)
        if (
            exact is None
            or exact in server_placed
            or any(chapter.thought == exact for chapter in plan.chapters)
        ):
            continue
        index_by_id = {chapter.chapter_id: i for i, chapter in enumerate(plan.chapters)}
        chapter_ids = raw.get("chapter_ids") if isinstance(raw.get("chapter_ids"), list) else []
        media_ids = raw.get("media_ids") if isinstance(raw.get("media_ids"), list) else []
        targets = {index_by_id[str(cid)] for cid in chapter_ids if str(cid) in index_by_id}
        resolved = {_alias_or_id(str(mid), alias_to_id, allowed) for mid in media_ids} - {None}
        targets.update(
            i
            for i, chapter in enumerate(plan.chapters)
            if any(source.media_id in resolved for source in chapter.sources)
        )
        bound_to_real_chapter = bool(targets)
        targets = {
            i
            for i in targets
            if not any(source.media_id in server_media for source in plan.chapters[i].sources)
        }
        target = next((i for i in sorted(targets) if not plan.chapters[i].thought), None)
        split: int | None = None
        if target is None:
            # Every target already shows copy (e.g. two quoted captions on two
            # shots the model put in one chapter): give the bound shot its own
            # chapter rather than failing the story.
            for index in sorted(targets):
                split = _isolate_bound_sources(plan, index, resolved)
                if split is not None:
                    break
            target = split
        else:
            # A caption bound to some shots of a chapter covers only those
            # shots, and a later caption on a sibling shot keeps a free chapter.
            # When a limit blocks the split, the whole chapter carries it.
            split = _isolate_bound_sources(plan, target, resolved)
            target = target if split is None else split
        if split is not None:
            repairs.append(f"split_creator_caption_source:{binding_index}:{split}")
        if target is not None:
            plan.chapters[target].thought = exact
            repairs.append(f"moved_creator_caption_binding_to_thought:{binding_index}:{target}")
        elif bound_to_real_chapter:
            unplaceable.setdefault(exact, binding_index)
    # A later binding of the same caption may still have found a free chapter.
    # A binding with no real target proves nothing: that caption stays required.
    shown = {chapter.thought for chapter in plan.chapters}
    for text, binding_index in unplaceable.items():
        if text not in shown:
            repairs.append(f"unplaceable_creator_caption:{binding_index}")
    return {text for text in unplaceable if text not in shown}


# The scheduler names a chapter's later 4-source beats "<chapter_id>-2" ...
# up to "-20" (80 sources); a new chapter id must never equal one of them.
_MAX_BEAT_SUFFIX = 20


def _beat_ids(chapter_id: str) -> set[str]:
    return {chapter_id, *(f"{chapter_id}-{k}" for k in range(2, _MAX_BEAT_SUFFIX + 1))}


def _isolate_bound_sources(plan: SemanticEditPlan, index: int, media_ids: set[str]) -> int | None:
    """Give a contiguous run of bound sources its own chapter: before, bound, after.

    Sources never reorder, so any order intent still holds, and each piece
    takes its sources' share of the chapter weight. The first piece keeps the
    chapter id and role (later pieces never open as a hook); the first
    unbound piece keeps the chapter's existing thought. Returns the bound
    piece's index, or None when the chapter is only the bound run, the run is
    not contiguous, or the chapter/beat limits or ids block the split.
    """
    chapter = plan.chapters[index]
    positions = [i for i, source in enumerate(chapter.sources) if source.media_id in media_ids]
    if (
        not positions
        or len(positions) == len(chapter.sources)
        or positions[-1] - positions[0] + 1 != len(positions)
    ):
        return None
    start, end = positions[0], positions[-1] + 1
    bound_at = 1 if start else 0
    pieces = [
        part
        for part in (chapter.sources[:start], chapter.sources[start:end], chapter.sources[end:])
        if part
    ]
    others = [other for other in plan.chapters if other is not chapter]
    beats = sum(math.ceil(len(other.sources) / 4) for other in others)
    if (
        len(others) + len(pieces) > 20
        or beats + sum(math.ceil(len(part) / 4) for part in pieces) > 20
    ):
        return None
    taken = set().union(*(_beat_ids(other.chapter_id) for other in plan.chapters))
    piece_ids = [chapter.chapter_id]
    base = re.sub(r"(?:-part-\d+)+$", "", chapter.chapter_id) or chapter.chapter_id
    number = 0
    while len(piece_ids) < len(pieces):
        number += 1
        candidate = f"{base}-part-{number}"
        if len(candidate) > 97:  # its "-20" beat id must fit the 100-char schema
            return None
        if not _beat_ids(candidate) & taken:
            taken |= _beat_ids(candidate)
            piece_ids.append(candidate)
    total = sum(source.weight for source in chapter.sources)
    thought_at = 1 if bound_at == 0 else 0
    plan.chapters[index : index + 1] = [
        chapter.model_copy(
            update={
                "chapter_id": piece_id,
                "sources": part,
                "weight": chapter.weight * sum(source.weight for source in part) / total,
                "thought": chapter.thought if position == thought_at else "",
                "role": "build" if position and chapter.role == "hook" else chapter.role,
            },
            deep=True,
        )
        for position, (piece_id, part) in enumerate(zip(piece_ids, pieces, strict=True))
    ]
    return index + bound_at


def _candidate_index(media: object, source_start_s: float) -> int | None:
    moments = getattr(media, "best_moments", [])
    candidates: list[tuple[float, int]] = []
    for index, moment in enumerate(moments):
        if not isinstance(moment, dict):
            continue
        value = moment.get("start_s", moment.get("start"))
        try:
            candidates.append((abs(float(value) - source_start_s), index))
        except (TypeError, ValueError):
            continue
    return min(candidates)[1] if candidates else None


def _legacy_source(
    media_id: str, source_start_s: float | None, input: EditProposalAgentInput
) -> SemanticSource:  # noqa: A002
    media = next((row for row in input.media if row.media_id == media_id), None)
    index = (
        _candidate_index(media, source_start_s) if media and source_start_s is not None else None
    )
    return SemanticSource(media_id=media_id, candidate_index=index)


def _alias_or_id(media_id: str, alias_to_id: dict[str, str], allowed: set[str]) -> str | None:
    resolved = alias_to_id.get(media_id)
    if resolved is not None:
        return resolved
    return media_id if media_id in allowed else None


def _semantic_constraint_block(
    input: EditProposalAgentInput, alias_to_id: dict[str, str]
) -> dict[str, object]:  # noqa: A002
    """Only server-confirmed constraints, rewritten into prompt aliases."""
    id_to_alias = {media_id: alias for alias, media_id in alias_to_id.items()}

    def aliases(ids: list[str]) -> list[str]:
        return [id_to_alias[media_id] for media_id in ids if media_id in id_to_alias]

    intents = []
    exclusive_groups = []
    for intent in input.clip_intents or []:
        data = intent.model_dump(mode="json")
        data["assignments"] = [
            {
                **assignment,
                "media_id": id_to_alias.get(assignment["media_id"], assignment["media_id"]),
            }
            for assignment in data["assignments"]
        ]
        intents.append(data)
        if intent.status == "resolved" and intent.op in {"group", "caption"}:
            exclusive_groups.append(
                {
                    "intent_id": intent.intent_id,
                    "aliases": aliases([assignment.media_id for assignment in intent.assignments]),
                }
            )
    cadence = input.montage_cadence
    return {
        "media_scope": input.media_scope,
        "required_media_ids": aliases(sorted(_semantic_required_media_ids(input))),
        "video_reuse_policy": input.video_reuse_policy,
        "required_chapter_count": len(input.shot_labels) if input.shot_labels else None,
        "montage_audio": (
            {
                "preserve_source_audio": input.montage_audio.preserve_source_audio,
                "preview_source_beds": input.montage_audio.preview_source_beds,
                "source_media_ids": aliases(input.montage_audio.source_media_ids),
            }
            if input.montage_audio
            else None
        ),
        "clip_intents": intents,
        "exclusive_group_aliases": exclusive_groups,
        "cadence": (
            {
                "mode": cadence.mode,
                "source_media_ids": aliases(cadence.source_media_ids),
                "reuse_policy": cadence.reuse_policy,
                "note": (
                    "Preserve source order only. The server chooses the repeat count and durations."
                ),
            }
            if cadence
            else None
        ),
        "mixed_media_timing": input.mixed_media_timing.model_dump(mode="json")
        if input.mixed_media_timing
        else None,
        "narration": {"present": input.narration_duration_s is not None},
    }


def semantic_plan_from_legacy(
    output: EditProposalAgentOutput,
    input: EditProposalAgentInput,  # noqa: A002
) -> SemanticEditPlan:
    """Project legacy output into semantic intent for fixtures and corrections only.

    This is deliberately not a production fallback: new work must ask the
    semantic agent, then let the server scheduler materialize a frame plan.
    """
    chapters: list[SemanticChapter] = []
    if output.fast_cuts:
        # Legacy cuts can number 80 while semantic plans intentionally cap
        # chapters at 20. Collapse repeated sources into their first semantic
        # appearance; the scheduler expands cadence/reuse into real cuts.
        cut_by_media: dict[str, object] = {}
        source_order: list[str] = []
        for cut in output.fast_cuts:
            if cut.media_id not in cut_by_media:
                cut_by_media[cut.media_id] = cut
                source_order.append(cut.media_id)
        for index in range(0, len(source_order), 4):
            source_ids = source_order[index : index + 4]
            source_cuts = [cut_by_media[media_id] for media_id in source_ids]
            role = "hook" if index == 0 else "payoff" if index + 4 >= len(source_order) else "build"
            chapters.append(
                SemanticChapter(
                    chapter_id=f"chapter-{index // 4 + 1}",
                    topic="Montage",
                    thought="",
                    role=role,
                    weight=max(sum(float(cut.output_duration_s) for cut in source_cuts), 0.001),
                    layout="fullscreen",
                    sources=[
                        _legacy_source(cut.media_id, cut.source_start_s, input)
                        for cut in source_cuts
                    ],
                )
            )
    else:
        total = sum(max(float(beat.duration_s), 0.0) for beat in output.story_beats) or 1.0
        for index, beat in enumerate(output.story_beats):
            chapters.append(
                SemanticChapter(
                    chapter_id=f"chapter-{index + 1}",
                    topic=beat.topic,
                    thought=beat.thought,
                    role="hook"
                    if index == 0
                    else "payoff"
                    if index == len(output.story_beats) - 1
                    else "build",
                    weight=max(float(beat.duration_s) / total, 0.001),
                    layout=beat.layout,
                    sources=[_legacy_source(media_id, None, input) for media_id in beat.media_ids],
                )
            )
    if not chapters:
        raise SchemaError("semantic_edit_proposal: legacy output has no editorial sources")
    bindings = [
        SemanticTextBinding(text=item.text, media_ids=[item.media_id])
        for item in output.montage_text_bindings
    ]
    plan = SemanticEditPlan(
        title=input.opening_title or output.title,
        chapters=chapters,
        montage_audio=input.montage_audio,
        text_bindings=bindings,
    )
    if input.shot_labels and input.direction != "fast_montage":
        if len(plan.chapters) != len(input.shot_labels):
            raise SchemaError("semantic_edit_proposal: legacy labels need one chapter each")
        for chapter, label in zip(plan.chapters, input.shot_labels, strict=True):
            chapter.thought = label
    elif captions := _creator_captions(input):
        label_keys = _resolved_label_keys(input)
        for chapter in plan.chapters:
            if creator_copy_match_key(chapter.thought) in label_keys:
                chapter.thought = ""
                continue
            exact = _resolve_creator_caption(chapter.thought, captions)
            chapter.thought = exact if exact is not None else ""
    SemanticEditProposalAgent._add_creator_caption_bindings(plan, input)
    if len(plan.text_bindings) > 12:
        raise SchemaError(
            "semantic_edit_proposal: legacy creator bindings exceed the 12-item limit"
        )
    return plan


class SemanticEditProposalAgent(Agent[EditProposalAgentInput, SemanticEditPlan]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.semantic_edit_proposal",
        prompt_id="semantic_edit_proposal",
        prompt_version="2.0.12",
        model="gemini-2.5-flash",
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = EditProposalAgentInput
    Output = SemanticEditPlan
    response_json = True

    def required_fields(self) -> list[str]:
        return ["chapters"]

    def render_prompt(self, input: EditProposalAgentInput) -> str:  # noqa: A002
        prompt_media, aliases, id_to_alias = _semantic_prompt_media(input)
        group_ids_by_alias = {row.media_id: [] for row in prompt_media}
        for intent in input.clip_intents or []:
            if intent.status != "resolved" or intent.op not in {"group", "caption"}:
                continue
            for assignment in intent.assignments:
                alias = id_to_alias.get(assignment.media_id)
                if alias is not None:
                    group_ids_by_alias[alias].append(intent.intent_id)
        media_rows = []
        for row in prompt_media:
            projection = _media_prompt_dict(row)
            projection["server_exclusive_group_ids"] = group_ids_by_alias[row.media_id]
            media_rows.append(projection)
        return load_prompt(
            "semantic_edit_proposal",
            direction=input.direction,
            goal=input.goal or input.idea or input.theme or "(none)",
            creator_request=input.creator_request or "(none)",
            opening_title=input.opening_title or "",
            closing_title=input.closing_title or "",
            shot_labels_json=json.dumps(input.shot_labels or [], ensure_ascii=False),
            constraints_json=json.dumps(
                _semantic_constraint_block(input, aliases), ensure_ascii=False
            ),
            media_json=json.dumps(media_rows, ensure_ascii=False),
        )

    def parse(self, raw_text: str, input: EditProposalAgentInput) -> SemanticEditPlan:  # noqa: A002
        self._schema_retry_hint: str | None = None
        try:
            return self._parse(raw_text, input)
        except SchemaError as exc:
            if str(exc) in {
                _GROUP_SPLIT_ERROR,
                _GROUP_UNRELATED_ERROR,
                "semantic_edit_proposal: caption intent has unrelated sources",
            }:
                self._schema_retry_hint = _GROUP_RETRY_HINT
            elif str(exc) in {
                "semantic_edit_proposal: creator shot labels need one chapter each",
                "semantic_edit_proposal: a video appears more than once under once reuse",
                "semantic_edit_proposal: requested media coverage was dropped",
            }:
                self._schema_retry_hint = (
                    "Correction: include every alias in required_media_ids. Under once reuse, "
                    "each video alias can appear only once across the entire chapters array."
                )
                if input.shot_labels:
                    self._schema_retry_hint += (
                        f" Return exactly {len(input.shot_labels)} chapters, one per shot label "
                        "in order. Put opening footage inside the first labeled chapter; "
                        "do not add an introduction or ending chapter."
                    )
            elif str(exc) in {
                "semantic_edit_proposal: source references unknown media",
                "semantic_edit_proposal: text binding references unknown media",
            }:
                self._schema_retry_hint = _unknown_media_retry_hint(len(input.media))
            elif str(exc) == "semantic_edit_proposal: creator caption was dropped":
                self._schema_retry_hint = _CAPTION_RETRY_HINT
            raise

    def schema_clarification(self) -> str:
        suffix = getattr(self, "_schema_retry_hint", None)
        return super().schema_clarification() + (f"\n\n{suffix}" if suffix else "")

    def _parse(self, raw_text: str, input: EditProposalAgentInput) -> SemanticEditPlan:  # noqa: A002
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"semantic_edit_proposal: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict):
            raise SchemaError("semantic_edit_proposal: response must be an object")
        if any(key in payload for key in _FORBIDDEN_TIMING_KEYS):
            raise SchemaError("semantic_edit_proposal: timeline fields belong to the scheduler")
        if not isinstance(payload.get("chapters"), list):
            raise SchemaError("semantic_edit_proposal: response needs chapters")

        _prompt_media_rows, alias_to_id, id_to_alias = _semantic_prompt_media(input)
        allowed = set(id_to_alias)
        normalized = {**payload, "montage_audio": input.montage_audio, "repairs": []}
        parse_repairs: list[str] = []
        unknown_sources: list[str] = []
        normalized_chapters: list[dict] = []
        # A recorded voiceover owns body text: its timed captions are
        # server-drawn. Any thought that is not creator-confirmed copy (a
        # quoted or resolved caption; shot labels are handled below) is AI
        # draft text that guided_story._text_elements would burn over those
        # captions. Blank it before validation so it never decides a
        # chapter's fate (e.g. an extra sourceless chapter for the last
        # voiceover line, or a thought over the schema's length cap), even
        # when the creator also asked for some on-screen copy.
        narrated = (
            input.direction != "fast_montage"
            and input.narration_duration_s is not None
            and not input.shot_labels
        )
        narrated_copy = _creator_captions(input) if narrated else {}
        narrated_repairs: list[str] = []
        for chapter_index, raw_chapter in enumerate(payload["chapters"]):
            if not isinstance(raw_chapter, dict):
                raise SchemaError(
                    f"semantic_edit_proposal: chapter {chapter_index} is not an object"
                )
            if any(key in raw_chapter for key in _FORBIDDEN_TIMING_KEYS):
                raise SchemaError("semantic_edit_proposal: chapter timing belongs to the scheduler")
            raw_sources = raw_chapter.get("sources")
            if not isinstance(raw_sources, list):
                raise SchemaError(f"semantic_edit_proposal: chapter {chapter_index} needs sources")
            raw_thought = str(raw_chapter.get("thought") or "").strip()
            if narrated and creator_copy_match_key(raw_thought) not in narrated_copy:
                raw_thought = ""
            if not raw_sources:
                # The server renders titles separately. Gemini sometimes adds
                # an otherwise-empty title/outro chapter despite that contract;
                # it has no editorial source or copy to preserve, so omit it.
                thought = raw_thought
                server_titles = {
                    title for title in (input.opening_title, input.closing_title) if title
                }
                if thought and thought not in server_titles:
                    raise SchemaError(
                        f"semantic_edit_proposal: chapter {chapter_index} needs sources"
                    )
                repair = (
                    "dropped_empty_server_title_chapter" if thought else "dropped_empty_chapter"
                )
                parse_repairs.append(f"{repair}:{chapter_index}")
                continue
            sources: list[dict] = []
            for source_index, raw_source in enumerate(raw_sources):
                if not isinstance(raw_source, dict):
                    raise SchemaError(
                        f"semantic_edit_proposal: source {chapter_index}:{source_index} is invalid"
                    )
                media_id = alias_to_id.get(str(raw_source.get("media_id") or ""))
                if media_id is None and str(raw_source.get("media_id") or "") in allowed:
                    media_id = str(raw_source["media_id"])
                if media_id is None:
                    unknown_sources.append(f"{chapter_index}:{source_index}")
                    continue
                candidate_index = raw_source.get("candidate_index")
                moments = next(row.best_moments for row in input.media if row.media_id == media_id)
                if candidate_index is not None:
                    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
                        raise SchemaError(
                            "semantic_edit_proposal: source references unknown candidate"
                        )
                    if not moments and candidate_index == 0:
                        # No prompt-visible preferred window exists. Treat a
                        # model's habitual candidate_index=0 as absent rather
                        # than rejecting an otherwise grounded source.
                        raw_source = {**raw_source, "candidate_index": None}
                        parse_repairs.append(
                            f"dropped_unavailable_candidate:{chapter_index}:{source_index}"
                        )
                    elif not moments or candidate_index < 0 or candidate_index >= len(moments):
                        raise SchemaError(
                            "semantic_edit_proposal: source references unknown candidate"
                        )
                repeated = next((row for row in sources if row["media_id"] == media_id), None)
                weights = (
                    None if repeated is None else (repeated.get("weight"), raw_source.get("weight"))
                )
                if weights is not None and all(
                    weight is None
                    or (isinstance(weight, int | float) and not isinstance(weight, bool))
                    for weight in weights
                ):
                    # One shot listed twice in a chapter (e.g. reused for a
                    # described but unattached photo) is one longer window.
                    repeated["weight"] = sum(1.0 if w is None else w for w in weights)
                    parse_repairs.append(f"merged_repeated_source:{chapter_index}:{source_index}")
                    continue
                sources.append({**raw_source, "media_id": media_id})
            if not sources:
                # Every source was an invented alias (m000). Only an otherwise
                # empty chapter may go; its copy has no grounded footage.
                if raw_thought:
                    raise SchemaError("semantic_edit_proposal: source references unknown media")
                parse_repairs.append(f"dropped_unknown_media_chapter:{chapter_index}")
                continue
            if narrated and str(raw_chapter.get("thought") or "").strip() and not raw_thought:
                raw_chapter = {**raw_chapter, "thought": ""}
                narrated_repairs.append(f"blanked_narrated_thought:{len(normalized_chapters)}")
            normalized_chapters.append({**raw_chapter, "sources": sources})
        if unknown_sources:
            covered = {
                source["media_id"]
                for chapter in normalized_chapters
                for source in chapter["sources"]
            }
            # Surplus only: every real alias is still used, so the invented
            # one cannot be an off-by-one substitute for a real source.
            if not set(alias_to_id.values()) <= covered:
                raise SchemaError("semantic_edit_proposal: source references unknown media")
            parse_repairs.extend(f"dropped_unknown_media_source:{ref}" for ref in unknown_sources)
        if input.shot_labels and input.direction != "fast_montage":
            if len(normalized_chapters) != len(input.shot_labels):
                raise SchemaError(
                    "semantic_edit_proposal: creator shot labels need one chapter each"
                )
            for chapter_index, (chapter, label) in enumerate(
                zip(normalized_chapters, input.shot_labels, strict=True)
            ):
                if chapter.get("thought") != label:
                    chapter["thought"] = label
                    parse_repairs.append(
                        f"replaced_server_owned_shot_label_thought:{chapter_index}"
                    )
        normalized["chapters"] = normalized_chapters
        raw_bindings = payload.get("text_bindings", [])
        if not isinstance(raw_bindings, list):
            if input.direction == "fast_montage":
                raise SchemaError("semantic_edit_proposal: text_bindings must be a list")
            if raw_bindings is not None:
                parse_repairs.append("dropped_invalid_non_montage_text_bindings")
            raw_bindings = []
        dropped_bindings: list[tuple[int, dict]] = []
        if input.direction != "fast_montage":
            # Only fast_montage renders the per-source montage lane (the
            # guided_story compiler ignores it elsewhere, and revisions strip
            # it). Drop it before validation so the 12-item cap, targets, and
            # per-source conflicts of an unrendered lane can never fail a
            # story whose visible copy lives in chapter thoughts.
            dropped_bindings = [
                (index, raw_binding)
                for index, raw_binding in enumerate(raw_bindings)
                if isinstance(raw_binding, dict) and isinstance(raw_binding.get("text"), str)
            ]
            if raw_bindings:
                parse_repairs.append(f"dropped_non_montage_text_bindings:{len(raw_bindings)}")
            raw_bindings = []
        normalized_bindings: list[dict] = []
        for index, raw_binding in enumerate(raw_bindings):
            if not isinstance(raw_binding, dict):
                raise SchemaError(f"semantic_edit_proposal: text binding {index} is invalid")
            raw_media_ids = raw_binding.get("media_ids", [])
            if not isinstance(raw_media_ids, list):
                raise SchemaError(
                    f"semantic_edit_proposal: text binding {index} media_ids is invalid"
                )
            media_ids: list[str] = []
            for raw_media_id in raw_media_ids:
                media_id = _alias_or_id(str(raw_media_id), alias_to_id, allowed)
                if media_id is None:
                    raise SchemaError(
                        "semantic_edit_proposal: text binding references unknown media"
                    )
                if media_id not in media_ids:
                    media_ids.append(media_id)
            normalized_bindings.append({**raw_binding, "media_ids": media_ids})
        normalized["text_bindings"] = normalized_bindings
        try:
            plan = SemanticEditPlan.model_validate(normalized)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"semantic_edit_proposal: invalid output — {exc}") from exc

        if input.direction == "fast_montage" and plan.chapters[0].role != "hook":
            raise SchemaError("semantic_edit_proposal: first remaining chapter must be hook")

        # Canonicalization below may discard generic copy, but never invalid
        # targets: reject those before deciding whether its text is server-owned.
        self._validate_bindings(plan, input)

        used = {source.media_id for chapter in plan.chapters for source in chapter.sources}
        required = _semantic_required_media_ids(input)
        if required and not required <= used:
            raise SchemaError("semantic_edit_proposal: requested media coverage was dropped")
        repairs = parse_repairs
        if input.opening_title:
            plan.title = input.opening_title
        if not input.shot_labels or input.direction == "fast_montage":
            _blank_grounded_label_thoughts(plan, input, repairs)
        _canonicalize_text_bindings(plan, input, repairs)
        if not input.shot_labels and (captions := _creator_captions(input)):
            for index, chapter in enumerate(plan.chapters):
                key = creator_copy_match_key(chapter.thought)
                exact = _resolve_creator_caption(chapter.thought, captions)
                if chapter.thought and key in captions and exact is None:
                    raise SchemaError(
                        "semantic_edit_proposal: creator caption variants are ambiguous"
                    )
                if chapter.thought and exact is None:
                    chapter.thought = ""
                    repairs.append(f"blanked_unrequested_thought:{index}")
                elif exact is not None:
                    chapter.thought = exact
                    if input.direction == "fast_montage":
                        plan.text_bindings.append(
                            SemanticTextBinding(text=exact, chapter_ids=[chapter.chapter_id])
                        )
        repairs.extend(narrated_repairs)
        _validate_reuse_once(plan, input)
        self._recover_single_misassigned_group(plan, input, alias_to_id, repairs)
        unplaceable: set[str] = set()
        if input.direction != "fast_montage":
            # After group recovery: a stray group member moved out of a
            # chapter frees that chapter (and its shots) for creator copy.
            unplaceable = _restore_dropped_creator_captions(
                plan, input, dropped_bindings, alias_to_id, allowed, repairs
            )
        self._apply_resolved_caption_intents(plan, input, alias_to_id)
        self._add_creator_caption_bindings(plan, input)
        self._enforce_creator_text_bindings(plan, input, unplaceable)
        if len(plan.text_bindings) > 12:
            raise SchemaError("semantic_edit_proposal: creator bindings exceed the 12-item limit")
        plan.repairs.extend(repairs)
        self._validate_intents(plan, input, alias_to_id)
        _validate_reuse_once(plan, input)
        self._validate_bindings(plan, input)
        return plan

    @staticmethod
    def _recover_single_misassigned_group(
        plan: SemanticEditPlan,
        input: EditProposalAgentInput,
        aliases: dict[str, str],
        repairs: list[str],
    ) -> None:
        """Move one stray trusted-group member into its sole exclusive anchor."""
        if input.shot_labels:
            return
        for intent in input.clip_intents or []:
            text = (
                intent.caption_text
                if intent.op == "caption"
                else intent.creator_text
                if intent.op == "group"
                else None
            )
            if intent.status != "resolved" or not text:
                continue
            ids = [aliases.get(item.media_id, item.media_id) for item in intent.assignments]
            group = set(ids)
            overlaps = False
            for other in input.clip_intents or []:
                other_ids = {
                    aliases.get(item.media_id, item.media_id) for item in other.assignments
                }
                if (
                    other.status == "resolved"
                    and other is not intent
                    and other.op in {"group", "caption"}
                    and other_ids
                    and other_ids != group
                    and group & other_ids
                ):
                    overlaps = True
                    break
            if overlaps:
                continue
            anchors = [
                i
                for i, c in enumerate(plan.chapters)
                if any(s.media_id in group for s in c.sources)
                and all(s.media_id in group for s in c.sources)
            ]
            mixed = [
                i
                for i, c in enumerate(plan.chapters)
                if any(s.media_id in group for s in c.sources)
                and any(s.media_id not in group for s in c.sources)
            ]
            if not mixed:
                continue
            if (
                len(anchors) != 1
                or len(mixed) != 1
                or any(i == 0 and plan.chapters[i].role == "hook" for i in mixed)
            ):
                continue
            anchor, origin = anchors[0], mixed[0]
            if plan.chapters[anchor].layout != plan.chapters[origin].layout:
                continue
            moved = [s for s in plan.chapters[origin].sources if s.media_id in group]
            remain = [s for s in plan.chapters[origin].sources if s.media_id not in group]
            if not moved or not remain:
                continue
            if plan.chapters[origin].thought:
                continue
            moved_ids = {source.media_id for source in moved}
            if moved_ids & {source.media_id for source in plan.chapters[anchor].sources}:
                continue
            if any(
                binding.text != text
                and (
                    plan.chapters[origin].chapter_id in binding.chapter_ids
                    or plan.chapters[anchor].chapter_id in binding.chapter_ids
                    or moved_ids & set(binding.media_ids)
                )
                for binding in plan.text_bindings
            ):
                continue
            anchor_sources = plan.chapters[anchor].sources
            masses = {
                id(source): chapter.weight
                * (source.weight / sum(item.weight for item in chapter.sources))
                for chapter in (plan.chapters[anchor], plan.chapters[origin])
                for source in chapter.sources
            }
            if not all(math.isfinite(mass) and mass > 0 for mass in masses.values()):
                continue
            rank = {media_id: index for index, media_id in enumerate(ids)}
            merged = list(anchor_sources)
            for source in sorted(moved, key=lambda item: rank[item.media_id]):
                insert_at = next(
                    (
                        index
                        for index, existing in enumerate(merged)
                        if rank.get(existing.media_id, len(rank)) > rank[source.media_id]
                    ),
                    len(merged),
                )
                merged.insert(insert_at, source)
            chapter_totals = [
                sum(masses[id(source)] for source in sources) for sources in (remain, merged)
            ]
            if len(merged) > 80 or not all(math.isfinite(total) for total in chapter_totals):
                continue
            plan.chapters[origin].sources = remain
            plan.chapters[anchor].sources = merged
            for chapter in (plan.chapters[anchor], plan.chapters[origin]):
                for source in chapter.sources:
                    source.weight = masses[id(source)]
                chapter.weight = sum(source.weight for source in chapter.sources)
            repairs.append(f"recovered_misassigned_group:{intent.intent_id}")

    @staticmethod
    def _validate_bindings(plan: SemanticEditPlan, input: EditProposalAgentInput) -> None:
        chapter_ids = {chapter.chapter_id for chapter in plan.chapters}
        scheduled_media_ids = {
            source.media_id for chapter in plan.chapters for source in chapter.sources
        }
        for binding in plan.text_bindings:
            if (
                not set(binding.chapter_ids) <= chapter_ids
                or not set(binding.media_ids) <= scheduled_media_ids
            ):
                raise SchemaError("semantic_edit_proposal: text binding references unknown target")

    @staticmethod
    def _add_creator_caption_bindings(
        plan: SemanticEditPlan, input: EditProposalAgentInput
    ) -> None:
        """Fast montages render text bindings, never chapter thoughts."""
        if input.direction != "fast_montage":
            return
        captions = _creator_captions(input)
        if not captions:
            return
        scheduled = {source.media_id for chapter in plan.chapters for source in chapter.sources}
        existing = {(binding.text, tuple(binding.media_ids)) for binding in plan.text_bindings}
        for intent in input.clip_intents or []:
            if intent.status != "resolved" or intent.op == "label" or not intent.creator_text:
                continue
            text = _resolve_creator_caption(intent.creator_text, captions)
            if creator_copy_match_key(intent.creator_text) in captions and text is None:
                raise SchemaError("semantic_edit_proposal: creator caption variants are ambiguous")
            targets = [
                assignment.media_id
                for assignment in intent.assignments
                if assignment.media_id in scheduled
            ]
            key = (text or "", tuple(targets))
            same_text_exists = any(binding.text == text for binding in plan.text_bindings)
            if text and targets and key not in existing and not same_text_exists:
                plan.text_bindings.append(SemanticTextBinding(text=text, media_ids=targets))
                existing.add(key)

    @staticmethod
    def _apply_resolved_caption_intents(
        plan: SemanticEditPlan, input: EditProposalAgentInput, aliases: dict[str, str]
    ) -> None:
        """Bind trusted resolved captions to their exclusive assigned chapter(s)."""
        if input.shot_labels:
            return
        assigned: dict[str, str] = {}
        caption_texts = {
            intent.caption_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "caption" and intent.caption_text
        }
        bound_texts = caption_texts | {
            intent.creator_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "group" and intent.creator_text
        }
        for chapter in plan.chapters:
            if chapter.thought in bound_texts:
                chapter.thought = ""
        plan.text_bindings = [
            binding for binding in plan.text_bindings if binding.text not in bound_texts
        ]
        for intent in input.clip_intents or []:
            if intent.status != "resolved":
                continue
            text = (
                intent.caption_text
                if intent.op == "caption"
                else intent.creator_text
                if intent.op == "group"
                else None
            )
            if not text:
                continue
            ids = {aliases.get(item.media_id, item.media_id) for item in intent.assignments}
            indexes = [
                index
                for index, chapter in enumerate(plan.chapters)
                if any(source.media_id in ids for source in chapter.sources)
            ]
            if not indexes:
                raise SchemaError("semantic_edit_proposal: caption intent source was dropped")
            for index in indexes:
                if any(source.media_id not in ids for source in plan.chapters[index].sources):
                    raise SchemaError(
                        "semantic_edit_proposal: caption intent has unrelated sources"
                    )
                prior = assigned.get(plan.chapters[index].chapter_id)
                if prior is not None and prior != text:
                    raise SchemaError(
                        "semantic_edit_proposal: caption intents conflict on a chapter"
                    )
                assigned[plan.chapters[index].chapter_id] = text
        for chapter in plan.chapters:
            if text := assigned.get(chapter.chapter_id):
                chapter.thought = text
                if input.direction == "fast_montage":
                    plan.text_bindings.append(
                        SemanticTextBinding(text=text, chapter_ids=[chapter.chapter_id])
                    )

    @staticmethod
    def _enforce_creator_text_bindings(
        plan: SemanticEditPlan,
        input: EditProposalAgentInput,  # noqa: A002
        unplaceable: set[str],
    ) -> None:
        """Quoted creator captions form a complete text-binding allowlist.

        ``unplaceable`` excuses only captions a dropped binding tied to real
        chapters that could not take them (see _restore_dropped_creator_captions);
        a caption the model never placed anywhere still fails.
        """
        captions = _creator_captions(input)
        label_keys = _resolved_label_keys(input)
        label_texts = _resolved_label_texts(input)
        # Resolved labels are re-grounded by the worker's dedicated context
        # lane.  They must never become generic montage text, even if the
        # model copied a server-resolved value verbatim.
        for binding in plan.text_bindings:
            key = creator_copy_match_key(binding.text)
            if binding.text in label_texts or (
                key in label_keys and binding.text not in _caption_texts(captions)
            ):
                raise SchemaError(
                    "semantic_edit_proposal: resolved label belongs to the grounded label lane"
                )
        if not captions:
            return
        for binding in plan.text_bindings:
            key = creator_copy_match_key(binding.text)
            if key not in captions or binding.text not in captions[key]:
                raise SchemaError("semantic_edit_proposal: unrequested text binding was invented")
        # Only fast_montage draws bindings; elsewhere a caption counts as
        # present only when a chapter thought will render it.
        rendered_bindings = plan.text_bindings if input.direction == "fast_montage" else []
        present = [
            text
            for text in [
                *(chapter.thought for chapter in plan.chapters),
                *(binding.text for binding in rendered_bindings),
            ]
            if text
        ]
        required_captions = _caption_texts(captions) - label_texts - unplaceable
        if not required_captions <= set(present):
            raise SchemaError("semantic_edit_proposal: creator caption was dropped")

    @staticmethod
    def _validate_intents(
        plan: SemanticEditPlan, input: EditProposalAgentInput, aliases: dict[str, str]
    ) -> None:
        if not input.clip_intents:
            return
        positions: dict[str, list[int]] = {}
        source_order: list[str] = []
        for index, chapter in enumerate(plan.chapters):
            for source in chapter.sources:
                positions.setdefault(source.media_id, []).append(index)
                source_order.append(source.media_id)
        for intent in input.clip_intents:
            if intent.status != "resolved":
                continue
            ids = [aliases.get(item.media_id, item.media_id) for item in intent.assignments]
            if not all(media_id in positions for media_id in ids):
                raise SchemaError("semantic_edit_proposal: clip intent source was dropped")
            if intent.op in {"group", "caption"}:
                indexes = sorted(
                    {index for media_id in ids for index in positions.get(media_id, [])}
                )
                if not indexes or indexes[-1] - indexes[0] + 1 != len(indexes):
                    raise SchemaError("semantic_edit_proposal: clip intent group was split")
                for chapter in plan.chapters[indexes[0] : indexes[-1] + 1]:
                    if any(source.media_id not in ids for source in chapter.sources):
                        raise SchemaError(
                            "semantic_edit_proposal: clip intent group has unrelated sources"
                        )
            if intent.op == "order" and intent.position in {"first", "last"}:
                own = [index for index, media_id in enumerate(source_order) if media_id in ids]
                other = [
                    index for index, media_id in enumerate(source_order) if media_id not in ids
                ]
                if (
                    own
                    and other
                    and (
                        (intent.position == "first" and max(own) > min(other))
                        or (intent.position == "last" and min(own) < max(other))
                    )
                ):
                    raise SchemaError("semantic_edit_proposal: clip intent order changed")
