"""KRI-591: a title hold the creator never stated is dropped before the KRI-476 question.

Prod thread f77a5c16 (turn 4cb618ca, 2026-10-11): the only title ask was
`Başlık: "Tigre'de bir gün"` -- no seconds anywhere -- yet Main Creator v53 set
`opening_title_duration_s = 3`, the phone Voiceover renderer cannot hold a title for an
exact time, and the KRI-476 check asked "Bu düzenleme başlığını tam 3 saniye tutamıyor.
Daha sade bir sürüm yapayım mı?" about a length nobody asked for.

Failure modes (written before the code):

* a hold the creator never stated (no number in their words) reaches the KRI-476 check and
  is asked about;
* a hold the creator DID state ("başlık 2 saniye kalsın", "for 2.5 seconds") is dropped, so
  the KRI-476 question (or the phone Talking title's exact hold) silently stops working;
* a number inside the on-screen copy ("ROME IN 3 DAYS", "Top 3") grounds a 3 s hold;
* evidence that is not the creator's own words (a model paraphrase, or Kria's own "tam 3
  saniye" question echoed back) grounds the hold;
* smart quotes / casing / spacing between the excerpt and the message cause a false drop;
* a decimal comma ("2,5 saniye") does not match 2.5;
* the evidence names a different number ("7 klibin hepsini kullan") than the hold;
* a strategy without a hold changes at all.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.kria import planner
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy
from tests.kria.test_strategy_policy import (
    _narrated,
    _narrated_manifest,
)

# The creator's first message in thread f77a5c16, verbatim (smart quotes as typed).
TIGRE_REQUEST = (
    "Seslendirmem Türkçe ama altyazılar İngilizce olsun. Başlık: “Tigre’de bir gün”. "
    "Nehir klibiyle başla. “Deniz” dediğimde Deniz’i laptop başında göster. “kırkayak” "
    "dediğimde küvet fotoğrafını göster. “mutfak” dediğimde mutfağı göster. Başlık dışında "
    "başka yazı olmasın."
)
TIGRE_TITLE = "Tigre’de bir gün"
HALT = "(test: the strategy check passed)"


def _inputs(*user_messages: str) -> SimpleNamespace:
    conversation = [{"role": "user", "content": text} for text in user_messages]
    return SimpleNamespace(
        agent_input=SimpleNamespace(conversation=conversation),
        creative_copy_state={},
        creative_copy_digest="d" * 24,
    )


async def plan_until_strategy_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest,  # noqa: ANN001
    strategy: CreativeStrategy,
    evidence: CreatorRenderIntentEvidence,
    user_message: str,
    history: tuple[str, ...] = (),
):
    """Run `_plan_creator_action` (brief binding + choice questions on, a Turkish chat)
    up to the REAL `check_strategy_for_runtime_v2`, then stop.

    Returns the plan, the strategy that reached the check, and the check's own result. A
    check that passes is turned into a sentinel question so nothing after it (clip
    intents, the song gate) runs.
    """

    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    seen: list = []
    real_check = planner.check_strategy_for_runtime_v2

    def _check(manifest, strategy, **kwargs):  # noqa: ANN001, ANN003, ANN202
        checked = real_check(manifest, strategy, **kwargs)
        seen.append((strategy, checked))
        if isinstance(checked, CheckedStrategy):
            return RefusedStrategy(question=HALT, code="test_halt")
        return checked

    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", _check)
    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=strategy,
            summary="Seslendirmeli bir düzenleme.",
            render_intent_evidence=evidence,
        )
    )
    with reply_language_for("tr"):
        result = await planner._plan_creator_action(
            None,
            thread_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            user_message=user_message,
            manifest=manifest,
            inputs=_inputs(*history, user_message),
            output=output,
        )
    [(checked_strategy, check_result)] = seen
    return result, checked_strategy, check_result


async def _plan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_message: str,
    hold: float | None,
    hold_evidence: str | None,
    title: str = TIGRE_TITLE,
    title_evidence: str = "Başlık: “Tigre’de bir gün”",
    history: tuple[str, ...] = (),
):
    strategy = _narrated(
        opening_title=title,
        **({"opening_title_duration_s": hold} if hold is not None else {}),
    )
    evidence = CreatorRenderIntentEvidence(
        opening_title=title_evidence,
        **({"opening_title_duration_s": hold_evidence} if hold_evidence else {}),
    )
    result, checked_strategy, _ = await plan_until_strategy_check(
        monkeypatch,
        manifest=_narrated_manifest(),
        strategy=strategy,
        evidence=evidence,
        user_message=user_message,
        history=history,
    )
    return result, checked_strategy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hold_evidence",
    [None, "Başlık: “Tigre’de bir gün”"],
    ids=["no-evidence", "title-excerpt-as-evidence"],
)
async def test_tigre_title_without_seconds_is_not_asked_about(
    prod_profile, monkeypatch, hold_evidence
) -> None:
    result, checked = await _plan(
        monkeypatch, user_message=TIGRE_REQUEST, hold=3.0, hold_evidence=hold_evidence
    )

    assert checked.opening_title_duration_s is None
    assert checked.opening_title == TIGRE_TITLE
    assert "saniye" not in (result.plan.response or "")
    assert result.plan.response == HALT


@pytest.mark.asyncio
async def test_kria_echoing_its_own_seconds_question_is_not_the_creators_words(
    prod_profile, monkeypatch
) -> None:
    """The next turn's conversation carries Kria's "tam 3 saniye" line; it is not a source."""

    result, checked = await _plan(
        monkeypatch,
        user_message="Evet baslik suresi onemli degil",
        hold=3.0,
        hold_evidence="Bu düzenleme başlığını tam 3 saniye tutamıyor.",
        history=(TIGRE_REQUEST,),
    )

    assert checked.opening_title_duration_s is None
    assert result.plan.response == HALT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "hold", "evidence", "spoken"),
    [
        (
            "Başlık: “Tigre’de bir gün”, başlık 2 saniye kalsın.",
            2.0,
            "başlık 2 saniye kalsın",
            "tam 2 saniye",
        ),
        (
            "Başlık: “Tigre’de bir gün”. Başlık ilk 2,5 saniyede görünsün.",
            2.5,
            "Başlık ilk 2,5 saniyede görünsün.",
            "tam 2.5 saniye",
        ),
    ],
)
async def test_a_stated_hold_still_reaches_the_kri476_question(
    prod_profile, monkeypatch, message, hold, evidence, spoken
) -> None:
    result, checked = await _plan(
        monkeypatch, user_message=message, hold=hold, hold_evidence=evidence
    )

    assert checked.opening_title_duration_s == hold
    assert spoken in result.plan.response
    assert result.plan.response.endswith("?")


def test_grounding_reads_the_creators_own_seconds() -> None:
    sources = [
        "Open with 'ROME IN 3 DAYS' for two seconds. Use all 7 clips.",
        "Baslik uc saniye kalsin, sonra 1.5s bekle",
    ]

    def grounded(hold: float, evidence: str | None) -> bool:
        return planner.title_hold_grounded(
            hold, evidence=evidence, user_sources=sources, strip=("ROME IN 3 DAYS",)
        )

    assert grounded(2.0, "Open with 'ROME IN 3 DAYS' for two seconds.")
    # Turkish typed without diacritics, and a unit glued to the number.
    assert grounded(3.0, "Baslik uc saniye kalsin")
    assert grounded(1.5, "sonra 1.5s bekle")
    # A number inside the title copy is not a hold.
    assert not grounded(3.0, "Open with 'ROME IN 3 DAYS' for two seconds.")
    # A number the excerpt does not contain, or a count that is not seconds.
    assert not grounded(7.0, "Open with 'ROME IN 3 DAYS' for two seconds.")
    assert not grounded(7.0, "Use all 7 clips.")
    # Not the creator's words (a paraphrase), or no excerpt at all.
    assert not grounded(2.0, "Hold the title for 2 seconds.")
    assert not grounded(2.0, None)
    assert not grounded(2.0, "")
    # Smart quotes, casing and spacing are not a reason to drop a real hold.
    assert planner.title_hold_grounded(
        2.0,
        evidence="TITLE ‘Lisbon’  for 2 SECONDS",
        user_sources=["title 'Lisbon' for 2 seconds"],
    )
    assert planner.title_hold_grounded(
        2.0, evidence="BAŞLIK 2 SANİYE KALSIN", user_sources=["BAŞLIK 2 SANİYE KALSIN!"]
    )


@pytest.mark.parametrize(
    ("title", "evidence", "hold", "kept"),
    [
        # PR 1548 review: the title's own text also occurs inside the stated seconds.
        ("3", 'Keep the title "3" for 3 seconds', 3.0, True),
        ("one", 'Show "one" for one second', 1.0, True),
        ("two", 'Put "two" on screen for two seconds', 2.0, True),
        ("GO", 'Show "GO" for two seconds', 2.0, True),
        # Seconds that are part of the on-screen copy are still not a hold.
        ("3 SECONDS TO MIDNIGHT", 'Open with "3 SECONDS TO MIDNIGHT"', 3.0, False),
        ("3 seconds", 'Title "3 seconds" for 2 seconds', 2.0, True),
        ("3 seconds", 'Title "3 seconds" for 2 seconds', 3.0, False),
        # PR 1548 recheck: the title is itself a seconds expression, and the creator states
        # the same seconds again as the hold. Only the copy's own occurrence is skipped.
        ("3 seconds", 'Keep the title "3 seconds" for 3 seconds', 3.0, True),
        ("one second", 'Show "one second" for one second', 1.0, True),
        ("3 seconds", "title 3 seconds for 3 seconds", 3.0, True),
        ("3 seconds", "Show “3 seconds” on screen", 3.0, False),
    ],
    ids=[
        "digit-title",
        "one-title",
        "two-title",
        "control",
        "copy-only",
        "copy-and-hold",
        "copy-hold-mismatch",
        "seconds-title-and-same-hold",
        "word-seconds-title-and-same-hold",
        "unquoted-title-then-hold",
        "seconds-title-only",
    ],
)
def test_a_title_that_matches_the_seconds_keeps_the_stated_hold(
    title: str, evidence: str, hold: float, kept: bool
) -> None:
    assert (
        planner.title_hold_grounded(
            hold, evidence=evidence, user_sources=[evidence], strip=(title,)
        )
        is kept
    )


@pytest.mark.parametrize(
    ("evidence", "hold", "kept"),
    [
        # PR 1548 external review: ordinary half-second forms within the 0.5-10 s schema.
        ("Hold the title for a second and a half", 1.5, True),
        ("Başlık iki buçuk saniye kalsın", 2.5, True),
        ("baslik iki bucuk saniye kalsin", 2.5, True),
        ("title for .5 s", 0.5, True),
        ("title for one and a half seconds", 1.5, True),
        ("title for 2 and a half seconds", 2.5, True),
        ("title for two seconds and a half", 2.5, True),
        ("title for half a second", 0.5, True),
        ("başlık yarım saniye", 0.5, True),
        ("başlık bir buçuk saniye kalsın", 1.5, True),
        ("başlık 3 buçuk saniye kalsın", 3.5, True),
        # Controls the review confirmed, and the half must change the value.
        ("title for 0.5 s", 0.5, True),
        ("title for 2 seconds", 2.0, True),
        ("başlık 2,5 saniye", 2.5, True),
        ("title for 2 and a half seconds", 2.0, False),
        ("Başlık iki buçuk saniye kalsın", 2.0, False),
    ],
)
def test_ordinary_half_second_holds_are_the_creators_words(
    evidence: str, hold: float, kept: bool
) -> None:
    assert planner.title_hold_grounded(hold, evidence=evidence, user_sources=[evidence]) is kept


@pytest.mark.parametrize(
    ("evidence", "hold"),
    [
        # PR 1548 external review: ordinary words that are not a duration.
        ("Use the bright ones", 1.0),
        ("Put the title on second clip", 10.0),
        ("Put the title on the second clip", 10.0),
        ("two shots of the river", 2.0),
        ("Show the title on seconds 3 to 5", 10.0),
        ("iki saniyelik", 3.0),
    ],
)
def test_ordinary_words_are_not_a_title_hold(evidence: str, hold: float) -> None:
    assert not planner.title_hold_grounded(hold, evidence=evidence, user_sources=[evidence])


@pytest.mark.parametrize(
    ("evidence", "hold"),
    [
        # Compact digit forms and spaced or hyphenated words stay supported.
        ("title for 2s", 2.0),
        ("title for .5s", 0.5),
        ("title for 2.5s", 2.5),
        ("a 3-second title", 3.0),
        ("a two-second title", 2.0),
        ("title for two seconds", 2.0),
        ("başlık on saniye kalsın", 10.0),
        ("başlık iki sn", 2.0),
    ],
)
def test_compact_and_spelled_holds_are_still_read(evidence: str, hold: float) -> None:
    assert planner.title_hold_grounded(hold, evidence=evidence, user_sources=[evidence])


@pytest.mark.parametrize(
    ("title", "evidence", "hold"),
    [
        ("ONE AND A HALF SECONDS", 'Open with "ONE AND A HALF SECONDS"', 1.5),
        ("Yarım saniye", 'Başlık: "Yarım saniye"', 0.5),
        ("İki buçuk saniye", 'Başlık: "İki buçuk saniye"', 2.5),
    ],
)
def test_half_second_words_inside_the_title_are_not_a_hold(
    title: str, evidence: str, hold: float
) -> None:
    assert not planner.title_hold_grounded(
        hold, evidence=evidence, user_sources=[evidence], strip=(title,)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hold_evidence", "kept"),
    [("for the first 2 seconds", 2.0), (None, None)],
    ids=["stated", "invented"],
)
async def test_the_phone_talking_title_keeps_only_a_stated_hold(
    prod_profile, monkeypatch, hold_evidence, kept
) -> None:
    """KRI-467: the phone Talking title is held for exactly the confirmed seconds, read
    from the approved strategy; an invented hold falls back to the first-word timing."""

    from tests.kria.test_strategy_policy import _talking, _talking_manifest

    message = "Put “3 sourdough mistakes” on top for the first 2 seconds, karaoke captions."
    result, _strategy, checked = await plan_until_strategy_check(
        monkeypatch,
        manifest=_talking_manifest(),
        strategy=_talking(
            caption_style="karaoke",
            opening_title="3 sourdough mistakes",
            opening_title_duration_s=2.0,
        ),
        evidence=CreatorRenderIntentEvidence(
            opening_title="Put “3 sourdough mistakes” on top",
            **({"opening_title_duration_s": hold_evidence} if hold_evidence else {}),
        ),
        user_message=message,
    )

    assert result.plan.response == HALT
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "3 sourdough mistakes"
    assert checked.strategy.opening_title_duration_s == kept
