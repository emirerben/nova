"""Deterministic layout engine for the editorial "word-cluster" intro.

A cluster intro renders the hook as independent word blocks with mixed sizes
and offset positions (magazine-style), revealed with a small stagger — the look
of aesthetic TikTok travel edits — instead of one centered block of stacked
lines. Two looks share this engine:

  legacy cluster (style=None — byte-identical to the original engine; heroes
  stack around an off-center axis, the connector tucks beside the first hero):

                what's  your
                  favorite
                       place?

  editorial cascade (style=EDITORIAL_STYLE): reading order IS spatial order —
  blocks step diagonally top-left → bottom-right, the ONE emphasis group
  renders in formal script (Great Vibes), everything else alternates between
  the lighter Playfair Display Regular body face and the Playfair Display
  Italic accent face (parity shifted per scene via `accent_parity`):

        when the                       (body, 1.0x)
              ~ days we lost ~         (script hero, 1.7x, Great Vibes)
                      found us.        (italic closer, 1.25x)

The AGENT decides only WHICH words carry the weight (`word_roles`: hero /
connector / closer — see `intro_writer`); THIS module owns all geometry. LLMs are
not trusted with coordinates: collision-free, clip-safe layout is computed here
from real glyph measurements, so every block is inside the frame by construction.

Output blocks use ONLY overlay fields both renderers already honor
(`position_x_frac` / `position_y_frac` / `text_size_px` / `font_family` /
`text_anchor="center"`), so no new burn-dict field and no renderer change is
needed — `generative_overlays` turns each block into an ordinary reveal+hold
overlay pair.

Import-light: skia is lazy-imported inside `compute_cluster_blocks` (same pattern
as `overlay_sizing.py`) so eval/CI environments without skia can import this
module, and callers can fall back to the linear intro when skia is unavailable.

Every function is deterministic — no randomness; horizontal jitter is
index-parity-based. Failure (unsuitable word count, nothing fits) returns None
and the caller renders the proven linear intro instead.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()

ROLE_HERO = "hero"
ROLE_CONNECTOR = "connector"
ROLE_CLOSER = "closer"
VALID_ROLES = (ROLE_HERO, ROLE_CONNECTOR, ROLE_CLOSER)

# Output canvas (mirrors text_overlay_skia.CANVAS_W/H — duplicated to keep this
# module skia-free at import time).
_CANVAS_W = 1080
_CANVAS_H = 1920

# Size ratios per role, applied to the resolved intro base px (the same
# `intro_text_size_px` ∈ [MIN_INTRO_PX, MAX_INTRO_PX] the linear intro uses, so
# the ±size-nudge endpoint keeps working: it scales the whole cluster). Ratios
# derived from the reference frames: hero words ≈ 3.3x the connector, the closer
# ≈ 0.55x a hero.
HERO_RATIO = 2.6
CLOSER_RATIO = 1.4
CONNECTOR_RATIO = 0.9

# Reveal stagger as fractions of the reveal window — matches the reference reveal
# (connector at 0, heroes ~0.4-0.5s, closer ~1.0-1.2s on a 3s intro).
_STAGGER_FRACS = (0.0, 0.13, 0.17, 0.33, 0.40)
# Per-block reveal duration: the Skia fade-in completes in 0.4s; 0.7s leaves a
# settle margin before the static hold takes over.
BLOCK_REVEAL_S = 0.7
_MIN_REVEAL_S = 0.3

# Frame-edge safety. 0.06 of canvas width (~65px) clears overlay_verify's edge
# margin plus the renderer's 6px shadow offset with room to spare.
_EDGE_MARGIN_FRAC = 0.06
_Y_MIN, _Y_MAX = 0.15, 0.85

# Cluster reads as editorial only on short hooks; outside this range the caller
# falls back to linear.
MIN_WORDS, MAX_WORDS = 3, 6
_MAX_BLOCKS = 5
_MAX_HERO_LINES = 3

# Geometry constants (canvas fractions), eyeballed from the reference frames:
# hero lines hug just right of center and alternate slightly; the connector sits
# to the first hero's left with a real gap; the closer sits below,
# offset right. Vertical step between block CENTERS is based on measured glyph
# height plus a small gap: blocks should read as one editorial unit, but never
# collide.
_CLUSTER_CENTER_Y = 0.44
_HERO_X_BASE = 0.575
_HERO_X_JITTER = 0.03
_HERO_STEP_RATIO = 1.08
_BLOCK_GAP_FRAC = 0.01
_CONNECTOR_BESIDE_Y_NUDGE_FRAC = 0.012  # short connectors sit beside hero0, not above it
_CONNECTOR_GAP_FRAC = 0.025  # visible gap; connectors must never collide with heroes
_CLOSER_X = 0.62
_CLOSER_STEP_RATIO = 0.92  # closer line center sits under the last hero with a gap

# Uniform-shrink floor: scaling the whole cluster below this fraction of the
# requested base means the text would read as noise — fall back to linear.
_MIN_CLUSTER_SCALE = 0.55

# Mirror of text_overlay_skia._MIN_FONT_SIZE (duplicated to stay import-light).
# The engine must never emit a px the renderer would silently raise.
_RENDERER_MIN_FONT_PX = 24

# Editorial style profile (decision D13): the SINGLE source of truth for the
# transcript-synced typographic sequence look. Every consumer — this engine's
# styled path, the sequence emitter in generative_overlays, and any renderer
# tuning for thin faces — reads these values from here; no second copy exists.
# Passing `style=EDITORIAL_STYLE` to `compute_cluster_blocks` selects the look;
# `style=None` is the kill-switch contract (byte-identical legacy output).
EDITORIAL_STYLE: dict = {
    # Faces: ONE emphasis group per scene in formal script; everything else
    # alternates between the lighter serif body and the italic serif accent
    # (the reference's third voice — flavor words like 'if' / 'good timing').
    # All bundled in assets/fonts/font-registry.json.
    "hero_font": "Great Vibes",
    "body_font": "Playfair Display Regular",
    "accent_font": "Playfair Display Italic",
    # Restrained size ratios (vs legacy 2.6/0.9/1.4): the editorial look gets
    # its contrast from the face change, not from scale alone.
    "hero_ratio": 1.7,
    "connector_ratio": 1.0,
    "closer_ratio": 1.25,
    # Script faces stop reading below this px. A shrinking cluster pins its
    # hero here while the other roles keep shrinking atomically; if the scene
    # still cannot fit, the engine declines (same None contract as legacy).
    "script_min_px": 64,
    # Thin script/serif strokes need a stronger drop shadow than the
    # renderer's bold-face default (alpha 160, blur 12, dy 6) to stay legible
    # on bright footage. Consumed by the overlay emitter, not by geometry.
    "shadow": {"alpha": 210, "blur": 18.0, "dy": 4.0},
    # Cascade geometry — reading-order diagonal, top-left → bottom-right.
    "cascade_x_start": 0.40,  # first block's center x
    "cascade_x_step": 0.07,  # per-block rightward stagger
    "cascade_x_jitter": 0.02,  # alternating ±jitter (index parity, deterministic)
    "cascade_y_step_ratio": 0.85,  # center gap = ratio * (h_prev + h_cur) / 2
    # Scene-alternation cluster centers, cycled by `scene_center_y(i)` so
    # consecutive scenes don't sit at the identical y.
    "scene_center_ys": (0.42, 0.46, 0.44),
    # Vertical band tightening: cascade block EDGES stay inside
    # [_Y_MIN + margin, _Y_MAX - margin], so a caller shifting a whole scene by
    # scene_center_y(i) - _CLUSTER_CENTER_Y (≤ this margin) keeps the no-clip
    # guarantee intact.
    "scene_shift_margin": 0.02,
    # Scenes are short transcript phrases — a single word is a valid scene
    # (legacy hooks require MIN_WORDS=3; that constant is untouched).
    "min_words": 1,
    # Editorial scenes read as 1-3 text blocks.
    "max_blocks": 3,
}

# Minimal stopword sets for the heuristic role fallback (en + tr — the two
# render languages the pipeline supports). Deliberately small: a missed stopword
# becomes a hero word, which is a taste miss, not a failure.
_STOPWORDS = {
    # en
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "and",
    "or",
    "but",
    "it",
    "its",
    "it's",
    "my",
    "your",
    "our",
    "their",
    "his",
    "her",
    "this",
    "that",
    "these",
    "those",
    "what",
    "what's",
    "when",
    "where",
    "who",
    "how",
    "why",
    "do",
    "does",
    "did",
    "you",
    "we",
    "i",
    "me",
    "so",
    "if",
    "with",
    "as",
    "than",
    "then",
    # tr
    "bir",
    "bu",
    "şu",
    "o",
    "ve",
    "ya",
    "de",
    "da",
    "ki",
    "ne",
    "mi",
    "mı",
    "mu",
    "mü",
    "ile",
    "için",
    "gibi",
    "ama",
    "çok",
    "en",
    "her",
}


def _derive_word_roles_with_guarantees(
    words: list[str], highlight_word: str | None = None
) -> tuple[list[str], dict[str, bool]]:
    """Heuristic word→role assignment when no agent annotation is available.

    Rules (deterministic):
    - the final word is the closer when it carries terminal punctuation (?!.),
      otherwise it competes as a normal word;
    - stopwords are connectors;
    - the agent's highlight_word (when present in the text) is always a hero;
    - everything else is a hero.
    - If no hero survives (all-stopword hook), promote the longest non-closer
      word so the cluster always has a focal point.
    - Contrast guarantees (the editorial look IS the size mix — an all-hero set
      renders as a flat same-size stack, the prod regression behind this rule):
      with no closer signal on a 4+ word hook, the final hero is demoted to
      closer; with no connector on a 5+ word hook, the leading hero is demoted
      to connector. Texts with no stopword/punctuation/highlight signal at all
      (user overrides, Turkish hooks) land on the reference shape instead of
      five identical hero lines.
    """
    guarantees = {
        "hero_present_enforced": False,
        "signal_free_contrast_enforced": False,
    }
    highlight = (highlight_word or "").lower().strip(".,!?;:\"'")
    roles: list[str] = []
    for i, word in enumerate(words):
        bare = word.lower().strip(".,!?;:\"'")
        if i == len(words) - 1 and word.rstrip()[-1:] in "?!.":
            roles.append(ROLE_CLOSER)
        elif bare and bare == highlight:
            roles.append(ROLE_HERO)
        elif bare in _STOPWORDS:
            roles.append(ROLE_CONNECTOR)
        else:
            roles.append(ROLE_HERO)
    if ROLE_HERO not in roles:
        candidates = [i for i, r in enumerate(roles) if r != ROLE_CLOSER]
        if not candidates:
            return roles, guarantees
        longest = max(candidates, key=lambda i: len(words[i]))
        roles[longest] = ROLE_HERO
        guarantees["hero_present_enforced"] = True

    def _bare(word: str) -> str:
        return word.lower().strip(".,!?;:\"'")

    hero_count = roles.count(ROLE_HERO)
    if (
        ROLE_CLOSER not in roles
        and len(words) >= 4
        and roles[-1] == ROLE_HERO
        and _bare(words[-1]) != highlight
        and hero_count >= 2
    ):
        roles[-1] = ROLE_CLOSER
        hero_count -= 1
        guarantees["signal_free_contrast_enforced"] = True
    if (
        ROLE_CONNECTOR not in roles
        and len(words) >= 5
        and roles[0] == ROLE_HERO
        and _bare(words[0]) != highlight
        and hero_count >= 2
    ):
        roles[0] = ROLE_CONNECTOR
        guarantees["signal_free_contrast_enforced"] = True
    return roles, guarantees


def derive_word_roles(words: list[str], highlight_word: str | None = None) -> list[str]:
    return _derive_word_roles_with_guarantees(words, highlight_word)[0]


def _record_cluster_roles_event(
    *,
    words: list[str],
    input_roles: list[str] | None,
    effective_roles: list[str],
    role_source: str,
    guarantees: dict[str, bool],
) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "overlay",
            "cluster_roles_derived",
            {
                "words": words,
                "input_roles": input_roles,
                "effective_roles": effective_roles,
                "role_source": role_source,
                "guarantees": guarantees,
            },
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break layout
        log.warning("cluster_roles_event_emit_failed", error=str(exc))


def _record_cluster_shrink_event(
    *,
    text: str,
    base_size_px: int,
    scale: float,
    measures: list[dict],
    usable_w: float,
    block_count: int,
) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "overlay",
            "cluster_shrink_applied",
            {
                "text": text,
                "base_size_px": base_size_px,
                "scale": round(scale, 4),
                "min_scale": _MIN_CLUSTER_SCALE,
                "widest_block_frac": round(max(m["w_frac"] for m in measures), 4),
                "usable_width_frac": round(usable_w, 4),
                "block_count": block_count,
            },
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break layout
        log.warning("cluster_shrink_event_emit_failed", error=str(exc))


def _group_blocks(words: list[str], roles: list[str]) -> list[dict] | None:
    """Group words into ordered cluster blocks.

    Adjacent same-role words merge into one block (each hero word stays its OWN
    block — hero words are the big stacked lines). Returns None when the shape is
    unusable (no hero). Caps at _MAX_BLOCKS / _MAX_HERO_LINES by merging the
    overflow into the last block of the same role tier.
    """
    blocks: list[dict] = []
    for word, role in zip(words, roles, strict=True):
        if blocks and role != ROLE_HERO and blocks[-1]["role"] == role:
            blocks[-1]["text"] += f" {word}"
        else:
            blocks.append({"role": role, "text": word})

    hero_blocks = [b for b in blocks if b["role"] == ROLE_HERO]
    if not hero_blocks:
        return None

    # Fold surplus hero lines (and everything after them — reading order is
    # sacred) into ONE final closer block. Merging surplus words into a HERO
    # line was the prod flat-stack bug: the merged line renders at hero size
    # (2.6x), its width forces the cluster-atomic shrink to crush every block,
    # and the size hierarchy — the entire editorial look — flattens away.
    # Closer-sized (1.4x) tail text keeps the heroes big. Block identity is used
    # to find the fold point — duplicate hero dicts compare equal ("go go go").
    if len(hero_blocks) > _MAX_HERO_LINES:
        first_extra = hero_blocks[_MAX_HERO_LINES]
        fold_at = next(i for i, b in enumerate(blocks) if b is first_extra)
        tail_text = " ".join(b["text"] for b in blocks[fold_at:])
        blocks = blocks[:fold_at] + [{"role": ROLE_CLOSER, "text": tail_text}]

    # Cap total block count by merging trailing non-hero blocks together.
    while len(blocks) > _MAX_BLOCKS:
        for i in range(len(blocks) - 1, 0, -1):
            if blocks[i]["role"] != ROLE_HERO:
                blocks[i - 1]["text"] += f" {blocks[i]['text']}"
                del blocks[i]
                break
        else:
            blocks[-2]["text"] += f" {blocks[-1]['text']}"
            del blocks[-1]
    return blocks


def _role_px(role: str, base_size_px: int, scale: float) -> int:
    ratio = {ROLE_HERO: HERO_RATIO, ROLE_CLOSER: CLOSER_RATIO, ROLE_CONNECTOR: CONNECTOR_RATIO}[
        role
    ]
    # Floor mirrors the renderer's _MIN_FONT_SIZE: anything smaller would be
    # raised back to 24px at draw time, breaking the measured-equals-rendered
    # contract the no-clip guarantee depends on.
    return max(_RENDERER_MIN_FONT_PX, int(round(base_size_px * ratio * scale)))


def _connector_font(hero_font: str | None, registry_fonts: dict) -> str | None:
    """Connectors render in the regular weight of the hero face when the registry
    has one ("Playfair Display" → "Playfair Display Regular") — the small words in
    the reference are visibly lighter than the heroes. Falls back to the hero face."""
    if hero_font:
        candidate = f"{hero_font} Regular"
        if candidate in registry_fonts:
            return candidate
        return hero_font
    # No explicit hero font → renderer falls back to Playfair Display Bold, so the
    # lighter sibling is the right connector default when bundled.
    if "Playfair Display Regular" in registry_fonts:
        return "Playfair Display Regular"
    return None


def normalize_typography(text: str) -> str:
    """Map typewriter punctuation to its typographic form (styled scenes only).

    ' → ’ (apostrophe — covers in-word cases like it's → it’s), double quotes
    become “ ” by CONTEXT (a quote following a word/closing character closes;
    anything else opens), and ... → …. Context beats pair-alternation because
    blocks are normalized independently — a phrase split across blocks
    ('"just' | 'luck".') would reset an alternating counter and flip the
    closing mark. Deterministic and idempotent; legacy (style=None) text is
    never touched.
    """
    text = text.replace("...", "…").replace("'", "’")
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == '"':
            prev = text[i - 1] if i > 0 else ""
            closes = bool(prev) and (prev.isalnum() or prev in ".,!?…’”)]")
            out.append("”" if closes else "“")
        else:
            out.append(ch)
    return "".join(out)


def scene_center_y(scene_index: int, style: dict | None = None) -> float:
    """Cluster center y for the scene at `scene_index` (deterministic cycle).

    Consecutive scenes alternate their cluster center slightly (0.42 / 0.46 /
    0.44 by default) so back-to-back scenes don't sit at the identical y. Every
    value stays within `scene_shift_margin` of `_CLUSTER_CENTER_Y`, and the
    styled layout keeps block edges inside the band tightened by that margin —
    so shifting a computed scene by `scene_center_y(i) - _CLUSTER_CENTER_Y`
    preserves the no-clip guarantee.
    """
    centers = (style or EDITORIAL_STYLE).get("scene_center_ys") or (_CLUSTER_CENTER_Y,)
    return float(centers[scene_index % len(centers)])


def _styled_role_px(role: str, base_size_px: int, scale: float, style: dict) -> int:
    ratio = {
        ROLE_HERO: style["hero_ratio"],
        ROLE_CLOSER: style["closer_ratio"],
        ROLE_CONNECTOR: style["connector_ratio"],
    }[role]
    px = int(round(base_size_px * ratio * scale))
    if role == ROLE_HERO:
        # Script faces stop reading below script_min_px: the hero pins there
        # while the rest of the cluster keeps shrinking atomically (the shrink
        # loop measures the floored px, so the fit check stays consistent and
        # an unfittable scene still declines at _MIN_CLUSTER_SCALE).
        return max(_RENDERER_MIN_FONT_PX, int(style["script_min_px"]), px)
    return max(_RENDERER_MIN_FONT_PX, px)


def _group_styled_blocks(words: list[str], roles: list[str], style: dict) -> list[dict] | None:
    """Group words into reading-order blocks for the editorial cascade.

    Differences from legacy `_group_blocks` (both deliberate):
    - ONE emphasis group per scene: hero words after the first contiguous hero
      run demote to closer (body face, closer size) — the editorial look is a
      single script phrase, not stacked hero lines.
    - ALL adjacent same-role words merge (heroes included): the emphasis group
      is one block, not one block per word.
    """
    effective = list(roles)
    seen_hero_run = False
    in_hero_run = False
    for i, role in enumerate(effective):
        if role == ROLE_HERO:
            if seen_hero_run and not in_hero_run:
                effective[i] = ROLE_CLOSER
            else:
                seen_hero_run = True
                in_hero_run = True
        else:
            in_hero_run = False

    blocks: list[dict] = []
    for word, role in zip(words, effective, strict=True):
        if blocks and blocks[-1]["role"] == role:
            blocks[-1]["text"] += f" {word}"
        else:
            blocks.append({"role": role, "text": word})

    if not any(b["role"] == ROLE_HERO for b in blocks):
        # Single-block scenes ARE their own emphasis; multi-block scenes still
        # need a focal point — promote the longest block instead of declining
        # (scene text comes from the transcript splitter, not a hook heuristic,
        # so an all-closer/all-connector annotation must still render).
        longest = max(blocks, key=lambda b: len(b["text"]))
        longest["role"] = ROLE_HERO

    # Cap the scene at max_blocks by merging adjacent non-hero neighbors from
    # the tail (same fold strategy as legacy; the hero block is never absorbed).
    max_blocks = int(style.get("max_blocks", 3))
    while len(blocks) > max_blocks:
        for i in range(len(blocks) - 1, 0, -1):
            if blocks[i]["role"] != ROLE_HERO and blocks[i - 1]["role"] != ROLE_HERO:
                blocks[i - 1]["text"] += f" {blocks[i]['text']}"
                del blocks[i]
                break
        else:
            blocks[-2]["text"] += f" {blocks[-1]['text']}"
            del blocks[-1]
    return blocks


def _compute_styled_blocks(
    text: str,
    *,
    word_roles: list[str] | None,
    base_size_px: int,
    reveal_window_s: float,
    style: dict,
    accent_parity: int = 0,
) -> list[dict] | None:
    """Editorial-cascade layout (`style=EDITORIAL_STYLE`). See EDITORIAL_STYLE
    for the knobs; `compute_cluster_blocks` documents the output contract
    (identical block keys to the legacy path).

    Geometry: blocks lay out STRICTLY in text order — each successive block's
    center y increases, x staggers left→right with alternating jitter. This
    also fixes the legacy connector-anchor bug for the styled path: a
    connector with no FOLLOWING hero sits after its PRECEDING block instead of
    snapping back up beside hero 0 (reading order is sacred).
    """
    words = (text or "").split()
    min_words = int(style.get("min_words", MIN_WORDS))
    if not (min_words <= len(words) <= MAX_WORDS):
        return None

    input_roles = list(word_roles) if word_roles is not None else None
    # Same validation as legacy minus the closer-strictly-final rule: the
    # cascade lays blocks in text order, so a mid-text closer cannot stack.
    if (
        word_roles is None
        or len(word_roles) != len(words)
        or not set(word_roles) <= set(VALID_ROLES)
    ):
        invalid_roles_rederived = word_roles is not None
        word_roles, heuristic_guarantees = _derive_word_roles_with_guarantees(words)
        role_source = "heuristic"
    else:
        invalid_roles_rederived = False
        heuristic_guarantees = {
            "hero_present_enforced": False,
            "signal_free_contrast_enforced": False,
        }
        role_source = "agent"

    _record_cluster_roles_event(
        words=words,
        input_roles=input_roles,
        effective_roles=list(word_roles),
        role_source=role_source,
        guarantees={
            "invalid_roles_rederived": invalid_roles_rederived,
            "closer_final_enforced": False,
            "hero_present_enforced": heuristic_guarantees["hero_present_enforced"],
            "signal_free_contrast_enforced": heuristic_guarantees["signal_free_contrast_enforced"],
            "all_hero_demoted_to_closer": False,
        },
    )

    blocks = _group_styled_blocks(words, word_roles, style)
    if not blocks:
        return None

    for block in blocks:
        block["text"] = normalize_typography(block["text"])

    # Lazy skia import — measurement + glyph gate only. Unavailable skia →
    # caller falls back (same contract as the legacy path).
    import skia  # noqa: PLC0415

    from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import (  # noqa: PLC0415
        MissingGlyphsError,
        _typeface_for_overlay,
        assert_glyphs_present,
    )

    registry_fonts = _FONT_REGISTRY.get("fonts", {})
    hero_face = style["hero_font"]
    body_face = style["body_font"]
    accent_face = style.get("accent_font")
    typeface_cache: dict[str, object] = {}

    def _typeface(family: str):
        if family not in typeface_cache:
            typeface_cache[family] = _typeface_for_overlay({"font_family": family})
        return typeface_cache[family]

    # Glyph gate (decision D18): never accept a styled layout whose face would
    # draw tofu. Hero blocks missing glyphs in the script face fall back to the
    # body face — accent blocks the same way; anything the body face can't
    # cover declines the whole scene (caller renders the static/linear
    # fallback). A face missing from the registry counts as missing glyphs —
    # `_typeface_for_overlay` would silently resolve it to Playfair Bold and
    # the check would test the wrong face.
    #
    # Third voice (reference behavior): non-hero blocks (connector AND closer)
    # alternate between the body serif and the italic accent — the k-th
    # non-hero block of the scene takes the accent when
    # (k + accent_parity) % 2 == 1. Callers pass the scene index as
    # `accent_parity` so consecutive scenes alternate which block opens italic
    # ('if'(italic) 'you'(serif) / 'and'(serif) 'good timing'(italic)). Hero
    # blocks NEVER take the accent face. Measurement below uses the FINAL
    # chosen face, so the no-clip contract is preserved through any fallback.
    non_hero_k = 0
    for block in blocks:
        if block["role"] == ROLE_HERO:
            preferred = hero_face
        else:
            wants_accent = bool(accent_face) and (non_hero_k + accent_parity) % 2 == 1
            preferred = accent_face if wants_accent else body_face
            non_hero_k += 1
        candidates = [preferred] if preferred == body_face else [preferred, body_face]
        face = None
        for candidate in candidates:
            if candidate not in registry_fonts:
                continue
            try:
                assert_glyphs_present(_typeface(candidate), block["text"])
            except MissingGlyphsError:
                continue
            face = candidate
            break
        if face is None:
            log.info(
                "intro_cluster_styled_glyph_gate_declined",
                text=text,
                block_text=block["text"],
            )
            return None
        block["family"] = face

    def _measure(block: dict, scale: float) -> dict:
        px = _styled_role_px(block["role"], base_size_px, scale, style)
        font = skia.Font(_typeface(block["family"]), px)
        font.setSubpixel(True)
        metrics = font.getMetrics()
        return {
            "family": block["family"],
            "px": px,
            "w_frac": font.measureText(block["text"]) / _CANVAS_W,
            "h_frac": (metrics.fDescent - metrics.fAscent) / _CANVAS_H,
        }

    usable_w = 1.0 - 2 * _EDGE_MARGIN_FRAC
    margin_y = float(style.get("scene_shift_margin", 0.0))
    usable_h = (_Y_MAX - margin_y) - (_Y_MIN + margin_y)
    step_ratio = float(style["cascade_y_step_ratio"])

    def _cascade_height(measures: list[dict]) -> float:
        total = measures[0]["h_frac"] / 2 + measures[-1]["h_frac"] / 2
        for prev, cur in zip(measures, measures[1:]):
            total += (prev["h_frac"] + cur["h_frac"]) / 2 * step_ratio
        return total

    # Cluster-atomic shrink (reused from legacy, plus a HEIGHT constraint: the
    # cascade clamps by shifting the whole scene, so the full diagonal must fit
    # in the tightened vertical band, not just each block's width).
    scale = 1.0
    while True:
        measures = [_measure(b, scale) for b in blocks]
        if max(m["w_frac"] for m in measures) <= usable_w and _cascade_height(measures) <= usable_h:
            break
        scale *= 0.92
        if scale < _MIN_CLUSTER_SCALE:
            log.info("intro_cluster_too_wide_for_frame", text=text, styled=True)
            return None

    if scale < 1.0:
        _record_cluster_shrink_event(
            text=text,
            base_size_px=base_size_px,
            scale=scale,
            measures=measures,
            usable_w=usable_w,
            block_count=len(blocks),
        )

    # Diagonal cascade, strictly in reading order: y centers strictly increase
    # (gap derived from adjacent block heights), x staggers rightward with a
    # deterministic alternating jitter. step (0.07) > 2*jitter (0.04) keeps x
    # monotonic too.
    ys: list[float] = [0.0] * len(blocks)
    for i in range(1, len(blocks)):
        ys[i] = ys[i - 1] + (measures[i - 1]["h_frac"] + measures[i]["h_frac"]) / 2 * step_ratio
    x_jitter = float(style["cascade_x_jitter"])
    xs: list[float] = [
        float(style["cascade_x_start"])
        + float(style["cascade_x_step"]) * i
        + (0.0 if i == 0 else (x_jitter if i % 2 else -x_jitter))
        for i in range(len(blocks))
    ]

    # Re-center on the default cluster center, then clamp by shifting the
    # WHOLE cascade (per-block y clamping could collapse two centers onto each
    # other and break the strict reading-order invariant). The shrink loop
    # guarantees the cascade fits the tightened band, so both bounds hold.
    top = ys[0] - measures[0]["h_frac"] / 2
    bottom = ys[-1] + measures[-1]["h_frac"] / 2
    shift = _CLUSTER_CENTER_Y - (top + bottom) / 2
    shift = min(shift, (_Y_MAX - margin_y) - bottom)
    shift = max(shift, (_Y_MIN + margin_y) - top)
    for i in range(len(blocks)):
        ys[i] += shift
        half_w = measures[i]["w_frac"] / 2
        xs[i] = min(max(xs[i], _EDGE_MARGIN_FRAC + half_w), 1.0 - _EDGE_MARGIN_FRAC - half_w)

    # Reveal stagger in block order — identical timing model to the legacy path
    # (the soft-fade feel comes from the renderer's fade-in/fade-out ramps).
    window = max(0.0, float(reveal_window_s))
    out: list[dict] = []
    for i, (b, m) in enumerate(zip(blocks, measures, strict=True)):
        start = _STAGGER_FRACS[min(i, len(_STAGGER_FRACS) - 1)] * window
        start = max(0.0, min(start, window - 0.5)) if window > 0.5 else 0.0
        reveal_end = min(start + BLOCK_REVEAL_S, window)
        out.append(
            {
                "text": b["text"],
                "role": b["role"],
                "text_size_px": m["px"],
                "font_family": m["family"],
                "position_x_frac": round(xs[i], 6),
                "position_y_frac": round(ys[i], 6),
                "start_offset_s": round(start, 3),
                "reveal_s": round(max(_MIN_REVEAL_S, reveal_end - start), 3),
            }
        )
    return out


def compute_cluster_blocks(
    text: str,
    *,
    word_roles: list[str] | None = None,
    base_size_px: int,
    font_family: str | None = None,
    reveal_window_s: float,
    style: dict | None = None,
    accent_parity: int = 0,
) -> list[dict] | None:
    """Compute the word-cluster layout for an intro hook.

    Returns an ordered block list, or None when the text doesn't suit a cluster
    (word count outside [MIN_WORDS, MAX_WORDS], no hero word, or the cluster
    cannot fit the frame at a readable size). Each block:

        {"text", "role", "text_size_px", "font_family",
         "position_x_frac", "position_y_frac",      # block CENTER (text_anchor="center")
         "start_offset_s", "reveal_s"}

    `word_roles` is the agent annotation aligned to `text.split()`; invalid or
    missing annotations fall back to `derive_word_roles`. `base_size_px` is the
    resolved intro size (the linear intro's `text_size_px`), scaled per role.

    `style=None` (the default) is the kill-switch contract: byte-identical
    legacy behavior. `style=EDITORIAL_STYLE` selects the editorial cascade —
    faces come from the style profile (`font_family` is ignored), blocks lay
    out strictly in reading order, punctuation is typographically normalized,
    and a glyph gate guarantees the chosen faces can draw every block.

    `accent_parity` (styled path only; ignored by the legacy path) shifts
    which non-hero block opens in the italic accent face: the k-th non-hero
    block uses the accent when (k + accent_parity) % 2 == 1. Callers pass the
    scene index so consecutive scenes alternate their italic lead.
    """
    if style is not None:
        return _compute_styled_blocks(
            text,
            word_roles=word_roles,
            base_size_px=base_size_px,
            reveal_window_s=reveal_window_s,
            style=style,
            accent_parity=accent_parity,
        )
    words = (text or "").split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        return None

    input_roles = list(word_roles) if word_roles is not None else None
    invalid_roles_rederived = False
    closer_final_enforced = False

    # Roles must align 1:1, use known vocab, and keep any closer STRICTLY final —
    # the geometry assumes the reference shape (closer tucks under the last hero);
    # a mid-text closer would stack two blocks at one position.
    if (
        word_roles is None
        or len(word_roles) != len(words)
        or not set(word_roles) <= set(VALID_ROLES)
        or ROLE_CLOSER in word_roles[:-1]
    ):
        invalid_roles_rederived = word_roles is not None
        closer_final_enforced = bool(word_roles and ROLE_CLOSER in word_roles[:-1])
        word_roles, heuristic_guarantees = _derive_word_roles_with_guarantees(words)
        role_source = "heuristic"
    else:
        heuristic_guarantees = {
            "hero_present_enforced": False,
            "signal_free_contrast_enforced": False,
        }
        role_source = "agent"

    all_hero_demoted_to_closer = len(word_roles) > 1 and all(
        role == ROLE_HERO for role in word_roles
    )
    _record_cluster_roles_event(
        words=words,
        input_roles=input_roles,
        effective_roles=list(word_roles),
        role_source=role_source,
        guarantees={
            "invalid_roles_rederived": invalid_roles_rederived,
            "closer_final_enforced": closer_final_enforced,
            "hero_present_enforced": heuristic_guarantees["hero_present_enforced"],
            "signal_free_contrast_enforced": heuristic_guarantees["signal_free_contrast_enforced"],
            "all_hero_demoted_to_closer": all_hero_demoted_to_closer,
        },
    )

    blocks = _group_blocks(words, word_roles)
    if blocks is None:
        return None

    # Editorial needs size contrast: a single-role (all-hero) cluster — agent
    # annotations are allowed to be all-hero — renders as a flat same-size
    # stack. Demote the final block to closer (closer-is-final is already the
    # geometry's shape); strictly better than declining or rendering flat.
    if len(blocks) > 1 and all(b["role"] == ROLE_HERO for b in blocks):
        blocks[-1]["role"] = ROLE_CLOSER

    # Lazy skia import — measurement only. Unavailable skia → caller falls back.
    import skia  # noqa: PLC0415

    from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import _typeface_for_overlay  # noqa: PLC0415

    registry_fonts = _FONT_REGISTRY.get("fonts", {})
    connector_family = _connector_font(font_family, registry_fonts)
    typeface_cache: dict[str | None, object] = {}

    def _typeface(family: str | None):
        if family not in typeface_cache:
            typeface_cache[family] = _typeface_for_overlay(
                {"font_family": family} if family else {}
            )
        return typeface_cache[family]

    def _measure(block: dict, scale: float) -> dict:
        family = connector_family if block["role"] == ROLE_CONNECTOR else font_family
        px = _role_px(block["role"], base_size_px, scale)
        font = skia.Font(_typeface(family), px)
        font.setSubpixel(True)
        metrics = font.getMetrics()
        return {
            "family": family,
            "px": px,
            "w_frac": font.measureText(block["text"]) / _CANVAS_W,
            "h_frac": (metrics.fDescent - metrics.fAscent) / _CANVAS_H,
        }

    usable_w = 1.0 - 2 * _EDGE_MARGIN_FRAC

    # Cluster-atomic shrink: scale ALL roles together until the widest block fits,
    # preserving the size relationships (unlike per-overlay _shrink_to_fit, which
    # would flatten the hierarchy). Deterministic 0.92 steps, hard floor below
    # which the cluster stops reading as editorial → linear fallback.
    scale = 1.0
    while True:
        measures = [_measure(b, scale) for b in blocks]
        if max(m["w_frac"] for m in measures) <= usable_w:
            break
        scale *= 0.92
        if scale < _MIN_CLUSTER_SCALE:
            log.info("intro_cluster_too_wide_for_frame", text=text)
            return None

    if scale < 1.0:
        _record_cluster_shrink_event(
            text=text,
            base_size_px=base_size_px,
            scale=scale,
            measures=measures,
            usable_w=usable_w,
            block_count=len(blocks),
        )

    hero_idx = [i for i, b in enumerate(blocks) if b["role"] == ROLE_HERO]
    hero_step = max(m["h_frac"] for i, m in enumerate(measures) if i in hero_idx) * (
        _HERO_STEP_RATIO
    )

    # Vertical: hero line centers stack downward; the closer sits under the
    # last hero. Connectors are placed after horizontal clamping below: short
    # connectors stay beside their anchor hero, long/edge-clamped connectors
    # ride above it so measured boxes still never collide.
    ys: list[float] = [0.0] * len(blocks)
    for line_no, i in enumerate(hero_idx):
        ys[i] = line_no * hero_step
    last_hero_y = ys[hero_idx[-1]]
    for i, b in enumerate(blocks):
        if b["role"] == ROLE_CLOSER:
            ys[i] = last_hero_y + hero_step * _CLOSER_STEP_RATIO

    # Horizontal: heroes alternate around the off-center axis; closer offset
    # right; connector hugs its hero's left edge.
    xs: list[float] = [0.0] * len(blocks)
    for line_no, i in enumerate(hero_idx):
        xs[i] = _HERO_X_BASE + (_HERO_X_JITTER if line_no % 2 else 0.0) * (
            -1.0 if line_no == 1 else 1.0
        )
    for i, b in enumerate(blocks):
        if b["role"] == ROLE_CLOSER:
            xs[i] = _CLOSER_X
        elif b["role"] == ROLE_CONNECTOR:
            anchor_hero = next((h for h in hero_idx if h > i), hero_idx[0])
            hero_left = xs[anchor_hero] - measures[anchor_hero]["w_frac"] / 2
            xs[i] = hero_left - measures[i]["w_frac"] / 2 - _CONNECTOR_GAP_FRAC
    for i in range(len(blocks)):
        half_w = measures[i]["w_frac"] / 2
        xs[i] = min(max(xs[i], _EDGE_MARGIN_FRAC + half_w), 1.0 - _EDGE_MARGIN_FRAC - half_w)

    def _overlaps_x(a: int, b: int) -> bool:
        a_left = xs[a] - measures[a]["w_frac"] / 2
        a_right = xs[a] + measures[a]["w_frac"] / 2
        b_left = xs[b] - measures[b]["w_frac"] / 2
        b_right = xs[b] + measures[b]["w_frac"] / 2
        return a_left < b_right - 1e-6 and b_left < a_right - 1e-6

    for i, b in enumerate(blocks):
        if b["role"] != ROLE_CONNECTOR:
            continue
        anchor_hero = next((h for h in hero_idx if h > i), hero_idx[0])
        if _overlaps_x(i, anchor_hero):
            ys[i] = (
                ys[anchor_hero]
                - measures[anchor_hero]["h_frac"] / 2
                - measures[i]["h_frac"] / 2
                - _BLOCK_GAP_FRAC
            )
        else:
            ys[i] = ys[anchor_hero] + _CONNECTOR_BESIDE_Y_NUDGE_FRAC

    # Re-center the cluster vertically on the canvas sweet spot, then clamp
    # every block inside the safe area (clamping is rare — sizes already fit).
    top = min(ys[i] - measures[i]["h_frac"] / 2 for i in range(len(blocks)))
    bottom = max(ys[i] + measures[i]["h_frac"] / 2 for i in range(len(blocks)))
    shift = _CLUSTER_CENTER_Y - (top + bottom) / 2
    for i in range(len(blocks)):
        ys[i] += shift
        half_h = measures[i]["h_frac"] / 2
        ys[i] = min(max(ys[i], _Y_MIN + half_h), _Y_MAX - half_h)

    # Reveal stagger in BLOCK order (connector→heroes→closer follows the text's
    # natural reading order, which is the reference behavior).
    window = max(0.0, float(reveal_window_s))
    out: list[dict] = []
    for i, (b, m) in enumerate(zip(blocks, measures, strict=True)):
        start = _STAGGER_FRACS[min(i, len(_STAGGER_FRACS) - 1)] * window
        start = max(0.0, min(start, window - 0.5)) if window > 0.5 else 0.0
        reveal_end = min(start + BLOCK_REVEAL_S, window)
        out.append(
            {
                "text": b["text"],
                "role": b["role"],
                "text_size_px": m["px"],
                "font_family": m["family"],
                # 6 decimals: enough to be JSON-stable, fine enough that rounding
                # can never push a clamped edge back outside the margin.
                "position_x_frac": round(xs[i], 6),
                "position_y_frac": round(ys[i], 6),
                "start_offset_s": round(start, 3),
                "reveal_s": round(max(_MIN_REVEAL_S, reveal_end - start), 3),
            }
        )
    return out
