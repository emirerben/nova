#!/usr/bin/env python3
"""Route scorecard: does every planner-chosen field and every stated ask reach the render, or
the approval card before approval, on the route that renders the draft? (KRI-1097 wave 2)

Generalized from ``score_assos.py`` (which keeps the unified-montage render replay). A fixture
is a recorded or reconstructed draft under ``tests/fixtures/kria_replay/<name>/fixture.json``:

    {"fixture_id", "route": "<RouteId>", "message", "planner_summary", "brief", "strategy",
     "clips": [{"id", "kind", "duration_s", "has_audio"?, "analysis"?}],
     "client_protocol"?, "source": {"recorded": [...], "reconstructed": [...]},
     "checks"?: {"fields": [...], "asks": ["r1", ...],
                 "honored"?: {"fields": [...], "asks": [...]}, "not_graded"?: {item: why}}}

``checks`` defaults are read from the plan, never from the code: every creator-visible field
the planner set away from the schema default (the pace always, since the card shows it; a length
only when the creator stated one) and every live requirement. A fixture that lists fewer leaves
the rest out of grading; each one left out is counted in the summary, and one with no reason in
``not_graded`` fails the run like an unexpected red.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_route.py --fixture NAME
    make kria-replay FIXTURE=NAME          # a fixture with no score_<name>.py of its own

Offline: a socket guard fails any network attempt and no model is called. The approval card is
assembled from the same pure functions the draft turn runs (`tasks.kria_runtime`): receipts,
route declines, the route's card lines. On code that predates the route registry it falls back
to what that code did (unified montage only, lines only for approval-readiness accounts), so the
same fixture shows the drops as NOT VERIFIED there.

Checks (VERIFIED / NOT VERIFIED; INCONCLUSIVE when a check could not run). Each carries a
``kind``: ``disclosed_ok`` (applied or said before approval), ``honored`` (applied in the render,
nothing else counts), ``gate`` or ``route`` / ``report``.

1.        route: the draft resolves to the fixture's route (the card reads that route's table).
2.<field> disclosed_ok: render evidence shows the field applied, or the card carries that
          field's own registry line (a `route_disclosure.Disclosure` with code
          ``not_applied_on_route`` and subject <field>) that the creator reads (a whole line,
          or named on the compact line), or a route decline about that field (an animation
          decline for ``title_animation``, a captions decline for ``caption_style``). Free text
          never counts, whatever it says ("applied exactly as planned, nothing is skipped").
3.<id>    disclosed_ok: the receipt is met (checked), or a typed decline before approval
          (``not_possible``, checked, with a reason), or for a length ask the card's own
          ``target_duration_s`` registry line.
4.<field> honored: render evidence applies the field (``checks.honored.fields``).
5.<id>    honored: render evidence honors the ask (``checks.honored.asks``).
10.claims gate: every field the route's table marks ``Applied`` is read off the strategy in the
          route's code (``READERS``: a strategy receiver, so ``step.target_duration_s`` is not a
          read), or its ``via`` names a ``"<path under app/>:<symbol>"`` that is defined there
          and names the field. A red one is listed in lane_targets.json or the run fails.
10.       consumer gate (REPORT ONLY): fields the route ignores that the card does not disclose.

Render evidence (checks 2, 4, 5) comes only from a route replay module,
``scripts/kria/route_replays/<route>.py``, under this contract:

* ``honored(fx) -> {"fields": {field: bool}, "asks": {req_id: bool}, "recipe": <JSON>}``, where
  ``recipe`` is what the route's code produced for ``fx`` (the plan the phone renders). Only a
  literal ``True`` counts.
* It must run the route's own code: the grader traces the call and a run that enters no file or
  function in the route's ``READERS`` gives no render evidence (its claims are discarded and
  the scorecard is INVALID).
* Negative controls rerun it (a) with every brief ask removed and every honored target field
  (and its provenance) removed from the plan, and (b) with the route's lane kill switch off
  (``LANE_SWITCH``). Every honored target must come back not honored in both, and the recipe
  of (a) must differ from the real one. A claim that survives either, or a claim with no
  recipe, makes the scorecard INVALID (exit 2).

Lane targets: ``tests/fixtures/kria_replay/lane_targets.json`` lists, per fixture, the checks
expected NOT VERIFIED today and the lane that turns each green. The report splits failures into
``expected_red`` (listed) and ``unexpected_red`` (not listed), and lists ``unexpected_green``
targets (a lane landed: remove the entry). An unexpected red exits 1.

Negative controls (mandatory, INVALID rule as in ``score_29_ekim.py``): each check's predicate
and the check functions themselves (`check_field`, `check_ask`, the card's ``on_card``) get a
synthetic card that ignores the ask (must FAIL) and one that honours it (must PASS); the gate
gets a synthetic unread field, an ``Applied`` claim with no reader, a colliding attribute read
and bad carriers (all must be flagged). A control that cannot run because the registry fails to
import is INVALID too; only code that predates the registry (no package) shows them n/a.
A failed control makes the scorecard INVALID (exit 2).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import importlib.util
import json
import re
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"
APP = API / "app"
FIXTURES = API / "tests" / "fixtures" / "kria_replay"
LANE_TARGETS = FIXTURES / "lane_targets.json"
REPLAYS = HERE / "route_replays"
REGISTRY_PACKAGE = APP / "kria" / "route_capabilities" / "__init__.py"
CREATOR_ID = uuid.UUID("00000000-0000-4000-8000-000000001097")

# `route_disclosure.NOT_APPLIED`: the code of a line the route's table built.
NOT_APPLIED_CODE = "not_applied_on_route"
# A route decline about an ask class that also says this strategy field is not applied. The
# grader owns this map: a decline about anything else (a title position, a pop-in) discloses
# no field.
DECLINE_DISCLOSES = {"text_animation": "title_animation", "captions": "caption_style"}
# Each route's lane kill switch (`config.py`): off, the route is byte-identical to before the
# lane, so a replay must show nothing honored.
LANE_SWITCH = {
    "voiceover_montage": "voiceover_route_follows_prompt_enabled",
    "talking_head": "talking_head_route_follows_prompt_enabled",
    "narrated": "narrated_route_follows_prompt_enabled",
    "lipsync_song": "lipsync_route_follows_prompt_enabled",
    "speech_excerpt": "speech_excerpt_route_follows_prompt_enabled",
    "slide_post": "slide_route_follows_prompt_enabled",
}
# Server provenance that states a field on the creator's behalf (removed with the field).
PROVENANCE = {
    "target_duration_s": ("creator_duration_s", "target_duration_requested"),
    "pacing": ("pacing_requested",),
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


def honored_targets(fx: dict) -> tuple[list[str], list[str]]:
    """The fields and asks a lane will honor in the render (``checks.honored``)."""
    honored = (fx.get("checks") or {}).get("honored") or {}
    return list(honored.get("fields") or []), list(honored.get("asks") or [])


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


def card_records(disclosures: list, reply: str) -> list[dict]:
    """The card's registry lines (`Disclosure` records), each with whether the creator reads it."""
    records = [d.as_record() for d in disclosures]
    return [{**r, "on_card": on_card(r, reply)} for r in records]


def decline_records(declines: list, receipts: list[dict], reply: str) -> list[dict]:
    """The route's declines (`route_disclosure.ask_declines`), each with the field it speaks for
    and whether the card shows it: a checked ``not_possible`` receipt for that ask with that
    reason, which the reply carries."""
    shown = {
        (r.get("requirement_id"), r.get("reason"))
        for r in receipts
        if r.get("status") == "not_possible" and r.get("verification") == "checked"
    }
    return [
        {
            "requirement_id": d.requirement_id,
            "subject": d.subject,
            "field": DECLINE_DISCLOSES.get(d.subject),
            "text": d.text,
            "on_card": (d.requirement_id, d.text) in shown and d.text in reply,
        }
        for d in declines
    ]


def approval_card(fx: dict) -> dict[str, Any]:
    """The creator-visible draft reply, from the pure functions the draft turn calls.

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
    reply = reply_from_receipts(
        CreativeBrief(version=brief.version, requirements=answered),
        receipts,
        summary=str(fx.get("planner_summary") or ""),
        notices=(),
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
        "not_applied": card_records(disclosures, reply),
        "declines": decline_records(
            ask_declines(answered, strategy, route=route) if shown else [],
            receipt_records,
            reply,
        ),
    }


def field_disclosed(card: dict, field: str) -> bool:
    """The card says ``field`` is not applied: that field's own registry line the creator reads,
    or a route decline about it. Free text, another field's line, or a decline about another ask
    never counts."""
    own_line = any(
        r.get("code") == NOT_APPLIED_CODE and r.get("subject") == field and r.get("on_card") is True
        for r in card.get("not_applied") or []
    )
    decline = any(
        r.get("field") == field and r.get("on_card") is True for r in card.get("declines") or []
    )
    return own_line or decline


def _strip_targets(fx: dict, fields: list[str]) -> dict:
    """``fx`` with every brief ask removed and ``fields`` (with their provenance) off the plan."""
    stripped = copy.deepcopy(fx)
    stripped["brief"]["requirements"] = []
    for field in fields:
        stripped["strategy"].pop(field, None)
        for key in PROVENANCE.get(field, ()):
            stripped["strategy"].pop(key, None)
    return stripped


def _route_code(route: str) -> tuple[set[str], set[str]]:
    """Absolute paths of the route's ``READERS`` files, and its generative_build functions."""
    registry = _registry()
    table = registry.tables.table_for(route) if registry is not None else None
    if table is None:
        return set(), set()
    files = {str((APP / rel).resolve()) for rel in table.READERS.files}
    return files, set(table.READERS.functions)


def _traced(run: Any, route: str) -> tuple[Any, int]:
    """``run()`` and whether it entered the route's own code (a call in ``READERS``)."""
    files, functions = _route_code(route)
    builder = str((APP / "tasks" / "generative_build.py").resolve())
    hits = [0]

    def profile(frame: Any, event: str, _arg: Any) -> None:
        if event != "call":
            return
        code = frame.f_code
        name = code.co_filename
        if name in files or (name == builder and code.co_name in functions):
            hits[0] += 1
            sys.setprofile(None)

    sys.setprofile(profile)
    try:
        value = run()
    finally:
        sys.setprofile(None)
    return value, hits[0]


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


def _run_replay(module: Any, fx: dict, route: str, *, control: bool = False) -> dict[str, Any]:
    """One run of the replay. A control rerun that raises is recorded (its control fails when
    the real run claims anything); the real run's own exception is the caller's to report."""
    try:
        seen, calls = _traced(lambda: module.honored(copy.deepcopy(fx)), route)
    except Exception as exc:  # noqa: BLE001 - a control that cannot run is never a pass
        if not control:
            raise
        return {"fields": {}, "asks": {}, "recipe": None, "route_code_calls": 0, "error": repr(exc)}
    seen = seen if isinstance(seen, dict) else {}
    return {
        "fields": {k: v is True for k, v in dict(seen.get("fields") or {}).items()},
        "asks": {k: v is True for k, v in dict(seen.get("asks") or {}).items()},
        "recipe": seen.get("recipe"),
        "route_code_calls": calls,
    }


def route_replay(fx: dict, route: str) -> dict[str, Any]:
    """Render evidence from ``route_replays/<route>.py`` (nothing without one), plus the
    reruns its controls read: asks removed, and the lane kill switch off."""
    module = _replay_module(route)
    if module is None:
        return {"fields": {}, "asks": {}, "source": None, "recipe": None, "route_code_calls": 0}
    from app.config import settings

    fields, _asks = honored_targets(fx)
    real = _run_replay(module, fx, route)
    stripped = _run_replay(module, _strip_targets(fx, fields), route, control=True)
    switch = LANE_SWITCH.get(route)
    switched_off = None
    if switch is not None and hasattr(settings, switch):
        with patch.object(settings, switch, False):
            switched_off = _run_replay(module, fx, route, control=True)
    claimed = any(real["fields"].values()) or any(real["asks"].values())
    trusted = claimed and real["route_code_calls"] > 0
    return {
        # Claims from a run that never entered the route's code are no render evidence.
        "fields": real["fields"] if trusted else {},
        "asks": real["asks"] if trusted else {},
        "recipe": real["recipe"],
        "route_code_calls": real["route_code_calls"],
        "claimed": claimed,
        "controls": {"asks_removed": stripped, "switch_off": switched_off, "switch": switch},
        "source": _shown_path(REPLAYS / f"{route}.py"),
    }


# --------------------------------------------------------------------- verdict logic
#
# Pure predicates: the checks feed them the real outputs, the controls synthetic ones.


def field_verdict(field: str, card: dict, honored: dict[str, bool]) -> dict[str, Any]:
    applied = honored.get(field) is True
    disclosed = field_disclosed(card, field)
    return {"pass": applied or disclosed, "applied": applied, "disclosed_on_card": disclosed}


def ask_verdict(
    receipt: dict | None, honored: bool = False, card: dict | None = None, field: str | None = None
) -> dict[str, Any]:
    """``field``: the strategy field whose own card line answers this ask (a length ask and the
    route's ``target_duration_s`` line)."""
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
            and r.get("code") == NOT_APPLIED_CODE
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


def honored_verdict(key: str, evidence: dict[str, bool]) -> dict[str, Any]:
    applied = evidence.get(key) is True
    return {"pass": applied, "applied_at_render": applied}


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
        gb_text = (APP / "tasks" / "generative_build.py").read_text()
        gb_lines = gb_text.splitlines()
        for node in ast.walk(ast.parse(gb_text)):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in readers.functions
            ):
                body = gb_lines[node.lineno - 1 : node.end_lineno]
                sources[f"tasks/generative_build.py:{node.name}"] = [""] * (node.lineno - 1) + body
    reads: dict[str, list[str]] = {}
    for field in fields:
        reads[field] = [
            f"{rel.split(':')[0]}:{n}"
            for rel, lines in sources.items()
            for n in reads_in(lines, field)
        ]
    return reads


def _names_field(field: str) -> re.Pattern:
    name = re.escape(field)
    return re.compile(rf"""["']{name}["']|\.{name}\b""")


def via_problem(via: str, field: str) -> str | None:
    """Why ``Applied(via=...)`` is no carrier for ``field``, or None when it is one: a
    ``"<path under app/>:<symbol>"`` whose symbol is defined in that file and names the field."""
    path, _, symbol = via.partition(":")
    file = APP / path
    if not path.endswith(".py") or not symbol.isidentifier():
        return f"{via!r} is not <path under app/>:<symbol>"
    if not file.is_file():
        return f"{path} does not exist"
    text = file.read_text()
    node = next(
        (
            n
            for n in ast.parse(text).body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and n.name == symbol
        ),
        None,
    )
    if node is None:
        return f"{symbol} is not defined in {path}"
    body = text.splitlines()[node.lineno - 1 : node.end_lineno]
    pattern = _names_field(field)
    if not any(pattern.search(line) and not line.strip().startswith("#") for line in body):
        return f"{path}:{symbol} never names {field}"
    return None


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
    (`field_disclosed`). ``registry_drift`` is report-only detail."""
    from app.kria.route_capabilities import Applied, NotApplied, RecordedOnly, table_for

    table = table_for(route)
    if table is None:
        raise RuntimeError(f"no registry table for route {route!r}")
    handling = {**table.FIELD_HANDLING, **(extra_handling or {})}
    names, hidden = strategy_fields()
    settable = [n for n in names if n not in hidden] + list(extra_fields)
    reads = route_reads(list(dict.fromkeys([*settable, *handling])), table.READERS, sources)
    bad = {
        f: via_problem(h.via, f)
        for f, h in handling.items()
        if isinstance(h, Applied) and h.via and via_problem(h.via, f)
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
    return {
        "registry_drift": drift,
        "pass": not undisclosed and not claimed,
        "settable_fields": len(settable),
        "claimed_applied_unread": claimed,
        "bad_carriers": bad,
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
    """Scored: no field the route's table marks applied lacks a reader or a real carrier."""
    name = "registry claims: every field the table marks applied is read by the route's code"
    if _registry() is None:
        return _kind(_no_registry("10.claims", name), "gate")
    gate = consumer_gate(route, card)
    claimed = gate["claimed_applied_unread"]
    why = (
        [
            f"the {route} table marks {claimed} applied, but nothing in the route's code reads "
            f"them off the strategy and no carrier names them ({gate['bad_carriers']})"
        ]
        if claimed
        else []
    )
    evidence = {k: gate[k] for k in ("claimed_applied_unread", "bad_carriers", "carriers")}
    return _kind(_result("10.claims", name, not claimed, why, evidence), "gate")


# ------------------------------------------------------------------------ the checks


def _kind(result: dict, kind: str) -> dict:
    return {**result, "kind": kind}


def check_route(fx: dict, card: dict) -> dict:
    wanted = fx["route"]
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


def check_field(fx: dict, card: dict, replay: dict, field: str) -> dict:
    verdict = field_verdict(field, card, replay["fields"])
    value = fx["strategy"].get(field)
    return _kind(
        _result(
            f"2.{field}",
            f"{field}: honored in the render or disclosed before approval",
            verdict["pass"],
            []
            if verdict["pass"]
            else [
                f"the plan chose {field}={value!r}; no render evidence applies it and the card "
                "carries no registry line or route decline for it"
            ],
            {**verdict, "value": value, "render_evidence": replay["source"]},
        ),
        "disclosed_ok",
    )


def _requirement(fx: dict, req_id: str) -> dict:
    return next((r for r in fx["brief"]["requirements"] if r["id"] == req_id), {})


def check_ask(fx: dict, card: dict, replay: dict, req_id: str) -> dict:
    receipt = next((r for r in card["receipts"] if r.get("requirement_id") == req_id), None)
    req = _requirement(fx, req_id)
    field = ask_field(req)
    verdict = ask_verdict(receipt, replay["asks"].get(req_id) is True, card, field)
    return _kind(
        _result(
            f"3.{req_id}",
            f"{req_id}: the ask is honored or declined before approval",
            verdict["pass"],
            []
            if verdict["pass"]
            else [
                f"{req.get('description') or req.get('literal')!r} has receipt "
                f"{verdict['receipt']}: neither met nor a typed decline"
                + (f", and the card has no {field} registry line" if field else "")
            ],
            {**verdict, "requirement": req},
        ),
        "disclosed_ok",
    )


def _no_render_evidence(fx: dict, replay: dict) -> str:
    if replay.get("error"):
        return f"the route replay raised: {replay['error'].get('why_it_fails')}"
    if replay["source"] is None:
        return (
            f"no render replay for this route (scripts/kria/route_replays/{fx['route']}.py): "
            "only the render can show it applied"
        )
    if replay.get("claimed") and not replay.get("route_code_calls"):
        return f"{replay['source']} never entered the route's code, so its claims are discarded"
    return f"{replay['source']} does not show it applied"


def check_honored_field(fx: dict, replay: dict, field: str) -> dict:
    verdict = honored_verdict(field, replay["fields"])
    value = fx["strategy"].get(field)
    return _kind(
        _result(
            f"4.{field}",
            f"{field}: applied in the render (lane target)",
            verdict["pass"],
            [f"the plan chose {field}={value!r}; {_no_render_evidence(fx, replay)}"],
            {**verdict, "value": value, "render_evidence": replay["source"]},
        ),
        "honored",
    )


def check_honored_ask(fx: dict, replay: dict, req_id: str) -> dict:
    verdict = honored_verdict(req_id, replay["asks"])
    req = _requirement(fx, req_id)
    return _kind(
        _result(
            f"5.{req_id}",
            f"{req_id}: honored in the render (lane target)",
            verdict["pass"],
            [
                f"{req.get('description') or req.get('literal')!r}: "
                f"{_no_render_evidence(fx, replay)}"
            ],
            {**verdict, "requirement": req, "render_evidence": replay["source"]},
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
_NO_REPLAY = {"fields": {}, "asks": {}, "source": None}


def _line(subject: str, text: str, code: str = NOT_APPLIED_CODE) -> SimpleNamespace:
    """A synthetic `route_disclosure.Disclosure`."""
    record = {"code": code, "subject": subject, "text": text}
    return SimpleNamespace(as_record=lambda record=record: dict(record))


def _decline(req_id: str, subject: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(requirement_id=req_id, subject=subject, text=text)


def _synthetic_card(
    lines: list[SimpleNamespace], declines=(), reply: str | None = None, *, receipted: bool = True
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
        "not_applied": card_records(list(lines), reply),
        "declines": decline_records(list(declines), receipts, reply),
    }


def _card_check(field: str, card: dict) -> bool:
    return check_field(_SYNTHETIC_FX, card, _NO_REPLAY, field)["pass"]


def _ask_check(card: dict) -> bool:
    return check_ask(_SYNTHETIC_FX, card, _NO_REPLAY, "r1")["pass"]


def check_function_controls() -> list[dict]:
    """The checks themselves (`check_field`, `check_ask`, ``on_card``) on synthetic cards."""
    pace = _line("pacing", "The balanced pacing isn't applied on this video yet: the cuts follow")
    length = _line("target_duration_s", "I can't make this video the length you asked for yet")
    title = _line("opening_title", "I can't add the opening title ('Three days in Kas') yet")
    opposite = "The balanced pacing is applied exactly as planned, nothing is skipped"
    length_opposite = "It will be exactly 30 seconds, nothing left out"
    title_pos = _decline("r9", "title_position", "I can't move the title to the spot you asked")
    animation = _decline("r8", "text_animation", "I can't animate on-screen text on this video")
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
            "2 check_field: a decline about another ask (title position) must FAIL",
            False,
            _card_check("opening_title", _synthetic_card([], [title_pos])),
        ),
        _row(
            "2 check_field: a text-animation decline discloses the title animation must PASS",
            True,
            _card_check("title_animation", _synthetic_card([], [animation])),
        ),
        _row(
            "2 check_field: a route decline no receipt carries is not on the card must FAIL",
            False,
            _card_check("title_animation", _synthetic_card([], [animation], receipted=False)),
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
    if registry is not None:
        compact = registry.lines.compact_line(["opening_title", "target_duration_s"])
        rows.append(
            _row(
                "2 on_card: a line the compact card line names is on the card must PASS",
                True,
                _card_check(
                    "opening_title", _synthetic_card([title], reply=f"Heads-up:\n- {compact}")
                ),
            )
        )
    else:
        rows.append(
            _row(
                "2 on_card: a line the compact card line names is on the card must PASS",
                True,
                None,
                "pre-registry code: no compact line",
            )
        )
    return rows


def predicate_controls() -> list[dict]:
    honest = _synthetic_card([_line("pacing", "The balanced pacing isn't applied on this video")])
    return [
        _row(
            "2 a card that names the pace without its registry line must FAIL",
            False,
            field_verdict(
                "pacing", _synthetic_card([], reply="Balanced pacing\nText Explainer direction"), {}
            )["pass"],
        ),
        _row(
            "2 the card's pacing registry line must PASS",
            True,
            field_verdict("pacing", honest, {})["pass"],
        ),
        _row(
            "2 render evidence applies the field must PASS",
            True,
            field_verdict("pacing", _synthetic_card([]), {"pacing": True})["pass"],
        ),
        _row(
            "2 render evidence that is not a literal True must FAIL",
            False,
            field_verdict("pacing", _synthetic_card([]), {"pacing": "yes"})["pass"],
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
            "4 no render evidence for a lane target must FAIL",
            False,
            honored_verdict("pacing", {})["pass"],
        ),
        _row(
            "4 render evidence that applies the lane target must PASS",
            True,
            honored_verdict("pacing", {"pacing": True})["pass"],
        ),
    ]


def gate_controls(route: str | None) -> list[dict]:
    """The consumer gate and its reader scan, on synthetic claims, code and carriers."""
    collide = [
        "        if step.in_s is None or step.target_duration_s is None:",
        '    plan = {"target_duration_s": 3}',
        '    seconds = row["target_duration_s"]',
        "    target = view.target_duration_s",
    ]
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
            "10 a real symbol that never names the field is not its carrier must FAIL",
            False,
            via_problem("pipeline/unified_montage.py:selected_visual_ids", "target_duration_s")
            is None,
        ),
        _row(
            "10 a real symbol that reads the field is its carrier must PASS",
            True,
            via_problem("pipeline/unified_montage.py:selected_visual_ids", "media_scope") is None,
        ),
    ]
    registry = _registry()
    if route is None or registry is None or registry.tables.table_for(route) is None:
        detail = "pre-registry code: no table to gate"
        return rows + [
            _row("10 a new unconsumed field must be flagged", False, None, detail),
            _row("10 an Applied claim with no reader must be flagged", False, None, detail),
            _row("10 an Applied claim read only as step.<field> must be flagged", False, None),
            _row("10 an Applied claim with a free-text carrier must be flagged", False, None),
        ]
    from app.kria.route_capabilities import Applied

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
    return rows


def replay_controls(fx: dict, replay: dict) -> list[dict]:
    """The route replay module against its own claims (the contract in the module docstring)."""
    fields, asks = honored_targets(fx)
    targets = [("fields", f) for f in fields] + [("asks", a) for a in asks]
    if replay.get("source") is None:
        why = "the route replay raised" if replay.get("error") else "no route replay"
        return [_row(f"replay: {why}, so no render claim to control", True, True)]

    def claims(run: dict | None) -> list[str]:
        if run is None:
            return []
        return [key for kind, key in targets if run[kind].get(key) is True]

    real = [key for kind, key in targets if replay[kind].get(key) is True]
    controls = replay["controls"]
    stripped = controls["asks_removed"]
    switched = controls["switch_off"]
    reruns = [run for run in (stripped, switched) if run is not None]
    rows = [
        _row(
            "replay: a claim whose control reruns raised must FAIL",
            False,
            bool(replay["claimed"] and any(run.get("error") for run in reruns)),
            [run.get("error") for run in reruns if run.get("error")],
        ),
        _row(
            "replay: a claim from a run that never entered the route's code must FAIL",
            False,
            bool(replay["claimed"] and not replay["route_code_calls"]),
            {"route_code_calls": replay["route_code_calls"]},
        ),
        _row(
            "replay: honored with every ask and target field removed must FAIL",
            False,
            bool(claims(stripped)),
            claims(stripped),
        ),
        _row(
            "replay: a claim with no recipe from the route's code must FAIL",
            False,
            bool(real and not replay.get("recipe")),
        ),
        _row(
            "replay: a claim whose recipe does not change without the asks must FAIL",
            False,
            bool(real and replay.get("recipe") == stripped.get("recipe")),
        ),
    ]
    if controls["switch"] is not None:
        rows.append(
            _row(
                f"replay: honored with {controls['switch']} off must FAIL",
                False,
                bool(claims(switched)),
                claims(switched),
            )
        )
    return rows


def negative_controls(route: str | None, fx: dict | None = None, replay: dict | None = None):
    """``ok`` None: the control could not run on code that predates the registry."""
    rows = [*predicate_controls(), *check_function_controls(), *gate_controls(route)]
    if fx is not None and replay is not None:
        rows += replay_controls(fx, replay)
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
        card_holder: dict[str, Any] = {}

        def card() -> dict:
            if "card" not in card_holder:
                card_holder["card"] = approval_card(fx)
            return card_holder["card"]

        replay = _guarded("replay", "route replay", lambda: route_replay(fx, fx["route"]))
        if "verdict" in replay:  # the replay module raised: no render evidence, reported
            replay = {"fields": {}, "asks": {}, "source": None, "error": replay}
        checks = [_guarded(1, "route", lambda: check_route(fx, card()))]
        checks += [
            _guarded(f"2.{f}", f, lambda f=f: check_field(fx, card(), replay, f))
            for f in planned_fields(fx)
        ]
        checks += [
            _guarded(f"3.{r}", r, lambda r=r: check_ask(fx, card(), replay, r))
            for r in planned_asks(fx)
        ]
        honored_fields, honored_asks = honored_targets(fx)
        checks += [
            _guarded(f"4.{f}", f, lambda f=f: check_honored_field(fx, replay, f))
            for f in honored_fields
        ]
        checks += [
            _guarded(f"5.{r}", r, lambda r=r: check_honored_ask(fx, replay, r))
            for r in honored_asks
        ]
        checks.append(
            _guarded("10.claims", "registry claims", lambda: claims_result(fx["route"], card()))
        )
        gate = _guarded(10, "consumer gate", lambda: gate_result(10, fx["route"], card()))
        checks.append({**gate, "report_only": True, "kind": "report"})
        try:
            controls = negative_controls(fx["route"], fx, replay)
        except RegistryBroken as exc:
            controls = [_row("the route registry imports", True, False, str(exc))]
        if registry_error is not None and not any(c["ok"] is False for c in controls):
            controls.append(_row("the route registry imports", True, False, registry_error))
        try:
            shown = card()
        except Exception as exc:  # noqa: BLE001 - reported, never green
            shown = {"error": f"{type(exc).__name__}: {exc}"}
    for c in checks:  # a guarded check that raised carries no kind of its own
        c.setdefault("kind", _KIND_OF_ID.get(str(c["id"]).split(".")[0], "disclosed_ok"))
        if str(c["id"]) == "10.claims":
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
    parser.add_argument("--route", help="grade against this route instead of the fixture's")
    parser.add_argument("--out", type=Path, help="JSON report path")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every check passes")
    args = parser.parse_args(argv)
    fixture_dir = args.fixture_dir or FIXTURES / args.fixture
    _BASE._bootstrap()
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
