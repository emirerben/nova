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
  pinned recipe that overlap the title window. The verify CLI measures each ink box (N3).
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
  non-stack ``none.rows`` equals the sent rows byte for byte; a stack becomes her words as one
  plain row under the job's pinned text policy.
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

Skia is imported lazily (no Skia at import).
"""

from __future__ import annotations

OFFER_VERSION = 1
MAX_LOOKS = 4
MAX_COMPILES = 12
PREVIEW_BYTES_CAP = 192 * 1024

from collections.abc import Mapping, Sequence  # noqa: E402
from typing import Any  # noqa: E402

# Route -> (title family, the route's own marker in ``source_params``). A guided/montage title
# carries no marker.
ROUTE_FAMILIES: dict[str, str] = {
    "talking": "opening-title",
    "narrated": "narrated-title",
    "montage": "guided-title",
}
_ROUTE_MARKERS: dict[str, tuple[str, str] | None] = {
    "talking": ("source", "opening_title"),
    "narrated": ("narrated_storyboard", "intro"),
    "montage": None,
}


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
