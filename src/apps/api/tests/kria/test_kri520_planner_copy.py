"""KRI-520: the planner writes its creator-visible copy in the chat's language.

Turkish copy is checked under ``reply_language_for("tr")``; English (unbound) must stay
byte-identical, so every English string is pinned literally. The text-edit / re-plan
routing wording is checked in Turkish with negatives (ordinary text edits must not
trigger a re-plan).
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.agents._schemas.creator_agent import (
    AskUser,
    CreativeStrategy,
    ProposeStrategy,
    ReviewDecision,
)
from app.agents.main_creator import CreativeCopyDecision
from app.kria import planner
from app.kria.brief import BriefCoverageError, wants_full_replan
from app.kria.contracts import KriaTurnPlan
from app.kria.reply_language import reply_language_for
from app.routes._copilot import CopilotTurnResponse, _honest_outcome
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.creative_copy_decisions import media_digest, wording_question
from app.services.kria_editor_ops import MAX_EDITOR_OPS
from tests.kria.test_choice_title_text import _gate, _incident_brief, _planned, _snapshot

DIGEST = "d" * 24
_TURKISH_LETTERS = re.compile(r"[çğıöşüÇĞİÖŞÜ]")


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64)


def _inputs() -> SimpleNamespace:
    return SimpleNamespace(creative_copy_state={}, creative_copy_digest=DIGEST)


def _decision(status: str, *, text: str | None = None, language: str = "en"):
    return CreativeCopyDecision(
        target="opening_title",
        status=status,
        proposed_text=text,
        source_evidence="creator request" if text else None,
        language=language,
    )


def _strategy_action(summary: str = "") -> ProposeStrategy:
    return ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            audio_strategy="original_audio",
            pacing="balanced",
            render_program="guided",
            rationale="",
        ),
        summary=summary,
    )


def _summary(plan: KriaTurnPlan) -> str:
    return plan.intents[0].arguments["summary"]


# -- adapt_creator_action ---------------------------------------------------------------


def test_review_summary_fallback_english_unchanged_and_turkish() -> None:
    action = ReviewDecision(kind="review_decision", decision="approve", summary="")
    assert (
        planner.adapt_creator_action(action).response == "The current cut is ready for your review."
    )
    with reply_language_for("tr"):
        assert (
            planner.adapt_creator_action(action).response == "Şu anki kesim incelemen için hazır."
        )
        # A model-written summary is never replaced.
        written = ReviewDecision(kind="review_decision", decision="approve", summary="Hazır!")
        assert planner.adapt_creator_action(written).response == "Hazır!"


def test_strategy_summary_fallback_english_unchanged_and_turkish() -> None:
    assert (
        _summary(planner.adapt_creator_action(_strategy_action()))
        == "I shaped a focused draft around the strongest available footage."
    )
    with reply_language_for("tr"):
        assert (
            _summary(planner.adapt_creator_action(_strategy_action()))
            == "Elindeki en güçlü çekimlerle odaklı bir taslak hazırladım."
        )


def test_unplaced_order_note_english_unchanged_and_turkish() -> None:
    unplaced = [
        ResolvedClipIntent(
            intent_id="i1",
            op="order",
            attribute="balloons",
            status="resolved",
            assignments=[],
            order_by=None,
        )
    ]
    action = _strategy_action("Draft.")
    plan = planner.adapt_creator_action(action, server_resolved_clip_intents=unplaced)
    assert _summary(plan) == (
        "Draft. I found no clips of balloons, so I can't place them where you asked."
    )
    with reply_language_for("tr"):
        plan = planner.adapt_creator_action(action, server_resolved_clip_intents=unplaced)
        assert _summary(plan) == (
            "Draft. balloons ile ilgili klip bulamadım, "
            "bu yüzden onları istediğin yere yerleştiremiyorum."
        )


def test_model_written_question_is_passed_through_untouched() -> None:
    ask = AskUser(
        kind="ask_user",
        question="Hangi an videoyu açsın?",
        reason_code="pacing_choice",
        options=[],
    )
    with reply_language_for("tr"):
        assert planner.adapt_creator_action(ask).response == "Hangi an videoyu açsın?"


# -- clip-intent resolution replies -----------------------------------------------------


def test_clip_intent_pending_reply_asks_for_devam() -> None:
    plan = planner._clip_intent_resolution_plan(question=None, status="pending")
    assert plan.response == (
        "I'm still checking some of your clips against that request. "
        'Reply "go ahead" in a moment and I\'ll pick up where I left off. '
        "No need to send the whole request again."
    )
    with reply_language_for("tr"):
        tr = planner._clip_intent_resolution_plan(question=None, status="pending").response
    assert '"devam"' in tr and "go ahead" not in tr
    assert tr == planner._CLIP_INTENT_PENDING_REPLY_TR


def test_clip_intent_failure_and_question_fallbacks() -> None:
    failed = planner._clip_intent_resolution_plan(question=None, status="provider_unavailable")
    assert failed.response == (
        "I couldn't reliably match that request to your clips. Please try again shortly."
    )
    asked = planner._clip_intent_resolution_plan(question=None, status="needs_creator")
    assert asked.response == "Which clips should I use for that part?"
    with reply_language_for("tr"):
        failed = planner._clip_intent_resolution_plan(question=None, status="provider_unavailable")
        asked = planner._clip_intent_resolution_plan(question=None, status="needs_creator")
        # A resolver-written question is kept as is.
        given = planner._clip_intent_resolution_plan(question="Hangi klip?", status="needs_creator")
    assert failed.response == (
        "Bu isteği kliplerinle güvenilir şekilde eşleştiremedim. Birazdan tekrar dene."
    )
    assert asked.response == "Bu bölüm için hangi klipleri kullanayım?"
    assert given.response == "Hangi klip?"


def test_labels_need_voiceover_reply() -> None:
    assert planner._labels_need_voiceover_reply() == (
        "Those labels need a recorded voiceover with guided visuals."
    )
    with reply_language_for("tr"):
        assert planner._labels_need_voiceover_reply() == (
            "Bu etiketler için kaydedilmiş bir seslendirme ve rehberli görseller gerekiyor."
        )


def test_still_checking_clips_reply() -> None:
    assert planner._clips_still_checking_reply(3) == (
        "I'm still checking 3 of your clips. "
        "Your request and completed answers are saved. "
        "Ask me to continue once those clips are ready."
    )
    with reply_language_for("tr"):
        assert planner._clips_still_checking_reply(3) == (
            "Kliplerinden 3 tanesini hâlâ kontrol ediyorum. "
            "İsteğin ve tamamlanan yanıtlar kaydedildi. "
            "Bu klipler hazır olunca devam etmemi iste."
        )


def test_song_pending_reply() -> None:
    assert planner._song_pending_plan().response == planner._SONG_PENDING_REPLY
    assert planner._SONG_PENDING_REPLY == (
        "I'm still analysing your song and matching your clips to it. "
        "Send your message again in a moment and I'll pick up where I left off."
    )
    with reply_language_for("tr"):
        assert planner._song_pending_plan().response == planner._SONG_PENDING_REPLY_TR


# -- editor replies ---------------------------------------------------------------------


def _many_ops() -> list[dict]:
    return [{"op": "edit_text", "id": f"t{i}", "text": f"Text {i}"} for i in range(60)]


def test_too_many_editor_ops_reply() -> None:
    assert len(_many_ops()) > MAX_EDITOR_OPS
    plan = planner.adapt_editor_action(reply="ok", ops=_many_ops())
    assert plan.response == (
        "That is more changes than I can apply in one go, so I left the video "
        "as it was. Ask for it in smaller steps, or for all of one kind of "
        "change at once (for example every font)."
    )
    with reply_language_for("tr"):
        tr = planner.adapt_editor_action(reply="ok", ops=_many_ops()).response
    assert tr.startswith("Bu, tek seferde uygulayabileceğimden fazla değişiklik")
    assert "tüm yazı tipleri" in tr


def test_phone_staged_reply_english_and_turkish() -> None:
    canned = "I prepared this edit for the editor to validate and stage."
    assert planner._phone_editor_reply(canned) == (
        "Updated your edit — it's in the editor now. Save when you're happy with it."
    )
    assert planner._phone_editor_reply(f"{canned} Times are in UTC.") == (
        "Updated your edit — it's in the editor now. Save when you're happy with it. "
        "Times are in UTC."
    )
    assert planner._phone_editor_reply("I prepared a tighter opening.") == (
        "I prepared a tighter opening."
    )
    staged = "Düzenlemeni güncelledim, şu an editörde. Memnun kaldığında kaydet."
    with reply_language_for("tr"):
        assert planner._phone_editor_reply(canned) == staged
        assert planner._phone_editor_reply(f"{canned} Saat UTC.") == f"{staged} Saat UTC."
        assert planner._phone_editor_reply("Daha sıkı bir açılış hazırladım.") == (
            "Daha sıkı bir açılış hazırladım."
        )


def test_phone_staged_reply_recognizes_the_turkish_copilot_canned_line() -> None:
    """The planner swaps the copilot's canned line for the phone wording in either language,
    so the Turkish line `routes/_copilot._honest_outcome` writes must be the one it knows."""
    output = SimpleNamespace(
        rejection_reasons=[],
        intent="edit",
        needs_clarification=False,
        reply="",
        reply_notes="",
        unmet_requests=[],
    )
    with reply_language_for("tr"):
        outcome, canned = _honest_outcome(output, [{"op": "edit_text"}], supports_proposed=True)
        assert outcome == "proposed"
        assert canned == planner._WEB_PROPOSED_REPLY_TR
        assert planner._phone_editor_reply(canned) == planner._PHONE_STAGED_REPLY_TR
    outcome, canned = _honest_outcome(output, [{"op": "edit_text"}], supports_proposed=True)
    assert canned == planner._WEB_PROPOSED_REPLY


def _copilot_reply(outcome: str, reply: str, ops: list[dict] | None = None) -> CopilotTurnResponse:
    return CopilotTurnResponse(
        intent="reject" if outcome == "unsupported" else "edit",
        ops=ops or [],
        confidence=0.5,
        reply=reply,
        outcome=outcome,
    )


async def _revision(
    monkeypatch: pytest.MonkeyPatch, response: CopilotTurnResponse, message: str = "x", **kw
):
    monkeypatch.setattr(
        planner,
        "_load_editor_target",
        AsyncMock(
            return_value=planner._EditorTarget(job_id=uuid.uuid4(), snapshot={}, conversation=[])
        ),
    )
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=response))
    db = SimpleNamespace(rollback=AsyncMock())
    return await planner._plan_editor_revision(
        db,
        thread_id=uuid.uuid4(),
        item=SimpleNamespace(id=uuid.uuid4()),
        user_message=message,
        **kw,
    )


@pytest.mark.asyncio
async def test_unsupported_refusal_carries_the_redo_offer_in_the_chat_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _revision(monkeypatch, _copilot_reply("unsupported", "No can do."))
    assert plan.response == "No can do." + planner._REDO_OFFER
    assert planner._REDO_OFFER == (
        ' If you want a fresh version built from your whole request, reply "redo" and I\'ll '
        "render it again."
    )
    with reply_language_for("tr"):
        plan = await _revision(monkeypatch, _copilot_reply("unsupported", "Bunu yapamam."))
        assert plan.response == "Bunu yapamam." + planner._REDO_OFFER_TR
        # An offer already in the reply (either language) is never added twice.
        again = await _revision(
            monkeypatch, _copilot_reply("unsupported", "Bunu yapamam." + planner._REDO_OFFER_TR)
        )
        assert again.response.count("yeniden yap") == 1
        english = await _revision(
            monkeypatch, _copilot_reply("unsupported", "No." + planner._REDO_OFFER)
        )
        assert english.response == "No." + planner._REDO_OFFER


def test_turkish_redo_offer_names_a_reply_the_router_accepts() -> None:
    quoted = re.findall(r'"([^"]+)"', planner._REDO_OFFER_TR)
    assert quoted == ["yeniden yap"]
    for reply in (quoted[0], "Yeniden yap", "YENİDEN YAP", "yeniden yap lutfen"):
        assert wants_full_replan(reply), reply
    # And the offer itself is not mistaken for a creator message.
    assert planner._REDO_OFFER_TR.startswith(" ")


@pytest.mark.asyncio
async def test_saved_request_too_large_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planner, "_load_editor_target", AsyncMock(side_effect=BriefCoverageError("x"))
    )
    args = {
        "thread_id": uuid.uuid4(),
        "item": SimpleNamespace(id=uuid.uuid4()),
        "user_message": "x",
    }
    plan = await planner._plan_editor_revision(SimpleNamespace(), **args)
    assert plan.response == (
        "Your saved request is too large or unavailable for this editor step. "
        "Your draft is unchanged. Which clip or part should I work on first?"
    )
    with reply_language_for("tr"):
        plan = await planner._plan_editor_revision(SimpleNamespace(), **args)
    assert plan.response.startswith("Kayıtlı isteğin bu düzenleme adımı için çok büyük")
    assert plan.turn_value == "question"


@pytest.mark.asyncio
async def test_speech_cut_needs_save_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _copilot_reply("proposed", "ok", ops=[{"op": "apply_speech_cut_candidate"}])
    monkeypatch.setattr(planner, "editor_state_has_lanes", lambda _s: True)
    plan = await _revision(monkeypatch, response, editor_state=object())
    assert plan.response == "Save your edits first, then I can cut the silences."
    with reply_language_for("tr"):
        plan = await _revision(monkeypatch, response, editor_state=object())
    assert plan.response == "Önce düzenlemelerini kaydet, sonra sessiz kısımları kesebilirim."


@pytest.mark.parametrize(
    ("miss", "english", "turkish_start"),
    [
        (
            "render_in_flight",
            "Your edit is still rendering — give it a moment, then ask again.",
            "Düzenlemen hâlâ hazırlanıyor",
        ),
        (
            "editor_state_stale",
            "Your video changed — reopen the editor and try again.",
            "Videon değişti",
        ),
        (
            "no_ready_variant",
            "I couldn't open your current edit to change it in place. Try again, "
            "or ask me for a new version.",
            "Şu anki düzenlemeni açıp yerinde değiştiremedim",
        ),
    ],
)
def test_editor_target_recovery_replies(miss: str, english: str, turkish_start: str) -> None:
    planner._editor_target_miss.set(miss)
    try:
        assert planner._editor_target_recovery(_manifest()).plan.response == english
        with reply_language_for("tr"):
            tr = planner._editor_target_recovery(_manifest()).plan.response
        assert tr.startswith(turkish_start)
        assert tr != english
    finally:
        planner._editor_target_miss.set(None)


def test_localized_editor_state_reply() -> None:
    from app.services.kria_editor_ops import EDITOR_STATE_STALE_REPLY, SPEECH_CUT_NEEDS_SAVE_REPLY

    for reply in (EDITOR_STATE_STALE_REPLY, SPEECH_CUT_NEEDS_SAVE_REPLY, "Something else."):
        assert planner.localized_editor_state_reply(reply) == reply
    with reply_language_for("tr"):
        assert planner.localized_editor_state_reply(EDITOR_STATE_STALE_REPLY) == (
            "Videon değişti. Editörü yeniden açıp tekrar dene."
        )
        assert planner.localized_editor_state_reply(SPEECH_CUT_NEEDS_SAVE_REPLY) == (
            "Önce düzenlemelerini kaydet, sonra sessiz kısımları kesebilirim."
        )
        assert planner.localized_editor_state_reply("Something else.") == "Something else."


# -- request recovery -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "english_detail", "turkish_detail"),
    [
        (
            "request_extraction_failed",
            "I couldn't reliably read every requested change.",
            "İstediğin her değişikliği güvenilir şekilde okuyamadım.",
        ),
        (
            "planning_batches_disagree",
            "The separate parts of your brief produced conflicting plans.",
            "İsteğinin ayrı bölümleri birbiriyle çelişen planlar çıkardı.",
        ),
        (
            "clip_planner_context_limit",
            "Your complete brief exceeds the clip planner's 12,000-character limit.",
            "Tüm isteğin, klip planlayıcının 12.000 karakterlik sınırını aşıyor.",
        ),
        (
            "context_limit",
            "Your complete request exceeds the context this planning step can safely read.",
            "Tüm isteğin, bu planlama adımının güvenle okuyabileceği sınırı aşıyor.",
        ),
    ],
)
def test_request_recovery_copy(reason: str, english_detail: str, turkish_detail: str) -> None:
    english = planner._request_recovery(_manifest(), None, reason=reason).plan.response
    assert english == (
        f"{english_detail} Your complete request is saved and your draft is unchanged. "
        "Which clip or part of the edit should I work on first?"
    )
    with reply_language_for("tr"):
        tr = planner._request_recovery(_manifest(), None, reason=reason).plan.response
    assert tr == (
        f"{turkish_detail} Tüm isteğin kaydedildi ve taslağın değişmedi. "
        "Önce düzenlemenin hangi klibi ya da bölümü üzerinde çalışayım?"
    )


# -- creative-copy questions ------------------------------------------------------------


def test_creative_copy_wording_question_turkish_with_english_aliases() -> None:
    english = planner._creative_copy_turn(
        _decision("candidate", text="One day, one city"),
        inputs=_inputs(),
        creator_request="Write a hook",
        manifest=_manifest(),
    )
    assert english.plan.response.startswith("I’d try “One day, one city”. Does this wording work?")
    assert [o["label"] for o in english.plan.choice_question["options"]][0] == "Use this wording"

    with reply_language_for("tr"):
        tr = planner._creative_copy_turn(
            _decision("candidate", text="One day, one city"),
            inputs=_inputs(),
            creator_request="Write a hook",
            manifest=_manifest(),
        )
    assert tr.plan.response.startswith(
        "Şunu deneyebilirim: “One day, one city”. Bu ifade sana uyar mı?"
    )
    options = tr.plan.choice_question["options"]
    approve = next(o for o in options if o["key"] == "approve")
    assert approve["label"] == "Bu ifadeyi kullan"
    assert "Use this wording" in approve["aliases"]  # typed English answers still match
    assert "Bu ifadeyi kullan" in tr.plan.response  # clients that render only text keep the answers
    # The wording itself is the creator's / model's, never translated.
    assert tr.plan.choice_question["candidate"] == "One day, one city"


def test_creative_copy_authorship_question_turkish() -> None:
    with reply_language_for("tr"):
        tr = planner._creative_copy_turn(
            _decision("unresolved"),
            inputs=_inputs(),
            creator_request="Add an opening hook",
            manifest=_manifest(),
        )
    assert tr.plan.response.startswith("Aklında bir fikir var mı, yoksa ben mi yazayım?")
    assert tr.plan.choice_question["kind"] == "creative_copy_authorship"
    assert {o["key"]: o["label"] for o in tr.plan.choice_question["options"]}["write_my_own"] == (
        "Bir fikrim var"
    )


def test_turkish_chat_localizes_labels_even_for_english_wording() -> None:
    """The decision's language describes the on-screen wording; the labels follow the chat."""
    with reply_language_for("tr"):
        tr = planner._creative_copy_turn(
            _decision("unresolved", language="en"),
            inputs=_inputs(),
            creator_request="hook",
            manifest=_manifest(),
        )
    assert "Bir fikrim var" in [o["label"] for o in tr.plan.choice_question["options"]]
    # Unbound keeps the previous rule: labels follow the decision's language.
    german = planner._creative_copy_turn(
        _decision("unresolved", language="de"),
        inputs=_inputs(),
        creator_request="hook",
        manifest=_manifest(),
    )
    assert "Ich habe eine Idee" in [o["label"] for o in german.plan.choice_question["options"]]


@pytest.mark.asyncio
async def test_gate_title_authorship_question_turkish(monkeypatch: pytest.MonkeyPatch) -> None:
    english = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    assert english.plan.response == "Do you have an idea, or would you like me to write one?"

    with reply_language_for("tr"):
        tr = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    assert tr.plan.mode == "respond" and not tr.plan.intents
    assert tr.plan.response == "Aklında bir fikir var mı, yoksa ben mi yazayım?"
    question = tr.plan.choice_question
    assert question["kind"] == "creative_copy_authorship"
    by_key = {o["key"]: o for o in question["options"]}
    assert by_key["write_my_own"]["label"] == "Bir fikrim var"
    assert by_key["write_my_own"]["aliases"]  # the English label stays an alias


@pytest.mark.asyncio
async def test_gate_open_copy_question_message_turkish(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = media_digest(_snapshot())
    authorship = [
        (
            "assistant",
            {
                "choice_question": planner.authorship_question(
                    target="opening_title", dependency_digest=digest
                )
            },
        )
    ]
    english = await _gate(monkeypatch, _planned(), brief=_incident_brief(), events=authorship)
    assert english.plan.response.startswith(
        "Do you have an idea, or would you like me to write one? You can also skip this text."
    )
    with reply_language_for("tr"):
        tr = await _gate(monkeypatch, _planned(), brief=_incident_brief(), events=authorship)
    assert tr.plan.response.startswith(
        "Aklında bir fikir var mı, yoksa ben mi yazayım? İstersen bu yazıyı atlayabilirsin."
    )
    assert "Bir fikrim var" in tr.plan.response

    wording = [
        (
            "assistant",
            {
                "choice_question": wording_question(
                    target="opening_title", candidate="Weekend away", dependency_digest=digest
                )
            },
        )
    ]
    english = await _gate(monkeypatch, _planned(), brief=_incident_brief(), events=wording)
    assert english.plan.response.startswith("“Weekend away” — does this wording work?")
    with reply_language_for("tr"):
        tr = await _gate(monkeypatch, _planned(), brief=_incident_brief(), events=wording)
    assert tr.plan.response.startswith("“Weekend away” — bu ifade sana uyar mı?")
    assert "Bu ifadeyi kullan" in tr.plan.response


# -- plan_live_turn guards --------------------------------------------------------------


@pytest.mark.asyncio
async def test_clips_changed_while_planning_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    act = planner.adapt_creator_action(_strategy_action("Draft."))
    planned = planner.PlannedKriaTurn(plan=act, manifest_hash="a" * 64, context_hash="b" * 64)
    monkeypatch.setattr(planner, "_plan_live_turn", AsyncMock(return_value=planned))
    snapshots = iter([{"a": 1}, {"a": 2}])

    import app.kria.brief_binding as binding

    monkeypatch.setattr(binding, "snapshot_media", lambda _item: next(snapshots))
    monkeypatch.setattr(binding, "media_identity", lambda snap: snap)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))

    async def run():
        return await planner.plan_live_turn(
            db, item_id=uuid.uuid4(), thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
        )

    changed = await run()
    assert changed.plan.response == (
        "Your clips changed while I was planning. Your draft is unchanged; "
        "please ask again using the current clips."
    )
    snapshots = iter([{"a": 1}, {"a": 2}])
    with reply_language_for("tr"):
        changed = await run()
    assert changed.plan.response == (
        "Ben planlarken kliplerin değişti. Taslağın değişmedi; lütfen güncel kliplerle tekrar iste."
    )


# -- every Turkish line is Turkish ------------------------------------------------------


def test_turkish_copy_constants_are_turkish_and_use_the_agreed_reply_words() -> None:
    for text in (
        planner._CLIP_INTENT_PENDING_REPLY_TR,
        planner._SONG_PENDING_REPLY_TR,
        planner._REDO_OFFER_TR,
        planner._PHONE_STAGED_REPLY_TR,
        planner._EDITOR_TARGET_RECOVERY_REPLY_TR,
        planner._EDITOR_TARGET_IN_FLIGHT_REPLY_TR,
    ):
        assert _TURKISH_LETTERS.search(text), text
    assert '"devam"' in planner._CLIP_INTENT_PENDING_REPLY_TR
    assert '"yeniden yap"' in planner._REDO_OFFER_TR


# -- routing: text edits stay quick edits, new versions re-plan --------------------------


@pytest.mark.parametrize(
    "message",
    [
        "başlığı büyüt",
        "Başlık: 'Hafta sonu kaçamağı' olsun",
        "altyazıyı sarı yap",
        "ALTYAZILARI KALIN YAP",
        "yazıları ortala",
        "yazı tipini değiştir",
        "fontu küçült",
        "metni kısalt",
        "etiketleri sil",
        "ifadeyi değiştir",
        "baslik cok kucuk",  # ASCII-typed
        "alt yazi sari olsun",
    ],
)
def test_turkish_text_edit_asks_are_text_edits_not_replans(message: str) -> None:
    assert planner._is_text_edit_ask(message), message
    assert planner._fast_path_eligible(message), message
    assert not wants_full_replan(message), message


@pytest.mark.parametrize(
    "message",
    [
        "ikinci klibi kısalt",
        "müziği kıs",
        "geçişleri hızlandır",
        "videoyu 20 saniyeye indir",
        "tamam",
    ],
)
def test_turkish_non_text_asks_are_not_text_edits(message: str) -> None:
    assert not planner._is_text_edit_ask(message), message
    assert planner._fast_path_eligible(message), message  # still a quick edit, just not text


@pytest.mark.parametrize(
    "message",
    [
        "baştan yap",
        "sıfırdan başla",
        "farklı bir versiyon istiyorum",
        "bana başka bir versiyon yap",
        "yeni bir video istiyorum",
        "yeni versiyon çıkar",
        "yeni düzenleme yap",
        "en iyi 5 klibi kullan",
        "5 en iyi klip olsun",
        "sadece en iyi klipleri koy",
        "yalnızca en komik anları kullan",
        "en komik kısımları seç",
        "klipleri karıştır",
        "daha fazla klip koy",
        "daha az klip olsun",
        "daha çok klip istiyorum",
        "tamamen yeniden kurgula",
        "SIFIRDAN YAP",
        "BAŞTAN YAP",
        "YENİDEN YAP",
        "farkli bir vibe",  # ASCII-typed
        "sifirdan yap",
    ],
)
def test_turkish_replan_cues_leave_the_quick_edit_path(message: str) -> None:
    assert not planner._fast_path_eligible(message), message


@pytest.mark.parametrize(
    "message",
    [
        "make the title bigger",
        "change the font",
        "make the captions yellow",
        "shorten the second clip",
        "turn the music down",
        "use the best 3 clips",
        "give me a completely different vibe",
        "shuffle the clips",
        "I want a new version",
    ],
)
def test_english_routing_is_unchanged(message: str) -> None:
    text_ask = bool(planner._TEXT_EDIT_ASK.search(message.casefold()))
    assert planner._is_text_edit_ask(message) == text_ask
    replan = bool(planner._REPLAN_CUES.search(message.casefold())) or wants_full_replan(message)
    assert planner._fast_path_eligible(message) == (not replan)


def test_english_fonts_plural_keeps_its_old_non_match() -> None:
    # Turkish "font" takes case endings only; English "fonts" was never a text-edit ask here.
    assert not planner._is_text_edit_ask("make the fonts bigger")
    assert planner._is_text_edit_ask("fontları büyüt")


# -- routing: a copilot refusal to a Turkish text ask stands; a non-text ask re-plans --------


def _wire(monkeypatch: pytest.MonkeyPatch, copilot_plan: KriaTurnPlan | None):
    from tests.kria.test_planner_editor_target_miss import _wire as wire

    db, item, creator_id, runs = wire(monkeypatch, miss=None)
    monkeypatch.setattr(planner, "_plan_editor_revision", AsyncMock(return_value=copilot_plan))
    return db, item, creator_id, runs


async def _ask(db, item, creator_id, message):  # noqa: ANN001, ANN202
    return await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message=message,
    )


_CLARIFY = KriaTurnPlan(mode="respond", turn_value="question", response="Hangi başlığı büyüteyim?")


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["başlığı büyüt", "altyazıyı sarı yap", "yazı tipini değiştir"])
async def test_copilot_clarification_to_a_turkish_text_ask_stands(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    db, item, creator_id, runs = _wire(monkeypatch, _CLARIFY)
    with capture_logs() as logs:
        result = await _ask(db, item, creator_id, message)
    assert result.plan.response == "Hangi başlığı büyüteyim?"
    assert result.plan.turn_value == "question"
    assert runs == []  # no Main Creator re-plan, no render
    assert not [e for e in logs if e["event"] == "kria_copilot_skipped_replan"]


@pytest.mark.asyncio
async def test_copilot_clarification_to_a_turkish_non_text_ask_replans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, _runs = _wire(monkeypatch, _CLARIFY)
    with capture_logs() as logs:
        result = await _ask(db, item, creator_id, "ikinci klibi kısalt")
    assert result.plan.response != "Hangi başlığı büyüteyim?"
    # The copilot's refusal did not stand: the turn went on to the planner's own route.
    assert any(
        e["event"] == "kria_copilot_skipped_replan" and e["reason"] == "fast_path_not_taken"
        for e in logs
    )


@pytest.mark.parametrize(
    "message",
    [
        "Yazın çektiğim klipleri başa al",  # "yazın" = in summer
        "Metin'in olduğu klibi çıkar",  # Metin is a name
        "En güzel yüz ifadelerini seç",  # a facial expression
    ],
)
def test_turkish_words_that_only_look_like_text_asks(message: str) -> None:
    assert not planner._is_text_edit_ask(message)
