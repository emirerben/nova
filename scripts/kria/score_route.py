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
only when the creator stated one) and every live requirement. The route fixtures list theirs
(`build_route_fixture.py` for the recorded ones).

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_route.py --fixture NAME
    make kria-replay FIXTURE=NAME          # a fixture with no score_<name>.py of its own

Offline: a socket guard fails any network attempt and no model is called. The approval card is
assembled from the same pure functions the draft turn runs (`tasks.kria_runtime`): receipts,
route declines, the route's card lines. On code that predates the route registry it falls back
to what that code did (unified montage only, lines only for approval-readiness accounts), so the
same fixture shows the drops as NOT VERIFIED there.

Checks (VERIFIED / NOT VERIFIED; INCONCLUSIVE when a check could not run). Each carries a
``kind``: ``disclosed_ok`` (applied or said before approval), ``honored`` (applied in the render,
nothing else counts) or ``route`` / ``report``.

1.        route: the draft resolves to the fixture's route (the card reads that route's table).
2.<field> disclosed_ok: a render replay shows the field applied, or a card line says it is not
          applied. Render evidence comes only from a route replay module
          (``scripts/kria/route_replays/<route>.py``: ``honored(fx) -> {"fields": {...},
          "asks": {...}}``); without one only the card can pass. Only the card's own lines and
          its typed declines count, never a hedge ("I can't check the captions on this draft
          yet" is not a disclosure).
3.<id>    disclosed_ok: the receipt is met (checked), or a typed decline before approval
          (``not_possible``, checked, with a reason), or for a length ask the card's length line
          (the route table's ``target_duration_s`` disclosure).
4.<field> honored: render evidence applies the field (``checks.honored.fields``).
5.<id>    honored: render evidence honors the ask (``checks.honored.asks``).
10.       consumer gate (REPORT ONLY): reads the route's registry table. A field marked
          ``Applied`` that no reader in the route's code touches is flagged, as is a field the
          route ignores that the card does not disclose.

Lane targets: ``tests/fixtures/kria_replay/lane_targets.json`` lists, per fixture, the checks
expected NOT VERIFIED today and the lane that turns each green. The report splits failures into
``expected_red`` (listed) and ``unexpected_red`` (not listed), and lists ``unexpected_green``
targets (a lane landed: remove the entry). ``checks.not_graded`` names asks or fields a fixture
deliberately leaves out, with the reason; the report echoes them.

Negative controls (mandatory, INVALID rule as in ``score_29_ekim.py``): each check's predicate
gets a synthetic output that ignores the ask (must FAIL) and one that honours it (must PASS);
the gate gets a synthetic unread field and a synthetic ``Applied`` claim with no reader (both
must be flagged). A failed control makes the scorecard INVALID (exit 2).
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
CREATOR_ID = uuid.UUID("00000000-0000-4000-8000-000000001097")


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


# ------------------------------------------------------------------------- card words

_NOT_APPLIED = re.compile(
    r"not\s+(?:be\s+)?(?:applied|used|shown|rendered|supported)|won't|will not|can't|cannot|"
    r"couldn't|isn't|aren't|ignored|left out|skipp?ed|not possible|unavailable",
    re.I,
)
FIELD_WORDS: dict[str, tuple[str, ...]] = {
    "direction": ("direction", "style of video", "video style"),
    "pacing": ("pacing", "pace", "tempo"),
    "story_structure": ("story structure", "story order", "structure", "storyline"),
    "intro_hook": ("hook", "opening line", "intro"),
    "caption_style": ("caption style", "captions"),
    "optional_treatments": ("treatment", "effects"),
    "title_animation": ("animation", "animated", "animate"),
    "label_position": ("labels", "label position"),
    "target_duration_s": ("length", "long", "seconds"),
    "hero_media_id": ("hero",),
    "licensed_sfx": ("sound effect",),
    "audio_strategy": ("music", "voiceover", "audio"),
    "opening_title": ("opening title", "title"),
    "shot_labels": ("label",),
}


def discloses_not_applied(card_text: str, field: str) -> bool:
    """A card line that names ``field`` and says it is not applied.

    A word counts from a word start only: "subtitles" does not name the title."""
    words = FIELD_WORDS.get(field, (field.replace("_", " "),))
    names = re.compile("|".join(rf"\b{re.escape(w)}" for w in words), re.I)
    for line in re.split(r"[\n.;]", card_text):
        if names.search(line) and _NOT_APPLIED.search(line):
            return True
    return False


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


def planned_fields(fx: dict) -> list[str]:
    """The fields to grade: the fixture's list, else what the planner set away from the default."""
    listed = (fx.get("checks") or {}).get("fields")
    if listed is not None:
        return list(listed)
    from app.agents._schemas.creator_agent import CreativeStrategy

    strategy = fx["strategy"]
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


def planned_asks(fx: dict) -> list[str]:
    listed = (fx.get("checks") or {}).get("asks")
    if listed is not None:
        return list(listed)
    return [r["id"] for r in fx["brief"]["requirements"] if r.get("status") != "superseded"]


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


def _registry():  # noqa: ANN202
    """The route registry, or None on code that predates it."""
    try:
        from app.kria import route_capabilities
        from app.kria.route_disclosure import decoupled_from_readiness, draft_route
    except ImportError:
        return None
    return SimpleNamespace(
        tables=route_capabilities,
        draft_route=draft_route,
        decoupled=decoupled_from_readiness,
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


def approval_card(fx: dict) -> dict[str, Any]:
    """The creator-visible draft reply, from the pure functions the draft turn calls.

    Mirrors `tasks.kria_runtime` for an account outside approval readiness (the default): the
    asks checked at draft, receipts with route declines, then the route's card lines. Lines and
    declines show only where that code shows them (readiness, or the registry's decoupling)."""
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
    from app.kria.route_disclosure import card_disclosures, card_text, with_route_declines
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
    receipts = (
        build_receipts(checked, facts, include_unchecked=True, strict_order=True) if checked else []
    )
    if shown:
        receipts = with_route_declines(receipts, checked, strategy, route=route)
    if not readiness and not facts.phone_talking:
        receipts = [r for r in receipts if r.verification != "unchecked"]
    reply = reply_from_receipts(
        CreativeBrief(version=brief.version, requirements=checked),
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
    card = {
        "route": route,
        "lines_shown": shown,
        "reply": reply,
        "changes": changes,
        "receipts": [r.model_dump(mode="json") for r in receipts],
        # ``on_card``: the line fits the card's character budget (`card_text`), so the creator
        # reads it; a line past the budget is recorded but discloses nothing.
        "not_applied": [{**d.as_record(), "on_card": d.text in reply} for d in disclosures],
        "text": "\n".join([reply, *[str(r.reason or "") for r in receipts]]),
    }
    card["disclosure_text"] = disclosure_text(card)
    return card


def disclosure_text(card: dict, field: str | None = None) -> str:
    """What the card says is not applied: its route lines and its typed declines, never an
    unchecked hedge or a summary that merely names a field. With ``field``, only that field's
    own lines (another field's line that happens to say "length" discloses nothing here)."""
    declines = [
        str(r.get("reason") or "")
        for r in card.get("receipts") or []
        if r.get("status") == "not_possible" and r.get("verification") == "checked"
    ]
    lines = [
        str(d.get("text") or "")
        for d in card.get("not_applied") or []
        if d.get("on_card", True) and (field is None or d.get("subject") == field)
    ]
    return "\n".join([*lines, *declines])


def route_replay(fx: dict, route: str) -> dict[str, Any]:
    """Render evidence from ``route_replays/<route>.py``, or nothing (only the card can pass)."""
    script = REPLAYS / f"{route}.py"
    if not script.is_file():
        return {"fields": {}, "asks": {}, "source": None}
    spec = importlib.util.spec_from_file_location(f"route_replay_{route}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = module.honored(fx)
    return {
        "fields": dict(seen.get("fields") or {}),
        "asks": dict(seen.get("asks") or {}),
        "source": str(script.relative_to(REPO)),
    }


# --------------------------------------------------------------------- verdict logic
#
# Pure predicates: the checks feed them the real outputs, the controls synthetic ones.


def field_verdict(field: str, card_text: str, honored: dict[str, bool]) -> dict[str, Any]:
    applied = bool(honored.get(field))
    disclosed = discloses_not_applied(card_text, field)
    return {"pass": applied or disclosed, "applied": applied, "disclosed_on_card": disclosed}


def ask_verdict(
    receipt: dict | None, honored: bool = False, card_line: str | None = None
) -> dict[str, Any]:
    """``card_line``: the card's own line for the field this ask maps to (a length ask and the
    route's ``target_duration_s`` line), when one says it is not applied."""
    receipt = receipt or {}
    checked = receipt.get("verification") == "checked"
    met = checked and receipt.get("status") == "met"
    declined = (
        checked
        and receipt.get("status") == "not_possible"
        and bool(str(receipt.get("reason") or "").strip())
    )
    on_card = bool(card_line and _NOT_APPLIED.search(card_line))
    return {
        "pass": bool(honored or met or declined or on_card),
        "met": met,
        "declined_before_approval": declined,
        "disclosed_on_card": on_card,
        "honored_at_render": honored,
        "receipt": {k: receipt.get(k) for k in ("status", "verification", "reason")},
    }


def ask_field(req: dict) -> str | None:
    """The strategy field whose card line answers this ask (typed facts only, KRI-667)."""
    facts = req.get("facts") or {}
    if req.get("kind") == "timing" and facts.get("duration_s") is not None:
        return "target_duration_s"
    return None


def honored_verdict(key: str, evidence: dict[str, bool]) -> dict[str, Any]:
    return {"pass": bool(evidence.get(key)), "applied_at_render": bool(evidence.get(key))}


# ------------------------------------------------------------------- consumer gate

EXEMPT_FIELDS = {"rationale": "explanatory prose shown to the creator, nothing to apply"}
_NOT_A_READ = re.compile(r"^\s*(#|\"\"\"|snapshot_kwargs\[|style\[)")


def _read_pattern(field: str) -> re.Pattern:
    return re.compile(
        rf"""(\[["']{field}["']\]|get\(["']{field}["']|\.{field}\b|["']{field}["']\s*:)"""
    )


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


def route_reads(fields: list[str], readers: Any) -> dict[str, list[str]]:
    """Read-shaped references to each field inside the route's files and functions."""
    sources = {rel: (APP / rel).read_text().splitlines() for rel in readers.files}
    gb_text = (APP / "tasks" / "generative_build.py").read_text()
    gb_lines = gb_text.splitlines()
    ranges = [
        (n.lineno, n.end_lineno)
        for n in ast.walk(ast.parse(gb_text))
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in readers.functions
    ]
    reads: dict[str, list[str]] = {}
    for field in fields:
        pattern = _read_pattern(field)
        hits = []
        for rel, lines in sources.items():
            hits += [
                f"{rel}:{i + 1}"
                for i, t in enumerate(lines)
                if pattern.search(t) and not _NOT_A_READ.match(t)
            ]
        for lo, hi in ranges:
            for i in range(lo - 1, hi):
                if pattern.search(gb_lines[i]) and not _NOT_A_READ.match(gb_lines[i]):
                    hits.append(f"tasks/generative_build.py:{i + 1}")
        reads[field] = hits
    return reads


def consumer_gate(
    route: str,
    card_text: str,
    *,
    extra_fields: tuple[str, ...] = (),
    extra_handling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The route's registry table against its code and the card.

    ``claimed_applied_unread``: marked ``Applied`` (no ``via``) but no reader in ``READERS``.
    ``unconsumed``: no reader and no carrier; ``undisclosed``: of those, what the card does not
    name as not applied. ``registry_drift`` is report-only detail."""
    from app.kria.route_capabilities import Applied, NotApplied, RecordedOnly, table_for

    table = table_for(route)
    if table is None:
        raise RuntimeError(f"no registry table for route {route!r}")
    handling = {**table.FIELD_HANDLING, **(extra_handling or {})}
    names, hidden = strategy_fields()
    settable = [n for n in names if n not in hidden] + list(extra_fields)
    reads = route_reads(settable, table.READERS)
    indirect = {f: h.via for f, h in handling.items() if isinstance(h, Applied) and h.via}
    claimed = sorted(
        f
        for f in settable
        if isinstance(handling.get(f), Applied) and not handling[f].via and not reads[f]
    )
    unconsumed = [
        f for f in settable if not reads[f] and f not in indirect and f not in EXEMPT_FIELDS
    ]
    undisclosed = [f for f in unconsumed if not discloses_not_applied(card_text, f)]
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
        "unconsumed": unconsumed,
        "undisclosed": undisclosed,
        "disclosed": [f for f in unconsumed if f not in undisclosed],
        "exempt": EXEMPT_FIELDS,
        "indirect_consumers": sorted(indirect),
    }


def gate_result(check_id: int | str, route: str, card_text: str) -> dict:
    if _registry() is None:
        return {
            "id": check_id,
            "name": "consumer gate (REPORT ONLY): needs the route's registry table",
            "pass": False,
            "verdict": "INCONCLUSIVE",
            "why_it_fails": ["no route registry on this code: the gate reads the route's table"],
            "evidence": {},
            "report_only": True,
        }
    gate = consumer_gate(route, card_text)
    why = []
    if gate["claimed_applied_unread"]:
        why.append(
            f"the {route} table marks {gate['claimed_applied_unread']} applied, but no reader "
            "in the route's code touches them"
        )
    if gate["undisclosed"]:
        why.append(
            f"the {route} route reads none of {gate['undisclosed']} and the approval card does "
            "not say they are not applied"
        )
    result = _result(
        check_id,
        "consumer gate (REPORT ONLY): fields the route ignores and the card does not disclose",
        gate["pass"],
        why,
        gate,
    )
    result["report_only"] = True
    result["verdict"] = "REPORT (clean)" if gate["pass"] else "REPORT (gaps)"
    return result


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
    verdict = field_verdict(field, disclosure_text(card, field), replay["fields"])
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
                "does not say it is not applied"
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
    line = next(
        (
            d.get("text")
            for d in card["not_applied"]
            if field and d.get("subject") == field and d.get("on_card", True)
        ),
        None,
    )
    verdict = ask_verdict(receipt, bool(replay["asks"].get(req_id)), line)
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
                + (f", and the card has no {field} line" if field else "")
            ],
            {**verdict, "requirement": req, "card_line": line},
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


def negative_controls(route: str | None) -> list[dict]:
    """``ok`` None: the control could not run on this code (no registry); never a pass."""
    silent = "Balanced pacing\nText Explainer direction"
    honest = "Heads-up for this video:\n- The balanced pacing isn't applied on this video yet"
    rows = [
        _row(
            "2 the card names the pace without saying it is dropped must FAIL",
            False,
            field_verdict("pacing", silent, {})["pass"],
        ),
        _row(
            "2 the card says the pace is not applied must PASS",
            True,
            field_verdict("pacing", honest, {})["pass"],
        ),
        _row(
            "2 render evidence applies the field must PASS",
            True,
            field_verdict("pacing", silent, {"pacing": True})["pass"],
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
            "2 an unchecked hedge on the card is not a disclosure must FAIL",
            False,
            field_verdict(
                "caption_style",
                disclosure_text(
                    {
                        "not_applied": [],
                        "receipts": [
                            {
                                "status": "partial",
                                "verification": "unchecked",
                                "reason": "I can't check the captions on this draft yet.",
                            }
                        ],
                    }
                ),
                {},
            )["pass"],
        ),
        _row(
            "2 a typed decline that names the field must PASS",
            True,
            field_verdict(
                "caption_style",
                disclosure_text(
                    {
                        "not_applied": [],
                        "receipts": [
                            {
                                "status": "not_possible",
                                "verification": "checked",
                                "reason": "I can't add the kinetic captions to this video yet",
                            }
                        ],
                    }
                ),
                {},
            )["pass"],
        ),
        _row(
            "2 a line about subtitles does not name the title must FAIL",
            False,
            field_verdict(
                "opening_title", "I can't add subtitles of the speech to this video yet", {}
            )["pass"],
        ),
        _row(
            "2 another field's line that names the word does not disclose this field must FAIL",
            False,
            field_verdict(
                "target_duration_s",
                disclosure_text(
                    {
                        "not_applied": [
                            {
                                "subject": "pacing",
                                "text": "The balanced pacing isn't applied on this video yet: "
                                "the cuts keep their usual length",
                                "on_card": True,
                            }
                        ],
                        "receipts": [],
                    },
                    "target_duration_s",
                ),
                {},
            )["pass"],
        ),
        _row(
            "2 a line past the card's budget is not read must FAIL",
            False,
            field_verdict(
                "opening_title",
                disclosure_text(
                    {
                        "not_applied": [
                            {
                                "subject": "opening_title",
                                "text": "I can't add the opening title on this video yet",
                                "on_card": False,
                            }
                        ],
                        "receipts": [],
                    }
                ),
                {},
            )["pass"],
        ),
        _row(
            "3 a length ask with no receipt and a card line that keeps the length must FAIL",
            False,
            ask_verdict(None, card_line="It will be about 30 seconds long")["pass"],
        ),
        _row(
            "3 a length ask with the card line saying the length is not applied must PASS",
            True,
            ask_verdict(None, card_line="I can't make this video the length you asked for yet")[
                "pass"
            ],
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
    if route is None or _registry() is None:
        detail = "no registry table on this code"
        rows += [
            _row("10 a new unconsumed field must be flagged", False, None, detail),
            _row("10 an Applied claim with no reader must be flagged", False, None, detail),
        ]
        return rows
    from app.kria.route_capabilities import Applied

    gate = consumer_gate(route, "", extra_fields=("zz_new_unconsumed_field",))
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
        "",
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
    return rows


# ---------------------------------------------------------------------------- report


def run_offline(fixture_dir: Path, route: str | None = None) -> dict:
    fx = load_fixture(fixture_dir)
    if route is not None:
        fx = {**fx, "route": route}
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
        gate = _guarded(10, "consumer gate", lambda: gate_result(10, fx["route"], card()["text"]))
        checks.append({**gate, "report_only": True, "kind": "report"})
        controls = negative_controls(fx["route"])
        try:
            shown = card()
        except Exception as exc:  # noqa: BLE001 - reported, never green
            shown = {"error": f"{type(exc).__name__}: {exc}"}
    for c in checks:  # a guarded check that raised carries no kind of its own
        c.setdefault("kind", _KIND_OF_ID.get(str(c["id"]).split(".")[0], "disclosed_ok"))
    scored = [c for c in checks if not c.get("report_only")]
    targets = lane_targets(fx["fixture_id"])
    red = [str(c["id"]) for c in scored if not c["pass"]]
    by_kind = {
        kind: {
            "passed": sum(1 for c in scored if c["kind"] == kind and c["pass"]),
            "failed": sum(1 for c in scored if c["kind"] == kind and not c["pass"]),
        }
        for kind in ("route", "disclosed_ok", "honored")
    }
    return {
        "fixture": fx["fixture_id"],
        "route": fx["route"],
        "mode": "offline",
        "source": fx.get("source", {}),
        "network_attempts_blocked": len(_BASE._NET_ATTEMPTS),
        "registry": _registry() is not None,
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
        "report_only": [c["id"] for c in checks if c.get("report_only")],
        "controls_ok": all(c["ok"] is not False for c in controls),
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
    out += [
        "",
        f"{report['passed']} verified, {report['failed']} not verified ({kinds}; "
        f"check(s) {report['report_only']} are report-only and never fail anything)",
        f"expected red (lane targets): {report['expected_red'] or 'none'}",
        f"unexpected red: {report['unexpected_red'] or 'none'}",
        f"unexpected green (remove from lane_targets.json): {report['unexpected_green'] or 'none'}",
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
        out.append("\nSCORECARD INVALID: a negative control did not behave")
    return "\n".join(out)


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
    if not report["controls_ok"]:
        return 2
    return 1 if args.strict and not report["all_green"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
