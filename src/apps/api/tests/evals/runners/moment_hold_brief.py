"""Qendresa says-done case 10: a title or ending held for N seconds is not the video's length.

Shared by the Main Creator eval (`meta.moment_hold_brief`). Her Q1 prompt says `Start with a big
title "..." for the first 3 seconds` and `End with "comment READ" and an arrow for the last 3
seconds`. If the brief writer files either as a `timing` requirement with `duration_s: 3`, the
product reader `app.services.length_policy.creator_duration` takes it as the whole video's length.
This check builds the brief from the answer's updates and asks that same reader (KRI-667: typed
facts only, no word matching). Replay goldens are hand-authored, so replay proves this check and
the answer shape; the paid live run measures what the model records.

Expectations:

- ``video_length_s``: the seconds `creator_duration` reads from the brief (``None`` = no length).
- ``texts``: each ``{"scope", "literal"}`` is present as a `text` update.
  A text with ``"ending": True`` must also carry ``facts["closing"]`` equal to its literal (the
  typed mark that the words are the ending) and must not be read by
  ``brief_checks._asks_whole_video_text`` as a line for the whole video (#2129).
- ``moment_seconds``: no `timing` update carries a `duration_s` equal to one of these.
"""

from __future__ import annotations

from typing import Any


def _requirement(update: dict[str, Any], index: int):
    from app.kria.brief import BriefRequirement

    return BriefRequirement(
        id=f"r{index + 1}",
        kind=update.get("kind") or "style",
        scope=update.get("scope") or "global",
        description=update.get("description") or "",
        literal=update.get("literal"),
        facts=update.get("facts") or {},
    )


def moment_hold_brief_failures(updates: list[dict[str, Any]], expect: dict[str, Any]) -> list[str]:
    from app.kria.brief import CreativeBrief
    from app.services.length_policy import creator_duration

    failures: list[str] = []
    reqs = []
    for index, update in enumerate(updates):
        try:
            reqs.append(_requirement(update, index))
        except ValueError as exc:  # a malformed live update is a failure, never a crash
            failures.append(f"update is not a valid requirement ({exc}): {update!r}")
    if failures:
        return failures

    found = creator_duration(brief=CreativeBrief(version=1, requirements=reqs))
    got = found.seconds if found is not None else None
    want = expect.get("video_length_s")
    if got != want:
        failures.append(f"video length read from the brief is {got!r}, expected {want!r}")

    from app.kria.brief_checks import _asks_whole_video_text

    def _norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    for text in expect.get("texts") or []:
        matches = [
            req
            for req in reqs
            if req.kind == "text" and req.scope == text["scope"] and req.literal == text["literal"]
        ]
        if not matches:
            failures.append(
                f"no text update with scope {text['scope']!r} and literal {text['literal']!r}"
            )
            continue
        if not text.get("ending"):
            continue
        req = matches[0]
        if _norm(req.facts.get("closing")) != _norm(text["literal"]):
            failures.append(
                f"ending text {text['literal']!r} has closing fact {req.facts.get('closing')!r}, "
                "expected the ending words themselves"
            )
        if _asks_whole_video_text(req):
            failures.append(
                f"ending text {text['literal']!r} is read as a whole-video line (#2129 receipt)"
            )

    held = [float(value) for value in expect.get("moment_seconds") or []]
    for req in reqs:
        value = req.facts.get("duration_s") if req.kind == "timing" else None
        if isinstance(value, (int, float)) and float(value) in held:
            failures.append(
                f"timing update carries duration_s {value!r}, seconds of one moment "
                f"({req.description!r})"
            )
    return failures
