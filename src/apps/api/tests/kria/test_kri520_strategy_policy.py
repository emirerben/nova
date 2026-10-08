"""KRI-520: strategy-policy notices and refusals reply in the chat's language.

They are appended to the model's (possibly Turkish) summary, so an English notice on a
Turkish chat made a mixed-language reply. English stays byte-identical.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import (
    GUIDED_VOICEOVER_EXECUTION_CONTRACT,
    USER_SONG_CONTRACT_NOTICE,
    USER_SONG_PHONE_ONLY_MESSAGE,
)
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import (
    CAPTIONS_KEPT_NOTICE,
    GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
    TRANSCRIPT_LABELS_DROPPED_NOTICE,
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
    localize_known_message,
)
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_NARRATION_IDENTITY
from tests.kria.test_strategy_policy import (
    CLIPS,
    PHOTOS,
    _narrated,
    _narrated_manifest,
    _talking,
    _talking_manifest,
)


def _guided_strategy() -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        media_scope="all",
        execution_contract=GUIDED_VOICEOVER_EXECUTION_CONTRACT,
        render_program="guided",
        selected_media_ids=[*CLIPS, *PHOTOS],
    )


def test_captions_kept_notice_in_turkish_and_english(prod_profile) -> None:
    english = check_strategy_for_runtime_v2(_talking_manifest(), _talking(caption_style="none"))
    with reply_language_for("tr"):
        turkish = check_strategy_for_runtime_v2(_talking_manifest(), _talking(caption_style="none"))

    assert isinstance(english, CheckedStrategy) and isinstance(turkish, CheckedStrategy)
    assert english.notices == (CAPTIONS_KEPT_NOTICE,)
    assert turkish.notices == (
        "Bu tür düzenleme şu an her zaman altyazı gösteriyor, o yüzden altyazıları bıraktım.",
    )
    assert turkish.strategy == english.strategy


def test_guided_voiceover_downgrade_notice_in_turkish(prod_profile) -> None:
    english = check_strategy_for_runtime_v2(_narrated_manifest(), _guided_strategy())
    with reply_language_for("tr"):
        turkish = check_strategy_for_runtime_v2(_narrated_manifest(), _guided_strategy())

    assert isinstance(english, CheckedStrategy) and isinstance(turkish, CheckedStrategy)
    assert GUIDED_VOICEOVER_DOWNGRADE_NOTICE in english.notices
    assert GUIDED_VOICEOVER_DOWNGRADE_NOTICE not in turkish.notices
    assert turkish.notices[0].startswith("Visuals'taki fotoğraf ve videolar henüz seslendirmeye")
    assert turkish.strategy == english.strategy


def test_transcript_labels_notice_stays_english_constant_and_has_a_turkish_twin() -> None:
    from app.kria import strategy_policy

    assert TRANSCRIPT_LABELS_DROPPED_NOTICE.startswith("Words from your voiceover")
    with reply_language_for("tr"):
        assert strategy_policy._transcript_labels_dropped_notice().startswith(
            "Seslendirmendeki sözler"
        )
    assert strategy_policy._transcript_labels_dropped_notice() == TRANSCRIPT_LABELS_DROPPED_NOTICE


def test_title_refusal_on_talking_edit_in_turkish(prod_profile, monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_title_enabled", False)
    with reply_language_for("tr"):
        refused = check_strategy_for_runtime_v2(
            _talking_manifest(), _talking(opening_title="Top 3")
        )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "title_unavailable"  # the machine code never changes
    assert refused.question == (
        "Konuşmalı düzenlemeler sözlerini altyazı olarak gösterir, o yüzden üstüne henüz "
        "başlık ekleyemiyorum. Başlıksız yapayım mı?"
    )


def test_title_refusal_on_voiceover_edit_in_turkish(prod_profile, monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_narrated_title_enabled", False)
    with reply_language_for("tr"):
        refused = check_strategy_for_runtime_v2(
            _narrated_manifest(), _narrated(opening_title="Barcelona")
        )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "title_unavailable"
    assert "iPhone" in refused.question and refused.question.endswith("?")
    assert "Başlıksız yapayım mı?" in refused.question


def test_simplification_question_in_turkish(prod_profile) -> None:
    with reply_language_for("tr"):
        refused = check_strategy_for_runtime_v2(
            _talking_manifest(),
            _talking(caption_style="none"),
            ask_before_simplifying=True,
        )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "simplification_requires_choice"
    assert refused.question == (
        "Bu tür düzenleme şu an her zaman altyazı gösteriyor, o yüzden altyazıları bıraktım. "
        "Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı?"
    )


def test_simplification_question_in_english_is_unchanged(prod_profile) -> None:
    refused = check_strategy_for_runtime_v2(
        _talking_manifest(), _talking(caption_style="none"), ask_before_simplifying=True
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.question == (
        f"{CAPTIONS_KEPT_NOTICE} Your current draft is unchanged. Should I make a simpler version?"
    )


def test_stated_title_length_question_in_turkish(prod_profile) -> None:
    strategy = _narrated(opening_title="My Trip", opening_title_duration_s=3.0)
    with reply_language_for("tr"):
        asked = check_strategy_for_runtime_v2(
            _narrated_manifest(),
            strategy,
            ask_before_simplifying=True,
            ask_about_stated_settings=True,
        )

    assert isinstance(asked, RefusedStrategy)
    assert asked.question == (
        "Bu düzenleme başlığını tam 3 saniye tutamıyor. "
        "Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı?"
    )


def test_repair_detail_and_fallback_in_turkish() -> None:
    from app.kria.strategy_policy import _repair_detail

    asked = CreativeStrategy(edit_format="montage", overlay_display="fullscreen")
    repaired = asked.model_copy(update={"overlay_display": None})
    with reply_language_for("tr"):
        assert _repair_detail(asked, repaired) == "Tam ekran Visuals bu düzenleme için yok."
    assert _repair_detail(asked, repaired) == "Full-screen Visuals aren't available for this edit."


def test_voiceover_without_a_video_is_refused_in_turkish(prod_profile) -> None:
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-narrated-photos",
        edit_format="narrated_planned",
        media=[{"media_id": media_id, "kind": "image"} for media_id in PHOTOS],
        has_voiceover=True,
        narration=dict(PROD_NARRATION_IDENTITY),
    )
    strategy = _guided_strategy().model_copy(update={"selected_media_ids": PHOTOS})
    with reply_language_for("tr"):
        refused = check_strategy_for_runtime_v2(manifest, strategy)

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "guided_voiceover_unavailable"
    assert refused.question == (
        "Seslendirmeli bir düzenleme için projeye en az bir video eklemen lazım. "
        "Bir klip ekle, onu seslendirmene göre ayarlarım."
    )


# --------------------------------------------- messages other modules write in English


def test_known_english_messages_are_translated_for_turkish_chats_only() -> None:
    for english in (
        USER_SONG_CONTRACT_NOTICE,
        USER_SONG_PHONE_ONLY_MESSAGE,
        "Couldn't find the closing photo you named; kept the normal ending.",
        "Couldn't find the closing badge you named; left it off.",
        "The requested licensed sound effect is unavailable. Choose another effect.",
    ):
        assert localize_known_message(english) == english
        with reply_language_for("en"):
            assert localize_known_message(english) == english
        with reply_language_for("tr"):
            turkish = localize_known_message(english)
        assert turkish != english
        assert localize_known_message(turkish) == turkish  # unbound: untouched


@pytest.mark.parametrize(
    ("english", "turkish"),
    [
        (
            "Couldn't find \"pasta\"'s photo/sticker; left it out.",
            '"pasta" için fotoğraf ya da çıkartma bulamadım; koymadım.',
        ),
        (
            "Day vlog shape needs music; kept a regular montage.",
            "Günlük vlog kurgusu için müzik gerekiyor; normal bir montaj yaptım.",
        ),
        (
            "Reading this as a montage in the day-vlog style.",
            "Bunu günlük vlog tarzında bir montaj olarak okudum.",
        ),
        (
            "The requested licensed sound effect 'boing' is unavailable.",
            "İstediğin lisanslı ses efekti ('boing') yok.",
        ),
    ],
)
def test_known_dynamic_messages_are_translated(english: str, turkish: str) -> None:
    with reply_language_for("tr"):
        assert localize_known_message(english) == turkish


def test_unknown_messages_and_already_turkish_text_pass_through() -> None:
    with reply_language_for("tr"):
        assert localize_known_message("Başka bir not.") == "Başka bir not."
        assert localize_known_message("Something nobody translated.") == (
            "Something nobody translated."
        )
