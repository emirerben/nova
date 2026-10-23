"""KRI-839: word-timed photo cards and sounds survive a caption-language request.

Café kit T2 (English speech, Turkish subtitles, a Cotopaxi photo as a corner card at
"Ecuador", a "ding" at "That's it"), cleanup declined. On the no-cleanup branch of
`_run_phone_subtitled_job` ONE whisper pass, forced to the caption language, fed both the
caption cues and every spoken-word consumer, so the beats were matched against a Turkish
translation and "Ecuador" / "That's it" were never found. The cleanup branch already grounds
on the SPOKEN words and uses the requested-language pass for caption text only.

Ways this can go wrong (written before the test; full list: the KRI-839 failure-mode list):

1. Beats / spoken-text asks / overlay grounding / the persisted `overlay_transcript` still
   read the forced-language words (Turkish): the card and the ding go missing.
2. The fix swaps the beat words but the captions stop being Turkish, or the variant is no
   longer stamped `caption_language == "tr"`.
3. The fix runs an extra whisper pass when no language was requested (or the request equals
   the spoken language): the no-request render must stay one call, `language=None`, byte
   identical.
4. The beats place by luck (position, caption text, the generic overlay matcher) instead of
   by the spoken word: the negative control says "Peru" and must NOT show the Cotopaxi card.
5. The generic KRI-176 matcher places the photo by meaning and hides a missed beat: it is
   stubbed to place nothing so only the beat can put the card there.
6. The ding silently lost to a feature gate: the gates are enabled the way the sibling
   beat tests do, and the ding is asserted by time on the compiled recipe.

Real audio: `tests/fixtures/kri839_cafe_t2/t2_01_words.json` holds whisper.cpp word timings of
the actual T2 clip (spoken pass) and the Turkish words the forced pass returns. Only the
provider transport, storage and frame sampling are stubbed; the phrase matcher, geometry
arbitration, lane compiler and recipe compiler are the real ones. No model calls.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.services.sfx_catalog import SfxEntry
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _beat_dict,
    _enable_beats,
    _fake_resolve_sfx,
    _grounding_mock,
    _make_fake_bind,
    _setup_subtitled,
)

_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri839_cafe_t2" / "t2_01_words.json").read_text(
        encoding="utf-8"
    )
)
SPOKEN = _FIXTURE["spoken"]
CAPTION_TR = _FIXTURE["caption_tr"]

CLIP_DURATION_S = 24.6
COTOPAXI_ID = "cotopaxi-asset"
# The word starts the beats must land on, read from the recorded spoken pass.
ECUADOR_S = next(w["start_s"] for w in SPOKEN["words"] if w["text"].startswith("Ecuador"))
THATS_IT_S = next(w["start_s"] for w in SPOKEN["words"] if w["text"].startswith("That's"))
CARD_TOLERANCE_S = 0.5
DING_TOLERANCE_S = 0.3
# The creator's trigger phrases, as `trigger_vocabulary_prompt` spells them for whisper.
TRIGGER_PROMPT = "Ecuador, That's it."


def _transcript(spec: dict, *, rename: dict[str, str] | None = None) -> Transcript:
    rename = rename or {}
    words = [
        Word(
            text=rename.get(w["text"], w["text"]),
            start_s=w["start_s"],
            end_s=w["end_s"],
            confidence=1.0,
        )
        for w in spec["words"]
    ]
    return Transcript(
        words=words, language=spec["language"], full_text=" ".join(w.text for w in words)
    )


def _run_t2(
    monkeypatch,
    *,
    requested: str | None,
    spoken_rename: dict[str, str] | None = None,
    spoken_text: tuple[str, str] | None = None,
) -> SimpleNamespace:
    """Run the café T2 Talking job through the real worker entry, cleanup declined.

    Whisper is stubbed by its `language` hint: None / "en" -> the recorded spoken pass,
    "tr" -> the Turkish forced pass. Every call is recorded. `spoken_text=(text, trigger)`
    pins a brief asking for `text` on screen when the creator says `trigger` (KRI-707).
    """
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch, duration_s=CLIP_DURATION_S)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    # Trace events would try a database connection (seconds each): record them in memory.
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", Mock())

    # The real beat grounder runs; only its DB seams and the face sampler are stubbed.
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "_load_ready_pool_assets",
        lambda *a, **k: [
            {
                "id": COTOPAXI_ID,
                "gcs_path": f"users/u1/plan/item1/pool/{COTOPAXI_ID}.jpg",
                "gcs_generation": "7",
                "kind": "image",
                "source_filename": "T2_02_visual.jpg",
                "duration_s": None,
                "aspect": 1.5,
                "user_context": "",
                "analysis": {},
            }
        ],
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "_load_sfx_entries",
        lambda *a, **k: [SfxEntry(id="sfx-ding", name="Ding", search_terms=("ding",))],
    )
    monkeypatch.setattr(
        phone_overlay_grounding_mod,
        "sample_face_regions",
        lambda path, anchors, **kw: ([], {"attempted": len(anchors), "decoded": len(anchors)}),
    )
    # The generic KRI-176 matcher places nothing: only a beat can put the card there.
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )

    beat_words: list[list[str]] = []
    real_grounder = phone_reaction_grounding_mod.ground_phone_reaction_beats

    def spy(*args, **kwargs):
        beat_words.append([str(w["text"]) for w in kwargs["words"]])
        return real_grounder(*args, **kwargs)

    monkeypatch.setattr(phone_reaction_grounding_mod, "ground_phone_reaction_beats", spy)

    calls: list[dict] = []

    def whisper(_path, *, language=None, verbatim_prompt=None, **_kw):
        calls.append({"language": language, "verbatim_prompt": verbatim_prompt})
        if language in (None, "en"):
            return _transcript(SPOKEN, rename=spoken_rename)
        if language == "tr":
            return _transcript(CAPTION_TR)
        raise AssertionError(f"unexpected whisper language hint {language!r}")

    monkeypatch.setattr(
        "app.pipeline.transcribe.transcribe_whisper_cached", Mock(side_effect=whisper)
    )

    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [
            _beat_dict(
                "cotopaxi", "Ecuador", visual_id=f"asset-{COTOPAXI_ID}", visual_role="photo"
            ),
            _beat_dict("ding", "That's it", sound="ding"),
        ]
    }
    if requested is not None:
        job.all_candidates["caption_language_request"] = requested
    if spoken_text is not None:
        text, trigger = spoken_text
        brief = CreativeBrief(
            version=1,
            requirements=[
                BriefRequirement(
                    id="r1",
                    kind="text",
                    scope="global",
                    description=f"Show {text} in big letters when I say {trigger}",
                    literal=text,
                    facts={"triggers": [trigger]},
                )
            ],
        )
        job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
            uuid.uuid4(), brief
        ).model_dump(mode="json")
        # The text row is kept off the speaker's face: no face found in the frame samples.
        monkeypatch.setattr(
            title_mod,
            "sample_face_regions",
            lambda path, anchors, **kw: ([], {"attempted": len(anchors), "decoded": len(anchors)}),
        )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    recipe = device_status(job, "subtitled").request.recipe
    tracks = {track.id: track for track in recipe.tracks}
    receipt = variant["phone_beat_receipt"]
    return SimpleNamespace(
        job=job,
        variant=variant,
        receipt=receipt,
        calls=calls,
        beat_words=beat_words,
        text_layers=list(recipe.text_layers),
        cards=list(tracks["subtitled-overlays"].clips) if "subtitled-overlays" in tracks else [],
        dings=list(tracks["sfx"].clips) if "sfx" in tracks else [],
        why=(
            f"whisper hints={[c['language'] for c in calls]}; "
            f"beats grounded on {beat_words[0][:12] if beat_words else None}...; "
            f"beat receipt placed={receipt['placed']} unplaced={receipt['unplaced']}"
        ),
    )


# --- case 1: Turkish captions requested, cleanup declined (RED on current code) ----------


def test_cotopaxi_card_lands_on_ecuador_when_turkish_captions_are_requested(monkeypatch):
    run = _run_t2(monkeypatch, requested="tr")

    assert len(run.cards) == 1, f"no Cotopaxi card on the compiled recipe. {run.why}"
    assert run.cards[0].timeline_start == pytest.approx(ECUADOR_S, abs=CARD_TOLERANCE_S), run.why


def test_ding_lands_on_thats_it_when_turkish_captions_are_requested(monkeypatch):
    run = _run_t2(monkeypatch, requested="tr")

    assert len(run.dings) == 1, f"no ding on the compiled recipe. {run.why}"
    assert run.dings[0].timeline_start == pytest.approx(THATS_IT_S, abs=DING_TOLERANCE_S), run.why


def test_both_beats_are_reported_placed_on_the_receipt(monkeypatch):
    run = _run_t2(monkeypatch, requested="tr")

    assert sorted(entry["beat_id"] for entry in run.receipt["placed"]) == ["cotopaxi", "ding"], (
        run.why
    )
    assert run.receipt["unplaced"] == [], run.why


def test_captions_still_come_from_the_turkish_pass(monkeypatch):
    run = _run_t2(monkeypatch, requested="tr")

    # The requested-language pass is still made, and it is the one the captions use. It keeps
    # the trigger prompt so its whisper cache key stays the one already cached for this clip.
    assert {"language": "tr", "verbatim_prompt": TRIGGER_PROMPT} in run.calls, run.why
    assert run.variant["caption_language"] == "tr"
    caption_text = " ".join(cue["text"] for cue in run.variant["caption_cues"])
    assert "Ekvador'danım" in caption_text
    assert "Ecuador" not in caption_text


def test_persisted_overlay_transcript_is_the_spoken_words_not_the_turkish_ones(monkeypatch):
    run = _run_t2(monkeypatch, requested="tr")

    persisted = run.variant["overlay_transcript"]
    assert [w["text"] for w in persisted] == [w["text"] for w in SPOKEN["words"]], run.why
    assert [w["start_s"] for w in persisted] == [w["start_s"] for w in SPOKEN["words"]]


def test_spoken_text_lands_on_ecuador_when_turkish_captions_are_requested(monkeypatch):
    """KRI-707 "show ECUADOR in big letters when I say Ecuador" waits for the SPOKEN word."""
    run = _run_t2(monkeypatch, requested="tr", spoken_text=("ECUADOR", "Ecuador"))

    [placement] = run.variant["spoken_text_placement"]["rows"]
    assert (placement["requirement_id"], placement["status"]) == ("r1", "placed"), run.why
    [row] = [r for r in run.variant["text_elements"] if r["text"] == "ECUADOR"]
    assert row["start_s"] == pytest.approx(ECUADOR_S, abs=CARD_TOLERANCE_S), run.why
    # The phone draws it at the word.
    [layer] = [
        layer
        for layer in run.text_layers
        if layer.id.startswith("text-") and "".join(r.text for r in layer.runs) == "ECUADOR"
    ]
    assert layer.start == pytest.approx(ECUADOR_S, abs=CARD_TOLERANCE_S), run.why


# --- case 2: negative control (green now and after the fix) --------------------------------


@pytest.mark.parametrize("requested", ["tr", None])
def test_negative_control_cotopaxi_stays_unplaced_when_ecuador_is_never_said(
    monkeypatch, requested
):
    """The spoken take says "Peru" instead of "Ecuador": the matcher must not place the
    Cotopaxi card blindly (by position, by caption text, or by the generic matcher).
    Without a request the spoken words are matched directly, so this is not vacuous now."""
    run = _run_t2(monkeypatch, requested=requested, spoken_rename={"Ecuador": "Peru"})

    assert run.cards == [], run.why
    assert ("cotopaxi", "never_heard") in {
        (entry["beat_id"], entry["reason"]) for entry in run.receipt["unplaced"]
    }, run.why
    assert "cotopaxi" not in {entry["beat_id"] for entry in run.receipt["placed"]}, run.why


# --- case 3: no caption language requested (green now and after the fix) ------------------


def test_without_a_caption_request_there_is_one_spoken_pass_and_the_beats_land(monkeypatch):
    run = _run_t2(monkeypatch, requested=None)

    assert run.calls == [{"language": None, "verbatim_prompt": TRIGGER_PROMPT}]
    assert run.beat_words == [[w["text"] for w in SPOKEN["words"]]]
    assert run.cards[0].timeline_start == pytest.approx(ECUADOR_S, abs=CARD_TOLERANCE_S)
    assert run.dings[0].timeline_start == pytest.approx(THATS_IT_S, abs=DING_TOLERANCE_S)
    assert run.variant["caption_language"] == "en"
    assert [w["text"] for w in run.variant["overlay_transcript"]] == [
        w["text"] for w in SPOKEN["words"]
    ]


def test_a_request_for_the_spoken_language_costs_no_second_pass(monkeypatch):
    """English captions on English speech: the spoken words ARE the caption words."""
    run = _run_t2(monkeypatch, requested="en")

    assert len(run.calls) == 1, run.why
    assert run.variant["caption_language"] == "en"
    assert run.cards[0].timeline_start == pytest.approx(ECUADOR_S, abs=CARD_TOLERANCE_S)
    assert run.dings[0].timeline_start == pytest.approx(THATS_IT_S, abs=DING_TOLERANCE_S)
