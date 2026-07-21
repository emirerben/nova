"""Curated text **style sets** — coherent, content-matched typography.

A style set is a named bundle of per-ROLE styling (font, size, color, effect,
position, anchor, timing) that fully *owns* the look of an on-screen text
overlay. The LLM selector / recipe supplies only the text, timing anchors, and a
role; the chosen set dictates the rest. This replaces the old per-attribute
free-decisioning (Gemini emitting arbitrary font/color/effect, the music
`lyrics_config` picking style ad-hoc) with a small curated design system.

Scope: agentic templates, music lyrics, and generative edits — NOT classic
templates (those never carry `style_set_id` and keep their existing path).

Storage mirrors the font registry (`assets/fonts/font-registry.json`): a single
hand-curated, version-controlled JSON at `assets/style_sets/style-sets.json`.
The top-level `version` string is folded into the Layer-2 cache key so editing
the library invalidates stale agentic recipes.

Renderer-parity contract (CLAUDE.md #296 class): every field a style set emits
must be honored by BOTH the Pillow and Skia renderers.  All fields in
``_STYLE_KEYS`` satisfy this invariant — including ``text_gradient``, which was
added together with renderer support in both paths (PR #487).  Adding a new
field here without updating both renderers breaks the invariant; the parity test
``test_both_renderers_honor_text_gradient`` is the sentinel.
"""

from __future__ import annotations

import json
import os

import structlog

from app.pipeline.text_overlay import (
    _FONT_REGISTRY,
    _POSITION_Y,
    FONTS_DIR,
)

log = structlog.get_logger()

# ── Role + effect vocabulary ─────────────────────────────────────────────────
# The union of roles all three paths can emit. Supersedes the narrower
# template_text VALID_ROLES; lyric_* roles map to the music lyric injectors.
VALID_STYLE_ROLES: frozenset[str] = frozenset(
    {
        "hook",
        "label",
        "cta",
        "reaction",
        "body",
        "lyric_line",
        "lyric_karaoke",
        "lyric_word_pop",
        "intro",
    }
)

# Effects a set may name: the schema's VALID_EFFECTS plus the two renderer-level
# lyric effects (karaoke-line / lyric-line) that never appear in the agent
# schema but are produced by the lyric injector.
_ALLOWED_EFFECTS: frozenset[str] = frozenset(
    {
        "pop-in",
        "fade-in",
        "scale-up",
        "font-cycle",
        "typewriter",
        "glitch",
        "bounce",
        "slide-in",
        "slide-up",
        "slide-down",
        "static",
        "none",
        "karaoke-line",
        "lyric-line",
        "stream-in",
        "staggered-slice",
    }
)

# Effects exposed in the instant-editor animation picker (the frontend INTRO_ANIMATIONS list).
# Strict subset of _ALLOWED_EFFECTS — does NOT include glitch, font-cycle, karaoke-line, lyric-line.
# Used to validate the `effect` field on EditVariantRequest.
_INTRO_ANIMATION_EFFECTS: frozenset[str] = frozenset(
    {
        "fade-in",
        "pop-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "bounce",
        "typewriter",
        "stream-in",
        "staggered-slice",
        "none",
        "static",
    }
)

# Concrete style keys a resolved set writes onto the overlay entry dict. Anything
# else in a role block (e.g. `timing`) is handled specially.
_STYLE_KEYS: tuple[str, ...] = (
    "font_family",
    "text_size",
    "text_size_px",
    "text_color",
    "highlight_color",
    "effect",
    "position",
    "position_x_frac",
    "position_y_frac",
    "text_anchor",
    "stroke_width",
    "cycle_fonts",
    # Gradient text fill — dict {colors, angle_deg, stops}.  Both renderers
    # honor this field (Skia: GradientShader, Pillow: numpy image composite).
    # Parity test: test_both_renderers_honor_text_gradient.
    "text_gradient",
)

# Role fallback chains: when a set doesn't define the requested role, try the
# nearest sibling before dropping to the default set.
_ROLE_FALLBACK: dict[str, tuple[str, ...]] = {
    "hook": ("intro", "reaction", "body"),
    "intro": ("hook", "reaction", "body"),
    "reaction": ("hook", "body"),
    "label": ("body",),
    "cta": ("body",),
    "body": (),
    "lyric_karaoke": ("lyric_line", "lyric_word_pop", "body"),
    "lyric_line": ("lyric_karaoke", "lyric_word_pop", "body"),
    "lyric_word_pop": ("lyric_line", "lyric_karaoke", "body"),
}

_DEFAULT_SET_ID = "default"

# ── Hardcoded fallback (mirrors text_overlay._FALLBACK_STYLE_MAP) ────────────
# A bad/missing JSON must never hard-fail a render. This minimal `default` set
# keeps every path renderable.
_FALLBACK_STYLE_SETS: dict = {
    "version": "fallback",
    "sets": [
        {
            "id": "default",
            "label": "Default (hardcoded fallback)",
            "tags": ["fallback"],
            "applies_to": ["agentic", "music", "generative"],
            "roles": {
                "hook": {
                    "font_family": "Montserrat",
                    "text_size": "xlarge",
                    "text_color": "#FFFFFF",
                    "effect": "pop-in",
                    "position": "center",
                    "text_anchor": "center",
                    "stroke_width": 6,
                },
                "body": {
                    "font_family": "DM Sans",
                    "text_size": "medium",
                    "text_color": "#FFFFFF",
                    "effect": "fade-in",
                    "position": "center",
                    "text_anchor": "center",
                    "stroke_width": 4,
                },
                "lyric_line": {
                    "font_family": "DM Sans",
                    "text_size": "large",
                    "text_color": "#FFFFFF",
                    "effect": "lyric-line",
                    "position": "bottom",
                    "text_anchor": "center",
                    "stroke_width": 4,
                },
                "lyric_karaoke": {
                    "font_family": "Montserrat",
                    "text_size": "large",
                    "text_color": "#FFFFFF",
                    "highlight_color": "#FFE14D",
                    "effect": "karaoke-line",
                    "position": "bottom",
                    "text_anchor": "center",
                    "stroke_width": 4,
                },
                "lyric_word_pop": {
                    "font_family": "Montserrat",
                    "text_size": "large",
                    "text_color": "#FFFFFF",
                    "effect": "pop-in",
                    "position": "bottom",
                    "text_anchor": "center",
                    "stroke_width": 4,
                },
                "intro": {
                    "font_family": "Montserrat",
                    "text_size": "xlarge",
                    "text_color": "#FFFFFF",
                    "effect": "pop-in",
                    "position": "center",
                    "text_anchor": "center",
                    "stroke_width": 6,
                },
            },
        }
    ],
}

_REGISTRY_PATH = os.path.join(os.path.dirname(FONTS_DIR), "style_sets", "style-sets.json")


def _load_style_sets(path: str = _REGISTRY_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if not data.get("sets"):
            raise ValueError("style-sets.json has no `sets`")
        return data
    except Exception as exc:  # noqa: BLE001 — fail open to the hardcoded set
        log.error("style_sets_load_failed", error=str(exc), path=path)
        return _FALLBACK_STYLE_SETS


_STYLE_SETS_DATA = _load_style_sets()
_SETS_BY_ID: dict[str, dict] = {s["id"]: s for s in _STYLE_SETS_DATA.get("sets", [])}
STYLE_SETS_VERSION: str = str(_STYLE_SETS_DATA.get("version", "unknown"))


def list_style_sets(*, applies_to: str | None = None) -> list[dict]:
    """Return curated sets as lightweight dicts for selector prompts / admin.

    When `applies_to` is given (e.g. "music"), only sets eligible for that path
    are returned. Each entry: {id, label, tags, applies_to}.
    """
    out: list[dict] = []
    for s in _STYLE_SETS_DATA.get("sets", []):
        eligible = s.get("applies_to", [])
        if applies_to is not None and applies_to not in eligible:
            continue
        out.append(
            {
                "id": s["id"],
                "label": s.get("label", s["id"]),
                "tags": s.get("tags", []),
                "applies_to": eligible,
            }
        )
    return out


def style_set_ids(*, applies_to: str | None = None) -> list[str]:
    """Just the ids — convenience for runtime validation of an agent's choice."""
    return [s["id"] for s in list_style_sets(applies_to=applies_to)]


def get_style_set(set_id: str | None) -> dict:
    """Return the full set dict for `set_id`, falling back to `default`.

    Never raises — an unknown id logs `style_set_unknown` and returns the
    default set so a bad selector choice can't break a render.
    """
    if set_id and set_id in _SETS_BY_ID:
        return _SETS_BY_ID[set_id]
    if set_id:
        log.warning("style_set_unknown", set_id=set_id, fallback=_DEFAULT_SET_ID)
    return _SETS_BY_ID.get(_DEFAULT_SET_ID, _FALLBACK_STYLE_SETS["sets"][0])


def _role_block(style_set: dict, role: str) -> dict | None:
    """Resolve a role within a set, walking the fallback chain, then the
    default set's same chain. Returns None only if nothing matches anywhere."""
    roles = style_set.get("roles", {})
    if role in roles:
        return roles[role]
    for sibling in _ROLE_FALLBACK.get(role, ()):
        if sibling in roles:
            return roles[sibling]
    # Cross-set fallback to the default set.
    if style_set.get("id") != _DEFAULT_SET_ID:
        default = _SETS_BY_ID.get(_DEFAULT_SET_ID)
        if default:
            return _role_block(default, role)
    # Last resort: any defined role in this set.
    return next(iter(roles.values()), None) if roles else None


def resolve_overlay_style(
    set_id: str | None,
    role: str,
    *,
    advisory: dict | None = None,
) -> dict:
    """Return concrete style fields for (set, role), ready to `entry.update(...)`.

    The set is AUTHORITATIVE: any non-null style key it defines overwrites the
    overlay. `advisory` (the LLM/legacy per-overlay fields such as Gemini's
    `font_color_hex` or a `lyrics_config` value) fills ONLY keys the set leaves
    null/absent — it never overrides the set. Text and timestamps are untouched.

    The returned dict also carries a `timing` sub-dict (role timing knobs:
    pre_roll_s, post_dwell_s, next_line_gap_s, fade_in_ms, fade_out_ms,
    word_reveal_min_gap_s) when the role defines one; callers that compute
    timing (the lyric injector) read it, others ignore it.
    """
    style_set = get_style_set(set_id)
    block = _role_block(style_set, role) or {}
    advisory = advisory or {}

    resolved: dict = {}
    for key in _STYLE_KEYS:
        val = block.get(key)
        if val is not None:
            resolved[key] = val
        elif advisory.get(key) is not None:
            resolved[key] = advisory[key]

    timing = block.get("timing")
    if isinstance(timing, dict):
        # Drop null knobs so callers can use `timing.get(k)` cleanly.
        resolved["timing"] = {k: v for k, v in timing.items() if v is not None}

    return resolved


# The role whose typography best represents a set in a UI preview chip — a
# generative text variant burns the AI hero-intro, which uses the `hook` role.
_PREVIEW_ROLE = "hook"


def style_set_preview(set_id: str) -> dict:
    """Display-only typography of a set's representative role, for the UI picker.

    Resolves `font_family` → `css_family`/`file`/`weight` via the font registry so
    the web app can render a style chip in the REAL typeface + colors BEFORE any
    re-render. These fields are projected to the browser only — they never reach
    the renderer burn dict, which keeps the #296 parity invariant intact. Walks
    the role-fallback chain via `_role_block`; always returns a dict.
    """
    catalog = get_style_set(set_id)
    block = _role_block(catalog, _PREVIEW_ROLE) or {}
    family = block.get("font_family")
    reg = _FONT_REGISTRY.get("fonts", {}).get(family or "", {})
    return {
        "label": catalog.get("label"),
        "tags": catalog.get("tags"),
        "font_family": family,
        "css_family": reg.get("css_family"),
        "font_file": reg.get("file"),
        "font_weight": reg.get("weight"),
        "text_color": block.get("text_color"),
        "highlight_color": block.get("highlight_color"),
        "effect": block.get("effect"),
    }


def style_set_intro_preview(set_id: str) -> dict:
    """Display-only `intro`-role styling of a set, for the instant-edit preview.

    The generative instant editor renders the hero intro client-side (DOM overlay
    on the fast-reburn base video), so it needs the FULL intro look — anchor, x/y
    frac, stroke — not just the chip typography `style_set_preview` projects from
    the hook role. Same projection-only contract: these fields go to the browser
    and never reach the renderer burn dict (#296 parity invariant intact).

    Deliberately omits `text_gradient`: `_resolve_intro_overlay_params` doesn't
    project it into the intro burn, so the server renders solid fill — a gradient
    preview would lie about the committed render. Walks the role-fallback chain
    via `_role_block`; always returns a dict.
    """
    catalog = get_style_set(set_id)
    block = _role_block(catalog, "intro") or {}
    family = block.get("font_family")
    reg = _FONT_REGISTRY.get("fonts", {}).get(family or "", {})
    return {
        "font_family": family,
        "css_family": reg.get("css_family"),
        "font_file": reg.get("file"),
        "font_weight": reg.get("weight"),
        "text_color": block.get("text_color"),
        "highlight_color": block.get("highlight_color"),
        "effect": block.get("effect"),
        "position": block.get("position"),
        "position_x_frac": block.get("position_x_frac"),
        "position_y_frac": block.get("position_y_frac"),
        "text_anchor": block.get("text_anchor"),
        "stroke_width": block.get("stroke_width"),
        "text_size_px": block.get("text_size_px"),
    }


# ── Lyric role ⇄ injector style mapping ──────────────────────────────────────
# The music lyric injector (`app.pipeline.lyric_injector`) runs one of three
# injectors keyed by `style`; each maps to a lyric role in a style set.
_LYRIC_ROLE_TO_STYLE: dict[str, str] = {
    "lyric_karaoke": "karaoke",
    "lyric_line": "line",
    "lyric_word_pop": "per-word-pop",
}
_STYLE_TO_LYRIC_ROLE: dict[str, str] = {v: k for k, v in _LYRIC_ROLE_TO_STYLE.items()}


def lyric_role_for_style(style: str) -> str:
    """Map an injector style ("karaoke"/"line"/"per-word-pop") to its lyric role."""
    return _STYLE_TO_LYRIC_ROLE.get(style, "lyric_line")


def lyric_style_for_set(set_id: str | None) -> str:
    """Pick the injector style implied by a music set.

    Honors an explicit top-level `lyric_style` on the set; otherwise infers it
    when the set defines exactly one lyric role. Falls back to the calm "line"
    style when ambiguous (e.g. the multi-role `default` set) or absent.
    """
    s = get_style_set(set_id)
    explicit = s.get("lyric_style")
    if explicit in _STYLE_TO_LYRIC_ROLE:
        return explicit
    lyric_roles = [r for r in s.get("roles", {}) if r in _LYRIC_ROLE_TO_STYLE]
    if len(lyric_roles) == 1:
        return _LYRIC_ROLE_TO_STYLE[lyric_roles[0]]
    return "line"


# ── Validation (unit-tested; warn-only at import) ────────────────────────────


def validate_style_sets(data: dict | None = None) -> list[str]:
    """Return a list of human-readable problems with the style-set library.

    Asserts every `font_family` exists in the font registry, every `effect` is
    renderer-known, every `position` is a `_POSITION_Y` key, and every role name
    is in `VALID_STYLE_ROLES`. Empty list == valid. Used by the unit test; also
    called at import to log (not raise) so a bad edit is visible in worker logs
    without crashing the boot.
    """
    data = data if data is not None else _STYLE_SETS_DATA
    font_names = set(_FONT_REGISTRY.get("fonts", {}).keys())
    valid_positions = set(_POSITION_Y.keys())
    problems: list[str] = []

    ids_seen: set[str] = set()
    for s in data.get("sets", []):
        sid = s.get("id", "<missing id>")
        if sid in ids_seen:
            problems.append(f"duplicate set id: {sid}")
        ids_seen.add(sid)
        for role, block in s.get("roles", {}).items():
            if role not in VALID_STYLE_ROLES:
                problems.append(f"{sid}.{role}: unknown role")
            ff = block.get("font_family")
            if ff is not None and ff not in font_names:
                problems.append(f"{sid}.{role}: font_family {ff!r} not in font registry")
            eff = block.get("effect")
            if eff is not None and eff not in _ALLOWED_EFFECTS:
                problems.append(f"{sid}.{role}: effect {eff!r} not allowed")
            pos = block.get("position")
            if pos is not None and pos not in valid_positions:
                problems.append(f"{sid}.{role}: position {pos!r} not in _POSITION_Y")
            grad = block.get("text_gradient")
            if grad is not None:
                if not isinstance(grad, dict):
                    problems.append(f"{sid}.{role}: text_gradient must be a dict")
                else:
                    gcolors = grad.get("colors")
                    if not isinstance(gcolors, list) or len(gcolors) < 2:
                        problems.append(
                            f"{sid}.{role}: text_gradient.colors must be a list of ≥2 hex strings"
                        )
                    else:
                        _hex_re_check = __import__("re").compile(r"^#[0-9A-Fa-f]{6}$")
                        for c in gcolors:
                            if not _hex_re_check.match(str(c)):
                                problems.append(
                                    f"{sid}.{role}: text_gradient color"
                                    f" {c!r} is not a valid #RRGGBB hex"
                                )
    if _DEFAULT_SET_ID not in ids_seen:
        problems.append(f"missing required set id {_DEFAULT_SET_ID!r}")
    return problems


_import_problems = validate_style_sets()
if _import_problems:
    log.warning("style_sets_validation_problems", problems=_import_problems)
