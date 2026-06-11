"""Deterministic layout engine for the editorial "word-cluster" intro.

A cluster intro renders the hook as 3-5 independent word blocks with mixed sizes
and offset positions (magazine-style), revealed with a small stagger — the look of
aesthetic TikTok travel edits — instead of one centered block of stacked lines:

                what's  your
                  favorite
                       place?

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
# hero lines hug just right of center and alternate slightly; the connector tucks
# against the first hero's left edge a touch lower; the closer sits below,
# offset right. Vertical step between hero line CENTERS is 0.95x the line height
# — the slight descender overlap that makes the cluster read as one unit.
_CLUSTER_CENTER_Y = 0.44
_HERO_X_BASE = 0.575
_HERO_X_JITTER = 0.03
_HERO_STEP_RATIO = 0.95
_CONNECTOR_Y_NUDGE_FRAC = 0.012  # connector center sits slightly below hero0's
_CONNECTOR_OVERLAP_FRAC = 0.008  # tuck into the hero's left edge, not flush
_CLOSER_X = 0.62
_CLOSER_STEP_RATIO = 0.62  # closer line center sits tighter under the last hero

# Uniform-shrink floor: scaling the whole cluster below this fraction of the
# requested base means the text would read as noise — fall back to linear.
_MIN_CLUSTER_SCALE = 0.55

# Mirror of text_overlay_skia._MIN_FONT_SIZE (duplicated to stay import-light).
# The engine must never emit a px the renderer would silently raise.
_RENDERER_MIN_FONT_PX = 24

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


def derive_word_roles(words: list[str], highlight_word: str | None = None) -> list[str]:
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
            return roles
        longest = max(candidates, key=lambda i: len(words[i]))
        roles[longest] = ROLE_HERO

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
    if (
        ROLE_CONNECTOR not in roles
        and len(words) >= 5
        and roles[0] == ROLE_HERO
        and _bare(words[0]) != highlight
        and hero_count >= 2
    ):
        roles[0] = ROLE_CONNECTOR
    return roles


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


def compute_cluster_blocks(
    text: str,
    *,
    word_roles: list[str] | None = None,
    base_size_px: int,
    font_family: str | None = None,
    reveal_window_s: float,
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
    """
    words = (text or "").split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        return None

    # Roles must align 1:1, use known vocab, and keep any closer STRICTLY final —
    # the geometry assumes the reference shape (closer tucks under the last hero);
    # a mid-text closer would stack two blocks at one position.
    if (
        word_roles is None
        or len(word_roles) != len(words)
        or not set(word_roles) <= set(VALID_ROLES)
        or ROLE_CLOSER in word_roles[:-1]
    ):
        word_roles = derive_word_roles(words)

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

    hero_idx = [i for i, b in enumerate(blocks) if b["role"] == ROLE_HERO]
    hero_step = max(m["h_frac"] for i, m in enumerate(measures) if i in hero_idx) * (
        _HERO_STEP_RATIO
    )

    # Vertical: hero line centers stack downward; connector rides beside hero 0;
    # the closer tucks under the last hero. Laid out relative to hero0 at y=0,
    # then the whole cluster is re-centered on _CLUSTER_CENTER_Y and clamped.
    ys: list[float] = [0.0] * len(blocks)
    for line_no, i in enumerate(hero_idx):
        ys[i] = line_no * hero_step
    last_hero_y = ys[hero_idx[-1]]
    for i, b in enumerate(blocks):
        if b["role"] == ROLE_CONNECTOR:
            anchor_hero = next((h for h in hero_idx if h > i), hero_idx[0])
            ys[i] = ys[anchor_hero] + _CONNECTOR_Y_NUDGE_FRAC
        elif b["role"] == ROLE_CLOSER:
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
            xs[i] = hero_left - measures[i]["w_frac"] / 2 + _CONNECTOR_OVERLAP_FRAC

    # Re-center the cluster vertically on the canvas sweet spot, then clamp
    # every block inside the safe area (clamping is rare — sizes already fit).
    top = min(ys[i] - measures[i]["h_frac"] / 2 for i in range(len(blocks)))
    bottom = max(ys[i] + measures[i]["h_frac"] / 2 for i in range(len(blocks)))
    shift = _CLUSTER_CENTER_Y - (top + bottom) / 2
    for i in range(len(blocks)):
        ys[i] += shift
        half_h = measures[i]["h_frac"] / 2
        ys[i] = min(max(ys[i], _Y_MIN + half_h), _Y_MAX - half_h)
        half_w = measures[i]["w_frac"] / 2
        xs[i] = min(max(xs[i], _EDGE_MARGIN_FRAC + half_w), 1.0 - _EDGE_MARGIN_FRAC - half_w)

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
