"""Pretty-print what each agent returned for every exported template.

Reads `tests/fixtures/agent_evals/{template_recipe,creative_direction}/prod_snapshots/*.json`
and prints a side-by-side per-template summary. No DB / no LLM call.

Usage (run from src/apps/api/):
  .venv/bin/python scripts/inspect_eval_fixtures.py
  .venv/bin/python scripts/inspect_eval_fixtures.py --template dimples_passport
  .venv/bin/python scripts/inspect_eval_fixtures.py --full
  .venv/bin/python scripts/inspect_eval_fixtures.py --agent template_recipe
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

FIXTURES_ROOT = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "agent_evals"
)

BAR = "─" * 72
DOT = "·" * 72


def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


def _bold(s: str) -> str:
    return _color(s, "1")


def _dim(s: str) -> str:
    return _color(s, "2")


def _cyan(s: str) -> str:
    return _color(s, "36")


def _green(s: str) -> str:
    return _color(s, "32")


def _yellow(s: str) -> str:
    return _color(s, "33")


def _slot_summary(slot: dict) -> str:
    pos = slot.get("position", "?")
    dur = slot.get("target_duration_s", "?")
    stype = slot.get("slot_type", "?")
    energy = slot.get("energy", "?")
    transition = slot.get("transition_in", "none")
    color = slot.get("color_hint", "none")
    speed = slot.get("speed_factor", 1.0)
    overlays = slot.get("text_overlays", []) or []
    n_ov = len(overlays)

    line = (
        f"  slot {pos}: {dur}s [{stype:>5}] energy={energy:<4} "
        f"transition={transition:<14} color={color:<10} speed={speed} "
        f"overlays={n_ov}"
    )
    return line


def _print_template_recipe(payload: dict, *, full: bool = False) -> None:
    out = payload.get("output", {})
    name = payload.get("meta", {}).get("template_name", "?")
    print(_bold(_cyan(f"▶ template_recipe — {name}")))
    print(
        f"  shot_count={out.get('shot_count')} "
        f"total={out.get('total_duration_s')}s "
        f"hook={out.get('hook_duration_s')}s "
        f"pacing={out.get('pacing_style', '')!r}"
    )
    print(
        f"  copy_tone={out.get('copy_tone', '')!r}  "
        f"caption_style={out.get('caption_style', '')!r}  "
        f"color_grade={out.get('color_grade', '')!r}"
    )
    print(
        f"  sync={out.get('sync_style', '')!r}  "
        f"transition_style={out.get('transition_style', '')!r}  "
        f"niche={out.get('subject_niche', '')!r}"
    )
    print(
        f"  flags: talking_head={out.get('has_talking_head')} "
        f"voiceover={out.get('has_voiceover')} "
        f"letterbox={out.get('has_permanent_letterbox')}"
    )
    slots = out.get("slots", [])
    if slots:
        print(_dim("  ── slots ──"))
        for s in slots:
            print(_slot_summary(s))
    inters = out.get("interstitials", [])
    if inters:
        print(_dim("  ── interstitials ──"))
        for it in inters:
            print(
                f"  after_slot={it.get('after_slot')} "
                f"type={it.get('type'):<16} animate_s={it.get('animate_s')} "
                f"hold_s={it.get('hold_s')} hold_color={it.get('hold_color')}"
            )
    if full:
        print(_dim("  ── full JSON ──"))
        print(json.dumps(out, indent=2, default=str))


def _print_creative_direction(payload: dict, *, full: bool = False) -> None:
    out = payload.get("output", {})
    name = payload.get("meta", {}).get("template_name", "?")
    text = out.get("text", "")
    wc = len(text.split())
    print(_bold(_cyan(f"▶ creative_direction — {name}")))
    print(_dim(f"  word_count={wc}"))
    if full:
        print()
        print(text)
    else:
        print()
        # Print first ~500 chars wrapped at sentence boundaries
        snippet = text[:500]
        if len(text) > 500:
            snippet += _dim(f" […{len(text) - 500} more chars]")
        print(snippet)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(_yellow(f"  ! failed to load {path.name}: {exc}"))
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=["template_recipe", "creative_direction", "clip_metadata"],
        default=None,
        help="Show only one agent's fixtures.",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Show only one template (filename slug, e.g. dimples_passport_travel_vlog).",
    )
    parser.add_argument(
        "--full", action="store_true", help="Print full JSON / full text (default: summary)."
    )
    args = parser.parse_args()

    if not FIXTURES_ROOT.exists():
        sys.exit(f"no fixtures found at {FIXTURES_ROOT}")

    agents_to_show = (
        [args.agent]
        if args.agent
        else ["template_recipe", "creative_direction", "clip_metadata"]
    )

    total = 0
    for agent in agents_to_show:
        agent_root = FIXTURES_ROOT / agent
        if not agent_root.exists():
            continue

        fixtures = sorted(agent_root.rglob("*.json"))
        if args.template:
            fixtures = [p for p in fixtures if p.stem == args.template]

        if not fixtures:
            continue

        print()
        print(_bold(_green(BAR)))
        print(_bold(_green(f"AGENT: {agent}  ({len(fixtures)} fixture(s))")))
        print(_bold(_green(BAR)))
        for path in fixtures:
            payload = _load(path)
            if payload is None:
                continue
            print()
            print(_dim(f"[{path.parent.name}/{path.stem}]"))
            if agent == "template_recipe":
                _print_template_recipe(payload, full=args.full)
            elif agent == "creative_direction":
                _print_creative_direction(payload, full=args.full)
            else:
                print(json.dumps(payload.get("output", {}), indent=2, default=str)[:400])
            print(_dim(DOT))
            total += 1

    print()
    print(_bold(f"Inspected {total} fixture(s)."))


if __name__ == "__main__":
    main()
