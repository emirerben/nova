"""KRI-1633: "Try intro styles", several fitted intro looks the creator picks from, on tap.

The server builds up to four fitted looks (from `intro_scene_pick.pick_candidates`) plus "No intro
style" for a video with an approved title, returns them as complete `TextElement` rows with a
server-compiled preview, and STORES NOTHING. The editor stages the tapped option's rows and her
normal editor-commit Save persists them as ordinary editable title rows (like KRI-1501's caption
translation). No model is called (the model arm, `INTRO_SCENE_MODEL_ENABLED`, stays off).

Data shape: `build_offer(snapshot, variant, title_rows, base_generation)` -> the wire dict in
`routes/intro_styles.py` (``options[*].{key, recommended, current, rows, preview}``). Each look is
`compile_intro_scene` -> `place_title_look` against this variant's own saved evidence -> remapped
to the route's title family -> dry-run through that route's real phone Save -> previewed from its
own placed rows.

Ways this could go wrong, and the guard for each (tests/pipeline/test_intro_styles.py,
tests/routes/test_intro_styles_e2e.py, tests/services/test_intro_styles_edit_ops.py,
tests/pipeline/test_intro_scene_pick.py, and `make verify-intro-styles`):

* F1 an old build, a flag-off or unadmitted account sees the entry or gets rows. The
  ``intro_styles`` capability key is sent only when on and only to a request at/above
  ``intro_styles_min_client_protocol``; the route is 404 ``intro_styles_disabled`` flag-off and 426
  below the minimum. Guard: protocol-56 (426, no key), flag off (404, no key), admitted (key).
* F2 saved preferences block the offer (the KRI-1633 bug: `has_text_ask` counted position and
  favorite fonts). Guard: a policy full of preferences is still offered; the old function blocks it
  (negative control, N1 in the verify report).
* F3 her explicit look ask for this title is ignored. ``requested.title`` font_family / color /
  stroke_width / shadow_enabled -> no capability and ``available:false, reason:title_look_asked``;
  a position alone does not block.
* F4 a font she excluded is offered. No pair whose solid or accent family is in any
  ``excluded_fonts`` list is issued.
* F5 the has_text_ask change restyles already-stamped jobs, or new jobs get a silent scene.
  `stamp_intro_scene` stamps only for the model arm; new True implies old True, so a KRI-1611 stamp
  re-renders byte-identically (golden `tests/fixtures/kri1633_old_stamp_rows.json`).
* F6 options not fitted (ink over her face, captions, logo or other text, outside the safe zone).
  Every look is placed with the real `place_title_look` against the variant's own evidence and its
  receipt must pass `placement_is_clean`; routes avoid the other text and media layers in the
  pinned recipe that overlap the title window (a caption that repeats her words is still a caption:
  layers are matched whole, and a ``caption-*`` layer is never her title). The verify CLI measures
  each ink box and the recipe's caption boxes on its own (N3, N7). Known v1 limit: placement
  works on layout boxes, and a few script faces draw ink a hair outside them (<1% of offered
  looks on random jobs, measured by raster); the tile is drawn over her frame so she can judge.
* F7 request-time face boxes are in another coordinate space than the worker's. The saved
  receipt's canvas-space ``face_band`` is replayed through a stub sampler on a canvas-sized
  display; a parity test re-places today's plain title and must reproduce its decision and
  ``default_y_frac`` within 0.01. No face evidence: ``face_source: typical``, top-band zones only.
* F8 duplicate options, fewer than promised, or a ``pen`` look approval cannot read (KRI-1624). At
  most 4 looks, distinct font_pair, ``pen`` never issued; 0 looks is ``available:false``.
* F9 her words are changed, dropped or reordered, or an unsaved title edit is lost. Words come from
  the ``title_rows`` the editor sends; `check_scene` runs; each look's rows read in line order equal
  her words (case-blind, Turkish-safe).
* F10 Turkish breaks. `issue_menu` filters faces by coverage; rows carry ``text_language: tr``; the
  preview layers show "TRENDİ" for "trendi" in a caps pair.
* F11 she picks a look and Save 422s. Each look is dry-run through the route's real phone Save
  (`prepare_editor_commit` on a detached copy, no DB write); a refused look is dropped; all refused
  is ``save_refused``.
* F12 Save weakens or refuses the render contract. An intro stack is matched as ONE text
  (`intro_plain_text`) when requirements are inherited, and `any` falls back to
  `stacked_title_matches` like `opening`.
* F13 chat ops / her edits do not override the pick or override only line 1. The title family is
  `opening-title` / `narrated-title` / `guided-title` (+ ``-line-N``): ops aimed at the title hit
  every line; a rewrite collapses the stack; a remove takes every line.
* F14 the pick does not survive the next render (a later caption, music or timeline Save, the
  guided recompile). E2E: pick Save then an unrelated Save; the pinned recipe's title layers equal
  the picked rows' layers, per route.
* F15 "No intro style" changes a title that was never styled, or does not restore today's title. A
  non-stack ``none.rows`` equals the sent rows byte for byte. Every look carries the title it
  replaced on line 1's ``intro_style.restore`` (the worker's font, size and place are not kept
  anywhere else), so "none" gives that title back while the words are still hers (N8); a stack
  without it (or with her words changed) becomes her words as one plain row under the job's
  pinned text policy, an approximation of the route's builder.
* F16 an offer built on an old baseline is applied over a newer render: 409 ``baseline_conflict``,
  and editor-commit's own compare-and-fail.
* F17 previews differ from what is applied, or fall back to text. Each preview is compiled from its
  option's own placed rows ('none' too); all-or-nothing; dropped past 192 KiB.
* F18 request-time cost / locking: the handler reads a detached copy, releases the session,
  computes in a threadpool, writes nothing and caps compiles at ``MAX_COMPILES`` per request.
* F19 ink unreadable on her footage is an accepted v1 limit (nothing is measured at request time):
  each look takes the first light ink of its seeded order. Per-footage ink stays with KRI-1619.
* F20 routes that cannot save a title get a broken offer (voiceover montage, speech montage, slide
  posts, cloud/web, landscape): absent capability with a reason code, tested per route.
* F21 the model is called. This module never imports `agents/intro_scene_designer`.
* F22-F25 are iOS (replace transaction, ``source_params`` round trip, entry gating, chrome).
* F26 protocol collision with other open PRs: `scripts/bump-protocol.py` is re-run after every
  rebase and tests compare against the constant.

KRI-1615 stage B: a Talking look may carry her own photos as cards, laid out with the title
(`intro_scene` arrangements, `services/intro_props_layout`). A card look is one more option, never
automatic; its ``cards`` are `IntroCard`s (the wire stage C parses). Her photos come from
`services/intro_card_pool` (one rule for the offer's query and the Save's pin), oldest first, as
many as the arrangement has slots. The M-numbers continue the intro-scene list (M1-M30 are in
`intro_scene`). Guards: tests/routes/test_intro_cards_e2e.py (real PostgreSQL, the real routes,
the real Save, only the storage download replaced) and `make verify-intro-styles`.

* M31 an old build, or the switch off, gets a card. Card looks are built only when the route
  resolved `cards_allowed` (``INTRO_CARDS_ENABLED`` and a build at ``INTRO_CARDS_PROTOCOL``)
  and loaded her photos; otherwise the offer is today's byte for byte, and the new build's
  offer minus its card options equals it.
* M32 a photo from another item or another user, or outside the item's pool namespace, becomes
  a card. `card_photo` checks owner, item and path for the offer and for the Save's pin, and
  the bind re-checks them before it reads a byte; a Save that posts such a card is refused
  with today's "isn't a photo Kria pinned" error (the wrong-content control).
* M33 a photo that is not ready, has no generation, is transparent (a sticker, KRI-1616), has
  no measured upright shape, is library-backed, deduplicated or too large. Excluded by
  `card_photo`, so neither offered nor pinned.
* M34 fewer photos than slots. Two or three photos issue ``around_speaker`` only, four or more
  also ``grid_2x2``; under two, no card look.
* M35 the arrangement does not fit her real framing. No card look (the reason is logged);
  measured on real Talking face bands by `app/cli/intro_cards_fit.py` (docs/pipelines/
  intro-scene.md): on a typical close speaker the title alone fills the room above her head.
* M36 cards or text already on screen with the title (the worker's cards, cards she placed, a
  corner line). Her content stays: no card look (``cards_in_the_way``). Intro cards of an
  earlier apply are not in the way.
* M37 an option applied twice, or another applied over a card look, stacks cards. Card ids are
  the slots' (``intro-card-<n>``); applying ANY option, plain looks and "No intro style"
  included, replaces every ``intro-card-*`` card with that option's ``cards`` (none for a plain
  look). Two Saves of the same look give the same lanes; the next offer marks it ``current``.
* M38 a Save after she deletes one card fails or moves the other. The deleted card leaves the
  lanes and the recipe; the other keeps its place. Its photo's receipt stays (append-only, like
  every pool pin).
* M39 an offer on an old baseline. 409 ``baseline_conflict`` before her photos are read.
* M40 a Turkish title. Card looks compile and save in TR like EN.
* M41 a bottom card lands on her captions, or the phone lifts it onto her face. The Save marks
  ``caption_clear`` itself (`intro_card_pool.caption_clear_decisions`: her captions'
  `caption_band`, the photo's shape, the card's place), never from the client; a look is
  offered only when the dry-run Save compiles every card exactly where the offer placed it
  (``card_moved`` otherwise).
* M42 the dry run proves less than the Save. It pins provisional receipts (no bytes are read at
  request time); the real Save binds the bytes and can still refuse
  ``intro_card_unavailable``.
* M43 the preview shows something else. A card look's preview carries its cards' boxes; previews
  stay all or nothing under `PREVIEW_BYTES_CAP`.
* M44 the request gets slow. Card looks have their own `MAX_CARD_COMPILES`, after the plain
  looks.
* M45 a card look outside a Talking edit with her face saved. Narrated and montage have no face
  evidence and no card lane; a typical face is never assumed for cards.

Skia is imported lazily (no Skia at import).
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.pipeline.title_family import retime_title_rows, title_window

log = structlog.get_logger(__name__)

OFFER_VERSION = 1
MAX_LOOKS = 4
MAX_COMPILES = 12
PREVIEW_BYTES_CAP = 192 * 1024
MAX_TITLE_CHARS = 160
MAX_TITLE_ROWS = 5
_PREVIEW_AFTER_ENTRANCE_S = 1.2
_PREVIEW_SHARE = 0.6
# The ink to trust when nothing is measured at request time (F19): as today's white title.
_LIGHT_INK_LUMINANCE = 0.5

ROUTE_FAMILIES: dict[str, str] = {
    "talking": "opening-title",
    "narrated": "narrated-title",
    "montage": "guided-title",
}
# The route's own marker in ``source_params`` (a guided/montage title carries none).
_ROUTE_MARKERS: dict[str, tuple[str, str] | None] = {
    "talking": ("source", "opening_title"),
    "narrated": ("narrated_storyboard", "intro"),
    "montage": None,
}
# Top-band zones are the only ones tried without face evidence: the head is unknown.
_TOP_BAND_ZONES = ("above_head", "top_left")


class TitleRowsInvalid(ValueError):
    """``title_rows`` the editor sent that this variant cannot offer styles for (422)."""


# --- the route table ----------------------------------------------------------------------------


def route_of(variant: Mapping[str, Any], assembly: Mapping[str, Any]) -> tuple[str | None, str]:
    """``(route, reason)``: the route that can save a title for ``variant``, else ``None`` and the
    reason code. The route table of the spec: Talking, narrated and guided/montage are IN;
    voiceover montage, speech montage, authored timelines, slides, cloud and landscape are OUT."""
    if variant.get("render_destination") != "device":
        return None, "not_phone_render"
    if variant.get("orientation") == "landscape":
        return None, "canvas_unsupported"
    archetype = variant.get("resolved_archetype")
    if variant.get("editor_timeline_mode") == "authored":
        return None, "route_unsupported"
    if archetype == "subtitled":
        return "talking", "ok"
    if archetype == "narrated":
        return "narrated", "ok"
    if archetype == "guided_story" and "guided_story_execution_plan" in assembly:
        return "montage", "ok"
    return None, "route_unsupported"


def title_look_asked(variant: Mapping[str, Any], assembly: Mapping[str, Any]) -> bool:
    """Did she ask for a look for this title (the pinned policy's ``requested.title``)?"""
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD  # noqa: PLC0415

    policy = variant.get(PHONE_TEXT_STYLE_FIELD) or assembly.get(PHONE_TEXT_STYLE_FIELD)
    return pick.has_text_ask(policy)


def protocol_allows(protocol: int | None) -> bool:
    from app.config import settings  # noqa: PLC0415

    return protocol is not None and protocol >= settings.intro_styles_min_client_protocol


def capability(
    job: Any,
    variant: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    protocol: int | None,
) -> bool:
    """Cheap (no Skia, no DB): may this variant show "Try intro styles"? Flag, protocol, a phone
    portrait variant on a route that can save a title, a saveable title lane, no look ask. The
    key is sent only when True, so every closed payload keeps its exact shape (F1)."""
    from app.config import settings  # noqa: PLC0415

    try:
        if not settings.intro_scene_for(getattr(job, "user_id", None)):
            return False
        if not protocol_allows(protocol):
            return False
        assembly = getattr(job, "assembly_plan", None) or {}
        route, _reason = route_of(variant, assembly)
        if route is None or capabilities.get("text_elements") is not True:
            return False
        return not title_look_asked(variant, assembly)
    except Exception as exc:  # noqa: BLE001 - a missing entry never breaks the editor payload
        log.warning("intro_styles.capability_failed", error=str(exc)[:200])
        return False


# --- her rows -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Title:
    """What the editor sent, read once: her words, window, language and whether it is a stack."""

    rows: tuple[dict[str, Any], ...]
    words: str  # typed form (" / " between typed lines) or her plain words
    start_s: float
    end_s: float
    language: str | None
    is_intro_stack: bool
    intro_key: str | None
    # The plain title this stack replaced, carried on line 1's ``intro_style.restore`` so "No intro
    # style" can give it back exactly (F15); for a title that is not a stack, its own rows.
    restore: tuple[dict[str, Any], ...] | None = None


def read_title_rows(rows: Sequence[Any], route: str) -> Title:
    """Validate the rows the editor sent (F9): 1-5 `TextElement`s of THIS route's title family,
    at most `MAX_TITLE_CHARS` of text, one window. Raises `TitleRowsInvalid`."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.phone_title_look import (  # noqa: PLC0415
        _look_params,
        intro_plain_text,
        is_title_look_row,
        title_family_of,
        title_look_text,
    )

    family = ROUTE_FAMILIES[route]
    if not isinstance(rows, Sequence) or not 1 <= len(rows) <= MAX_TITLE_ROWS:
        raise TitleRowsInvalid("title_rows needs 1-5 rows")
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or title_family_of(row.get("id")) != family:
            raise TitleRowsInvalid("a row is not in this video's title family")
        try:
            element = TextElement.model_validate(dict(row))
        except Exception as exc:  # noqa: BLE001 - untrusted editor rows
            raise TitleRowsInvalid("a title row is invalid") from exc
        if element.removed or not str(element.text or "").strip():
            raise TitleRowsInvalid("a title row is empty or removed")
        clean.append(dict(row))
    if sum(len(str(row["text"])) for row in clean) > MAX_TITLE_CHARS:
        raise TitleRowsInvalid("the title is too long")
    if len({row["id"] for row in clean}) != len(clean):
        raise TitleRowsInvalid("duplicate title row ids")
    look = [row for row in clean if is_title_look_row(row)]
    stack = intro_plain_text(clean) if len(clean) >= 2 and len(look) == len(clean) else None
    if len(clean) >= 2 and len(look) != len(clean):
        raise TitleRowsInvalid("a title of several rows must be one look")
    restore: tuple[dict[str, Any], ...] | None = None
    if stack is not None:
        words = stack
        first = min(look, key=lambda row: int((_look_params(row) or {}).get("line") or 0))
        style = (_look_params(first) or {}).get("intro_style") or {}
        key = style.get("key")
        saved = style.get("restore")
        if isinstance(saved, list) and 1 <= len(saved) <= MAX_TITLE_ROWS:
            restore = tuple(copy.deepcopy(dict(row)) for row in saved if isinstance(row, Mapping))
    elif len(clean) >= 2:
        words = title_look_text(clean) or ""
        key = None
    else:
        words = " ".join(str(clean[0]["text"]).split())
        key = None
    if stack is None:
        restore = tuple(copy.deepcopy(row) for row in clean)
    start = min(float(row.get("start_s") or 0.0) for row in clean)
    end = max(float(row.get("end_s") or 0.0) for row in clean)
    if end <= start:
        raise TitleRowsInvalid("the title has no visible window")
    language = next((row.get("text_language") for row in clean if row.get("text_language")), None)
    return Title(
        rows=tuple(clean),
        words=words,
        start_s=start,
        end_s=end,
        language=language if isinstance(language, str) else None,
        is_intro_stack=stack is not None,
        intro_key=key if isinstance(key, str) else None,
        restore=restore,
    )


def remap_to_route(rows: Sequence[Mapping[str, Any]], route: str) -> list[dict[str, Any]]:
    """``rows`` (a compiled stack, any family) as ``route``'s title rows: line 1 keeps the family
    id, later lines are ``<family>-line-N``, and the route's own marker replaces the others."""
    from app.pipeline.phone_title_look import TITLE_LOOK_PARAM, _look_params  # noqa: PLC0415

    family = ROUTE_FAMILIES[route]
    marker = _ROUTE_MARKERS[route]
    ordered = sorted(rows, key=lambda row: int((_look_params(row) or {}).get("line") or 0))
    out: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        params = {
            key: value
            for key, value in dict(row.get("source_params") or {}).items()
            if key not in ("source", "narrated_storyboard")
        }
        if marker is not None:
            params[marker[0]] = marker[1]
        if TITLE_LOOK_PARAM in params:
            params[TITLE_LOOK_PARAM] = dict(params[TITLE_LOOK_PARAM])
        out.append(
            {
                **row,
                "id": family if index == 0 else f"{family}-line-{index + 1}",
                "source_params": params,
            }
        )
    return out


# --- evidence -----------------------------------------------------------------------------------


@dataclass
class Evidence:
    """What the placement may use: the saved face (canvas space), the boxes to keep clear."""

    face: Any = None  # NormalizedBox | None
    face_source: str = "none"  # "saved" (a detected face) | "typical" | "none"
    avoid: list[Any] = field(default_factory=list)
    zones: tuple[str, ...] = tuple(scene_mod.ZONES)

    @property
    def top_band_only(self) -> bool:
        return self.face_source != "saved"


def _box(value: Mapping[str, Any]) -> Any:
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    return NormalizedBox(
        float(value["left"]), float(value["top"]), float(value["right"]), float(value["bottom"])
    )


def saved_face_band(variant: Mapping[str, Any]) -> Any:
    """The canvas-space face band the worker placed Talking's title against (its receipt's
    ``face_band``), or ``None`` when the receipt has no face evidence (F7)."""
    receipt = variant.get("opening_title_placement")
    band = receipt.get("face_band") if isinstance(receipt, Mapping) else None
    if not isinstance(band, Mapping):
        return None
    try:
        return _box(band)
    except (KeyError, TypeError, ValueError):
        return None


def recipe_layers(assembly: Mapping[str, Any], variant_id: str) -> list[dict[str, Any]]:
    """The pinned device recipe's text layers as plain dicts; ``[]`` when unreadable."""
    from app.services.device_render import DEVICE_RENDER_FIELD  # noqa: PLC0415

    try:
        record = (assembly.get(DEVICE_RENDER_FIELD) or {}).get(variant_id) or {}
        request = record.get("request") or (record.get("status") or {}).get("request") or {}
        layers = (request.get("recipe") or {}).get("text_layers") or []
        return [layer for layer in layers if isinstance(layer, dict)]
    except Exception:  # noqa: BLE001 - no layers only means no avoid boxes
        return []


def layer_box(layer: Mapping[str, Any], canvas: Any) -> Any:
    """A conservative canvas-normalized box around a recipe text layer's runs (width from the
    glyph count: the layer carries only its origin and size)."""
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    left = top = 1.0
    right = bottom = 0.0
    for run in layer.get("runs") or []:
        size = float(run.get("font_size") or 0)
        x = float(run.get("x") or 0)
        base = float(run.get("baseline_y") or 0)
        width = 0.55 * size * len(str(run.get("text") or ""))
        left = min(left, x / canvas.width)
        right = max(right, (x + width) / canvas.width)
        top = min(top, (base - size) / canvas.height)
        bottom = max(bottom, (base + 0.3 * size) / canvas.height)
    if right <= left or bottom <= top:
        return None
    return NormalizedBox(max(0.0, left), max(0.0, top), min(1.0, right), min(1.0, bottom))


def other_layers(assembly: Mapping[str, Any], variant_id: str, title: Title) -> list[dict]:
    """The OTHER text layers of the pinned recipe shown during the title's window (the captions,
    a corner line, a closing line). The layers that draw her current title are not other text:
    those are matched whole (a caption that repeats her words, which she usually says, is NOT her
    title), and a caption layer never is."""
    from app.text_fold import fold_text  # noqa: PLC0415

    own = {fold_text(" ".join(str(row["text"]).split())) for row in title.rows}
    own.add(fold_text(title.words))
    out: list[dict] = []
    for layer in recipe_layers(assembly, variant_id):
        if (
            float(layer.get("end", 0)) <= title.start_s
            or float(layer.get("start", 0)) >= title.end_s
        ):
            continue
        text = fold_text(" ".join(str(run.get("text") or "") for run in layer.get("runs") or []))
        if not text.strip() or (
            text in own and not str(layer.get("id") or "").startswith("caption")
        ):
            continue
        out.append(layer)
    return out


def avoid_boxes(
    assembly: Mapping[str, Any], variant_id: str, title: Title, canvas: Any
) -> list[Any]:
    """Boxes of the other text layers (`other_layers`): the title keeps clear of them (F6)."""
    boxes = (layer_box(layer, canvas) for layer in other_layers(assembly, variant_id, title))
    return [box for box in boxes if box is not None]


def evidence_for(
    route: str,
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    title: Title,
    canvas: Any,
) -> Evidence:
    """Face evidence (Talking, from its receipt) and the boxes to avoid. Narrated and montage
    have no face evidence at request time: top-band zones only. A Talking receipt without a
    detected face is ``typical`` (the typical speaker stands in; top-band zones only)."""
    avoid = avoid_boxes(assembly, str(variant.get("variant_id") or ""), title, canvas)
    face = saved_face_band(variant) if route == "talking" else None
    if face is not None:
        return Evidence(face=face, face_source="saved", avoid=avoid)
    typical = scene_mod.__getattr__("TYPICAL_FACE") if route == "talking" else None
    return Evidence(
        face=typical,
        face_source="typical" if route == "talking" else "none",
        avoid=avoid,
        zones=_TOP_BAND_ZONES,
    )


def place_with(evidence: Evidence, canvas: Any) -> Callable[..., Any]:
    """The real `place_title_look` against ``evidence``: a stub sampler answers with the saved
    canvas-space face on a canvas-sized display (identity mapping), the `place_on_typical_face`
    pattern, so the request sees the face the worker saw (F7)."""
    from app.pipeline.phone_title_look import place_title_look  # noqa: PLC0415
    from app.pipeline.render_geometry import ProtectedRegion  # noqa: PLC0415

    def sample(_path, anchors, **_kwargs):  # noqa: ANN001, ANN202
        regions = [ProtectedRegion(0.0, 1e6, evidence.face, kind="face")] if evidence.face else []
        return regions, {
            "attempted": len(anchors),
            "decoded": len(anchors),
            "detected": len(regions),
        }

    def place(rows: list[dict], position: str | None) -> tuple[list[dict], dict[str, Any]]:
        return place_title_look(
            rows,
            clip_path="request-time-evidence.mp4",
            keep_segments=[(0.0, 1e6)],
            display_width=canvas.width,
            display_height=canvas.height,
            canvas=canvas,
            sample=sample,
            position=position,
            avoid=evidence.avoid,
        )

    return place


# --- ids, seeds ---------------------------------------------------------------------------------


def _digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def offer_id(job_id: Any, variant_id: str, base_generation: str, words: str) -> str:
    return "is1-" + _digest(job_id, variant_id, base_generation, words, OFFER_VERSION)[:16]


def offer_seed(job_id: Any, variant_id: str, words: str) -> str:
    return _digest(job_id, variant_id, words)[:16]


def first_light_ink(seed: str, key: str) -> str:
    """The first light ink of the seeded order (F19: nothing is measured at request time)."""
    from app.pipeline import caption_legibility as legibility  # noqa: PLC0415

    order = pick.ink_order(f"{seed}|{key}")
    return next(
        (
            n
            for n in order
            if legibility.relative_luminance(scene_mod.INKS[n]) >= _LIGHT_INK_LUMINANCE
        ),
        "white",
    )


# --- detached job -------------------------------------------------------------------------------


def detach(job: Any) -> SimpleNamespace:
    """A plain copy of the job's loaded columns (deep-copied plan), safe to use after the session
    is released and to mutate in a dry-run Save (F11, F18)."""
    values = {k: v for k, v in vars(job).items() if not k.startswith("_")}
    for key in ("assembly_plan", "all_candidates"):
        values[key] = copy.deepcopy(values.get(key) or {})
    return SimpleNamespace(**values)


def _variant_of(job: Any, variant_id: str) -> dict[str, Any] | None:
    return next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants") or []
            if isinstance(v, dict) and v.get("variant_id") == variant_id
        ),
        None,
    )


# --- the dry-run Save ---------------------------------------------------------------------------


def dry_run_save(
    job: Any,
    variant_id: str,
    route: str,
    option_rows: Sequence[Mapping[str, Any]],
    *,
    base_generation: str,
    save_arguments: Mapping[str, Any],
    cards: Sequence[IntroCard] = (),
    photos: Sequence[Any] = (),
) -> str | None:
    """Push ``option_rows`` through this route's real phone Save on a throwaway copy (nothing is
    written anywhere) and return the refusal, or ``None`` when Save accepts them (F11).

    With ``cards`` (KRI-1615) the Save also posts them after her other cards, as the editor does,
    with provisional receipts for ``photos`` (no bytes are read here: M42) and the server's own
    ``caption_clear`` marks; it refuses (``card_moved``) unless every card compiles exactly where
    the offer placed it (M41)."""
    from app.pipeline.phone_title_look import title_family_of  # noqa: PLC0415
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    scratch = detach(job)
    variant = _variant_of(scratch, variant_id) or {}
    keep = [
        copy.deepcopy(row)
        for row in variant.get("text_elements") or []
        if isinstance(row, dict) and title_family_of(row.get("id")) is None
    ]
    revision = None
    try:
        revision = gj._guided_v2_revision(scratch, variant) if route == "montage" else None
    except Exception:  # noqa: BLE001 - no revision token: the Save is judged without one
        revision = None
    try:
        extra = _stage_cards(scratch, variant, cards, photos) if cards else {}
        body = gj.EditorCommitRequest(
            base_generation=base_generation,
            text_elements=[*[copy.deepcopy(dict(r)) for r in option_rows], *keep],
            **({"guided_revision_number": revision["revision_number"]} if revision else {}),
            **extra,
        )
        gj.prepare_editor_commit(scratch, variant_id, body, **dict(save_arguments))
    except Exception as exc:  # noqa: BLE001 - any refusal drops this look
        detail = getattr(exc, "detail", None)
        code = detail.get("code") if isinstance(detail, dict) else None
        return str(code or detail or type(exc).__name__)[:120]
    return _card_moved(scratch, variant_id, cards) if cards else None


# --- the fit ------------------------------------------------------------------------------------


@dataclass
class Look:
    key: str
    rows: list[dict[str, Any]]
    zone: str
    ink: str
    entrance: str


def _non_pen_entrance(menu: scene_mod.IntroMenu, pair: str, seed: str) -> str:
    """An entrance for ``pair`` that is never ``pen`` (F8, KRI-1624: approval cannot read it)."""
    options = [e for e in menu.entrances_for(pair) if e != "pen"]
    return options[int(_digest(seed, pair, "entrance")[:8], 16) % len(options)]


def fit_looks(
    title: Title,
    *,
    route: str,
    evidence: Evidence,
    canvas: Any,
    seed: str,
    offer: str,
    language: str | None,
    excluded_fonts: Sequence[str],
    timeline_duration_s: float,
    budget: list[int],
    declines: dict[str, int],
    accept: Callable[[Look], str | None] | None = None,
    restore: Sequence[Mapping[str, Any]] | None = None,
) -> list[Look]:
    """Up to `MAX_LOOKS` fitted looks with distinct font pairs. Each candidate x zone is compiled
    (`compile_intro_scene`, no model) and placed with the real `place_title_look` against
    ``evidence``; a look is kept only when its real receipt is clean (F6). ``budget`` is the
    remaining compile count (shared, F18). ``accept`` is the per-look Save dry-run: a refusal
    drops the look (F11). Never raises."""
    menu = scene_mod.issue_menu(title.words)
    candidates = pick.pick_candidates(
        title.words, seed=seed, recent=(), excluded_fonts=frozenset(excluded_fonts)
    )
    place = place_with(evidence, canvas)
    window = round(title.end_s - title.start_s, 3)
    looks: list[Look] = []
    used: set[str] = set()
    for candidate in candidates:
        pair = candidate["font_pair"]
        if len(looks) >= MAX_LOOKS:
            break
        if pair in used or pair not in menu.font_pairs:
            continue
        entrance = candidate["entrance"]
        if entrance == "pen":
            entrance = _non_pen_entrance(menu, pair, seed)
        ink = first_light_ink(seed, pair)
        zones = [z for z in candidate["zones"] if z in evidence.zones]
        for zone in zones:
            if budget[0] <= 0:
                declines["compile_budget"] = declines.get("compile_budget", 0) + 1
                return looks
            budget[0] -= 1
            try:
                scene = scene_mod.IntroScene(
                    decision="compose",
                    voices=tuple(candidate["voices"]),
                    font_pair=pair,
                    ink=ink,
                    zone=zone,
                    entrance=entrance,
                    sound="none",
                    stagger=candidate.get("stagger", scene_mod.TOGETHER),
                    stack=candidate.get("stack", scene_mod.GAP),
                )
                out = scene_mod.compile_intro_scene(
                    scene,
                    title.words,
                    canvas=canvas,
                    timeline_duration_s=max(timeline_duration_s, window),
                    language=language,
                    duration_s=window,
                )
                if out.rows is None:
                    declines[out.reason] = declines.get(out.reason, 0) + 1
                    continue
                rows = retime_title_rows(out.rows, title.start_s, title.end_s)
                placed, receipt = place(rows, out.position)
                if not scene_mod.placement_is_clean(receipt):
                    declines["placement_unclean"] = declines.get("placement_unclean", 0) + 1
                    continue
                look = Look(
                    pair,
                    _tagged(placed, route, offer, pair, entrance, zone, ink, restore),
                    zone,
                    ink,
                    entrance,
                )
                refusal = accept(look) if accept else None
                if refusal:
                    declines[f"save_refused:{refusal}"] = (
                        declines.get(f"save_refused:{refusal}", 0) + 1
                    )
                    continue
                looks.append(look)
                used.add(pair)
                break
            except Exception as exc:  # noqa: BLE001 - one look failing never fails the offer
                log.warning("intro_styles.fit_failed", error=str(exc)[:200], pair=pair)
                declines["fit_error"] = declines.get("fit_error", 0) + 1
    return looks


def _tagged(
    placed: Sequence[Mapping[str, Any]],
    route: str,
    offer: str,
    pair: str,
    entrance: str,
    zone: str,
    ink: str,
    restore: Sequence[Mapping[str, Any]] | None = None,
    arrangement: str | None = None,
) -> list[dict[str, Any]]:
    """The placed rows as ``route``'s title rows, each carrying ``title_look.intro_style`` (so the
    next offer can mark the current look and tell a stack from a plain title). Line 1 also carries
    ``restore``, the title this look replaces, for "No intro style" (F15). A card look's rows
    also name its ``arrangement`` and take its key."""
    from app.pipeline.phone_title_look import TITLE_LOOK_PARAM  # noqa: PLC0415

    rows = remap_to_route(placed, route)
    style = {
        "version": 1,
        "offer_id": offer,
        "key": card_look_key(pair, arrangement) if arrangement else pair,
        "font_pair": pair,
        "entrance": entrance,
        "zone": zone,
        "ink": ink,
        **({"arrangement": arrangement} if arrangement else {}),
    }
    for index, row in enumerate(rows):
        look = dict(row["source_params"][TITLE_LOOK_PARAM])
        look["intro_style"] = dict(style)
        if index == 0 and restore:
            look["intro_style"]["restore"] = copy.deepcopy([dict(r) for r in restore])
        row["source_params"] = {**row["source_params"], TITLE_LOOK_PARAM: look}
    return rows


# --- her photos as cards (KRI-1615 stage B, M31-M45) --------------------------------------------

MAX_CARD_COMPILES = 8
# A card look's title sits between its card rows, so only the top-band zones are tried.
_CARD_ZONES = ("above_head", "top_left")
_PLACEMENT_EPS = 1e-6


class IntroCard(BaseModel):
    """One card of a card look, as the offer sends it (stage C parses exactly this).

    Canvas fractions of the 1080x1920 frame: ``x_frac``/``y_frac`` the centre, ``scale`` the
    width; the phone draws the photo at its own shape. The card shows with the title
    (``start_s``-``end_s``). ``caption_clear`` is what the Save will mark from the same server
    evidence; the Save never reads it from the client (a `MediaOverlay` has no such field)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    slot: str
    media_id: str
    gcs_path: str
    generation: str
    x_frac: float
    y_frac: float
    scale: float
    caption_clear: bool
    z: int
    start_s: float
    end_s: float

    def media_overlay(self) -> dict[str, Any]:
        """The editor Save's ``media_overlays`` entry for this card."""
        return {
            "id": self.id,
            "kind": "image",
            "src_gcs_path": self.gcs_path,
            "display_mode": "pip",
            "position": "custom",
            "x_frac": self.x_frac,
            "y_frac": self.y_frac,
            "scale": self.scale,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "z": self.z,
        }


@dataclass
class CardLook:
    look: Look
    cards: list[IntroCard]
    boxes: list[Any]  # NormalizedBox per card, slot order (the preview draws these)


def card_look_key(pair: str, arrangement: str) -> str:
    return f"{pair}+{arrangement}"


def cards_allowed(protocol: int | None) -> bool:
    """May this request get card looks (M31)? The switch and a build that parses ``cards``."""
    from app.config import settings  # noqa: PLC0415

    return (
        settings.intro_cards_enabled
        and protocol is not None
        and protocol >= settings.intro_cards_min_client_protocol
    )


def _saved_cards(assembly: Mapping[str, Any], variant: Mapping[str, Any]) -> list[dict]:
    from app.services.phone_subtitled_editor import (  # noqa: PLC0415
        project_phone_subtitled_editor_sections,
    )

    sections = project_phone_subtitled_editor_sections(dict(assembly), dict(variant)) or {}
    return [card for card in sections.get("media_overlays") or [] if isinstance(card, dict)]


def cards_in_the_way(assembly: Mapping[str, Any], variant: Mapping[str, Any], title: Title) -> bool:
    """A card that is not an intro card, or text that is not hers or a caption, shows with the
    title (M36): her content stays, so no card look is offered."""
    from app.services.intro_card_pool import is_intro_card_id  # noqa: PLC0415

    for card in _saved_cards(assembly, variant):
        if is_intro_card_id(card.get("id")):
            continue
        if float(card.get("start_s") or 0) < title.end_s and float(card.get("end_s") or 0) > (
            title.start_s
        ):
            return True
    layers = other_layers(assembly, str(variant.get("variant_id") or ""), title)
    return any(not str(layer.get("id") or "").startswith("caption") for layer in layers)


def card_stage(
    variant: Mapping[str, Any], assembly: Mapping[str, Any], title: Title, evidence: Evidence
) -> Any:
    """The speaker and captions her cards are laid out around, or None (M45): Talking's saved
    face only (a typical face is never assumed for cards), her captions from `caption_band`."""
    from app.services.intro_card_pool import caption_band  # noqa: PLC0415
    from app.services.intro_props_layout import Stage  # noqa: PLC0415

    if evidence.face_source != "saved" or evidence.face is None:
        return None
    return Stage((evidence.face,), caption_band(variant, assembly, (title.start_s, title.end_s)))


def _stage_cards(
    scratch: Any, variant: dict, cards: Sequence[IntroCard], photos: Sequence[Any]
) -> dict[str, Any]:
    """The dry run's ``media_overlays`` (her other cards, then these, as the editor posts them),
    with a provisional receipt for each photo the job has not pinned and the server's marks."""
    from app.services import intro_card_pool as pool  # noqa: PLC0415
    from app.services.phone_sources import PHONE_VISUALS_FIELD  # noqa: PLC0415
    from app.services.phone_subtitled_editor import pinned_visual  # noqa: PLC0415

    plan = scratch.assembly_plan
    posted = [
        *(c for c in _saved_cards(plan, variant) if not pool.is_intro_card_id(c.get("id"))),
        *(card.media_overlay() for card in cards),
    ]
    receipts = [
        {
            "media_id": photo.media_id,
            "gcs_path": photo.gcs_path,
            "generation": photo.generation,
            "sha256": "0" * 64,
            "byte_count": 1,
        }
        for photo in photos
        if pinned_visual(plan, photo.media_id) is None
    ]
    marks = pool.caption_clear_decisions(variant, plan, posted, photos)
    plan[PHONE_VISUALS_FIELD] = [*(plan.get(PHONE_VISUALS_FIELD) or []), *receipts]
    setattr(scratch, pool.CAPTION_CLEAR_ATTR, marks)
    return {"media_overlays": posted}


def _card_moved(scratch: Any, variant_id: str, cards: Sequence[IntroCard]) -> str | None:
    """``card_moved`` unless the recompiled recipe draws every card where the offer put it."""
    from app.services.device_render import device_status  # noqa: PLC0415

    recipe = device_status(scratch, variant_id).request.recipe
    drawn = {
        clip.id: clip.visual_placement
        for track in recipe.tracks
        for clip in track.clips
        if clip.visual_placement is not None
    }
    for card in cards:
        placement = drawn.get(f"subtitled-overlay-{card.id}")
        if placement is None or any(
            abs(float(got if got is not None else -1) - want) > _PLACEMENT_EPS
            for got, want in (
                (placement.width_fraction, card.scale),
                (placement.x_fraction, card.x_frac),
                (placement.y_fraction, card.y_frac),
            )
        ):
            return "card_moved"
    return None


def fit_card_looks(
    title: Title,
    *,
    route: str,
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    evidence: Evidence,
    photos: Sequence[Any],
    canvas: Any,
    seed: str,
    offer: str,
    language: str | None,
    excluded_fonts: Sequence[str],
    timeline_duration_s: float,
    declines: dict[str, int],
    accept: Callable[[Look, list[IntroCard], Sequence[Any]], str | None] | None = None,
    restore: Sequence[Mapping[str, Any]] | None = None,
) -> list[CardLook]:
    """At most one card look per arrangement her photos fill (``around_speaker`` from two,
    ``grid_2x2`` from four), in `ARRANGEMENTS` order. Each candidate x top-band zone is compiled
    with the arrangement on her real stage (`compile_intro_scene` lays the cards and the title out
    together and declines anything that does not fit); the first that the dry-run Save accepts,
    cards where they were placed, is the look. Never raises."""
    from app.services import intro_card_pool as pool  # noqa: PLC0415

    def decline(reason: str) -> list[CardLook]:
        declines[f"cards:{reason}"] = declines.get(f"cards:{reason}", 0) + 1
        return []

    if route != "talking" or len(photos) < 2:
        return decline("too_few_photos") if route == "talking" else []
    stage = card_stage(variant, assembly, title, evidence)
    if stage is None:
        return decline("no_face")
    if cards_in_the_way(assembly, variant, title):
        return decline("in_the_way")
    menu = scene_mod.issue_menu(title.words, photo_count=len(photos))
    tries = [
        (candidate, zone)
        for candidate in pick.pick_candidates(
            title.words, seed=seed, recent=(), excluded_fonts=frozenset(excluded_fonts)
        )
        if candidate["font_pair"] in menu.font_pairs
        for zone in candidate["zones"]
        if zone in _CARD_ZONES
    ]
    window = round(title.end_s - title.start_s, 3)

    def fit(candidate: dict, zone: str, arrangement: str, chosen: list) -> CardLook | str:
        pair = candidate["font_pair"]
        entrance = candidate["entrance"]
        if entrance == "pen":
            entrance = _non_pen_entrance(menu, pair, seed)
        ink = first_light_ink(seed, pair)
        out = scene_mod.compile_intro_scene(
            scene_mod.IntroScene(
                decision="compose",
                voices=tuple(candidate["voices"]),
                font_pair=pair,
                ink=ink,
                zone=zone,
                entrance=entrance,
                sound="none",
                stagger=candidate.get("stagger", scene_mod.TOGETHER),
                stack=candidate.get("stack", scene_mod.GAP),
                arrangement=arrangement,
            ),
            title.words,
            canvas=canvas,
            timeline_duration_s=max(timeline_duration_s, window),
            language=language,
            duration_s=window,
            photo_aspects=[photo.aspect for photo in chosen],
            stage=stage,
        )
        if out.rows is None or out.props is None:
            return out.reason
        rows = retime_title_rows(out.rows, title.start_s, title.end_s)
        start, end = title_window(rows)
        placed = [
            {
                "id": pool.card_id(index),
                "slot": card.slot,
                "media_id": photo.media_id,
                "gcs_path": photo.gcs_path,
                "generation": photo.generation,
                "x_frac": card.x_frac,
                "y_frac": card.y_frac,
                "scale": card.scale,
                "z": card.z,
                "start_s": start,
                "end_s": end,
            }
            for index, (card, photo) in enumerate(zip(out.props.cards, chosen, strict=True))
        ]
        marks = pool.caption_clear_decisions(
            variant,
            assembly,
            [{**card, "src_gcs_path": card["gcs_path"]} for card in placed],
            chosen,
        )
        cards = [IntroCard(**card, caption_clear=marks.get(card["id"], False)) for card in placed]
        look = Look(
            card_look_key(pair, arrangement),
            _tagged(rows, route, offer, pair, entrance, zone, ink, restore, arrangement),
            zone,
            ink,
            entrance,
        )
        refusal = accept(look, cards, chosen) if accept else None
        if refusal:
            return f"save_refused:{refusal}"
        return CardLook(look, cards, [card.box for card in out.props.cards])

    looks: list[CardLook] = []
    for arrangement in menu.arrangements:
        if arrangement == scene_mod.NO_ARRANGEMENT:
            continue
        chosen = list(photos[: scene_mod.ARRANGEMENTS[arrangement].slots])
        for candidate, zone in tries[:MAX_CARD_COMPILES]:
            try:
                result = fit(candidate, zone, arrangement, chosen)
            except Exception as exc:  # noqa: BLE001 - one look failing never fails the offer
                log.warning("intro_styles.card_fit_failed", error=str(exc)[:200])
                result = "fit_error"
            if isinstance(result, CardLook):
                looks.append(result)
                break
            decline(result)
        else:
            if len(tries) > MAX_CARD_COMPILES:
                decline("compile_budget")
    return looks


# --- "No intro style" ---------------------------------------------------------------------------


def plain_title_rows(
    title: Title,
    *,
    route: str,
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    evidence: Evidence,
    canvas: Any,
    timeline_duration_s: float,
) -> list[dict[str, Any]]:
    """Today's plain title for her words and window, built by the route's own builder and styled
    under the job's pinned text policy, then placed clear of the evidence (F15). Raises when the
    builder cannot run (the caller drops the option rather than substituting)."""
    from app.pipeline.informative_text import policy_enabled, shapes_enabled  # noqa: PLC0415
    from app.pipeline.phone_text_style import resolve_phone_text_style  # noqa: PLC0415
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD  # noqa: PLC0415

    window = round(title.end_s - title.start_s, 3)
    policy = variant.get(PHONE_TEXT_STYLE_FIELD) or assembly.get(PHONE_TEXT_STYLE_FIELD) or {}
    plain = " ".join(title.words.replace(" / ", " ").split())
    # The route's own builder, called OUTSIDE a text-policy scope: inside one, the builders'
    # `generation_style_rows` asks the typography agent (a model) to pick fonts. Styling is then
    # the deterministic `resolve_phone_text_style` under the job's pinned policy (F15, F21).
    if route == "talking":
        from app.pipeline.phone_subtitled_title import talking_title_element  # noqa: PLC0415

        row = talking_title_element(
            plain,
            duration_s=window,
            first_word_end_s=None,
            timeline_duration_s=max(timeline_duration_s, window),
            canvas=canvas,
        )
    elif route == "narrated":
        from app.pipeline.phone_narrated_plan import narrated_title_element  # noqa: PLC0415

        element = narrated_title_element(
            plain,
            end_s=window,
            timeline_duration_s=max(timeline_duration_s, window),
            canvas=canvas,
        )
        row = element.model_dump(mode="json", exclude_none=True) if element else None
    else:
        row = _guided_plain_title(plain, window, canvas)
    if row is None:
        raise ValueError("no plain title")
    [row] = resolve_phone_text_style(
        [row],
        preferences=policy.get("preferences"),
        requested=policy.get("requested"),
        selected_fonts=policy.get("selected_fonts"),
        canvas=canvas,
        informative=policy_enabled(policy),
        shapes=shapes_enabled(policy),
    )
    row = {**row, "start_s": title.start_s, "end_s": title.end_s}
    if title.language:
        row["text_language"] = title.language
    placed, receipt = place_with(evidence, canvas)([row], None)
    if not scene_mod.placement_is_clean(receipt):
        placed = [row]
    return [dict(r) for r in placed]


def _guided_plain_title(text: str, window: float, canvas: Any) -> dict[str, Any]:
    """The guided/montage title row as `guided_story` builds it (id, size, ink and shadow)."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    return TextElement(
        id="guided-title",
        text=text,
        start_s=0.0,
        end_s=window,
        role="generative_intro",
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        font_family="Inter-Bold",
        size_px=76,
        color="#FFF8F0",
        highlight_color="#D9FF70",
        stroke_width=0,
        shadow_enabled=True,
        shadow_style="standard",
        effect="static",
        alignment="center",
        max_width_frac=0.84,
    ).model_dump(mode="json", exclude_none=True)


# --- previews -----------------------------------------------------------------------------------


def _frame_under(
    assembly: Mapping[str, Any], variant_id: str, at_s: float
) -> dict[str, Any] | None:
    """The source clip under the title at ``at_s`` (output clock) from the pinned recipe's main
    video track, and the second of it shown there; ``None`` when it cannot be read."""
    from app.services.device_render import DEVICE_RENDER_FIELD  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD  # noqa: PLC0415

    try:
        record = (assembly.get(DEVICE_RENDER_FIELD) or {}).get(variant_id) or {}
        request = record.get("request") or (record.get("status") or {}).get("request") or {}
        tracks = (request.get("recipe") or {}).get("tracks") or []
        main = next((t for t in tracks if t.get("kind") == "video" and t.get("clips")), None)
        if main is None:
            return None
        clip = next(
            (
                c
                for c in main["clips"]
                if float(c["timeline_start"])
                <= at_s
                < float(c["timeline_start"]) + float(c["source_duration"]) / float(c["rate"])
            ),
            main["clips"][-1],
        )
        asset = str(clip["source_asset_id"])
        source_at = float(clip["source_start"]) + max(
            0.0, at_s - float(clip["timeline_start"])
        ) * float(clip["rate"])
        visuals = {str(v.get("media_id")): v for v in assembly.get(PHONE_VISUALS_FIELD) or []}
        media_id = asset[len("visual-") :] if asset.startswith("visual-") else asset
        if media_id in visuals and visuals[media_id].get("kind") == "image":
            return {"media_id": media_id, "media_kind": "image", "at_s": 0.0}
        known = {str(b.get("media_id")) for b in assembly.get(PHONE_SOURCES_FIELD) or []}
        if media_id not in known and media_id not in visuals:
            return None
        return {"media_id": media_id, "media_kind": "video", "at_s": round(source_at, 3)}
    except Exception:  # noqa: BLE001 - no frame means label tiles
        return None


def preview_for(
    rows: Sequence[Mapping[str, Any]],
    *,
    frame_source: Mapping[str, Any],
    variant_id: str,
    canvas: Any,
) -> dict[str, Any] | None:
    """The option's own placed rows compiled for the phone, exactly as the render would draw them
    (no restyle), over her clip under the title (F17). ``None`` when anything fails."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415
    from app.pipeline.portable_text_layout import compile_text_overlay  # noqa: PLC0415

    try:
        _start, end = title_window(rows)
        last_entrance = max(float(r["start_s"]) for r in rows)  # KRI-1618: a late accent line
        time_s = round(
            last_entrance + min(_PREVIEW_AFTER_ENTRANCE_S, _PREVIEW_SHARE * (end - last_entrance)),
            3,
        )
        frame = _frame_under(frame_source, variant_id, time_s)
        if frame is None:
            return None
        overlays = build_overlays_from_text_elements(
            [TextElement.model_validate(dict(r)) for r in rows],
            video_duration_s=max(end, 0.1),
            independent_box_alignment=True,
        )
        layers: list[dict] = []
        fonts: dict[str, str] = {}
        for index, overlay in enumerate(overlays):
            layer, font = compile_text_overlay(overlay, layer_id=f"preview-{index}", canvas=canvas)
            layers.append(layer.model_dump(mode="json", exclude_none=True))
            if font is not None:
                fonts[font.id] = font.catalog_id
        return {**frame, "time_s": time_s, "text_layers": layers, "fonts": fonts}
    except Exception as exc:  # noqa: BLE001 - previews are all or nothing
        log.warning("intro_styles.preview_failed", error=str(exc)[:200])
        return None


# --- the offer ----------------------------------------------------------------------------------


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "version": OFFER_VERSION,
        "available": False,
        "reason": reason,
        "offer_id": None,
        "options": [],
    }


def _timeline_duration(variant: Mapping[str, Any], title: Title) -> float:
    try:
        return max(float(variant.get("duration_s") or 0.0), title.end_s)
    except (TypeError, ValueError):
        return title.end_s


def build_offer(
    job: Any,
    variant_id: str,
    title_rows: Sequence[Any],
    base_generation: str,
    *,
    save_arguments: Mapping[str, Any] | None = None,
    photos: Sequence[Any] = (),
) -> dict[str, Any]:
    """The wire offer for one variant: up to `MAX_LOOKS` fitted looks plus "No intro style".

    ``job`` is a detached copy (`detach`). Writes nothing and calls no model (F18, F21).
    Raises `TitleRowsInvalid` for rows that are not this variant's title (422).

    ``photos`` (KRI-1615, `intro_card_pool.CardPhoto`, oldest first) are her card photos, given
    only when the route resolved `cards_allowed`; with them, card looks follow the plain looks
    (M31: without them the offer is today's)."""
    from app.pipeline.canvas import PORTRAIT  # noqa: PLC0415

    started = time.monotonic()
    assembly = job.assembly_plan or {}
    variant = _variant_of(job, variant_id)
    if variant is None:
        return _unavailable("route_unsupported")
    route, reason = route_of(variant, assembly)
    if route is None:
        return _unavailable(reason)
    if title_look_asked(variant, assembly):
        return _unavailable("title_look_asked")
    title = read_title_rows(title_rows, route)
    canvas = PORTRAIT
    words = scene_mod.title_words(title.words)
    if len(words) < 2:
        return _unavailable("title_too_short")
    from app.pipeline.intro_scene_pick import language_of  # noqa: PLC0415
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD  # noqa: PLC0415

    language = title.language or language_of(title.words, variant.get("caption_language"))
    policy = variant.get(PHONE_TEXT_STYLE_FIELD) or assembly.get(PHONE_TEXT_STYLE_FIELD)
    evidence = evidence_for(route, variant, assembly, title, canvas)
    offer = offer_id(getattr(job, "id", ""), variant_id, base_generation, title.words)
    seed = offer_seed(getattr(job, "id", ""), variant_id, title.words)
    budget = [MAX_COMPILES]
    declines: dict[str, int] = {}
    arguments = dict(save_arguments or {})

    def accept(look: Look) -> str | None:
        return dry_run_save(
            job,
            variant_id,
            route,
            look.rows,
            base_generation=base_generation,
            save_arguments=arguments,
        )

    looks = fit_looks(
        title,
        route=route,
        evidence=evidence,
        canvas=canvas,
        seed=seed,
        offer=offer,
        language=language,
        excluded_fonts=sorted(pick.excluded_font_families(policy)),
        timeline_duration_s=_timeline_duration(variant, title),
        budget=budget,
        declines=declines,
        accept=accept,
        restore=title.restore,
    )
    log_fields = {
        "route": route,
        "looks": len(looks),
        "face_source": evidence.face_source,
        "declines": declines,
        "ms": round((time.monotonic() - started) * 1000),
    }
    if not looks:
        reason = (
            "save_refused"
            if any(k.startswith("save_refused") for k in declines)
            else "nothing_fits"
        )
        if not scene_mod.issue_menu(title.words).font_pairs or not pick.accent_splits(title.words):
            reason = (
                "title_too_short"
                if len(words) < 2 or not pick.accent_splits(title.words)
                else reason
            )
        log.info("intro_styles.offer", reason=reason, **log_fields)
        return _unavailable(reason)

    def accept_cards(look: Look, cards: list[IntroCard], chosen: Sequence[Any]) -> str | None:
        return dry_run_save(
            job,
            variant_id,
            route,
            look.rows,
            base_generation=base_generation,
            save_arguments=arguments,
            cards=cards,
            photos=chosen,
        )

    card_looks = (
        fit_card_looks(
            title,
            route=route,
            variant=variant,
            assembly=assembly,
            evidence=evidence,
            photos=photos,
            canvas=canvas,
            seed=seed,
            offer=offer,
            language=language,
            excluded_fonts=sorted(pick.excluded_font_families(policy)),
            timeline_duration_s=_timeline_duration(variant, title),
            declines=declines,
            accept=accept_cards,
            restore=title.restore,
        )
        if photos
        else []
    )
    options: list[dict[str, Any]] = [
        {
            "key": look.key,
            "recommended": index == 0,
            "current": title.is_intro_stack and title.intro_key == look.key,
            "rows": look.rows,
            "preview": None,
        }
        for index, look in enumerate(looks)
    ]
    options += [
        {
            "key": card_look.look.key,
            "recommended": False,
            "current": title.is_intro_stack and title.intro_key == card_look.look.key,
            "rows": card_look.look.rows,
            "preview": None,
            "cards": [card.model_dump(mode="json") for card in card_look.cards],
        }
        for card_look in card_looks
    ]
    none_rows = _none_rows(
        title, route=route, variant=variant, assembly=assembly, evidence=evidence, canvas=canvas
    )
    if none_rows is None:
        log.info("intro_styles.offer", reason="none_unavailable", **log_fields)
        return _unavailable("nothing_fits")
    options.append(
        {
            "key": "none",
            "recommended": False,
            "current": not title.is_intro_stack,
            "rows": none_rows,
            "preview": None,
        }
    )
    _attach_previews(
        options,
        assembly,
        variant_id,
        canvas,
        cards={look.look.key: preview_cards(look) for look in card_looks},
    )
    if photos:
        log_fields["card_looks"] = len(card_looks)
    log.info(
        "intro_styles.offer", reason="ok", previews=options[0]["preview"] is not None, **log_fields
    )
    return {
        "version": OFFER_VERSION,
        "available": True,
        "reason": None,
        "offer_id": offer,
        "route": route,
        "title_family": ROUTE_FAMILIES[route],
        "language": language,
        "replace_ids": [row["id"] for row in title.rows],
        "options": options,
    }


def _restored_title(title: Title, route: str) -> list[dict[str, Any]] | None:
    """The plain title this stack replaced, when it still reads as her words: the rows exactly as
    the worker styled and placed them, in the current window. The rows came back from the editor,
    so they are validated like any title rows. ``None`` falls back to the route's builder."""
    from app.pipeline.phone_title_look import _case_blind  # noqa: PLC0415

    if not title.restore:
        return None
    try:
        original = read_title_rows(list(title.restore), route)
    except TitleRowsInvalid:
        return None

    def plain(words: str) -> str:  # a typed stack's " / " is not part of her words
        return _case_blind(" ".join(words.replace(" / ", " ").split()))

    if original.is_intro_stack or plain(original.words) != plain(title.words):
        return None
    rows = [copy.deepcopy(dict(row)) for row in original.rows]
    if abs(original.start_s - title.start_s) > 0.01 or abs(original.end_s - title.end_s) > 0.01:
        rows = retime_title_rows(rows, title.start_s, title.end_s)
    return rows


def _none_rows(
    title: Title,
    *,
    route: str,
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    evidence: Evidence,
    canvas: Any,
) -> list[dict[str, Any]] | None:
    """``none.rows``: a title that was never styled is returned byte for byte (a tap changes
    nothing); a stack becomes the plain title it replaced when its words are still hers, else the
    route's own plain title for her words (F15)."""
    if not title.is_intro_stack:
        return [copy.deepcopy(dict(row)) for row in title.rows]
    restored = _restored_title(title, route)
    if restored is not None:
        return restored
    try:
        return plain_title_rows(
            title,
            route=route,
            variant=variant,
            assembly=assembly,
            evidence=evidence,
            canvas=canvas,
            timeline_duration_s=_timeline_duration(variant, title),
        )
    except Exception as exc:  # noqa: BLE001 - a builder that cannot run is reported, not replaced
        log.warning("intro_styles.none_failed", error=str(exc)[:200])
        return None


def preview_cards(card_look: CardLook) -> list[dict[str, Any]]:
    """A card look's cards as its preview draws them (M43): her photo in each box, slot order,
    ``box`` in canvas fractions (the phone draws the photo there, or a plate until it loads)."""
    return [
        {
            "media_id": card.media_id,
            "media_kind": "image",
            "box": {"left": box.left, "top": box.top, "right": box.right, "bottom": box.bottom},
            "z": card.z,
        }
        for card, box in zip(card_look.cards, card_look.boxes, strict=True)
    ]


def _attach_previews(
    options: list[dict[str, Any]],
    assembly: Mapping[str, Any],
    variant_id: str,
    canvas: Any,
    *,
    cards: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Previews are all or nothing: one that cannot be drawn, or a payload past the cap, leaves
    every option without one and the app shows label tiles (F17). A card look's preview also
    carries its ``cards`` (KRI-1615)."""
    previews = [
        preview_for(option["rows"], frame_source=assembly, variant_id=variant_id, canvas=canvas)
        for option in options
    ]
    previews = [
        {**preview, "cards": (cards or {})[option["key"]]}
        if preview is not None and option.get("cards")
        else preview
        for option, preview in zip(options, previews, strict=True)
    ]
    if any(p is None for p in previews):
        return
    staged = [
        {**option, "preview": preview} for option, preview in zip(options, previews, strict=True)
    ]
    if len(json.dumps(staged, separators=(",", ":")).encode("utf-8")) > PREVIEW_BYTES_CAP:
        log.warning("intro_styles.preview_too_large")
        return
    for option, preview in zip(options, previews, strict=True):
        option["preview"] = preview
