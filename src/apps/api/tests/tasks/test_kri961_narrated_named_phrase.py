"""KRI-961: Don Julio N1, a phone Voiceover edit, end to end with the words the render heard.

The creator says "ojo de bife" in a self-recorded English voiceover and asked for two things
about it: "English captions on my voice, spell "ojo de bife" right" (brief r2, ``spellings``)
and "show the ribeye photo as a small corner card while I say "ojo de bife"" (r6 + the
strategy's reaction beat). Whisper heard "Olloda bife". Before approval r6 read Done (the
draft check reads the plan, which has the beat); the finished video burned "Olloda bife" and
said "I never heard ojo de bife in your voice", with no card.

Drives `_run_phone_narrated_job` with this run's whisper words (fixture), the brief pinned on
the job and the beat, through the real caption build, spelling, beat grounding and lane
compile; only storage, the pool/SFX catalog seams and asset binding are faked. Failure modes
are listed in tests/pipeline/test_kri961_named_phrase_near_match.py (FM10 is this module:
the captions, the card and both receipts agree on what was heard).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import BeatFact, PlanFacts, build_receipts
from app.services.device_render import device_status
from app.tasks import kria_runtime
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _enable_beats,
    _make_fake_bind,
    _pool_asset,
    _setup_narrated,
    _words,
)

RUN = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri961_don_julio_n1_narrated.json").read_text(
        "utf-8"
    )
)
WHISPER = [(w["text"], w["start_s"], w["end_s"]) for w in RUN["whisper_words"]]
REQUIREMENTS = [BriefRequirement.model_validate(r) for r in RUN["requirements"]]
BEAT = RUN["reaction_beat"]
PHOTO_ID = BEAT["visual_id"].removeprefix("asset-")
OLLODA = next(w for w in RUN["whisper_words"] if w["text"] == "Olloda")
BIFE = next(w for w in RUN["whisper_words"] if w["text"] == "bife")


def setup_run(monkeypatch, *, requirements=REQUIREMENTS, beats=(BEAT,), words=WHISPER):
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        edit_format="narrated_planned",
        spoken_words=_words(*words),
        voiceover_s=RUN["voiceover_duration_s"],
        clip_duration_s=30.0,  # every clip outlasts its step: no retime
    )
    _enable_beats(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "_load_ready_pool_assets",
        lambda *a, **k: [_pool_asset(PHOTO_ID, "ribeye.jpeg")],
    )
    monkeypatch.setattr(phone_reaction_grounding_mod, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    job.all_candidates["creator_strategy"] = {
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "reaction_beats": [dict(b) for b in beats],
    }
    binding = None
    if requirements:
        binding = BriefBinding.create(
            uuid.uuid4(), CreativeBrief(version=2, requirements=list(requirements))
        )
        snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
        job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    return job, snapshot, binding


def run(job, snapshot):
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    return device_status(job, "narrated").request.recipe, variant


def review(job, variant, binding):
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return text, {row["requirement_id"]: row for row in payload}


def _phrase_cue(variant):
    return next(c for c in variant["caption_cues"] if "butcher" in c["text"])


def test_captions_card_and_receipts_all_hear_ojo_de_bife(monkeypatch):
    job, snapshot, binding = setup_run(monkeypatch)

    recipe, variant = run(job, snapshot)

    # r2: the caption reads the creator's spelling; its words carry the heard span's time.
    cue = _phrase_cue(variant)
    assert cue["text"] == "Inside, the butcher cuts the ojo de bife right in front of you."
    assert " ".join(w["text"] for w in cue["words"]) == cue["text"]
    texts = [w["text"] for w in cue["words"]]
    ojo, de, bife = (cue["words"][texts.index(t)] for t in ("ojo", "de", "bife"))
    assert ojo["start_s"] == OLLODA["start_s"] and de["end_s"] == OLLODA["end_s"]
    assert (bife["start_s"], bife["end_s"]) == (BIFE["start_s"], BIFE["end_s"])
    assert not any("Olloda" in c["text"] for c in variant["caption_cues"])

    # r6: the corner card starts where the voice says the phrase.
    overlays = next(track for track in recipe.tracks if track.id == "subtitled-overlays")
    [card] = overlays.clips
    assert card.timeline_start == pytest.approx(OLLODA["start_s"])
    receipt = variant["phone_beat_receipt"]
    assert receipt["unplaced"] == []
    [placed] = receipt["placed"]
    assert placed["beat_id"] == BEAT["beat_id"] and placed["trigger"] == "ojo de bife"
    assert placed["at_s"] == pytest.approx(OLLODA["start_s"])
    assert placed["heard_as"] == "Olloda bife"  # evidence of the near hearing

    # The finished video says Done for both, judged on what it shows, never "I never heard".
    text, by_id = review(job, variant, binding)
    assert by_id["r2"]["status"] == "met", by_id["r2"]
    assert "ojo de bife is spelled as you wrote it" in by_id["r2"]["reason"]
    assert "captions are in English" in by_id["r2"]["reason"]
    assert by_id["r6"]["status"] == "met", by_id["r6"]
    assert '"ojo de bife" at 8.8 s' in by_id["r6"]["reason"]
    assert "never heard" not in text


def test_the_spelling_ask_reaches_the_render_from_the_recorded_facts():
    """Prod: "English captions on my voice" read as a speech-excerpt ask ("my voice"), so the
    brief's recorded ``spellings`` never reached any caption writer and r2 went unchecked."""
    from app.kria.brief_checks import asks_caption_spelling, brief_caption_spellings

    brief = CreativeBrief(version=2, requirements=list(REQUIREMENTS))
    assert brief_caption_spellings(brief) == ("ojo de bife",)
    assert [req.id for req in REQUIREMENTS if asks_caption_spelling(req)] == ["r2"]


def test_captions_that_keep_the_mishearing_are_never_done(monkeypatch):
    """The Voiceover record knows the captions' language, not their lines: a spelling ask is
    judged on the finished captions, so a caption still reading "Olloda bife" is no Done."""
    job, snapshot, binding = setup_run(monkeypatch)
    monkeypatch.setattr(gb, "_apply_creator_spellings", lambda cues, _targets: cues)

    _recipe, variant = run(job, snapshot)

    assert _phrase_cue(variant)["text"] == RUN["provenance"]["rendered_caption"]
    assert "r2" not in {
        row["requirement_id"]
        for row in job.assembly_plan["narrated_alignment"].get("requirement_receipts") or []
    }
    _text, by_id = review(job, variant, binding)
    assert by_id["r2"]["status"] == "partial"
    assert '"Olloda bife" for ojo de bife' in by_id["r2"]["reason"]


def test_the_draft_and_the_finished_video_agree_on_the_card(monkeypatch):
    """FM10: before approval r6 read Done off the plan (no transcript is read there); the
    render must hear what the draft assumed, so both receipts say the same."""
    [r6] = [r for r in REQUIREMENTS if r.id == "r6"]
    draft = PlanFacts(
        reaction_beats_available=True,
        reaction_beats=(
            BeatFact(trigger=BEAT["trigger"], visual_id=BEAT["visual_id"], display_mode="pip"),
        ),
    )
    [draft_receipt] = build_receipts([r6], draft, include_unchecked=True)
    job, snapshot, binding = setup_run(monkeypatch)
    _recipe, variant = run(job, snapshot)
    _text, by_id = review(job, variant, binding)

    assert draft_receipt.status == "met"
    assert by_id["r6"]["status"] == draft_receipt.status


def test_without_a_spelling_ask_the_captions_stay_as_heard(monkeypatch):
    """FM14: the narrated captions change only for words the creator asked to spell."""
    job, snapshot, _binding = setup_run(monkeypatch, requirements=[], beats=())

    _recipe, variant = run(job, snapshot)

    assert _phrase_cue(variant)["text"] == RUN["provenance"]["rendered_caption"]


def test_a_trigger_the_voice_never_says_is_still_never_heard(monkeypatch):
    """FM3: nothing in this voiceover sounds like "the wine list"."""
    beat = {**BEAT, "beat_id": "wine", "trigger": "the wine list"}
    r6 = BriefRequirement(
        id="r6",
        kind="timing",
        scope="global",
        description="Show the ribeye photo as a small corner card while I say 'the wine list'",
        facts={"triggers": ["the wine list"], "display_mode": "pip"},
    )
    job, snapshot, binding = setup_run(monkeypatch, requirements=[r6], beats=(beat,))

    _recipe, variant = run(job, snapshot)

    receipt = variant["phone_beat_receipt"]
    assert receipt["placed"] == []
    assert receipt["unplaced"] == [
        {"beat_id": "wine", "trigger": "the wine list", "reason": "never_heard"}
    ]
    text, by_id = review(job, variant, binding)
    assert by_id["r6"]["status"] != "met"
    assert "never heard the wine list" in text
