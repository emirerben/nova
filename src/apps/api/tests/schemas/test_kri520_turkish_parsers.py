"""KRI-520: the deterministic request readers understand Turkish.

Main Creator overwrites the model's choice of mixed-media timing, alternation cadence,
footage reuse and media scope with what these readers find in the creator's own words.
They were English-only, so a Turkish creator got the model's guess instead of what they
asked for. Every Turkish case here has a near-miss that must NOT match, and the English
behaviour is pinned alongside so the new branch can never change it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents._schemas.creator_policy import (
    explicit_scope_from_stated_media_count,
    states_explicit_media_narrowing_cue,
)
from app.agents.main_creator import (
    MainCreatorAgent,
    MainCreatorInput,
    _explicit_media_scope_from_request,
)
from app.schemas.edit_proposal import (
    recognize_explicit_cadence_reuse_policy,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    recognize_total_duration_s,
    rejects_round_robin_cadence,
    resolve_video_reuse_policy,
    turkish_media_scope,
)

# ── mixed-media timing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Fotoğraflar çok hızlı geçsin, videolar daha uzun kalsın",
        "FOTOĞRAFLAR HIZLI, VİDEOLAR DAHA YAVAŞ",
        "fotograflar hizli gecsin videolar daha yavas olsun",
        "videolarda biraz daha kal, fotoğraflar hızlı geçsin",
        "resimler çok hızlı, klipler uzun kalsın",
        "görseller hızlı geçsin videolar daha uzun kalsın",
        "Önce biraz anlatayım. Fotoğrafları hızlı geç, klibi daha uzun tut.",
    ],
)
def test_turkish_quick_photos_long_videos_is_recognised(text: str) -> None:
    profile = recognize_mixed_media_timing(text)

    assert profile is not None
    assert (profile.image_hold, profile.video_hold, profile.boundary_style) == (
        "very_fast",
        "longer",
        "cut",
    )
    assert profile.image_hold_s is None


@pytest.mark.parametrize(
    "text",
    [
        # negated on either side
        "fotoğraflar hızlı olmasın, videolar uzun kalsın",
        "fotoğraflar hızlı geçsin, videolar uzun kalmasın",
        "fotoğraflar hızlı geçmesin videolar daha uzun kalsın",
        # only one half
        "fotoğraflar çok hızlı geçsin",
        "videolar daha uzun kalsın",
        # unrelated Turkish
        "Videoyu hızlı bir montaj yap",
        "Fotoğraf serisi güzel, video uzun sürdü",
        "bugün hava çok güzel, videolar uzun",
        "Müziği biraz yavaşlat ve başlığı büyüt",
        "",
    ],
)
def test_turkish_mixed_media_near_misses_stay_with_the_model(text: str) -> None:
    assert recognize_mixed_media_timing(text) is None


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("fotoğraflar 0,3 saniye, videolar uzun kalsın", 0.3),
        ("fotoğraflar 0.2 sn olsun videolar daha uzun", 0.2),
        ("resimler 300 ms olsun, videolar daha uzun", 0.3),
        # a correction overrides the earlier value
        ("fotoğraflar 0,1 saniye olsun. Hayır, fotoğraflar 0,2 saniye olsun, videolar uzun", 0.2),
    ],
)
def test_turkish_exact_photo_length_is_read(text: str, seconds: float) -> None:
    profile = recognize_mixed_media_timing(text)

    assert profile is not None
    assert profile.image_hold_s == pytest.approx(seconds)
    assert profile.video_hold == "longer"


def test_turkish_photo_length_outside_the_contract_is_ignored() -> None:
    assert recognize_mixed_media_timing("fotoğraflar 1,5 saniye, videolar uzun") is None
    assert recognize_mixed_media_timing("fotoğraflar 5 saniye olsun") is None


def test_english_mixed_media_timing_is_unchanged() -> None:
    profile = recognize_mixed_media_timing("photos very fast and videos longer")
    assert profile is not None and profile.image_hold == "very_fast"
    assert recognize_mixed_media_timing("photos should not be fast, videos longer") is None
    assert recognize_mixed_media_timing("make the photos 0.2 seconds, videos longer") is not None


# ── alternation cadence ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("iki videoyu sırayla göster, her 2 saniyede bir", 2.0),
        ("videolar dönüşümlü olsun, her 1,5 sn", 1.5),
        ("iki video arasında gidip gel, her saniye", 1.0),
        ("birer birer değiştir, 2 saniye", 2.0),
        ("videolar dönüşümlü olarak 2 saniyelik parçalar", 2.0),
        ("Sırayla oynat: her 0.5 saniyede bir diğer videoya geç", 0.5),
        ("Alternate between them every 2 seconds", 2.0),
    ],
)
def test_turkish_round_robin_cadence(text: str, seconds: float) -> None:
    assert recognize_round_robin_cadence(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        # alternation without a cut length, or a length without alternation
        "klipleri sırayla koy",
        "her 2 saniyede bir yeni bir plan göster",
        "videoyu 30 saniye yap",
        "başlık 2 saniye görünsün",
        # a length that is not a whole frame count at 30 fps
        "sırayla göster her 0,35 saniye",
    ],
)
def test_turkish_round_robin_needs_alternation_and_a_cut_length(text: str) -> None:
    assert recognize_round_robin_cadence(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "sırayla olmasın",
        "karışık olmasın",
        "dönüşümlü yapma",
        "artık sırayla değil",
        "sırayla yapmayı bırak",
        "hayır, dönüşümlü olsun",
        "sırayla gitmesin",
    ],
)
def test_turkish_round_robin_rejection(text: str) -> None:
    assert rejects_round_robin_cadence(text) is True


@pytest.mark.parametrize(
    "text",
    ["sırayla göster her 2 sn", "dönüşümlü olsun", "klipleri sırayla koy", "tekrar etme"],
)
def test_turkish_requests_that_keep_alternating_are_not_rejections(text: str) -> None:
    assert rejects_round_robin_cadence(text) is False


def test_english_cadence_is_unchanged() -> None:
    assert recognize_round_robin_cadence("alternate every 2 seconds") == 2.0
    assert rejects_round_robin_cadence("Stop alternating") is True
    assert rejects_round_robin_cadence("alternate every 2 seconds") is False


# ── reuse policy ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "klipleri tekrar kullan",
        "Klibi tekrar kullan",
        "videoları tekrarla",
        "videoyu iki kez göster",
        "klipleri döngüye al",
        "klibi bir kez daha göster",
        "klipler tekrar tekrar kullan",
        "videolar döngü halinde olsun",
    ],
)
def test_turkish_reuse_is_granted_only_by_creator_words(text: str) -> None:
    assert resolve_video_reuse_policy(text) == "allow_repeat"


@pytest.mark.parametrize(
    "text",
    [
        "klipleri tekrar etme",
        "aynı klibi tekrar kullanma",
        "videoları tekrarlama",
        "tekrar etmesin",
        "tekrar eden klipler olmasın",
        "her klibi sadece bir kez kullan",
        "klipleri sadece bir kere göster",
        "videoyu bir kez göster",
        "döngü olmasın",
    ],
)
def test_turkish_no_repeat_wins(text: str) -> None:
    assert resolve_video_reuse_policy(text, "allow_repeat") == "once"


@pytest.mark.parametrize(
    "text",
    [
        # redo-the-edit and retry verbs are not footage reuse
        "videoyu tekrar oluştur",
        "videoyu tekrar dene",
        "tekrar render al",
        "klipleri tekrar analiz et",
        # "repeat" with no footage noun, or a quoted on-screen phrase
        'Başlık: "tekrar kullan"',
        "Kapadokya'da tekrar kullanılabilir",
        "klipleri sırayla koy",
        "klibi bir kez daha dene",
        # "bir kez" inside a cut length is not a reuse rule
        "videolar her 2 saniyede bir kez değişsin",
    ],
)
def test_turkish_unrelated_sentences_keep_the_saved_policy(text: str) -> None:
    assert resolve_video_reuse_policy(text) == "once"
    assert resolve_video_reuse_policy(text, "distinct_windows") == "distinct_windows"


def test_turkish_alternating_clips_use_distinct_windows() -> None:
    assert resolve_video_reuse_policy("iki video arasında gidip gel") == "distinct_windows"
    assert resolve_video_reuse_policy("klipler dönüşümlü olsun") == "distinct_windows"


@pytest.mark.parametrize(
    ("text", "policy"),
    [
        ("tekrar kullanabilirsin", "allow_repeat"),
        ("döngüye al", "allow_repeat"),
        ("iki kez kullan", "allow_repeat"),
        ("tekrarla", "allow_repeat"),
        ("tekrar etme", "no_repeat"),
        ("tekrarlama", "no_repeat"),
        # Like English "only once", it needs footage to be about clip reuse.
        ("sadece bir kere", None),
        ("her klibi sadece bir kere", "no_repeat"),
        ("tekrar etmek istemiyorum", "no_repeat"),
        ("tekrar dene", None),
        ('Başlık: "tekrar kullan"', None),
        ("2 saniyede bir sırayla göster", None),
    ],
)
def test_turkish_explicit_cadence_reuse(text: str, policy: str | None) -> None:
    assert recognize_explicit_cadence_reuse_policy(text) == policy


def test_turkish_only_once_about_a_title_keeps_the_cadence_reuse() -> None:
    # "The title appears only once" says nothing about reusing footage.
    assert resolve_video_reuse_policy("Başlık sadece bir kez görünsün", "allow_repeat") == (
        "allow_repeat"
    )
    assert resolve_video_reuse_policy("Logo yalnızca bir kez çıksın", "allow_repeat") == (
        "allow_repeat"
    )


def test_english_reuse_is_unchanged() -> None:
    assert resolve_video_reuse_policy("repeat the clips") == "allow_repeat"
    assert resolve_video_reuse_policy("do not repeat the clips", "allow_repeat") == "once"
    assert recognize_explicit_cadence_reuse_policy("please repeat them") == "allow_repeat"
    assert recognize_explicit_cadence_reuse_policy("do not repeat") == "no_repeat"


# ── total duration ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30 saniyelik video yap", 30),
        ("45 saniyelik bir montaj olsun", 45),
        ("toplam 30 saniye olsun", 30),
        ("video 20 saniye olsun", 20),
        ("Make it 30 seconds", 30),
    ],
)
def test_turkish_total_duration(text: str, seconds: int) -> None:
    assert recognize_total_duration_s(text) == seconds


@pytest.mark.parametrize(
    "text",
    ["toplam 30 saniye çekim yaptım", "fotoğraflar 5 saniye olsun", "2 saniyelik video yap"],
)
def test_turkish_total_duration_needs_a_whole_video_length(text: str) -> None:
    assert recognize_total_duration_s(text) is None


# ── media scope ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("Tüm klipleri kullan", "all"),
        ("bütün klipler olsun", "all"),
        ("hepsini kullan", "all"),
        ("her şeyi kullan", "all"),
        ("tüm yüklediğim videoları ekle", "all"),
        ("sadece en iyileri kullan", "selected"),
        ("en iyi kısımlar yeter", "selected"),
        ("sadece seçili klipleri kullan", "selected"),
        ("hepsini kullanma", "selected"),
        ("tüm klipleri kullanmak istemiyorum", "selected"),
        # everything wins over "best parts" when the creator says both
        ("tüm klipleri kullan, en iyi kısımlarını al", "all"),
        # not a scope statement
        ("tüm video boyunca altyazı olsun", None),
        # Visuals ("görsel") are overlay media in Kria, not the clips
        ("Tüm görseller tam ekran olsun", None),
        ("Bugün tüm gün çekim yaptım", None),
        ("videoyu kısalt", None),
    ],
)
def test_turkish_media_scope(text: str, scope: str | None) -> None:
    assert turkish_media_scope(text) == scope
    assert _explicit_media_scope_from_request(text) == scope


def test_english_media_scope_is_unchanged() -> None:
    assert _explicit_media_scope_from_request("Use all of them") == "all"
    assert _explicit_media_scope_from_request("do not use all of them") == "selected"
    assert _explicit_media_scope_from_request("I like tumbling videos") is None


def _manifest_of(count: int) -> SimpleNamespace:
    return SimpleNamespace(media=[SimpleNamespace(kind="video") for _ in range(count)])


@pytest.mark.parametrize(
    "text",
    [
        "16 klip ile devam et",
        "16 klibi kullan",
        "16 tane video ile devam",
        "tüm 16 ile devam",
        "continue with 16 clips",
    ],
)
def test_stated_clip_count_naming_the_whole_set_is_all(text: str) -> None:
    assert explicit_scope_from_stated_media_count(text, _manifest_of(16)) is True


@pytest.mark.parametrize(
    "text",
    [
        "en iyi 5 klibi seç",
        "16 klibin en iyilerini kullan",
        "30 klip ile devam",
        "16 saniyelik video",
        "16 videos but pick the best",
        "16 klip ve 5 fotoğraf",
    ],
)
def test_stated_clip_count_that_narrows_or_differs_is_not_all(text: str) -> None:
    assert explicit_scope_from_stated_media_count(text, _manifest_of(16)) is False


@pytest.mark.parametrize(
    ("text", "narrows"),
    [
        ("sadece en iyilerini kullan", True),
        ("en iyi kısımlar yeter", True),
        ("Atla şu klibi", True),
        ("ikinci klibi videodan çıkar", True),
        ("yeni versiyon çıkar", False),
        ("başlığı kaldır", False),
        ("bazılarını çıkar", True),
        ("daha az klip kullan", True),
        ("hepsini kullan", False),
        ("Atlanta'da çekildi", False),
        ("tüm klipler olsun", False),
        ("use the best ones", True),
    ],
)
def test_narrowing_cue_in_turkish(text: str, narrows: bool) -> None:
    assert states_explicit_media_narrowing_cue(text) is narrows


# ── Main Creator overwrites the model's choice with what the creator said ───────────


def _manifest(videos: int = 2) -> ResolvedCreatorManifest:
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=f"match-{index}", kind="video", duration_s=20)
            for index in range(videos)
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _raw(**strategy_update: object) -> str:
    strategy = {
        "direction": "guided_story",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "montage_audio": None,
        "render_program": "guided",
        "selected_media_ids": [],
        "target_duration_s": 24,
        "rationale": "Build a concise visual arc.",
        **strategy_update,
    }
    return json.dumps(
        {"action": {"kind": "propose_strategy", "strategy": strategy, "summary": "A story."}}
    )


def _parse(user_message: str, *, history: list[str] | None = None, videos: int = 2):
    agent_input = MainCreatorInput(
        user_message=user_message,
        conversation=[{"role": "user", "content": text} for text in history or []],
        capability_manifest=_manifest(videos),
    )
    output = MainCreatorAgent(None).parse(_raw(), agent_input)  # type: ignore[arg-type]
    assert isinstance(output.action, ProposeStrategy)
    return output.action.strategy


def test_main_creator_turns_a_turkish_alternation_request_into_a_cadence() -> None:
    strategy = _parse("Videolar sırayla her 2 saniyede bir değişsin. Tekrar kullanabilirsin.")

    assert strategy.montage_cadence is not None
    assert strategy.montage_cadence.cut_duration_s == 2
    assert strategy.montage_cadence.reuse_policy == "allow_repeat"
    assert strategy.video_reuse_policy == "allow_repeat"


def test_main_creator_prefers_the_latest_turkish_cadence_revision() -> None:
    strategy = _parse(
        "Aslında sırayla her 2 saniyede bir değişsin, klipleri tekrar kullanabilirsin.",
        history=["Videolar sırayla her 1 saniyede bir değişsin, tekrar etme."],
    )

    assert strategy.montage_cadence is not None
    assert strategy.montage_cadence.cut_duration_s == 2
    assert strategy.montage_cadence.reuse_policy == "allow_repeat"


def test_main_creator_drops_the_cadence_when_the_creator_stops_alternating() -> None:
    strategy = _parse(
        "Sırayla olmasın, normal montaj yap.",
        history=["Videolar sırayla her 2 saniyede bir değişsin."],
    )

    assert strategy.montage_cadence is None


def test_main_creator_reads_turkish_mixed_media_timing_from_the_conversation() -> None:
    strategy = _parse(
        "Tamam, öyle olsun.",
        history=["Fotoğraflar çok hızlı geçsin, videolar daha uzun kalsın."],
    )

    assert strategy.mixed_media_timing is not None
    assert strategy.mixed_media_timing.image_hold == "very_fast"
    assert strategy.mixed_media_timing.video_hold == "longer"


def test_main_creator_keeps_everything_when_the_turkish_creator_says_all() -> None:
    assert _parse("Tüm klipleri kullan, hiçbirini eleme.").media_scope == "all"
    assert _parse("Sadece en iyilerini kullan.").media_scope == "selected"


def test_main_creator_without_turkish_signals_changes_nothing() -> None:
    strategy = _parse("Bu videoyu havalı yap.")

    assert strategy.montage_cadence is None
    assert strategy.mixed_media_timing is None
    assert strategy.video_reuse_policy == "once"
