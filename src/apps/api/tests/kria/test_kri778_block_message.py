"""KRI-778: the message of a render the receipts blocked reads as whole sentences.

Café M2 (jobs 417eecd4 / 50f67a06) showed: "some of the groups you named are not where you
said This draft is about 4.7s; you asked for 1s. This draft is about 4.7s; you asked for 14s.
Your draft is saved. ..." The worker-path tests live in
``tests/tasks/test_unified_montage_dispatch.py``; these pin the assembly itself.

How the message can go wrong (each is checked below):
1. A reason with no full stop runs into the next sentence (prod).
2. A reason that starts lowercase reads as a fragment (prod).
3. Every global length ask is judged against the same draft, so the draft's length is said
   once per ask, each with a different target (prod: 1 s and 14 s).
4. The length sentence carries an outro clause, or is Turkish, and is not recognised, so
   the repeat comes back; or the length checker's words change and nothing notices.
5. A Turkish reason starting with "i" is capitalised to "I", not "İ".
6. A reason that already ends in a quote or bracket after its full stop gets a second one;
   one ending in a bracket with no stop gets none.
7. The title-only blocker (a typed, answerable decline) changes shape.
8. The chat's Turkish re-wording of an English worker message stops matching (a format-limit
   receipt among the stored receipts, which the worker leaves out of the message).
9. A reason the checkers can write today hits one of the above (the sweep of every static
   reason in ``brief_checks``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.kria import brief_checks
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    NO_TITLE_REASON,
    PlanFacts,
    build_receipts,
    needs_creator_choice,
    render_block_recovery,
)
from app.kria.reply_language import reply_language_for
from app.tasks.kria_runtime import _localized_block_recovery

SUFFIX_EN = "Your draft is saved. Should I try again or simplify this request?"
SUFFIX_TR = "Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa isteği sadeleştireyim mi?"

# The receipts job 417eecd4 stored in `assembly_plan.request_recovery` (prod, 2026-10-21).
PROD_RECEIPTS = [
    {
        "stage": "checked",
        "reason": "some of the groups you named are not where you said",
        "status": "partial",
        "verification": "checked",
        "requirement_id": "r1",
    },
    {
        "stage": "matched",
        "reason": "I can't verify this timing automatically.",
        "status": "partial",
        "verification": "unchecked",
        "requirement_id": "r2",
    },
    {
        "stage": "checked",
        "reason": "This draft is about 4.7s; you asked for 1s.",
        "status": "partial",
        "verification": "checked",
        "requirement_id": "r3",
    },
    {
        "stage": "understood",
        "reason": "I can't check the sound mix on this draft yet.",
        "status": "partial",
        "verification": "unchecked",
        "requirement_id": "r4",
    },
    {
        "stage": "checked",
        "reason": None,
        "status": "met",
        "verification": "checked",
        "requirement_id": "r5",
    },
    {
        "stage": "matched",
        "reason": "I can't verify text for that target clip in this draft.",
        "status": "partial",
        "verification": "unchecked",
        "requirement_id": "r6",
    },
    {
        "stage": "checked",
        "reason": "This draft is about 4.7s; you asked for 14s.",
        "status": "partial",
        "verification": "checked",
        "requirement_id": "r7",
    },
]


def _row(reason: str, requirement_id: str = "r1") -> dict:
    return {
        "requirement_id": requirement_id,
        "status": "partial",
        "verification": "checked",
        "reason": reason,
    }


def test_the_stored_prod_receipts_replay_as_sentences_with_one_length() -> None:
    """Modes 1-3 on the exact rows prod stored. A receipt keeps the words it was stored
    with (new receipts name the missed moments instead, see the worker test)."""
    failures = [row for row in PROD_RECEIPTS if needs_creator_choice(row)]
    assert [row["requirement_id"] for row in failures] == ["r1", "r3", "r7"]
    assert render_block_recovery(failures).message == (
        "Some of the groups you named are not where you said. "
        f"This draft is about 4.7s; you asked for 14s. {SUFFIX_EN}"
    )


def _length_reasons(*, outro_s: float = 0.0) -> list[dict]:
    """What the real length checker writes for M2's two global length asks (mode 4)."""
    asks = [
        BriefRequirement(
            id="r3",
            kind="timing",
            scope="global",
            description="Freeze the frame for one second on Atakan's bite",
            facts={"duration_s": 1, "freeze_frame": True},
        ),
        BriefRequirement(
            id="r7", kind="timing", scope="global", description="About 14 seconds",
            facts={"duration_s": 14},
        ),
    ]  # fmt: skip
    facts = PlanFacts(duration_s=4.7, rendered_output=True, outro_s=outro_s)
    rows = [receipt.model_dump(mode="json") for receipt in build_receipts(asks, facts)]
    assert [row["reason"] for row in rows][0] != rows[1]["reason"]  # two different sentences
    return rows


@pytest.mark.parametrize(
    ("language", "outro_s", "expected"),
    [
        (None, 0.0, "This draft is about 4.7s; you asked for 14s."),
        (
            None,
            1.6,
            "This draft is about 4.7s (plus a 1.6s outro on the finished video); "
            "you asked for 14s.",
        ),
        ("tr", 0.0, "Bu taslak yaklaşık 4.7 saniye; sen 14 saniye istedin."),
        (
            "tr",
            1.6,
            "Bu taslak yaklaşık 4.7 saniye (bitmiş videoda buna ek olarak 1.6 saniyelik "
            "bir kapanış var); sen 14 saniye istedin.",
        ),
    ],
)
def test_the_draft_length_is_said_once_with_the_last_length_ask(language, outro_s, expected):
    """KRI-777's freeze limitation is separate from the single total-length reason."""
    with reply_language_for(language):
        rows = _length_reasons(outro_s=outro_s)
        message = render_block_recovery([_row("Your order is off.", "r1"), *rows]).message
    suffix = SUFFIX_TR if language == "tr" else SUFFIX_EN
    freeze = (
        "Bu düzenlemede henüz bir kareyi donduramıyorum, o yüzden o an normal akıyor."
        if language == "tr"
        else "I can't freeze a frame in this edit yet, so that moment plays normally."
    )
    assert message == f"Your order is off. {freeze} {expected} {suffix}"


@pytest.mark.parametrize(
    ("reason", "language", "shown"),
    [
        # Mode 2 + 1: lowercase, no stop.
        ("some of the clips moved", None, "Some of the clips moved."),
        # Mode 5: Turkish dotted i.
        ("istediğin yazı yok", "tr", "İstediğin yazı yok."),
        ("ışık değişmedi", "tr", "Işık değişmedi."),
        # Mode 6: a closing quote/bracket after the stop is a sentence already.
        ('Say "continue without a title".', None, 'Say "continue without a title".'),
        ("It reads “Hello.”", None, "It reads “Hello.”"),
        ("None of your lines made it in (intro, outro)", None,
         "None of your lines made it in (intro, outro)."),
        # A reason cut at the receipt's 300 characters ends mid-word: it still ends.
        ("x" * 299 + ",", None, "X" + "x" * 298 + "."),
    ],
)  # fmt: skip
def test_each_reason_is_shown_as_a_sentence(reason, language, shown):
    with reply_language_for(language):
        message = render_block_recovery([_row(reason), _row("Next.", "r2")]).message
    assert message.startswith(f"{shown} Next. ")


def test_exact_repeats_are_still_said_once():
    message = render_block_recovery([_row("Same thing."), _row("same thing", "r2")]).message
    assert message == f"Same thing. {SUFFIX_EN}"


def test_the_title_only_blocker_keeps_its_typed_answer():
    """Mode 7: unchanged by this ticket."""
    block = render_block_recovery([_row(NO_TITLE_REASON)])
    assert block.decline_reason == "needs_choice" and block.field_path == "opening_title"
    assert block.message == (
        "I couldn't make the video yet: you asked for a title but gave no words. "
        'Your draft is saved. Tell me the words for the title, or say "continue without a title".'
    )


def test_a_turkish_chat_rewords_the_english_message_beside_a_format_limit():
    """Mode 8: the worker leaves a format limit (KRI-689: the phone plays clips at natural
    speed) out of the message; the chat must leave it out too, or its English rebuild never
    matches and a Turkish chat keeps the English card."""
    receipts = [
        _row("This draft is ordered by attachment, not the order you asked for.", "r1"),
        _row(brief_checks._NO_CLIP_SPEED, "r2"),
    ]
    stored = render_block_recovery([r for r in receipts if needs_creator_choice(r)]).message
    with reply_language_for("tr"):
        shown = _localized_block_recovery(stored, receipts)
    assert shown.endswith(SUFFIX_TR)
    assert "Your draft" not in shown


# --- Mode 9: every static reason the checkers can block a render with ---

_SOURCE = Path(brief_checks.__file__)
_STATUSES_THAT_BLOCK = {"partial", "not_possible"}


def _static_text(node: ast.expr) -> str | None:
    """A string literal, or an f-string with each placeholder as ``X``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [part.value if isinstance(part, ast.Constant) else "X" for part in node.values]
        return "".join(str(part) for part in parts)
    return None


def _blocking_reason_texts() -> list[str]:
    """Every English and Turkish reason text a not-met receipt is written with, read off the
    checkers' source: literals, ``say(en=..., tr=...)`` and the module's reason constants."""
    tree = ast.parse(_SOURCE.read_text())
    constants = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    followed: set[str] = set()
    texts: list[str] = []

    def collect(node: ast.expr | None) -> None:
        if node is None:
            return
        if isinstance(node, ast.Name) and node.id in constants:
            collect(constants[node.id])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in functions
            and node.func.id not in followed
        ):
            # A reason helper (`_listed_order_miss(facts)`): every sentence it can say.
            followed.add(node.func.id)
            for inner in ast.walk(functions[node.func.id]):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "say"
                ):
                    collect(inner)
        elif isinstance(node, ast.IfExp):
            collect(node.body)
            collect(node.orelse)
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "say"
        ):
            for keyword in node.keywords:
                collect(keyword.value)
        else:
            text = _static_text(node)
            if text is not None:
                texts.append(text)

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_receipt"
            and len(node.args) >= 3
        ):
            continue
        status = node.args[1]
        if isinstance(status, ast.Constant) and status.value not in _STATUSES_THAT_BLOCK:
            continue
        collect(node.args[2])
    # The Turkish twins of the static English reasons (`_loc`).
    texts.extend(brief_checks._REASON_TR.values())
    return [text for text in dict.fromkeys(texts) if text.strip()]


# Starts with a capital (or "iPhone"), ends with its stop and any closing quote/bracket.
_SENTENCE = re.compile(r"^(?:[^a-zçğıöşü]|i[A-Z]).*[.!?…][\"'”’»)\]]*$", re.DOTALL)


def test_the_sweep_finds_the_checkers_reasons():
    texts = _blocking_reason_texts()
    assert len(texts) > 100
    assert "some of the groups you named are not where you said" not in texts
    assert "I couldn't place everything you listed in the order you gave." in texts


@pytest.mark.parametrize("reason", _blocking_reason_texts())
def test_every_blocking_reason_is_shown_as_its_own_sentence(reason):
    language = "tr" if re.search(r"[çğışöüÇĞİŞÖÜ]", reason) else None
    with reply_language_for(language):
        message = render_block_recovery([_row(reason), _row("Next.", "r2")]).message
    head, sep, _rest = message.partition(" Next. ")
    assert sep, message
    assert _SENTENCE.match(head), head
    assert "groups" not in head.casefold() or "group" in reason.casefold()
