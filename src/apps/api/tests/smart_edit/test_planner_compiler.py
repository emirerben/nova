from __future__ import annotations

from app.smart_edit.compiler import compile_smart_plan, resolve_sfx_placements
from app.smart_edit.planner import plan_smart_captions


def _cue(text: str, start: float, end: float) -> dict:
    tokens = text.split()
    step = (end - start) / len(tokens)
    return {
        "text": text,
        "start_s": start,
        "end_s": end,
        "words": [
            {
                "text": token,
                "start_s": round(start + index * step, 3),
                "end_s": round(start + (index + 1) * step, 3),
            }
            for index, token in enumerate(tokens)
        ],
    }


def test_turkish_talking_head_compiles_semantic_caption_number_title_and_sfx_lanes() -> None:
    cues = [
        _cue("Peki o dönem neden markalar maskot kullanıyordu?", 0.0, 2.8),
        _cue("Birincisi Selocanlar markayı hatırlatıyordu.", 4.0, 6.2),
        _cue("Şimdi başka bir konuya geçelim.", 8.0, 10.0),
        _cue("İkincisi Çelik ve Çeliknaz güven veriyordu.", 12.0, 14.5),
        _cue("Örneğin Selpak fili çok güçlüydü.", 17.0, 19.0),
        _cue("Sonuç olarak karakter markayı akılda tutuyordu.", 22.0, 24.2),
        _cue("Takip et, devamını birlikte inceleyelim.", 27.0, 29.0),
    ]

    planned = plan_smart_captions(cues, preset_version="cigdem-v1", language="tr")

    assert planned is not None
    assert [event.role for event in planned.document.events] == [
        "hook",
        "list_item",
        "context_shift",
        "list_item",
        "example",
        "payoff",
        "cta",
    ]
    assert planned.validation_receipt["normalized_word_count"] == sum(
        len(cue["words"]) for cue in cues
    )

    compiled = compile_smart_plan(planned.document, cues)

    assert [cue.get("smart_style") for cue in compiled.caption_cues] == [
        "hook",
        "list_item",
        "context",
        "list_item",
        "example",
        "payoff",
        "cta",
    ]
    assert [element["text"] for element in compiled.text_elements] == [
        "1",
        "Şimdi başka bir konuya geçelim.",
        "2",
    ]
    numbers = [element for element in compiled.text_elements if element["text"].isdigit()]
    assert all(element["y_frac"] == 0.075 for element in numbers)
    assert all(element["font_family"] == "Inter-Bold" for element in compiled.text_elements)
    assert [intent["at_s"] for intent in compiled.sfx_intents] == [4.0, 12.0, 27.0]
    assert compiled.boundary_effects == [
        {
            "event_id": planned.document.events[2].event_id,
            "effect": "soft_whip",
            "at_s": 8.0,
        }
    ]


def test_smart_sfx_never_falls_back_to_unrelated_voice_clip() -> None:
    intents = [{"event_id": "0" * 24, "intent": "pop_in", "at_s": 5.0, "gain": 0.68}]
    female_voice_only = [
        {
            "id": "voice",
            "name": "woman says wow",
            "audio_gcs_path": "sound-effects/voice/audio.mp3",
            "duration_s": 1.0,
        }
    ]
    assert resolve_sfx_placements(intents, female_voice_only) == []

    misleading_voice_match = [
        {
            "id": "voice-pop",
            "name": "female pop voice",
            "audio_gcs_path": "sound-effects/voice-pop/audio.mp3",
            "duration_s": 0.4,
        }
    ]
    assert resolve_sfx_placements(intents, misleading_voice_match) == []

    clean_pop = female_voice_only + [
        {
            "id": "pop",
            "name": "clean soft pop",
            "audio_gcs_path": "sound-effects/pop/audio.wav",
            "duration_s": 0.3,
        }
    ]
    resolved = resolve_sfx_placements(intents, clean_pop)
    assert len(resolved) == 1
    assert resolved[0]["sound_effect_id"] == "pop"
    assert resolved[0]["gain"] == 0.68


def test_planner_returns_none_for_empty_caption_input() -> None:
    assert plan_smart_captions([], preset_version="cigdem-v1", language="tr") is None
