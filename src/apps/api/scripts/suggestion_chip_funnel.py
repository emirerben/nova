#!/usr/bin/env python3
"""Suggestion-chip funnel: shown -> tapped -> kept, from existing tables only (KRI-756).

Read-only. No network, no new table, no migration, no model call. Derived after the fact from:

* ``creation_threads.state["suggestion_sets"]``  (ring of the last 5 offered sets per thread)
* ``creation_thread_events``  (``user_message`` rows stamped with ``payload.suggestion`` when the
  message equals an offered chip; undo and completion events around them)

Definitions (nothing in the app emits ``kept``; it is computed here):

* shown   a set that was generated and served. One count per chip of every ring set. "Shown"
          means served by GET /creation-threads/{id}/suggestions, not seen on screen: there is no
          client beacon. The ring keeps 5 sets per thread, so a long thread under-counts shown
          (which inflates tap rates). ``--shown-log`` merges the unbounded structlog
          ``suggestion_chips.shown`` lines (JSON, one per line) to correct that.
* tapped  a ``user_message`` whose ``payload.suggestion`` is present. The server stamps it when
          the request carries ``suggestion: {set_id, chip_id}`` OR, absent that, when the text
          equals an offered chip's message. The stamp (8 fixed keys) does NOT record which, so
          typed lookalikes are counted as taps, and a media-only iOS send (it sends exactly
          ``Suggest an edit.``) counts as a tap of chip #1. CHIP #1 TAP BIAS: read chip #1 as an
          upper bound and judge the lever chips (rank >= 2) on their own; the report prints both.
* split   every table is also cut by ``label_source`` (agent | template, per chip) and by
          ``wording`` (agent | fallback | pending, per set) so the Flash-vs-template question is
          answerable from real taps. A tap is joined to its set through ``set_id``; a set that
          fell out of the 5-set ring reports ``unknown``.
* kept    tapped AND the turn completed AND no ``plan_section_undo`` / ``plan_undo_all`` /
          ``draft_undone`` before the next ``user_message``.
          completed = a completion event inside the window and no failure event;
          ``assistant_question`` / ``approval_requested`` / ``format_prompt`` or an
          ``assistant_response`` with ``turn_value == "question"`` is "asked back" (the chip
          did not execute) and is reported separately, not as kept.

BASELINE CAVEAT: the old iOS ``PromptChip`` rows only filled the composer, so they have NO tap
baseline in events. Do not compare a tap rate with a tap rate. The comparison baseline is the
share of threads reaching a completed first render and the number of follow-up chat edits per
completed render, printed in the "baseline" block (before the flag: from the old ring-less
threads; after: next to the chip arms).

Usage (from ``src/apps/api``)::

    .venv/bin/python scripts/suggestion_chip_funnel.py --fixture          # offline demo + proof
    .venv/bin/python scripts/suggestion_chip_funnel.py --days 14          # read the database
    .venv/bin/python scripts/suggestion_chip_funnel.py --days 14 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

UNDO_EVENTS = frozenset({"plan_section_undo", "plan_undo_all", "draft_undone"})
COMPLETED_EVENTS = frozenset({"draft_applied", "generation_ready", "assistant_execution", "ready"})
FAILED_EVENTS = frozenset(
    {
        "assistant_error",
        "assistant_render_failed",
        "system_render_failed",
        "turn_cancelled",
        "render_cancelled",
        "system_cancelled",
    }
)
ASKED_BACK_EVENTS = frozenset({"assistant_question", "approval_requested", "format_prompt"})
RING_KEY = "suggestion_sets"


@dataclass
class ThreadRecord:
    thread_id: str
    ring: list[Mapping[str, Any]] = field(default_factory=list)
    events: list[Mapping[str, Any]] = field(default_factory=list)  # ordered by sequence


@dataclass
class Cell:
    shown: int = 0
    tapped: int = 0
    completed: int = 0
    asked_back: int = 0
    failed: int = 0
    undone: int = 0
    kept: int = 0


Key = tuple[str, str, int]  # (state, kind, rank) -- rank is 1-based, as on the wire


@dataclass
class Funnel:
    cells: dict[Key, Cell] = field(default_factory=lambda: defaultdict(Cell))
    by_source: dict[str, Cell] = field(default_factory=lambda: defaultdict(Cell))
    by_wording: dict[str, Cell] = field(default_factory=lambda: defaultdict(Cell))
    threads: int = 0
    threads_with_set: int = 0
    user_messages: int = 0
    chip_taps: int = 0
    orphan_taps: int = 0  # tap stamped with a set that fell out of the 5-set ring
    chip_one_taps: int = 0  # taps on the first-prompt default_edit (includes media-only sends)
    threads_first_message_was_tap: int = 0
    threads_completed: int = 0
    threads_with_follow_up_edit: int = 0

    def totals(self) -> Cell:
        total = Cell()
        for cell in self.cells.values():
            for name in vars(total):
                setattr(total, name, getattr(total, name) + getattr(cell, name))
        return total


def _window(events: list[Mapping[str, Any]], index: int) -> list[Mapping[str, Any]]:
    """Events strictly after ``events[index]`` and before the next user_message."""
    out = []
    for event in events[index + 1 :]:
        if event.get("event_type") == "user_message":
            break
        out.append(event)
    return out


def classify_window(window: Iterable[Mapping[str, Any]]) -> dict[str, bool]:
    types = [str(e.get("event_type") or "") for e in window]
    payloads = [e.get("payload") if isinstance(e.get("payload"), Mapping) else {} for e in window]
    undone = any(t in UNDO_EVENTS for t in types)
    failed = any(t in FAILED_EVENTS for t in types)
    asked_back = any(t in ASKED_BACK_EVENTS for t in types) or any(
        t == "assistant_response" and p.get("turn_value") == "question"
        for t, p in zip(types, payloads, strict=True)
    )
    completed = (
        any(t in COMPLETED_EVENTS for t in types)
        or any(
            t == "assistant_response" and p.get("turn_value") != "question"
            for t, p in zip(types, payloads, strict=True)
        )
    ) and not failed
    return {
        "undone": undone,
        "failed": failed,
        "asked_back": asked_back and not completed,
        "completed": completed,
        "kept": completed and not undone,
    }


def derive_funnel(
    threads: Iterable[ThreadRecord], *, extra_shown: Iterable[Mapping[str, Any]] = ()
) -> Funnel:
    funnel = Funnel()
    seen_sets: set[tuple[str, str]] = set()
    chip_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    set_wording: dict[tuple[str, str], str] = {}

    def add_shown(thread_id: str, entry: Mapping[str, Any]) -> None:
        set_id = str(entry.get("set_id") or "")
        if not set_id or (thread_id, set_id) in seen_sets:
            return
        seen_sets.add((thread_id, set_id))
        wording = str(entry.get("wording") or "unknown")
        set_wording[(thread_id, set_id)] = wording
        for chip in entry.get("chips") or []:
            if not isinstance(chip, Mapping):
                continue
            key = (
                str(entry.get("state") or ""),
                str(chip.get("kind") or ""),
                int(chip.get("rank") or 0),
            )
            funnel.cells[key].shown += 1
            funnel.by_source[str(chip.get("label_source") or "unknown")].shown += 1
            funnel.by_wording[wording].shown += 1
            chip_index[(thread_id, str(chip.get("id") or ""))] = chip

    # Unbounded structlog lines first: they carry set-level wording and the ordered kinds only
    # (contract section 8), so rank is positional (1-based) and label_source is unknown.
    for line in extra_shown:
        thread_id = str(line.get("thread_id") or "")
        kinds = list(line.get("kinds") or [])
        chip_ids = list(line.get("chip_ids") or [])
        entry = {
            "set_id": line.get("set_id"),
            "state": line.get("state"),
            "wording": line.get("wording"),
            "chips": [
                {"id": chip_ids[i] if i < len(chip_ids) else "", "kind": kind, "rank": i + 1}
                for i, kind in enumerate(kinds)
            ],
        }
        add_shown(thread_id, entry)

    for record in threads:
        funnel.threads += 1
        for entry in record.ring:
            if isinstance(entry, Mapping):
                add_shown(record.thread_id, entry)
        if record.ring:
            funnel.threads_with_set += 1
        events = list(record.events)
        user_idx = [i for i, e in enumerate(events) if e.get("event_type") == "user_message"]
        funnel.user_messages += len(user_idx)
        completed_any = False
        for position, i in enumerate(user_idx):
            window = classify_window(_window(events, i))
            completed_any = completed_any or window["completed"]
            payload = (
                events[i].get("payload") if isinstance(events[i].get("payload"), Mapping) else {}
            )
            tap = (
                payload.get("suggestion")
                if isinstance(payload.get("suggestion"), Mapping)
                else None
            )
            if position == 0 and tap is not None:
                funnel.threads_first_message_was_tap += 1
            if tap is None:
                continue
            funnel.chip_taps += 1
            key = (
                str(tap.get("state") or ""),
                str(tap.get("kind") or ""),
                int(tap.get("rank") or 0),
            )
            if (record.thread_id, str(tap.get("set_id") or "")) not in seen_sets:
                funnel.orphan_taps += 1
            source = str(tap.get("label_source") or "unknown")
            wording = set_wording.get((record.thread_id, str(tap.get("set_id") or "")), "unknown")
            if key[1] == "default_edit" and key[0] == "first_prompt":
                funnel.chip_one_taps += 1
            for cell in (funnel.cells[key], funnel.by_source[source], funnel.by_wording[wording]):
                cell.tapped += 1
                cell.completed += window["completed"]
                cell.asked_back += window["asked_back"]
                cell.failed += window["failed"]
                cell.undone += window["undone"]
                cell.kept += window["kept"]
        if completed_any:
            funnel.threads_completed += 1
            if len(user_idx) >= 2:
                funnel.threads_with_follow_up_edit += 1
    return funnel


def _rate(num: int, den: int) -> str:
    return "-" if den == 0 else f"{100 * num / den:.0f}%"


def _row(label: str, cell: Cell) -> str:
    parts = [
        label,
        cell.shown,
        cell.tapped,
        _rate(cell.tapped, cell.shown),
        cell.completed,
        cell.asked_back,
        cell.failed,
        cell.undone,
        cell.kept,
        _rate(cell.kept, cell.tapped),
    ]
    return "| " + " | ".join(str(p) for p in parts) + " |"


def render(funnel: Funnel) -> str:
    lines = ["# Suggestion-chip funnel", ""]
    total = funnel.totals()
    lines += [
        f"threads read: {funnel.threads} (with a served set: {funnel.threads_with_set})",
        f"chips shown: {total.shown}  tapped: {total.tapped}  kept: {total.kept}  "
        f"(tap rate {_rate(total.tapped, total.shown)}, "
        f"kept/tapped {_rate(total.kept, total.tapped)})",
        f"orphan taps (their set fell out of the 5-set ring): {funnel.orphan_taps}",
        "",
        "| state | kind | rank | shown | tapped | tap rate | completed | asked back "
        "| failed | undone | kept | kept/tapped |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (state, kind, rank), cell in sorted(funnel.cells.items()):
        lines.append(_row(f"{state} | {kind} | {rank}", cell))
    for title, table in (("label_source", funnel.by_source), ("wording", funnel.by_wording)):
        lines += [
            "",
            f"## split by {title}",
            "",
            f"| {title} | shown | tapped | tap rate | completed | asked back "
            "| failed | undone | kept | kept/tapped |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for name, cell in sorted(table.items()):
            lines.append(_row(name, cell))
    levers = total.tapped - funnel.chip_one_taps
    lines += [
        "",
        "## chip #1 tap bias",
        "",
        "Chip #1 (first-prompt `default_edit`, message `Suggest an edit.`) is also what a "
        "media-only send transmits, and the stamp does not say whether the creator tapped it. "
        "Treat its tap count as an upper bound.",
        f"* taps on chip #1: {funnel.chip_one_taps} of {total.tapped} "
        f"({_rate(funnel.chip_one_taps, total.tapped)})",
        f"* taps on lever chips (rank >= 2, unambiguous): {levers}",
        f"* threads whose first message was a tap: {funnel.threads_first_message_was_tap} of "
        f"{funnel.threads_with_set} threads with a served set",
    ]
    lines += [
        "",
        "## baseline (the comparison is NOT a tap rate)",
        "",
        "The old iOS PromptChips only filled the composer, so they left no tap in events.",
        f"* user messages: {funnel.user_messages}, of which chip taps: {funnel.chip_taps}",
        f"* threads whose FIRST message was a chip tap: {funnel.threads_first_message_was_tap}",
        f"* threads reaching a completed render/turn: {funnel.threads_completed} "
        f"({_rate(funnel.threads_completed, funnel.threads)})",
        f"* of those, threads with a follow-up edit message: {funnel.threads_with_follow_up_edit} "
        f"({_rate(funnel.threads_with_follow_up_edit, funnel.threads_completed)})",
        "",
        "Compare the share of completed first renders and chat edits per completed render "
        "between flag-off and flag-on cohorts (KRIA_SUGGESTION_CHIPS_USER_IDS gives a clean arm).",
    ]
    return "\n".join(lines) + "\n"


# ── Offline proof input ──────────────────────────────────────────────────────────────


def synthetic_fixture() -> list[ThreadRecord]:
    """Four threads that exercise every branch, including an undo (ranks are 1-based).

    t1  first_prompt: tapped chip #1, draft applied, kept.
    t2  first_prompt: tapped 'captions' (agent-worded), completed, then undone before the next
        message.
    t3  ready: tapped 'sfx', asked back; typed own edit; tapped 'music', failed.
    t4  first_prompt set served and ignored; typed own prompt.
    """

    def ev(seq, event_type, payload=None):
        return {"sequence": seq, "event_type": event_type, "payload": payload or {}}

    def tap(set_id, chip_id, kind, rank, state, source="template"):
        return {
            "suggestion": {
                "set_id": set_id,
                "chip_id": chip_id,
                "kind": kind,
                "rank": rank,
                "state": state,
                "label_source": source,
                "prompt_version": "2026-10-19.1",
                "default_edit": kind == "default_edit",
            }
        }

    def fp(sid, wording="agent"):
        agent = wording == "agent"
        return {
            "set_id": sid,
            "state": "first_prompt",
            "wording": wording,
            "chips": [
                {
                    "id": "sc_d",
                    "kind": "default_edit",
                    "rank": 1,
                    "message": "Suggest an edit.",
                    "label_source": "template",
                    "default_edit": True,
                },
                {
                    "id": "sc_c",
                    "kind": "captions",
                    "rank": 2,
                    "message": "Add captions",
                    "label_source": "agent" if agent else "template",
                    "default_edit": False,
                },
                {
                    "id": "sc_h",
                    "kind": "hook",
                    "rank": 3,
                    "message": "Add a hook that matches the video",
                    "label_source": "agent" if agent else "template",
                    "default_edit": False,
                },
            ],
        }

    ready = {
        "set_id": "sg_r3",
        "state": "ready",
        "wording": "fallback",
        "chips": [
            {
                "id": "sc_s",
                "kind": "sfx",
                "rank": 1,
                "message": "Add sound effects",
                "label_source": "template",
            },
            {
                "id": "sc_m",
                "kind": "music",
                "rank": 2,
                "message": "Turn the music down",
                "label_source": "template",
            },
        ],
    }
    return [
        ThreadRecord(
            "t1",
            [fp("sg_a", "fallback")],
            [
                ev(1, "thread_created"),
                ev(2, "user_message", tap("sg_a", "sc_d", "default_edit", 1, "first_prompt")),
                ev(3, "draft_applied"),
                ev(4, "generation_ready"),
            ],
        ),
        ThreadRecord(
            "t2",
            [fp("sg_b")],
            [
                ev(1, "user_message", tap("sg_b", "sc_c", "captions", 2, "first_prompt", "agent")),
                ev(2, "draft_applied"),
                ev(3, "draft_undone"),
                ev(4, "user_message"),
                ev(5, "draft_applied"),
            ],
        ),
        ThreadRecord(
            "t3",
            [fp("sg_c", "fallback"), ready],
            [
                ev(1, "user_message", {}),
                ev(2, "draft_applied"),
                ev(3, "generation_ready"),
                ev(4, "user_message", tap("sg_r3", "sc_s", "sfx", 1, "ready")),
                ev(5, "assistant_question"),
                ev(6, "user_message", {}),
                ev(7, "draft_applied"),
                ev(8, "user_message", tap("sg_r3", "sc_m", "music", 2, "ready")),
                ev(9, "assistant_error"),
            ],
        ),
        ThreadRecord(
            "t4",
            [fp("sg_d")],
            [ev(1, "user_message", {}), ev(2, "draft_applied")],
        ),
    ]


# ── Database mode (read-only) ────────────────────────────────────────────────────────


def load_threads_from_db(days: int, limit: int) -> list[ThreadRecord]:
    from sqlalchemy import or_, select, text  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415
    from app.models import CreationThread, CreationThreadEvent  # noqa: PLC0415

    since = datetime.now(UTC) - timedelta(days=days)
    with sync_session() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        tapped_threads = (
            select(CreationThreadEvent.thread_id)
            .where(
                CreationThreadEvent.event_type == "user_message",
                CreationThreadEvent.payload.has_key("suggestion"),
                CreationThreadEvent.created_at >= since,
            )
            .distinct()
        )
        threads = session.execute(
            select(CreationThread.id, CreationThread.state)
            .where(
                CreationThread.created_at >= since,
                or_(
                    CreationThread.state.has_key(RING_KEY),
                    CreationThread.id.in_(tapped_threads),
                ),
            )
            .order_by(CreationThread.created_at.desc())
            .limit(limit)
        ).all()
        records: list[ThreadRecord] = []
        for thread_id, state in threads:
            rows = session.execute(
                select(
                    CreationThreadEvent.sequence,
                    CreationThreadEvent.event_type,
                    CreationThreadEvent.payload,
                )
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            ).all()
            records.append(
                ThreadRecord(
                    str(thread_id),
                    list((state or {}).get(RING_KEY) or []),
                    [{"sequence": r[0], "event_type": r[1], "payload": r[2] or {}} for r in rows],
                )
            )
        session.rollback()
    return records


def read_shown_log(path: Path) -> list[dict[str, Any]]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event") == "suggestion_chips.shown":
            out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture", action="store_true", help="synthetic events, no database")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=5000, help="max threads to read")
    parser.add_argument("--shown-log", type=Path, help="JSON-lines structlog output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    threads = synthetic_fixture() if args.fixture else load_threads_from_db(args.days, args.limit)
    extra = read_shown_log(args.shown_log) if args.shown_log else []
    funnel = derive_funnel(threads, extra_shown=extra)
    if args.json:
        payload = {
            "cells": {f"{s}|{k}|{r}": vars(c) for (s, k, r), c in sorted(funnel.cells.items())},
            "by_source": {k: vars(c) for k, c in sorted(funnel.by_source.items())},
            "by_wording": {k: vars(c) for k, c in sorted(funnel.by_wording.items())},
            **{
                k: v
                for k, v in vars(funnel).items()
                if k not in ("cells", "by_source", "by_wording")
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render(funnel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
