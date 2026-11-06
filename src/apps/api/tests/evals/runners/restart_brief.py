"""KRI-1189: did the brief model record a restart ask the way the receipts read it?

Shared by the Main Creator and brief-extractor evals (`meta.restart_brief`). Replay runs the
hand-authored golden answers, so it proves only this check and the answer shape; the paid live run
measures the model. Two things are judged on ``brief_updates``:

- ``cut_restarts``: some update records ``cut_restarts: true`` exactly when the fixture expects it
  (an unquoted "the sentence I restarted"), never for a named or quoted restart, a "keep my
  retakes" ask, or "start over" about the edit or a clip.
- ``cut_lines``: every entry is the creator's own words, said in its update's own text (the test
  the render applies, `opening_cut_ask._said_in_requirement`). Green 3's model copied the take's
  transcript ("So Lumo took its tech from cold hard, no again.") there.
"""

from __future__ import annotations

from typing import Any


def restart_brief_failures(updates: list[dict[str, Any]], expect: dict[str, Any]) -> list[str]:
    from app.kria.brief import BriefRequirement
    from app.kria.opening_cut_ask import _said_in_requirement

    failures: list[str] = []
    restarts = []
    cleanup_values = []
    for update in updates:
        facts = update.get("facts") or {}
        if facts.get("cut_restarts") is True:
            restarts.append(update)
        if isinstance(facts.get("speech_cleanup"), bool):
            cleanup_values.append(facts["speech_cleanup"])
        lines = facts.get("cut_lines") or []
        if isinstance(lines, str):
            lines = [lines]
        try:
            req = BriefRequirement(
                id="r1",
                kind=update.get("kind") or "style",
                scope=update.get("scope") or "global",
                description=update.get("description") or "",
                literal=update.get("literal"),
                facts=facts,
            )
        except ValueError as exc:  # a malformed live update is a failure, never a crash
            failures.append(f"update is not a valid requirement ({exc}): {update!r}")
            continue
        for line in lines:
            if not isinstance(line, str) or not _said_in_requirement(req, line):
                failures.append(
                    f"cut_lines entry {line!r} is not in its update's own words "
                    f"({req.description!r}): a transcript copy the render drops"
                )
    if expect["cut_restarts"] and not restarts:
        failures.append("no update records cut_restarts: true for an unquoted restart ask")
    if not expect["cut_restarts"] and restarts:
        failures.append(
            "cut_restarts: true recorded for an ask that is not an unquoted restart: "
            f"{[u.get('description') for u in restarts]}"
        )
    if "speech_cleanup" in expect and expect["speech_cleanup"] not in cleanup_values:
        failures.append(f"no update records speech_cleanup: {expect['speech_cleanup']}")
    return failures
