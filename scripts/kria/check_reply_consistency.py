#!/usr/bin/env python3
"""Reply consistency over every recorded Kria draft (KRI-1346 / KRI-1347).

For each route replay fixture (``tests/fixtures/kria_replay/<name>/fixture.json``, plus the
recorded drafts under ``tests/fixtures/kria_reply_consistency/``) this builds
the creator-visible draft card from the same pure functions the draft turn runs
(`score_route.approval_card`, English and Turkish), once as recorded and once as the held first
draft (asks the creator later left out reopened), and asks `app.kria.reply_consistency` for
every pair of lines that cannot both be true:

* ``draft_self``: a Done / Will-do line beside a heads-up that its capability won't be applied;
* ``wrong_capability``: a "can't" that names another capability than the one asked;
* ``render``: a heads-up "won't be applied" the fixture's recorded render receipt
  (``prod_outcome.render.phone_lane_receipt`` / ``phone_beat_receipt``) shows applied.

It runs with ``KRIA_HONEST_REPLY_LINES_ENABLED`` off (today's replies: the contradictions are
reported, not failed) and on (must be zero). Offline: production's flags and verified features
(`tests/_prod_profile.py`), no model, no network.

The synthetic manifest of `score_route` advertises no Visuals capability and no catalog, so a
fixture whose recorded strategy plans reaction beats would read "pop-ins aren't available".
Here the manifest also advertises what the phone predicates production runs say for the
fixture's format (`reaction_beats`, `media_overlays`, `media_overlays:fullscreen` on a phone
Talking item) and lists the strategy's own visual ids as image media, as the prod item's
Visuals did.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/check_reply_consistency.py \
        [--fixture NAME ...] [--json OUT]
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"
if str(API) not in sys.path:
    sys.path.insert(0, str(API))


def _score_route():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("score_route", HERE / "score_route.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SR = _score_route()
# Recorded drafts kept for this check only (no route-grader `checks` / `replay_inputs`).
OWN_FIXTURES = API / "tests" / "fixtures" / "kria_reply_consistency"


def _prod_manifest(base):  # noqa: ANN001, ANN202
    """`score_route.manifest` plus the Visuals capabilities and catalog prod's item had."""

    def build(fx: dict):  # noqa: ANN202
        from app.agents._schemas.creator_agent import CapabilityAvailability, CreatorMediaRef
        from app.services.creator_capabilities import (
            CAPABILITY_MEDIA_OVERLAY_FULLSCREEN,
            CAPABILITY_REACTION_BEATS,
        )
        from app.services.phone_rollout import (
            phone_subtitled_overlays_supported,
            phone_subtitled_reaction_beats_supported,
        )

        out = base(fx)
        fmt = str(fx["strategy"].get("edit_format") or "")
        caps = dict(out.capabilities)
        if fmt == "subtitled":
            on = CapabilityAvailability(available=True)
            if phone_subtitled_overlays_supported():
                caps["media_overlays"] = on
                caps[CAPABILITY_MEDIA_OVERLAY_FULLSCREEN] = on
            if phone_subtitled_reaction_beats_supported():
                caps[CAPABILITY_REACTION_BEATS] = on
        ids = {
            str(b.get("visual_id"))
            for b in fx["strategy"].get("reaction_beats") or []
            if b.get("visual_id")
        }
        known = {m.media_id for m in out.media}
        media = list(out.media) + [
            CreatorMediaRef(media_id=i, kind="image") for i in sorted(ids - known)
        ]
        return out.model_copy(update={"capabilities": caps, "media": media})

    return build


def _held(fx: dict) -> dict | None:
    """The first draft before the creator chose to leave asks out, or None."""
    held = copy.deepcopy(fx)
    reopened = False
    for req in held["brief"]["requirements"]:
        if req.get("status") == "superseded":
            req["status"] = "open"
            req.get("facts", {}).pop("superseded_by", None)
            reopened = True
    return held if reopened else None


def check(fx: dict, flag: bool) -> list[dict[str, Any]]:
    from app.config import settings
    from app.kria.brief import CreativeBrief
    from app.kria.reply_consistency import contradictions

    rows: list[dict[str, Any]] = []
    variants = [("draft", fx)] + ([("held", h)] if (h := _held(fx)) is not None else [])
    render = (fx.get("prod_outcome") or {}).get("render")
    for variant, case in variants:
        with (
            SR._prod_flags(),  # noqa: SLF001
            patch.object(settings, "kria_honest_reply_lines_enabled", flag),
            patch.object(SR, "manifest", _prod_manifest(SR.manifest)),
        ):
            cards = SR.approval_cards(case)
            live = CreativeBrief.model_validate(case["brief"]).live()
            for lang, card in cards.items():
                shown = [d for d in card["not_applied"] if d.get("on_card", True)]
                found = contradictions(
                    live,
                    card["receipts"],
                    disclosures=shown,
                    strategy=case["strategy"],
                    render=render if variant == "draft" else None,
                )
                rows.append(
                    {
                        "fixture": fx["fixture_id"],
                        "variant": variant,
                        "lang": lang,
                        "flag": flag,
                        "route": card["route"],
                        "reply": card["reply"],
                        "contradictions": [c.as_record() for c in found],
                    }
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture", action="append", default=[])
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    dirs = {
        p.parent.name: p.parent
        for root in (SR.FIXTURES, OWN_FIXTURES)
        for p in sorted(root.glob("*/fixture.json"))
        if {"brief", "strategy", "clips"} <= set(json.loads(p.read_text()).keys())
    }
    names = args.fixture or sorted(dirs)
    rows: list[dict[str, Any]] = []
    for name in names:
        fx = SR.load_fixture(dirs[name])
        fx.setdefault("fixture_id", name)
        for flag in (False, True):
            try:
                rows += check(fx, flag)
            except Exception as exc:  # noqa: BLE001 - report, never hide a fixture
                rows.append(
                    {"fixture": name, "flag": flag, "error": f"{type(exc).__name__}: {exc}"}
                )
    failed = False
    for row in rows:
        mark = "ERROR" if "error" in row else ("OK" if not row["contradictions"] else "CONTRADICTS")
        print(
            f"{mark:12} {row['fixture']:20} flag={'on ' if row['flag'] else 'off'} "
            f"{row.get('variant', ''):6} {row.get('lang', '')}"
        )
        for c in row.get("contradictions", []):
            where = f"{c['kind']}: {c['capability']} {c['requirement_id']}"
            print(f"             - {where}: {c['detail']}")
        if "error" in row:
            print(f"             {row['error']}")
            failed = True
        elif row["flag"] and row["contradictions"]:
            failed = True
    off = sum(len(r.get("contradictions", [])) for r in rows if not r["flag"])
    on = sum(len(r.get("contradictions", [])) for r in rows if r["flag"])
    print(f"\n{len(names)} fixtures; contradictions switch off: {off}, switch on: {on}")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
