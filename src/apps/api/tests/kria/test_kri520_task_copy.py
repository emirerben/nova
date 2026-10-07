"""KRI-520: the copy the Kria tasks write themselves follows the chat's language.

English stays byte-identical (it is the old string, pinned here); a Turkish chat gets
Turkish. Every table of fixed copy has a Turkish line for each of its keys, so a code
added later cannot silently stay English in a Turkish chat.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.kria import registry
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import current_reply_language, reply_language_for
from app.tasks import kria_runtime as kr
from app.tasks.content_plan_build import JOB_FAILURE_MESSAGES, PHONE_GATE_MESSAGES

_NON_ASCII_TR = set("çğıöşüÇĞİÖŞÜ")


def _turkish(text: str) -> bool:
    """Crude but enough: Turkish copy here always carries at least one Turkish letter."""
    return any(ch in _NON_ASCII_TR for ch in text)


# -- the tables carry a Turkish line for every key ------------------------------------


def test_every_dispatch_refusal_has_turkish_copy() -> None:
    outcomes = set(kr._SPEECH_CLEANUP_DISPATCH_REFUSALS) | set(kr._VISUALS_DISPATCH_REFUSALS)
    assert outcomes == set(kr._DISPATCH_REFUSALS_TR)
    for outcome in outcomes:
        with reply_language_for("tr"):
            assert _turkish(kr._dispatch_refusal_copy(outcome))
        english = kr._SPEECH_CLEANUP_DISPATCH_REFUSALS.get(
            outcome
        ) or kr._VISUALS_DISPATCH_REFUSALS.get(outcome)
        assert kr._dispatch_refusal_copy(outcome) == english
    assert kr._dispatch_refusal_copy("publish_failed") is None


def test_every_phone_gate_reason_has_turkish_copy() -> None:
    assert set(PHONE_GATE_MESSAGES) == set(kr._PHONE_GATE_MESSAGES_TR)
    for reason, (_code, english) in PHONE_GATE_MESSAGES.items():
        assert kr._phone_gate_refusal_copy(reason).startswith(english)
        with reply_language_for("tr"):
            turkish = kr._phone_gate_refusal_copy(reason)
        assert turkish.startswith(kr._PHONE_GATE_MESSAGES_TR[reason])
        assert english not in turkish


def test_every_job_failure_code_has_turkish_copy() -> None:
    assert set(JOB_FAILURE_MESSAGES) == set(kr._JOB_FAILURE_MESSAGES_TR)
    for code, english in JOB_FAILURE_MESSAGES.items():
        assert kr._job_failure_copy(code, None) == english
        with reply_language_for("tr"):
            assert kr._job_failure_copy(code, None) == kr._JOB_FAILURE_MESSAGES_TR[code]
    # An unmapped code still gets a real sentence in both languages; None stays None.
    assert kr._job_failure_copy("some_new_code", None).startswith("Something went wrong")
    with reply_language_for("tr"):
        assert kr._job_failure_copy("some_new_code", None) == kr._JOB_FAILURE_DEFAULT_TR
        assert kr._job_failure_copy(None, None) is None


def test_every_device_edit_refusal_code_has_turkish_copy() -> None:
    assert set(kr._DEVICE_EDIT_REFUSALS) == set(kr._DEVICE_EDIT_REFUSALS_TR)
    for code in [*kr._DEVICE_EDIT_REFUSALS, "something_unmapped"]:
        english = kr._DEVICE_EDIT_REFUSALS.get(code, kr._DEVICE_EDIT_REFUSAL_FALLBACK)
        assert kr._device_edit_refusal_copy(code) == english
        with reply_language_for("tr"):
            assert _turkish(kr._device_edit_refusal_copy(code))


# -- refusal copy ---------------------------------------------------------------------


def test_device_refusal_copy_is_the_old_english_and_a_turkish_sibling() -> None:
    decline = {"decline_reason": "capability_unavailable"}
    old_english = (
        "This render path can't keep that requirement. "
        "Tell me what you'd like to change and I'll try a different approach. "
        "That edit was not applied. Your last good version is still available."
    )
    assert kr._device_refusal_copy(decline) == old_english
    assert kr._device_refusal_copy(decline, last_good=False).endswith("Nothing was published.")

    other = {"decline_reason": "evidence_missing", "message": "Clip 3 has no speech."}
    assert kr._device_refusal_copy(other) == (
        "Clip 3 has no speech. That edit was not applied. Your last good version is still "
        "available. Tell me to redo it and I'll make a new version from what you already "
        "approved."
    )

    with reply_language_for("tr"):
        turkish = kr._device_refusal_copy(decline)
        assert turkish == (
            "Bu video hazırlama yolu bu isteği koruyamıyor. "
            "Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim. "
            "Bu düzenleme uygulanmadı. Son iyi sürümün hâlâ duruyor."
        )
        # The contract's own message is carried as written.
        assert kr._device_refusal_copy(other).startswith("Clip 3 has no speech. Bu düzenleme")


def test_phone_gate_refusal_ends_with_the_way_forward_in_each_language() -> None:
    assert kr._phone_gate_refusal_copy("not_a_reason") == (
        "That kind of edit isn't available for iPhone renders yet. "
        "Tell me what you'd like to change and I'll try a different approach."
    )
    with reply_language_for("tr"):
        assert kr._phone_gate_refusal_copy("not_a_reason") == (
            "Bu tür bir düzenleme iPhone'da henüz kullanılamıyor. "
            "Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim."
        )


# -- the review and the song note -----------------------------------------------------


def test_render_ready_review_is_byte_identical_english_and_turkish_for_a_turkish_chat() -> None:
    assert kr._render_ready_review_copy("original_text") == (
        "The original text cut is ready. The approved render finished; review the opening, "
        "pacing, and text, then tell me what you want changed."
    )
    with reply_language_for("tr"):
        assert kr._render_ready_review_copy("original_text").startswith(
            "Orijinal sesli versiyon hazır."
        )
        # An id the table doesn't know is left out, never mixed in as an English word.
        assert kr._render_ready_review_copy("surprise_cut").startswith("Videon hazır.")
        assert "surprise" not in kr._render_ready_review_copy("surprise_cut")


def _song_job(receipt: dict) -> SimpleNamespace:
    return SimpleNamespace(assembly_plan={"unified_montage": {"user_song": receipt}})


def test_user_song_note_english_unchanged_and_turkish_for_each_case() -> None:
    receipt = {
        "fallback_reason": "no_lyrics",
        "kept_broll_ids": ["a", "b"],
        "low_confidence_ids": ["c"],
        "placed_outside_ids": ["d", "e", "f"],
        "placed": [{"method": "lyrics"}, {"method": "lyrics"}, {"method": "time"}],
    }
    english = kr._user_song_note(_song_job(receipt))
    assert english == (
        "I couldn't find where your takes sit in the song, so I used it as background music "
        "cut to the beat. To lip-sync, play the song out loud while filming, or sing along "
        "clearly so I can match your words (earbuds work, but the sync is a bit looser). "
        "2 takes have no usable singing or words, so they are in as short muted clips. "
        "Trim or remove them in the editor. "
        "1 take is placed by my best guess and may be slightly off; check it in the editor. "
        "3 takes sit later in the song than a 2-minute video can hold. "
        "I matched 2 takes by your singing."
    )
    with reply_language_for("tr"):
        turkish = kr._user_song_note(_song_job(receipt))
    assert turkish.count(". ") >= 4
    assert "2 çekimde kullanılabilir şarkı ya da söz yok" in turkish
    assert "1 çekimi tahminime göre yerleştirdim" in turkish
    assert "3 çekim şarkıda" in turkish
    assert "2 çekimi söylediğin sözlere göre eşleştirdim." in turkish
    assert "take" not in turkish


# -- plan guards and receipts ---------------------------------------------------------


def test_paraphrase_guard_replacement_follows_the_chat_language() -> None:
    echo = PlannedKriaTurn(
        KriaTurnPlan(
            mode="respond", turn_value="question", response="You want a fast matcha launch video."
        ),
        "manifest",
        "context",
    )
    english = kr._useful_plan(echo, user_message="Make a fast matcha launch video").plan.response
    assert english == (
        "I need one concrete creative choice before I can make a useful edit decision. "
        "Which moment should viewers remember?"
    )
    with reply_language_for("tr"):
        turkish = kr._useful_plan(echo, user_message="Make a fast matcha launch video").plan
    assert turkish.response.startswith("Anlamlı bir düzenleme kararı")
    assert turkish.turn_value == "question"

    act = PlannedKriaTurn(
        KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                {
                    "intent_id": "apply-ops",
                    "tool_name": "draft.apply_editor_ops",
                    "tool_version": 1,
                    "arguments": {
                        "summary": "You want a fast matcha launch video.",
                        "operations": [{"op": "a"}, {"op": "b"}],
                    },
                }
            ],
        ),
        "manifest",
        "context",
    )
    assert (
        kr._useful_plan(act, user_message="Make a fast matcha launch video")
        .plan.intents[0]
        .arguments["summary"]
        == "I prepared 2 reversible editor changes."
    )
    with reply_language_for("tr"):
        guarded = kr._useful_plan(act, user_message="Make a fast matcha launch video")
    assert guarded.plan.intents[0].arguments["summary"] == (
        "Editörde 2 geri alınabilir değişiklik hazırladım."
    )


def test_strategy_change_chips_follow_the_chat_language() -> None:
    arguments = SimpleNamespace(
        summary="Open on the whisk.",
        strategy=SimpleNamespace(pacing="fast", edit_format="day_vlog"),
    )
    assert kr._strategy_changes(arguments) == [
        "Open on the whisk.",
        "Fast pacing",
        "Day Vlog format",
    ]
    with reply_language_for("tr"):
        assert kr._strategy_changes(arguments) == [
            "Open on the whisk.",
            "Tempo: Hızlı",
            "Format: Günlük vlog",
        ]


class _Option:
    def __init__(self, label: str) -> None:
        self.label = label


def _dead_end(monkeypatch: pytest.MonkeyPatch, *, kind: str, labels: list[str]) -> None:
    conflict = SimpleNamespace(
        kind=kind,
        conflict_id="c1",
        input_digest="d1",
        options=[_Option(label) for label in labels],
        intro="You gave me a specific order for the clips.",
        reason="I can't check that order against your clips, so I can't promise it.",
    )
    monkeypatch.setattr(kr, "open_conflicts", lambda *_a, **_k: [conflict])
    monkeypatch.setattr(kr, "count_asks", lambda *_a, **_k: kr.MAX_ASKS_PER_QUESTION)


def test_order_dead_end_quotes_the_labels_the_matcher_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dead_end(
        monkeypatch,
        kind=kr.CONFLICT_ORDER_BASIS,
        labels=["Use the order you added the clips", "Continue without a fixed order"],
    )
    plan, reason = kr._unresolved_choice_plan({"x": 1}, None, None, has_draft=True)
    assert reason == kr.KEEP_OPEN_REASON
    assert plan.response == (
        "You gave me a specific order for the clips. I can't check that order against your "
        "clips, so I can't promise it. I won't guess, so I haven't made an edit yet. To go "
        'ahead, reply "Use the order you added the clips" or "Continue without a fixed '
        'order". Your current draft is unchanged.'
    )

    _dead_end(
        monkeypatch,
        kind=kr.CONFLICT_ORDER_BASIS,
        labels=["Klipleri eklediğin sırayla kullan", "Sabit sıra olmadan devam et"],
    )
    with reply_language_for("tr"):
        plan, _ = kr._unresolved_choice_plan({"x": 1}, None, None, has_draft=True)
    assert 'Devam etmek için "Klipleri eklediğin sırayla kullan" veya "Sabit sıra olmadan' in (
        plan.response
    )
    assert plan.response.endswith('devam et" yaz. Mevcut taslağın değişmedi.')
    assert "reply" not in plan.response

    _dead_end(monkeypatch, kind=kr.CONFLICT_TITLE_TEXT, labels=["Başlıksız devam et"])
    with reply_language_for("tr"):
        plan, _ = kr._unresolved_choice_plan({"x": 1}, None, None)
    assert plan.response.endswith('"Başlıksız devam et" yaz ya da istediğin kelimeleri yaz.')


# -- registry --------------------------------------------------------------------------


def test_project_inspect_decisions_follow_the_chat_language() -> None:
    empty = {"media_labels": []}
    with_media = {"media_labels": ["whisking.mov"], "strongest_moment": "whisking.mov"}
    handler = registry._inspect_project

    assert handler(None, empty).editorial_decision == (
        "This project needs footage before I can make an editorial decision."
    )
    assert handler(None, with_media).editorial_decision == (
        "Open with whisking.mov; it gives the story an immediate visual point of view."
    )
    with reply_language_for("tr"):
        assert handler(None, empty).editorial_decision.startswith("Düzenleme kararı")
        assert handler(None, with_media).editorial_decision == (
            "whisking.mov ile aç; hikâye ilk andan net bir görsel bakış açısı kazanır."
        )
        # A decision the thread already carries is kept as written.
        kept = handler(None, {**with_media, "editorial_decision": "Lead with the pour."})
        assert kept.editorial_decision == "Lead with the pour."


# -- the binding never outlives the work ---------------------------------------------


def test_thread_language_scope_is_released_with_its_stack() -> None:
    from contextlib import ExitStack

    thread = SimpleNamespace(state={"reply_language": "tr"})
    assert current_reply_language() is None
    with ExitStack() as stack:
        kr._bind_thread_language(stack, thread)
        assert current_reply_language() == "tr"
    assert current_reply_language() is None
    with ExitStack() as stack:
        # A thread that never told its language binds nothing: English copy.
        kr._bind_thread_language(stack, SimpleNamespace(state={}))
        assert current_reply_language() is None


def test_retryable_failure_message_uses_the_thread_language_without_a_turn_binding() -> None:
    """`_claim` projects a claims-exhausted failure before any turn binds a language."""
    from app.models import CreationThread, CreatorAgentTurn

    class _Db:
        def __init__(self) -> None:
            self.added: list = []

        def execute(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN202
            return SimpleNamespace(scalar_one=lambda: -1)

        def add(self, row) -> None:  # noqa: ANN001
            self.added.append(row)

        def flush(self) -> None:
            return None

    def _project(language: str | None) -> str:
        turn = CreatorAgentTurn(id=uuid.uuid4())
        thread = CreationThread(
            id=uuid.uuid4(), revision=1, state={"reply_language": language} if language else {}
        )
        db = _Db()
        kr._project_retryable_failure(db, turn, thread, code="runtime_turn_failed")
        return db.added[0].content

    assert _project(None).startswith("I couldn't finish that step, but your project")
    assert _project("en").startswith("I couldn't finish that step, but your project")
    assert current_reply_language() is None
    turkish = _project("tr")
    assert turkish == (
        "Bu adımı tamamlayamadım ama projen ve kayıtlı taslağın güvende. İsteği tekrar dene."
    )
    assert current_reply_language() is None


# -- the turn task's own refusals ------------------------------------------------------


def _run_turn_whose_draft_raises(
    monkeypatch: pytest.MonkeyPatch, error: Exception, language: str | None
) -> KriaTurnPlan:
    from unittest.mock import patch

    from app.config import settings
    from app.kria.planner import adapt_editor_action

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    planned = PlannedKriaTurn(
        plan=adapt_editor_action(reply="Smaller text.", ops=[{"op": "set_title", "title": "Hi"}]),
        manifest_hash="m",
        context_hash="c",
    )

    async def _plan(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return planned

    snapshot = {"item_id": str(uuid.uuid4()), **({"reply_language": language} if language else {})}
    with (
        patch("app.tasks.kria_runtime._claim", return_value=(snapshot, "make it smaller", 3, 8)),
        patch("app.tasks.kria_runtime._plan_with_live_agent", _plan),
        patch("app.tasks.kria_runtime._useful_plan", side_effect=lambda p, **_k: p),
        patch("app.tasks.kria_runtime._complete_draft_turn", side_effect=error),
        patch(
            "app.tasks.kria_runtime._complete_response_turn",
            return_value=kr._Completion(committed=True),
        ) as respond,
        patch("app.tasks.kria_runtime._fail_turn") as fail,
    ):
        assert kr.run_kria_turn.run(str(uuid.uuid4()))["status"] == "completed"
    fail.assert_not_called()
    assert current_reply_language() is None
    return respond.call_args.kwargs["plan"]


@pytest.mark.parametrize("language", [None, "tr"])
def test_an_editor_op_the_edit_cannot_honour_is_refused_in_the_turn_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError

    plan = _run_turn_whose_draft_raises(
        monkeypatch, KriaEditorOpError("Speed changes are not available."), language
    )
    assert plan.response == (
        "Bu düzenlemede bunu yapamıyorum: Speed changes are not available. Hiçbir şey değişmedi."
        if language == "tr"
        else "I can't do that on this edit: Speed changes are not available. Nothing was changed."
    )


@pytest.mark.parametrize("language", [None, "tr"])
@pytest.mark.parametrize("error_name", ["EditorStateStaleError", "EditorStateSpeechCutError"])
def test_a_stale_editor_state_is_refused_in_the_turn_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None, error_name: str
) -> None:
    from app.kria.planner import localized_editor_state_reply
    from app.services import kria_editor_ops

    error_class = getattr(kria_editor_ops, error_name)
    english = error_class.reply
    plan = _run_turn_whose_draft_raises(monkeypatch, error_class(english), language)
    if language is None:
        assert plan.response == english
    else:
        with reply_language_for("tr"):
            expected = localized_editor_state_reply(english)
        assert plan.response == expected
        assert plan.response != english
