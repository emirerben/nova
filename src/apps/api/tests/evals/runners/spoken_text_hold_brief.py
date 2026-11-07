"""KRI-1292: a text at a spoken word records how long it stays as a typed ``hold_s``.

Shared by the Main Creator eval (`meta.spoken_text_hold_brief`). The answer's updates go through
the product's own `parse_brief_updates` and `requirement_spoken_text`, and the check wants the
number in the typed ``hold_s`` fact itself: the creator's sentence stays in the description, where
the wording fallback could also find a length, so reading the ask alone would not show the field.
Replay goldens are hand-authored: replay proves this check and the answer shape; the paid live run
measures what the model records.

Expectation: ``{"literal": <the text>, "hold_s": <seconds, or None for no recorded hold>}``.
"""

from __future__ import annotations

from typing import Any


def spoken_text_hold_brief_failures(
    updates: list[dict[str, Any]], expect: dict[str, Any]
) -> list[str]:
    from app.kria.brief import BriefRequirement, BriefUpdateBatchError, parse_brief_updates
    from app.kria.spoken_text_ask import requirement_spoken_text

    try:
        parsed = parse_brief_updates(updates)
    except BriefUpdateBatchError as exc:
        return [f"brief_updates do not parse: {exc}"]
    texts = [u for u in parsed if u.kind == "text" and u.literal == expect["literal"]]
    if len(texts) != 1:
        return [f"expected one text update {expect['literal']!r}, found {len(texts)}"]
    [update] = texts
    want = expect["hold_s"]
    if update.facts.get("hold_s") != want:
        return [f"the text update records hold_s {update.facts.get('hold_s')!r}, expected {want!r}"]
    ask = requirement_spoken_text(
        BriefRequirement(
            id="r1",
            kind="text",
            scope=update.scope or "global",
            literal=update.literal,
            description=update.description,
            facts=update.facts,
        )
    )
    if ask is None:
        return ["the text update is not read as a text at a spoken word"]
    if ask.hold_s != want:
        return [f"the spoken-word text's hold reads {ask.hold_s!r}, expected {want!r}"]
    return []
