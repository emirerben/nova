"""Qendresa says-done cases 6-8: did the brief model record her caption shape and tight pauses?

Shared by the Main Creator and brief-extractor evals (`meta.caption_cleanup_brief`). The receipts
and the phone render act only on typed facts (KRI-667): `caption_words` / `caption_box` on the
captions requirement (#2104) and `pauses: "tight"` on the cleanup ask (#2102). A brief without them
is silently judged against the plain look and the long-pause cleanup. Replay runs hand-authored
golden answers, so it proves this check and the answer shape; the paid live run measures the model.

Every update is read by the product's own readers, never by matching words:

- ``captions``: exactly one update carries the `captions` fact, and `caption_look_ask` of that
  same update gives the expected words / box / position / colour (``None`` = must be absent). No
  other update carries a caption look (a split look is how the item-23 golden lost the box).
- ``tight_pauses``: some update is a cleanup ask (`wants_speech_cleanup`), and whether any is
  tight (`wants_tight_pauses`) equals the expectation.
"""

from __future__ import annotations

from typing import Any

_LOOK_KEYS = (
    "caption_size",
    "caption_position",
    "caption_color",
    "caption_case",
    "caption_words",
    "caption_box",
)
_EXPECTED_LOOK = ("words", "box", "position", "color")


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


def caption_cleanup_brief_failures(
    updates: list[dict[str, Any]], expect: dict[str, Any]
) -> list[str]:
    from app.kria.caption_look_ask import caption_look_ask
    from app.kria.tight_pause_ask import wants_speech_cleanup, wants_tight_pauses

    failures: list[str] = []
    reqs = []
    for index, update in enumerate(updates):
        try:
            reqs.append(_requirement(update, index))
        except ValueError as exc:  # a malformed live update is a failure, never a crash
            failures.append(f"update is not a valid requirement ({exc}): {update!r}")
    if failures:
        return failures

    want_captions = expect.get("captions")
    if want_captions is not None:
        captions = [req for req in reqs if "captions" in req.facts]
        if len(captions) != 1:
            failures.append(
                f"{len(captions)} updates carry `captions`, expected exactly one: "
                f"{[req.description for req in captions]}"
            )
        else:
            (req,) = captions
            if req.facts.get("captions") != want_captions["captions"]:
                got_captions = req.facts.get("captions")
                failures.append(
                    f"captions {got_captions!r}, expected {want_captions['captions']!r}"
                )
            ask = caption_look_ask(req)
            got = {field: getattr(ask, field) if ask else None for field in _EXPECTED_LOOK}
            for field in _EXPECTED_LOOK:
                if got[field] != want_captions.get(field):
                    failures.append(
                        f"caption look {field} {got[field]!r} on the captions requirement, "
                        f"expected {want_captions.get(field)!r} ({req.facts!r})"
                    )
        split = [
            req.description
            for req in reqs
            if "captions" not in req.facts and any(key in req.facts for key in _LOOK_KEYS)
        ]
        if split:
            failures.append(f"caption look recorded apart from the captions requirement: {split}")

    if "tight_pauses" in expect:
        cleanup = [req for req in reqs if wants_speech_cleanup(req)]
        if not cleanup:
            failures.append("no update records a speech-cleanup ask (speech_cleanup: true)")
        tight = any(wants_tight_pauses(req) for req in cleanup)
        if tight != expect["tight_pauses"]:
            failures.append(
                f"pauses tight is {tight}, expected {expect['tight_pauses']}: "
                f"{[req.facts for req in cleanup]}"
            )
    return failures
