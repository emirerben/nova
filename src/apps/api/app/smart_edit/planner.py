"""Transcript-anchored semantic planner for Smart talking-head edits."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.agents.smart_edit_planner import (
    SmartEditPlannerAgent,
    SmartEditPlannerInput,
    SmartPlannerAsset,
    SmartPlannerCandidate,
    SmartPlannerProposal,
)
from app.smart_edit.captions import build_semantic_caption_cues, build_smart_caption_cues
from app.smart_edit.presets import SmartEditPreset, load_preset
from app.smart_edit.schemas import (
    MAX_SMART_WORDS,
    SMART_EDIT_SCHEMA_VERSION_V2,
    AudioTreatmentLane,
    BaselineCaptionCue,
    BoundaryEffectLane,
    CameraLane,
    CaptionEmphasisLane,
    EventAnchor,
    SemanticRole,
    SfxLane,
    SmartEditEvent,
    SmartEditPlanDocument,
    SmartWord,
    TextLane,
    VisualLane,
    build_event_id,
)

PLANNER_VERSION = "smart-events-2026-07-18.4"
PLANNER_VERSION_V2 = "smart-events-2026-07-20.1"
_TOKEN_RE = re.compile(r"\S+")
_WORD_RE = re.compile(r"[a-z0-9]+")

_DIRECT_ORDINALS = {
    "birinci": 1,
    "birincisi": 1,
    "ilk": 1,
    "ikinci": 2,
    "ikincisi": 2,
    "ucuncu": 3,
    "ucuncusu": 3,
    "dorduncu": 4,
    "dorduncusu": 4,
    "besinci": 5,
    "besincisi": 5,
    "altinci": 6,
    "altincisi": 6,
}
_CARDINALS = {
    "bir": 1,
    "iki": 2,
    "uc": 3,
    "dort": 4,
    "bes": 5,
    "alti": 6,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
}
_LIST_TERMS = {"baslik", "baslikta", "madde", "maddede", "neden", "adim", "bolum"}
_CONTEXT_PREFIXES = (
    "peki",
    "simdi",
    "gelelim",
    "diger taraftan",
    "bir diger",
    "ama asil",
    "bunun yaninda",
    "siradaki",
    "neden",
    "nasil",
)
_EXAMPLE_PREFIXES = ("ornegin", "mesela", "buna ornek", "ornek olarak")
_PAYOFF_PREFIXES = (
    "sonuc olarak",
    "kisacasi",
    "ozetle",
    "en onemlisi",
    "iste bu yuzden",
    "dolayisiyla",
)
_CTA_TERMS = ("takip et", "yorum", "kaydet", "paylas", "abone ol", "sen de")
_KEYWORD_STOP = {
    "bir",
    "iki",
    "uc",
    "dort",
    "bes",
    "ilk",
    "olarak",
    "birinci",
    "ikinci",
    "ucuncu",
    "dorduncu",
    "neden",
    "nasil",
    "ve",
    "de",
    "da",
    "bu",
    "su",
}
_ASSET_STOP = _KEYWORD_STOP | {
    "image",
    "video",
    "gorsel",
    "logo",
    "png",
    "jpg",
    "jpeg",
    "mp4",
    "character",
    "characters",
    "family",
    "mascot",
    "maskot",
    "the",
    "and",
}


@dataclass(frozen=True, slots=True)
class SmartPlanBuild:
    normalized_words: list[SmartWord]
    caption_cues: list[dict[str, Any]]
    document: SmartEditPlanDocument
    planner_versions: dict[str, str]
    validation_receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _V2SemanticTimeline:
    candidates: list[SmartPlannerCandidate]
    role_by_word_id: dict[str, SemanticRole]
    boundary_after_word_ids: set[str]
    hook_end_word_id: str
    declared_chapter_count: int | None
    detected_chapters: list[int]
    missing_chapters: list[int]
    chapter_order_valid: bool


def _fold(value: str) -> str:
    value = value.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    return " ".join(_WORD_RE.findall(value))


def _cue_tokens(cue: dict[str, Any]) -> list[str]:
    return [token for token in _TOKEN_RE.findall(str(cue.get("text") or "").strip()) if token]


def _word_windows(cue: dict[str, Any], tokens: list[str]) -> list[tuple[float, float, str]]:
    raw_words = cue.get("words")
    if isinstance(raw_words, list) and len(raw_words) == len(tokens):
        windows: list[tuple[float, float, str]] = []
        try:
            for raw in raw_words:
                if not isinstance(raw, dict):
                    raise ValueError
                start = max(0.0, float(raw.get("start_s", 0.0)))
                end = max(start + 0.01, float(raw.get("end_s", start + 0.01)))
                quality = str(raw.get("timing_quality") or "aligned")
                if quality not in {"aligned", "segment_estimate", "unsafe"}:
                    quality = "aligned"
                windows.append((start, end, quality))
            return windows
        except (TypeError, ValueError):
            pass

    start = max(0.0, float(cue.get("start_s", 0.0) or 0.0))
    end = max(start + 0.01, float(cue.get("end_s", start + 0.01) or start + 0.01))
    step = (end - start) / max(1, len(tokens))
    return [
        (start + index * step, start + (index + 1) * step, "segment_estimate")
        for index in range(len(tokens))
    ]


def _normalize_captions(
    cues: list[dict[str, Any]], *, language: str
) -> tuple[list[SmartWord], list[BaselineCaptionCue], list[list[str]]]:
    words: list[SmartWord] = []
    baseline: list[BaselineCaptionCue] = []
    cue_word_ids: list[list[str]] = []
    next_word = 1
    for cue_index, cue in enumerate(cues):
        tokens = _cue_tokens(cue)
        if not tokens or next_word - 1 + len(tokens) > MAX_SMART_WORDS:
            continue
        ids: list[str] = []
        for token, (start_s, end_s, quality) in zip(tokens, _word_windows(cue, tokens)):
            word_id = f"w{next_word:06d}"
            next_word += 1
            words.append(
                SmartWord(
                    word_id=word_id,
                    spoken_text=token,
                    display_text=token,
                    normalized_text=_fold(token) or token.casefold(),
                    start_ms=round(start_s * 1000),
                    end_ms=max(round(end_s * 1000), round(start_s * 1000) + 1),
                    timing_quality=quality,
                    display_alignment=[word_id],
                    language=language[:16] or None,
                )
            )
            ids.append(word_id)
        baseline.append(
            BaselineCaptionCue(
                cue_id=f"smart-cue-{cue_index + 1:03d}",
                word_ids=ids,
                display_text=str(cue.get("text") or "").strip(),
            )
        )
        cue_word_ids.append(ids)
    return words, baseline, cue_word_ids


def _coerce_assets(raw_assets: list[dict[str, Any]] | None) -> list[SmartPlannerAsset]:
    result: list[SmartPlannerAsset] = []
    for raw in raw_assets or []:
        if not isinstance(raw, dict):
            continue
        analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
        try:
            result.append(
                SmartPlannerAsset(
                    asset_id=str(raw.get("id") or raw.get("asset_id") or ""),
                    kind="video" if raw.get("kind") == "video" else "image",
                    subject=str(analysis.get("subject") or raw.get("subject") or "")[:200],
                    description=str(analysis.get("description") or raw.get("description") or "")[
                        :400
                    ],
                    on_screen_text=str(analysis.get("on_screen_text") or "")[:300],
                    brands=[
                        str(value)[:80]
                        for value in (analysis.get("brands") or raw.get("brands") or [])[:10]
                    ],
                    filename=str(raw.get("source_filename") or raw.get("filename") or "")[:160],
                    aspect=float(raw["aspect"]) if raw.get("aspect") else None,
                    duration_s=float(raw["duration_s"]) if raw.get("duration_s") else None,
                )
            )
        except Exception:
            continue
    return [asset for asset in result if asset.asset_id][:20]


def _asset_terms(asset: SmartPlannerAsset) -> set[str]:
    values = [asset.subject, asset.description, asset.on_screen_text, asset.filename, *asset.brands]
    return {
        token
        for value in values
        for token in _fold(value).split()
        if len(token) >= 3 and token not in _ASSET_STOP
    }


def _token_variants(token: str) -> set[str]:
    """Return conservative lexical variants for metadata/transcript matching.

    Asset analysis often names a mascot in singular form (``selocan``) while
    Turkish speech uses a plural or possessive form (``selocanlar``).  Exact
    token overlap silently loses an otherwise high-confidence visual.  Keep
    this intentionally small instead of pretending to be a full stemmer.
    """

    variants = {token}
    for suffix in ("lari", "leri", "lar", "ler"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            variants.add(token[: -len(suffix)])
    return variants


def _matching_assets(
    cue_words: list[SmartWord],
    assets: list[SmartPlannerAsset],
    preset: SmartEditPreset,
) -> list[tuple[str, str]]:
    """Return matched asset IDs with the word that names each asset.

    A cue-level match is insufficient for an accumulating hook: it makes every
    visual enter on the first word even when the name is spoken several seconds
    later.  This keeps the existing deterministic allowlist while grounding
    each selected asset to the earliest matching transcript word.
    """

    folded = " ".join(word.normalized_text for word in cue_words)
    text_tokens = {variant for token in folded.split() for variant in _token_variants(token)}
    word_tokens = [
        {variant for token in word.normalized_text.split() for variant in _token_variants(token)}
        for word in cue_words
    ]
    scored: list[tuple[int, int, str, str]] = []
    for asset in assets:
        terms = _asset_terms(asset)
        overlap = terms & text_tokens
        brand_hits = sum(1 for brand in asset.brands if _fold(brand) and _fold(brand) in folded)
        phrase_hit = int(bool(_fold(asset.subject) and _fold(asset.subject) in folded))
        metadata = _fold(
            " ".join(
                (
                    asset.filename,
                    asset.subject,
                    asset.description,
                    asset.on_screen_text,
                    *asset.brands,
                )
            )
        )
        matched_aliases = {
            _fold(transcript_term)
            for alias in preset.visual_aliases
            if any(
                _fold(asset_term) and _fold(asset_term) in metadata
                for asset_term in alias.asset_terms
            )
            for transcript_term in alias.transcript_terms
            if _fold(transcript_term) and _fold(transcript_term) in folded
        }
        alias_tokens = {token for phrase in matched_aliases for token in phrase.split() if token}
        score = len(overlap) + brand_hits * 4 + phrase_hit * 3 + len(matched_aliases) * 6
        if score < 1:
            continue

        brand_tokens = {
            token
            for brand in asset.brands
            if _fold(brand) and _fold(brand) in folded
            for token in _fold(brand).split()
        }
        anchor_terms = alias_tokens or brand_tokens or overlap
        anchor_index = next(
            (index for index, tokens in enumerate(word_tokens) if tokens & anchor_terms),
            None,
        )
        if anchor_index is None:
            continue
        scored.append((anchor_index, -score, asset.asset_id, cue_words[anchor_index].word_id))
    scored.sort()
    return [(asset_id, anchor_word_id) for _, _, asset_id, anchor_word_id in scored[:5]]


def _number_marker_at(text: str) -> tuple[int | None, int]:
    for index, raw_token in enumerate(_TOKEN_RE.findall(text)):
        folded_token = _fold(raw_token)
        if folded_token in _DIRECT_ORDINALS:
            return _DIRECT_ORDINALS[folded_token], index
        if re.fullmatch(r"[1-9][.)]", raw_token.strip()):
            return int(raw_token.strip()[0]), index
    return None, -1


def _declared_count(words: list[SmartWord]) -> tuple[int | None, int]:
    folded = [word.normalized_text for word in words]
    for index, token in enumerate(folded):
        number = _CARDINALS.get(token)
        if number is None:
            continue
        if any(term in _LIST_TERMS for term in folded[index + 1 : index + 5]):
            return number, index
    return None, -1


def _candidate_id(role: str, word_id: str, ordinal: int) -> str:
    return f"{role}-{word_id}-{ordinal}"


def extract_candidates(
    words: list[SmartWord],
    baseline: list[BaselineCaptionCue],
    assets: list[SmartPlannerAsset],
    preset: SmartEditPreset,
) -> list[SmartPlannerCandidate]:
    if not words or not baseline:
        return []
    by_id = {word.word_id: word for word in words}
    declared_count, declaration_index = _declared_count(words)
    candidates: list[SmartPlannerCandidate] = []
    last_end_ms = 0
    next_cardinal = 1

    for cue_index, cue in enumerate(baseline):
        cue_words = [by_id[word_id] for word_id in cue.word_ids]
        if not cue_words:
            continue
        folded = _fold(cue.display_text)
        start_ms = cue_words[0].start_ms
        pause_ms = max(0, start_ms - last_end_ms)
        last_end_ms = cue_words[-1].end_ms
        role: SemanticRole | None = None
        sequence_number, sequence_token_index = _number_marker_at(cue.display_text)
        candidate_words = cue_words
        if sequence_number is not None and sequence_token_index > 0:
            # Caption correction may group a chapter declaration and its first
            # heading ("4 başlıkta anlatayım: 1. Somutlaştırma") into one cue.
            # Anchor the chapter UI/SFX to the spoken marker, not the beginning
            # of that earlier sentence.  The same rule fixes mid-cue 3./4.
            candidate_words = cue_words[sequence_token_index:]
        if (
            sequence_number is not None
            and len(candidate_words) == 1
            and cue_index + 1 < len(baseline)
        ):
            next_words = [by_id[word_id] for word_id in baseline[cue_index + 1].word_ids]
            gap_ms = next_words[0].start_ms - candidate_words[-1].end_ms if next_words else 10_000
            if next_words and gap_ms <= 700:
                # Correction sometimes leaves a spoken marker as a one-token cue
                # and moves its heading into the immediately following cue.  Pull
                # a short phrase forward so "1" and "Somutlaştırma" share one
                # semantic event without swallowing the following paragraph.
                candidate_words = [*candidate_words, *next_words[:6]]
        if cue_index == 0:
            role = "hook"
        elif sequence_number is not None:
            role = "list_item"
            next_cardinal = max(next_cardinal, sequence_number + 1)
        elif declared_count and start_ms > words[declaration_index].end_ms:
            first = cue_words[0].normalized_text
            cardinal = _CARDINALS.get(first)
            if cardinal == next_cardinal and cardinal <= declared_count:
                role = "list_item"
                sequence_number = cardinal
                next_cardinal += 1
        if role is None and any(folded.startswith(prefix) for prefix in _CTA_TERMS):
            role = "cta"
        if role is None and any(term in folded for term in _CTA_TERMS):
            role = "cta"
        if role is None and any(folded.startswith(prefix) for prefix in _PAYOFF_PREFIXES):
            role = "payoff"
        if role is None and any(folded.startswith(prefix) for prefix in _EXAMPLE_PREFIXES):
            role = "example"
        if role is None and any(folded.startswith(prefix) for prefix in _CONTEXT_PREFIXES):
            role = "context_shift"
        if role is None and pause_ms >= 1150:
            role = "context_shift"

        asset_matches = _matching_assets(cue_words, assets, preset)
        asset_ids = [asset_id for asset_id, _ in asset_matches]
        asset_anchor_word_ids = dict(asset_matches)
        if asset_matches and any(
            anchor_word_id not in {word.word_id for word in candidate_words}
            for _, anchor_word_id in asset_matches
        ):
            # A cue may contain an ordinal later in the sentence ("the first
            # mascot") that narrows the semantic text span.  A visual named
            # earlier in that same cue still needs its anchor inside the event
            # span, so retain the complete grounded cue for visual events.
            candidate_words = cue_words
        in_hook = start_ms / 1000 < preset.density.hook_window_s
        if role is None and asset_ids:
            role = "hook" if in_hook else "example"
        if role is None:
            continue
        candidates.append(
            SmartPlannerCandidate(
                candidate_id=_candidate_id(role, candidate_words[0].word_id, cue_index),
                role=role,
                start_word_id=candidate_words[0].word_id,
                end_word_id=candidate_words[-1].word_id,
                anchor_word_id=candidate_words[0].word_id,
                sequence_number=sequence_number,
                evidence=" ".join(word.display_text for word in candidate_words)[:200],
                suggested_asset_ids=asset_ids,
                suggested_asset_anchor_word_ids=asset_anchor_word_ids,
            )
        )

    # A badge is an analyzed pool asset with explicit badge/skip-ad identity.
    badge_assets = [
        asset.asset_id
        for asset in assets
        if {"badge", "skip", "skipad"} & _asset_terms(asset)
        or "skip ad" in _fold(" ".join((asset.subject, asset.filename, asset.on_screen_text)))
    ]
    if badge_assets:
        # Persistent brand furniture must animate in shortly after playback,
        # not appear on frame zero and not wait until the hook window ends.
        anchor = words[min(1, len(words) - 1)]
        candidates.append(
            SmartPlannerCandidate(
                candidate_id=_candidate_id("hook", anchor.word_id, 999),
                role="hook",
                start_word_id=anchor.word_id,
                end_word_id=anchor.word_id,
                anchor_word_id=anchor.word_id,
                evidence="persistent badge asset",
                suggested_asset_ids=badge_assets[:1],
            )
        )
    return candidates[:80]


def _fallback_proposals(
    candidates: list[SmartPlannerCandidate],
    words: list[SmartWord],
    preset: SmartEditPreset,
) -> list[SmartPlannerProposal]:
    by_id = {word.word_id: word for word in words}
    proposals: list[SmartPlannerProposal] = []
    hook_visuals = 0
    for candidate in candidates:
        start_ms = by_id[candidate.start_word_id].start_ms
        in_hook = start_ms / 1000 < preset.density.hook_window_s
        visuals = list(candidate.suggested_asset_ids)
        scene_token: str | None = None
        if visuals:
            if "persistent badge" in candidate.evidence:
                scene_token = "persistent_badge"
            elif in_hook and hook_visuals < preset.density.hook_max_visuals:
                visuals = visuals[: preset.density.hook_max_visuals - hook_visuals]
                hook_visuals += len(visuals)
                scene_token = "hook_accumulation"
            else:
                visuals = visuals[:1]
                scene_token = "single_example"

        text_token = None
        sfx_roles: list[str] = []
        boundary_token = None
        if candidate.role == "list_item" and candidate.sequence_number:
            text_token = "section_heading"
            sfx_roles = ["chapter_number_pop", "keyword_typewriter_tick"]
        elif candidate.role == "context_shift":
            text_token = "context_title"
            boundary_token = "horizontal_motion_blur"
            sfx_roles = ["transition_whip"]
        elif candidate.role == "cta":
            sfx_roles = ["cta_click"]
        if visuals:
            visual_role = "visual_enter_accent" if in_hook else "visual_enter_soft"
            sfx_roles = [*sfx_roles, visual_role][:3]
        if scene_token == "persistent_badge":
            sfx_roles = ["badge_enter"]

        proposals.append(
            SmartPlannerProposal(
                role=candidate.role,  # type: ignore[arg-type]
                start_word_id=candidate.start_word_id,
                end_word_id=candidate.end_word_id,
                anchor_word_id=candidate.anchor_word_id,
                confidence_tier=(
                    "high"
                    if candidate.role in {"hook", "list_item", "cta"}
                    or candidate.sequence_number is not None
                    else "medium"
                ),
                sequence_number=candidate.sequence_number,
                text_token=text_token,
                scene_token=scene_token,
                visual_asset_ids=visuals,
                visual_anchor_word_ids={
                    asset_id: candidate.suggested_asset_anchor_word_ids.get(
                        asset_id, candidate.anchor_word_id
                    )
                    for asset_id in visuals
                },
                sfx_roles=sfx_roles,
                boundary_token=boundary_token,
                rationale=f"deterministic:{candidate.candidate_id}",
            )
        )
    return proposals


def _merge_agent_with_guards(
    agent: list[SmartPlannerProposal],
    fallback: list[SmartPlannerProposal],
    *,
    prefer_guard_scene: bool = False,
) -> list[SmartPlannerProposal]:
    if not agent:
        return fallback
    fallback_by_span = {
        (
            proposal.role,
            proposal.start_word_id,
            proposal.end_word_id,
            proposal.anchor_word_id,
        ): proposal
        for proposal in fallback
    }
    result: list[SmartPlannerProposal] = []
    accepted: set[tuple[str, str, str, str]] = set()
    for proposal in agent:
        key = (
            proposal.role,
            proposal.start_word_id,
            proposal.end_word_id,
            proposal.anchor_word_id,
        )
        guard = fallback_by_span.get(key)
        if guard is None or key in accepted:
            continue
        accepted.add(key)
        # Deterministic candidates are the minimum viable edit.  The model can
        # refine tokens and choose an allowlisted visual, but omitting a matched
        # pool asset, chapter number, transition, or its clean SFX must not
        # silently strip the authored result.
        guarded_sfx = list(dict.fromkeys([*guard.sfx_roles, *proposal.sfx_roles]))[:3]
        guarded_visuals = (
            list(guard.visual_asset_ids)
            if prefer_guard_scene
            else list(dict.fromkeys([*guard.visual_asset_ids, *proposal.visual_asset_ids]))[:5]
        )
        guarded_anchors = {
            asset_id: guard.visual_anchor_word_ids.get(
                asset_id,
                proposal.visual_anchor_word_ids.get(asset_id, guard.anchor_word_id),
            )
            for asset_id in guarded_visuals
        }
        result.append(
            proposal.model_copy(
                update={
                    "sequence_number": guard.sequence_number
                    if guard.sequence_number is not None
                    else proposal.sequence_number,
                    "text_token": proposal.text_token or guard.text_token,
                    "scene_token": (
                        guard.scene_token
                        if prefer_guard_scene and guard.visual_asset_ids
                        else proposal.scene_token or guard.scene_token
                    ),
                    "visual_asset_ids": guarded_visuals,
                    "visual_anchor_word_ids": guarded_anchors,
                    "sfx_roles": guarded_sfx,
                    "boundary_token": proposal.boundary_token or guard.boundary_token,
                }
            )
        )
    for key, proposal in fallback_by_span.items():
        if key not in accepted:
            result.append(proposal)
    return result


def _event_lanes(
    proposal: SmartPlannerProposal,
    *,
    event_id: str,
    word_ids: list[str],
    visual_asset_id: str | None,
    visual_index: int,
    composition_index: int,
    preset: SmartEditPreset,
) -> list:
    lanes: list = []
    if visual_index == 0:
        lanes.append(
            CaptionEmphasisLane(
                kind="caption_emphasis",
                token={
                    "hook": "hook_lime",
                    "context_shift": "context_lime",
                    "list_item": "list_keyword",
                    "example": "example_soft",
                    "payoff": "payoff_lime",
                    "cta": "cta_lime",
                }[proposal.role],
                baseline_caption_word_ids=word_ids,
            )
        )
        if proposal.text_token == "section_heading" and proposal.sequence_number:
            lanes.append(
                TextLane(
                    kind="text",
                    token="section_heading",
                    transcript_word_ids=word_ids,
                    transform="list_number_from_sequence",
                    sequence_number=proposal.sequence_number,
                )
            )
        elif proposal.text_token == "context_title":
            claimed = word_ids[: min(7, len(word_ids))]
            lanes.append(
                TextLane(
                    kind="text",
                    token="context_title",
                    transcript_word_ids=claimed,
                    transform="verbatim",
                    claimed_word_ids=claimed,
                    caption_visibility="suppress_claimed_span",
                )
            )

    if visual_asset_id:
        if proposal.scene_token == "hook_accumulation":
            # Hook visuals can be discovered across several transcript cues.
            # Allocate zones across the whole composition instead of resetting
            # to the first zone for every proposal, which stacks unrelated
            # assets on top of each other.
            zone = preset.hook_zone_sequence[composition_index % len(preset.hook_zone_sequence)]
            exit_policy = "group_end"
            group_id = "hook_accumulation"
        elif proposal.scene_token == "persistent_badge":
            zone = "badge_top_left"
            exit_policy = "video_end"
            group_id = "persistent_badge"
        else:
            zone = "single_top" if visual_index % 2 == 0 else "single_left"
            exit_policy = "event_end"
            group_id = None
        lanes.append(
            VisualLane(
                kind="visual",
                asset_id=visual_asset_id,
                zone=zone,
                entrance_token="pop_in",
                exit_policy=exit_policy,
                composition_group_id=group_id,
                group_order=composition_index,
            )
        )

    roles = list(proposal.sfx_roles)
    if visual_index > 0:
        roles = [role for role in roles if role.startswith("visual_enter")]
    if roles:
        lanes.append(
            SfxLane(
                kind="sfx",
                role_tokens=roles,
                sync_to_event_id=event_id,
                offset_ms=0,
                gain_token="preset",
            )
        )
    if visual_index == 0 and proposal.boundary_token:
        lanes.append(
            BoundaryEffectLane(kind="boundary_effect", effect_token=proposal.boundary_token)
        )
    return lanes


def _events_from_proposals(
    proposals: list[SmartPlannerProposal],
    *,
    words: list[SmartWord],
    preset: SmartEditPreset,
) -> tuple[list[SmartEditEvent], list[dict[str, str]]]:
    by_id = {word.word_id: word for word in words}
    word_ids = [word.word_id for word in words]
    index_by_id = {word_id: index for index, word_id in enumerate(word_ids)}
    omissions: list[dict[str, str]] = []
    events: list[SmartEditEvent] = []
    collisions: dict[tuple[str, str, str], int] = {}
    hook_composition_index = 0

    for proposal in proposals[: preset.density.max_events]:
        if any(
            word_id not in by_id
            for word_id in (proposal.start_word_id, proposal.end_word_id, proposal.anchor_word_id)
        ):
            omissions.append({"reason": "unknown_word", "anchor": proposal.anchor_word_id})
            continue
        start_index = index_by_id[proposal.start_word_id]
        end_index = index_by_id[proposal.end_word_id]
        if end_index < start_index:
            omissions.append({"reason": "inverted_span", "anchor": proposal.anchor_word_id})
            continue
        span_ids = word_ids[start_index : end_index + 1]
        visual_ids: list[str | None] = list(proposal.visual_asset_ids) or [None]
        for visual_index, visual_id in enumerate(visual_ids):
            composition_index = visual_index
            if proposal.scene_token == "hook_accumulation" and visual_id:
                composition_index = hook_composition_index
                hook_composition_index += 1
            key = (proposal.role, proposal.start_word_id, proposal.end_word_id)
            collision = collisions.get(key, 0)
            collisions[key] = collision + 1
            event_id = build_event_id(
                preset_version=f"{preset.preset_id}/{preset.version}",
                role=proposal.role,
                start_word_id=proposal.start_word_id,
                end_word_id=proposal.end_word_id,
                collision_ordinal=collision,
            )
            visual_anchor_word_id = (
                proposal.visual_anchor_word_ids.get(visual_id, proposal.anchor_word_id)
                if visual_id
                else proposal.anchor_word_id
            )
            if visual_anchor_word_id not in by_id:
                omissions.append(
                    {"reason": "unknown_visual_anchor", "anchor": visual_anchor_word_id}
                )
                continue
            anchor_ms = by_id[visual_anchor_word_id].start_ms
            # The transcript word is the temporal source of truth.  A generic
            # per-cue stagger made later assets appear before their own names.
            stagger_ms = 0
            active_start_ms = max(0, anchor_ms + stagger_ms)
            if proposal.scene_token == "hook_accumulation":
                active_end_ms = max(
                    active_start_ms + 500,
                    round(preset.density.hook_window_s * 1000),
                )
            elif proposal.scene_token == "persistent_badge":
                active_end_ms = max(active_start_ms + 500, words[-1].end_ms)
            elif visual_id:
                active_end_ms = min(
                    words[-1].end_ms,
                    active_start_ms + round(preset.density.normal_visual_duration_s * 1000),
                )
            else:
                active_end_ms = max(
                    by_id[proposal.end_word_id].end_ms,
                    active_start_ms + 500,
                )
            lanes = _event_lanes(
                proposal,
                event_id=event_id,
                word_ids=span_ids,
                visual_asset_id=visual_id,
                visual_index=visual_index,
                composition_index=composition_index,
                preset=preset,
            )
            try:
                events.append(
                    SmartEditEvent(
                        event_id=event_id,
                        role=proposal.role,
                        start_word_id=proposal.start_word_id,
                        end_word_id=proposal.end_word_id,
                        anchor=EventAnchor(
                            word_id=visual_anchor_word_id,
                            offset_ms=stagger_ms,
                        ),
                        active_start_ms=active_start_ms,
                        active_end_ms=max(active_start_ms + 1, active_end_ms),
                        confidence_tier=proposal.confidence_tier,
                        spatial_owner=proposal.scene_token
                        or ("smart_title" if proposal.text_token else None),
                        enabled=True,
                        lanes=lanes,
                        provenance=[PLANNER_VERSION, f"preset:{preset.preset_id}/{preset.version}"],
                    )
                )
            except Exception as exc:
                omissions.append(
                    {
                        "reason": "event_validation",
                        "anchor": proposal.anchor_word_id,
                        "detail": str(exc)[:120],
                    }
                )
    hook_events = [
        event
        for event in events
        if any(
            isinstance(lane, VisualLane) and lane.composition_group_id == "hook_accumulation"
            for lane in event.lanes
        )
    ]
    if hook_events:
        group_end_ms = min(
            words[-1].end_ms,
            max(
                round(preset.density.hook_window_s * 1000),
                max(event.active_start_ms for event in hook_events)
                + round(preset.density.hook_group_hold_s * 1000),
            ),
        )
        events = [
            event.model_copy(update={"active_end_ms": group_end_ms})
            if event in hook_events and group_end_ms > event.active_start_ms
            else event
            for event in events
        ]
    events.sort(key=lambda event: (event.active_start_ms, event.event_id))
    return events, omissions


def _chapter_markers_v2(
    words: list[SmartWord],
    baseline: list[BaselineCaptionCue],
) -> tuple[list[tuple[int, int, int]], int | None]:
    """Detect every grounded list marker without a cascading sequence state."""

    declared_count, declaration_index = _declared_count(words)
    cue_position: dict[str, tuple[int, int, int]] = {}
    for cue_index, cue in enumerate(baseline):
        for position, word_id in enumerate(cue.word_ids):
            cue_position[word_id] = (cue_index, position, len(cue.word_ids))

    # confidence, word index. Direct ordinal/numbered markers outrank bare
    # cardinals even when a weak incidental cardinal appeared earlier.
    chosen: dict[int, tuple[int, int]] = {}
    for index, word in enumerate(words):
        normalized = word.normalized_text
        _, position, cue_length = cue_position.get(word.word_id, (-1, -1, -1))
        direct = _DIRECT_ORDINALS.get(normalized)
        is_numbered_marker = bool(re.fullmatch(r"[1-9][.)]", word.spoken_text.strip()))
        if direct is None and is_numbered_marker:
            direct = int(word.spoken_text.strip()[0])
        if direct is not None:
            if declared_count is None and position != 0 and not is_numbered_marker:
                continue
            if declared_count is None or direct <= declared_count:
                previous = chosen.get(direct)
                if previous is None or previous[0] < 2:
                    chosen[direct] = (2, index)
            continue

        cardinal = _CARDINALS.get(normalized)
        if (
            cardinal is None
            or declared_count is None
            or index <= declaration_index
            or cardinal > declared_count
        ):
            continue
        edge = position in {0, cue_length - 1}
        pause_before = index > 0 and word.start_ms - words[index - 1].end_ms >= 250
        pause_after = index + 1 < len(words) and words[index + 1].start_ms - word.end_ms >= 250
        punctuation_boundary = index > 0 and words[index - 1].spoken_text.rstrip().endswith(
            (":", ";", ".", "?")
        )
        if edge or pause_before or pause_after or punctuation_boundary:
            chosen.setdefault(cardinal, (1, index))

    marker_indexes = {index for _, index in chosen.values()}
    markers: list[tuple[int, int, int]] = []
    for number, (_, marker_index) in chosen.items():
        keyword_index = _marker_keyword_index(words, marker_index, marker_indexes)
        markers.append((number, marker_index, keyword_index))
    markers.sort(key=lambda marker: marker[1])
    return markers, declared_count


def _marker_keyword_index(
    words: list[SmartWord], marker_index: int, marker_indexes: set[int]
) -> int:
    """First non-stopword after a chapter marker (the spoken heading word)."""

    keyword_index = marker_index
    previous_end_ms = words[marker_index].end_ms
    for candidate_index in range(marker_index + 1, min(len(words), marker_index + 5)):
        candidate = words[candidate_index]
        if candidate_index in marker_indexes or candidate.start_ms - previous_end_ms > 900:
            break
        previous_end_ms = candidate.end_ms
        if (
            candidate.normalized_text not in _KEYWORD_STOP
            and candidate.normalized_text not in _LIST_TERMS
            and candidate.normalized_text not in _CARDINALS
        ):
            keyword_index = candidate_index
            break
    return keyword_index


@dataclass(frozen=True)
class _SceneHints:
    """Validated scene-matcher output mapped onto transcript word indexes.

    matches: (word_index, asset_id, anchor_word_id) — word-anchored visual
    matches in transcript order. chapter_tags: word_index → spoken sequence
    number. role_tags: word_index → semantic role (never hook/list_item).
    """

    matches: list[tuple[int, str, str]]
    chapter_tags: dict[int, int]
    role_tags: dict[int, SemanticRole]

    @property
    def hinted_asset_ids(self) -> set[str]:
        return {asset_id for _, asset_id, _ in self.matches}


def _merge_hint_chapters(
    words: list[SmartWord],
    markers: list[tuple[int, int, int]],
    hints: _SceneHints | None,
) -> list[tuple[int, int, int]]:
    """Union agent-tagged chapters with vocab-detected ones.

    The vocab tables stay authoritative when they fire (transcript-derived
    facts); the agent ADDS chapters they cannot see — any language, spoken
    numbers the dictionaries don't cover. Conflicts dedupe by sequence number
    and by word index; the detected marker wins.
    """

    if hints is None or not hints.chapter_tags:
        return markers
    detected_numbers = {number for number, _, _ in markers}
    detected_indexes = {index for _, index, _ in markers}
    merged = list(markers)
    marker_indexes = set(detected_indexes) | set(hints.chapter_tags.keys())
    for word_index, number in sorted(hints.chapter_tags.items()):
        if number in detected_numbers or word_index in detected_indexes:
            continue
        keyword_index = _marker_keyword_index(words, word_index, marker_indexes)
        merged.append((number, word_index, keyword_index))
        detected_numbers.add(number)
    merged.sort(key=lambda marker: marker[1])
    return merged


def _semantic_hook_end_index_v2(
    words: list[SmartWord],
    baseline: list[BaselineCaptionCue],
    chapter_markers: list[tuple[int, int, int]],
    preset: SmartEditPreset,
) -> int:
    if not words:
        return 0
    index_by_id = {word.word_id: index for index, word in enumerate(words)}
    cap_s = preset.density.hook_max_duration_s or preset.density.hook_window_s
    cap_ms = words[0].start_ms + round(cap_s * 1000)
    cap_index = max(
        0,
        max(
            (index for index, word in enumerate(words) if word.start_ms < cap_ms),
            default=0,
        ),
    )
    transitions: list[int] = []
    for cue_index, cue in enumerate(baseline[1:], start=1):
        first_index = index_by_id[cue.word_ids[0]]
        if first_index > cap_index:
            break
        folded = _fold(cue.display_text)
        prior_question = baseline[cue_index - 1].display_text.rstrip().endswith("?")
        if any(folded.startswith(prefix) for prefix in _CONTEXT_PREFIXES) or prior_question:
            transitions.append(first_index)
    transitions.extend(marker_index for _, marker_index, _ in chapter_markers)
    valid = [index for index in transitions if 0 < index <= cap_index]
    if valid:
        return max(0, min(valid) - 1)
    return min(cap_index, len(words) - 1)


def _run_scene_matcher(
    words: list[SmartWord],
    assets: list[SmartPlannerAsset],
    *,
    language: str,
    job_id: str | None,
    use_agent: bool,
) -> tuple[_SceneHints | None, dict[str, Any]]:
    """Run the word→visual matching brain; fail open to the vocab heuristics.

    Returns (hints, receipt). hints=None means "behave exactly as before the
    agent existed" — kill switch off, no API key, or any failure. The agent
    runs even with an empty asset pool: chapter/role tags are language-
    agnostic value on their own.
    """

    from app.config import settings  # noqa: PLC0415

    if not use_agent:
        return None, {"status": "disabled_use_agent"}
    if not getattr(settings, "smart_scene_matcher_enabled", True):
        return None, {"status": "disabled_by_flag"}
    if not settings.gemini_api_key:
        return None, {"status": "no_api_key"}
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.scene_matcher import SceneMatcherAgent, SceneMatcherInput  # noqa: PLC0415

        output = SceneMatcherAgent(default_client()).run(
            SceneMatcherInput(
                words=[{"word_id": word.word_id, "text": word.display_text} for word in words],
                assets=assets,
                language=language,
            ),
            ctx=RunContext(job_id=job_id),
        )
        index_by_id = {word.word_id: index for index, word in enumerate(words)}
        matches = sorted(
            (index_by_id[match.anchor_word_id], match.asset_id, match.anchor_word_id)
            for match in output.matches
            if match.anchor_word_id in index_by_id
        )
        chapter_tags: dict[int, int] = {}
        role_tags: dict[int, SemanticRole] = {}
        for tag in output.cue_tags:
            index = index_by_id.get(tag.anchor_word_id)
            if index is None or index == 0:
                # The very first word is the hook, never a tag anchor.
                continue
            if tag.role == "list_item" and tag.sequence_number is not None:
                chapter_tags[index] = tag.sequence_number
            elif tag.role in {"context_shift", "payoff", "cta"}:
                role_tags[index] = tag.role
        hints = _SceneHints(matches=matches, chapter_tags=chapter_tags, role_tags=role_tags)
        return hints, {
            "status": "ok",
            "matches": len(matches),
            "chapter_tags": len(chapter_tags),
            "role_tags": len(role_tags),
        }
    except Exception as exc:  # noqa: BLE001 — advisory brain, never blocks a render
        return None, {"status": "failed_open", "error_class": type(exc).__name__}


def _semantic_timeline_v2(
    words: list[SmartWord],
    baseline: list[BaselineCaptionCue],
    assets: list[SmartPlannerAsset],
    preset: SmartEditPreset,
    scene_hints: _SceneHints | None = None,
) -> _V2SemanticTimeline:
    index_by_id = {word.word_id: index for index, word in enumerate(words)}
    chapter_markers, declared_count = _chapter_markers_v2(words, baseline)
    chapter_markers = _merge_hint_chapters(words, chapter_markers, scene_hints)
    hook_end_index = _semantic_hook_end_index_v2(words, baseline, chapter_markers, preset)
    hook_words = words[: hook_end_index + 1]
    # Word-anchored agent matches take precedence over the token-overlap
    # heuristic: the heuristic can only pair a visual with a literally-shared
    # token, which cross-matches generic terms ("flag") and misses world-
    # knowledge pairs ("Argentina #10 jersey" ↔ "Messi"). Assets the agent
    # never mentioned still fall through to the heuristic.
    hinted_ids = scene_hints.hinted_asset_ids if scene_hints else set()

    def _range_matches(lo: int, hi: int) -> list[tuple[str, str]]:
        if scene_hints is None:
            return []
        seen: set[str] = set()
        ranged: list[tuple[str, str]] = []
        for word_index, asset_id, anchor_word_id in scene_hints.matches:
            if lo <= word_index <= hi and asset_id not in seen:
                seen.add(asset_id)
                ranged.append((asset_id, anchor_word_id))
        return ranged

    hook_matches = _range_matches(0, hook_end_index) + [
        match
        for match in _matching_assets(hook_words, assets, preset)
        if match[0] not in hinted_ids
    ]
    candidates: list[SmartPlannerCandidate] = [
        SmartPlannerCandidate(
            candidate_id=_candidate_id("hook", words[0].word_id, 0),
            role="hook",
            start_word_id=words[0].word_id,
            end_word_id=words[hook_end_index].word_id,
            anchor_word_id=words[0].word_id,
            evidence="semantic_hook",
            suggested_asset_ids=[asset_id for asset_id, _ in hook_matches],
            suggested_asset_anchor_word_ids=dict(hook_matches),
        )
    ]
    role_by_word_id: dict[str, SemanticRole] = {word.word_id: "example" for word in words}
    for word in hook_words:
        role_by_word_id[word.word_id] = "hook"
    boundary_after_word_ids = {words[hook_end_index].word_id}

    cue_ranges: list[tuple[int, int]] = []
    for cue in baseline:
        cue_word_indexes = [index_by_id[wid] for wid in cue.word_ids if wid in index_by_id]
        if cue_word_indexes:
            cue_ranges.append((min(cue_word_indexes), max(cue_word_indexes)))

    chapter_word_indexes: set[int] = set()
    claimed_chapter_assets: set[str] = set()
    for ordinal, (number, marker_index, title_end_index) in enumerate(chapter_markers, start=1):
        chapter_words = words[marker_index : title_end_index + 1]
        chapter_word_indexes.update(range(marker_index, title_end_index + 1))
        for word in chapter_words:
            role_by_word_id[word.word_id] = "list_item"
        boundary_after_word_ids.add(chapter_words[-1].word_id)
        # A visual named INSIDE a chapter heading ("number one Spain") must
        # attach here: cues overlapping a chapter are skipped below, so this
        # candidate is the only carrier for hint matches in that range. Absorb
        # the FULL span of every overlapping cue — a hint anchored later in
        # the same sentence ("number four ... Elliot Anderson") would
        # otherwise vanish with the skipped cue.
        span_lo, span_hi = marker_index, title_end_index
        for cue_lo, cue_hi in cue_ranges:
            if cue_lo <= title_end_index and cue_hi >= marker_index:
                span_lo, span_hi = min(span_lo, cue_lo), max(span_hi, cue_hi)
        chapter_matches = [
            match
            for match in _range_matches(max(span_lo, hook_end_index + 1), span_hi)
            if match[0] not in claimed_chapter_assets
        ]
        claimed_chapter_assets.update(asset_id for asset_id, _ in chapter_matches)
        candidates.append(
            SmartPlannerCandidate(
                candidate_id=_candidate_id("list_item", chapter_words[0].word_id, ordinal),
                role="list_item",
                start_word_id=chapter_words[0].word_id,
                end_word_id=chapter_words[-1].word_id,
                anchor_word_id=chapter_words[0].word_id,
                sequence_number=number,
                evidence=" ".join(word.display_text for word in chapter_words)[:200],
                suggested_asset_ids=[asset_id for asset_id, _ in chapter_matches],
                suggested_asset_anchor_word_ids=dict(chapter_matches),
            )
        )

    last_end_ms = 0
    for cue_index, cue in enumerate(baseline):
        cue_words = [words[index_by_id[word_id]] for word_id in cue.word_ids]
        cue_indexes = {index_by_id[word.word_id] for word in cue_words}
        if (
            not cue_words
            or max(cue_indexes) <= hook_end_index
            or cue_indexes & chapter_word_indexes
        ):
            last_end_ms = cue_words[-1].end_ms if cue_words else last_end_ms
            continue
        folded = _fold(cue.display_text)
        pause_ms = max(0, cue_words[0].start_ms - last_end_ms)
        last_end_ms = cue_words[-1].end_ms
        hint_role: SemanticRole | None = None
        if scene_hints is not None:
            hint_role = next(
                (
                    scene_hints.role_tags[index]
                    for index in sorted(cue_indexes)
                    if index in scene_hints.role_tags
                ),
                None,
            )
        role: SemanticRole | None = None
        if min(cue_indexes) == hook_end_index + 1:
            role = "context_shift"
        elif hint_role is not None:
            # Language-agnostic semantic tag from the scene matcher — the
            # vocab prefixes below only ever fire on Turkish.
            role = hint_role
        elif any(folded.startswith(prefix) for prefix in _CTA_TERMS) or any(
            term in folded for term in _CTA_TERMS
        ):
            role = "cta"
        elif any(folded.startswith(prefix) for prefix in _PAYOFF_PREFIXES):
            role = "payoff"
        elif any(folded.startswith(prefix) for prefix in _EXAMPLE_PREFIXES):
            role = "example"
        elif any(folded.startswith(prefix) for prefix in _CONTEXT_PREFIXES) or pause_ms >= 1150:
            role = "context_shift"
        asset_matches = _range_matches(min(cue_indexes), max(cue_indexes)) + [
            match
            for match in _matching_assets(cue_words, assets, preset)
            if match[0] not in hinted_ids
        ]
        if role is None and asset_matches:
            role = "example"
        if role is None:
            continue
        for word in cue_words:
            role_by_word_id[word.word_id] = role
        candidates.append(
            SmartPlannerCandidate(
                candidate_id=_candidate_id(role, cue_words[0].word_id, cue_index + 100),
                role=role,
                start_word_id=cue_words[0].word_id,
                end_word_id=cue_words[-1].word_id,
                anchor_word_id=cue_words[0].word_id,
                evidence=" ".join(word.display_text for word in cue_words)[:200],
                suggested_asset_ids=[asset_id for asset_id, _ in asset_matches],
                suggested_asset_anchor_word_ids=dict(asset_matches),
            )
        )

    # A scene-hinted asset is CONTENT anchored to a spoken word, never brand
    # furniture — Gemini describing a jersey crest as "badge" must not pin a
    # player photo to a corner for the whole video (2026-07-21 Messi report).
    badge_assets = [
        asset.asset_id
        for asset in assets
        if asset.asset_id not in hinted_ids
        and (
            {"badge", "skip", "skipad"} & _asset_terms(asset)
            or "skip ad" in _fold(" ".join((asset.subject, asset.filename, asset.on_screen_text)))
        )
    ]
    if badge_assets:
        anchor = words[min(1, hook_end_index)]
        candidates.append(
            SmartPlannerCandidate(
                candidate_id=_candidate_id("hook", anchor.word_id, 999),
                role="hook",
                start_word_id=anchor.word_id,
                end_word_id=anchor.word_id,
                anchor_word_id=anchor.word_id,
                evidence="persistent badge asset",
                suggested_asset_ids=badge_assets[:1],
            )
        )

    detected = [number for number, _, _ in chapter_markers]
    detected_set = set(detected)
    missing = (
        [number for number in range(1, declared_count + 1) if number not in detected_set]
        if declared_count
        else []
    )
    return _V2SemanticTimeline(
        candidates=candidates[:80],
        role_by_word_id=role_by_word_id,
        boundary_after_word_ids=boundary_after_word_ids,
        hook_end_word_id=words[hook_end_index].word_id,
        declared_chapter_count=declared_count,
        detected_chapters=detected,
        missing_chapters=missing,
        chapter_order_valid=detected == sorted(detected),
    )


def _baseline_from_semantic_cues(cues: list[dict[str, Any]]) -> list[BaselineCaptionCue]:
    baseline: list[BaselineCaptionCue] = []
    for index, cue in enumerate(cues):
        word_ids = [str(word_id) for word_id in cue.get("smart_word_ids") or []]
        if not word_ids:
            continue
        baseline.append(
            BaselineCaptionCue(
                cue_id=f"smart-cue-{index + 1:03d}",
                word_ids=word_ids,
                display_text=str(cue.get("text") or "").strip(),
            )
        )
    return baseline


def _fallback_proposals_v2(
    candidates: list[SmartPlannerCandidate],
    words: list[SmartWord],
    assets: list[SmartPlannerAsset],
    preset: SmartEditPreset,
    *,
    hook_end_word_id: str,
) -> list[SmartPlannerProposal]:
    index_by_id = {word.word_id: index for index, word in enumerate(words)}
    hook_end_index = index_by_id[hook_end_word_id]
    assets_by_id = {asset.asset_id: asset for asset in assets}
    proposals: list[SmartPlannerProposal] = []
    for candidate in candidates:
        is_badge = "persistent badge" in candidate.evidence
        in_hook = (
            index_by_id[candidate.start_word_id] <= hook_end_index and candidate.role == "hook"
        )
        visuals = list(candidate.suggested_asset_ids)
        scene_token: str | None = None
        if visuals:
            if is_badge:
                scene_token = "persistent_badge"
                visuals = visuals[:1]
            elif in_hook:
                scene_token = "hook_accumulation"
                visuals = visuals[: preset.density.hook_max_visuals]
            elif len(visuals) >= 2:
                scene_token = "example_pair"
                visuals = visuals[:2]
            elif assets_by_id.get(visuals[0]) and assets_by_id[visuals[0]].kind == "video":
                scene_token = "fullscreen_cutaway"
                visuals = visuals[:1]
            else:
                scene_token = "single_example"
                visuals = visuals[:1]

        text_token = None
        sfx_roles: list[str] = []
        boundary_token = None
        if candidate.role == "list_item" and candidate.sequence_number:
            text_token = "section_heading"
            sfx_roles = ["chapter_number_pop", "keyword_typewriter_tick"]
        elif candidate.role == "context_shift":
            text_token = "context_title"
            boundary_token = "horizontal_motion_blur"
            sfx_roles = ["transition_whip", "keyword_typewriter_tick"]
        elif candidate.role == "cta":
            sfx_roles = ["cta_click"]
        if visuals:
            sfx_roles = [
                *sfx_roles,
                "visual_enter_accent" if in_hook else "visual_enter_soft",
            ][:3]
        if is_badge:
            sfx_roles = ["badge_enter"]
        proposals.append(
            SmartPlannerProposal(
                role=candidate.role,  # type: ignore[arg-type]
                start_word_id=candidate.start_word_id,
                end_word_id=candidate.end_word_id,
                anchor_word_id=candidate.anchor_word_id,
                confidence_tier=(
                    "high"
                    if candidate.role in {"hook", "list_item", "cta"}
                    or candidate.sequence_number is not None
                    else "medium"
                ),
                sequence_number=candidate.sequence_number,
                text_token=text_token,
                scene_token=scene_token,
                visual_asset_ids=visuals,
                visual_anchor_word_ids={
                    asset_id: candidate.suggested_asset_anchor_word_ids.get(
                        asset_id, candidate.anchor_word_id
                    )
                    for asset_id in visuals
                },
                sfx_roles=sfx_roles,
                boundary_token=boundary_token,
                rationale=f"deterministic-v2:{candidate.candidate_id}",
            )
        )
    return proposals


def _event_lanes_v2(
    proposal: SmartPlannerProposal,
    *,
    event_id: str,
    word_ids: list[str],
    visual_asset_id: str | None,
    visual_index: int,
    composition_index: int,
    preset: SmartEditPreset,
    include_camera: bool,
    include_audio: bool,
) -> list:
    lanes: list = []
    if visual_index == 0:
        lanes.append(
            CaptionEmphasisLane(
                kind="caption_emphasis",
                token={
                    "hook": "hook_lime",
                    "context_shift": "context_lime",
                    "list_item": "list_keyword",
                    "example": "example_soft",
                    "payoff": "payoff_lime",
                    "cta": "cta_lime",
                }[proposal.role],
                baseline_caption_word_ids=word_ids,
            )
        )
        if proposal.text_token == "section_heading" and proposal.sequence_number:
            lanes.append(
                TextLane(
                    kind="text",
                    token="section_heading",
                    transcript_word_ids=word_ids,
                    transform="list_number_from_sequence",
                    sequence_number=proposal.sequence_number,
                    claimed_word_ids=word_ids,
                    caption_visibility="suppress_claimed_span",
                )
            )
        elif proposal.text_token == "context_title":
            claimed = word_ids[: min(7, len(word_ids))]
            lanes.append(
                TextLane(
                    kind="text",
                    token="context_title",
                    transcript_word_ids=claimed,
                    transform="verbatim",
                    claimed_word_ids=claimed,
                    caption_visibility="suppress_claimed_span",
                )
            )
        if include_camera and preset.camera:
            lanes.append(
                CameraLane(
                    kind="camera",
                    token=preset.camera.token,
                    intensity_token=preset.camera.intensity_token,
                )
            )
        if include_audio and preset.audio_treatment:
            lanes.append(
                AudioTreatmentLane(
                    kind="audio_treatment",
                    token=preset.audio_treatment.token,
                    selection_token=preset.audio_treatment.selection_token,
                )
            )

    if visual_asset_id and proposal.scene_token:
        scene = preset.scene_layouts.get(proposal.scene_token)
        if scene:
            zone = scene.zones[composition_index % len(scene.zones)]
            lanes.append(
                VisualLane(
                    kind="visual",
                    asset_id=visual_asset_id,
                    zone=zone,
                    entrance_token=scene.entrance_token,
                    exit_policy=scene.exit_policy,
                    composition_group_id=scene.composition_group_id,
                    group_order=composition_index,
                )
            )

    roles = list(proposal.sfx_roles)
    if visual_index > 0:
        roles = [role for role in roles if role.startswith("visual_enter")]
    if roles:
        lanes.append(
            SfxLane(
                kind="sfx",
                role_tokens=roles,
                sync_to_event_id=event_id,
                offset_ms=0,
                gain_token="preset",
            )
        )
    if visual_index == 0 and proposal.boundary_token:
        lanes.append(
            BoundaryEffectLane(kind="boundary_effect", effect_token=proposal.boundary_token)
        )
    return lanes


def _events_from_proposals_v2(
    proposals: list[SmartPlannerProposal],
    *,
    words: list[SmartWord],
    preset: SmartEditPreset,
    hook_end_word_id: str,
    scene_hints: _SceneHints | None = None,
) -> tuple[list[SmartEditEvent], list[dict[str, str]]]:
    by_id = {word.word_id: word for word in words}
    word_ids = [word.word_id for word in words]
    index_by_id = {word_id: index for index, word_id in enumerate(word_ids)}
    hook_end_ms = by_id[hook_end_word_id].end_ms
    omissions: list[dict[str, str]] = []
    events: list[SmartEditEvent] = []
    collisions: dict[tuple[str, str, str], int] = {}
    hook_composition_index = 0
    audio_added = False
    last_camera_ms = -1_000_000
    hint_anchor_ids: dict[str, set[str]] = {}
    if scene_hints is not None:
        for _, hint_asset_id, hint_anchor_word_id in scene_hints.matches:
            hint_anchor_ids.setdefault(hint_asset_id, set()).add(hint_anchor_word_id)

    # Truncate AFTER the chronological sort: merge appends un-matched
    # deterministic fallbacks (chapter headings included) at the END, so a
    # pre-sort cut silently dropped whole chapters instead of the latest
    # low-value events.
    ordered_proposals = sorted(
        proposals,
        key=lambda proposal: (
            index_by_id.get(proposal.start_word_id, len(words)),
            index_by_id.get(proposal.anchor_word_id, len(words)),
        ),
    )[: preset.density.max_events]
    for proposal in ordered_proposals:
        if any(
            word_id not in by_id
            for word_id in (proposal.start_word_id, proposal.end_word_id, proposal.anchor_word_id)
        ):
            omissions.append({"reason": "unknown_word", "anchor": proposal.anchor_word_id})
            continue
        start_index = index_by_id[proposal.start_word_id]
        end_index = index_by_id[proposal.end_word_id]
        if end_index < start_index:
            omissions.append({"reason": "inverted_span", "anchor": proposal.anchor_word_id})
            continue
        span_ids = word_ids[start_index : end_index + 1]
        visual_ids = list(proposal.visual_asset_ids)
        if visual_ids:
            scene = preset.scene_layouts.get(proposal.scene_token or "")
            if scene is None or not scene.min_assets <= len(visual_ids) <= scene.max_assets:
                omissions.append(
                    {"reason": "invalid_scene_cardinality", "anchor": proposal.anchor_word_id}
                )
                visual_ids = []
        expanded_visual_ids: list[str | None] = visual_ids or [None]
        for visual_index, visual_id in enumerate(expanded_visual_ids):
            composition_index = visual_index
            if proposal.scene_token == "hook_accumulation" and visual_id:
                composition_index = hook_composition_index
                hook_composition_index += 1
            key = (proposal.role, proposal.start_word_id, proposal.end_word_id)
            collision = collisions.get(key, 0)
            collisions[key] = collision + 1
            event_id = build_event_id(
                preset_version=f"{preset.preset_id}/{preset.version}",
                role=proposal.role,
                start_word_id=proposal.start_word_id,
                end_word_id=proposal.end_word_id,
                collision_ordinal=collision,
            )
            visual_anchor_word_id = (
                proposal.visual_anchor_word_ids.get(visual_id, proposal.anchor_word_id)
                if visual_id
                else proposal.anchor_word_id
            )
            anchor_known = visual_anchor_word_id in by_id
            anchor_in_span = anchor_known and (
                start_index <= index_by_id[visual_anchor_word_id] <= end_index
            )
            # A scene-matcher anchor is transcript ground truth even when the
            # carrying proposal's text span doesn't reach it — a chapter
            # heading absorbs visuals named later in the same sentence
            # ("number four ... Elliot Anderson"). Only unhinted out-of-span
            # anchors are the hallucination class this wall exists for.
            anchor_hint_grounded = (
                visual_id is not None
                and anchor_known
                and visual_anchor_word_id in hint_anchor_ids.get(visual_id, ())
            )
            if not anchor_in_span and not anchor_hint_grounded:
                omissions.append(
                    {"reason": "unknown_visual_anchor", "anchor": visual_anchor_word_id}
                )
                continue
            event_anchor_word_id = visual_anchor_word_id
            if not anchor_in_span:
                # The event model requires its anchor inside the text span;
                # the true spoken time still drives active_start_ms below.
                event_anchor_word_id = (
                    proposal.end_word_id
                    if index_by_id[visual_anchor_word_id] > end_index
                    else proposal.start_word_id
                )
            active_start_ms = by_id[visual_anchor_word_id].start_ms
            if proposal.scene_token == "hook_accumulation":
                active_end_ms = min(
                    words[-1].end_ms,
                    hook_end_ms + round(preset.density.hook_group_hold_s * 1000),
                )
            elif proposal.scene_token == "persistent_badge":
                active_end_ms = words[-1].end_ms
            elif visual_id:
                active_end_ms = min(
                    words[-1].end_ms,
                    active_start_ms + round(preset.density.normal_visual_duration_s * 1000),
                )
            else:
                active_end_ms = max(by_id[proposal.end_word_id].end_ms, active_start_ms + 500)

            include_camera = False
            if (
                visual_index == 0
                and preset.camera
                and proposal.role in preset.camera.eligible_roles
                and active_start_ms - last_camera_ms >= preset.camera.cooldown_ms
            ):
                include_camera = True
                last_camera_ms = active_start_ms
            include_audio = (
                visual_index == 0
                and not audio_added
                and proposal.role == "hook"
                and proposal.scene_token != "persistent_badge"
                and preset.audio_treatment is not None
            )
            audio_added = audio_added or include_audio
            lanes = _event_lanes_v2(
                proposal,
                event_id=event_id,
                word_ids=span_ids,
                visual_asset_id=visual_id,
                visual_index=visual_index,
                composition_index=composition_index,
                preset=preset,
                include_camera=include_camera,
                include_audio=include_audio,
            )
            try:
                events.append(
                    SmartEditEvent(
                        event_id=event_id,
                        role=proposal.role,
                        start_word_id=proposal.start_word_id,
                        end_word_id=proposal.end_word_id,
                        anchor=EventAnchor(word_id=event_anchor_word_id, offset_ms=0),
                        active_start_ms=active_start_ms,
                        active_end_ms=max(active_start_ms + 1, active_end_ms),
                        confidence_tier=proposal.confidence_tier,
                        spatial_owner=proposal.scene_token
                        or ("smart_title" if proposal.text_token else None),
                        enabled=True,
                        lanes=lanes,
                        provenance=[
                            PLANNER_VERSION_V2,
                            f"preset:{preset.preset_id}/{preset.version}",
                        ],
                    )
                )
            except Exception as exc:
                omissions.append(
                    {
                        "reason": "event_validation",
                        "anchor": proposal.anchor_word_id,
                        "detail": str(exc)[:120],
                    }
                )
    events.sort(key=lambda event: (event.active_start_ms, event.event_id))
    return events, omissions


def plan_smart_captions(
    cues: list[dict[str, Any]],
    *,
    preset_version: str,
    language: str,
    preset_id: str = "cigdem",
    assets: list[dict[str, Any]] | None = None,
    job_id: str | None = None,
    use_agent: bool = True,
) -> SmartPlanBuild | None:
    """Build a complete Smart event plan from corrected word-timed captions."""

    preset = load_preset(preset_id, preset_version)
    if preset.version == "v2":
        normalized_words, source_baseline, _ = _normalize_captions(cues, language=language)
        if not normalized_words or not source_baseline:
            return None
        planner_assets = _coerce_assets(assets)
        scene_hints, scene_receipt = _run_scene_matcher(
            normalized_words,
            planner_assets,
            language=language,
            job_id=job_id,
            use_agent=use_agent,
        )
        semantic = _semantic_timeline_v2(
            normalized_words,
            source_baseline,
            planner_assets,
            preset,
            scene_hints=scene_hints,
        )
        smart_cues = build_semantic_caption_cues(
            normalized_words,
            preset.caption,
            role_by_word_id=semantic.role_by_word_id,
            boundary_after_word_ids=semantic.boundary_after_word_ids,
        )
        baseline = _baseline_from_semantic_cues(smart_cues)
        if not baseline:
            return None
        fallback = _fallback_proposals_v2(
            semantic.candidates,
            normalized_words,
            planner_assets,
            preset,
            hook_end_word_id=semantic.hook_end_word_id,
        )
        proposals: list[SmartPlannerProposal] = []
        agent_proposal_count = 0
        planner_source = "deterministic"
        if use_agent and semantic.candidates:
            try:
                from app.agents._model_client import default_client  # noqa: PLC0415
                from app.agents._runtime import RunContext  # noqa: PLC0415
                from app.config import settings  # noqa: PLC0415

                if settings.gemini_api_key:
                    output = SmartEditPlannerAgent(default_client()).run(
                        SmartEditPlannerInput(
                            words=[
                                {
                                    "word_id": word.word_id,
                                    "text": word.display_text,
                                    "normalized": word.normalized_text,
                                }
                                for word in normalized_words
                            ],
                            candidates=semantic.candidates,
                            assets=planner_assets,
                            preset_id=preset.preset_id,
                            preset_version=preset.version,
                            language=language,
                        ),
                        ctx=RunContext(job_id=job_id),
                    )
                    proposals = output.proposals
                    agent_proposal_count = len(proposals)
                    planner_source = "agent" if proposals else "deterministic_rejected_agent"
            except Exception:
                proposals = []
        proposals = _merge_agent_with_guards(
            proposals,
            fallback,
            prefer_guard_scene=True,
        )
        events, omissions = _events_from_proposals_v2(
            proposals,
            words=normalized_words,
            preset=preset,
            hook_end_word_id=semantic.hook_end_word_id,
            scene_hints=scene_hints,
        )
        document = SmartEditPlanDocument(
            schema_version=SMART_EDIT_SCHEMA_VERSION_V2,
            preset_id=preset.preset_id,
            preset_version=preset.version,
            baseline_captions=baseline,
            events=events,
        )
        return SmartPlanBuild(
            normalized_words=normalized_words,
            caption_cues=smart_cues,
            document=document,
            planner_versions={
                "semantic_planner": PLANNER_VERSION_V2,
                "decision_source": planner_source,
            },
            validation_receipt={
                "valid": True,
                "normalized_word_count": len(normalized_words),
                "baseline_caption_count": len(baseline),
                "candidate_count": len(semantic.candidates),
                "agent_grounded_proposal_count": agent_proposal_count,
                "event_count": len(events),
                "semantic_hook_end_word_id": semantic.hook_end_word_id,
                "declared_chapter_count": semantic.declared_chapter_count,
                "detected_chapters": semantic.detected_chapters,
                "missing_chapters": semantic.missing_chapters,
                "chapter_order_valid": semantic.chapter_order_valid,
                "omissions": omissions,
                "scene_matcher": scene_receipt,
                "roles": {
                    role: sum(event.role == role for event in events)
                    for role in ("hook", "context_shift", "list_item", "example", "payoff", "cta")
                },
            },
        )

    smart_cues = build_smart_caption_cues(cues, preset.caption)
    normalized_words, baseline, _ = _normalize_captions(smart_cues, language=language)
    if not normalized_words or not baseline:
        return None
    planner_assets = _coerce_assets(assets)
    candidates = extract_candidates(normalized_words, baseline, planner_assets, preset)
    fallback = _fallback_proposals(candidates, normalized_words, preset)
    proposals: list[SmartPlannerProposal] = []
    agent_proposal_count = 0
    planner_source = "deterministic"
    if use_agent and candidates:
        try:
            from app.agents._model_client import default_client  # noqa: PLC0415
            from app.agents._runtime import RunContext  # noqa: PLC0415
            from app.config import settings  # noqa: PLC0415

            if settings.gemini_api_key:
                output = SmartEditPlannerAgent(default_client()).run(
                    SmartEditPlannerInput(
                        words=[
                            {
                                "word_id": word.word_id,
                                "text": word.display_text,
                                "normalized": word.normalized_text,
                            }
                            for word in normalized_words
                        ],
                        candidates=candidates,
                        assets=planner_assets,
                        preset_id=preset.preset_id,
                        preset_version=preset.version,
                        language=language,
                    ),
                    ctx=RunContext(job_id=job_id),
                )
                proposals = output.proposals
                agent_proposal_count = len(proposals)
                planner_source = "agent" if proposals else "deterministic_rejected_agent"
        except Exception:
            proposals = []
    proposals = _merge_agent_with_guards(proposals, fallback)
    events, omissions = _events_from_proposals(
        proposals,
        words=normalized_words,
        preset=preset,
    )
    document = SmartEditPlanDocument(
        preset_id=preset.preset_id,
        preset_version=preset.version,
        baseline_captions=baseline,
        events=events,
    )
    return SmartPlanBuild(
        normalized_words=normalized_words,
        caption_cues=smart_cues,
        document=document,
        planner_versions={
            "semantic_planner": PLANNER_VERSION,
            "decision_source": planner_source,
        },
        validation_receipt={
            "valid": True,
            "normalized_word_count": len(normalized_words),
            "baseline_caption_count": len(baseline),
            "candidate_count": len(candidates),
            "agent_grounded_proposal_count": agent_proposal_count,
            "event_count": len(events),
            "omissions": omissions,
            "roles": {
                role: sum(event.role == role for event in events)
                for role in ("hook", "context_shift", "list_item", "example", "payoff", "cta")
            },
        },
    )
