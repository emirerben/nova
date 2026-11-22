"""The ask ledger of a chat edit (KRI-1563).

After a chat edit on an existing draft, code (never the model's prose) accounts for each part
of what the creator asked and for each change the turn made:

* every requirement of the turn ends in one state: ``done`` (with the change that proves it, or
  a checker's verdict), ``declined`` (with a reason) or ``needs_choice`` (the turn asks them);
* every change in the turn's diff is named on the line of the ask that caused it, or listed as
  an extra ("Also changed: ..."). A change an ask claimed but its line never mentions, or the
  part of a change beyond what any ask named, is an extra too.

The reply is built from the ledger, and ``AskLedger.gaps`` checks both promises against the
reply text that was actually written, so a phrase cut, a lane the diff cannot read or a
trimmed reply shows up as a gap (logged in production, a failure in tests).

A turn with no ops goes through the ledger too (``no_op_ledger``): each part is declined with
the reason the copilot gave for it, or waits on the question the turn asks.

Kill switch: ``KRIA_ASK_LEDGER_ENABLED`` (``ask_ledger_enabled``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import TextStyleRow
from app.kria.contracts import RequirementReceipt
from app.kria.editor_receipts import bind_editor_turn, unmet_reason
from app.kria.reply_language import say
from app.services.kria_editor_ops_diff import DiffEntry, EditorDiff, describe_diff, describe_entry

STATES = ("done", "declined", "needs_choice")


def ask_ledger_enabled() -> bool:
    return bool(settings.kria_ask_ledger_enabled)


@dataclass(frozen=True)
class AskRow:
    requirement_id: str
    state: str  # one of STATES
    proof: tuple[str, ...]  # the phrases of the changes this ask's line names
    reason: str


@dataclass(frozen=True)
class ChangeRow:
    key: str  # lane.field, for logs (never creator text)
    phrases: tuple[str, ...]  # how a reply line may name it (live-count and plain wording)
    owners: tuple[str, ...]  # the requirements whose lines name it; () = listed as an extra


@dataclass(frozen=True)
class AskLedger:
    asks: tuple[AskRow, ...]
    changes: tuple[ChangeRow, ...]
    receipts: tuple[RequirementReceipt, ...]
    extras: tuple[str, ...] = ()  # the "Also changed" phrases

    def row(self, requirement_id: str) -> AskRow | None:
        return next((row for row in self.asks if row.requirement_id == requirement_id), None)

    def gaps(self, requirement_ids: Iterable[str], reply: str | None = None) -> list[str]:
        """Every broken promise, as ``code:id`` strings (no creator text)."""
        found: list[str] = []
        for rid in requirement_ids:
            row = self.row(rid)
            if row is None or row.state not in STATES:
                found.append(f"no_state:{rid}")
            elif row.state in ("declined", "needs_choice") and not row.reason.strip():
                found.append(f"no_reason:{rid}")
            elif row.state == "done" and not (row.proof or row.reason.strip()):
                found.append(f"no_proof:{rid}")
        if reply is not None:
            for change in self.changes:
                if not any(phrase and phrase in reply for phrase in change.phrases):
                    found.append(f"unnamed_change:{change.key}")
        return found


def ask_state(receipt: RequirementReceipt, proof: Sequence[str]) -> str:
    """The ledger state a receipt stands for.

    "Partly" is declined: the part that was not done is the reason, the part that was is
    proof. "Have a look" (no dimension named, only what changed) is done on that proof.
    """
    if receipt.status == "met":
        return "done"
    if receipt.verification == "unchecked" and receipt.status == "partial" and proof:
        return "done"
    return "declined"


def _phrase_forms(entry: DiffEntry, live: int) -> tuple[str, ...]:
    forms = (describe_entry(entry, live_text_count=live), describe_entry(entry))
    return tuple(dict.fromkeys(forms))


def editor_ledger(
    reqs: Sequence[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    diff: EditorDiff,
    rows: Sequence[TextStyleRow],
    *,
    unmet: Sequence[Mapping[str, str]] = (),
) -> AskLedger:
    """The ledger of an editor turn that changed the draft (its ops compiled into ``diff``)."""
    live = diff.live_text_count
    bound = bind_editor_turn(reqs, receipts, diff, rows, unmet=unmet, complete=True)
    owners: dict[tuple[str, ...], list[str]] = {}
    keys: dict[tuple[str, ...], str] = {}
    asks: list[AskRow] = []
    for receipt in bound.receipts:
        proof_entries = bound.named.get(receipt.requirement_id, [])
        proof: list[str] = []
        for entry in proof_entries:
            if entry.derived or not entry.targets:
                continue
            forms = _phrase_forms(entry, live)
            proof.append(forms[0])
            owners.setdefault(forms, []).append(receipt.requirement_id)
            keys.setdefault(forms, f"{entry.lane}.{entry.field}")
        asks.append(
            AskRow(
                requirement_id=receipt.requirement_id,
                state=ask_state(receipt, proof),
                proof=tuple(dict.fromkeys(proof)),
                reason=str(receipt.reason or ""),
            )
        )
    for entry in bound.unbound:
        if entry.derived or not entry.targets:
            continue
        forms = _phrase_forms(entry, live)
        owners.setdefault(forms, [])
        keys.setdefault(forms, f"{entry.lane}.{entry.field}")
    changes = tuple(
        ChangeRow(key=keys[forms], phrases=forms, owners=tuple(dict.fromkeys(ids)))
        for forms, ids in owners.items()
    )
    extras = tuple(describe_diff(bound.unbound, live_text_count=live, limit=None))
    return AskLedger(
        asks=tuple(asks), changes=changes, receipts=tuple(bound.receipts), extras=extras
    )


def no_op_ledger(
    reqs: Sequence[BriefRequirement],
    *,
    reply: str,
    asks_question: bool,
    unmet: Sequence[Mapping[str, str]] = (),
) -> AskLedger:
    """The ledger of an editor turn whose copilot changed nothing.

    A part the copilot named as not done is declined with its reason. Every other part waits
    on the question the turn asks, or (no question) is declined with the turn's own reply.
    """
    asks: list[AskRow] = []
    receipts: list[RequirementReceipt] = []
    for req in reqs:
        declined = unmet_reason(req, unmet)
        if declined is None and asks_question:
            state, reason = "needs_choice", reply.strip()
            receipt = RequirementReceipt(
                requirement_id=req.id,
                status="partial",
                verification="unchecked",
                stage="understood",
            )
        else:
            state, reason = "declined", (declined or reply).strip()
            receipt = RequirementReceipt(
                requirement_id=req.id,
                status="not_possible",
                verification="checked",
                stage="understood",
            )
        asks.append(AskRow(requirement_id=req.id, state=state, proof=(), reason=reason))
        receipts.append(receipt.with_reason(reason or None))
    return AskLedger(asks=tuple(asks), changes=(), receipts=tuple(receipts))


def no_op_reply(reqs: Sequence[BriefRequirement], ledger: AskLedger, *, question: str) -> str:
    """One line per part of a turn that changed nothing, then the turn's question (if any).

    A single-part ask keeps the turn's own reply: it already answers that one part.
    """
    if len(reqs) < 2:
        return question
    lines: list[str] = []
    for req in reqs:
        row = ledger.row(req.id)
        if row is None:
            continue
        if row.state == "needs_choice":
            lines.append(say(en="Your call: {ask}", tr="Karar senin: {ask}", ask=req.text()))
            continue
        reason = row.reason.rstrip(".")
        lines.append(
            say(
                en="Couldn't: {ask} ({reason})",
                tr="Yapamadım: {ask} ({reason})",
                ask=req.text(),
                reason=reason,
            )
            if reason and reason != question.strip().rstrip(".")
            else say(en="Couldn't: {ask}", tr="Yapamadım: {ask}", ask=req.text())
        )
    body = "\n".join(f"- {line}" for line in lines)
    return f"{body}\n{question.strip()}" if question.strip() else body


__all__ = [
    "STATES",
    "AskLedger",
    "AskRow",
    "ChangeRow",
    "ask_ledger_enabled",
    "ask_state",
    "editor_ledger",
    "no_op_ledger",
    "no_op_reply",
]
