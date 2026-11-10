"""One reply, one truth: hold / draft / review lines may not contradict each other or the render
(KRI-1346 / KRI-1347, yorum3).

Kria's reply is built from several sources: requirement receipts (Done / Partly / Couldn't /
Will do / Have a look), the route's heads-up lines (`route_disclosure`, "I can't add the
treatments ... so they won't be applied"), the planner's policy notices ("... shows as the
opening title for the first few seconds") and, after the render, the phone receipts
(`phone_lane_receipt`, `phone_beat_receipt`). yorum3 showed "Done: show that picture full
screen" beside "I can't add the treatments (overlays)", while the render drew all four.

This module types each line by the capability it is about, from typed facts only (KRI-667:
never the creator's words), and answers three questions:

* ``contradictions``: which lines disagree (draft vs itself, review vs the render receipt, a
  "can't" that names another capability than the one asked). The route replay checker
  (`scripts/kria/check_reply_consistency.py`) fails on any.
* ``reconcile_disclosures``: the heads-up without a treatment a Done / Will-do line for the
  same capability contradicts (the receipt is judged against the manifest's capability, the
  table line is static, so the receipt wins).
* ``reconcile_notices``: a "have a look" ask that a policy notice says the plan changed becomes
  one Partly line carrying that notice, and the notice is not repeated.
* ``reconcile_review``: a review "Done" for pictures the render receipt placed none of becomes
  "have a look", never a false Done.

Every function is pure and fail-open, and a no-op with ``KRIA_HONEST_REPLY_LINES_ENABLED`` off
(`brief_checks.honest_reply_lines`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.kria.brief import BriefRequirement
from app.kria.contracts import RequirementReceipt

log = structlog.get_logger()

# Capabilities a line can be about. Kept small and explicit: a capability is listed only when
# both a requirement fact and a heads-up subject or a render receipt can name it.
VISUALS = "visuals"  # photos / videos shown over the talk (cards or full screen)
CLOSING_CARD = "closing_card"  # an ending screen with the creator's words
SPEAKER_BACKGROUND = "speaker_background"  # a photo, video or colour behind the speaker
SOUND_EFFECTS = "sound_effects"

# `optional_treatments` value -> capability (`creator_capabilities` treatment table).
TREATMENT_CAPABILITY: dict[str, str] = {"overlays": VISUALS, "sfx": SOUND_EFFECTS}
# Heads-up subjects whose whole line is about one capability.
SUBJECT_CAPABILITY: dict[str, str] = {"overlay_display": VISUALS, "licensed_sfx": SOUND_EFFECTS}

# Receipt states that tell the creator the thing is (or will be) in the video.
_CLAIMS_DONE = frozenset({"met"})


@dataclass(frozen=True)
class Contradiction:
    """Two lines of one reply (or a line and the render) that cannot both be true."""

    kind: str  # "draft_self" | "render" | "wrong_capability"
    capability: str
    requirement_id: str | None
    detail: str

    def as_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capability": self.capability,
            "requirement_id": self.requirement_id,
            "detail": self.detail,
        }


def _enabled() -> bool:
    from app.kria.brief_checks import honest_reply_lines  # noqa: PLC0415

    return honest_reply_lines()


# --- what each line is about ------------------------------------------------------------------


def requirement_capabilities(req: BriefRequirement) -> frozenset[str]:
    """The capabilities an ask is about, from its typed facts (and the beat checker's own
    predicate, which the Done / Couldn't line for it comes from)."""
    from app.kria import brief_checks  # noqa: PLC0415

    out: set[str] = set()
    facts = req.facts
    if brief_checks.asks_closing_card(req):
        out.add(CLOSING_CARD)
    elif brief_checks._asked_background_color(req) is not None or facts.get(  # noqa: SLF001
        "speaker_background"
    ):
        out.add(SPEAKER_BACKGROUND)
    if facts.get("display_mode") in {"fullscreen", "card", "pip"}:
        out.add(VISUALS)
    else:
        try:
            if brief_checks._wants_beats(req):  # noqa: SLF001 - the beat checker's own test
                out.add(VISUALS)
        except Exception:  # noqa: BLE001 - a classification never fails a reply
            pass
    return frozenset(out)


def reason_capability(reason: object) -> str | None:
    """The capability a "can't" reason names, for the reasons that name one (both languages)."""
    if not isinstance(reason, str) or not reason:
        return None
    from app.kria import brief_checks as bc  # noqa: PLC0415

    def starts(*texts: str) -> bool:
        return any(
            reason.startswith(t) or reason.startswith(bc._REASON_TR.get(t, t))  # noqa: SLF001
            for t in texts
        )

    if starts(
        bc._CC_NOT_TALKING,  # noqa: SLF001
        bc._CC_UNAVAILABLE,  # noqa: SLF001
        bc._CC_NOT_DRAWN,  # noqa: SLF001
        bc._CC_APP_TOO_OLD,  # noqa: SLF001
        bc._CC_TOO_LONG,  # noqa: SLF001
        bc._CC_UNDRAWABLE,  # noqa: SLF001
        bc._CC_WORDS_DIFFER,  # noqa: SLF001
    ):
        return CLOSING_CARD
    if starts(
        bc._SB_NOT_TALKING,  # noqa: SLF001
        bc._SB_UNAVAILABLE,  # noqa: SLF001
        bc._SB_APP_TOO_OLD,  # noqa: SLF001
        bc._SBC_NOT_TALKING,  # noqa: SLF001
        bc._SBC_UNAVAILABLE,  # noqa: SLF001
        bc._SBC_APP_TOO_OLD,  # noqa: SLF001
    ):
        return SPEAKER_BACKGROUND
    return None


def disclosure_capabilities(
    subject: str, text: str, strategy: Mapping[str, Any] | None
) -> dict[str, str]:
    """``{capability: treatment-or-subject}`` a heads-up line says will not be applied.

    The treatments line lists the treatments it leaves out in brackets (`_lines.treatments`),
    our own text built from the strategy's typed list, so a treatment counts when its token is
    in that bracket."""
    if subject in SUBJECT_CAPABILITY:
        return {SUBJECT_CAPABILITY[subject]: subject}
    if subject != "optional_treatments":
        return {}
    out: dict[str, str] = {}
    for treatment in (strategy or {}).get("optional_treatments") or []:
        capability = TREATMENT_CAPABILITY.get(str(treatment))
        if capability and str(treatment) in text:
            out[capability] = str(treatment)
    return out


# --- contradictions -----------------------------------------------------------------------------


def _row(receipt: RequirementReceipt | Mapping[str, Any]) -> Mapping[str, Any]:
    return receipt.model_dump() if isinstance(receipt, RequirementReceipt) else receipt


def _claims_done(row: Mapping[str, Any]) -> bool:
    return row.get("status") in _CLAIMS_DONE


def _promised(row: Mapping[str, Any]) -> bool:
    """Done, or "Will do at render" (a planned receipt the reply promises)."""
    return _claims_done(row) or row.get("stage") == "planned"


def _render_visuals(render: Mapping[str, Any] | None) -> bool | None:
    """Whether the finished video holds pictures over the talk, from the phone receipts; None
    when the render recorded neither receipt."""
    if not render:
        return None
    lane = render.get("phone_lane_receipt")
    beats = render.get("phone_beat_receipt")
    if not isinstance(lane, Mapping) and not isinstance(beats, Mapping):
        return None
    applied = isinstance(lane, Mapping) and "overlays" in (lane.get("applied") or [])
    placed = isinstance(beats, Mapping) and bool(beats.get("placed"))
    return applied or placed


def contradictions(
    requirements: Iterable[BriefRequirement],
    receipts: Sequence[RequirementReceipt | Mapping[str, Any]],
    *,
    disclosures: Iterable[Any] = (),
    strategy: Mapping[str, Any] | None = None,
    render: Mapping[str, Any] | None = None,
) -> list[Contradiction]:
    """Every pair of lines in one reply (or a line and the render receipt) that disagree.

    ``disclosures`` are `route_disclosure.Disclosure` objects or their records; ``render`` is
    the finished variant (its ``phone_lane_receipt`` / ``phone_beat_receipt``) for a review."""
    reqs = {req.id: req for req in requirements}
    rows = [_row(r) for r in receipts]
    out: list[Contradiction] = []
    heads_up: dict[str, str] = {}
    for item in disclosures:
        subject = getattr(item, "subject", None) or (
            item.get("subject") if isinstance(item, Mapping) else None
        )
        text = getattr(item, "text", None) or (
            item.get("text") if isinstance(item, Mapping) else ""
        )
        for capability, what in disclosure_capabilities(
            str(subject or ""), str(text or ""), strategy
        ).items():
            heads_up[capability] = what
    rendered_visuals = _render_visuals(render)
    for row in rows:
        req = reqs.get(str(row.get("requirement_id")))
        if req is None:
            continue
        caps = requirement_capabilities(req)
        for capability in caps:
            if capability in heads_up and _promised(row):
                out.append(
                    Contradiction(
                        "draft_self",
                        capability,
                        req.id,
                        f"{row.get('status')} beside a heads-up that {heads_up[capability]} "
                        "won't be applied",
                    )
                )
        if VISUALS in caps and rendered_visuals is not None:
            if _claims_done(row) and row.get("generation_id") and not rendered_visuals:
                out.append(
                    Contradiction("render", VISUALS, req.id, "Done, but the render placed none")
                )
        named = (
            reason_capability(row.get("reason")) if row.get("status") == "not_possible" else None
        )
        if named is not None and named not in caps:
            out.append(
                Contradiction(
                    "wrong_capability",
                    named,
                    req.id,
                    f"the 'can't' names {named}; "
                    f"the ask is about {sorted(caps) or 'something else'}",
                )
            )
    if rendered_visuals and VISUALS in heads_up:
        out.append(
            Contradiction(
                "render", VISUALS, None, "heads-up says won't be applied; the render applied it"
            )
        )
    return out


# --- reconciliation (switch on) ----------------------------------------------------------------


def reconcile_disclosures(
    disclosures: Sequence[Any],
    *,
    requirements: Iterable[BriefRequirement],
    receipts: Sequence[RequirementReceipt | Mapping[str, Any]],
    strategy: Mapping[str, Any] | None,
) -> list[Any]:
    """The heads-up lines minus what a Done / Will-do line for the same capability contradicts.

    A treatments line keeps the treatments nothing contradicts (rebuilt with the same builder,
    so its wording and code stay the table's); a line about one capability is dropped whole."""
    if not _enabled() or not disclosures:
        return list(disclosures)
    try:
        found = [
            c
            for c in contradictions(
                requirements, receipts, disclosures=disclosures, strategy=strategy
            )
            if c.kind == "draft_self"
        ]
        if not found:
            return list(disclosures)
        drop = {c.capability for c in found}
        out: list[Any] = []
        for item in disclosures:
            caps = disclosure_capabilities(item.subject, item.text, strategy)
            if not caps or not (set(caps) & drop):
                out.append(item)
                continue
            if item.subject == "optional_treatments":
                from dataclasses import replace  # noqa: PLC0415

                from app.kria.route_capabilities import _lines  # noqa: PLC0415

                kept = [
                    t
                    for t in (strategy or {}).get("optional_treatments") or []
                    if TREATMENT_CAPABILITY.get(str(t)) not in drop and str(t) in item.text
                ]
                text = _lines.treatments(kept, strategy or {})
                if text:
                    out.append(replace(item, text=text))
        log.warning(
            "kria_reply_contradiction_rewritten",
            contradictions=[c.as_record() for c in found],
        )
        return out
    except Exception:  # noqa: BLE001 - a consistency check never fails a draft
        log.exception("kria_reply_consistency_failed")
        return list(disclosures)


def reconcile_notices(
    requirements: Iterable[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    notices: Sequence[str],
) -> tuple[list[RequirementReceipt], list[str]]:
    """A "have a look" ask a policy notice says the plan changed: one Partly line.

    yorum3: "Have a look: small label in the top left for the whole video ("Yorumla")" and,
    under it, '"Yorumla" shows as the opening title for the first few seconds. Talking edits
    can't keep text on screen the whole time yet.' The notice quotes the ask's literal (our
    own text, built from the plan's typed pin), so the ask's receipt carries the notice as its
    reason and the notice is not repeated."""
    receipts = list(receipts)
    notices = list(notices or ())
    if not _enabled() or not notices:
        return receipts, notices
    try:
        reqs = {req.id: req for req in requirements}
        used: set[int] = set()
        out: list[RequirementReceipt] = []
        for receipt in receipts:
            req = reqs.get(receipt.requirement_id)
            literal = " ".join(str(req.literal or "").split()) if req is not None else ""
            index = (
                next(
                    (i for i, n in enumerate(notices) if f'"{literal}"' in n and i not in used),
                    None,
                )
                if literal and receipt.verification == "unchecked"
                else None
            )
            if index is None:
                out.append(receipt)
                continue
            used.add(index)
            out.append(
                receipt.model_copy(
                    update={
                        "status": "partial",
                        "verification": "checked",
                        "stage": "checked",
                        "reason": notices[index].rstrip("."),
                    }
                )
            )
        return out, [n for i, n in enumerate(notices) if i not in used]
    except Exception:  # noqa: BLE001 - a consistency check never fails a draft
        log.exception("kria_reply_notice_consistency_failed")
        return receipts, notices


def reconcile_review(
    requirements: Iterable[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    render: Mapping[str, Any] | None,
) -> list[RequirementReceipt]:
    """A review "Done" the render receipt contradicts becomes "have a look" (never a false
    Done); everything else stays as judged."""
    receipts = list(receipts)
    if not _enabled():
        return receipts
    try:
        found = {
            c.requirement_id
            for c in contradictions(requirements, receipts, render=render)
            if c.kind == "render" and c.requirement_id
        }
        if not found:
            return receipts
        log.warning("kria_review_contradiction_rewritten", requirement_ids=sorted(found))
        return [
            r.model_copy(
                update={"status": "partial", "verification": "unchecked", "stage": "understood"}
            )
            if r.requirement_id in found
            else r
            for r in receipts
        ]
    except Exception:  # noqa: BLE001 - a consistency check never fails a review
        log.exception("kria_review_consistency_failed")
        return receipts
