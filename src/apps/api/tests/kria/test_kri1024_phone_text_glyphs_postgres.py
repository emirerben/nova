"""KRI-1024 follow-up: every phone format names undrawable creator text before approval.

The first KRI-1024 fix caught a creator literal no phone font can draw only on the unified
phone montage (the KRI-775 preflight). A Talking, Narrated or voiceover-montage draft still
reached the approval card and failed after it: every phone writer styles its text through the
same role-scoped pass (`phone_text_style.resolve_phone_text_style`, which refuses text no
curated phone font draws), and the phone painter refuses any face but the recipe's own and
Apple Color Emoji (`PortableTextVectorPainter-109`). No bundled font draws CJK or Arabic.

These replay the Merope T1 Talking chat (``tests/fixtures/kri787_merope_t1_turns.json``)
through the real runtime (``submit_turn`` + ``run_kria_turn`` over scratch Postgres), with
the Main Creator's call replaced by its recorded output, the creator's title swapped:

- a title holding characters no phone font draws gets a checked "Couldn't" naming the text
  and the characters, and no approval card;
- a flag in the title (color emoji, drawn by the phone's emoji face) changes nothing.

The Plaza T1 Talking run (2026-10-28, job c7430eed) asked for "The Cabildo 🇦🇷"; its title was
lost for another reason (KRI-1009), and once drawn it reaches the shared text pass below.
"""

from __future__ import annotations

import copy
import uuid

import pytest
import pytest_asyncio

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.database import engine as async_engine
from tests.kria.test_kri787_merope_t1_replay import RENDERS, turn1_brief_updates, without_answers
from tests.kria.test_kri787_merope_t1_runtime_postgres import (
    MESSAGES,
    Chat,
    _flags,  # noqa: F401 - the Merope prod flags, autouse
    _pending_approval,
    _seed_t1,
    _turn,
)

TITLE = "Colombian dessert, Ecuadorian hands"
FLAG_TITLE = TITLE + " \U0001f1e8\U0001f1f4"
# "Dessert" in Chinese: no bundled font draws it, and it is not emoji.
UNDRAWABLE_TITLE = TITLE + " 甜点"


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


async def _first_draft(monkeypatch: pytest.MonkeyPatch, title: str) -> tuple[Chat, dict]:
    """Prod's turn 1 with the creator's title written as ``title``; returns its receipts."""
    user_id, thread_id = _seed_t1(cleanup_chosen=False)
    chat = Chat(user_id, thread_id, [])
    strategy = without_answers(RENDERS[0]["draft_strategy"])
    strategy["opening_title"] = title
    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(strategy),
            summary="",
            render_intent_evidence=CreatorRenderIntentEvidence(opening_title=f'Title: "{title}"'),
        ),
        brief_updates=[
            update.model_copy(update={"literal": title}) if update.literal == TITLE else update
            for update in turn1_brief_updates()
        ],
    )
    assert TITLE in MESSAGES[0]
    await _turn(monkeypatch, chat, MESSAGES[0].replace(TITLE, title), output)
    rows = (chat.replies[-1].payload or {}).get("requirement_receipts") or []
    return chat, {row["requirement_id"]: row for row in rows}


def _title_receipt(receipts: dict, title: str) -> dict:
    assert "r6" in receipts, receipts
    return receipts["r6"]


@pytest.mark.asyncio
async def test_a_talking_title_no_phone_font_draws_is_named_before_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, receipts = await _first_draft(monkeypatch, UNDRAWABLE_TITLE)
    receipt = _title_receipt(receipts, UNDRAWABLE_TITLE)
    assert (receipt["status"], receipt["verification"]) == ("not_possible", "checked")
    assert "甜点" in receipt["reason"] and UNDRAWABLE_TITLE in receipt["reason"]
    assert _pending_approval(chat.thread_id) is None, "the approval card must wait"
    assert '"甜点"' in str(chat.replies[-1].content)


@pytest.mark.asyncio
async def test_a_flag_in_the_talking_title_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain_chat, plain = await _first_draft(monkeypatch, TITLE)
    _flag_chat, flagged = await _first_draft(monkeypatch, FLAG_TITLE)
    plain_title = _title_receipt(plain, TITLE)
    flag_title = _title_receipt(flagged, FLAG_TITLE)
    assert "No phone font" not in str(flag_title.get("reason") or "")
    assert (flag_title["status"], flag_title["verification"]) == (
        plain_title["status"],
        plain_title["verification"],
    )


def test_the_shared_phone_text_pass_styles_a_flag_title() -> None:
    """The role-scoped pass every phone writer runs (Talking, Narrated, voiceover, montage),
    with the Plaza T1 Talking title and that job's policy (`{"all_text": {}}`)."""
    from app.services.phone_text_styling import style_rows  # noqa: PLC0415

    policy = {"version": 1, "requested": {}, "preferences": {"all_text": {}}}
    row = {
        "id": "guided-title",
        "text": "The Cabildo \U0001f1e6\U0001f1f7",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "top",
        "size_px": 76,
    }
    styled = style_rows([copy.deepcopy(row)], policy)
    assert styled[0]["text"] == row["text"]
    assert styled[0]["font_family"]


def test_the_shared_phone_text_pass_names_undrawable_text() -> None:
    from app.pipeline.phone_text_style import undrawable_message  # noqa: PLC0415
    from app.services.creator_render_contract import CreatorRenderContractError  # noqa: PLC0415
    from app.services.phone_text_styling import style_rows  # noqa: PLC0415

    policy = {"version": 1, "requested": {}, "preferences": {"all_text": {}}}
    row = {"id": f"guided-title-{uuid.uuid4().hex[:6]}", "text": "The Cabildo 广场"}
    row.update(start_s=0.0, end_s=2.0, position="top", size_px=76)
    with pytest.raises(CreatorRenderContractError) as refused:
        style_rows([row], policy)
    assert str(refused.value) == undrawable_message("The Cabildo 广场", "广场")
