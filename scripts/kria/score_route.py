#!/usr/bin/env python3
"""Route scorecard: does every planner-chosen field and every stated ask reach the render, or
the approval card before approval, on the route that renders the draft? (KRI-1097 wave 2)

Generalized from ``score_assos.py`` (which keeps the unified-montage render replay). A fixture
is a recorded or reconstructed draft under ``tests/fixtures/kria_replay/<name>/fixture.json``:

    {"fixture_id", "route": "<RouteId>", "message", "planner_summary", "brief", "strategy",
     "clips": [{"id", "kind", "duration_s", "has_audio"?, "analysis"?}],
     "client_protocol"?, "replay_inputs"?: {...},
     "source": {"recorded": [...], "reconstructed": [...]},
     "checks"?: {"fields": [...], "asks": ["r1", ...],
                 "honored"?: {"fields": {field: key}, "asks": {req_id: key}},
                 "perturb"?: {"length_s": [seconds, ...]}, "not_graded"?: {item: why}}}

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
          (``not_possible``, checked, with a reason; a route decline counts only when it is the
          registry's decline for what the ask's own typed facts ask, the rule check 2 uses),
          or for a length ask the card's own ``target_duration_s`` registry line; both languages.
4.<field> honored / 5.<id> honored: the value the route's render entry returns meets what was
          asked (``checks.honored``), under the replay contract below.
10.claims gate: every field the route's table marks ``Applied`` is read by a function of the
          route's code (``READERS``), or by its ``via``: a function in the route's own
          ``READERS`` or one of the ``SHARED_CARRIERS``. Either way the function reads the field
          off its strategy parameter (or a local computed from one; ``step.target_duration_s``
          is no strategy) and lets that value reach what it returns, a call it makes for its
          effect, an attribute it sets or a branch that returns or raises (AST, best effort); a
          class, another route's file, a similarly named field, a local dict, a log-only read or
          a helper that drops the field is no read. A named ``via`` that is no carrier fails
          even when another line reads the field. A reader listed by two routes fails it.
10.lines  gate: the card lines every route's table builds for fixed probe plans, both
          languages, with their typed codes, equal the recorded golden
          (``tests/fixtures/kria_replay/golden_lines.json``), and every not-applied line says
          "not" in its language (``NEGATION``), so a line turned into a claim fails even after
          the golden is re-recorded with it.
10.entry  gate: the table's ``RENDER_ENTRY`` is a function of the route's own ``READERS`` that
          another function of the route's code calls, with a parseable recipe path per key.
10.pin    gate: the table's entry and recipe paths equal the route's pin in
          ``tests/fixtures/kria_replay/render_entries.json`` (grader-owned, so moving either
          is a reviewer-visible diff, like the golden lines), and so do the entry's parameters
          but for ``PLAN_PARAMS`` a lane adds to pass the plan in (any other new parameter is
          an override the module could feed an asked value through); the pin's job runner
          imports the entry from its module (or defines it in its own file), calls it with a
          value computed from its own parameters (the job, its snapshot, its strategy; never a
          literal or an unrelated local) in each of the entry's plan parameters and uses what
          it returns, and a function of ``tasks/generative_build.py`` calls that runner.
10.       consumer gate (REPORT ONLY): fields the route ignores that the card does not disclose.

Replay contract (render evidence for checks 2, 3, 4, 5). Each route table names its render
entry (``RENDER_ENTRY``): the one function the route's job runner builds the recipe with (for a
huge job runner, the smallest pure function that compiles the recipe: the voiceover, Talking and
narrated compilers, ``plan_lipsync_montage``, the speech compiler), and per key where its value
sits in what the entry returns (``title_animation`` the title entrance, ``pace`` a pace name or
the clip holds, ``length_s`` the rendered seconds, ``caption_mode`` none / sentence / word). A
lane adds ``scripts/kria/route_replays/<route>.py`` with ``recipe(fx)`` that calls that entry
with ``fx["strategy"]`` and ``fx["requirements"]`` (or ``fx["brief"]``) as they are, building
any other input from ``fx["clips"]`` / ``fx["inputs"]`` (the fixture's ``replay_inputs``). Its
return value is ignored: the grader takes the value at each path the moment the entry returns.
The lane keeps the entry the single source by passing the plan into it, never by patching the
recipe after it returns: a patch after the entry earns nothing here, 10.entry checks the
route's code calls the entry and 10.pin that the entry and paths are the pinned ones, rendered
from by the pinned job runner.

Every run executes a fresh copy of the module in a fresh interpreter (one per lane-switch
value, the switch set before the module first runs). The job reaches that interpreter on stdin
from a file with no name, so no argument or path leads the module to the other runs, and it
runs them in an order drawn from the OS's randomness, so a counter kept in route code cannot
map a run to an answer written into the module. The plan and the asks reach the module as
recording proxies with the surface the job runner hands the entry: the plan a dict; each ask
read as an item (``req["facts"]``) or as the real ``BriefRequirement`` validated from it
(``req.facts``, ``req.kind``, ``req.live``, ``req.text()``...); the brief the same over a real
``CreativeBrief`` whose ``requirements`` and ``live()`` give the recorded asks. Any access whose
nearest owning frame is the module's file (getattr, item, ``.get``, iteration, ``dict()``,
copy, ``==``, hash, ``repr``, ``.value``, ``.live()``...) is noted, and so is the module
reading the lane switch or the whole settings object, or handing the plan, the asks or a
function of its own to any route function but the entry (a helper that reads the plan for
it). A static pass refuses a module that imports beyond
``app`` (never ``app.config``) and plain stdlib, or imports a name that comes from anywhere else
through them (an app module's ``sys``, ``Path`` or ``json``, resolved by importing it), names the
settings or a frame, heap, memory or pickle module or accessor (``sys``, ``gc``, ``ctypes``,
``inspect``, ``pickle``, ``_getframe``, ``f_back``...), introspects (``getattr``, ``type``,
``object``, ``vars``, dunders) or changes state that outlives a run. A claimed key must:

* track every other value (every title animation, every pace, a length ladder or
  ``checks.perturb.length_s``, every caption style), clip holds growing fast to relaxed;
* hold with the plan field alone (typed ask dropped) and with the typed ask alone (field
  dropped);
* leave every other claimed value as it was when moved or dropped (a moved length may re-time
  the clips), and with nothing asked for it equal the value with nothing asked at all;
* change nothing with the lane switch (``LANE_SWITCH``) off;
* come from runs that each call the entry exactly once with the fixture's own plan objects, with
  no module access to the plan, the asks or the switch, no static-rule break and no error.

A module that fails one makes the scorecard INVALID (exit 2). The same controls run every time
on synthetic modules over synthetic routes through the same fresh interpreters: only the honest
module over the honest entry may pass.

Lane targets: ``tests/fixtures/kria_replay/lane_targets.json`` lists, per fixture, the checks
expected NOT VERIFIED today and the lane that turns each green (``targets``), and every render
target the fixture must keep grading (``must_grade``). The report splits failures into
``expected_red`` and ``unexpected_red``, lists ``unexpected_green`` (a lane landed: remove it
from ``targets``) and ``missing_targets`` (a target the fixture no longer grades). The file only
moves toward green from its foundation copy, ``lane_targets.foundation.json`` (``target_drift``):
``must_grade`` may only grow, ``targets`` may only lose entries, and each lost one must be
VERIFIED on the run, so a lane can neither hide a red nor relabel a regression as a lane target.
An unexpected red, a missing target or a drift exits 1.

Negative controls (mandatory, INVALID rule as in ``score_29_ekim.py``): each check's predicate
and the check functions themselves get a synthetic card that ignores the ask (must FAIL) and one
that honours it (must PASS); the gates get unread fields, claims with no reader, bad carriers,
a shared reader file, a reworded line, an unpinned entry, recipe path or parameter, a runner
that drops, only names or shadows the entry or hands it no plan, a constant or its own, and lane
targets that drift (all must be flagged). A control that cannot run because
the registry fails to import is INVALID too; only code that predates the registry (no package)
shows them n/a. A failed control makes the scorecard INVALID (exit 2).

What a lane reviewer must still check by hand:

* the job runner renders from the entry's return and does not override the claimed recipe
  field afterwards (10.pin only shows one entry call of the pinned runner gets the job's plan
  and passes its value on, into its return, a call, an attribute or a branch; not that the
  recipe it renders is that value, nor that the call runs on the path the job takes);
* a diff of ``render_entries.json``: that the new entry is the function the route renders
  from, and that the pinned runner is the one the dispatch runs for the route (10.pin only
  shows some function of ``generative_build.py`` calls it);
* the entry's recipe path is the field the phone actually draws (the title rows, not a copy),
  and a plan parameter (``PLAN_PARAMS``) a lane adds carries the plan, not an override under
  that name;
* any change to ``lane_targets.foundation.json``: only the foundation records it, and a lane
  that edits it can hide a red or relabel a regression past ``target_drift``;
* a route that branches on ``isinstance(req, BriefRequirement)`` or ``CreativeBrief``: the
  replay's asks are mappings that read like those types, not instances of them;
* ``replay_inputs`` and the clips carry no plan or ask values;
* the escape classes the grader does not test mechanically, which the lane's adversarial
  reviewer must attempt against that lane's replay module and the route code it calls: frame
  walking (a route helper that reads the worker's frames for the module), the heap (``gc``),
  raw memory (``ctypes``), behaviour keyed to time or randomness, state hidden in imported
  objects, C-level reads and threads (the static pass refuses these only by name, and only in
  the module's own file);
* a lane that reads a ``style`` animation ask through `animation_ask` passes ``styles=True``
  behind its own switch (the default reads ``text`` asks only, as the unified montage does),
  so it covers every ask the card's ``asks_text_animation`` declines;
* an ``Applied()`` field without ``via`` (a file-wide read scan, not a data flow), and a
  ``via`` carrier whose value reaches a call or attribute the render never uses;
* that a not-applied line's sentence is true for the route, beyond containing a negation;
* outside approval readiness the planner summary can say "balanced pace for about 30 seconds"
  above lines saying the pace and length are not applied (kept by design; KRI-1097 wave 2).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import functools
import importlib.util
import inspect
import itertools
import json
import os
import random
import re
import sys
import tempfile
import types
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from enum import Enum
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
RENDER_ENTRIES = FIXTURES / "render_entries.json"
LANE_TARGETS_FOUNDATION = FIXTURES / "lane_targets.foundation.json"
REPLAYS = HERE / "route_replays"
REGISTRY_PACKAGE = APP / "kria" / "route_capabilities" / "__init__.py"
BUILDER = "tasks/generative_build.py"
CREATOR_ID = uuid.UUID("00000000-0000-4000-8000-000000001097")
LANGS = ("en", "tr")

# The typed codes of a table's card lines (`route_capabilities._types`): a line counts only
# with the code its table entry gives the field.
TABLE_CODES = frozenset({"not_applied_on_route", "recorded_only_on_route"})
NOT_APPLIED_CODE = "not_applied_on_route"
# Each route's lane kill switch (`config.py`): off, the route is byte-identical to before the
# lane, so an ask must change nothing in its recipe.
# The replay contract's own inputs (``fx["strategy"]``, ``fx["requirements"]``,
# ``fx["brief"]``): the only parameters a lane may add to its render entry without a pin edit,
# and the ones its job runner must fill from the job.
PLAN_PARAMS = ("strategy", "requirements", "brief")
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
    by_field = {spec.field: key for key, spec in RECIPE_KEYS.items()}
    out = []
    for kind, prefix in (("fields", "4"), ("asks", "5")):
        listed = honored.get(kind) or {}
        if not isinstance(listed, Mapping):  # a bare list names fields, never their keys
            listed = {str(i): by_field.get(str(i), str(i)) for i in listed}
        for ident, key in listed.items():
            out.append(Target(f"{prefix}.{ident}", kind, ident, str(key)))
    return out


def lane_targets(fixture_id: str) -> dict[str, dict]:
    """Checks expected NOT VERIFIED on today's code for this fixture, with their lane."""
    if not LANE_TARGETS.is_file():
        return {}
    data = json.loads(LANE_TARGETS.read_text(encoding="utf-8"))
    return dict((data.get("targets") or {}).get(fixture_id) or {})


def must_grade(fixture_id: str) -> list[str]:
    """Check ids this fixture must keep grading (``must_grade``: every render target, red or
    green), whatever its ``checks`` say."""
    if not LANE_TARGETS.is_file():
        return []
    data = json.loads(LANE_TARGETS.read_text(encoding="utf-8"))
    return list((data.get("must_grade") or {}).get(fixture_id) or [])


def missing_targets(required: list[str], graded: set[str]) -> list[str]:
    """Required check ids (lane targets and ``must_grade``) the scorecard did not run."""
    return sorted({i for i in required if i not in graded})


def target_drift(
    fixture_id: str,
    verified: set[str],
    current: Mapping[str, Any] | None = None,
    foundation: Mapping[str, Any] | None = None,
) -> list[str]:
    """How the fixture's entries in ``LANE_TARGETS`` moved off ``LANE_TARGETS_FOUNDATION``
    (empty when only toward green): ``must_grade`` may only grow, ``targets`` may only lose
    entries, and each lost one must be a check the grader ``verified`` on this run, so a lane
    can neither hide a red nor relabel a regression as a lane target. ``current`` /
    ``foundation`` (controls only) stand in for the files."""
    if foundation is None:
        if not LANE_TARGETS_FOUNDATION.is_file():
            return [] if not LANE_TARGETS.is_file() else ["the foundation copy is missing"]
        foundation = json.loads(LANE_TARGETS_FOUNDATION.read_text(encoding="utf-8"))
    if current is None:
        lane_file = LANE_TARGETS.is_file()
        current = json.loads(LANE_TARGETS.read_text(encoding="utf-8")) if lane_file else {}

    def of(data: Mapping[str, Any], part: str) -> Any:
        return (data.get(part) or {}).get(fixture_id) or {}

    kept = set(of(current, "must_grade"))
    was, now = of(foundation, "targets"), of(current, "targets")
    return [
        *(f"must_grade lost {i}" for i in of(foundation, "must_grade") if i not in kept),
        *(
            f"{i} is a new lane target: a check that turns red is a regression to fix"
            for i in now
            if i not in was
        ),
        *(
            f"{i} left the lane targets but the grader does not verify it"
            for i in was
            if i not in now and i not in verified
        ),
    ]


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


def _registry_declines(lines: Any) -> tuple[tuple[Any, Any, str | None], ...]:
    """``(ask predicate, decline text, field)`` for every registry route decline."""
    return (
        (lines.asks_text_animation, lines.animation_decline, "title_animation"),
        (lines.asks_captions_on, lines.captions_decline, "caption_style"),
        (lines.asks_title_position, lines.title_position_decline, None),
    )


def decline_field(req: Any, text: str) -> str | None:
    """The strategy field a route decline speaks for: decided by the declined ask's own typed
    facts and the registry's exact decline text in the card's language, never by the subject
    label the table picked. A title-position decline speaks for no field."""
    registry = _registry()
    if registry is None or req is None:
        return None
    for asks, decline, field in _registry_declines(registry.lines):
        if field and asks(req) and text == decline():
            return field
    return None


def decline_about_ask(req: Any, text: str) -> bool:
    """A route decline answers this ask only when it is the registry's decline, in the card's
    language, for what the ask's own typed facts ask (`decline_field`'s rule): an animation
    decline on an animation ask, never on a timing or framing ask."""
    registry = _registry()
    if registry is None or req is None:
        return False
    return any(
        asks(req) and text == decline() for asks, decline, _ in _registry_declines(registry.lines)
    )


def is_route_decline(text: object) -> bool:
    """``text`` is one of the registry's route-decline reasons, in either language."""
    registry = _registry()
    return registry is not None and str(text or "").startswith(
        tuple(registry.lines.DECLINE_PREFIXES)
    )


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
    """A render recipe value the route's entry returns, and what it answers: a strategy field
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
# The value of each caption style as a typed caption fact, for the perturbation runs.
_CAPTION_VALUES = (
    ("none", "off"),
    ("clean", "on"),
    ("kinetic", "word_by_word"),
    ("karaoke", "word_by_word"),
    ("editorial", "on"),
)
# Lengths a stated length is moved to; a fixture whose footage cannot reach some of them lists
# its own (``checks.perturb.length_s``, two or more).
_LENGTH_LADDER = (10, 15, 20, 30, 45, 60)
_PACE_ORDER = ("fast", "balanced", "relaxed")


def _animation_values() -> tuple[str, ...]:
    from typing import get_args

    from app.schemas.text_style_intent import TitleAnimation

    return tuple(get_args(TitleAnimation))


def _pace_values() -> tuple[str, ...]:
    from typing import get_args

    from app.agents._schemas.creator_agent import CreativePace

    return tuple(get_args(CreativePace))


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


def _numbers(values: list) -> list[float]:
    """Every value as seconds, or [] when one is not a number (a boolean is not one)."""
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in values):
        return []
    return [float(v) for v in values]


def honors(key: str, value: Any, wanted: Any, bound: str = "exact") -> bool:
    """The recipe value meets the asked one (the grader's own comparison, per key). A pace is
    a pace name, or the clip holds, each inside that pace's hold bounds (`pace_hold_bounds`)."""
    if wanted is None or value is None:
        return False
    values = value if isinstance(value, list) else [value]
    if not values:
        return False
    if key == "title_animation":
        if str(wanted).strip().casefold() == "any":
            return all(_animation(v) is not None for v in values)
        want = _animation(wanted)
        return want is not None and all(_animation(v) == want for v in values)
    if key == "pace":
        if all(isinstance(v, str) for v in values):
            return all(v.casefold() == str(wanted).casefold() for v in values)
        from app.schemas.edit_proposal import PACE_HOLD_BOUNDS

        holds, bounds = _numbers(values), PACE_HOLD_BOUNDS.get(str(wanted))
        return (
            bool(holds)
            and bounds is not None
            and all(bounds.minimum_s <= h <= bounds.maximum_s for h in holds)
        )
    if key == "length_s":
        from app.services.length_policy import creator_duration_satisfied

        seconds = _numbers(values)
        return len(seconds) == 1 and creator_duration_satisfied(seconds[0], float(wanted), bound)  # type: ignore[arg-type]
    if key == "caption_mode":
        modes = _CAPTION_MODES.get(str(wanted).strip().casefold())
        return bool(modes) and all(v in modes for v in values)
    return False


def perturbations(fx: dict, key: str, wanted: Any) -> list[tuple[Any, Any]]:
    """``(field value, fact value)`` for every other value ``key`` can be asked for."""
    if key == "title_animation":
        current = None if str(wanted).casefold() == "any" else _animation(wanted)
        return [(a, a) for a in _animation_values() if a != current]
    if key == "pace":
        return [(p, p) for p in _pace_values() if p != str(wanted).casefold()]
    if key == "length_s":
        listed = ((fx.get("checks") or {}).get("perturb") or {}).get("length_s")
        ladder = listed if isinstance(listed, list) else _LENGTH_LADDER
        far = [s for s in ladder if abs(float(s) - float(wanted)) > 0.1 * float(wanted)]
        return [(s, s) for s in far]
    if key == "caption_mode":
        current = _CAPTION_MODES.get(str(wanted).strip().casefold())
        return [(f, a) for f, a in _CAPTION_VALUES if _CAPTION_MODES[f] != current]
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


def _drop_field(fx: dict, key: str) -> dict:
    """``fx`` without the strategy field (and its provenance) for ``key``; the asks stay."""
    out = copy.deepcopy(fx)
    spec = RECIPE_KEYS[key]
    for name in (spec.field, *spec.provenance):
        out["strategy"].pop(name, None)
    return out


def _drop_asks(fx: dict, key: str) -> dict:
    """``fx`` without the typed asks for ``key``; the strategy field stays."""
    out = copy.deepcopy(fx)
    fact = RECIPE_KEYS[key].fact
    out["brief"]["requirements"] = [
        r for r in out["brief"]["requirements"] if fact not in (r.get("facts") or {})
    ]
    return out


def _drop_keys(fx: dict, keys: list[str]) -> dict:
    """``fx`` with nothing asked for ``keys``: the fields, their provenance and the asks."""
    out = fx
    for key in keys:
        out = _drop_asks(_drop_field(out, key), key)
    return copy.deepcopy(out)


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


# --- recipe paths: where a key's value sits in what the render entry returns

_SEGMENT = re.compile(
    r"^(?P<name>\*\*|[A-Za-z_]\w*|\d+)?"
    r"(?P<fan>\[(?:(?P<attr>[A-Za-z_]\w*)(?P<op>\^?=)(?P<value>[^\]]*))?\])?$"
)
# A dot outside ``[...]``: a match value may hold one (``[volume=0.0]``).
_SEPARATOR = re.compile(r"\.(?![^\[\]]*\])")


class _Step(NamedTuple):
    name: str | None
    fan: bool
    match: tuple[str, str, str] | None


def parse_path(path: str) -> list[_Step] | None:
    """``a.b[].c[id^=text-].d``: a key or attribute per segment, ``[]`` every item,
    ``[attr=v]`` / ``[attr^=v]`` the items whose ``attr`` is (starts with) ``v`` (a number
    equal to ``v`` when both are numbers), ``**`` any depth, a number an index. None when the
    path does not parse."""
    if not isinstance(path, str) or not path:
        return None
    steps = []
    for raw in _SEPARATOR.split(path):
        found = _SEGMENT.match(raw)
        if found is None or not raw or (found["name"] == "**" and found["fan"]):
            return None
        match = (found["attr"], found["op"], found["value"]) if found["attr"] else None
        steps.append(_Step(found["name"], bool(found["fan"]), match))
    return steps


def _child(node: Any, name: str, opaque: tuple[type, ...]) -> Any:
    if node is None or isinstance(node, (*opaque, str, bytes)):
        return None
    if name.isdigit():
        index = int(name)
        return node[index] if isinstance(node, list | tuple) and index < len(node) else None
    if isinstance(node, Mapping):
        return node.get(name)
    if name.startswith("_"):
        return None
    try:
        return getattr(node, name, None)
    except Exception:  # noqa: BLE001 - a property that raises holds no value
        return None


def _children(node: Any, opaque: tuple[type, ...]) -> list:
    if node is None or isinstance(node, (*opaque, str, bytes, int, float)):
        return []
    if isinstance(node, Mapping):
        return list(node.values())
    if isinstance(node, list | tuple | set | frozenset):
        return list(node)
    if hasattr(node, "__dict__"):
        return list(vars(node).values())
    return [getattr(node, s, None) for s in getattr(node, "__slots__", ())]


def _descend(node: Any, opaque: tuple[type, ...]) -> list:
    out, stack, seen = [], [(node, 0)], set()
    while stack and len(out) < 50_000:
        current, depth = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        out.append(current)
        if depth < 12:
            stack.extend((child, depth + 1) for child in _children(current, opaque))
    return out


def _matches(item: Any, match: tuple[str, str, str], opaque: tuple[type, ...]) -> bool:
    attr, op, value = match
    got = _child(item, attr, opaque)
    got = getattr(got, "value", got) if isinstance(got, Enum) else got
    if not isinstance(got, str | int | float):
        return False
    if op == "=" and isinstance(got, int | float) and not isinstance(got, bool):
        with contextlib.suppress(ValueError):
            return float(got) == float(value)
    return str(got) == value if op == "=" else str(got).startswith(value)


def _walk_path(node: Any, steps: list[_Step], opaque: tuple[type, ...]) -> list:
    if not steps:
        return [node]
    step, rest = steps[0], steps[1:]
    if step.name == "**":
        return [v for sub in _descend(node, opaque) for v in _walk_path(sub, rest, opaque)]
    current = _child(node, step.name, opaque) if step.name else node
    if current is None:
        return []
    if not step.fan:
        return _walk_path(current, rest, opaque)
    items = list(current) if isinstance(current, list | tuple) else []
    kept = [i for i in items if step.match is None or _matches(i, step.match, opaque)]
    return [v for item in kept for v in _walk_path(item, rest, opaque)]


def _leaf(value: Any) -> Any:
    value = value.value if isinstance(value, Enum) else value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return f"<{type(value).__name__}>"


def path_value(obj: Any, path: str, opaque: tuple[type, ...] = ()) -> Any:
    """The value at ``path`` in ``obj``: None when nothing is there, the value when one leaf
    holds it, else the list of leaves (one per title row, one per clip hold)."""
    steps = parse_path(path)
    if steps is None:
        return None
    leaves = [_leaf(v) for v in _walk_path(obj, steps, opaque) if v is not None]
    return None if not leaves else leaves[0] if len(leaves) == 1 else leaves


# --- the replay module's static rules

_REPLAY_STDLIB = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "itertools",
        "math",
        "operator",
        "types",
        "typing",
        "uuid",
    }
)
_REPLAY_BANNED_NAMES = frozenset(
    {
        "settings",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "dir",
        "globals",
        "locals",
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "type",
        "object",
        "super",
        "breakpoint",
        "hash",
        "id",
        "memoryview",
        "input",
        "help",
        "exit",
        "quit",
        "__builtins__",
    }
)
# Modules a replay module may not reach by any name or attribute (``typing.sys``,
# ``dataclasses.inspect``, an app module's ``gc``): frames, the heap, raw memory, pickling,
# the process and its files.
_REPLAY_BANNED_MODULES = frozenset(
    {
        "sys",
        "gc",
        "ctypes",
        "inspect",
        "pickle",
        "_pickle",
        "copyreg",
        "marshal",
        "traceback",
        "threading",
        "_thread",
        "builtins",
        "importlib",
        "os",
        "io",
    }
)
_REPLAY_BANNED_ATTRS = frozenset(
    {
        "settings",
        "f_back",
        "f_locals",
        "f_globals",
        "f_code",
        "gi_frame",
        "cr_frame",
        "tb_frame",
        "_getframe",
        "_getframemodulename",
        "_current_frames",
        "_current_exceptions",
        "currentframe",
        "getouterframes",
        "getinnerframes",
        "getframeinfo",
        "walk_stack",
        "extract_stack",
        "get_objects",
        "get_referrers",
        "get_referents",
        "settrace",
        "setprofile",
    }
    | _REPLAY_BANNED_MODULES
)
_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "clear",
        "update",
        "setdefault",
        "add",
        "discard",
        "sort",
        "reverse",
    }
)


def _root(node: ast.AST) -> str | None:
    """The name an expression hangs off: ``a`` for ``a.b[0].c()``."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute | ast.Subscript | ast.Starred | ast.Await):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.BoolOp):
            node = node.values[0]
        else:
            return None


def _bound_names(fn: ast.AST) -> set[str]:
    """Parameters and every name ``fn`` binds itself (its locals)."""
    names: set[str] = set()
    if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        a = fn.args
        names |= {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
        names |= {p.arg for p in (a.vararg, a.kwarg) if p is not None}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node is not fn:
            names.add(node.name)
    return names


def _imported_value(module: str, name: str) -> tuple[bool, Any]:
    """``(found, value)`` for ``from module import name``, imported the way the module would."""
    try:
        owner = importlib.import_module(module)
    except Exception:  # noqa: BLE001 - a module that does not import gives nothing
        return False, None
    if hasattr(owner, name):
        return True, getattr(owner, name)
    try:
        return True, importlib.import_module(f"{module}.{name}")
    except Exception:  # noqa: BLE001 - no such name or submodule
        return False, None


def _origin(value: Any) -> str:
    """The module a value comes from: a module's own name, the defining module of a class or
    function, else its type's module."""
    if isinstance(value, types.ModuleType):
        return value.__name__
    if isinstance(value, type) or callable(value):
        found = getattr(value, "__module__", None)
        return found if isinstance(found, str) else type(value).__module__
    return type(value).__module__


def lint_replay_module(
    source: str, roots: tuple[str, ...] = ("app",), paths: tuple[str, ...] = ()
) -> list[str]:
    """Why the replay module breaks the replay rules, one line each (empty when it keeps them):
    it imports only the app's code (never ``app.config``) and plain stdlib, each imported name
    coming from one of those (an app or stdlib module that re-exports ``sys``, ``Path`` or
    ``json`` does not launder it), never names the settings or a frame, heap, memory or pickle
    module, introspects (``getattr``, ``type``, ``vars``, dunders, frames) or keeps state from
    one run to the next, and defines ``recipe(fx)``. ``paths`` (controls only) are searched for
    ``roots`` outside ``app``."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"does not parse: {exc}"]
    problems: list[str] = []

    def importable(name: str) -> bool:
        if name == "app.config" or name.startswith("app.config."):
            return False
        name = name[1:] if name.startswith("_") and name[1:] in _REPLAY_STDLIB else name
        return name in _REPLAY_STDLIB or any(name == r or name.startswith(f"{r}.") for r in roots)

    def laundered(module: str, name: str) -> str | None:
        found, value = _imported_value(module, name)
        if not found:
            return f"imports {name} from {module}, which does not have it"
        origin = _origin(value)
        if origin == "builtins" and not callable(value):
            return None
        return None if importable(origin) else f"imports {name} from {module}: it is {origin}'s"

    added = [p for p in paths if p not in sys.path]
    sys.path[:0] = added
    imported: set[str] = set()
    try:
        for node in ast.walk(tree):
            line = f"line {getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add((alias.asname or alias.name).split(".")[0])
                    if not importable(alias.name):
                        problems.append(f"{line}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                imported |= {a.asname or a.name for a in node.names}
                if node.level or not importable(node.module or ""):
                    problems.append(f"{line}: imports from {'.' * node.level}{node.module or ''}")
                    continue
                for alias in node.names:
                    why = "imports settings" if alias.name == "settings" else None
                    why = why or laundered(node.module or "", alias.name)
                    problems += [f"{line}: {why}"] if why else []
    finally:
        for p in added:
            sys.path.remove(p)
    for node in ast.walk(tree):
        line = f"line {getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.alias) and {node.name, node.asname} & _REPLAY_BANNED_MODULES:
            problems.append(f"{line}: names {node.asname or node.name}")
        elif isinstance(node, ast.Name) and (
            node.id in _REPLAY_BANNED_NAMES or node.id in _REPLAY_BANNED_MODULES
        ):
            problems.append(f"{line}: names {node.id}")
        elif isinstance(node, ast.Attribute) and (
            node.attr in _REPLAY_BANNED_ATTRS
            or (node.attr.startswith("__") and node.attr.endswith("__"))
        ):
            problems.append(f"{line}: reads .{node.attr}")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            problems.append(f"{line}: {type(node).__name__.lower()} state")
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign | ast.Delete)
            else [node.target]
            if isinstance(node, ast.AugAssign | ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Attribute | ast.Subscript) and _root(target) in imported:
                problems.append(f"line {node.lineno}: changes {_root(target)} (imported code)")
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        own = _bound_names(fn)
        for node in ast.walk(fn):
            changed = None
            if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(
                node.ctx, ast.Store | ast.Del
            ):
                changed = _root(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
            ):
                changed = _root(node.func.value)
            if changed is not None and changed not in own:
                problems.append(
                    f"line {node.lineno}: changes {changed}, which outlives the run (state "
                    "kept between runs)"
                )
    recipe = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "recipe"), None
    )
    if recipe is None:
        problems.append("defines no recipe(fx)")
    return sorted(set(problems))


# --- one replay run, in a fresh interpreter (`replay_worker`)


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


class _Recorder:
    """Who touches the fixture's plan, asks and lane switch during a replay run. An access is
    the replay module's when, walking out from it, a frame of the module's file comes before
    any frame of the route's code (a helper the module calls reads on the module's behalf)."""

    def __init__(self, module_file: str, code: RouteCode) -> None:
        self.module_file = module_file
        self.code = code
        self.on = False
        self.paused = False
        self.reads: list[str] = []
        self.settings: list[str] = []

    def _module_made(self, frame: Any) -> bool:
        while frame is not None:
            code = frame.f_code
            if _real(code.co_filename) == self.module_file:
                return True
            if self.code.owns(code):
                return False
            frame = frame.f_back
        return False

    def read(self, what: str, frame: Any) -> None:
        if self.on and not self.paused and self._module_made(frame):
            self.reads.append(what)

    def setting(self, name: str, frame: Any) -> None:
        if self.on and not self.paused and self._module_made(frame):
            self.settings.append(name)

    def take(self) -> tuple[list[str], list[str]]:
        reads, settings = sorted(set(self.reads)), sorted(set(self.settings))
        self.reads, self.settings = [], []
        return reads, settings


def _recorded_mapping(data: dict, recorder: _Recorder, label: str, typed: Any = None) -> Any:
    """``data`` behind a mapping that notes every access, its values held in this closure.
    An attribute the mapping lacks reads off ``typed``, the real object the job runner hands
    over (a ``BriefRequirement``, the ``CreativeBrief``), else off the plain dict."""
    from collections.abc import MutableMapping

    real = data if typed is None else typed

    def note(what: str) -> None:
        recorder.read(f"{label}{what}", sys._getframe(2))

    class RecordedMapping(MutableMapping):
        __slots__ = ()

        def __getattribute__(self, name: str) -> Any:
            note(f".{name}")
            return object.__getattribute__(self, name)

        def __getattr__(self, name: str) -> Any:
            if name.startswith("__"):
                raise AttributeError(name)
            return getattr(real, name)

        def __getitem__(self, key: Any) -> Any:
            note(f"[{key!r}]")
            return data[key]

        def __setitem__(self, key: Any, value: Any) -> None:
            note(f"[{key!r}]=")
            data[key] = value

        def __delitem__(self, key: Any) -> None:
            note(f"del [{key!r}]")
            del data[key]

        def __iter__(self) -> Any:
            note(" iter")
            return iter(list(data))

        def __len__(self) -> int:
            note(" len")
            return len(data)

        def __contains__(self, key: object) -> bool:
            note(f" contains {key!r}")
            return key in data

        def __eq__(self, other: object) -> bool:
            note(" ==")
            return data == other

        def __ne__(self, other: object) -> bool:
            note(" !=")
            return data != other

        def __hash__(self) -> int:
            note(" hash")
            raise TypeError("the fixture's plan is not hashable")

        def __bool__(self) -> bool:
            note(" bool")
            return bool(data)

        def __repr__(self) -> str:
            note(" repr")
            return repr(data)

        def __format__(self, spec: str) -> str:
            note(" format")
            return format(repr(data), spec)

        def __copy__(self) -> Any:
            note(" copy")
            return _recorded_mapping(dict(data), recorder, label, copy.copy(typed))

        def __deepcopy__(self, memo: dict) -> Any:
            note(" deepcopy")
            return _recorded_mapping(
                copy.deepcopy(data, memo), recorder, label, copy.deepcopy(typed, memo)
            )

        def __reduce_ex__(self, protocol: Any) -> Any:
            note(" pickle")
            raise TypeError("the fixture's plan cannot be pickled")

        def __or__(self, other: Any) -> dict:
            note(" |")
            return {**data, **other}

        def __ror__(self, other: Any) -> dict:
            note(" |")
            return {**other, **data}

        def __dir__(self) -> list[str]:
            note(" dir")
            return []

        def copy(self) -> dict:
            note(".copy()")
            return dict(data)

    return RecordedMapping()


def _recorded_sequence(items: list, recorder: _Recorder, label: str) -> Any:
    """``items`` behind a sequence that notes every access; an attribute it lacks reads off
    the list itself (the job runner hands over ``brief.live()``, a list)."""
    from collections.abc import Sequence

    def note(what: str) -> None:
        recorder.read(f"{label}{what}", sys._getframe(2))

    class RecordedSequence(Sequence):
        __slots__ = ()

        def __getattribute__(self, name: str) -> Any:
            note(f".{name}")
            return object.__getattribute__(self, name)

        def __getattr__(self, name: str) -> Any:
            if name.startswith("__"):
                raise AttributeError(name)
            return getattr(items, name)

        def __getitem__(self, index: Any) -> Any:
            note(f"[{index!r}]")
            return items[index]

        def __iter__(self) -> Any:
            note(" iter")
            return iter(list(items))

        def __len__(self) -> int:
            note(" len")
            return len(items)

        def __contains__(self, value: object) -> bool:
            note(" contains")
            return any(value is item for item in items)

        def __eq__(self, other: object) -> bool:
            note(" ==")
            return items == other

        def __ne__(self, other: object) -> bool:
            note(" !=")
            return items != other

        def __hash__(self) -> int:
            note(" hash")
            raise TypeError("the fixture's asks are not hashable")

        def __bool__(self) -> bool:
            note(" bool")
            return bool(items)

        def __repr__(self) -> str:
            note(" repr")
            return repr(items)

        def __copy__(self) -> Any:
            note(" copy")
            return _recorded_sequence(list(items), recorder, label)

        def __deepcopy__(self, memo: dict) -> Any:
            note(" deepcopy")
            return _recorded_sequence(copy.deepcopy(items, memo), recorder, label)

        def __reduce_ex__(self, protocol: Any) -> Any:
            note(" pickle")
            raise TypeError("the fixture's asks cannot be pickled")

        def __add__(self, other: Any) -> list:
            note(" +")
            return [*items, *other]

        def __radd__(self, other: Any) -> list:
            note(" +")
            return [*other, *items]

        def __dir__(self) -> list[str]:
            note(" dir")
            return []

    return RecordedSequence()


_WHOLE_SETTINGS = frozenset(
    {
        "__dict__",
        "__getstate__",
        "__reduce__",
        "__reduce_ex__",
        "model_dump",
        "model_dump_json",
        "model_copy",
        "dict",
        "json",
        "copy",
    }
)


@contextlib.contextmanager
def _settings_guard(settings: Any, switch: str | None, recorder: _Recorder):  # noqa: ANN202
    """Note the module reading the lane switch or the whole settings object."""
    cls = type(settings)
    previous = cls.__dict__.get("__getattribute__")
    watched = {switch} | _WHOLE_SETTINGS if switch else set(_WHOLE_SETTINGS)

    def guarded(self: Any, name: str) -> Any:
        if name in watched:
            recorder.setting(name, sys._getframe(1))
        return object.__getattribute__(self, name)

    cls.__getattribute__ = guarded
    try:
        yield
    finally:
        if previous is None:
            del cls.__getattribute__
        else:
            cls.__getattribute__ = previous


def _module_callable(value: Any, module_file: str) -> bool:
    """``value`` is a function, method or partial defined in the replay module's file."""
    func = getattr(value, "func", value) if isinstance(value, functools.partial) else value
    func = getattr(func, "__func__", func)
    code = getattr(func, "__code__", None) if isinstance(func, types.FunctionType) else None
    return code is not None and _real(code.co_filename) == module_file


def _plan_in(frame: Any, wanted: dict[int, str], module_file: str | None = None) -> set[str]:
    """Which of the fixture's plan objects (by identity) a call received: its arguments, or
    up to two levels inside a list, tuple, dict or object among them; ``"callable"`` when it
    received a function of the replay module (``module_file``)."""
    found: set[str] = set()
    try:
        stack = [(v, 0) for v in list(frame.f_locals.values())]
    except Exception:  # noqa: BLE001 - an unreadable frame holds nothing
        return found
    while stack:
        value, depth = stack.pop()
        if id(value) in wanted:
            found.add(wanted[id(value)])
            continue
        if module_file is not None and _module_callable(value, module_file):
            found.add("callable")
            continue
        if depth >= 2:
            continue
        kind = type(value)
        if kind in (list, tuple, set, frozenset):
            stack += [(v, depth + 1) for v in value]
        elif kind is dict:
            stack += [(v, depth + 1) for v in value.values()]
        else:
            try:
                inner = object.__getattribute__(value, "__dict__")
            except Exception:  # noqa: BLE001 - no attributes to look in
                continue
            if isinstance(inner, dict):
                stack += [(v, depth + 1) for v in inner.values()]
    return found


def _replay_run(
    program: Any, module_file: str, run: dict, suite: dict, recorder: _Recorder
) -> dict:
    """One ``recipe(fx)`` call on a freshly executed module: the values at the table's paths
    in what the entry returned (taken the moment it returns), and who touched what."""
    module = types.ModuleType("route_replay")
    module.__file__ = module_file
    result: dict[str, Any] = {
        "values": {},
        "entry_plan_calls": 0,
        "entry_other_calls": 0,
        "route_calls": 0,
        "module_reads": [],
        "settings_reads": [],
        "handed": [],
        "error": None,
    }
    try:
        exec(program, module.__dict__)  # noqa: S102 - the lane's replay module, a fresh copy
    except Exception as exc:  # noqa: BLE001 - reported
        result["error"] = f"the replay module does not load: {type(exc).__name__}: {exc}"
        return result
    recipe = module.__dict__.get("recipe")
    from app.kria.brief import CreativeBrief

    raw = [dict(r) for r in run["brief"].get("requirements") or []]
    version = run["brief"].get("version")
    try:
        typed = CreativeBrief.model_validate(
            {"requirements": raw, **({} if version is None else {"version": version})}
        )
    except Exception as exc:  # noqa: BLE001 - reported
        result["error"] = f"the run's brief is no CreativeBrief: {exc}"
        return result
    strategy = _recorded_mapping(dict(run["strategy"]), recorder, "strategy")
    requirements = _recorded_sequence(
        [
            _recorded_mapping(r, recorder, f"requirements[{i}]", req)
            for i, (r, req) in enumerate(zip(raw, typed.requirements, strict=True))
        ],
        recorder,
        "requirements",
    )
    brief = _recorded_mapping(
        {"version": version, "requirements": requirements},
        recorder,
        "brief",
        CreativeBrief.model_construct(version=typed.version, requirements=requirements),
    )
    fx = {
        "route": run["route"],
        "strategy": strategy,
        "requirements": requirements,
        "brief": brief,
        "clips": copy.deepcopy(run["clips"]),
        "client_protocol": run.get("client_protocol"),
        "inputs": copy.deepcopy(run.get("inputs") or {}),
    }
    plan = {id(strategy): "strategy", id(requirements): "asks", id(brief): "asks"}
    entry = (_real(suite["entry"]["file"]), suite["entry"]["name"])
    paths = suite["paths"]
    opaque = (type(strategy), type(requirements), type(brief))
    state: dict[str, Any] = {"depth": 0, "frame": None}

    def profile(frame: Any, event: str, arg: Any) -> None:
        if event != "call" and event != "return":
            return
        code = frame.f_code
        is_entry = (_real(code.co_filename), code.co_qualname) == entry
        if event == "call":
            owned = recorder.code.owns(code)
            if owned:
                result["route_calls"] += 1
            if owned and not is_entry:
                back = frame.f_back
                if back is not None and _real(back.f_code.co_filename) == module_file:
                    recorder.paused = True
                    try:
                        handed = _plan_in(frame, plan, module_file)
                    finally:
                        recorder.paused = False
                    if handed:
                        result["handed"].append(f"{code.co_qualname}: {sorted(handed)}")
            if not is_entry:
                return
            if state["depth"] == 0:
                recorder.paused = True
                try:
                    given = _plan_in(frame, plan)
                finally:
                    recorder.paused = False
                ok = "strategy" in given and ("asks" in given or not suite["needs_asks"])
                result["entry_plan_calls" if ok else "entry_other_calls"] += 1
                state["frame"] = frame
            state["depth"] += 1
        elif event == "return" and is_entry:
            state["depth"] -= 1
            if state["depth"] == 0 and frame is state["frame"] and not result["values"]:
                recorder.paused = True
                try:
                    result["values"] = {k: path_value(arg, p, opaque) for k, p in paths.items()}
                finally:
                    recorder.paused = False

    if not callable(recipe):
        result["error"] = "the replay module defines no recipe(fx)"
        return result
    sys.setprofile(profile)
    recorder.on = True
    try:
        recipe(fx)
    except Exception as exc:  # noqa: BLE001 - reported
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        sys.setprofile(None)
        recorder.on = False
    result["module_reads"], result["settings_reads"] = recorder.take()
    return result


def _run_suite(suite: dict) -> dict:
    """Every run of one replay module with the lane switch at the suite's value, set before
    the module is first executed in this fresh interpreter."""
    from app.config import settings

    for extra in suite.get("sys_path") or ():
        if extra not in sys.path:
            sys.path.insert(0, extra)
    module_file = _real(suite["module"])
    code = RouteCode(
        frozenset(_real(f) for f in suite["route_files"]), frozenset(suite["route_functions"])
    )
    recorder = _Recorder(module_file, code)
    try:
        program = compile(Path(module_file).read_text(encoding="utf-8"), module_file, "exec")
    except Exception as exc:  # noqa: BLE001 - reported
        return {"setup_error": f"{type(exc).__name__}: {exc}", "runs": {}}
    switch = suite.get("switch")
    order = list(suite["runs"])
    random.SystemRandom().shuffle(order)
    with contextlib.ExitStack() as stack:
        if switch and hasattr(settings, switch):
            stack.enter_context(patch.object(settings, switch, bool(suite["switch_on"])))
        stack.enter_context(_settings_guard(settings, switch, recorder))
        runs = {run["id"]: _replay_run(program, module_file, run, suite, recorder) for run in order}
    return {"setup_error": None, "runs": runs}


def replay_worker() -> int:
    """``--replay-worker``: run the suites of one batch (one lane-switch value), read from
    stdin so no path or argument names them (stdin then points at the null device, so nothing
    can seek back to the job), and write the results where the job says. Started by
    `_workers`, always in a fresh interpreter."""
    job = json.loads(sys.stdin.read())
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    _BASE._bootstrap()
    with _prod_flags():
        out = [_run_suite(suite) for suite in job["suites"]]
    Path(job["out"]).write_text(json.dumps({"suites": out}, default=str), encoding="utf-8")
    return 0


def _workers(batches: list[list[dict]]) -> list[list[dict]]:
    """Each batch of suites in its own fresh interpreter, side by side. The job reaches the
    worker on stdin from a file with no name, so nothing the replay module can see (its
    arguments, a path) leads to the other runs."""
    import shutil
    import subprocess

    tmp = Path(tempfile.mkdtemp(prefix="route-replay-"))
    started = []
    try:
        for i, suites in enumerate(batches):
            out = tmp / f"out-{i}.json"
            with tempfile.TemporaryFile("w+", encoding="utf-8") as feed:
                json.dump({"suites": suites, "out": str(out)}, feed, default=str)
                feed.seek(0)
                proc = subprocess.Popen(  # noqa: S603 - this script, the same interpreter
                    [sys.executable, str(HERE / "score_route.py"), "--replay-worker"],
                    cwd=str(API),
                    stdin=feed,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            started.append((proc, out, suites))
        results = []
        for proc, out, suites in started:
            try:
                _, err = proc.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                proc.kill()
                err = "timed out after 900 s"
            if out.is_file():
                results.append(json.loads(out.read_text(encoding="utf-8"))["suites"])
            else:
                why = f"the replay worker failed: {(err or '')[-1500:]}"
                results.append([{"setup_error": why, "runs": {}} for _ in suites])
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- planning the runs and grading them


class ReplayPlan(NamedTuple):
    """The runs of one replay module over one fixture, with what grading them needs."""

    fx: dict
    targets: list[Target]
    keys: list[str]
    on: list[tuple[str, dict]]
    off: list[tuple[str, dict]]
    moves: dict[str, list[tuple[str, dict, Any]]]


def _seed(fx: dict) -> int:
    import hashlib

    digest = hashlib.sha256(str(fx.get("fixture_id")).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def plan_runs(fx: dict, targets: list[Target]) -> ReplayPlan:
    """Switch on: the fixture, nothing asked, each key moved to every other value, dropped,
    field only (its typed asks dropped) and ask only (its field dropped). Switch off: the
    fixture, each move and each drop. Each list shuffled, the same way for the same fixture."""
    keys = sorted({t.key for t in targets if t.key in RECIPE_KEYS})
    on = [("real", fx), ("stripped", _drop_keys(fx, keys))]
    off = [("off", fx)]
    moves: dict[str, list[tuple[str, dict, Any]]] = {}
    for key in keys:
        first = asked(fx, next(t for t in targets if t.key == key))[0]
        moves[key] = []
        if first is not None:
            for i, (field_value, fact_value) in enumerate(perturbations(fx, key, first)):
                moved = _set_key(fx, key, field_value, fact_value)
                moves[key].append((f"moved:{key}:{i}", moved, fact_value))
                on.append((f"moved:{key}:{i}", moved))
                off.append((f"off:moved:{key}:{i}", moved))
        on += [
            (f"dropped:{key}", _drop_keys(fx, [key])),
            (f"field_only:{key}", _drop_asks(fx, key)),
            (f"ask_only:{key}", _drop_field(fx, key)),
        ]
        off.append((f"off:dropped:{key}", _drop_keys(fx, [key])))
    rng = random.Random(_seed(fx))
    rng.shuffle(on)
    rng.shuffle(off)
    return ReplayPlan(fx, targets, keys, on, off, moves)


def _payload(run_id: str, fx: dict) -> dict:
    return {
        "id": run_id,
        "route": fx.get("route"),
        "strategy": fx["strategy"],
        "brief": {
            "version": fx["brief"].get("version"),
            "requirements": fx["brief"]["requirements"],
        },
        "clips": fx.get("clips") or [],
        "client_protocol": fx.get("client_protocol"),
        "inputs": fx.get("replay_inputs") or {},
    }


def suites_for(plan: ReplayPlan, base: dict) -> tuple[dict, dict]:
    """The switch-on and switch-off suites for `_workers` (``base``: module, entry, paths,
    route code, switch)."""
    needs_asks = any(t.kind == "asks" for t in plan.targets)
    common = {**base, "needs_asks": needs_asks}
    return (
        {**common, "switch_on": True, "runs": [_payload(i, f) for i, f in plan.on]},
        {**common, "switch_on": False, "runs": [_payload(i, f) for i, f in plan.off]},
    )


def _medians(values: list[Any]) -> float | None:
    numbers = _numbers(values if isinstance(values, list) else [values]) if values else []
    if not numbers:
        return None
    numbers.sort()
    return numbers[len(numbers) // 2]


def grade_runs(
    plan: ReplayPlan, on: dict, off: dict, *, entry: str, switch: str | None, lint: list[str]
) -> dict:
    """The replay's claims and the grader's controls on them. ``honored[check_id]`` is the
    grader's own comparison of the real run's entry return; ``valid`` is False when a control
    fails (the scorecard is then INVALID)."""
    fx, targets = plan.fx, plan.targets
    runs = {**on.get("runs", {}), **off.get("runs", {})}
    setup = [s["setup_error"] for s in (on, off) if s.get("setup_error")]
    empty = {"values": {}, "error": "the run did not happen"}
    real = runs.get("real", empty)
    recipe = real.get("values") or {}
    wanted = {t.check_id: asked(fx, t) for t in targets}
    honored = {t.check_id: honors(t.key, recipe.get(t.key), *wanted[t.check_id]) for t in targets}
    claimed = sorted({t.key for t in targets if honored[t.check_id]})
    every = [(run_id, runs.get(run_id, empty)) for run_id, _ in [*plan.on, *plan.off]]
    entry_bad = [
        f"{run_id}: {r.get('entry_plan_calls', 0)} with the plan, {r.get('entry_other_calls', 0)} "
        "without"
        for run_id, r in every
        if r.get("entry_plan_calls") != 1 or r.get("entry_other_calls")
    ]
    reads = sorted({f"{run_id}: {w}" for run_id, r in every for w in r.get("module_reads") or []})
    sreads = sorted(
        {f"{run_id}: {w}" for run_id, r in every for w in r.get("settings_reads") or []}
    )
    handed = sorted({f"{run_id}: {w}" for run_id, r in every for w in r.get("handed") or []})
    errors = [*setup, *[f"{run_id}: {r['error']}" for run_id, r in every if r.get("error")]]
    rows = [
        _row(
            "replay: a module that breaks the replay rules (settings, introspection, state "
            "between runs, imports outside app/ and plain stdlib) must FAIL",
            False,
            bool(lint),
            lint[:8],
        ),
        _row(
            f"replay: a run that does not call the render entry {entry} exactly once with the "
            "fixture's own plan and asks must FAIL",
            False,
            bool(entry_bad),
            entry_bad[:6],
        ),
        _row(
            "replay: the module reading the fixture's plan or asks itself (only the route's "
            "code may) must FAIL",
            False,
            bool(reads),
            reads[:8],
        ),
        _row(
            f"replay: the module reading {switch or 'the lane switch'} or the settings (only "
            "the route's code may) must FAIL",
            False,
            bool(sreads),
            sreads[:8],
        ),
        _row(
            "replay: the module handing the plan, the asks or its own function to route code "
            "other than the render entry must FAIL",
            False,
            bool(handed),
            handed[:6],
        ),
        _row("replay: a run that raised must FAIL", False, bool(errors), errors[:4]),
    ]

    def value(run_id: str, key: str) -> Any:
        return (runs.get(run_id, empty).get("values") or {}).get(key)

    def met(run_id: str, run_fx: dict, mine: list[Target]) -> bool:
        return all(honors(t.key, value(run_id, t.key), *asked(run_fx, t)) for t in mine)

    def kept(run_id: str, other: Target, moved_key: str) -> bool:
        """Another claimed value through a move of ``moved_key``: unchanged, except that a
        moved length may re-time the clips and a length is met within its own window."""
        if moved_key == "length_s" or other.key == "length_s":
            return honors(other.key, value(run_id, other.key), *wanted[other.check_id])
        return _same(value(run_id, other.key), recipe.get(other.key))

    for key in claimed:
        mine = [t for t in targets if t.key == key and honored[t.check_id]]
        others = [t for t in targets if t.key != key and t.key in claimed and honored[t.check_id]]
        moved = plan.moves.get(key) or []
        unfollowed = {
            str(asked_value): value(run_id, key)
            for run_id, run_fx, asked_value in moved
            if not met(run_id, run_fx, mine)
        }
        leaked = [
            run_id
            for run_id in [*(m[0] for m in moved), f"dropped:{key}"]
            if not all(kept(run_id, t, key) for t in others)
        ]
        fields = [t for t in mine if t.kind == "fields"]
        ask_targets = [t for t in mine if t.kind == "asks"]
        field_only = not fields or met(f"field_only:{key}", _drop_asks(fx, key), fields)
        ask_only = not ask_targets or met(f"ask_only:{key}", _drop_field(fx, key), ask_targets)
        switched = [f"off:moved:{key}:{i}" for i in range(len(moved))] + [f"off:dropped:{key}"]
        moving = {r: value(r, key) for r in switched if not _same(value(r, key), value("off", key))}
        numeric = bool(_numbers(recipe[key] if isinstance(recipe[key], list) else [recipe[key]]))
        rows += [
            _row(
                f"replay {key}: a recipe that does not follow every other value asked "
                f"({len(moved)} moves) must FAIL",
                False,
                bool(unfollowed) or not moved,
                _json(unfollowed or ("no other value to move to" if not moved else {})),
            ),
            _row(
                f"replay {key}: moving or dropping it changing another claimed value must FAIL",
                False,
                bool(leaked),
                leaked[:6],
            ),
            _row(
                f"replay {key}: the plan's field alone (typed ask dropped) not met must FAIL",
                False,
                not field_only,
                _json(value(f"field_only:{key}", key)),
            ),
            _row(
                f"replay {key}: the typed ask alone (plan field dropped) not met must FAIL",
                False,
                not ask_only,
                _json(value(f"ask_only:{key}", key)),
            ),
            _row(
                f"replay {key}: an ask that still changes the recipe with {switch} off must FAIL",
                False,
                bool(moving),
                _json({"off": value("off", key), "moved": moving}),
            ),
        ]
        if numeric and key == "pace":
            order = {str(a).casefold(): _medians(value(r, key)) for r, _f, a in moved}
            order[str(wanted[mine[0].check_id][0]).casefold()] = _medians(recipe[key])
            ranked = [order[p] for p in _PACE_ORDER if order.get(p) is not None]
            rising = all(a < b for a, b in itertools.pairwise(ranked))
            rows.append(
                _row(
                    "replay pace: clip holds that do not grow from fast to balanced to relaxed "
                    "must FAIL",
                    False,
                    not rising,
                    _json(order),
                )
            )
        elif not numeric:
            rows.append(
                _row(
                    f"replay {key}: with nothing asked for it, a value other than the one with "
                    "nothing asked at all must FAIL",
                    False,
                    not _same(value(f"dropped:{key}", key), value("stripped", key)),
                    _json(
                        {"dropped": value(f"dropped:{key}", key), "none": value("stripped", key)}
                    ),
                )
            )
    return {
        "recipe": _json(recipe),
        "asked": _json(wanted),
        "honored": honored,
        "controls": rows,
        "valid": all(r["ok"] is True for r in rows),
        "route_code_calls": real.get("route_calls", 0),
        "runs": len(every),
    }


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _shown_path(path: Path) -> str:
    return str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)


def render_entry(route: str | None) -> Any:
    """The route table's ``RENDER_ENTRY``, or None (no registry, no table, or none named)."""
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None and route else None
    return getattr(table, "RENDER_ENTRY", None) if table is not None else None


def route_replay(fx: dict, route: str) -> dict[str, Any]:
    """Render evidence from ``route_replays/<route>.py`` under the contract above (nothing
    without one): graded on what the route's render entry returns, never on what the module
    returns."""
    script = REPLAYS / f"{route}.py"
    if not script.is_file():
        return {"source": None, "honored": {}, "controls": [], "valid": True, "recipe": None}
    targets = honored_targets(fx)
    entry = render_entry(route)
    source = _shown_path(script)
    if entry is None:
        row = _row("replay: a route table that names no RENDER_ENTRY must FAIL", False, True)
        return {"source": source, "honored": {}, "controls": [row], "valid": False, "recipe": None}
    path, _, symbol = entry.entry.partition(":")
    code = route_code(route)
    base = {
        "module": str(script),
        "entry": {"file": str(APP / path), "name": symbol},
        "paths": {k: v for k, v in entry.recipe.items() if k in RECIPE_KEYS},
        "route_files": sorted(code.files),
        "route_functions": sorted(code.functions),
        "switch": LANE_SWITCH.get(route),
    }
    plan = plan_runs(fx, targets)
    on, off = suites_for(plan, base)
    (on_result,), (off_result,) = _workers([[on], [off]])
    lint = lint_replay_module(script.read_text(encoding="utf-8"))
    graded = grade_runs(
        plan, on_result, off_result, entry=entry.entry, switch=base["switch"], lint=lint
    )
    return {**graded, "source": source, "entry": entry.entry, "paths": base["paths"]}


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
    receipt: dict | None,
    honored: bool = False,
    card: dict | None = None,
    field: str | None = None,
    *,
    decline_counts: bool = True,
) -> dict[str, Any]:
    """One language's card. ``field``: the strategy field whose own card line answers this ask
    (a length ask and the route's ``target_duration_s`` line). ``decline_counts`` False: the
    receipt's reason is a route decline about another ask (`check_ask`)."""
    receipt = receipt or {}
    checked = receipt.get("verification") == "checked"
    met = checked and receipt.get("status") == "met"
    declined = (
        checked
        and receipt.get("status") == "not_possible"
        and bool(str(receipt.get("reason") or "").strip())
        and decline_counts
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


# --- carriers (AST): a function that reads the field off its strategy parameter into what it
# returns, passes on or writes

_STRATEGY_NAME = re.compile(r"\w*strategy\w*")
_STRATEGY_CALL = re.compile(r"strategy", re.I)
# Methods that only log or trace: a value handed to one reaches no render.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_LOG_FUNCTIONS = frozenset({"print", "record_pipeline_event", "trace", "_trace", "_log"})


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _params(fn: ast.AST) -> set[str]:
    a = fn.args  # type: ignore[attr-defined]
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    return names | {p.arg for p in (a.vararg, a.kwarg) if p is not None}


def _assignments(fn: ast.AST) -> list[tuple[list[ast.AST], ast.AST]]:
    """``(targets, value)`` for every binding in ``fn``: assignments, loops, ``with ... as``."""
    out: list[tuple[list[ast.AST], ast.AST]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            out.append((list(node.targets), node.value))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
            out.append(([node.target], node.value))
        elif isinstance(node, ast.NamedExpr):
            out.append(([node.target], node.value))
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            out.append(([node.target], node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            out.append(([node.optional_vars], node.context_expr))
    return out


def _names_in(target: ast.AST) -> set[str]:
    """The names a binding target sets: plain names, or the root of ``x[...]`` / ``x.a``."""
    if isinstance(target, ast.Attribute | ast.Subscript):
        root = _root(target)
        return {root} if root else set()
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _from_params(fn: ast.AST) -> set[str]:
    """Parameters and every local computed from one (``job = load(job_id)``)."""
    rooted = _params(fn)
    bindings = [
        ({n.id for n in ast.walk(value) if isinstance(n, ast.Name)}, targets)
        for targets, value in _assignments(fn)
    ]
    for _ in range(6):
        before = len(rooted)
        for names, targets in bindings:
            if names & rooted:
                for target in targets:
                    rooted |= _names_in(target)
        if len(rooted) == before:
            break
    return rooted


def _is_strategy(node: ast.AST, aliases: set[str], rooted: set[str]) -> bool:
    """``node`` is the function's strategy: a strategy-named parameter (or a local computed
    from a parameter), an alias of one, ``p.strategy``, ``(strategy or {})``,
    ``p.get("creator_strategy")`` or ``p["creator_strategy"]`` off a parameter. A local
    built from literals (``caption_strategy = {...}``) is no strategy whatever its name."""
    if isinstance(node, ast.Name):
        return node.id in aliases or (bool(_STRATEGY_NAME.fullmatch(node.id)) and node.id in rooted)
    if isinstance(node, ast.Attribute):
        return bool(_STRATEGY_NAME.fullmatch(node.attr)) and _root(node) in rooted
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _is_strategy(node.values[0], aliases, rooted)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        key = _const_str(node.args[0]) if node.func.attr == "get" and node.args else None
        return bool(key and _STRATEGY_NAME.fullmatch(key)) and _root(node) in rooted
    if isinstance(node, ast.Subscript):
        key = _const_str(node.slice)
        return bool(key and _STRATEGY_NAME.fullmatch(key)) and _root(node) in rooted
    return False


def _aliases(fn: ast.AST, rooted: set[str]) -> set[str]:
    """Names bound to the strategy in ``fn``: ``s = strategy or {}``, ``typed =
    _strategy(strategy)``, ``raw = dict(strategy)``."""
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
                and any(_is_strategy(arg, found, rooted) for arg in value.args)
            )
            if _is_strategy(value, found, rooted) or converted:
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


def _is_logging(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in _LOG_METHODS
    return isinstance(func, ast.Name) and func.id in _LOG_FUNCTIONS


def _flows(fn: ast.AST, sources: list[ast.AST]) -> bool:
    """A value read at ``sources`` reaches what ``fn`` returns or yields, a call it makes for
    its effect (never a log or trace), an attribute it sets, or an item of a parameter it
    sets, directly or through locals computed from it, or decides a branch that returns or
    raises. Best effort: a value stored on a local nothing uses afterwards does not flow."""
    ids = {id(n) for n in sources}
    params = _params(fn)
    tainted: set[str] = set()

    def touched(expr: ast.AST | None) -> bool:
        return expr is not None and any(
            id(n) in ids
            or (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in tainted)
            for n in ast.walk(expr)
        )

    def call_args(call: ast.Call) -> list[ast.AST]:
        return [*call.args, *(k.value for k in call.keywords)]

    nodes = list(ast.walk(fn))
    bindings = _assignments(fn)
    mutators = [
        (node, root)
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
        and (root := _root(node.func.value))
    ]
    for _ in range(6):
        before = len(tainted)
        for targets, value in bindings:
            if touched(value):
                for target in targets:
                    tainted |= _names_in(target)
        for node, root in mutators:
            if any(touched(a) for a in call_args(node)):
                tainted.add(root)
        if len(tainted) == before:
            break
    for node in nodes:
        if isinstance(node, ast.Return | ast.Yield | ast.YieldFrom) and touched(node.value):
            return True
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign) and touched(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) or (
                    isinstance(target, ast.Subscript) and _root(target) in params
                ):
                    return True
        if isinstance(node, ast.Expr):
            call = node.value.value if isinstance(node.value, ast.Await) else node.value
            if (
                isinstance(call, ast.Call)
                and not _is_logging(call)
                and any(touched(a) for a in call_args(call))
            ):
                return True
        if (
            isinstance(node, ast.If | ast.While)
            and touched(node.test)
            and any(isinstance(n, ast.Return | ast.Raise) for n in ast.walk(node))
        ):
            return True
    return False


def _helper(func: ast.AST, tree: ast.Module, path: str | None) -> ast.AST | None:
    """The definition a helper call resolves to: a top-level function of this file, or one
    imported from ``app`` (``from app.x import name``)."""
    if not isinstance(func, ast.Name):
        return None
    own = _definition(tree, func.id)
    if isinstance(own, ast.FunctionDef | ast.AsyncFunctionDef):
        return own
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith("app."):
            continue
        for alias in node.names:
            if (alias.asname or alias.name) == func.id:
                source = APP / (node.module.removeprefix("app.").replace(".", "/") + ".py")
                if source.is_file():
                    found = _definition(ast.parse(source.read_text()), alias.name)
                    if isinstance(found, ast.FunctionDef | ast.AsyncFunctionDef):
                        return found
    return None


def _helper_reads(call: ast.Call, helper: ast.AST) -> bool:
    """``helper(strategy, "field")`` reads its key argument off its strategy argument into
    what it returns (``mapping[key]``, ``mapping.get(key)``, ``getattr(mapping, key)``): a
    helper that drops or only compares the key is no read."""
    a = helper.args  # type: ignore[attr-defined]
    positional = [p.arg for p in (*a.posonlyargs, *a.args)]
    bound = {
        **{positional[i]: arg for i, arg in enumerate(call.args) if i < len(positional)},
        **{k.arg: k.value for k in call.keywords if k.arg},
    }
    keys = {name for name, arg in bound.items() if _const_str(arg) is not None}
    objects = {name for name in bound if name not in keys}
    reads = []
    for node in ast.walk(helper):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            pair = (_root(node.value), node.slice)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            pair = (_root(node.func.value), node.args[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
            pair = (_root(node.args[0]), node.args[1]) if len(node.args) > 1 else (None, None)
        else:
            continue
        receiver, key = pair
        if receiver in objects and isinstance(key, ast.Name) and key.id in keys:
            reads.append(node)
    return bool(reads) and _flows(helper, reads)


def strategy_reads(
    fn: ast.AST, field: str, tree: ast.Module | None = None, path: str | None = None
) -> list[ast.AST]:
    """Where ``fn`` reads ``field`` off its strategy parameter (`_is_strategy`): attribute,
    item or ``.get`` reads, ``getattr``, or a helper of this file or of ``app`` that reads the
    key it is given (`_helper_reads`). A delete or a write is no read."""
    rooted = _from_params(fn)
    aliases = _aliases(fn, rooted)
    loops = _loop_keys(fn)

    def key(node: ast.AST) -> bool:
        if _const_str(node) == field:
            return True
        return isinstance(node, ast.Name) and field in loops.get(node.id, ())

    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr == field:
            if isinstance(node.ctx, ast.Load) and _is_strategy(node.value, aliases, rooted):
                found.append(node)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if _is_strategy(node.value, aliases, rooted) and key(node.slice):
                found.append(node)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                if _is_strategy(func.value, aliases, rooted) and node.args and key(node.args[0]):
                    found.append(node)
                    continue
            if not (
                any(_is_strategy(a, aliases, rooted) for a in node.args)
                and any(key(a) for a in node.args)
            ):
                continue
            if isinstance(func, ast.Name) and func.id == "getattr":
                found.append(node)
            elif tree is not None and (helper := _helper(func, tree, path)) is not None:
                if _helper_reads(node, helper):
                    found.append(node)
    return found


def function_reads(
    fn: ast.AST, field: str, tree: ast.Module | None = None, path: str | None = None
) -> bool:
    """``fn`` reads ``field`` off its strategy parameter and that value reaches what it
    returns, passes on or writes (`strategy_reads`, `_flows`)."""
    reads = strategy_reads(fn, field, tree, path)
    return bool(reads) and _flows(fn, reads)


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
    ``SHARED_CARRIERS`` that reads the field off its strategy parameter into what it returns,
    passes on or writes (`function_reads`). ``readers`` and ``texts`` (controls only) stand in
    for the route's readers and the files' source."""
    if readers is None and texts is None:
        return _via_problem_cached(via, field, route)
    return _via_problem(via, field, route, readers=readers, texts=texts)


@functools.cache
def _via_problem_cached(via: str, field: str, route: str | None) -> str | None:
    return _via_problem(via, field, route, readers=None, texts=None)


def _via_problem(
    via: str, field: str, route: str | None, *, readers: Any, texts: Mapping[str, str] | None
) -> str | None:
    path, _, symbol = via.partition(":")
    if not path.endswith(".py") or not all(p.isidentifier() for p in symbol.split(".")):
        return f"{via!r} is not <path under app/>:<function>"
    text = (texts or {}).get(path)
    if text is None and not (APP / path).is_file():
        return f"{path} does not exist"
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
    tree = ast.parse(text) if text is not None else _parsed(path)
    node = _definition(tree, symbol)
    if node is None:
        return f"{symbol} is not defined in {path}"
    if isinstance(node, ast.ClassDef):
        return f"{via} is a class: a schema names the field, it reads nothing"
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return f"{via} is not a function"
    if not strategy_reads(node, field, tree, path):
        return f"{via} never reads {field} off its strategy parameter"
    if not function_reads(node, field, tree, path):
        return f"{via} reads {field} but nothing it returns, passes on or writes uses it"
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


@functools.cache
def _parsed(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text())


def _readers_in(rel: str, tree: ast.Module, field: str, only: frozenset[str] | None) -> list[str]:
    """``rel:function`` for each function of ``tree`` (top-level ones named in ``only``, when
    given) that reads ``field`` off its strategy parameter (`function_reads`)."""
    functions = (
        [fn for fn in tree.body if getattr(fn, "name", None) in only]
        if only is not None
        else list(ast.walk(tree))
    )
    out = []
    for fn in functions:
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        named = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        named |= {s for n in ast.walk(fn) if (s := _const_str(n))}
        if field in named and function_reads(fn, field, tree, rel):
            out.append(f"{rel}:{fn.name}")
    return out


@functools.cache
def _file_readers(rel: str, field: str, only: frozenset[str] | None) -> tuple[str, ...]:
    return tuple(_readers_in(rel, _parsed(rel), field, only))


def strict_reads(
    fields: list[str], readers: Any, sources: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    """Functions of the route's code (``READERS`` files and builder functions) that read each
    field off their strategy parameter into what they return, pass on or write
    (`function_reads`). ``sources`` (controls only) replaces the route's code."""
    out: dict[str, list[str]] = {f: [] for f in fields}
    for field in fields:
        if sources is not None:
            for rel, lines in sources.items():
                try:
                    tree = ast.parse("\n".join(lines))
                except SyntaxError:
                    continue
                out[field] += _readers_in(rel, tree, field, None)
            continue
        for rel in readers.files:
            out[field] += _file_readers(rel, field, None)
        if readers.functions:
            out[field] += _file_readers(BUILDER, field, frozenset(readers.functions))
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

    ``claimed_applied_unread``: marked ``Applied`` with a ``via`` that is no carrier
    (``bad_carriers`` says why), or with none and no function of the route's code that reads
    it off its strategy parameter into what it returns, passes on or writes (`strict_reads`).
    ``unconsumed`` (report only, the line scan `route_reads`): no reader and no carrier;
    ``undisclosed``: of those, what the card does not disclose (`field_disclosed`).
    ``shared_readers``: reader files another route lists too. ``registry_drift`` is report-only
    detail."""
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
    plain = [f for f, h in handling.items() if isinstance(h, Applied) and not h.via]
    readers = strict_reads(plain, table.READERS, sources)
    claimed = sorted(
        f
        for f, h in handling.items()
        if isinstance(h, Applied) and (f in bad if h.via else not readers[f])
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
        "readers": readers,
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


def _calls(fn: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
        for n in ast.walk(fn)
    )


def entry_problems(route: str, entry: Any = None) -> list[str]:
    """Why the route's ``RENDER_ENTRY`` is no render entry (empty when it is): a function in
    the route's own ``READERS`` that another function of the route's code calls (the job
    runner renders from it), with a recipe path per known key that parses. ``entry``
    (controls only) stands in for the table's."""
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None else None
    if table is None:
        return [f"no registry table for {route!r}"]
    entry = entry if entry is not None else getattr(table, "RENDER_ENTRY", None)
    if entry is None:
        return ["the route's table names no RENDER_ENTRY"]
    path, _, symbol = entry.entry.partition(":")
    if not path.endswith(".py") or not all(p.isidentifier() for p in symbol.split(".")):
        return [f"{entry.entry!r} is not <path under app/>:<function>"]
    readers = table.READERS
    if not (path in readers.files or (path == BUILDER and symbol in readers.functions)):
        return [f"{entry.entry} is not in the route's READERS (route code)"]
    if not (APP / path).is_file():
        return [f"{path} does not exist"]
    node = _definition(ast.parse((APP / path).read_text()), symbol)
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return [f"{entry.entry} is not a function"]
    name = symbol.rsplit(".", 1)[-1]
    callers = []
    for rel in readers.files:
        tree = ast.parse((APP / rel).read_text())
        callers += [
            f"{rel}:{fn.name}"
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and fn is not node
            and not (rel == path and fn.name == name)
            and _calls(fn, name)
        ]
    builder = ast.parse((APP / BUILDER).read_text())
    callers += [
        f"{BUILDER}:{fn.name}"
        for fn in builder.body
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        and fn.name in readers.functions
        and fn.name != name
        and _calls(fn, name)
    ]
    problems = [] if callers else [f"no function of the route's code calls {symbol}"]
    for key, recipe_path in dict(entry.recipe).items():
        if key not in RECIPE_KEYS:
            problems.append(f"recipe key {key!r} is none of {sorted(RECIPE_KEYS)}")
        elif parse_path(recipe_path) is None:
            problems.append(f"recipe path {recipe_path!r} for {key} does not parse")
    return problems


def entry_result(route: str) -> dict:
    """Scored: the route's table names its render entry (`entry_problems`)."""
    name = "render entry: the table names the function the route's job runner renders from"
    if _registry() is None:
        return _kind(_no_registry("10.entry", name), "gate")
    problems = entry_problems(route)
    entry = render_entry(route)
    evidence = {
        "entry": getattr(entry, "entry", None),
        "recipe": dict(getattr(entry, "recipe", {}) or {}),
    }
    return _kind(_result("10.entry", name, not problems, problems, evidence), "gate")


# ------------------------------------------------------------------- render entry pins


def render_entry_pins() -> dict[str, Any]:
    """``route -> {"runner", "entry", "recipe"}`` from ``RENDER_ENTRIES`` (empty without it)."""
    if not RENDER_ENTRIES.is_file():
        return {}
    return json.loads(RENDER_ENTRIES.read_text(encoding="utf-8"))["routes"]


def signature(fn: ast.AST) -> list[str]:
    """A function's parameters in order: each name, ``/`` after the positional-only ones,
    ``*`` (or ``*name``) before the keyword-only ones, ``**name`` last."""
    a = fn.args  # type: ignore[attr-defined]
    out = [p.arg for p in a.posonlyargs] + (["/"] if a.posonlyargs else [])
    out += [p.arg for p in a.args]
    if a.vararg is not None:
        out.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        out.append("*")
    out += [p.arg for p in a.kwonlyargs]
    return out + ([f"**{a.kwarg.arg}"] if a.kwarg is not None else [])


def _computed_from(fn: ast.AST) -> Any:
    """Whether an expression uses a parameter of ``fn`` or a local computed from one."""
    rooted = _from_params(fn)
    return lambda expr: any(
        isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in rooted
        for n in ast.walk(expr)
    )


def _spread_sets(fn: ast.AST, spread: ast.AST, key: str, computed: Any) -> bool:
    """``**spread`` carries ``key`` with a computed value: a ``{...}`` or ``dict(...)`` that
    holds it, or a local that one builds, whose ``[key]`` is assigned or that ``.update``
    gives it."""

    def holds(node: ast.AST) -> bool:
        if isinstance(node, ast.Dict):
            return any(
                k is not None and _const_str(k) == key and computed(v)
                for k, v in zip(node.keys, node.values, strict=True)
            )
        return (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "dict"
            and any(k.arg == key and computed(k.value) for k in node.keywords)
        )

    if holds(spread):
        return True
    if not isinstance(spread, ast.Name):
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name) and target.id == spread.id and holds(node.value):
                    return True
                if (
                    isinstance(target, ast.Subscript)
                    and getattr(target.value, "id", None) == spread.id
                    and _const_str(target.slice) == key
                    and computed(node.value)
                ):
                    return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and getattr(node.func.value, "id", None) == spread.id
            and (
                any(holds(a) for a in node.args)
                or any(k.arg == key and computed(k.value) for k in node.keywords)
            )
        ):
            return True
    return False


def hands_plan(fn: ast.AST, call: ast.Call, plan: Sequence[str], positional: Sequence[str]) -> bool:
    """``call`` gives each of the entry's ``plan`` parameters a value computed from ``fn``'s
    own parameters (the job, its snapshot, its strategy), by keyword, by position or through a
    ``**mapping`` that sets that key. A literal, a constant, a local built from neither or a
    parameter left out does not count (AST, best effort)."""
    computed = _computed_from(fn)
    given: dict[str, ast.AST] = {}
    for name, arg in zip(positional, call.args, strict=False):
        if isinstance(arg, ast.Starred):
            break
        given[name] = arg
    given |= {k.arg: k.value for k in call.keywords if k.arg}
    spreads = [k.value for k in call.keywords if k.arg is None]
    return all(
        computed(given[p]) if p in given else any(_spread_sets(fn, s, p, computed) for s in spreads)
        for p in plan
    )


def calls_entry(
    caller: str,
    target: str,
    *,
    used: bool,
    texts: Mapping[str, str] | None = None,
    plan: Sequence[str] = (),
    positional: Sequence[str] = (),
) -> bool:
    """A function of ``caller`` (``"<path>:<function>"``, or ``"<path>"`` for any function of
    that file but the target) calls ``target`` (``"<path>:<function>"``) by a name bound to it:
    imported from the target's module (in the function or at the top of its file) or defined
    at the top of that same file. ``used``: the call's value must reach what the function
    returns, a call it makes, an attribute it sets or a branch (`_flows`); a value thrown away
    or parked on a local nothing reads does not count. ``plan``: that call must also hand the
    target's plan parameters the job's own plan (`hands_plan`; ``positional`` names the
    target's positional parameters). ``texts`` (controls only) stands in for files under
    ``app/``."""
    path, _, function = caller.partition(":")
    target_path, _, symbol = target.partition(":")
    name = symbol.rsplit(".", 1)[-1]
    module = "app." + target_path.removesuffix(".py").replace("/", ".")
    if texts is not None and path in texts:
        tree = ast.parse(texts[path])
    elif (APP / path).is_file():
        tree = _parsed(path)
    else:
        return False

    def imported(nodes: Any) -> set[str]:
        return {
            alias.asname or alias.name
            for node in nodes
            if isinstance(node, ast.ImportFrom) and node.module == module
            for alias in node.names
            if alias.name == name
        }

    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    top = imported(tree.body)
    if path == target_path and any(fn.name == name for fn in functions):
        top.add(name)
    callers = [fn for fn in functions if (fn.name == function if function else fn.name != name)]
    for fn in callers:
        nodes = list(ast.walk(fn))
        local = imported(nodes)
        calls = [
            n
            for n in nodes
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in (top | local)
        ]
        if not calls:
            continue
        names = (top - _bound_names(fn)) | local
        bound = [n for n in calls if n.func.id in names and hands_plan(fn, n, plan, positional)]
        if bound and (not used or _flows(fn, bound)):
            return True
    return False


def _entry_parameters(entry: str, texts: Mapping[str, str] | None) -> list[str] | None:
    """The parameters (`signature`) of ``"<path>:<function>"`` under ``app/``, or None."""
    path, _, symbol = entry.partition(":")
    if texts is not None and path in texts:
        tree = ast.parse(texts[path])
    elif (APP / path).is_file():
        tree = _parsed(path)
    else:
        return None
    node = _definition(tree, symbol)
    return signature(node) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else None


def parameter_problems(params: list[str] | None, pinned: Any) -> list[str]:
    """Why an entry's parameters are not its pin's: every pinned one, in order, and nothing
    else but the plan parameters (``PLAN_PARAMS``, or a bare ``*`` before them) a lane adds to
    pass the plan in, so no override parameter can carry an asked value past the plan."""
    if params is None:
        return ["the pinned entry is no function of app/"]
    if not isinstance(pinned, list):
        return ["the pin lists no params for its entry"]
    kept = [p for p in params if p in pinned]
    added = [p for p in params if p not in pinned and p not in (*PLAN_PARAMS, "*")]
    if kept == pinned and not added:
        return []
    return [
        f"the entry's parameters {params} are not the pinned {pinned}: a lane adds only "
        f"{list(PLAN_PARAMS)} on its own, any other change is a pin edit review reads"
    ]


def pin_problems(
    route: str,
    entry: Any = None,
    pins: Mapping[str, Any] | None = None,
    texts: Mapping[str, str] | None = None,
) -> list[str]:
    """Why the route's ``RENDER_ENTRY`` is not its pin in ``RENDER_ENTRIES`` (empty when it
    is): the same entry, recipe paths and parameters (`parameter_problems`), a pinned runner
    that imports the entry, calls it with the job's plan in each of its plan parameters and
    uses its value (`calls_entry`), and a worker module (``BUILDER``) that calls the runner.
    ``entry`` / ``pins`` / ``texts`` (controls only) stand in for the table's, the file's and
    the code's."""
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None else None
    if table is None:
        return [f"no registry table for {route!r}"]
    entry = entry if entry is not None else getattr(table, "RENDER_ENTRY", None)
    pin = (render_entry_pins() if pins is None else pins).get(route)
    if pin is None:
        return [f"{_shown_path(RENDER_ENTRIES)} pins no render entry for {route}"]
    problems = []
    named = getattr(entry, "entry", None)
    if named != pin["entry"]:
        problems.append(f"the table's entry {named!r} is not the pinned {pin['entry']!r}")
    recipe = dict(getattr(entry, "recipe", None) or {})
    if recipe != dict(pin["recipe"]):
        problems.append(f"the table's recipe paths {recipe} are not the pinned {pin['recipe']}")
    params = _entry_parameters(pin["entry"], texts)
    problems += parameter_problems(params, pin.get("params"))
    plan = [p for p in params or () if p in PLAN_PARAMS]
    positional = list(itertools.takewhile(lambda p: not p.startswith("*"), params or ()))
    positional = [p for p in positional if p != "/"]
    if not calls_entry(
        pin["runner"], pin["entry"], used=True, texts=texts, plan=plan, positional=positional
    ):
        problems.append(
            f"the pinned runner {pin['runner']} does not import {pin['entry']}, call it with "
            f"the job's own plan in {plan or 'its parameters'} and use what it returns"
        )
    if not calls_entry(BUILDER, pin["runner"], used=False, texts=texts):
        problems.append(f"no function of {BUILDER} calls the pinned runner {pin['runner']}")
    return problems


def pin_result(route: str) -> dict:
    """Scored: the route's render entry is its pin, and the pinned runner renders from it."""
    name = (
        "render entry pin: the table's entry and recipe paths are the pinned ones, and the "
        "production job runner renders from that entry"
    )
    if _registry() is None:
        return _kind(_no_registry("10.pin", name), "gate")
    problems = pin_problems(route)
    evidence = {"pins": _shown_path(RENDER_ENTRIES), "pin": render_entry_pins().get(route)}
    return _kind(_result("10.pin", name, not problems, problems, evidence), "gate")


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


# A word that says "not" in each card language: every not-applied line and route decline
# carries one (Turkish: the negative verb forms -mıyor/-meyecek/-mez, "yok", "değil").
NEGATION = {
    "en": re.compile(r"\b(?:not|no|never|none|without|cannot)\b|n['’]t\b", re.I),
    "tr": re.compile(r"m[ıiuü]yor|m[ae]y[ae]c[ae]k|m[ae]z\b|\byok\b|\bdeğil|\bhiçbir", re.I),
}


def negation_problems(routes: Mapping[str, Any]) -> list[str]:
    """Not-applied lines and route declines (``golden_lines`` shape) with no negation word in
    their language: a line reworded into a claim that the value is applied, even when the
    golden is re-recorded with it."""
    out = []
    for route, langs in routes.items():
        for lang, probes in (langs or {}).items():
            pattern = NEGATION.get(lang)
            for probe, got in probes.items():
                for record in [*got.get("lines", []), *got.get("declines", [])]:
                    text = str(record.get("text") or "")
                    if record.get("code") in TABLE_CODES and pattern and not pattern.search(text):
                        out.append(f"{route}/{lang}/{probe}: {text!r}")
    return out


def lines_result(route: str, current: dict[str, Any] | None = None) -> dict:
    """Scored: the route's card lines equal the recorded golden (both languages, codes), and
    each says the value is not applied (`negation_problems`)."""
    name = "card lines: the route's lines and their typed codes equal the recorded golden"
    if _registry() is None:
        return _kind(_no_registry("10.lines", name), "gate")
    if not GOLDEN_LINES.is_file():
        why = [f"no golden at {_shown_path(GOLDEN_LINES)}: record it with --write-golden-lines"]
        return _kind(_result("10.lines", name, False, why, {}), "gate")
    recorded = json.loads(GOLDEN_LINES.read_text(encoding="utf-8"))["routes"].get(route)
    now = (current if current is not None else golden_lines()).get(route)
    changed = _diff_paths(recorded, now)
    affirmative = negation_problems({route: now})
    why = []
    if changed:
        why.append(
            f"the {route} lines differ from the golden at {changed[:6]}: if the change is "
            "meant, re-record with --write-golden-lines and review the diff"
        )
    if affirmative:
        why.append(f"not-applied lines that do not say so: {affirmative[:4]}")
    evidence = {"changed": changed, "no_negation": affirmative}
    return _kind(_result("10.lines", name, not why, why, evidence), "gate")


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


def _decline_counts(req: dict, receipt: dict | None, card: dict, lang: str) -> bool:
    """A not-possible receipt whose reason is a route decline (the route's decline for this
    ask, or any registry decline text) counts only when that decline is about what this ask's
    typed facts ask (`decline_about_ask`, in the card's language)."""
    reason = str((receipt or {}).get("reason") or "")
    route = {
        d.get("text")
        for d in card.get("declines") or []
        if d.get("requirement_id") == req.get("id")
    }
    if reason not in route and not is_route_decline(reason):
        return True
    from app.kria.brief import BriefRequirement

    try:
        model = BriefRequirement.model_validate(req)
    except Exception:  # noqa: BLE001 - an ask the brief cannot hold is about nothing
        return False
    with _language(lang):
        return decline_about_ask(model, reason)


def check_ask(fx: dict, cards: Mapping[str, dict], replay: dict, req_id: str) -> dict:
    req = _requirement(fx, req_id)
    field = ask_field(req)
    render = _target_render(replay, "asks", req_id, fx)
    by_lang = {}
    for lang, card in cards.items():
        receipt = next((r for r in card["receipts"] if r.get("requirement_id") == req_id), None)
        by_lang[lang] = ask_verdict(
            receipt,
            render,
            card,
            field,
            decline_counts=_decline_counts(req, receipt, card, lang),
        )
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
    if target.key not in (replay.get("paths") or {}):
        return (
            f"the route's RENDER_ENTRY names no recipe path for {target.key}, so nothing the "
            "entry returns can show it"
        )
    value = (replay.get("recipe") or {}).get(target.key)
    return (
        f"the render entry {replay.get('entry')} returns {target.key}={value!r} at "
        f"{replay['paths'][target.key]!r}, which does not meet the asked "
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
            {
                "id": "r1",
                "kind": "timing",
                "scope": "global",
                "facts": {"duration_s": 30},
                "description": "30 s",
            },
            {
                "id": "r2",
                "kind": "style",
                "scope": "global",
                "facts": {"animation": "pop"},
                "description": "animate it",
            },
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


def _decline_check(req_id: str, declined_id: str, decline: str) -> bool:
    """Check 3 on ``req_id`` with the route's ``decline`` text answering ``declined_id``, one
    card per language (each in its own words)."""
    registry = _registry()
    req = _requirement(_SYNTHETIC_FX, declined_id)
    cards = {}
    for lang in LANGS:
        with _language(lang):
            ask = _req(req["id"], req["kind"], req["scope"], req["facts"])
            text = getattr(registry.lines, decline)()
            cards[lang] = _synthetic_card([], [_decline(ask, "text_animation", text)])
    return check_ask(_SYNTHETIC_FX, cards, _NO_REPLAY, req_id)["pass"]


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
        _row(
            "3 check_ask: the animation decline on an animation ask must PASS",
            True,
            _decline_check("r2", "r2", "animation_decline"),
        ),
        _row(
            "3 check_ask: the animation decline answering a length ask must FAIL",
            False,
            _decline_check("r1", "r1", "animation_decline"),
        ),
        _row(
            "3 check_ask: the title-position decline answering an animation ask must FAIL",
            False,
            _decline_check("r2", "r2", "title_position_decline"),
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
        _row(
            "4 clip holds inside the asked pace's hold bounds must PASS",
            True,
            honors("pace", [2.4, 2.6], "relaxed"),
        ),
        _row(
            "4 clip holds outside the asked pace's hold bounds must FAIL",
            False,
            honors("pace", [0.9, 2.6], "relaxed"),
        ),
        _row(
            "lane targets: a render target the fixture stopped grading must be flagged",
            False,
            not missing_targets(["4.title_animation", "5.r2"], {"4.title_animation"}),
        ),
        *_drift_controls(),
    ]


def _drift_controls() -> list[dict]:
    was = {"must_grade": {"f": ["4.a", "5.b"]}, "targets": {"f": {"4.a": {}, "5.b": {}}}}
    landed = {**was, "targets": {"f": {"5.b": {}}}}
    cases = {
        "a must_grade entry dropped must be flagged": (
            {**was, "must_grade": {"f": ["4.a"]}},
            set(),
        ),
        "a new lane target (a regression relabelled) must be flagged": (
            {**was, "targets": {"f": {**was["targets"]["f"], "2.direction": {}}}},
            set(),
        ),
        "a target removed while its check is still red must be flagged": (landed, set()),
    }
    rows = [
        _row(f"lane targets: {name}", False, not target_drift("f", verified, now, was))
        for name, (now, verified) in cases.items()
    ]
    return [
        *rows,
        _row(
            "lane targets: a target removed once the grader verifies it must PASS",
            True,
            not target_drift("f", {"4.a"}, landed, was),
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


def similar_name(strategy):
    return strategy.get("target_duration_s_hint")


def unrelated_local(segments):
    caption_strategy = {"target_duration_s": 30, "mode": "sentence"}
    return caption_strategy.get("target_duration_s"), segments


def log_only(strategy):
    logger.info("length %s ignored", strategy.get("target_duration_s"))
    return None


def _without(mapping, key):
    return {k: v for k, v in mapping.items() if k != key}


def drops_it(strategy):
    return _without(strategy, "target_duration_s")


def deletes_it(strategy):
    del strategy["target_duration_s"]
    return strategy


def _value(mapping, key):
    return mapping.get(key)


def reads_through_helper(strategy):
    seconds = _value(strategy, "target_duration_s")
    return {"length_s": seconds}


def decides_a_refusal(strategy):
    if strategy.get("target_duration_s") is None:
        raise ValueError("a length is needed")


class Plan:
    target_duration_s: float = 0.0
"""
# Job runners and worker modules for the 10.pin call rule (`calls_entry`).
_RUNNERS = {
    "tasks/synthetic_runner.py": """
from app.pipeline.synthetic_plan import compile_plan


def renders(job):
    recipe = compile_plan(job)
    return recipe


def drops_it(job):
    compile_plan(job)
    return {"duration": 30.0}


def parks_it(job):
    echo = compile_plan(job)
    del echo
    return {"duration": 30.0}


def names_only(job):
    return {"entry": compile_plan}


def same_name_elsewhere(job):
    from app.pipeline.echo_plan import compile_plan

    return compile_plan(job)


def hands_the_plan(job):
    return compile_plan(job, strategy=job.strategy, requirements=job.brief.live())


def spreads_the_plan(job):
    extra = {}
    extra["strategy"] = job.strategy
    extra["requirements"] = job.brief.live()
    return compile_plan(job, **extra)


def drops_the_plan(job):
    return compile_plan(job)


def hands_a_constant(job):
    return compile_plan(job, strategy={}, requirements=())


def hands_its_own_plan(job):
    plan = {"title_animation": "pop"}
    return compile_plan(job, strategy=plan, requirements=[])
""",
    "tasks/synthetic_worker.py": """
from app.tasks.synthetic_runner import renders


def dispatch(job):
    renders(job)
""",
    "tasks/synthetic_idle.py": """
from app.tasks.synthetic_runner import renders


def dispatch(job):
    return renders
""",
}


def gate_controls(route: str | None) -> list[dict]:
    """The consumer gate, its reader scan and the carrier rule, on synthetic claims and code."""
    collide = [
        "def other_objects(step, view, row):",
        "    if step.in_s is None or step.target_duration_s is None:",
        "        return None",
        '    plan = {"target_duration_s": 3}',
        '    seconds = row["target_duration_s"]',
        "    target = view.target_duration_s",
        "    return plan, seconds, target",
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
        _row(
            "10 a read through a helper that returns the key it is given must PASS",
            True,
            carrier("reads_through_helper"),
        ),
        _row("10 a read that decides a refusal must PASS", True, carrier("decides_a_refusal")),
        _row(
            "10 a read of a similarly named field is not a carrier must FAIL",
            False,
            carrier("similar_name"),
        ),
        _row(
            "10 a read off a local dict named like a strategy (not the parameter) must FAIL",
            False,
            carrier("unrelated_local"),
        ),
        _row("10 a read that is only logged must FAIL", False, carrier("log_only")),
        _row(
            "10 a helper that removes the field is not a read must FAIL",
            False,
            carrier("drops_it"),
        ),
        _row("10 deleting the field is not a read must FAIL", False, carrier("deletes_it")),
    ]
    entry_at = "pipeline/synthetic_plan.py:compile_plan"
    runs = {
        "renders": ("imports, calls and uses the entry must PASS", True),
        "drops_it": ("throws the entry's value away must FAIL", False),
        "parks_it": ("keeps the entry's value on a local nothing reads must FAIL", False),
        "names_only": ("only names the entry must FAIL", False),
        "same_name_elsewhere": ("calls a same-named function of another module must FAIL", False),
    }
    rows += [
        _row(
            f"10.pin a runner that {what}",
            want,
            calls_entry(f"tasks/synthetic_runner.py:{fn}", entry_at, used=True, texts=_RUNNERS),
        )
        for fn, (what, want) in runs.items()
    ]
    wiring = {
        "hands_the_plan": ("hands the job's plan to the entry's plan parameters must PASS", True),
        "spreads_the_plan": ("hands the job's plan through a **mapping must PASS", True),
        "drops_the_plan": ("leaves the entry's plan parameters out must FAIL", False),
        "hands_a_constant": ("hands the entry a constant plan must FAIL", False),
        "hands_its_own_plan": ("hands the entry a plan it built itself must FAIL", False),
    }
    rows += [
        _row(
            f"10.pin a runner that {what}",
            want,
            calls_entry(
                f"tasks/synthetic_runner.py:{fn}",
                entry_at,
                used=True,
                texts=_RUNNERS,
                plan=("strategy", "requirements"),
                positional=("job",),
            ),
        )
        for fn, (what, want) in wiring.items()
    ]
    holds = {"clips": [{"volume": 0, "hold_s": 1.5}, {"volume": 1.0, "hold_s": 2.5}]}
    rows.append(
        _row(
            "10.entry a recipe path matching a float (clips[volume=0.0]) must PASS",
            True,
            path_value(holds, "clips[volume=0.0].hold_s") == 1.5,
        )
    )
    rows += [
        _row(
            f"10.pin a worker module that {'calls' if want else 'never calls'} the runner must "
            f"{'PASS' if want else 'FAIL'}",
            want,
            calls_entry(worker, "tasks/synthetic_runner.py:renders", used=False, texts=_RUNNERS),
        )
        for worker, want in (
            ("tasks/synthetic_worker.py", True),
            ("tasks/synthetic_idle.py", False),
        )
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
    entry = render_entry(route)
    if entry is not None:
        from app.kria.route_capabilities import RenderEntry

        bad_entries = {
            "an entry outside the route's code": RenderEntry(f"{other_file}:x", {}),
            "an entry its file does not define": RenderEntry(
                f"{entry.entry.partition(':')[0]}:_no_such_entry_{route}", {}
            ),
            "a recipe path that does not parse": RenderEntry(
                entry.entry, {"title_animation": "text_layers[id^=]].effect"}
            ),
        }
        rows += [
            _row(f"10.entry {what} must be flagged", False, not entry_problems(route, bad))
            for what, bad in bad_entries.items()
        ]
        unpinned = {
            "an entry its pin does not name": RenderEntry(
                f"{entry.entry.partition(':')[0]}:_echo_{route}", entry.recipe
            ),
            "a recipe path its pin does not name": RenderEntry(
                entry.entry, {**entry.recipe, "length_s": "echo.length_s"}
            ),
        }
        rows += [
            _row(f"10.pin {what} must be flagged", False, not pin_problems(route, bad))
            for what, bad in unpinned.items()
        ]
        pins = render_entry_pins()
        listed = list((pins.get(route) or {}).get("params") or [])
        dropped = next(
            (p for p in reversed(listed) if p.isidentifier() and p not in PLAN_PARAMS), ""
        )
        short = {**pins, route: {**pins.get(route, {}), "params": listed}}
        short[route]["params"] = [p for p in listed if p != dropped]
        rows.append(
            _row(
                f"10.pin an entry parameter its pin does not list ({dropped}, an override) must "
                "be flagged",
                False,
                not pin_problems(route, pins=short),
            )
        )
    affirmative = {
        "en": "It is applied exactly as planned, the pace you chose",
        "tr": "Tam planlandığı gibi uygulanıyor, seçtiğin tempo",
    }
    for lang, text in affirmative.items():
        probe = {"p": {"lines": [{"code": NOT_APPLIED_CODE, "text": text}], "declines": []}}
        rows.append(
            _row(
                f"10.lines a {lang.upper()} not-applied line with no negation must be flagged",
                False,
                not negation_problems({"probe": {lang: probe}}),
            )
        )
    recorded = json.loads(GOLDEN_LINES.read_text(encoding="utf-8"))["routes"]
    rows.append(
        _row(
            "10.lines every recorded not-applied line says so must PASS",
            True,
            not negation_problems(recorded),
            negation_problems(recorded)[:3],
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
        rerecorded = golden_lines()
        reworded = lines_result(route, rerecorded)
    rows += [
        _row(
            f"10.lines a line reworded to say it is applied ({field}) must be flagged",
            False,
            reworded["pass"],
            reworded["why_it_fails"][:1],
        ),
        _row(
            f"10.lines the reworded line with its golden re-recorded ({field}) must be flagged",
            False,
            not negation_problems({route: rerecorded[route]}),
        ),
    ]
    return rows


# --- the replay contract on synthetic modules over synthetic routes

# A route whose render entry ``plan`` follows the title animation, the pace (as clip holds)
# and the stated length, field or typed ask, only with the lane switch on. Each variant breaks
# one of those; ``entrance_for`` is a helper that reads the plan but is not the entry. Its
# ``sys`` and ``Path`` are there for a module to try importing through it.
_SYNTHETIC_ROUTE = """
import sys
from pathlib import Path

from app.config import settings
from app.schemas.edit_proposal import pace_hold_bounds
from app.schemas.text_style_intent import normalize_title_animation


def _fact(requirements, name):
    for req in REQ_ITER:
        facts = REQ_FACTS or {}
        if name in facts:
            return facts[name]
    return None


def entrance_for(strategy, requirements):
    return normalize_title_animation(
        strategy.get("title_animation") or _fact(requirements, "animation")
    )


def label(text):
    return {"text": str(text)}


def plan(strategy, requirements, clips, entrance=None):
    if ENTRANCE_HANDED:
        return {
            "title": {"rows": [{"text": "Three days in Kas", "entrance": entrance}]},
            "clips": [{"id": clip["id"], "hold_s": 2.5} for clip in clips],
            "length_s": 30.0,
        }
    follows = SWITCH_READ
    animation = FIELD_ANIMATION or ASK_ANIMATION
    pace = strategy.get("pacing")
    length = FIELD_LENGTH or ASK_LENGTH
    entrance = normalize_title_animation(animation) if follows and animation else None
    if not (ENTRANCE_KEPT):
        entrance = None
    if not (PACE_KEPT):
        pace = None
    hold = pace_hold_bounds(pace if follows and pace else "balanced").preferred_s
    return {
        "title": {"rows": [{"text": "Three days in Kas", "entrance": ROW_ENTRANCE}]},
        "clips": [{"id": clip["id"], "hold_s": hold} for clip in clips],
        "length_s": float(length) if follows and length else 18.0,
    }
"""
_ROUTE_PARTS = {
    "SWITCH_READ": "bool(settings.SWITCH)",
    "FIELD_ANIMATION": 'strategy.get("title_animation")',
    "ASK_ANIMATION": '_fact(requirements, "animation")',
    "FIELD_LENGTH": 'strategy.get("creator_duration_s")',
    "ASK_LENGTH": '_fact(requirements, "duration_s")',
    "ENTRANCE_KEPT": "True",
    "PACE_KEPT": "True",
    "ROW_ENTRANCE": "entrance",
    "ENTRANCE_HANDED": "False",
    "REQ_ITER": "requirements",
    "REQ_FACTS": 'req.get("facts")',
}
_ROUTE_VARIANTS = {
    "honest": {},
    "typed": {"REQ_ITER": "requirements.live()", "REQ_FACTS": "req.facts"},
    "noswitch": {"SWITCH_READ": "True"},
    "two": {"ENTRANCE_KEPT": 'entrance in ("pop", "fade")'},
    "field": {"ASK_ANIMATION": "None", "ASK_LENGTH": "None"},
    "ask": {"FIELD_ANIMATION": "None", "FIELD_LENGTH": "None"},
    "leak": {"PACE_KEPT": "animation"},
    "hardcoded": {"ROW_ENTRANCE": '"fade"'},
    "noread": {"ENTRANCE_HANDED": "True"},
}
_CALL = 'plan(fx["strategy"], fx["requirements"], fx["clips"])'
# name -> (route variant, module source, whether the static rules apply, expected to pass)
_SYNTHETIC_CASES: dict[str, tuple[str, str, bool, bool]] = {
    "an honest module over an honest entry must PASS": (
        "honest",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        True,
    ),
    "an honest module over an entry that reads the brief as typed objects must PASS": (
        "typed",
        'def recipe(fx):\n    return plan(fx["strategy"], fx["brief"], fx["clips"])\n',
        True,
        True,
    ),
    "a module that reads the typed brief itself (brief.live()) must FAIL": (
        "typed",
        "def recipe(fx):\n    fx['brief'].live()\n"
        '    return plan(fx["strategy"], fx["brief"], fx["clips"])\n',
        True,
        False,
    ),
    "a constant recipe that never calls the entry must FAIL": (
        "honest",
        "def recipe(fx):\n    label('x')\n"
        "    return {'title': {'rows': [{'entrance': 'pop'}]}, 'length_s': 30.0}\n",
        True,
        False,
    ),
    "a route helper that reads the plan (not the render entry) must FAIL": (
        "honest",
        "def recipe(fx):\n"
        "    return {'title': {'rows': [{'entrance': entrance_for(fx['strategy'], "
        "fx['requirements'])}]}}\n",
        True,
        False,
    ),
    "an entry fed a plan the module wrote, not the fixture's, must FAIL": (
        "honest",
        "def recipe(fx):\n"
        "    return plan({'title_animation': 'pop', 'pacing': 'relaxed', "
        "'creator_duration_s': 30}, [], fx['clips'])\n",
        True,
        False,
    ),
    "a module that reads the plan itself (.get) must FAIL": (
        "honest",
        f"def recipe(fx):\n    fx['strategy'].get('title_animation')\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that copies the plan (dict()) must FAIL": (
        "honest",
        f"def recipe(fx):\n    dict(fx['strategy'])\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that compares the plan (==) must FAIL": (
        "honest",
        f"def recipe(fx):\n    fx['strategy'] == {{}}\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that iterates the asks must FAIL": (
        "honest",
        f"def recipe(fx):\n    [r for r in fx['requirements']]\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that probes an attribute of the plan (.value) must FAIL": (
        "honest",
        "def recipe(fx):\n    try:\n        fx['strategy'].value\n    except AttributeError:\n"
        f"        pass\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that feeds a route helper's reading of the plan into the entry must FAIL": (
        "noread",
        "def recipe(fx):\n"
        "    asked = entrance_for(fx['strategy'], fx['requirements'])\n"
        "    return plan(fx['strategy'], fx['requirements'], fx['clips'], entrance=asked)\n",
        True,
        False,
    ),
    "a module that hands the route a function of its own must FAIL": (
        "honest",
        f"def recipe(fx):\n    label(lambda: 'pop')\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that hashes the plan (static rules off) must FAIL": (
        "honest",
        "def recipe(fx):\n    try:\n        hash(fx['strategy'])\n    except TypeError:\n"
        f"        pass\n    return {_CALL}\n",
        False,
        False,
    ),
    "a module that deep-copies the plan (static rules off) must FAIL": (
        "honest",
        "import copy\n\n\ndef recipe(fx):\n    copy.deepcopy(fx['strategy'])\n"
        f"    return {_CALL}\n",
        False,
        False,
    ),
    "a module that calls the entry twice must FAIL": (
        "honest",
        f"def recipe(fx):\n    {_CALL}\n    return {_CALL}\n",
        True,
        False,
    ),
    "a module that reads the lane switch (static rules off) must FAIL": (
        "honest",
        "from app.config import settings\n\n\ndef recipe(fx):\n"
        f"    if not settings.SWITCH:\n        return {{}}\n    return {_CALL}\n",
        False,
        False,
    ),
    "a module that hands the switch to a route helper (static rules off) must FAIL": (
        "honest",
        "from app.config import settings\n\n\ndef recipe(fx):\n"
        f"    label(settings.SWITCH)\n    return {_CALL}\n",
        False,
        False,
    ),
    "a module that counts its runs must FAIL": (
        "honest",
        f"N = [0]\n\n\ndef recipe(fx):\n    N[0] += 1\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry that ignores the lane switch must FAIL": (
        "noswitch",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry that follows only two of the animations must FAIL": (
        "two",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry that reads only the plan field (ignores the typed ask) must FAIL": (
        "field",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry that reads only the typed ask (ignores the plan field) must FAIL": (
        "ask",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry whose pace follows only when an animation is asked must FAIL": (
        "leak",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
    "an entry whose helper follows the ask but whose recipe hard-codes the entrance must FAIL": (
        "hardcoded",
        f"def recipe(fx):\n    return {_CALL}\n",
        True,
        False,
    ),
}
# name -> a module that reaches a banned module through an allowed one (static rules only)
_LAUNDERED = {
    "a module importing pathlib's Path through route code (renamed) must FAIL": (
        "from synthetic_route_honest import Path as Where, plan\n\n\n"
        f"def recipe(fx):\n    Where('job.json')\n    return {_CALL}\n"
    ),
    "a module importing sys through route code must FAIL": (
        "from synthetic_route_honest import plan, sys\n\n\n"
        f"def recipe(fx):\n    sys.argv\n    return {_CALL}\n"
    ),
    "a module reaching frames through an allowed stdlib module (typing.sys) must FAIL": (
        "import typing\n\nfrom synthetic_route_honest import plan\n\n\n"
        f"def recipe(fx):\n    typing.sys._getframe(0)\n    return {_CALL}\n"
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
        "target_duration_requested": True,
    },
    "brief": {
        "version": 1,
        "requirements": [
            {"id": "r1", "kind": "style", "scope": "global", "facts": {"animation": "pop"}},
            {"id": "r2", "kind": "timing", "scope": "global", "facts": {"duration_s": 30}},
        ],
    },
    "clips": [{"id": f"c{i}", "kind": "video", "duration_s": 6.0} for i in range(3)],
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
_REPLAY_PATHS = {
    "title_animation": "title.rows[].entrance",
    "pace": "clips[].hold_s",
    "length_s": "length_s",
}


@functools.cache
def replay_contract_controls() -> tuple[dict, ...]:
    """The replay contract on synthetic modules over synthetic routes, through the same fresh
    interpreters a lane's replay runs in: only the honest module over the honest entry may come
    out valid with every target met. Run once per process."""
    from app.config import settings

    switch = "talking_head_route_follows_prompt_enabled"
    if not hasattr(settings, switch):
        return tuple(
            _row(f"replay contract: {name}", want, None, "no lane switch on this code")
            for name, want in [
                *((name, case[3]) for name, case in _SYNTHETIC_CASES.items()),
                *((name, False) for name in _LAUNDERED),
            ]
        )
    targets = honored_targets(_REPLAY_FX)
    plan = plan_runs(_REPLAY_FX, targets)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        routes = {}
        for variant, parts in _ROUTE_VARIANTS.items():
            source = _SYNTHETIC_ROUTE
            for marker, default in _ROUTE_PARTS.items():
                source = source.replace(marker, parts.get(marker, default))
            path = Path(tmp) / f"synthetic_route_{variant}.py"
            path.write_text(source.replace("SWITCH", switch), encoding="utf-8")
            routes[variant] = path
        on_suites, off_suites, lints = [], [], []
        for i, (variant, module_source, linted, _want) in enumerate(_SYNTHETIC_CASES.values()):
            route = routes[variant]
            source = f"from synthetic_route_{variant} import entrance_for, label, plan\n\n\n"
            source += module_source.replace("SWITCH", switch)
            module = Path(tmp) / f"replay_case_{i}.py"
            module.write_text(source, encoding="utf-8")
            base = {
                "module": str(module),
                "entry": {"file": str(route), "name": "plan"},
                "paths": _REPLAY_PATHS,
                "route_files": [_real(str(route))],
                "route_functions": [],
                "switch": switch,
                "sys_path": [tmp],
            }
            on, off = suites_for(plan, base)
            on_suites.append(on)
            off_suites.append(off)
            roots = ("app", f"synthetic_route_{variant}")
            lints.append(lint_replay_module(source, roots, (tmp,)) if linted else [])
        on_results, off_results = _workers([on_suites, off_suites])
        for name, source in _LAUNDERED.items():
            why = lint_replay_module(source, ("app", "synthetic_route_honest"), (tmp,))
            rows.append(_row(f"replay contract: {name}", False, not why, why[:3]))
    for name, lint, on, off in zip(_SYNTHETIC_CASES, lints, on_results, off_results, strict=True):
        graded = grade_runs(plan, on, off, entry="plan", switch=switch, lint=lint)
        got = graded["valid"] and all(graded["honored"].values())
        failed = [r["name"] for r in graded["controls"] if r["ok"] is not True]
        rows.append(_row(f"replay contract: {name}", _SYNTHETIC_CASES[name][3], got, failed))
    return tuple(rows)


def replay_controls(replay: dict) -> list[dict]:
    """The real route replay module's own controls (`grade_runs`)."""
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
        *[dict(row) for row in replay_contract_controls()],
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
        checks.append(_guarded("10.entry", "render entry", lambda: entry_result(fx["route"])))
        checks.append(_guarded("10.pin", "render entry pin", lambda: pin_result(fx["route"])))
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
        if str(c["id"]) in {"10.claims", "10.lines", "10.entry", "10.pin"}:
            c["kind"] = "gate"
    scored = [c for c in checks if not c.get("report_only")]
    targets = lane_targets(fx["fixture_id"])
    red = [str(c["id"]) for c in scored if not c["pass"]]
    graded = {str(c["id"]) for c in checks}
    missing = missing_targets([*targets, *must_grade(fx["fixture_id"])], graded)
    verified = {str(c["id"]) for c in scored if c["verdict"] == "VERIFIED"}
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
        "unexpected_green": sorted((set(targets) & graded) - set(red)),
        "missing_targets": missing,
        "target_drift": target_drift(fx["fixture_id"], verified),
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
        "lane targets this fixture no longer grades (restore them in its checks.honored): "
        f"{report['missing_targets'] or 'none'}",
        "lane targets moved off their foundation copy (only toward green): "
        f"{report.get('target_drift') or 'none'}",
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
    """2 INVALID; 1 an unexpected red, a lane target the fixture no longer grades, lane
    targets moved off their foundation copy, a fixture exclusion with no reason, or
    (``strict``) any red; else 0."""
    if not report["controls_ok"]:
        return 2
    if (
        report["unexpected_red"]
        or report["missing_targets"]
        or report.get("target_drift")
        or report["exclusions"]["unexplained"]
    ):
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
    where.add_argument("--replay-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--route", help="grade against this route instead of the fixture's")
    parser.add_argument("--out", type=Path, help="JSON report path")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every check passes")
    args = parser.parse_args(argv)
    if args.replay_worker:
        return replay_worker()
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
