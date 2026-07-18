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
from app.smart_edit.captions import build_smart_caption_cues
from app.smart_edit.presets import SmartEditPreset, load_preset
from app.smart_edit.schemas import (
    MAX_SMART_WORDS,
    BaselineCaptionCue,
    BoundaryEffectLane,
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
        guarded_visuals = list(
            dict.fromkeys([*guard.visual_asset_ids, *proposal.visual_asset_ids])
        )[:5]
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
                    "scene_token": proposal.scene_token or guard.scene_token,
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
