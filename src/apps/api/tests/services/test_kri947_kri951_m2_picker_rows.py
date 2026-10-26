"""KRI-947 + KRI-951: one tap about a moment counts for every row about that moment.

Prod thread 545F53A9 (Don Julio walk-in M2, 2026-10-26): every picker question and answer of
the thread is in ``tests/fixtures/kri947_kri951_don_julio_walkin_m2_picker.json``.

- KRI-947: the creator tapped the video for "the queue" (asked under ``include``); the next
  re-plan filed the queue under ``order`` and asked again. The same happened to the ribeye
  photo: skipped under ``include``/``caption``, re-asked under ``order``.
- KRI-951: one question listed "the butcher holding a cut of beef" twice and "the ribeye
  photo" twice, and the iPhone drew two identical rows, because two intents about the same
  moment (``include`` + ``order``, ``include`` + ``caption``) each got a row.

Ways this can fail, each pinned below:

1. A tap (or skip / "none") under one op is not counted when a re-plan asks the same moment
   under another op, so the creator is asked again (the incident, KRI-947).
2. The question names a moment twice (the incident, KRI-951).
3. Two rows about the same moment stay two rows, so the creator must tap twice, and a tap
   on only one leaves the other to be asked again.
4. Over-sharing: an answer leaks into a different moment, into ``exclude`` (KRI-667: kept
   clips must never become left-out clips) or into ``label`` (an existing pin), or across
   ops through loose word overlap ("the butcher" -> "the butcher's knife").
5. Rows offering different clips are joined: a tap REPLACES the clips, so a clip already
   found for one of them would be lost.
6. Joining rows changes the row count and the resolver's own sanity check silently drops
   the whole picker.
7. Skip or "None of these" on a joined row misses the rows hidden behind it.
8. Rebuilding a joined question (the exclude merge feeds it back in) loses the hidden rows.
9. A long description is cut at a different place under each op prefix, so the same moment
   neither shares nor joins.
10. Wording: one moment left must read as one ("Tap the video if it's in there"); names that
    differ only in capitals or punctuation count once; "and N more" counts names, not rows;
    Turkish too.
11. The title-wording card (KRI-802) stops noticing the title's words when they sit on a
    hidden row.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverOutput
from app.kria import planner
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import (
    build_clip_question,
    clip_question_text,
    fold_clip_selections,
    intent_key,
)
from tests.kria.test_kri802_question_batch import _picker, _wording

_FIXTURE = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "kri947_kri951_don_julio_walkin_m2_picker.json"
    ).read_text()
)
VIDEO = _FIXTURE["video"]
PHOTO = "asset-d17aeec6-0000-4000-8000-000000000001"  # never offered in prod (KRI-949)


def _question(sequence: int) -> dict:
    (event,) = [e for e in _FIXTURE["events"] if e["sequence"] == sequence]
    return event["clip_question"]


def _sent(sequence: int) -> str:
    (event,) = [e for e in _FIXTURE["events"] if e["sequence"] == sequence]
    return event["sent_text"]


def _thread_before(sequence: int) -> list[tuple[str, dict]]:
    """The real picker events (chronological) the creator had sent before ``sequence``."""
    out: list[tuple[str, dict]] = []
    for event in _FIXTURE["events"]:
        if event["sequence"] >= sequence:
            break
        payload = {k: v for k, v in event.items() if k in ("clip_question", "clip_selection")}
        out.append((event["role"], payload))
    return out


def _intent(row: dict, **update) -> ClipIntent:
    return ClipIntent(
        intent_id=row["intent_id"],
        op=row["op"],
        attribute=row["label"],
        creator_text=row.get("creator_text"),
    ).model_copy(update=update)


def _planned(row: dict) -> PlannedClipIntent:
    return PlannedClipIntent(**_intent(row).model_dump(), source_quote=row["label"])


def _tap(question: dict, key: str, media_ids: list[str]) -> tuple[str, dict]:
    return (
        "user",
        {
            "clip_selection": {
                "question_id": question["question_id"],
                "answers": [{"key": key, "media_ids": media_ids}],
            }
        },
    )


# ── 1. KRI-947 over the real thread ───────────────────────────────────────────


def test_prod_queue_tap_answers_the_order_row_of_round_3() -> None:
    round3 = _question(10)
    sel = fold_clip_selections(_thread_before(10))
    by_key = {row["key"]: sel.match(_intent(row)) for row in round3["categories"]}

    queue = by_key["order:the-queue-under-the-striped-awning"]
    assert queue is not None and queue.media_ids == (VIDEO,)  # the seq-7 tap, not asked again
    # The butcher was never answered: still asked.
    assert by_key["include:the-butcher-holding-a-cut-of-beef"] is None
    assert by_key["order:the-butcher-holding-a-cut-of-beef"] is None


def test_prod_photo_skip_answers_the_order_row_of_round_6() -> None:
    (row,) = _question(16)["categories"]
    entry = fold_clip_selections(_thread_before(16)).match(_intent(row))
    assert entry is not None and entry.none  # the seq-13 skip holds: not asked a third time


def test_every_row_the_creator_never_answered_is_still_asked() -> None:
    # Round 4's photo rows were first asked in round 4: nothing before seq 12 answers them.
    sel = fold_clip_selections(_thread_before(12))
    assert all(sel.match(_intent(row)) is None for row in _question(12)["categories"])


# ── 4. Wrong-content controls: the answer never leaks ─────────────────────────


def test_the_queue_answer_never_settles_another_moment_or_a_left_out_or_label_row() -> None:
    sel = fold_clip_selections(_thread_before(10))
    queue = _question(6)["categories"][0]
    assert sel.match(_intent(queue)) is not None  # control: the same moment shares
    others = [
        # another moment whose words overlap the queue (similarity 0.67) under another op
        ClipIntent(
            intent_id="order_butcher",
            op="order",
            attribute=queue["label"].replace("queue", "butcher"),
        ),
        # same words, but "leave it out" (KRI-667) and a per-clip label (existing pin)
        ClipIntent(intent_id="skip_queue", op="exclude", attribute=queue["label"]),
        ClipIntent(intent_id="label_queue", op="label", attribute=queue["label"]),
        # loose word overlap across ops (similarity 0.71 to the queue answer)
        ClipIntent(
            intent_id="caption_queue_sign",
            op="caption",
            attribute="the queue under the striped awning sign board",
            creator_text="Worth the wait",
        ),
    ]
    assert [sel.match(intent) for intent in others] == [None] * len(others)


# ── 2/3/5/8/10. KRI-951 rows and wording over the real questions ──────────────


def test_prod_round_3_asks_the_butcher_once_in_one_row() -> None:
    assert _sent(10).count('"the butcher holding a cut of beef"') == 2  # the incident text
    question = build_clip_question(_question(10)["categories"], question_id="r3")
    rows = question["categories"]

    assert [row["key"] for row in rows] == [
        "include:the-butcher-holding-a-cut-of-beef",
        "order:the-queue-under-the-striped-awning",
    ]
    assert [(h["key"], h["op"], h["intent_id"]) for h in rows[0]["also_answers"]] == [
        ("order:the-butcher-holding-a-cut-of-beef", "order", "order_butcher")
    ]
    assert "also_answers" not in rows[1]
    assert clip_question_text(rows, only_video=True) == (
        'I couldn\'t find "the butcher holding a cut of beef", '
        '"the queue under the striped awning" in your video. '
        "Tap the video for each one that's in it, or mark the ones that aren't."
    )


def test_prod_round_4_asks_the_photo_once_in_one_row_in_both_languages() -> None:
    assert _sent(12).count('"the ribeye photo"') == 2  # the incident text
    question = build_clip_question(_question(12)["categories"], question_id="r4")
    (row,) = question["categories"]

    assert (row["key"], row["label"], row["candidate_media_ids"]) == (
        "include:the-ribeye-photo",
        "the ribeye photo",
        [VIDEO],
    )
    (hidden,) = row["also_answers"]
    assert hidden == {
        "key": "caption:the-ribeye-photo",
        "op": "caption",
        "intent_id": "caption_ribeye_photo",
        "creator_text": "Ojo de bife 🥩",
    }
    assert clip_question_text([row], only_video=True) == (
        'I couldn\'t find "the ribeye photo" in your video. '
        "Tap the video if it's in there, or tell me it isn't."
    )
    with reply_language_for("tr"):
        assert clip_question_text([row], only_video=True) == (
            'Videonda "the ribeye photo" olan yeri bulamadım. '
            "Varsa videoya dokun, yoksa olmadığını söyle."
        )


@pytest.mark.parametrize("how", ["tap", "none", "skip"])
def test_one_answer_on_the_joined_row_answers_every_hidden_row(how: str) -> None:
    question = build_clip_question(_question(12)["categories"], question_id="r4")
    (row,) = question["categories"]
    answer = {
        "tap": {"answers": [{"key": row["key"], "media_ids": [VIDEO]}]},
        "none": {"none_keys": [row["key"]]},
        "skip": {"skipped": True},
    }[how]
    sel = fold_clip_selections(
        [
            ("assistant", {"clip_question": question}),
            ("user", {"clip_selection": {"question_id": "r4", **answer}}),
        ]
    )
    caption = _question(12)["categories"][1]
    later = [
        *(_intent(r, intent_id=f"replanned_{r['op']}") for r in _question(12)["categories"]),
        # The hidden caption row reworded by a later re-plan: only its own identity
        # (intent id + "Ojo de bife 🥩") still binds the answer (KRI-697).
        _intent(caption, attribute="the steak on the plate"),
    ]
    for intent in later:
        entry = sel.match(intent)
        assert entry is not None, intent
        assert entry.media_ids == ((VIDEO,) if how == "tap" else ())
        assert entry.none is (how != "tap")


def test_rows_that_must_stay_apart_are_not_joined() -> None:
    base = _question(12)["categories"][0]  # include: the ribeye photo, offers the video

    def row(op: str, candidates: list[str], **extra) -> dict:
        return {
            **base,
            "op": op,
            "key": f"{op}:the-ribeye-photo",
            "intent_id": f"{op}_ribeye_photo",
            "candidate_media_ids": candidates,
            **extra,
        }

    pairs = {
        # a clip already found for one of them: a joined tap would drop it (failure 5)
        "different clips": [row("include", [VIDEO, PHOTO]), row("order", [VIDEO])],
        "left out": [row("include", [VIDEO]), row("exclude", [VIDEO])],
        "label": [row("include", [VIDEO]), row("label", [VIDEO], creator_text="Ojo de bife")],
    }
    for name, rows in pairs.items():
        question = build_clip_question(rows, question_id="q")
        assert len(question["categories"]) == 2, name
        assert all("also_answers" not in r for r in question["categories"]), name


def test_rebuilding_a_joined_question_keeps_its_hidden_rows() -> None:
    once = build_clip_question(_question(12)["categories"], question_id="r4")
    twice = build_clip_question(once["categories"], question_id="r4")
    assert twice == once


def test_a_long_description_still_shares_and_joins_under_every_op() -> None:
    long = "the " + " ".join(["butcher holding a big cut of beef at the counter"] * 3)
    include = ClipIntent(intent_id="include_butcher", op="include", attribute=long)
    order = ClipIntent(intent_id="order_butcher", op="order", attribute=long)
    assert intent_key(include).partition(":")[2] != intent_key(order).partition(":")[2]
    rows = [
        {
            "key": intent_key(i),
            "label": long,
            "op": i.op,
            "intent_id": i.intent_id,
            "candidate_media_ids": [VIDEO],
        }
        for i in (include, order)
    ]
    question = build_clip_question(rows, question_id="q")
    assert len(question["categories"]) == 1
    sel = fold_clip_selections(
        [
            ("assistant", {"clip_question": {**question, "categories": rows[:1]}}),
            _tap(question, rows[0]["key"], [VIDEO]),
        ]
    )
    assert sel.match(order.model_copy(update={"intent_id": "clip_order_2"})) is not None


def test_wording_counts_moments_not_rows() -> None:
    def rows(*labels: str) -> list[dict]:
        return [{"label": label} for label in labels]

    assert clip_question_text(rows("the ribeye photo", "The ribeye photo."), only_video=True) == (
        'I couldn\'t find "the ribeye photo" in your video. '
        "Tap the video if it's in there, or tell me it isn't."
    )
    many = rows("sign", "cook", "Sign", "flowers", "cook", "plaques")
    assert clip_question_text(many, only_video=True) == (
        'I couldn\'t find "sign", "cook", "flowers", "plaques" in your video. '
        "Tap the video for each one that's in it, or mark the ones that aren't."
    )
    assert clip_question_text(rows("sign", "Sign!")) == (
        'I couldn\'t tell which of your clips show "sign". '
        "Tap the clips that do, or tell me there aren't any."
    )
    with reply_language_for("tr"):
        assert clip_question_text(rows("tabela", "Tabela", "aşçı")) == (
            'Şunlar için hangi klipleri kastettiğini anlayamadım: "tabela", "aşçı". '
            "Her biri için klipleri seç ya da hiç klibi olmayanları işaretle."
        )


def test_title_words_on_a_hidden_row_keep_the_wording_card_alone() -> None:
    title = _wording().plan.choice_question["candidate"]
    rows = [
        {
            "key": f"{op}:the-opening",
            "label": "the opening",
            "op": op,
            "intent_id": f"{op}_opening",
            "candidate_media_ids": [VIDEO],
            **extra,
        }
        for op, extra in (("include", {}), ("caption", {"creator_text": title}))
    ]
    question = build_clip_question(rows, question_id="c")
    assert len(question["categories"]) == 1  # the caption row is hidden behind the include
    wording = _wording()
    assert planner._with_wording_question(wording, _picker(plan={"clip_question": question})) is (
        wording
    )


# ── 1/3/6. The real chain: planner rows -> resolver -> picker -> tap -> next turn ──


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=VIDEO,
            kind="video",
            analysis={"subject": "walk into a steakhouse", "description": "Don Julio, Palermo"},
            gcs_path=f"users/u/clips/{VIDEO}",
        ),
        IntentClip(
            media_id=PHOTO,
            kind="image",
            analysis={"subject": "a grilled ribeye on a plate", "description": "steak"},
            gcs_path=f"users/u/assets/{PHOTO}",
        ),
    ]


def _wire(monkeypatch, rows: list[dict]) -> MagicMock:
    """The planner replays these real rows; the resolver finds nothing on its own."""
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=[_planned(r) for r in rows])
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = MagicMock(return_value=ClipRequestResolverOutput())
    monkeypatch.setattr(
        ClipRequestResolverAgent, "run", lambda self, input, *, ctx=None: resolver(input)
    )
    return resolver


async def _turn(selections=None):
    return await service.plan_and_resolve_clip_intents(
        creator_request=(
            "Six moments in order: the sign, the queue under the striped awning, the butcher "
            "holding a cut of beef, the ribeye photo, the cook, the flowers."
        ),
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )


@pytest.mark.asyncio
async def test_prod_rounds_1_to_3_ask_the_butcher_once_and_never_the_queue_again(
    monkeypatch,
) -> None:
    # Round 1: the queue, under include.
    _wire(monkeypatch, _question(6)["categories"])
    first = (await _turn()).resolution
    assert first.needs_creator
    (row,) = first.clip_question["categories"]
    assert (row["key"], row["candidate_media_ids"]) == (
        "include:the-queue-under-the-striped-awning",
        [VIDEO],
    )
    thread = [("assistant", {"clip_question": first.clip_question})]
    thread.append(_tap(first.clip_question, row["key"], [VIDEO]))  # the seq-7 tap

    # Round 3: the real re-plan (queue under order, butcher under include AND order).
    _wire(monkeypatch, _question(10)["categories"])
    third = (await _turn(fold_clip_selections(thread))).resolution
    settled = {i.intent_id: i for i in third.intents if i.status == "resolved"}
    assert settled["order_queue"].media_ids() == [VIDEO]
    (butcher,) = third.clip_question["categories"]  # one row, not two (failure 6: not dropped)
    assert butcher["key"] == "include:the-butcher-holding-a-cut-of-beef"
    assert [h["key"] for h in butcher["also_answers"]] == [
        "order:the-butcher-holding-a-cut-of-beef"
    ]
    asked = {i.question for i in third.intents if i.status != "resolved"}
    assert asked == {
        'I couldn\'t find "the butcher holding a cut of beef" in your video. '
        "Tap the video if it's in there, or tell me it isn't."
    }

    # One tap on the one row settles both butcher intents; nothing is asked any more.
    thread += [("assistant", {"clip_question": third.clip_question})]
    thread.append(_tap(third.clip_question, butcher["key"], [VIDEO]))
    resolver = _wire(monkeypatch, _question(10)["categories"])
    fourth = (await _turn(fold_clip_selections(thread))).resolution
    resolver.assert_not_called()
    assert not fourth.needs_creator and fourth.clip_question is None
    assert {i.intent_id: i.media_ids() for i in fourth.intents} == {
        "include_butcher": [VIDEO],
        "order_queue": [VIDEO],
        "order_butcher": [VIDEO],
    }
