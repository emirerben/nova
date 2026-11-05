"""The Main Creator must hear the whole talking take, not its first ~480 characters.

Incident: S4 run "fb 1" (prod 2026-11-04, thread 6A624CDB, job c39a3210). An 83 s talking take
with ten Visuals and "behind me whichever photo or video I'm talking about": every
`background_media.sequence` phrase came from the first 29 s, because `_chat_evidence` kept the
take's timed `segments` only up to CHAT_EVIDENCE_SEGMENT_CHARS (480). The first five sentences
were 445 characters; sentences 6-14 never reached the planner, so nothing after ~30 s could be
placed on its word. Failure modes: docs/reviews/planner-speech-budget-failure-modes.md.

Offline replay through the real boundary: stored clip analysis -> resolve_item_creator_context
-> MainCreatorInput -> MainCreatorAgent.render_prompt. The take below is invented Turkish speech
with the incident's shape (14 sentences, ~850 characters, things named after 60 s); the real
creator transcript stays out of git.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.services import creator_sessions

# (start_s, end_s, text) — times shaped like the fb 1 take (pauses between sentences).
TAKE = [
    (0.6, 4.7, "Bugün Kadıköy çarşısında yürüyerek bir yemek turu yapıyoruz."),
    (5.5, 10.5, "İlk durağımız balıkçılar sokağı, sabahın erken saatinde bile çok kalabalık."),
    (11.4, 18.4, "Tezgâhlarda lüfer, palamut ve istavrit var, fiyatlar tahtaya tek tek yazılmış."),
    (19.6, 24.0, "Mesele sadece balık değil, burada herkes birbirini tanıyor."),
    (24.7, 29.3, "Esnafla sohbet etmeden buradan geçmek neredeyse imkânsız."),
    (30.8, 35.8, "Sonra köşedeki fırından sıcak bir simit aldık ve yürümeye devam ettik."),
    (37.5, 42.6, "Moda'ya doğru inerken yol üstündeki eski kitapçılara uğradık."),
    (43.0, 44.9, "Bir tanesinde çok eski bir harita bulduk."),
    (45.7, 54.1, "Öğle yemeği için meşhur bir esnaf lokantasında kuru fasulye ve pilav yedik."),
    (56.5, 59.6, "Tatlı olarak da kazandibi söyledik, gerçekten çok iyiydi."),
    (60.6, 64.3, "Moda sahilinde gün batımını izlemek için banklara oturduk."),
    (64.9, 72.3, "Rüzgâr biraz serindi ama manzara her şeye değdi, adalar tam karşımızdaydı."),
    (73.8, 75.5, "Sizce bir sonraki turu nerede yapalım?"),
    (75.8, 79.3, "Yorumlara yazın, en çok istenen yere gidelim."),
]
LATE_ANCHOR = TAKE[10]  # "Moda sahilinde ..." at 60.6 s — the part fb 1's planner never saw


def _talking_analysis(rows: list[tuple[float, float, str]], *, duration_s: float = 82.7) -> dict:
    return {
        "duration_s": duration_s,
        "understanding": {
            "subject": "a man talking to the camera",
            "speech": {
                "has_speech": True,
                "to_camera": True,
                "language": "tr",
                "transcript": " ".join(text for _, _, text in rows),
                "segments": [{"start_s": s, "end_s": e, "text": t} for s, e, t in rows],
            },
        },
    }


def _item(assignments: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="talking_to_camera",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=assignments,
    )


def _clip(media_id: str, analysis: dict | None, *, duration_s: float = 82.7) -> dict:
    row = {
        "media_id": media_id,
        "gcs_path": f"users/u/{media_id}",
        "kind": "video",
        "duration_s": duration_s,
    }
    if analysis is not None:
        row["analysis"] = analysis
    return row


def _visual(analysis: dict | None, *, kind: str = "video") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=kind,
        status="ready",
        duration_s=8.4 if kind == "video" else None,
        aspect=None,
        user_context=None,
        analysis=analysis,
        source_filename="IMG_8103.MP4",
        created_at=None,
    )


async def _resolve(item, visuals=()):
    assets = MagicMock()
    assets.scalars.return_value = list(visuals)
    tracks = MagicMock()
    tracks.scalars.return_value = []
    sfx = MagicMock()
    sfx.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [assets, tracks, sfx]
    return await creator_sessions.resolve_item_creator_context(
        db, item, persona=SimpleNamespace(user_id=uuid.uuid4())
    )


def _segments(entry: dict) -> list[dict]:
    return (entry.get("analysis_only_not_copy") or {}).get("speech", {}).get("segments", [])


def _chars(segments: list[dict]) -> int:
    return sum(len(seg["text"]) for seg in segments)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(creator_sessions.settings, "speech_excerpt_montage_enabled", True)
    monkeypatch.setattr(creator_sessions.settings, "speaker_background_enabled", False)
    monkeypatch.setattr(creator_sessions.settings, "ios_native_device_only_enabled", False)


@pytest.mark.asyncio
async def test_fb1_long_talking_take_reaches_the_planner_prompt_whole() -> None:
    """F1: the incident. The sentence said at 60.6 s must be quotable by the planner."""
    # Precondition: the late anchor lies beyond the old 480-character view (fb 1: sentence 6 of 14).
    assert _chars([{"text": t} for _, _, t in TAKE[: TAKE.index(LATE_ANCHOR) + 1]]) > 480

    manifest, media_context = await _resolve(_item([_clip("take.mp4", _talking_analysis(TAKE))]))
    segments = _segments(media_context[0])

    # Every sentence, in spoken order, with its own source times (nothing re-attributed).
    assert [(s["s"], s["e"], s["text"]) for s in segments] == [
        (round(s, 1), round(e, 1), t) for s, e, t in TAKE
    ]

    prompt = MainCreatorAgent(None).render_prompt(
        MainCreatorInput(
            user_message=(
                "Arkamda hangi fotoğraftan bahsediyorsam o olsun, ben sağ altta küçük olayım."
            ),
            media_context=media_context,
            capability_manifest=manifest,
        )
    )
    late = json.dumps(
        {"s": LATE_ANCHOR[0], "e": LATE_ANCHOR[1], "text": LATE_ANCHOR[2]}, ensure_ascii=False
    )
    assert late in prompt
    # Wrong-content control: speech the take never had must not appear.
    assert "Galata Kulesi" not in prompt


@pytest.mark.asyncio
async def test_clips_without_speech_do_not_take_a_share_of_the_budget() -> None:
    """F4: a talking take next to silent b-roll still reaches the planner whole."""
    clips = [_clip("take.mp4", _talking_analysis(TAKE))] + [
        _clip(f"broll-{n}.mp4", {"understanding": {"subject": "street"}}, duration_s=5.0)
        for n in range(5)
    ]
    _manifest, media_context = await _resolve(_item(clips))

    assert len(_segments(media_context[0])) == len(TAKE)
    assert all(not _segments(entry) for entry in media_context[1:])


@pytest.mark.asyncio
async def test_talking_clips_share_one_bounded_budget() -> None:
    """F2/F3: several long talking clips split the budget; the total never exceeds
    max(budget, 480 per clip), i.e. never more than the old worst case for many clips."""
    long_take = [
        (
            float(i * 4),
            float(i * 4 + 3),
            f"Bu {i}. cümle, konuşma biraz uzun sürüyor ve devam ediyor.",
        )
        for i in range(60)
    ]

    for n in (1, 3, 20):
        clips = [
            _clip(f"take-{k}.mp4", _talking_analysis(long_take, duration_s=240.0), duration_s=240.0)
            for k in range(n)
        ]
        _manifest, media_context = await _resolve(_item(clips))
        per_clip = [_chars(_segments(entry)) for entry in media_context]

        assert all(chars > 0 for chars in per_clip)
        assert sum(per_clip) <= max(creator_sessions.CHAT_EVIDENCE_SPEECH_BUDGET_CHARS, 480 * n)
        # whole sentences only, in order, from the start of each clip
        for entry in media_context:
            texts = [seg["text"] for seg in _segments(entry)]
            assert texts == [t for _, _, t in long_take[: len(texts)]]
    # a single clip gets far more than the old 480 characters
    _manifest, single = await _resolve(
        _item([_clip("take.mp4", _talking_analysis(long_take, duration_s=240.0), duration_s=240.0)])
    )
    assert _chars(_segments(single[0])) > 2000


@pytest.mark.asyncio
async def test_visuals_keep_the_old_speech_cap() -> None:
    """F5: a Visual's own speech is not the narration; its evidence is unchanged."""
    visual = _visual(_talking_analysis(TAKE, duration_s=8.4))
    _manifest, media_context = await _resolve(
        _item([_clip("take.mp4", _talking_analysis(TAKE))]), [visual]
    )

    pool = [entry for entry in media_context if str(entry.get("media_id", "")).startswith("asset-")]
    assert len(pool) == 1
    assert 0 < _chars(_segments(pool[0])) <= 480
    assert pool[0]["analysis_only_not_copy"] == creator_sessions._chat_evidence(
        visual.analysis, kind="video"
    )


@pytest.mark.asyncio
async def test_speech_length_never_changes_the_manifest() -> None:
    """F9: more speech for the planner must not shift the confirm fence's manifest hash."""
    item = _item([_clip("take.mp4", _talking_analysis(TAKE[:2]))])
    short_manifest, _ = await _resolve(item)
    item.clip_assignments = [_clip("take.mp4", _talking_analysis(TAKE))]
    long_manifest, _ = await _resolve(item)

    assert short_manifest.model_dump() == long_manifest.model_dump()
