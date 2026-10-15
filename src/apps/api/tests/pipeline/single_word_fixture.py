"""Shared inputs for the single-word caption tests and their off-state golden.

A ~190 s Talking cue set with per-word timings: the shape the reported long talk
had (hundreds of words, one cue every few seconds). Everything is derived
arithmetically, so the golden is reproducible on any machine.
"""

from __future__ import annotations

import hashlib
import json

from app.pipeline.phone_captions import PhoneCaptionLook
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from tests.pipeline.test_phone_subtitled_plan import _binding

TALK_SECONDS = 190.0
_VOCAB = [
    "Merhaba",
    "arkadaşlar",
    "bugün",
    "size",
    "İstanbul'daki",
    "kahve",
    "dükkanını",
    "gösteriyorum",
    "and",
    "then",
    "we",
    "walked",
    "over",
    "the",
    "bridge",
    "Güzel!",
    "ok,",
    "yer.",
]


def talk_cues(*, seconds: float = TALK_SECONDS, words_per_cue: int = 9) -> list[dict]:
    cues: list[dict] = []
    step = 3.2
    cursor = 0.0
    index = 0
    while cursor + step <= seconds:
        words = []
        slice_s = (step - 0.2) / words_per_cue
        for position in range(words_per_cue):
            start = round(cursor + position * slice_s, 3)
            words.append(
                {
                    "text": _VOCAB[(index + position * 5) % len(_VOCAB)],
                    "start_s": start,
                    "end_s": round(start + slice_s * 0.9, 3),
                }
            )
        cues.append(
            {
                "text": " ".join(word["text"] for word in words),
                "start_s": round(cursor, 3),
                "end_s": round(cursor + step - 0.1, 3),
                "words": words,
            }
        )
        cursor += step
        index += 1
    return cues


def talk_recipe(*, caption_style: str, look: PhoneCaptionLook | None, cues=None):
    return compile_phone_subtitled_plan(
        (_binding(duration_s=TALK_SECONDS + 10),),
        caption_cues=talk_cues() if cues is None else cues,
        caption_style=caption_style,
        caption_look=look,
    )


def recipe_digest(recipe) -> str:
    dumped = recipe.model_dump(mode="json")
    # A set on the model: its list order follows PYTHONHASHSEED, its content is the contract.
    dumped["required_capabilities"] = sorted(dumped["required_capabilities"])
    payload = json.dumps(dumped, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


# Looks whose output must not move while singleWordText is off/unqualified.
OFF_STATE_CASES: dict[str, tuple[str, dict]] = {
    "word-unset": ("word", {}),
    "word-highlight-true": ("word", {"highlight_spoken_word": True}),
    "word-highlight-false": ("word", {"highlight_spoken_word": False}),
    "sentence-unset": ("sentence", {}),
    "sentence-highlight-true": ("sentence", {"highlight_spoken_word": True}),
    "word-left-highlight-false": (
        "word",
        {
            "highlight_spoken_word": False,
            "text_anchor": "left",
            "position_x_frac": 80 / 1080,
            "text_size_px": 96,
        },
    ),
}


def off_state_digests() -> dict[str, str]:
    return {
        name: recipe_digest(
            talk_recipe(caption_style=style, look=PhoneCaptionLook(**look) if look else None)
        )
        for name, (style, look) in OFF_STATE_CASES.items()
    }


# --- the editor Save path ---------------------------------------------------------

_SAVE_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "soundEffects",
    "visualBlocks",
    "alphaOverlay",
    "stillImages",
    "visualVideos",
]


def talk_job(monkeypatch, *, verified: list[str] | None = None):
    """A phone Talking variant (pinned revision 1, sentence captions) over the long cue set."""
    import uuid
    from types import SimpleNamespace

    from app.kria.device_render import make_device_request
    from app.routes import generative_jobs as gj
    from app.services.device_render import pin_device_request
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", _SAVE_FEATURES + list(verified or [])
    )
    cues = talk_cues()
    bindings = (_binding(duration_s=TALK_SECONDS + 10),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=cues)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": cues,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def save_word_style(job, *, highlight: bool | None, base_generation: str = "first"):
    """Save `Display: Word` (+ the highlight setting) the way the editor posts it."""
    from app.routes import generative_jobs as gj

    appearance = (
        gj.EditorCaptionAppearance(highlight_spoken_word=highlight)
        if highlight is not None
        else None
    )
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=base_generation,
            caption_meta=gj.EditorCommitCaptionMeta(style="word", appearance=appearance),
        ),
        user_id="owner",
        plan_item_id="item",
    )


def saved_recipe(job):
    from app.services.device_render import device_status

    return device_status(job, "subtitled").request.recipe


def save_digests() -> dict[str, str]:
    import pytest

    result = {}
    for name, highlight in (
        ("save-word-unset", None),
        ("save-word-true", True),
        ("save-word-false", False),
    ):
        with pytest.MonkeyPatch.context() as monkeypatch:
            job = talk_job(monkeypatch)
            save_word_style(job, highlight=highlight)
            result[name] = recipe_digest(saved_recipe(job))
    return result
