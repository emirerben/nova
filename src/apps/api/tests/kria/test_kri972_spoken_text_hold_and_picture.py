"""KRI-972 / KRI-747 / KRI-1018: the text on a spoken word, on the Don Julio lunch T1 take.

Prompt: "When he says "grilled cheese", put PROVOLETA big on screen for about a second."
The take (``kri970_provolone_takes.json`` T1, job 81ce940b, whisper-1 words as prod stored them)
says "Provolone, grilled cheese." once, at 5.56 s / 6.32 s. Prod timed the text on "Provolone"
(0.76 s early) and held it 2.0 s; a letterboxed render drew the same row at y 0.15, which is
the black bar (Plaza de Mayo T2, job maximo 2).

Replayed with no model and no network, through the real boundaries: detector -> the question ->
the creator's tap -> the brief the tap's message rewrote -> ``resolve_choices`` ->
``answered_brief`` -> ``BriefBinding`` -> the real phone Talking job
(``_run_phone_subtitled_job``) -> the receipts.

Failure modes, written before the code:

1. The tap sends the option's sentence back and the brief's trigger comes out as "Provolone
   grilled cheese": the answer's digest no longer matches, the same question is asked again
   about the longer words and the second pick times "Provolone". (The picked words must be the
   creator's own: "grilled cheese".)
2. The fix over-reaches: a real change of the creator's words after the answer ("actually
   'melted cheese'") is narrowed back, or a trigger no answered question was about is shortened.
   (Only a run of the widened trigger that reproduces the answered question's EXACT digest.)
3. "about a second" stays in ``description``: the hold is 2.0 s and the receipt says "Done".
   Also: seconds inside the on-screen copy ("3 SECONDS LEFT") read as a hold; two different
   lengths in one ask pick one; a take that ends before the hold clips it (not a failure).
   Numbers that are not a hold must not become one (round 2, written before the fix): "within
   the first 5 seconds when he says cheese" (a window for the WORD), "in the first 5 seconds",
   "after 3 seconds", "for the first 5 seconds of the video", "for a second time he says it"
   (a count), "a second later", "about a second after he says cheese" (an offset), "in 2
   seconds", "no longer than 2 seconds", Turkish "ilk 5 saniye içinde" / "bir saniye sonra".
4. The receipt checks the hold only on the happy path: a 2.0 s row for a 1.0 s ask must be
   "partial".
5. Flag off (``PHONE_SUBTITLED_SPOKEN_TEXT_ENABLED=false``): the contract still skips the pin, so
   nothing answers the ask and the render is silently looser than before KRI-707.
6. The spoken-text row wears the title's role: ``plan_review`` files it under "title", the
   whole-text-span timing check and the editor's title classifier take it for the title.
7. A literal over 80 characters is cut and the receipt does not say so.
8. Letterbox: the row's preset y 0.15 is the black bar; a portrait/fill render must stay exactly
   as it was; a row taller than the picture band must shrink; a face in the picture is still
   avoided; the receipt reports "Done" from the row's id, not from a measurement of the row as
   drawn (a restyled or removed row, or one left in the bar, is not "met").
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.pipeline.phone_subtitled_title as title_mod
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.plan_review import section_of_text_bar
from app.kria.spoken_text_ask import (
    brief_spoken_texts,
    requirement_spoken_text,
    spoken_text_hold_s,
)
from app.pipeline.phone_spoken_text import find_spoken_word
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.services.choice_questions import (
    CONFLICT_SPEECH_OCCURRENCE,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    collect_conflicts,
    resolve_choices,
)
from app.services.clip_speech import SilenceDetectionResult
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.length_policy_state import (
    LENGTH_POLICIES_FIELD,
    LENGTH_POLICY_FIELD,
    stamp_length_policy,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests._silent_pcm import silent_pcm
from tests.pipeline.test_phone_subtitled_plan import _binding as _wide_binding
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TAKE = json.loads((_FIXTURES / "kri970_provolone_takes.json").read_text())["takes"]["T1"]
WORDS = TAKE["words"]
DURATION = float(TAKE["duration_s"])
THREAD = "867d2f93-0000-4000-8000-00000000d0ca"
MEDIA = "c0"
PROMPT = 'When he says "grilled cheese", put PROVOLETA 🧀 big on screen for about a second.'
# "Provolone," 5.56 -> "grilled" 6.32 -> "cheese." 6.32 .. 6.80 in the take.
PROVOLONE_S, GRILLED_S = 5.56, 6.32
FACE = NormalizedBox(0.34, 0.14, 0.66, 0.40)
STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "edit_format": "subtitled",
    "media_scope": "selected",
    "caption_style": "clean",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "target_duration_s": 8,
    "selected_media_ids": [MEDIA],
}
CAP = ChoiceCapability(speech_occurrence=True)
_CANVAS = SimpleNamespace(width=1080, height=1920)


def _segments() -> list[dict]:
    segments: list[dict] = []
    current: list[dict] = []
    for word in WORDS:
        current.append(word)
        if word["text"][-1] in ".?":
            segments.append(
                {
                    "text": " ".join(w["text"] for w in current),
                    "start_s": current[0]["start_s"],
                    "end_s": current[-1]["end_s"],
                    "words": copy.deepcopy(current),
                }
            )
            current = []
    return segments


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": MEDIA,
                "kind": "video",
                "duration_s": DURATION,
                "analysis": {
                    "duration_s": DURATION,
                    "understanding": {"speech": {"segments": _segments()}},
                },
            }
        ]
    }


def _brief(
    triggers=("grilled cheese",),
    *,
    description="big on screen for about a second",
    literal="PROVOLETA 🧀",
    extra_facts=None,
) -> CreativeBrief:
    return CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r3",
                kind="text",
                scope="global",
                literal=literal,
                description=description,
                facts={"triggers": list(triggers), "speaker": "he", **(extra_facts or {})},
            )
        ]
    )


def _tap(choice, ordinal: int = 1) -> list[tuple[str, dict]]:
    question = build_choice_question(choice.candidate())
    key = next(o.key for o in choice.options if o.key.startswith(f"occ_{ordinal}_of_"))
    return [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {"choice_selection": {"question_id": question["question_id"], "option_key": key}},
        ),
    ]


def _ask(brief):
    return [
        c
        for c in collect_conflicts(STRATEGY, brief, _snapshot(), CAP)
        if c.kind == CONFLICT_SPEECH_OCCURRENCE
    ]


def _approve(brief, events):
    resolution = resolve_choices(STRATEGY, brief, _snapshot(), events, CAP)
    final = answered_brief(brief, resolution.strategy)
    binding = BriefBinding.create(
        THREAD,
        final,
        media_snapshot=_snapshot(),
        choice_answers=list(resolution.strategy.get("choice_answers") or []),
    )
    return final, binding, resolution


# -- the real phone Talking job ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _phone_draws_text(monkeypatch):
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def _words() -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in WORDS
    ]


def _preflight_snapshot(storage_path: str) -> dict:
    fingerprint = "b" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DURATION,
            source_window_start_s=0.0,
            source_window_end_s=DURATION,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=_words(), language="en", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
        pcm_loader=silent_pcm,
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": MEDIA,
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _run_job(monkeypatch, binding: BriefBinding, brief: CreativeBrief, *, letterbox=False) -> dict:
    """The approved take through the real phone Talking worker (cleanup off, the checked
    preflight words on the job), with ``binding`` pinned. ``letterbox``: a 1920x1080 take
    framed with bars (``landscape_fit: fit``), the Plaza de Mayo shape."""

    def faces(path, anchors, **kwargs):
        return [ProtectedRegion(0.0, DURATION, FACE, "face")], {
            "attempted": len(anchors),
            "decoded": len(anchors),
        }

    monkeypatch.setattr(title_mod, "sample_face_regions", faces)
    job, snapshot, _session, phone = _setup_subtitled(
        monkeypatch, duration_s=DURATION, transcript_words=_words()
    )
    if letterbox:
        wide = _wide_binding(MEDIA, duration_s=DURATION, width=1920, height=1080)
        job.assembly_plan[PHONE_SOURCES_FIELD] = [wide.model_dump(mode="json")]
        snapshot[PHONE_SOURCES_FIELD] = [wide.model_dump(mode="json")]
        job.all_candidates["landscape_fit"] = "fit"
    job.all_candidates.update(
        {"creator_strategy": copy.deepcopy(STRATEGY), REQUIREMENT_VERSION_FIELD: 1}
    )
    contract = build_render_contract(
        STRATEGY,
        generation_id="generation",
        brief=brief,
        media_snapshot=_snapshot(),
        clip_order=[MEDIA],
    )
    assert contract is not None
    snapshot.update(
        {
            "speech_cleanup_contract": "off_v1",
            "creator_brief_binding": binding.model_dump(mode="json"),
            "_speech_cleanup_internal": {
                "preflight_snapshot": _preflight_snapshot(phone.proxy_path)
            },
            CONTRACT_FIELD: contract.model_dump(mode="json"),
        }
    )
    snapshot.update(stamp_length_policy({}, STRATEGY, contract, mode="log_only"))
    for key in (
        "speech_cleanup_contract",
        "creator_brief_binding",
        "_speech_cleanup_internal",
        CONTRACT_FIELD,
        LENGTH_POLICY_FIELD,
        LENGTH_POLICIES_FIELD,
    ):
        job.assembly_plan[key] = copy.deepcopy(snapshot[key])
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _receipt_for(final: CreativeBrief, variant: dict):
    [receipt] = build_receipts(
        [r for r in final.live() if r.id == "r3"],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    return receipt


def _luma(rgb) -> float:
    r, g, b = (c / 255 for c in rgb[:3])
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b) -> float:
    hi, lo = sorted((_luma(a), _luma(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _frame_proof(row: dict, band: tuple[float, float], face: dict, name: str) -> dict:
    """The row drawn by the real Skia renderer over a letterboxed frame (black bars, a bright
    sky picture with the speaker's dark head where the face is), measured on the pixels:
    how much of the ink is in the picture and the contrast the text has against what it sits
    on. Returns the measurements; writes ``<name>.png`` next to the report."""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.overlay_verify import render_overlay_frame

    width, height = 1080, 1920
    frame = Image.new("RGB", (width, height), (0, 0, 0))
    top, bottom = round(band[0] * height), round(band[1] * height)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, top, width, bottom), fill=(150, 200, 245))  # bright sky
    draw.ellipse(  # the speaker's head, where the placement sampled the face (canvas fractions)
        (
            face["left"] * width,
            face["top"] * height,
            face["right"] * width,
            face["bottom"] * height,
        ),
        fill=(70, 50, 40),
    )
    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)],
        video_duration_s=max(float(row["end_s"]), 0.1),
        independent_box_alignment=True,
    )
    layer = render_overlay_frame(overlay).resize((width, height))
    alpha = np.asarray(layer)[..., 3]
    ink = alpha > 40
    drawn = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
    rows = np.where(ink.any(axis=1))[0]
    inside = float(ink[top:bottom].sum()) / max(1, int(ink.sum()))
    arr = np.asarray(drawn).astype(int)
    solid = alpha > 250
    fill = arr[solid & (arr.sum(axis=2) > 600)]
    stroke = arr[solid & (arr.sum(axis=2) < 60)]
    ring = ink & ~solid
    result = {
        "ink_rows": [int(rows.min()), int(rows.max())] if rows.size else None,
        "picture_rows": [top, bottom],
        "ink_inside_picture": round(inside, 4),
        "fill_pixels": int(len(fill)),
        "stroke_pixels": int(len(stroke)),
    }
    if len(fill) and len(stroke):
        result["fill_vs_stroke_contrast"] = round(
            _contrast(fill.mean(axis=0), stroke.mean(axis=0)), 2
        )
        around = np.asarray(frame).astype(int)[ring] if ring.any() else np.zeros((0, 3))
        sky = np.asarray(frame).astype(int)[ink & (np.asarray(frame).sum(axis=2) > 0)]
        if len(sky):
            result["stroke_vs_picture_contrast"] = round(
                _contrast(stroke.mean(axis=0), sky.mean(axis=0)), 2
            )
        del around
    target = os.environ.get("KRI972_REPORT_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        drawn.resize((width // 2, height // 2)).save(Path(target) / f"{name}.png")
    return result


def _dump(name: str, payload: dict) -> None:
    target = os.environ.get("KRI972_REPORT_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str))


# -- 1 + 3 + 4: the incident, end to end ---------------------------------------------------------


def test_t1_provoleta_after_the_tap_lands_on_grilled_for_about_a_second(monkeypatch):
    original = _brief()
    [choice] = _ask(original)
    assert "grilled cheese" in choice.intro
    assert choice.options[0].label.startswith("0:06 “…cheese. Okay. Provolone, grilled cheese.")
    events = _tap(choice)

    # The brief the tap's message rewrote (prod brief r3, turn 2): the option sentence's words
    # ride into the trigger.
    rewritten = _brief(("Provolone grilled cheese",))

    # Control (failure mode 1): the rewritten words alone time "Provolone", 0.76 s early, and
    # the question about them is asked all over again.
    alone = brief_spoken_texts(rewritten)
    [raw] = alone
    assert raw.triggers == ("Provolone grilled cheese",) and raw.pin is None
    heard = find_spoken_word(WORDS, raw.triggers)
    assert heard is not None and heard[1] == pytest.approx(PROVOLONE_S, abs=0.01)
    assert abs(heard[1] - GRILLED_S) > 0.3
    assert len(_ask(rewritten)) == 1

    # The fix: the answer still counts, the trigger is the creator's own words again, and the
    # pick is pinned to the occurrence they chose.
    final, binding, resolution = _approve(rewritten, events)
    assert resolution.question is None
    [answer] = binding.choice_answers
    assert answer["value"] == "grilled cheese"
    [ask] = brief_spoken_texts(final, binding.choice_answers)
    assert ask.triggers == ("grilled cheese",)
    assert ask.pin is not None and ask.pin.ordinal == 1 and ask.pin.of == 1
    assert ask.hold_s == 1.0

    variant = _run_job(monkeypatch, binding, final)
    [row] = [r for r in variant["text_elements"] if r["text"] == "PROVOLETA 🧀"]
    assert row["id"] == "spoken-text-r3"
    assert row["start_s"] == pytest.approx(GRILLED_S, abs=0.3)
    assert row["end_s"] - row["start_s"] == pytest.approx(1.0, abs=0.05)
    [placement] = variant["spoken_text_placement"]["rows"]
    assert placement["status"] == "placed" and placement["pinned"] is True
    assert placement["trigger"] == "grilled cheese"
    assert placement["hold_s"] == 1.0 and placement["hold_clipped"] is False

    receipt = _receipt_for(final, variant)
    assert receipt.status == "met", receipt
    _dump(
        "t1_provoleta",
        {
            "request": PROMPT,
            "question": choice.options[0].label,
            "answer": answer,
            "words_source_s": {"provolone": PROVOLONE_S, "grilled": GRILLED_S},
            "placed": row,
            "placement": placement,
            "receipt": receipt.model_dump(mode="json"),
        },
    )

    # 4: the same render claiming 2.0 s for a 1.0 s ask is not "met".
    longer = copy.deepcopy(variant)
    for element in longer["text_elements"]:
        if element["id"] == "spoken-text-r3":
            element["end_s"] = element["start_s"] + 2.0
    for entry in longer["spoken_text_placement"]["rows"]:
        entry["end_s"] = entry["start_s"] + 2.0
    wrong = _receipt_for(final, longer)
    assert wrong.status == "partial" and "not the 1 s you asked for" in (wrong.reason or "")


def test_a_real_change_of_words_after_the_answer_is_not_narrowed_back():
    original = _brief()
    [choice] = _ask(original)
    events = _tap(choice)
    # The creator changed their mind about the words: not a widening of what was answered.
    changed = _brief(("melted cheese",))
    final, _binding, resolution = _approve(changed, events)
    assert [a for a in resolution.strategy.get("choice_answers", [])] == []
    [ask] = brief_spoken_texts(final)
    assert ask.triggers == ("melted cheese",)


def test_a_widened_trigger_no_answer_was_about_is_left_alone():
    # No question was ever answered: nothing to restore, whatever the trigger looks like.
    final, _binding, resolution = _approve(_brief(("Provolone grilled cheese",)), [])
    assert resolution.question is not None
    [ask] = brief_spoken_texts(final)
    assert ask.triggers == ("Provolone grilled cheese",)


@pytest.mark.parametrize(
    ("description", "literal", "facts", "expected"),
    [
        # How long the TEXT stays.
        ("big on screen for about a second", "X", None, 1.0),
        ("show it for 3 seconds", "X", None, 3.0),
        ("for about two seconds", "X", None, 2.0),
        ("for just a second", "X", None, 1.0),
        ("keep it up for 2,5 seconds, big", "X", None, 2.5),
        ("let it stay 2 seconds", "X", None, 2.0),
        ("show it for a second.", "X", None, 1.0),
        ("bir saniye boyunca ekranda olsun", "X", None, 1.0),
        ("2 saniye boyunca göster", "X", None, 2.0),
        ("3 saniye kalsın", "X", None, 3.0),
        # Seconds that are not a hold (round 2).
        ("put X on screen within the first 5 seconds when he says cheese", "X", None, None),
        ("in the first 5 seconds he says cheese, show it big", "X", None, None),
        ("after 3 seconds of him talking, show it", "X", None, None),
        ("for the first 5 seconds of the video, when he says cheese", "X", None, None),
        ("for a second time he says it, show X big", "X", None, None),
        ("the text should appear a second later", "X", None, None),
        ("about a second after he says cheese", "X", None, None),
        ("when he says cheese in 2 seconds", "X", None, None),
        ("no longer than 2 seconds", "X", None, None),
        ("ilk 5 saniye içinde peynir dediğinde", "X", None, None),
        ("peynir dedikten bir saniye sonra", "X", None, None),
        # A real hold next to a number that is not one.
        ("within the first 5 seconds, show it for about a second", "X", None, 1.0),
        # No length, the copy's own seconds, two holds, a quote, a negation, a typed field.
        ("big on screen for a moment", "X", None, None),
        ("put 3 SECONDS LEFT big", "3 SECONDS LEFT", None, None),
        ("for 3 SECONDS LEFT, show it for 2 seconds", "3 SECONDS LEFT", None, 2.0),
        ("for 1 second, for 2 seconds", "X", None, None),
        ("don't keep it for 2 seconds", "X", None, None),
        ("big on screen", "X", {"hold_s": 1.5}, 1.5),
        ("big on screen", "X", {"hold_s": 400}, None),
        ("within the first 5 seconds", "X", {"hold_s": 1.5}, 1.5),
        # KRI-1292: the typed hold decides; the wording is only for briefs that predate it.
        ("for 1 second, no, 2 seconds", "X", {"hold_s": 2.0}, 2.0),
        ("within the first 5 seconds when he says cheese", "X", {"hold_s": 1.0}, 1.0),
        ("peynir dediğinde ekranda dursun", "X", {"hold_s": 1}, 1.0),
        ("big on screen for a second", "X", {"hold_s": 3}, 3.0),
        ("big on screen for a second", "X", {"hold_s": 400}, 1.0),
        ("big on screen", "X", {"hold_s": 400}, None),
        ("big on screen", "X", {"hold_s": "1.5"}, 1.5),
        ("big on screen", "X", {"hold_s": 0}, None),
        ("big on screen", "X", {"hold_s": -2}, None),
        ("big on screen", "X", {"hold_s": float("nan")}, None),
        ("big on screen for 2 seconds", "X", {"hold_s": True}, 2.0),
        ("big on screen for 2 seconds", "X", {"hold_s": "a second"}, 2.0),
    ],
)
def test_the_hold_is_the_creators_own_length_or_nothing(description, literal, facts, expected):
    req = _brief(description=description, literal=literal, extra_facts=facts).requirements[0]
    assert spoken_text_hold_s(req) == expected
    ask = requirement_spoken_text(req)
    assert ask is not None and ask.hold_s == expected


@pytest.mark.parametrize(
    ("hold_s", "stored"),
    [
        (1.0, 1.0),
        (2, 2.0),
        ("1.5", 1.5),
        ("1,5", 1.5),
        (0.1, None),
        (0, None),
        (-1, None),
        (400, None),
        ("a second", None),
        (True, None),
        (None, None),
        (float("inf"), None),
    ],
)
def test_the_model_written_hold_is_a_length_or_not_stored(hold_s, stored):
    from app.kria.brief import parse_brief_updates

    [update] = parse_brief_updates(
        [
            {
                "kind": "text",
                "scope": "global",
                "literal": "PROVOLETA",
                "description": "big on screen for about a second",
                "facts": {"triggers": ["grilled cheese"], "hold_s": hold_s},
            }
        ]
    )
    assert update.facts.get("hold_s") == stored
    assert update.facts["triggers"] == ["grilled cheese"]


def test_a_photo_hold_is_not_a_text_hold():
    from app.kria.brief import parse_brief_updates

    [update] = parse_brief_updates(
        [
            {
                "kind": "timing",
                "scope": "clip:p1",
                "description": "keep the photo for 2 seconds",
                "facts": {"hold_s": "2"},
            }
        ]
    )
    assert update.facts["hold_s"] == "2"


def _through_the_brief(prior: CreativeBrief, raw_updates: list[dict]) -> CreativeBrief:
    """The product's own update path: parse -> bind to the shown brief -> apply."""
    from app.kria.brief import apply_updates, bind_updates, parse_brief_updates

    bound = bind_updates(prior, parse_brief_updates(raw_updates))
    return apply_updates(prior, bound, source_turn_id="t2")


def _live(brief: CreativeBrief) -> BriefRequirement:
    return brief.live()[0]


@pytest.mark.parametrize(
    ("hold_s", "stored"),
    [
        ("1,5", 1.5),
        ("2", 2.0),
        (3, 3.0),
        (400, None),
        (0, None),
        (-1, None),
        ("a second", None),
        (True, None),
        (None, None),
    ],
)
def test_a_partial_change_to_a_text_stores_a_length_or_none(hold_s, stored):
    # The correction turn: target + facts, no kind. It used to skip the boundary and keep "1,5".
    prior = _brief(extra_facts={"hold_s": 1.0})
    after = _through_the_brief(
        prior,
        [{"operation": "change", "target_requirement_id": "r3", "facts": {"hold_s": hold_s}}],
    )
    req = _live(after)
    assert req.kind == "text" and req.facts.get("hold_s") == stored
    assert req.facts["triggers"] == ["grilled cheese"]


def test_a_change_that_does_not_touch_the_length_keeps_it():
    prior = _brief(extra_facts={"hold_s": 1.0})
    after = _through_the_brief(
        prior,
        [{"operation": "change", "target_requirement_id": "r3", "description": "in red"}],
    )
    assert _live(after).facts["hold_s"] == 1.0


def test_a_partial_change_to_a_photo_hold_is_left_as_written():
    prior = CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="clip:p1",
                description="keep the photo for 2 seconds",
                facts={"hold_s": 2},
            )
        ]
    )
    after = _through_the_brief(
        prior,
        [{"operation": "change", "target_requirement_id": "r1", "facts": {"hold_s": "3"}}],
    )
    assert _live(after).facts["hold_s"] == "3"


def test_a_length_changed_to_one_a_text_cannot_hold_falls_back_to_the_wording():
    # One rule for an out-of-range typed length, wherever it arrives: it is not stored, and the
    # creator's own words in the requirement decide (here "for about a second"), else the default.
    added = _through_the_brief(
        CreativeBrief(),
        [
            {
                "kind": "text",
                "scope": "global",
                "literal": "PROVOLETA",
                "description": "big on screen for about a second",
                "facts": {"triggers": ["grilled cheese"], "hold_s": 400},
            }
        ],
    )
    req = _live(added)
    assert "hold_s" not in req.facts and spoken_text_hold_s(req) == 1.0
    no_words = _through_the_brief(
        CreativeBrief(),
        [
            {
                "kind": "text",
                "scope": "global",
                "literal": "PROVOLETA",
                "description": "big on screen",
                "facts": {"triggers": ["grilled cheese"], "hold_s": 400},
            }
        ],
    )
    assert "hold_s" not in _live(no_words).facts and spoken_text_hold_s(_live(no_words)) is None
    changed = _through_the_brief(
        _brief(extra_facts={"hold_s": 2.0}),
        [{"operation": "change", "target_requirement_id": "r3", "facts": {"hold_s": 400}}],
    )
    assert "hold_s" not in _live(changed).facts
    assert spoken_text_hold_s(_live(changed)) == 1.0


def test_a_text_turned_into_a_timing_clip_does_not_keep_its_text_length():
    from app.kria.asset_holds import hold_seconds

    prior = _brief(extra_facts={"hold_s": 1.0})
    prior.requirements[0].scope = "clip:p1"
    after = _through_the_brief(
        prior,
        [
            {
                "operation": "change",
                "target_requirement_id": "r3",
                "kind": "timing",
                "description": "show the photo",
            }
        ],
    )
    req = _live(after)
    assert req.kind == "timing" and hold_seconds(req) is None


def test_a_stored_brief_without_the_field_still_reads_its_wording():
    # A brief persisted before KRI-1292 is re-read on every retry and reburn.
    req = _brief(description="put it big on screen for about a second").requirements[0]
    assert "hold_s" not in req.facts and spoken_text_hold_s(req) == 1.0


def test_a_take_that_ends_before_the_hold_clips_it_without_failing_the_receipt(monkeypatch):
    # Said 0.5 s before the end of the take, asked for 3 s: the row is clipped, still "Done".
    req = _brief(("Perfecto",), description="for 3 seconds").requirements[0]
    ask = requirement_spoken_text(req)
    assert ask is not None and ask.hold_s == 3.0
    from app.pipeline.phone_spoken_text import spoken_text_element

    row = spoken_text_element(
        ask,
        start_s=DURATION - 0.5,
        timeline_duration_s=DURATION,
        canvas=SimpleNamespace(width=1080, height=1920),
    )
    assert row is not None and row["end_s"] == pytest.approx(DURATION)


# -- 5: the kill switch -----------------------------------------------------------------------


def test_flag_off_pins_the_literal_like_before_the_feature(monkeypatch):
    brief = _brief()

    def pinned_texts() -> list:
        contract = build_render_contract(
            STRATEGY,
            generation_id="generation",
            brief=brief,
            media_snapshot=_snapshot(),
            clip_order=[MEDIA],
        )
        assert contract is not None
        return [
            t for t in json.dumps(contract.model_dump(mode="json")).split('"') if "PROVOLETA" in t
        ]

    on = pinned_texts()
    monkeypatch.setattr(settings, "phone_subtitled_spoken_text_enabled", False)
    off = pinned_texts()
    assert on == [] and off, "flag off must pin the literal again (the pre-KRI-707 behaviour)"


# -- 6: not the title ---------------------------------------------------------------------------


def test_the_spoken_text_row_is_not_filed_under_the_title(monkeypatch):
    final, binding, _resolution = _approve(_brief(), [])
    variant = _run_job(monkeypatch, binding, final)
    [row] = [r for r in variant["text_elements"] if r["id"] == "spoken-text-r3"]
    assert row["role"] == "generative_intro"  # the renderers and the iOS editor draw this role
    assert section_of_text_bar(row) == "overlays"
    title = {**row, "id": "opening-title"}
    assert section_of_text_bar(title) == "title"

    from app.services.kria_editor_ops_text import classify

    bars = [
        {
            "id": "spoken-text-r3",
            "role": "generative_intro",
            "start_s": 0.2,
            "end_s": 1.2,
            "removed": False,
            "clip_id": None,
            "caption": False,
        }
    ]
    assert classify(bars)["spoken-text-r3"] != "title"


# -- 7: the cut literal ----------------------------------------------------------------------------


def test_a_literal_over_the_limit_is_cut_and_the_receipt_says_so(monkeypatch):
    long_literal = "PROVOLETA " * 10  # 100 characters
    brief = _brief(literal=long_literal.strip())
    final, binding, _resolution = _approve(brief, _tap(_ask(brief)[0]))
    variant = _run_job(monkeypatch, binding, final)
    [placement] = variant["spoken_text_placement"]["rows"]
    assert placement["status"] == "placed" and placement["truncated"] is True
    receipt = _receipt_for(final, variant)
    assert receipt.status == "partial" and "longer than fits on screen" in (receipt.reason or "")


# -- 8: letterbox ---------------------------------------------------------------------------------


def test_letterboxed_take_puts_the_big_text_inside_the_picture(monkeypatch):
    final, binding, _resolution = _approve(_brief(), _tap(_ask(_brief())[0]))
    variant = _run_job(monkeypatch, binding, final, letterbox=True)
    [row] = [r for r in variant["text_elements"] if r["id"] == "spoken-text-r3"]
    [placement] = variant["spoken_text_placement"]["rows"]
    band = placement["placement"]["picture_band"]
    assert band == pytest.approx([0.3418, 0.6582], abs=0.001)
    assert placement["placement"]["inside_picture"] is True
    # Light fill with an outline, spelled out on the row (never a default or a restyle).
    assert row["color"] == "#FFFFFF" and row["stroke_color"] == "#000000"
    assert row["stroke_width"] > 0
    box = title_mod._translate_centered_box_to_y(
        title_mod._measure_title_box(row, canvas=SimpleNamespace(width=1080, height=1920)),
        float(row["y_frac"]),
    )
    assert band[0] <= box.top and box.bottom <= band[1]
    # The face in the picture is still kept clear.
    assert placement["face_collision"] is False
    receipt = _receipt_for(final, variant)
    assert receipt.status == "met", receipt
    # The pixels. Before: the row as origin/main drew it (y 0.15, no look); after: this row.
    from app.kria.spoken_text_ask import requirement_spoken_text
    from app.pipeline.phone_spoken_text import spoken_text_element

    ask = requirement_spoken_text(final.requirements[0])
    before_row = spoken_text_element(
        ask, start_s=row["start_s"], timeline_duration_s=DURATION, canvas=_CANVAS
    )
    assert before_row is not None
    before = _frame_proof(
        before_row, tuple(band), placement["placement"]["face_band"], "letterbox_before"
    )
    after = _frame_proof(row, tuple(band), placement["placement"]["face_band"], "letterbox_after")
    assert before["ink_inside_picture"] == 0.0  # the old row is wholly in the black bar
    assert after["ink_inside_picture"] == 1.0
    assert after["fill_vs_stroke_contrast"] >= 3.0 and after["stroke_vs_picture_contrast"] >= 3.0
    _dump(
        "t2_letterbox",
        {"band": band, "placed": row, "placement": placement, "before": before, "after": after},
    )

    # Controls. The old preset (y 0.15) is the bar: the same check says "outside".
    in_bar = {**row, "y_frac": 0.15}
    assert (
        title_mod._picture_receipt(in_bar, tuple(band), SimpleNamespace(width=1080, height=1920))[
            "inside_picture"
        ]
        is False
    )
    # A receipt that says the row is in the bar is never "Done".
    outside = copy.deepcopy(variant)
    outside["spoken_text_placement"]["rows"][0]["placement"]["inside_picture"] = False
    bar = _receipt_for(final, outside)
    assert bar.status == "partial" and "black bars" in (bar.reason or "")
    # A restyled row (the look changed after the check) is not "Done" either.
    restyled = copy.deepcopy(variant)
    for element in restyled["text_elements"]:
        if element["id"] == "spoken-text-r3":
            element["color"] = "#000000"
    restyled_receipt = _receipt_for(final, restyled)
    assert restyled_receipt.status == "partial"
    # The wording names what was checked (the look the placement chose), never "readable".
    assert "light fill and dark outline" in (restyled_receipt.reason or "")
    assert "readable" not in (restyled_receipt.reason or "")
    # A row removed from the video is not "Done".
    gone = copy.deepcopy(variant)
    gone["text_elements"] = [e for e in gone["text_elements"] if e["id"] != "spoken-text-r3"]
    assert _receipt_for(final, gone).status != "met"


def test_a_portrait_take_is_placed_exactly_as_before(monkeypatch):
    final, binding, _resolution = _approve(_brief(), _tap(_ask(_brief())[0]))
    variant = _run_job(monkeypatch, binding, final)
    [row] = [r for r in variant["text_elements"] if r["id"] == "spoken-text-r3"]
    [placement] = variant["spoken_text_placement"]["rows"]
    assert "picture_band" not in placement["placement"] and "picture_look" not in placement
    assert row.get("color") is None and row.get("stroke_color") is None


@pytest.mark.parametrize("letterbox", [False, True])
def test_a_creator_colour_is_never_reported_as_honoured_by_the_picture_look(monkeypatch, letterbox):
    # The row's colours are the worker's (white on a letterbox, the title look otherwise); a
    # creator-asked colour is judged by the text-look check against the row as drawn.
    brief = _brief(extra_facts={"text_color": "#FFD400"})
    final, binding, _resolution = _approve(brief, _tap(_ask(brief)[0]))
    variant = _run_job(monkeypatch, binding, final, letterbox=letterbox)
    receipt = _receipt_for(final, variant)
    assert receipt.status != "met", receipt


def test_the_picture_band_is_the_speakers_and_ends_with_its_span():
    # The band is the first speaker's letterboxed picture. The same word said past that span
    # plays another clip (an extra speaker) whose picture is unknown: no band, no claim.
    [ask] = brief_spoken_texts(_brief())

    def placement(keep_segments):
        rows, receipt = gb._phone_talking_spoken_text_rows(
            [ask],
            words=_words(),
            cues=[],
            caption_style="clean",
            shown_rows=[],
            timeline_duration_s=DURATION,
            binding=_wide_binding(MEDIA, duration_s=DURATION, width=1920, height=1080),
            clip_path=None,
            keep_segments=keep_segments,
            landscape_fit="fit",
        )
        assert [r["id"] for r in rows] == ["spoken-text-r3"]
        return receipt["rows"][0]["placement"]

    inside = placement([(0.0, DURATION)])
    assert inside["picture_band"] == pytest.approx([0.3418, 0.6582], abs=0.001)
    assert inside["inside_picture"] is True
    past = placement([(0.0, GRILLED_S - 1.0)])
    assert "picture_band" not in past and "inside_picture" not in past
