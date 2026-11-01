#!/usr/bin/env python3
"""Route scorecard: does every planner-chosen field and every stated ask reach the render, or
the approval card before approval, on the route that renders the draft? (KRI-1097 wave 2)

Generalized from ``score_assos.py`` (which keeps the unified-montage render replay). A fixture
is a recorded or reconstructed draft under ``tests/fixtures/kria_replay/<name>/fixture.json``:

    {"fixture_id", "route": "<RouteId>", "message", "planner_summary", "brief", "strategy",
     "clips": [{"id", "kind", "duration_s", "has_audio"?, "analysis"?}],
     "client_protocol"?, "source": {"recorded": [...], "reconstructed": [...]},
     "checks"?: {"fields": [...], "asks": ["r1", ...],
                 "honored"?: {"fields": {field: key}, "asks": {req_id: key}},
                 "perturb"?: {key: value}, "not_graded"?: {item: why}}}

``checks`` defaults are read from the plan, never from the code: every creator-visible field
the planner set away from the schema default (the pace always, since the card shows it; a length
only when the creator stated one) and every live requirement. A fixture that lists fewer leaves
the rest out of grading; each one left out is counted in the summary, and one with no reason in
``not_graded`` fails the run like an unexpected red.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_route.py --fixture NAME
    make kria-replay FIXTURE=NAME          # a fixture with no score_<name>.py of its own
    ... score_route.py --write-golden-lines  # re-record the card-line golden (review its diff)

Offline: a socket guard fails any network attempt and no model is called. The approval card is
assembled from the same pure functions the draft turn runs (`tasks.kria_runtime`): receipts,
route declines, the route's card lines, once in English and once in Turkish
(`reply_language_for`). On code that predates the route registry it falls back to what that
code did (unified montage only, lines only for approval-readiness accounts), so the same
fixture shows the drops as NOT VERIFIED there.

Checks (VERIFIED / NOT VERIFIED; INCONCLUSIVE when a check could not run). Each carries a
``kind``: ``disclosed_ok`` (applied or said before approval), ``honored`` (applied in the render,
nothing else counts), ``gate`` or ``route`` / ``report``.

1.        route: the draft resolves to the fixture's route (the card reads that route's table).
2.<field> disclosed_ok: the render replay applies the field, or the English AND the Turkish
          card each carry that field's own registry line the creator reads (a whole line, or
          named on the compact line) whose typed code is the code the route's table gives the
          field (``not_applied_on_route`` / ``recorded_only_on_route``), or a route decline
          whose ask's own facts are about that field and whose text is the registry's decline
          for it. Free text never counts, whatever it says.
3.<id>    disclosed_ok: the receipt is met (checked), or a typed decline before approval
          (``not_possible``, checked, with a reason), or for a length ask the card's own
          ``target_duration_s`` registry line; in both languages.
4.<field> honored / 5.<id> honored: the render replay's recipe value meets what was asked
          (``checks.honored``), under the replay contract below.
10.claims gate: every field the route's table marks ``Applied`` is read off the strategy in the
          route's code (``READERS``: a strategy receiver, so ``step.target_duration_s`` is not a
          read), or its ``via`` names a function in the route's own ``READERS`` or one of the
          ``SHARED_CARRIERS`` whose body reads the field off a strategy object (AST); a class,
          another route's file or a symbol that only names the field is no carrier. A reader
          file or function listed by two routes fails it too.
10.lines  gate: the card lines every route's table builds for fixed probe plans, both
          languages, with their typed codes, equal the recorded golden
          (``tests/fixtures/kria_replay/golden_lines.json``): rewording a line, or turning it
          into a claim that the value is applied, changes the golden, which a reviewer reads.
10.       consumer gate (REPORT ONLY): fields the route ignores that the card does not disclose.

Replay contract (render evidence for checks 2, 3, 4, 5). A lane adds
``scripts/kria/route_replays/<route>.py`` exporting ``recipe(fx) -> {key: value}``: the render
recipe values the route's own code builds for the fixture's inputs, never a verdict. Keys
(``RECIPE_KEYS``): ``title_animation`` the entrance on the title rows (str, or one per row),
``pace`` the pace the clip holds use, ``length_s`` the rendered seconds, ``caption_mode``
none / sentence / word. The grader compares each value with the asked one (a field's strategy
value, an ask's typed fact) and runs its own controls on the module, each needed for a claim:

* perturbation: the asked value changed (pop to fade, relaxed to fast, 30 s to 45 s, or
  ``checks.perturb``) must change the value to match;
* drop: one asked key removed (field, provenance and asks) reverts only that key to the value
  with nothing asked; every other claimed key keeps its value;
* switch off: with the lane switch (``LANE_SWITCH``) off the ask changes nothing;
* provenance: every claimed value is one a function in the route's ``READERS`` returned during
  that run, and the run entered the route's code;
* switch probe: the lane switch is read only by the route's code, never by the module.

A module that fails one makes the scorecard INVALID (exit 2). The same controls run every time
on synthetic modules over a synthetic route: an honest one must pass; a constant, an ask-keyed,
a switch-keyed and two echo modules must fail.

Lane targets: ``tests/fixtures/kria_replay/lane_targets.json`` lists, per fixture, the checks
expected NOT VERIFIED today and the lane that turns each green. The report splits failures into
``expected_red`` (listed) and ``unexpected_red`` (not listed), and lists ``unexpected_green``
targets (a lane landed: remove the entry). An unexpected red exits 1.

Negative controls (mandatory, INVALID rule as in ``score_29_ekim.py``): each check's predicate
and the check functions themselves get a synthetic card that ignores the ask (must FAIL) and one
that honours it (must PASS); the gates get unread fields, claims with no reader, bad carriers,
a shared reader file and a reworded line (all must be flagged). A control that cannot run
because the registry fails to import is INVALID too; only code that predates the registry (no
package) shows them n/a. A failed control makes the scorecard INVALID (exit 2).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import functools
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
import types
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"
APP = API / "app"
FIXTURES = API / "tests" / "fixtures" / "kria_replay"
LANE_TARGETS = FIXTURES / "lane_targets.json"
GOLDEN_LINES = FIXTURES / "golden_lines.json"
REPLAYS = HERE / "route_replays"
REGISTRY_PACKAGE = APP / "kria" / "route_capabilities" / "__init__.py"
BUILDER = "tasks/generative_build.py"
CREATOR_ID = uuid.UUID("00000000-0000-4000-8000-000000001097")
LANGS = ("en", "tr")

# The typed codes of a table's card lines (`route_capabilities._types`): a line counts only
# with the code its table entry gives the field.
TABLE_CODES = frozenset({"not_applied_on_route", "recorded_only_on_route"})
# Each route's lane kill switch (`config.py`): off, the route is byte-identical to before the
# lane, so an ask must change nothing in its recipe.
LANE_SWITCH = {
    "voiceover_montage": "voiceover_route_follows_prompt_enabled",
    "talking_head": "talking_head_route_follows_prompt_enabled",
    "narrated": "narrated_route_follows_prompt_enabled",
    "lipsync_song": "lipsync_route_follows_prompt_enabled",
    "speech_excerpt": "speech_excerpt_route_follows_prompt_enabled",
    "slide_post": "slide_route_follows_prompt_enabled",
}
# Shared functions outside every route's own files that may carry an ``Applied`` field
# (`route_capabilities._lines`). Each must still read the field off a strategy object.
SHARED_CARRIERS = {
    "kria/runtime.py:_apply_strategy_approval_media": "approval writes the format onto the item",
    "tasks/generative_build.py:_run_generative_job_impl": "the dispatch",
    "services/creator_render_contract.py:build_render_contract": "the approved render contract",
    "tasks/content_plan_build.py:_creator_selected_clip_paths": "the creator's clip selection",
    "services/phone_text_styling.py:requested_style": "the shared phone text style pass",
    "services/generative_jobs.py:build_generative_job": "the job builder's flat keys",
    "agents/_schemas/creator_policy.py:effective_render_program": "the render-program policy",
    "pipeline/unified_montage.py:_title": "the title resolver both montage writers call",
}


def _load_29_ekim():  # noqa: ANN202
    """The 29-ekim scorecard's helpers (socket guard, ``_result``, ``_guarded``)."""
    spec = importlib.util.spec_from_file_location("score_29_ekim", HERE / "score_29_ekim.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BASE = _load_29_ekim()
_result = _BASE._result
_guarded = _BASE._guarded


def _prod_flags() -> contextlib.ExitStack:
    """Every boolean flag production has on, and the device features it has verified."""
    from app.config import settings

    if str(API) not in sys.path:
        sys.path.insert(0, str(API))
    from tests._prod_profile import PROD_TRUE_FLAGS, PROD_VERIFIED_FEATURES

    stack = contextlib.ExitStack()
    for name in PROD_TRUE_FLAGS:
        if hasattr(settings, name):
            stack.enter_context(patch.object(settings, name, True))
    stack.enter_context(
        patch.object(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    )
    stack.enter_context(patch.object(settings, "phone_render_user_ids", []))
    return stack


def _language(lang: str):  # noqa: ANN202
    from app.kria.reply_language import reply_language_for

    return reply_language_for(lang)


# --------------------------------------------------------------------------- fixture


def load_fixture(fixture_dir: Path) -> dict[str, Any]:
    return json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))


def media_rows(fx: dict) -> list[dict]:
    """The item's clip assignments, as phone analysis proxies (the phone dispatch gate)."""
    return [
        {
            "kind": clip.get("kind", "video"),
            "analysis": copy.deepcopy(clip.get("analysis") or {}),
            "gcs_path": f"users/synthetic/threads/synthetic/analysis-proxy-ios-{clip['id']}.mp4",
            "media_id": clip["id"],
            "has_audio": clip.get("has_audio", True),
            "duration_s": clip["duration_s"],
            "storage_generation": "1",
            "generation": "1",
            "has_analysis": bool(clip.get("analysis")),
        }
        for clip in fx["clips"]
    ]


def manifest(fx: dict):  # noqa: ANN201
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )

    fmt = str(fx["strategy"].get("edit_format") or "montage")
    on = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id=f"{fx['fixture_id']}-item",
        edit_format=fmt,
        render_program=str(fx["strategy"].get("render_program") or "guided"),
        media=[
            CreatorMediaRef(
                media_id=c["id"], kind=c.get("kind", "video"), duration_s=c["duration_s"]
            )
            for c in fx["clips"]
        ],
        catalog=[],
        capabilities={"dispatch_render": on, f"edit_format:{fmt}": on},
        context_hash="c" * 64,
        manifest_hash="d" * 64,
    )


# Fields a creator sees chosen on the card or in the plan summary.
CREATOR_VISIBLE = (
    "direction",
    "pacing",
    "intro_hook",
    "story_structure",
    "caption_style",
    "optional_treatments",
    "archetype",
    "hero_media_id",
    "overlay_display",
    "licensed_sfx",
    "title_animation",
    "label_position",
    "opening_title",
    "closing_title",
    "shot_labels",
    "target_duration_s",
)


def _plan_fields(strategy: dict) -> list[str]:
    """What the planner set away from the default (the pace always; a length only stated)."""
    from app.agents._schemas.creator_agent import CreativeStrategy

    defaults = CreativeStrategy()
    out = []
    for field in CREATOR_VISIBLE:
        if field == "pacing":
            out.append(field)  # the card always shows the plan's pace
            continue
        if field == "target_duration_s":
            stated = strategy.get("creator_duration_s") is not None or bool(
                strategy.get("target_duration_requested")
            )
            if stated:
                out.append(field)
            continue
        value = strategy.get(field)
        default = getattr(defaults, field, None)
        if value not in (None, "", [], default):
            out.append(field)
    return out


def planned_fields(fx: dict) -> list[str]:
    """The fields to grade: the fixture's list, else what the planner set away from the default."""
    listed = (fx.get("checks") or {}).get("fields")
    return list(listed) if listed is not None else _plan_fields(fx["strategy"])


def _live_asks(fx: dict) -> list[str]:
    return [r["id"] for r in fx["brief"]["requirements"] if r.get("status") != "superseded"]


def planned_asks(fx: dict) -> list[str]:
    listed = (fx.get("checks") or {}).get("asks")
    return list(listed) if listed is not None else _live_asks(fx)


def exclusions(fx: dict) -> dict[str, Any]:
    """Plan-set fields and live asks a fixture's own lists leave out of grading, with why."""
    reasons = (fx.get("checks") or {}).get("not_graded") or {}
    fields = [f for f in _plan_fields(fx["strategy"]) if f not in planned_fields(fx)]
    asks = [a for a in _live_asks(fx) if a not in planned_asks(fx)]
    left_out = [*fields, *asks]
    return {
        "count": len(left_out),
        "fields": fields,
        "asks": asks,
        "unexplained": [item for item in left_out if item not in reasons],
    }


class Target(NamedTuple):
    """One render target: check ``4.<field>`` or ``5.<req_id>`` and the recipe key it reads."""

    check_id: str
    kind: str  # "fields" | "asks"
    ident: str
    key: str


def honored_targets(fx: dict) -> list[Target]:
    """The fields and asks a lane will honor in the render (``checks.honored``)."""
    honored = (fx.get("checks") or {}).get("honored") or {}
    out = []
    for kind, prefix in (("fields", "4"), ("asks", "5")):
        for ident, key in dict(honored.get(kind) or {}).items():
            out.append(Target(f"{prefix}.{ident}", kind, ident, str(key)))
    return out


def lane_targets(fixture_id: str) -> dict[str, dict]:
    """Checks expected NOT VERIFIED on today's code for this fixture, with their lane."""
    if not LANE_TARGETS.is_file():
        return {}
    data = json.loads(LANE_TARGETS.read_text(encoding="utf-8"))
    return dict((data.get("targets") or {}).get(fixture_id) or {})


# --------------------------------------------------------------------- route and card


class RegistryBroken(RuntimeError):
    """The registry package exists but does not import: never graded as pre-registry code."""


def _registry():  # noqa: ANN202
    """The route registry, or None on code that predates it (no package on disk).

    A package that exists and fails to import raises `RegistryBroken`: a broken registry must
    never pass for code that has none (its controls would all read n/a)."""
    if not REGISTRY_PACKAGE.is_file():
        return None
    try:
        from app.kria import route_capabilities
        from app.kria.route_capabilities import _lines
        from app.kria.route_disclosure import (
            decoupled_from_readiness,
            draft_route,
            requirements_with_route_declines,
        )
    except Exception as exc:  # noqa: BLE001 - reported as INVALID
        raise RegistryBroken(f"{type(exc).__name__}: {exc}") from exc
    return SimpleNamespace(
        tables=route_capabilities,
        lines=_lines,
        draft_route=draft_route,
        decoupled=decoupled_from_readiness,
        answered=requirements_with_route_declines,
    )


def card_route(fx: dict) -> str | None:
    """The route whose card lines this draft gets, as the draft turn picks it."""
    from app.kria.brief_checks import defers_to_unified_montage

    strategy = fx["strategy"]
    paths = [r["gcs_path"] for r in media_rows(fx)]
    phone_montage = defers_to_unified_montage(
        creator_id=CREATOR_ID,
        edit_format=strategy.get("edit_format"),
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=paths,
    )
    registry = _registry()
    if registry is None:
        from app.config import settings

        legacy = settings.kria_route_disclosure_enabled and phone_montage
        return "unified_montage" if legacy else None
    return registry.draft_route(
        creator_id=CREATOR_ID,
        edit_format=strategy.get("edit_format"),
        strategy=strategy,
        clip_paths=paths,
        phone_montage=phone_montage,
    )


def table_codes(route: str | None) -> dict[str, str]:
    """``subject -> code`` for every line the route's table can build (the code its entry
    gives it). Empty on code that predates the registry."""
    registry = _registry()
    if registry is None or route is None:
        return {}
    from app.kria.route_capabilities import NotApplied, RecordedOnly
    from app.kria.route_disclosure import NOT_APPLIED, _table

    table = _table(route)
    if table is None:
        return {}
    codes = {
        field: handling.code
        for field, handling in table.FIELD_HANDLING.items()
        if isinstance(handling, NotApplied | RecordedOnly)
    }
    codes.update(dict.fromkeys(getattr(table, "NOTICES", {}), NOT_APPLIED))
    return codes


def _compact_labels(line: str) -> list[str] | None:
    """The subjects a compact card line names, or None for any other line."""
    registry = _registry()
    if registry is None:
        return None
    body = line[2:] if line.startswith("- ") else line
    for prefix in (registry.lines.COMPACT_PREFIX_EN, registry.lines.COMPACT_PREFIX_TR):
        if body.startswith(prefix):
            return [item.strip() for item in body[len(prefix) :].split(",")]
    return None


def on_card(record: dict, reply: str) -> bool:
    """The creator reads this disclosure: its whole sentence is a card line, or the card's
    compact line names its subject (`route_disclosure.card_text`)."""
    lines = reply.splitlines()
    text = str(record.get("text") or "")
    if text and f"- {text}" in lines:
        return True
    registry = _registry()
    label = registry.lines.subject_label(str(record.get("subject"))) if registry else None
    return bool(label) and any(label in (_compact_labels(line) or ()) for line in lines)


def card_records(disclosures: list, reply: str, codes: Mapping[str, str]) -> list[dict]:
    """The card's registry lines (`Disclosure` records): whether the creator reads each, and
    the code the route's table gives its subject (``codes``, `table_codes`)."""
    records = [d.as_record() for d in disclosures]
    return [
        {**r, "on_card": on_card(r, reply), "table_code": codes.get(str(r.get("subject")))}
        for r in records
    ]


def decline_field(req: Any, text: str) -> str | None:
    """The strategy field a route decline speaks for: decided by the declined ask's own typed
    facts and the registry's exact decline text in the card's language, never by the subject
    label the table picked. A title-position decline speaks for no field."""
    registry = _registry()
    if registry is None or req is None:
        return None
    lines = registry.lines
    for asks, decline, field in (
        (lines.asks_text_animation, lines.animation_decline, "title_animation"),
        (lines.asks_captions_on, lines.captions_decline, "caption_style"),
    ):
        if asks(req) and text == decline():
            return field
    return None


def decline_records(
    declines: list, receipts: list[dict], reply: str, requirements: list
) -> list[dict]:
    """The route's declines (`route_disclosure.ask_declines`), each with the field it speaks for
    (`decline_field`) and whether the card shows it: a checked ``not_possible`` receipt for that
    ask with that reason, which the reply carries."""
    shown = {
        (r.get("requirement_id"), r.get("reason"))
        for r in receipts
        if r.get("status") == "not_possible" and r.get("verification") == "checked"
    }
    by_id = {req.id: req for req in requirements}
    return [
        {
            "requirement_id": d.requirement_id,
            "subject": d.subject,
            "field": decline_field(by_id.get(d.requirement_id), d.text),
            "text": d.text,
            "on_card": (d.requirement_id, d.text) in shown and d.text in reply,
        }
        for d in declines
    ]


def approval_card(fx: dict) -> dict[str, Any]:
    """The creator-visible draft reply, from the pure functions the draft turn calls, in the
    language bound by the caller (`approval_cards`).

    Mirrors `tasks.kria_runtime` for an account outside approval readiness (the default): the
    asks checked at draft plus the asks the route declines (`requirements_with_route_declines`),
    receipts with route declines, then the route's card lines. Lines and declines show only
    where that code shows them (readiness, or the registry's decoupling)."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.config import settings
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import (
        build_receipts,
        defers_caption_words_to_phone_render,
        defers_text_to_phone,
        defers_to_narrated_render,
        defers_to_unified_montage,
        plan_facts_from_strategy,
        reply_from_receipts,
        requirements_to_check_at_draft,
    )
    from app.kria.route_disclosure import (
        ask_declines,
        card_disclosures,
        card_text,
        with_route_declines,
    )
    from app.tasks.kria_runtime import _strategy_changes

    strategy = fx["strategy"]
    rows = media_rows(fx)
    paths = [r["gcs_path"] for r in rows]
    media = {"clip_paths": paths, "clip_assignments": rows}
    fmt = strategy.get("edit_format")
    audio = strategy.get("audio_strategy")
    common = {"creator_id": CREATOR_ID, "edit_format": fmt, "audio_strategy": audio}
    talking_writer = defers_caption_words_to_phone_render(clip_paths=paths, **common)
    facts = plan_facts_from_strategy(
        strategy,
        clip_ids=[c["id"] for c in fx["clips"]],
        manifest=manifest(fx),
        speech_cleanup_enabled=False,
        speech_cleanup_offered=False,
    )
    facts = replace(
        facts,
        phone_talking=talking_writer and fmt in {"subtitled", "talking_head"},
        caption_words_at_render=talking_writer,
        phone_text=defers_text_to_phone(creator_id=CREATOR_ID, clip_paths=paths),
        phone_narrated=defers_to_narrated_render(clip_paths=paths, **common),
        phone_montage=defers_to_unified_montage(clip_paths=paths, **common),
        media_snapshot=media,
    )
    route = card_route(fx)
    registry = _registry()
    readiness = settings.approval_readiness_for(CREATOR_ID)
    shown = route is not None and (readiness or (registry is not None and registry.decoupled()))
    brief = CreativeBrief.model_validate(fx["brief"])
    checked = (
        brief.live()
        if readiness
        else requirements_to_check_at_draft(
            brief.live(),
            creator_id=CREATOR_ID,
            strategy=strategy,
            item_edit_format=fmt,
            clip_paths=paths,
            client_protocol=fx.get("client_protocol"),
        )
    )
    answered = (
        registry.answered(checked, brief.live(), strategy, route=route)
        if registry is not None and route is not None and not readiness
        else checked
    )
    receipts = (
        build_receipts(checked, facts, include_unchecked=True, strict_order=True) if checked else []
    )
    if shown:
        receipts = with_route_declines(receipts, answered, strategy, route=route)
    if not readiness and not facts.phone_talking:
        receipts = [r for r in receipts if r.verification != "unchecked"]
    # Outside readiness a route decline follows the planner summary (code that has the switch).
    after_summary = (
        {"declines_after_summary": shown and not readiness}
        if "declines_after_summary" in inspect.signature(reply_from_receipts).parameters
        else {}
    )
    reply = reply_from_receipts(
        CreativeBrief(version=brief.version, requirements=answered),
        receipts,
        summary=str(fx.get("planner_summary") or ""),
        notices=(),
        **after_summary,
    )
    disclosures = (
        card_disclosures(brief.live(), strategy, route=route, media=media) if shown else []
    )
    if disclosures:
        reply = f"{reply}\n{card_text(disclosures)}"
    arguments = SimpleNamespace(
        strategy=CreativeStrategy.model_validate(strategy),
        summary=str(fx.get("planner_summary") or ""),
    )
    changes = [c for c in _strategy_changes(arguments) if c != arguments.summary]
    receipt_records = [r.model_dump(mode="json") for r in receipts]
    return {
        "route": route,
        "lines_shown": shown,
        "reply": reply,
        "changes": changes,
        "receipts": receipt_records,
        "not_applied": card_records(disclosures, reply, table_codes(route)),
        "declines": decline_records(
            ask_declines(answered, strategy, route=route) if shown else [],
            receipt_records,
            reply,
            list(brief.live()),
        ),
    }


def approval_cards(fx: dict) -> dict[str, dict]:
    """The card in every reply language (``LANGS``)."""
    out = {}
    for lang in LANGS:
        with _language(lang):
            out[lang] = approval_card(fx)
    return out


def field_disclosed(card: dict, field: str) -> bool:
    """The card says ``field`` is not applied: that field's own registry line the creator reads,
    with the code the route's table gives the field, or a route decline about it
    (`decline_field`). Free text, another field's line, another code, or a decline about
    another ask never counts."""
    own_line = any(
        r.get("code") in TABLE_CODES
        and r.get("code") == r.get("table_code")
        and r.get("subject") == field
        and r.get("on_card") is True
        for r in card.get("not_applied") or []
    )
    decline = any(
        r.get("field") == field and r.get("on_card") is True for r in card.get("declines") or []
    )
    return own_line or decline


# ----------------------------------------------------------------- the replay contract


class RecipeKey(NamedTuple):
    """A render recipe value a replay module returns, and what it answers: a strategy field
    (with the server provenance that states it) and a typed ask fact."""

    field: str
    fact: str
    provenance: tuple[str, ...] = ()


RECIPE_KEYS: dict[str, RecipeKey] = {
    "title_animation": RecipeKey("title_animation", "animation"),
    "pace": RecipeKey("pacing", "pace", ("pacing_requested",)),
    "length_s": RecipeKey(
        "target_duration_s", "duration_s", ("creator_duration_s", "target_duration_requested")
    ),
    "caption_mode": RecipeKey("caption_style", "captions"),
}
# A caption style or caption fact, as the caption modes that meet it (`kria.runtime`
# approval: clean/editorial sentences, kinetic/karaoke word by word, none off).
_CAPTION_MODES = {
    "none": {"none"},
    "off": {"none"},
    "clean": {"sentence"},
    "editorial": {"sentence"},
    "kinetic": {"word"},
    "karaoke": {"word"},
    "on": {"sentence", "word"},
    "word_by_word": {"word"},
}
_ANIMATION_PERTURB = ("fade", "typewriter", "pop", "slide")
_PACE_PERTURB = ("fast", "relaxed", "balanced")


def _requirement(fx: dict, req_id: str) -> dict:
    return next((r for r in fx["brief"]["requirements"] if r["id"] == req_id), {})


def asked(fx: dict, target: Target) -> tuple[Any, str]:
    """``(value, bound)`` the creator asked for this target: the strategy field's value (a
    length: the stated seconds), or the ask's typed fact (a length: with its bound)."""
    from app.services.length_policy import duration_bound

    spec = RECIPE_KEYS[target.key]
    if target.kind == "fields":
        strategy = fx["strategy"]
        if target.key == "length_s":
            stated = strategy.get("creator_duration_s")
            return (stated if stated is not None else strategy.get(spec.field)), "exact"
        return strategy.get(spec.field), "exact"
    facts = _requirement(fx, target.ident).get("facts") or {}
    bound = duration_bound(facts) if target.key == "length_s" else "exact"
    return facts.get(spec.fact), bound


def _animation(value: Any) -> str | None:
    from app.schemas.text_style_intent import normalize_title_animation

    return normalize_title_animation(value)


def honors(key: str, value: Any, wanted: Any, bound: str = "exact") -> bool:
    """The recipe value meets the asked one (the grader's own comparison, per key)."""
    if wanted is None or value is None:
        return False
    if key == "title_animation":
        values = value if isinstance(value, list) else [value]
        if not values:
            return False
        if str(wanted).strip().casefold() == "any":
            return all(_animation(v) is not None for v in values)
        want = _animation(wanted)
        return want is not None and all(_animation(v) == want for v in values)
    if key == "pace":
        return isinstance(value, str) and value.casefold() == str(wanted).casefold()
    if key == "length_s":
        from app.services.length_policy import creator_duration_satisfied

        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        return creator_duration_satisfied(float(value), float(wanted), bound)  # type: ignore[arg-type]
    if key == "caption_mode":
        modes = _CAPTION_MODES.get(str(wanted).strip().casefold())
        return bool(modes) and value in modes
    return False


def _perturbed(key: str, wanted: Any, override: Any = None) -> tuple[Any, Any]:
    """``(field value, fact value)`` that asks for something else than ``wanted``."""
    if override is not None:
        return override, override
    if key == "title_animation":
        current = None if str(wanted).casefold() == "any" else _animation(wanted)
        pick = next(a for a in _ANIMATION_PERTURB if _animation(a) != current)
        return pick, pick
    if key == "pace":
        pick = next(p for p in _PACE_PERTURB if p != str(wanted).casefold())
        return pick, pick
    if key == "length_s":
        seconds = float(wanted) * 1.5
        pick = round(seconds if seconds <= 60 else float(wanted) * 0.5)
        return pick, pick
    if key == "caption_mode":
        off = _CAPTION_MODES.get(str(wanted).strip().casefold()) == {"none"}
        return ("kinetic", "word_by_word") if off else ("none", "off")
    raise KeyError(key)


def _set_key(fx: dict, key: str, field_value: Any, fact_value: Any) -> dict:
    """``fx`` asking for another value of ``key`` everywhere it is asked (field and asks)."""
    out = copy.deepcopy(fx)
    spec = RECIPE_KEYS[key]
    strategy = out["strategy"]
    if spec.field in strategy or key == "length_s":
        strategy[spec.field] = field_value
    if key == "length_s" and strategy.get("creator_duration_s") is not None:
        strategy["creator_duration_s"] = field_value
    for req in out["brief"]["requirements"]:
        facts = req.get("facts") or {}
        if spec.fact in facts:
            facts[spec.fact] = fact_value
    return out


def _drop_keys(fx: dict, keys: list[str]) -> dict:
    """``fx`` with nothing asked for ``keys``: the fields, their provenance and the asks."""
    out = copy.deepcopy(fx)
    for key in keys:
        spec = RECIPE_KEYS[key]
        for name in (spec.field, *spec.provenance):
            out["strategy"].pop(name, None)
        out["brief"]["requirements"] = [
            r for r in out["brief"]["requirements"] if spec.fact not in (r.get("facts") or {})
        ]
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    numeric = (int, float)
    if (
        isinstance(a, numeric)
        and isinstance(b, numeric)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        return abs(float(a) - float(b)) <= 1e-6
    return a == b


def _holds(obj: Any, value: Any, budget: list[int]) -> bool:
    """``obj`` (a returned value) contains ``value`` somewhere within a bounded walk."""
    stack = [(obj, 0)]
    seen: set[int] = set()
    while stack and budget[0] > 0:
        current, depth = stack.pop()
        budget[0] -= 1
        if type(current) is type(value) or (
            isinstance(current, int | float) and isinstance(value, int | float)
        ):
            if not isinstance(current, bool) and _same(current, value):
                return True
        if depth >= 8 or id(current) in seen or isinstance(current, str | bytes | int | float):
            continue
        seen.add(id(current))
        if isinstance(current, Mapping):
            children = list(current.values())
        elif isinstance(current, list | tuple | set | frozenset):
            children = list(current)
        elif hasattr(current, "__dict__"):
            children = list(vars(current).values())
        elif hasattr(current, "__slots__"):
            children = [getattr(current, s, None) for s in current.__slots__]
        else:
            continue
        stack.extend((child, depth + 1) for child in children)
    return False


def returned_by_route(value: Any, returns: list) -> bool:
    """Every part of ``value`` is in something the route's code returned during the run."""
    parts = value if isinstance(value, list) else [value]
    budget = [400_000]
    return bool(parts) and all(any(_holds(r, part, budget) for r in returns) for part in parts)


class SwitchProbe:
    """A lane switch's value that records the code reading it (``readers``)."""

    def __init__(self, value: bool, readers: list) -> None:
        self.value = value
        self.readers = readers

    def _read(self) -> None:
        self.readers.append(sys._getframe(2).f_code)

    def __bool__(self) -> bool:
        self._read()
        return self.value

    def __eq__(self, other: object) -> bool:
        self._read()
        return self.value == other

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        self._read()
        return repr(self.value)


class RouteCode(NamedTuple):
    """Where a route's code lives at run time: absolute file paths and its builder functions."""

    files: frozenset[str]
    functions: frozenset[str]

    def owns(self, code: Any) -> bool:
        name = _real(code.co_filename)
        return name in self.files or (
            name == _real(str(APP / BUILDER)) and code.co_name in self.functions
        )


@functools.cache
def _real(path: str) -> str:
    return os.path.realpath(path)


def route_code(route: str) -> RouteCode:
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None else None
    if table is None:
        return RouteCode(frozenset(), frozenset())
    return RouteCode(
        frozenset(_real(str(APP / rel)) for rel in table.READERS.files),
        frozenset(table.READERS.functions),
    )


def _run(module: Any, fx: dict, code: RouteCode, switch: str | None, on: bool) -> dict:
    """One ``module.recipe(fx)`` run with the lane switch ``on`` (a `SwitchProbe`): the recipe,
    calls into the route's code, what that code returned, and who read the switch."""
    from app.config import settings

    calls = [0]
    returns: list = []
    readers: list = []

    def profile(frame: Any, event: str, arg: Any) -> None:
        if event == "call" and code.owns(frame.f_code):
            calls[0] += 1
        elif event == "return" and arg is not None and code.owns(frame.f_code):
            returns.append(arg)

    probe = (
        patch.object(settings, switch, SwitchProbe(on, readers))
        if switch is not None and hasattr(settings, switch)
        else contextlib.nullcontext()
    )
    with probe:
        sys.setprofile(profile)
        try:
            recipe = module.recipe(copy.deepcopy(fx))
        except Exception as exc:  # noqa: BLE001 - recorded: a control that cannot run fails
            return {
                "recipe": {},
                "calls": calls[0],
                "returns": [],
                "readers": readers,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            sys.setprofile(None)
    return {
        "recipe": dict(recipe) if isinstance(recipe, Mapping) else {},
        "calls": calls[0],
        "returns": returns,
        "readers": readers,
        "error": None,
    }


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def grade_recipe(
    module: Any, fx: dict, code: RouteCode, switch: str | None, targets: list[Target]
) -> dict:
    """The module's recipe against what was asked, and the grader's controls on it.

    ``honored[check_id]`` is the grader's comparison; ``valid`` is False when a control fails
    for a key some target claims (the scorecard is then INVALID)."""
    real = _run(module, fx, code, switch, True)
    if real["error"] is not None:
        raise RuntimeError(f"the route replay raised: {real['error']}")
    recipe = real["recipe"]
    wanted = {t.check_id: asked(fx, t) for t in targets}
    honored = {t.check_id: honors(t.key, recipe.get(t.key), *wanted[t.check_id]) for t in targets}
    claimed = sorted({t.key for t in targets if honored[t.check_id]})
    rows: list[dict] = []
    runs = [real]
    if not claimed:
        rows.append(_row("replay: the recipe meets no target, nothing to control", True, True))
        return _replay_report(recipe, wanted, honored, rows, runs)
    keys = sorted({t.key for t in targets})
    perturb = dict((fx.get("checks") or {}).get("perturb") or {})
    stripped = _run(module, _drop_keys(fx, keys), code, switch, True)
    off = _run(module, fx, code, switch, False)
    off_stripped = _run(module, _drop_keys(fx, keys), code, switch, False)
    runs += [stripped, off, off_stripped]
    rows.append(
        _row(
            "replay: a claim from a run that never entered the route's code must FAIL",
            False,
            real["calls"] == 0,
            {"route_code_calls": real["calls"]},
        )
    )
    for key in claimed:
        mine = [t for t in targets if t.key == key and honored[t.check_id]]
        first = wanted[mine[0].check_id][0]
        field_value, fact_value = _perturbed(key, first, perturb.get(key))
        moved_fx = _set_key(fx, key, field_value, fact_value)
        moved = _run(module, moved_fx, code, switch, True)
        dropped = _run(module, _drop_keys(fx, [key]), code, switch, True)
        runs += [moved, dropped]
        follows = all(
            honors(key, moved["recipe"].get(key), *asked(moved_fx, t)) for t in mine
        ) and not _same(moved["recipe"].get(key), recipe.get(key))
        others = [k for k in claimed if k != key]
        reverts = _same(dropped["recipe"].get(key), stripped["recipe"].get(key)) and all(
            _same(dropped["recipe"].get(k), recipe.get(k)) for k in others
        )
        inert = _same(off["recipe"].get(key), off_stripped["recipe"].get(key))
        traced = returned_by_route(recipe.get(key), real["returns"]) and returned_by_route(
            moved["recipe"].get(key), moved["returns"]
        )
        rows += [
            _row(
                f"replay {key}: a value that does not follow the asked one ({first!r} -> "
                f"{fact_value!r}) must FAIL",
                False,
                not follows,
                _json({"real": recipe.get(key), "perturbed": moved["recipe"].get(key)}),
            ),
            _row(
                f"replay {key}: dropping its ask must revert only its value to the no-ask "
                "default, else FAIL",
                False,
                not reverts,
                _json(
                    {
                        "dropped": {k: dropped["recipe"].get(k) for k in claimed},
                        "default": stripped["recipe"].get(key),
                        "real": {k: recipe.get(k) for k in claimed},
                    }
                ),
            ),
            _row(
                f"replay {key}: an ask that still changes the recipe with {switch} off must FAIL",
                False,
                not inert,
                _json(
                    {"off": off["recipe"].get(key), "off_no_ask": off_stripped["recipe"].get(key)}
                ),
            ),
            _row(
                f"replay {key}: a value the route's code never returned must FAIL",
                False,
                not traced,
            ),
        ]
    errors = [r["error"] for r in runs if r["error"]]
    rows.append(
        _row("replay: a claim whose control reruns raised must FAIL", False, bool(errors), errors)
    )
    outside = sorted(
        {f"{c.co_filename}:{c.co_name}" for r in runs for c in r["readers"] if not code.owns(c)}
    )
    rows.append(
        _row(
            f"replay: {switch} read outside the route's code (by the replay module) must FAIL",
            False,
            bool(outside),
            outside,
        )
    )
    return _replay_report(recipe, wanted, honored, rows, runs)


def _replay_report(recipe, wanted, honored, rows, runs) -> dict:  # noqa: ANN001
    return {
        "recipe": _json(recipe),
        "asked": _json(wanted),
        "honored": honored,
        "controls": rows,
        "valid": all(r["ok"] is True for r in rows),
        "route_code_calls": runs[0]["calls"],
    }


def _shown_path(path: Path) -> str:
    return str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)


def _replay_module(route: str):  # noqa: ANN202
    script = REPLAYS / f"{route}.py"
    if not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"route_replay_{route}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route_replay(fx: dict, route: str) -> dict[str, Any]:
    """Render evidence from ``route_replays/<route>.py`` under the contract above (nothing
    without one)."""
    module = _replay_module(route)
    if module is None:
        return {"source": None, "honored": {}, "controls": [], "valid": True, "recipe": None}
    graded = grade_recipe(
        module, fx, route_code(route), LANE_SWITCH.get(route), honored_targets(fx)
    )
    return {**graded, "source": _shown_path(REPLAYS / f"{route}.py")}


def applied(replay: dict, target_id: str) -> bool:
    """A render target the replay's recipe meets, under valid controls."""
    return bool(replay.get("valid")) and replay.get("honored", {}).get(target_id) is True


# --------------------------------------------------------------------- verdict logic
#
# Pure predicates: the checks feed them the real outputs, the controls synthetic ones.


def field_verdict(field: str, cards: Mapping[str, dict], render: bool) -> dict[str, Any]:
    by_lang = {lang: field_disclosed(card, field) for lang, card in cards.items()}
    disclosed = bool(by_lang) and all(by_lang.values())
    return {
        "pass": render is True or disclosed,
        "applied": render is True,
        "disclosed_on_card": disclosed,
        "disclosed_by_language": by_lang,
    }


def ask_verdict(
    receipt: dict | None, honored: bool = False, card: dict | None = None, field: str | None = None
) -> dict[str, Any]:
    """One language's card. ``field``: the strategy field whose own card line answers this ask
    (a length ask and the route's ``target_duration_s`` line)."""
    receipt = receipt or {}
    checked = receipt.get("verification") == "checked"
    met = checked and receipt.get("status") == "met"
    declined = (
        checked
        and receipt.get("status") == "not_possible"
        and bool(str(receipt.get("reason") or "").strip())
    )
    line = next(
        (
            r.get("text")
            for r in (card or {}).get("not_applied") or []
            if field
            and r.get("code") in TABLE_CODES
            and r.get("code") == r.get("table_code")
            and r.get("subject") == field
            and r.get("on_card") is True
        ),
        None,
    )
    on_card = line is not None
    return {
        "pass": bool(honored is True or met or declined or on_card),
        "met": met,
        "declined_before_approval": declined,
        "disclosed_on_card": on_card,
        "card_line": line,
        "honored_at_render": honored is True,
        "receipt": {k: receipt.get(k) for k in ("status", "verification", "reason")},
    }


def ask_field(req: dict) -> str | None:
    """The strategy field whose card line answers this ask (typed facts only, KRI-667)."""
    facts = req.get("facts") or {}
    if req.get("kind") == "timing" and facts.get("duration_s") is not None:
        return "target_duration_s"
    return None


# ------------------------------------------------------------------- consumer gate

EXEMPT_FIELDS = {"rationale": "explanatory prose shown to the creator, nothing to apply"}
_NOT_A_READ = re.compile(r"^\s*(#|\"\"\"|snapshot_kwargs\[|style\[)")
# What holds a strategy: a name with "strategy" in it, `(strategy or {})`, or the job's
# `.get("creator_strategy") or {})`. `step.<field>`, `view.<field>` or `{"<field>": ...}` are
# other objects (an assembly step, the brief view, a dict being written).
_RECEIVER = (
    r"(?:\b\w*strategy\w*"
    r"|\(\s*\w*strategy\w*\s+or\s+\{\}\s*\)"
    r"|creator_strategy[\"']\s*\)\s*or\s*\{\}\s*\))"
)


def strategy_read(field: str) -> re.Pattern:
    """A read of ``field`` off the strategy: ``strategy.get("f")``, ``strategy["f"]``,
    ``strategy.f``, ``getattr(strategy, "f")`` or a helper ``_value(strategy, "f")``."""
    name = re.escape(field)
    return re.compile(
        rf"""{_RECEIVER}\s*(?:\.get\(\s*["']{name}["']|\[\s*["']{name}["']\s*\]|\.{name}\b)"""
        rf"""|\b\w+\(\s*{_RECEIVER}\s*,\s*["']{name}["']"""
    )


def reads_in(lines: list[str], field: str) -> list[int]:
    """Line numbers (1-based) that read ``field`` off a strategy."""
    pattern = strategy_read(field)
    return [i + 1 for i, t in enumerate(lines) if pattern.search(t) and not _NOT_A_READ.match(t)]


def strategy_fields() -> tuple[list[str], set[str]]:
    tree = ast.parse((APP / "agents" / "_schemas" / "creator_agent.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CreativeStrategy":
            anns = [
                n
                for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            ]
            names = [a.target.id for a in anns]
            hidden = {a.target.id for a in anns if "SkipJsonSchema" in ast.unparse(a.annotation)}
            return names, hidden
    raise RuntimeError("CreativeStrategy not found")


def route_reads(
    fields: list[str], readers: Any, sources: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    """Strategy reads of each field inside the route's files and generative_build functions.

    ``sources`` (controls only) replaces the route's code with synthetic files."""
    if sources is None:
        sources = {rel: (APP / rel).read_text().splitlines() for rel in readers.files}
        gb_text = (APP / BUILDER).read_text()
        gb_lines = gb_text.splitlines()
        for node in ast.walk(ast.parse(gb_text)):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in readers.functions
            ):
                body = gb_lines[node.lineno - 1 : node.end_lineno]
                sources[f"{BUILDER}:{node.name}"] = [""] * (node.lineno - 1) + body
    reads: dict[str, list[str]] = {}
    for field in fields:
        reads[field] = [
            f"{rel.split(':')[0]}:{n}"
            for rel, lines in sources.items()
            for n in reads_in(lines, field)
        ]
    return reads


# --- carriers (AST): a function whose body reads the field off a strategy object

_STRATEGY_NAME = re.compile(r"\w*strategy\w*")
_STRATEGY_CALL = re.compile(r"strategy", re.I)


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_strategy(node: ast.AST, aliases: set[str]) -> bool:
    """``node`` evaluates to a strategy: a name with "strategy" in it or an alias of one,
    ``x.strategy``, ``(strategy or {})``, ``x.get("creator_strategy")``,
    ``x["creator_strategy"]``."""
    if isinstance(node, ast.Name):
        return bool(_STRATEGY_NAME.fullmatch(node.id)) or node.id in aliases
    if isinstance(node, ast.Attribute):
        return bool(_STRATEGY_NAME.fullmatch(node.attr))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _is_strategy(node.values[0], aliases)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        key = _const_str(node.args[0]) if node.func.attr == "get" and node.args else None
        return bool(key and _STRATEGY_NAME.fullmatch(key))
    if isinstance(node, ast.Subscript):
        key = _const_str(node.slice)
        return bool(key and _STRATEGY_NAME.fullmatch(key))
    return False


def _aliases(fn: ast.AST) -> set[str]:
    """Names bound to a strategy in ``fn``: ``s = strategy or {}``, ``typed = _strategy(x)``
    over a strategy, ``raw = dict(strategy)``."""
    found: set[str] = set()
    for _ in range(3):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
                continue
            value = node.value
            converted = (
                isinstance(value, ast.Call)
                and (
                    _STRATEGY_CALL.search(ast.unparse(value.func))
                    or (isinstance(value.func, ast.Name) and value.func.id == "dict")
                )
                and any(_is_strategy(arg, found) for arg in value.args)
            )
            if _is_strategy(value, found) or converted:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                found |= {t.id for t in targets if isinstance(t, ast.Name)}
    return found


def _loop_keys(fn: ast.AST) -> dict[str, set[str]]:
    """Loop names that range over a literal of field names (``for source, dest in (...)``)."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.For | ast.comprehension) and isinstance(
            node.iter, ast.Tuple | ast.List | ast.Set
        ):
            strings = {s for s in map(_const_str, ast.walk(node.iter)) if s}
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    out.setdefault(name.id, set()).update(strings)
    return out


def function_reads(fn: ast.AST, field: str) -> bool:
    """``fn``'s body reads ``field`` off a strategy object."""
    aliases = _aliases(fn)
    loops = _loop_keys(fn)

    def key(node: ast.AST) -> bool:
        if _const_str(node) == field:
            return True
        return isinstance(node, ast.Name) and field in loops.get(node.id, ())

    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr == field:
            if _is_strategy(node.value, aliases):
                return True
        elif isinstance(node, ast.Subscript) and _is_strategy(node.value, aliases):
            if key(node.slice):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                if _is_strategy(func.value, aliases) and node.args and key(node.args[0]):
                    return True
            if any(_is_strategy(a, aliases) for a in node.args) and any(key(a) for a in node.args):
                return True  # getattr(strategy, "f"), _value(strategy, "f")
    return False


def _definition(tree: ast.Module, symbol: str) -> ast.AST | None:
    """``Name`` (a top-level def or class) or ``Class.method`` in ``tree``."""
    head, _, tail = symbol.partition(".")
    for node in tree.body:
        if getattr(node, "name", None) != head:
            continue
        if not tail:
            return node
        if isinstance(node, ast.ClassDef):
            return next((m for m in node.body if getattr(m, "name", None) == tail), None)
    return None


def _owner_of(path: str, symbol: str, route: str | None) -> str | None:
    """Another route whose ``READERS`` holds ``path`` (or the builder function ``symbol``)."""
    registry = _registry()
    if registry is None:
        return None
    for rid, table in registry.tables.ROUTE_TABLES.items():
        if str(rid) == route:
            continue
        readers = table.READERS
        if path in readers.files or (path == BUILDER and symbol in readers.functions):
            return str(rid)
    return None


def via_problem(
    via: str,
    field: str,
    route: str | None = None,
    *,
    readers: Any = None,
    texts: Mapping[str, str] | None = None,
) -> str | None:
    """Why ``Applied(via=...)`` is no carrier for ``field`` on ``route``, or None when it is:
    a function (``path:name`` or ``path:Class.method``) in the route's own ``READERS`` or one of
    ``SHARED_CARRIERS``, whose body reads the field off a strategy object. ``readers`` and
    ``texts`` (controls only) stand in for the route's readers and the files' source."""
    path, _, symbol = via.partition(":")
    if not path.endswith(".py") or not all(p.isidentifier() for p in symbol.split(".")):
        return f"{via!r} is not <path under app/>:<function>"
    text = (texts or {}).get(path)
    if text is None:
        if not (APP / path).is_file():
            return f"{path} does not exist"
        text = (APP / path).read_text()
    if readers is None and route is not None and _registry() is not None:
        table = _registry().tables.table_for(route)
        readers = table.READERS if table is not None else None
    own = readers is not None and (
        path in readers.files or (path == BUILDER and symbol in readers.functions)
    )
    if not own and via not in SHARED_CARRIERS:
        other = _owner_of(path, symbol, route)
        if other is not None:
            return f"{via} is in {other}'s code, not this route's"
        return f"{via} is neither in this route's READERS nor a shared carrier"
    node = _definition(ast.parse(text), symbol)
    if node is None:
        return f"{symbol} is not defined in {path}"
    if isinstance(node, ast.ClassDef):
        return f"{via} is a class: a schema names the field, it reads nothing"
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return f"{via} is not a function"
    if not function_reads(node, field):
        return f"{via} never reads {field} off a strategy"
    return None


def ownership_problems(route: str, extra_files: tuple[str, ...] = ()) -> list[str]:
    """Reader files or builder functions the route lists that another route lists too: a
    reader belongs to one route, so a table can't claim fields another route's code reads.
    ``extra_files`` (controls only) adds files to the route's ``READERS``."""
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None else None
    if table is None:
        return []
    files = (*table.READERS.files, *extra_files)
    out = []
    for rid, other in registry.tables.ROUTE_TABLES.items():
        if str(rid) == route:
            continue
        out += [f"{f} is also {rid}'s reader file" for f in files if f in other.READERS.files]
        out += [
            f"{BUILDER}:{f} is also {rid}'s reader function"
            for f in table.READERS.functions
            if f in other.READERS.functions
        ]
    return out


def consumer_gate(
    route: str,
    card: dict,
    *,
    extra_fields: tuple[str, ...] = (),
    extra_handling: dict[str, Any] | None = None,
    sources: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The route's registry table against its code and the card.

    ``claimed_applied_unread``: marked ``Applied`` but no strategy read in ``READERS`` and no
    valid carrier (``bad_carriers`` says why a ``via`` is none). ``unconsumed``: no reader and
    no carrier; ``undisclosed``: of those, what the card does not disclose
    (`field_disclosed`). ``shared_readers``: reader files another route lists too.
    ``registry_drift`` is report-only detail."""
    from app.kria.route_capabilities import Applied, NotApplied, RecordedOnly, table_for

    table = table_for(route)
    if table is None:
        raise RuntimeError(f"no registry table for route {route!r}")
    handling = {**table.FIELD_HANDLING, **(extra_handling or {})}
    names, hidden = strategy_fields()
    settable = [n for n in names if n not in hidden] + list(extra_fields)
    reads = route_reads(list(dict.fromkeys([*settable, *handling])), table.READERS, sources)
    bad = {
        f: problem
        for f, h in handling.items()
        if isinstance(h, Applied) and h.via and (problem := via_problem(h.via, f, route))
    }
    carried = {
        f: h.via for f, h in handling.items() if isinstance(h, Applied) and h.via and f not in bad
    }
    claimed = sorted(
        f
        for f, h in handling.items()
        if isinstance(h, Applied) and not reads[f] and f not in carried
    )
    unconsumed = [
        f for f in settable if not reads[f] and f not in carried and f not in EXEMPT_FIELDS
    ]
    undisclosed = [f for f in unconsumed if not field_disclosed(card, f)]
    not_applied = {f for f, h in handling.items() if isinstance(h, NotApplied | RecordedOnly)}
    drift = {
        "listed_but_read": sorted(f for f in not_applied if f in settable and reads[f]),
        "unread_but_unlisted": sorted(f for f in unconsumed if f not in not_applied),
    }
    shared = ownership_problems(route)
    return {
        "registry_drift": drift,
        "pass": not undisclosed and not claimed and not shared,
        "settable_fields": len(settable),
        "claimed_applied_unread": claimed,
        "bad_carriers": bad,
        "shared_readers": shared,
        "unconsumed": unconsumed,
        "undisclosed": undisclosed,
        "disclosed": [f for f in unconsumed if f not in undisclosed],
        "exempt": EXEMPT_FIELDS,
        "carriers": carried,
    }


def _no_registry(check_id: str | int, name: str) -> dict:
    return {
        "id": check_id,
        "name": name,
        "pass": False,
        "verdict": "INCONCLUSIVE",
        "why_it_fails": ["no route registry on this code: the gate reads the route's table"],
        "evidence": {},
    }


def gate_result(check_id: int | str, route: str, card: dict) -> dict:
    name = "consumer gate (REPORT ONLY): fields the route ignores and the card does not disclose"
    if _registry() is None:
        return {**_no_registry(check_id, name), "report_only": True}
    gate = consumer_gate(route, card)
    why = []
    if gate["undisclosed"]:
        why.append(
            f"the {route} route reads none of {gate['undisclosed']} and the approval card does "
            "not say they are not applied"
        )
    result = _result(check_id, name, not gate["undisclosed"], why, gate)
    result["report_only"] = True
    result["verdict"] = "REPORT (clean)" if not gate["undisclosed"] else "REPORT (gaps)"
    return result


def claims_result(route: str, card: dict) -> dict:
    """Scored: no field the route's table marks applied lacks a reader or a real carrier, and
    no reader file is another route's."""
    name = "registry claims: every field the table marks applied is read by the route's code"
    if _registry() is None:
        return _kind(_no_registry("10.claims", name), "gate")
    gate = consumer_gate(route, card)
    claimed = gate["claimed_applied_unread"]
    why = []
    if claimed:
        why.append(
            f"the {route} table marks {claimed} applied, but nothing in the route's code reads "
            f"them off the strategy and no carrier reads them ({gate['bad_carriers']})"
        )
    if gate["shared_readers"]:
        why.append(f"the {route} READERS list another route's code: {gate['shared_readers']}")
    evidence = {
        k: gate[k] for k in ("claimed_applied_unread", "bad_carriers", "carriers", "shared_readers")
    }
    return _kind(_result("10.claims", name, not why, why, evidence), "gate")


# ------------------------------------------------------------------- golden lines

# Fixed plans every route's table builds its card lines for (`golden_lines`).
GOLDEN_PROBES: dict[str, dict[str, Any]] = {
    "every_field": {
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "intro_hook": "Nobody tells you this about the old town",
        "story_structure": ["Arrive at the harbour", "Climb to the castle", "Sunset view"],
        "pacing": "relaxed",
        "direction": "guided_story",
        "caption_style": "kinetic",
        "optional_treatments": ["zoom", "shake"],
        "archetype": "day_in_life",
        "hero_media_id": "clip-01",
        "overlay_display": "fullscreen",
        "licensed_sfx": [{"id": "whoosh"}],
        "title_animation": "pop",
        "label_position": "top",
        "opening_title": "Kas in three days",
        "closing_title": "See you next summer",
        "shot_labels": ["day one", "day two"],
        "font_family": "Inter",
        "text_color": "#ffffff",
        "mixed_media_timing": {"boundary_style": "crossfade"},
        "creator_duration_s": 25,
        "target_duration_s": 25,
        "target_duration_requested": True,
    },
    "voice_no_captions": {
        "edit_format": "montage",
        "audio_strategy": "voiceover",
        "pacing": "balanced",
        "direction": "native",
        "caption_style": "none",
        "title_animation": "fade",
    },
}
GOLDEN_ASKS: list[dict[str, Any]] = [
    {
        "id": "g1",
        "kind": "text",
        "scope": "title",
        "description": "animate the title",
        "facts": {"animation": "pop"},
    },
    {
        "id": "g2",
        "kind": "style",
        "scope": "global",
        "description": "add captions",
        "facts": {"captions": "on"},
    },
    {
        "id": "g3",
        "kind": "text",
        "scope": "title",
        "description": "title in the middle",
        "facts": {"position": "center"},
    },
    {
        "id": "g4",
        "kind": "timing",
        "scope": "global",
        "description": "25 seconds",
        "facts": {"duration_s": 25},
    },
]


def golden_lines() -> dict[str, Any]:
    """Every route's card lines for ``GOLDEN_PROBES`` and ``GOLDEN_ASKS``, per language: each
    line's typed code, subject and text, the route's declines, and the card text."""
    from app.config import settings
    from app.kria.brief import CreativeBrief
    from app.kria.route_capabilities import ROUTE_TABLES
    from app.kria.route_disclosure import (
        ask_declines,
        card_disclosures,
        card_text,
        strategy_notices,
    )

    asks = CreativeBrief.model_validate({"version": 1, "requirements": GOLDEN_ASKS}).live()
    out: dict[str, Any] = {}
    with (
        _prod_flags(),
        patch.object(settings, "route_registry_enabled", True),
        patch.object(settings, "kria_route_disclosure_enabled", True),
    ):
        for rid in ROUTE_TABLES:
            route = str(rid)
            out[route] = {}
            for lang in LANGS:
                with _language(lang):
                    out[route][lang] = {
                        name: {
                            "lines": [
                                d.as_record()
                                for d in strategy_notices(probe, route=route, requirements=asks)
                            ],
                            "declines": [
                                d.as_record() for d in ask_declines(asks, probe, route=route)
                            ],
                            "card": card_text(card_disclosures(asks, probe, route=route)),
                        }
                        for name, probe in GOLDEN_PROBES.items()
                    }
    return out


def write_golden_lines() -> Path:
    data = {
        "about": (
            "KRI-1097 wave 2: the card lines every route's table builds for the probe plans "
            "in scripts/kria/score_route.py (GOLDEN_PROBES, GOLDEN_ASKS), in English and "
            "Turkish, with each line's typed code. Recorded, never hand-edited: a line whose "
            "wording or code changes shows up here as a diff for review. Re-record with "
            "score_route.py --write-golden-lines."
        ),
        "routes": golden_lines(),
    }
    GOLDEN_LINES.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return GOLDEN_LINES


def _diff_paths(a: Any, b: Any, where: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += _diff_paths(a.get(key), b.get(key), f"{where}/{key}")
        return out
    return [] if a == b else [where or "/"]


def lines_result(route: str, current: dict[str, Any] | None = None) -> dict:
    """Scored: the route's card lines equal the recorded golden (both languages, codes)."""
    name = "card lines: the route's lines and their typed codes equal the recorded golden"
    if _registry() is None:
        return _kind(_no_registry("10.lines", name), "gate")
    if not GOLDEN_LINES.is_file():
        why = [f"no golden at {_shown_path(GOLDEN_LINES)}: record it with --write-golden-lines"]
        return _kind(_result("10.lines", name, False, why, {}), "gate")
    recorded = json.loads(GOLDEN_LINES.read_text(encoding="utf-8"))["routes"].get(route)
    now = (current if current is not None else golden_lines()).get(route)
    changed = _diff_paths(recorded, now)
    why = (
        [
            f"the {route} lines differ from the golden at {changed[:6]}: if the change is "
            "meant, re-record with --write-golden-lines and review the diff"
        ]
        if changed
        else []
    )
    return _kind(_result("10.lines", name, not changed, why, {"changed": changed}), "gate")


# ------------------------------------------------------------------------ the checks


def _kind(result: dict, kind: str) -> dict:
    return {**result, "kind": kind}


def check_route(fx: dict, cards: Mapping[str, dict]) -> dict:
    wanted = fx["route"]
    card = cards[LANGS[0]]
    ok = card["route"] == wanted
    return _kind(
        _result(
            1,
            "route: the draft resolves to the route that renders it",
            ok,
            []
            if ok
            else [f"the draft card reads the {card['route']!r} table, but {wanted!r} renders it"],
            {
                "fixture_route": wanted,
                "card_route": card["route"],
                "lines_shown": card["lines_shown"],
            },
        ),
        "route",
    )


def _target_render(replay: dict, kind: str, ident: str, fx: dict) -> bool:
    prefix = "4" if kind == "fields" else "5"
    return any(
        t.check_id == f"{prefix}.{ident}" and applied(replay, t.check_id)
        for t in honored_targets(fx)
    )


def check_field(fx: dict, cards: Mapping[str, dict], replay: dict, field: str) -> dict:
    verdict = field_verdict(field, cards, _target_render(replay, "fields", field, fx))
    value = fx["strategy"].get(field)
    return _kind(
        _result(
            f"2.{field}",
            f"{field}: honored in the render or disclosed before approval",
            verdict["pass"],
            []
            if verdict["pass"]
            else [
                f"the plan chose {field}={value!r}; no render recipe applies it and the card "
                "carries no registry line or route decline for it in "
                f"{[k for k, v in verdict['disclosed_by_language'].items() if not v]}"
            ],
            {**verdict, "value": value, "render_evidence": replay.get("source")},
        ),
        "disclosed_ok",
    )


def check_ask(fx: dict, cards: Mapping[str, dict], replay: dict, req_id: str) -> dict:
    req = _requirement(fx, req_id)
    field = ask_field(req)
    render = _target_render(replay, "asks", req_id, fx)
    by_lang = {
        lang: ask_verdict(
            next((r for r in card["receipts"] if r.get("requirement_id") == req_id), None),
            render,
            card,
            field,
        )
        for lang, card in cards.items()
    }
    ok = bool(by_lang) and all(v["pass"] for v in by_lang.values())
    first = by_lang.get(LANGS[0]) or next(iter(by_lang.values()), {})
    return _kind(
        _result(
            f"3.{req_id}",
            f"{req_id}: the ask is honored or declined before approval",
            ok,
            []
            if ok
            else [
                f"{req.get('description') or req.get('literal')!r} has receipt "
                f"{first.get('receipt')}: neither met nor a typed decline in "
                f"{[k for k, v in by_lang.items() if not v['pass']]}"
                + (f", and the card has no {field} registry line" if field else "")
            ],
            {
                **first,
                "by_language": {k: v["pass"] for k, v in by_lang.items()},
                "requirement": req,
            },
        ),
        "disclosed_ok",
    )


def _no_render_evidence(fx: dict, replay: dict, target: Target) -> str:
    if replay.get("error"):
        return f"the route replay raised: {replay['error'].get('why_it_fails')}"
    if replay.get("source") is None:
        return (
            f"no render replay for this route (scripts/kria/route_replays/{fx['route']}.py): "
            "only the render's recipe can show it applied"
        )
    if not replay.get("valid"):
        return f"{replay['source']} failed the grader's replay controls, so no claim counts"
    value = (replay.get("recipe") or {}).get(target.key)
    return (
        f"{replay['source']} returns {target.key}={value!r}, which does not meet the asked "
        f"{(replay.get('asked') or {}).get(target.check_id)!r}"
    )


def check_honored(fx: dict, replay: dict, target: Target) -> dict:
    ok = applied(replay, target.check_id)
    if target.kind == "fields":
        name = f"{target.ident}: applied in the render (lane target)"
        about = f"the plan chose {target.ident}={fx['strategy'].get(target.ident)!r}"
    else:
        req = _requirement(fx, target.ident)
        name = f"{target.ident}: honored in the render (lane target)"
        about = repr(req.get("description") or req.get("literal"))
    problem = None if target.key in RECIPE_KEYS else f"unknown recipe key {target.key!r}"
    return _kind(
        _result(
            target.check_id,
            name,
            ok and problem is None,
            [f"{about}; {problem or _no_render_evidence(fx, replay, target)}"],
            {
                "recipe_key": target.key,
                "applied_at_render": ok,
                "render_evidence": replay.get("source"),
            },
        ),
        "honored",
    )


# ------------------------------------------------------------------ negative controls


def _row(name: str, expect_pass: bool, got: bool | None, detail: Any = "") -> dict:
    return {
        "name": name,
        "expected_pass": expect_pass,
        "got_pass": got,
        "ok": None if got is None else expect_pass == got,
        "detail": detail,
    }


_SYNTHETIC_FX = {
    "fixture_id": "control",
    "route": "control",
    "strategy": {
        "pacing": "balanced",
        "target_duration_s": 30,
        "title_animation": "pop",
        "opening_title": "Three days in Kas",
    },
    "brief": {
        "requirements": [
            {"id": "r1", "kind": "timing", "facts": {"duration_s": 30}, "description": "30 s"}
        ]
    },
}
_NO_REPLAY = {"source": None, "honored": {}, "valid": True}
_CODES = {
    "pacing": "not_applied_on_route",
    "target_duration_s": "not_applied_on_route",
    "opening_title": "not_applied_on_route",
    "title_animation": "not_applied_on_route",
}


def _line(subject: str, text: str, code: str = "not_applied_on_route") -> SimpleNamespace:
    """A synthetic `route_disclosure.Disclosure`."""
    record = {"code": code, "subject": subject, "text": text}
    return SimpleNamespace(as_record=lambda record=record: dict(record))


def _req(req_id: str, kind: str, scope: str, facts: dict) -> Any:
    from app.kria.brief import BriefRequirement

    return BriefRequirement(id=req_id, kind=kind, scope=scope, description="ask", facts=facts)


def _decline(req: Any, subject: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(requirement_id=req.id, subject=subject, text=text, req=req)


def _synthetic_card(
    lines: list[SimpleNamespace],
    declines=(),
    reply: str | None = None,
    *,
    receipted: bool = True,
    codes: Mapping[str, str] = _CODES,
) -> dict:
    """A card built through the real record functions (`card_records`, `decline_records`).
    ``receipted`` False: the declines are computed but no receipt carries them."""
    if reply is None:
        reply = "\n".join(
            ["Heads-up for this video:", *[f"- {d.as_record()['text']}" for d in lines]]
        )
    receipts = [
        {
            "requirement_id": d.requirement_id,
            "status": "not_possible",
            "verification": "checked",
            "reason": d.text,
        }
        for d in declines
        if receipted
    ]
    reply = "\n".join([reply, *[f"Couldn't: ({r['reason']})" for r in receipts]])
    return {
        "route": "control",
        "reply": reply,
        "receipts": receipts,
        "not_applied": card_records(list(lines), reply, codes),
        "declines": decline_records(list(declines), receipts, reply, [d.req for d in declines]),
    }


def _card_check(field: str, card: dict, tr: dict | None = None) -> bool:
    cards = {"en": card, "tr": card if tr is None else tr}
    return check_field(_SYNTHETIC_FX, cards, _NO_REPLAY, field)["pass"]


def _ask_check(card: dict) -> bool:
    return check_ask(_SYNTHETIC_FX, {"en": card, "tr": card}, _NO_REPLAY, "r1")["pass"]


def check_function_controls() -> list[dict]:
    """The checks themselves (`check_field`, `check_ask`, ``on_card``) on synthetic cards."""
    pace = _line("pacing", "The balanced pacing isn't applied on this video yet: the cuts follow")
    length = _line("target_duration_s", "I can't make this video the length you asked for yet")
    title = _line("opening_title", "I can't add the opening title ('Three days in Kas') yet")
    opposite = "The balanced pacing is applied exactly as planned, nothing is skipped"
    length_opposite = "It will be exactly 30 seconds, nothing left out"
    rows = [
        _row(
            "2 check_field: another field's card line does not disclose this one must FAIL",
            False,
            _card_check("target_duration_s", _synthetic_card([pace])),
        ),
        _row(
            "2 check_field: the field's own card line must PASS",
            True,
            _card_check("target_duration_s", _synthetic_card([pace, length])),
        ),
        _row(
            "2 check_field: a line on the English card the Turkish card lacks must FAIL",
            False,
            _card_check("pacing", _synthetic_card([pace]), tr=_synthetic_card([])),
        ),
        _row(
            "2 check_field: an opposite-meaning free-text line naming the field must FAIL",
            False,
            _card_check("pacing", _synthetic_card([], reply=f"Heads-up:\n- {opposite}")),
        ),
        _row(
            "2 check_field: a line with another code (not the route's table) must FAIL",
            False,
            _card_check("pacing", _synthetic_card([_line("pacing", opposite, code="note")])),
        ),
        _row(
            "2 check_field: a line whose code is not the one the table gives the field must FAIL",
            False,
            _card_check(
                "pacing",
                _synthetic_card([_line("pacing", opposite, code="recorded_only_on_route")]),
            ),
        ),
        _row(
            "2 on_card: a recorded line the reply does not carry is not on the card must FAIL",
            False,
            _card_check("opening_title", _synthetic_card([title], reply="Heads-up:\n- other")),
        ),
        _row(
            "3 check_ask: a length ask with only another field's line must FAIL",
            False,
            _ask_check(_synthetic_card([pace])),
        ),
        _row(
            "3 check_ask: a length ask with the length registry line must PASS",
            True,
            _ask_check(_synthetic_card([length])),
        ),
        _row(
            "3 check_ask: a length ask with an opposite-meaning free-text line must FAIL",
            False,
            _ask_check(_synthetic_card([], reply=f"Heads-up:\n- {length_opposite}")),
        ),
    ]
    registry = _registry()
    if registry is None:
        detail = "pre-registry code: no compact line or registry declines"
        return rows + [
            _row(
                "2 on_card: a line the compact card line names is on the card must PASS",
                True,
                None,
                detail,
            ),
            _row("2 decline controls", True, None, detail),
        ]
    lines = registry.lines
    compact = lines.compact_line(["opening_title", "target_duration_s"])
    animate = _req("r8", "style", "global", {"animation": "pop"})
    framing = _req("r7", "style", "global", {"framing": "vertical", "keep_face_in_frame": True})
    title_spot = _req("r9", "text", "title", {"position": "center"})
    rows += [
        _row(
            "2 on_card: a line the compact card line names is on the card must PASS",
            True,
            _card_check("opening_title", _synthetic_card([title], reply=f"Heads-up:\n- {compact}")),
        ),
        _row(
            "2 check_field: a text-animation decline on an animation ask discloses the title "
            "animation must PASS",
            True,
            _card_check(
                "title_animation",
                _synthetic_card(
                    [], [_decline(animate, "text_animation", lines.animation_decline())]
                ),
            ),
        ),
        _row(
            "2 check_field: a decline labelled text_animation on a framing ask must FAIL",
            False,
            _card_check(
                "title_animation",
                _synthetic_card(
                    [], [_decline(framing, "text_animation", lines.animation_decline())]
                ),
            ),
        ),
        _row(
            "2 check_field: a text_animation decline with another decline's text must FAIL",
            False,
            _card_check(
                "title_animation",
                _synthetic_card(
                    [], [_decline(animate, "text_animation", lines.title_position_decline())]
                ),
            ),
        ),
        _row(
            "2 check_field: a decline about another ask (title position) must FAIL",
            False,
            _card_check(
                "opening_title",
                _synthetic_card(
                    [], [_decline(title_spot, "title_position", lines.title_position_decline())]
                ),
            ),
        ),
        _row(
            "2 check_field: a route decline no receipt carries is not on the card must FAIL",
            False,
            _card_check(
                "title_animation",
                _synthetic_card(
                    [],
                    [_decline(animate, "text_animation", lines.animation_decline())],
                    receipted=False,
                ),
            ),
        ),
    ]
    return rows


def predicate_controls() -> list[dict]:
    honest = _synthetic_card([_line("pacing", "The balanced pacing isn't applied on this video")])
    both = {"en": honest, "tr": honest}
    named = _synthetic_card([], reply="Balanced pacing\nText Explainer direction")
    bare = _synthetic_card([])
    return [
        _row(
            "2 a card that names the pace without its registry line must FAIL",
            False,
            field_verdict("pacing", {"en": named, "tr": named}, False)["pass"],
        ),
        _row(
            "2 the card's pacing registry line must PASS",
            True,
            field_verdict("pacing", both, False)["pass"],
        ),
        _row(
            "2 a render recipe that meets the field must PASS",
            True,
            field_verdict("pacing", {"en": bare, "tr": bare}, True)["pass"],
        ),
        _row(
            "3 an undecided receipt must FAIL",
            False,
            ask_verdict({"status": "partial", "verification": "unchecked", "reason": "x"})["pass"],
        ),
        _row(
            "3 a typed decline before approval must PASS",
            True,
            ask_verdict(
                {"status": "not_possible", "verification": "checked", "reason": "I can't yet"}
            )["pass"],
        ),
        _row(
            "3 a checked met receipt must PASS",
            True,
            ask_verdict({"status": "met", "verification": "checked", "reason": None})["pass"],
        ),
        _row(
            "4 a recipe value that is not the asked animation must FAIL",
            False,
            honors("title_animation", "fade", "pop"),
        ),
        _row(
            "4 a recipe value that is the asked animation must PASS",
            True,
            honors("title_animation", ["pop", "pop"], "pop"),
        ),
        _row(
            "4 a boolean where a value belongs must FAIL",
            False,
            honors("title_animation", True, "pop") or honors("length_s", True, 30),
        ),
        _row("4 a length 50% off the asked one must FAIL", False, honors("length_s", 45.0, 30)),
        _row(
            "4 a length within 10% of the asked one must PASS", True, honors("length_s", 31.5, 30)
        ),
    ]


_SYNTHETIC_WRITER = """
def names_only(step, plan):
    plan = {"target_duration_s": 3}
    return step.target_duration_s, plan


def reads_it(strategy):
    return strategy.get("target_duration_s")


def reads_typed(raw_strategy):
    typed = _strategy(raw_strategy)
    return typed.target_duration_s


class Plan:
    target_duration_s: float = 0.0
"""


def gate_controls(route: str | None) -> list[dict]:
    """The consumer gate, its reader scan and the carrier rule, on synthetic claims and code."""
    collide = [
        "        if step.in_s is None or step.target_duration_s is None:",
        '    plan = {"target_duration_s": 3}',
        '    seconds = row["target_duration_s"]',
        "    target = view.target_duration_s",
    ]
    writer = "pipeline/synthetic_writer.py"
    synthetic = SimpleNamespace(files=(writer,), functions=())
    texts = {writer: _SYNTHETIC_WRITER}

    def carrier(symbol: str) -> bool:
        return (
            via_problem(f"{writer}:{symbol}", "target_duration_s", readers=synthetic, texts=texts)
            is None
        )

    rows = [
        _row(
            "10 an assembly step's target_duration_s is not a strategy read must FAIL",
            False,
            bool(reads_in(collide, "target_duration_s")),
            collide,
        ),
        _row(
            "10 strategy.get('target_duration_s') is a strategy read must PASS",
            True,
            bool(reads_in(['    raw = strategy.get("target_duration_s")'], "target_duration_s")),
        ),
        _row(
            "10 a carrier that is free text is not a carrier must FAIL",
            False,
            via_problem("trust me", "caption_style") is None,
        ),
        _row(
            "10 a carrier in a file that does not exist must FAIL",
            False,
            via_problem("pipeline/no_such_writer.py:plan", "caption_style") is None,
        ),
        _row(
            "10 a function that only names the field is not a carrier must FAIL",
            False,
            carrier("names_only"),
        ),
        _row("10 a class that names the field is not a carrier must FAIL", False, carrier("Plan")),
        _row(
            "10 a function in the route's own files that reads the field must PASS",
            True,
            carrier("reads_it"),
        ),
        _row(
            "10 a read through a converted strategy (typed = _strategy(s)) must PASS",
            True,
            carrier("reads_typed"),
        ),
    ]
    registry = _registry()
    if registry is None or route is None or registry.tables.table_for(route) is None:
        detail = "pre-registry code: no table to gate"
        names = (
            "10 the schema class CreativeStrategy is not a carrier must FAIL",
            "10 a function in another route's file is not this route's carrier must FAIL",
            "10 a route that lists another route's reader file must be flagged",
            "10 a new unconsumed field must be flagged",
            "10 an Applied claim with no reader must be flagged",
            "10 an Applied claim read only as step.<field> must be flagged",
            "10 an Applied claim with a free-text carrier must be flagged",
            "10.lines a line reworded to say it is applied must be flagged",
        )
        return rows + [_row(n, False, None, detail) for n in names]
    from app.kria.route_capabilities import Applied, NotApplied, table_for

    other = next(str(rid) for rid in registry.tables.ROUTE_TABLES if str(rid) != route)
    other_file = table_for(other).READERS.files[0]
    schema = "agents/_schemas/creator_agent.py:CreativeStrategy"
    rows += [
        _row(
            "10 the schema class CreativeStrategy is not a carrier must FAIL",
            False,
            via_problem(schema, "caption_style", route) is None,
            via_problem(schema, "caption_style", route),
        ),
        _row(
            "10 a function in another route's file is not this route's carrier must FAIL",
            False,
            via_problem(f"{other_file}:x", "caption_style", route) is None,
            via_problem(f"{other_file}:x", "caption_style", route),
        ),
        _row(
            "10 a route that lists another route's reader file must be flagged",
            False,
            not ownership_problems(route, extra_files=(other_file,)),
            ownership_problems(route, extra_files=(other_file,)),
        ),
    ]
    empty = {"not_applied": [], "declines": []}
    gate = consumer_gate(route, empty, extra_fields=("zz_new_unconsumed_field",))
    rows.append(
        _row(
            "10 a new unconsumed field must be flagged",
            False,
            "zz_new_unconsumed_field" not in gate["undisclosed"],
            gate["undisclosed"][-1:],
        )
    )
    gate = consumer_gate(
        route,
        empty,
        extra_fields=("zz_claimed_field",),
        extra_handling={"zz_claimed_field": Applied()},
    )
    rows.append(
        _row(
            "10 an Applied claim with no reader must be flagged",
            False,
            "zz_claimed_field" not in gate["claimed_applied_unread"],
            gate["claimed_applied_unread"],
        )
    )
    gate = consumer_gate(
        route,
        empty,
        extra_handling={"target_duration_s": Applied()},
        sources={"pipeline/synthetic_writer.py": collide},
    )
    rows.append(
        _row(
            "10 an Applied claim read only as step.<field> must be flagged",
            False,
            "target_duration_s" not in gate["claimed_applied_unread"],
            gate["claimed_applied_unread"],
        )
    )
    gate = consumer_gate(
        route, empty, extra_handling={"caption_style": Applied(via="the captions just work")}
    )
    rows.append(
        _row(
            "10 an Applied claim with a free-text carrier must be flagged",
            False,
            "caption_style" not in gate["claimed_applied_unread"],
            gate["bad_carriers"].get("caption_style"),
        )
    )
    table = table_for(route)
    field = next((f for f, h in table.FIELD_HANDLING.items() if isinstance(h, NotApplied)), None)
    if field is None:
        rows.append(
            _row(
                "10.lines a line reworded to say it is applied must be flagged",
                False,
                None,
                "the route's table has no NotApplied field",
            )
        )
        return rows
    claim = NotApplied(lambda _v, _s: "It is applied exactly as planned, nothing is skipped")
    with patch.dict(table.FIELD_HANDLING, {field: claim}):
        reworded = lines_result(route)
    rows.append(
        _row(
            f"10.lines a line reworded to say it is applied ({field}) must be flagged",
            False,
            reworded["pass"],
            reworded["why_it_fails"][:1],
        )
    )
    return rows


# --- the replay contract on synthetic modules over a synthetic route

_SYNTHETIC_ROUTE = """
from app.config import settings

DEFAULT_PACE = "balanced"
DEFAULT_LENGTH_S = 18.0


def _fact(requirements, name):
    return next((r["facts"][name] for r in requirements if name in (r.get("facts") or {})), None)


def plan(strategy, requirements):
    follows = bool(settings.SWITCH)
    animation = strategy.get("title_animation") or _fact(requirements, "animation")
    pace = strategy.get("pacing")
    length = strategy.get("creator_duration_s") or _fact(requirements, "duration_s")
    return {
        "rows": [{"entrance": animation if follows else None}],
        "pace": pace if follows and pace else DEFAULT_PACE,
        "length_s": float(length) if follows and length else DEFAULT_LENGTH_S,
    }


def label(text):
    return {"text": str(text)}
"""
_DEFAULTS = '{"title_animation": None, "pace": "balanced", "length_s": 18.0}'
_ECHO = (
    '{"title_animation": s.get("title_animation"), "pace": s.get("pacing") or "balanced", '
    '"length_s": float(s.get("creator_duration_s") or 18.0)}'
)
_SYNTHETIC_MODULES = {
    "an honest recipe from the route's code must PASS": (
        True,
        "def recipe(fx):\n"
        "    out = route.plan(fx['strategy'], fx['brief']['requirements'])\n"
        "    return {'title_animation': out['rows'][0]['entrance'], 'pace': out['pace'],\n"
        "            'length_s': out['length_s']}\n",
    ),
    "a constant recipe must FAIL": (
        False,
        "def recipe(fx):\n"
        "    route.label('x')\n"
        "    return {'title_animation': 'pop', 'pace': 'relaxed', 'length_s': 30.0}\n",
    ),
    "a recipe keyed on the asks being present must FAIL": (
        False,
        "def recipe(fx):\n"
        "    route.label('x')\n"
        "    if not fx['brief']['requirements']:\n"
        f"        return {_DEFAULTS}\n"
        "    return {'title_animation': 'pop', 'pace': 'relaxed', 'length_s': 30.0}\n",
    ),
    "a recipe keyed on the asks and the lane switch (one route call) must FAIL": (
        False,
        "def recipe(fx):\n"
        "    route.label('x')\n"
        "    if not (fx['brief']['requirements'] and settings.SWITCH):\n"
        f"        return {_DEFAULTS}\n"
        "    return {'title_animation': 'pop', 'pace': 'relaxed', 'length_s': 30.0}\n",
    ),
    "an echo of the asked values keyed on the lane switch must FAIL": (
        False,
        "def recipe(fx):\n"
        "    route.label('x')\n"
        "    s = fx['strategy']\n"
        "    if not settings.SWITCH:\n"
        f"        return {_DEFAULTS}\n"
        f"    return {_ECHO}\n",
    ),
    "an echo of the asked values beside one route call must FAIL": (
        False,
        f"def recipe(fx):\n    route.label('x')\n    s = fx['strategy']\n    return {_ECHO}\n",
    ),
}
_REPLAY_FX = {
    "fixture_id": "replay-control",
    "route": "control",
    "strategy": {
        "title_animation": "pop",
        "pacing": "relaxed",
        "creator_duration_s": 30,
        "target_duration_s": 30,
    },
    "brief": {
        "requirements": [
            {"id": "r1", "kind": "style", "scope": "global", "facts": {"animation": "pop"}},
            {"id": "r2", "kind": "timing", "scope": "global", "facts": {"duration_s": 30}},
        ]
    },
    "checks": {
        "honored": {
            "fields": {
                "title_animation": "title_animation",
                "pacing": "pace",
                "target_duration_s": "length_s",
            },
            "asks": {"r1": "title_animation", "r2": "length_s"},
        }
    },
}


def replay_contract_controls() -> list[dict]:
    """`grade_recipe` on synthetic modules over a synthetic route file: only the honest one
    may come out valid with every target met."""
    from app.config import settings

    switch = "talking_head_route_follows_prompt_enabled"
    if not hasattr(settings, switch):
        return [
            _row(f"replay contract: {name}", want, None, "no lane switch on this code")
            for name, (want, _src) in _SYNTHETIC_MODULES.items()
        ]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic_route.py"
        path.write_text(_SYNTHETIC_ROUTE.replace("SWITCH", switch), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("synthetic_route", path)
        assert spec is not None and spec.loader is not None
        route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(route)
        code = RouteCode(frozenset({_real(str(path))}), frozenset())
        targets = honored_targets(_REPLAY_FX)
        for name, (want, source) in _SYNTHETIC_MODULES.items():
            module = types.ModuleType("synthetic_replay")
            module.__dict__.update(route=route, settings=settings)
            exec(
                compile(source.replace("SWITCH", switch), f"<control: {name}>", "exec"),  # noqa: S102
                module.__dict__,
            )
            graded = grade_recipe(module, _REPLAY_FX, code, switch, targets)
            got = graded["valid"] and all(graded["honored"].values())
            failed = [r["name"] for r in graded["controls"] if r["ok"] is not True]
            rows.append(_row(f"replay contract: {name}", want, got, failed))
    return rows


def replay_controls(replay: dict) -> list[dict]:
    """The real route replay module's own controls (`grade_recipe`)."""
    if replay.get("source") is None:
        why = "the route replay raised" if replay.get("error") else "no route replay"
        return [_row(f"replay: {why}, so no render claim to control", True, True)]
    return list(replay.get("controls") or [])


def negative_controls(route: str | None, replay: dict | None = None) -> list[dict]:
    """``ok`` None: the control could not run on code that predates the registry."""
    rows = [
        *predicate_controls(),
        *check_function_controls(),
        *gate_controls(route),
        *replay_contract_controls(),
    ]
    if replay is not None:
        rows += replay_controls(replay)
    return rows


def controls_valid(controls: list[dict], *, pre_registry: bool) -> bool:
    """Every control behaved. One that could not run (n/a) is valid only on code that predates
    the registry; with a registry package on disk it means the grader lost its own checks."""
    if any(c["ok"] is False for c in controls):
        return False
    return pre_registry or all(c["ok"] is True for c in controls)


# ---------------------------------------------------------------------------- report


def run_offline(fixture_dir: Path, route: str | None = None) -> dict:
    fx = load_fixture(fixture_dir)
    if route is not None:
        fx = {**fx, "route": route}
    pre_registry = not REGISTRY_PACKAGE.is_file()
    registry_error = None
    try:
        _registry()
    except RegistryBroken as exc:
        registry_error = str(exc)
    with _prod_flags():
        holder: dict[str, Any] = {}

        def cards() -> dict[str, dict]:
            if "cards" not in holder:
                holder["cards"] = approval_cards(fx)
            return holder["cards"]

        replay = _guarded("replay", "route replay", lambda: route_replay(fx, fx["route"]))
        if "verdict" in replay:  # the replay module raised: no render evidence, reported
            replay = {"source": None, "honored": {}, "valid": True, "error": replay}
        checks = [_guarded(1, "route", lambda: check_route(fx, cards()))]
        checks += [
            _guarded(f"2.{f}", f, lambda f=f: check_field(fx, cards(), replay, f))
            for f in planned_fields(fx)
        ]
        checks += [
            _guarded(f"3.{r}", r, lambda r=r: check_ask(fx, cards(), replay, r))
            for r in planned_asks(fx)
        ]
        checks += [
            _guarded(t.check_id, t.ident, lambda t=t: check_honored(fx, replay, t))
            for t in honored_targets(fx)
        ]
        checks.append(
            _guarded(
                "10.claims", "registry claims", lambda: claims_result(fx["route"], cards()["en"])
            )
        )
        checks.append(_guarded("10.lines", "card lines", lambda: lines_result(fx["route"])))
        gate = _guarded(10, "consumer gate", lambda: gate_result(10, fx["route"], cards()["en"]))
        checks.append({**gate, "report_only": True, "kind": "report"})
        try:
            controls = negative_controls(fx["route"], replay)
        except RegistryBroken as exc:
            controls = [_row("the route registry imports", True, False, str(exc))]
        if registry_error is not None and not any(c["ok"] is False for c in controls):
            controls.append(_row("the route registry imports", True, False, registry_error))
        try:
            shown = cards()
        except Exception as exc:  # noqa: BLE001 - reported, never green
            shown = {"error": f"{type(exc).__name__}: {exc}"}
    for c in checks:  # a guarded check that raised carries no kind of its own
        c.setdefault("kind", _KIND_OF_ID.get(str(c["id"]).split(".")[0], "disclosed_ok"))
        if str(c["id"]) in {"10.claims", "10.lines"}:
            c["kind"] = "gate"
    scored = [c for c in checks if not c.get("report_only")]
    targets = lane_targets(fx["fixture_id"])
    red = [str(c["id"]) for c in scored if not c["pass"]]
    by_kind = {
        kind: {
            "passed": sum(1 for c in scored if c["kind"] == kind and c["pass"]),
            "failed": sum(1 for c in scored if c["kind"] == kind and not c["pass"]),
        }
        for kind in ("route", "disclosed_ok", "honored", "gate")
    }
    return {
        "fixture": fx["fixture_id"],
        "route": fx["route"],
        "mode": "offline",
        "source": fx.get("source", {}),
        "network_attempts_blocked": len(_BASE._NET_ATTEMPTS),
        "registry": not pre_registry and registry_error is None,
        "pre_registry": pre_registry,
        "passed": sum(1 for c in scored if c["pass"]),
        "failed": sum(1 for c in scored if not c["pass"]),
        "all_green": all(c["pass"] for c in scored),
        "by_kind": by_kind,
        "inconclusive": [str(c["id"]) for c in checks if c["verdict"] == "INCONCLUSIVE"],
        "lane_targets": targets,
        "expected_red": [i for i in red if i in targets],
        "unexpected_red": [i for i in red if i not in targets],
        "unexpected_green": sorted(set(targets) - set(red)),
        "not_graded": (fx.get("checks") or {}).get("not_graded") or {},
        "exclusions": exclusions(fx),
        "report_only": [c["id"] for c in checks if c.get("report_only")],
        "controls_ok": controls_valid(controls, pre_registry=pre_registry),
        "negative_controls": controls,
        "replay": replay,
        "card": shown,
        "checks": checks,
    }


_KIND_OF_ID = {
    "1": "route",
    "2": "disclosed_ok",
    "3": "disclosed_ok",
    "4": "honored",
    "5": "honored",
}


def summary(report: dict) -> str:
    out = [
        f"{report['fixture']} route scorecard ({report['route']}, offline, "
        f"{report['network_attempts_blocked']} network attempts blocked)",
        f"route registry on this code: {'yes' if report['registry'] else 'no'}",
        "",
        f"{'#':<22} {'kind':<13} {'verdict':<13} check",
    ]
    for c in report["checks"]:
        target = report["lane_targets"].get(str(c["id"]))
        lane = f"  [lane target {target.get('lane')}]" if target and not c["pass"] else ""
        out.append(f"{str(c['id']):<22} {c['kind']:<13} {c['verdict']:<13} {c['name']}{lane}")
        for line in c["why_it_fails"]:
            out.append(f"      - {line}")
    kinds = ", ".join(
        f"{kind} {v['passed']}/{v['passed'] + v['failed']}" for kind, v in report["by_kind"].items()
    )
    left_out = report["exclusions"]
    out += [
        "",
        f"{report['passed']} verified, {report['failed']} not verified ({kinds}; "
        f"check(s) {report['report_only']} are report-only and never fail anything)",
        f"expected red (lane targets): {report['expected_red'] or 'none'}",
        f"unexpected red: {report['unexpected_red'] or 'none'}",
        f"unexpected green (remove from lane_targets.json): {report['unexpected_green'] or 'none'}",
        f"left out of grading by the fixture's lists: {left_out['count']} "
        f"(fields {left_out['fields']}, asks {left_out['asks']}; "
        f"without a reason in not_graded: {left_out['unexplained'] or 'none'})",
    ]
    for item, why in report["not_graded"].items():
        out.append(f"not graded {item}: {why}")
    out += [
        "",
        "negative controls (each must behave as stated):",
    ]
    for c in report["negative_controls"]:
        mark = "n/a" if c["ok"] is None else ("ok " if c["ok"] else "BAD")
        out.append(f"  {mark} {c['name']}")
    if not report["controls_ok"]:
        out.append("\nSCORECARD INVALID: a negative control did not behave (or could not run)")
    return "\n".join(out)


def exit_code(report: dict, *, strict: bool = False) -> int:
    """2 INVALID; 1 an unexpected red, a fixture exclusion with no reason, or (``strict``)
    any red; else 0."""
    if not report["controls_ok"]:
        return 2
    if report["unexpected_red"] or report["exclusions"]["unexplained"]:
        return 1
    return 1 if strict and not report["all_green"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--fixture", help=f"fixture name under {FIXTURES.relative_to(REPO)}")
    where.add_argument("--fixture-dir", type=Path, help="a fixture directory anywhere")
    where.add_argument(
        "--write-golden-lines", action="store_true", help="re-record the card-line golden"
    )
    parser.add_argument("--route", help="grade against this route instead of the fixture's")
    parser.add_argument("--out", type=Path, help="JSON report path")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every check passes")
    args = parser.parse_args(argv)
    _BASE._bootstrap()
    if args.write_golden_lines:
        print(f"wrote {_shown_path(write_golden_lines())}")
        return 0
    fixture_dir = args.fixture_dir or FIXTURES / args.fixture
    report = run_offline(fixture_dir, args.route)
    out = args.out or REPO / "test-results" / f"{fixture_dir.name}-replay" / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    text = summary(report)
    (out.parent / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else text)
    print(f"\nreport: {out}", file=sys.stderr)
    return exit_code(report, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
