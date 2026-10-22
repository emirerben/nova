"""KRI-795 sweep: every refusal the render contract raises for a decision the creator must make
reaches the chat in plain words with a way forward that exists, in English and in Turkish.

The full failure-mode list is in `test_kri795_choice_refusal_postgres.py` (written before the
code). Swept here over every `needs_choice` / `requirement_conflict` refusal, each raised by the
real contract code (no hand-copied sentence):

* F1 the card blames the iPhone or sends the creator to another clip or format
* F2 "Tell me which option you want" with no options shown
* F3 the unresolved item named in contract words ("ordering rule", "approved media")
* F10 a Turkish chat gets the worker's English, or loses the reason
* F14 requirement_conflict refusals blame the iPhone
* F15 the way forward appears twice
* F16 a device-record refusal says "which option" with no options
* the plain-words table drifts from what the contract writes (a key no refusal produces)
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.reply_language import detect_chat_language, reply_language_for
from app.services.clip_order_sequence import unclear_sequence_words
from app.services.creator_render_contract import (
    _SEQUENCE_UNRESOLVED,
    CreatorRenderContractError,
    build_render_contract,
    check_phone_dispatch_contract,
    decline_payload,
    unresolved_decline,
)
from app.services.device_render import (
    device_record,
    mark_device_failed,
    record_contract_decline,
    save_device_record,
)
from app.tasks import kria_runtime
from app.tasks.kria_runtime import _UNRESOLVED_PLAIN
from tests.kria.test_choice_conflict_gate import _brief, _rows
from tests.kria.test_runtime_phone_v2 import (
    VARIANT,
    _declined_job,
    _device_job,
    _observe,
    _observe_refusal,
    _publish,
)

GENERATION = "gen-1"


def _order(**facts: object) -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="clips 1, 2, 3 in that sequence",
        facts=facts,
    )


def _dispatch(strategy: dict | None, brief, rows, **check) -> CreatorRenderContractError:  # noqa: ANN001
    """Build the contract the way dispatch does and run the dispatch gate; return the refusal."""
    try:
        contract = build_render_contract(
            strategy,
            generation_id=GENERATION,
            brief=brief,
            media_snapshot={"clip_assignments": rows},
        )
        check_phone_dispatch_contract(
            contract,
            snapshot_generation_id=check.pop("generation", GENERATION),
            has_voiceover_candidate=check.pop("has_voiceover", False),
            **check,
        )
    except CreatorRenderContractError as exc:
        return exc
    raise AssertionError("the contract did not refuse")


def _written_in_turkish(
    make: Callable[[], CreatorRenderContractError],
) -> CreatorRenderContractError:
    with reply_language_for("tr"):
        return make()


_ATTACHMENT_ANSWER = {
    "choice_answers": [
        {
            "conflict": "order_basis",
            "kind": "order_basis",
            "option": "attachment_order",
            "input_digest": "d",
            "requirement_ids": ["r2"],
            "source": "creator",
        }
    ]
}

# Every refusal that needs the creator's decision, raised by the real contract code.
REFUSALS: dict[str, Callable[[], CreatorRenderContractError]] = {
    # T1: a rule the media cannot prove.
    "order_rule": lambda: _dispatch(None, _brief(_order()), _rows(3)),
    "capture_times": lambda: _dispatch(
        None, _brief(_order(key="capture_time")), _rows(3, dated=False)
    ),
    "clips_unknown": lambda: _dispatch(
        _ATTACHMENT_ANSWER, _brief(_order(key="attachment")), [{"kind": "video"}]
    ),
    "pin_not_found": lambda: _dispatch(
        None, _brief(_order(first_clip="Caro's tray clip")), _rows(3)
    ),
    "text_clip_unknown": lambda: _dispatch(
        {"shot_labels": []},
        _brief(
            BriefRequirement(
                id="r3",
                kind="text",
                scope="per_clip",
                literal="Mutfak",
                description="the kitchen clip",
            )
        ),
        _rows(3),
    ),
    # A dispatch bound to a Turkish chat (KRI-765) writes the quoted sentence in Turkish.
    "pin_not_found_tr": lambda: _written_in_turkish(
        lambda: _dispatch(None, _brief(_order(first_clip="Caro'nun tepsi klibi")), _rows(3))
    ),
    "sequence_words": lambda: unresolved_decline(unclear_sequence_words("the sunset ones")),
    "first_or_last": lambda: unresolved_decline(_SEQUENCE_UNRESOLVED),
    "voice_missing": lambda: _dispatch({"audio_strategy": "voiceover"}, None, _rows(1)),
    "other_revision": lambda: _dispatch(None, _brief(), _rows(1), generation="gen-2"),
    "two_soundtracks": lambda: _dispatch(
        {"montage_audio": {"preserve_source_audio": True, "source_media_ids": ["c00"]}},
        None,
        _rows(3),
        user_song={"song_id": "s1"},
    ),
}

_DEAD_ENDS = (
    "can't render yet",  # F1
    "different clip or format",  # F1
    "Tell me which option you want",  # F2
    "Hangi seçeneği istediğini söyle",  # F2, its Turkish twin
    "ordering rule",  # F3
    "approved media",  # F3
)


@pytest.fixture(autouse=True)
def _choices_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


@pytest.fixture(params=["en", "tr"])
def language(request, monkeypatch: pytest.MonkeyPatch) -> str:  # noqa: ANN001
    monkeypatch.setattr(kria_runtime, "thread_reply_language", lambda _thread: request.param)
    return request.param


def _assert_readable(content: str, exc: CreatorRenderContractError, language: str) -> None:
    for wrong in _DEAD_ENDS:
        assert wrong not in content, content
    assert content.rstrip().endswith((".", "?")), content
    if language == "tr":
        assert detect_chat_language(content) == "tr", content  # F10
        if detect_chat_language(str(exc)) != "tr":
            assert str(exc) not in content, content  # F10: the worker's English never leaks
    elif detect_chat_language(str(exc)) == "tr":
        assert str(exc) not in content, content  # an English chat never shows the Turkish
    alternative = getattr(exc, "alternative", None)
    if alternative:
        assert content.count(alternative) <= 1, content  # F15


@pytest.mark.parametrize("source", sorted(REFUSALS))
def test_every_dispatch_refusal_that_needs_a_decision_is_plain_and_has_a_way_forward(
    source: str, language: str
) -> None:
    exc = REFUSALS[source]()
    assert exc.decline_reason in {"needs_choice", "requirement_conflict"}, source
    job = _declined_job(
        "phone_plan_unsupported",
        decline={**decline_payload(exc), "failure_reason": "phone_plan_unsupported"},
        detail=str(exc),
    )
    _, execution, events = _observe(job, {})

    assert (execution.error["recovery"], execution.error["retryable"]) == ("ask_user", False)
    assert events[-1]["payload"]["decline_reason"] == exc.decline_reason
    _assert_readable(events[-1]["content"], exc, language)


@pytest.mark.parametrize("source", sorted(REFUSALS))
def test_every_device_refusal_that_needs_a_decision_is_plain_and_has_a_way_forward(
    source: str, language: str
) -> None:
    """F16: the same refusal recorded on a phone render's record at publication."""
    exc = REFUSALS[source]()
    job = _device_job()
    _publish(job)
    job.assembly_plan["variants"][0]["render_status"] = "awaiting_device"
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "syncing"
    save_device_record(job, VARIANT, record)
    record_contract_decline(job, VARIANT, exc, stage="publication")
    mark_device_failed(job, VARIANT, reason_code="unsupported_recipe", detail=str(exc))

    _, execution, events = _observe_refusal(job)

    assert (execution.error["recovery"], execution.error["retryable"]) == ("ask_user", False)
    content = events[-1]["content"]
    _assert_readable(content, exc, language)
    assert ("not applied" if language == "en" else "uygulanmadı") in content


def test_the_plain_words_table_only_holds_sentences_the_contract_writes() -> None:
    written = {str(make()) for make in REFUSALS.values()}
    assert set(_UNRESOLVED_PLAIN) <= written


def test_the_t1_refusal_reads_as_the_order_problem_and_what_to_say() -> None:
    exc = REFUSALS["order_rule"]()
    assert str(exc) == "I can't verify this ordering rule from the approved media."  # prod text
    job = _declined_job(
        "phone_plan_unsupported",
        decline={**decline_payload(exc), "failure_reason": "phone_plan_unsupported"},
        detail=str(exc),
    )
    _, _, events = _observe(job, {})
    assert events[-1]["content"] == (
        "I couldn't keep the clip order you asked for. Tell me how you want the clips "
        "ordered, and I'll make a new draft."
    )


def test_a_quoted_refusal_keeps_the_creators_words_in_both_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = REFUSALS["pin_not_found"]()
    job = _declined_job(
        "phone_plan_unsupported",
        decline={**decline_payload(exc), "failure_reason": "phone_plan_unsupported"},
        detail=str(exc),
    )
    _, _, events = _observe(job, {})
    assert events[-1]["content"] == (
        "I couldn't tell which clip \"Caro's tray clip\" means, so I can't confirm the order. "
        "Tell me which clip you mean, and I'll make a new draft."
    )
    monkeypatch.setattr(kria_runtime, "thread_reply_language", lambda _thread: "tr")
    _, _, events = _observe(job, {})
    assert events[-1]["content"] == (
        '"Caro\'s tray clip" derken hangi klibi kastettiğini anlayamadım, o yüzden sırayı '
        "doğrulayamıyorum. Hangi klibi kastettiğini söyle, yeni bir taslak hazırlayayım."
    )
