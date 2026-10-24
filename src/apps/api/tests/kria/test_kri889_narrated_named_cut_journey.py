"""KRI-889: "cut the line 'with bread, no'" removes it from a narrated (voiceover) edit.

Before KRI-889 a named line was cut only from a phone Talking take (KRI-821). A narrated
edit plays a separate recorded voiceover, so the line stayed in the export and the receipt
said "I can't cut out one exact line yet". Now the voiceover file itself is cut, server-side,
and the shorter file is what the phone recipe and the cloud render play.

The voiceover here is synthesized: every "word" is a 0.4 s tone at its own frequency, so
word timings are exact by construction (the transcriber is a deterministic stub, never
Whisper) and the output is checked by frequency: a cut word's tone must be absent from the
played file, every kept word's tone present where its caption says it is. Real FFmpeg and
the real local object store run end to end; nothing is mocked between the brief and the
bytes the device downloads.

Failure modes (written before the fix):

F1  The named line is still audible in the phone export (recipe plays raw or plain-cleanup file).
F2  Cleanup-off phone job: the device grant hands over the RAW voiceover.
F3  Captions show the cut words, or are shifted from where the words now play.
F4  Video steps do not tile the shorter voice (compile fails or the last slot runs past it).
F6  A retake said twice is cut at a guessed take; must cut only the named occurrence.
F7  A line the voiceover never says is reported as cut.
F8  Cleanup-on job: the grant/worker 409s ("Voiceover changed") because the verify sites
    still hash the plain snapshot plan instead of the plan that was really cut.
F9  The plain cleanup audio filter changes bytes (guided stories, every cleanup render).
F10 Editor Save (a new pinned revision) drops the narration binding, so the device falls
    back to the raw voiceover.
F12 Explicit cleanup-off: the named cut also applies pause/filler cuts.
F13 Cleanup counts swallow the named cut, or the named cut reads as a cleanup retake.
F16 Captions/re-transcription read the RAW file instead of the cut one.
F18 Kill switch off is not byte-identical, or the draft promises a cut the worker won't make.
F19 The derivative is written outside the item's speech-cleanup prefix (not deleted with it).
F20 A cleanup-off job's named cut makes the variant claim speech cleanup "applied".
F21 Cloud Background Sound re-mix plays the RAW voiceover under cut-time timings, so the
    cut line comes back and everything after it drifts while the receipt still says "cut".
F22 With the cut voice missing, the re-mix silently plays the raw recording anyway.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.render_assets import VoiceoverRenderAsset
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.speech_cleanup_apply import (
    _cut_audio_filter,
    effective_cut_fingerprint,
    hydrate_speech_cleanup_snapshot,
)
from app.pipeline.transcribe import Transcript, Word
from app.services.device_narration_binding import authorized_device_narration
from app.services.device_render import device_record, device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_phone_subtitled_narrated_dispatch import _job_and_session

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RATE = 48000
WORD_S = 0.4
GAP_S = 0.2
LEAD_IN_S = 0.3
# A long pause after "Merope" for speech cleanup to cut (cleanup-on journey).
PAUSE_AFTER = "Merope"
PAUSE_S = 1.6
TEXT = (
    "So this is Merope and here is the waffle cookie with bread, no, with blackberry jam. "
    "Thank you Karo. It is really good. Thank you Karo. See you soon."
)
VOICE = "voiceover-uploads/direct/u/i/voice.wav"
THREAD = "6f0c9a4e-4d0c-4a4f-9f63-0f9a7e1c8b89"
ITEM_ID = uuid.UUID("0b7f3f8e-63a4-4d47-9a0e-6c1d2b8e4f10")


def _script() -> tuple[list[dict], float]:
    words: list[dict] = []
    t = LEAD_IN_S
    for index, text in enumerate(TEXT.split()):
        words.append(
            {"text": text, "start_s": round(t, 3), "end_s": round(t + WORD_S, 3), "i": index}
        )
        t += WORD_S + GAP_S + (PAUSE_S if text == PAUSE_AFTER else 0.0)
    return words, round(t + 0.3, 3)


WORDS, DURATION = _script()


def _raw_words() -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in WORDS
    ]


def _freq(index: int) -> float:
    return 300.0 + 80.0 * index


def _write_voiceover(path: Path) -> None:
    samples = np.zeros(round(DURATION * RATE))
    ramp = round(0.005 * RATE)
    for word in WORDS:
        first, end = round(word["start_s"] * RATE), round(word["end_s"] * RATE)
        n = np.arange(end - first)
        tone = 0.3 * np.sin(2 * np.pi * _freq(word["i"]) * n / RATE)
        envelope = np.ones(end - first)
        envelope[:ramp] = np.linspace(0, 1, ramp)
        envelope[-ramp:] = np.linspace(1, 0, ramp)
        samples[first:end] = tone * envelope
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((samples * 32767).astype("<i2").tobytes())


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as wav:
        assert (wav.getframerate(), wav.getnchannels()) == (RATE, 1)
        data = np.frombuffer(wav.readframes(wav.getnframes()), "<i2")
    return data.astype(np.float64) / 32768


def _amplitude(samples: np.ndarray, start_s: float, end_s: float, freq: float) -> float:
    first, end = max(0, round(start_s * RATE)), min(len(samples), round(end_s * RATE))
    window = samples[first:end]
    if len(window) < 64:
        return 0.0
    hann = np.hanning(len(window))
    n = np.arange(len(window))
    return float(2 * abs(np.dot(window * hann, np.exp(-2j * np.pi * freq * n / RATE))) / hann.sum())


def _peak(samples: np.ndarray, freq: float) -> float:
    step = 0.05
    return max(
        _amplitude(samples, t, t + 0.1, freq) for t in np.arange(0, len(samples) / RATE, step)
    )


def _removed_before(t: float, removed: list[tuple[float, float]]) -> float:
    return sum(min(e, t) - s for s, e in removed if s < t)


def _assert_audio(
    samples: np.ndarray, removed: list[tuple[float, float]], cut_indexes: set[int]
) -> list[dict]:
    """Cut tones absent anywhere; every kept tone present where the shift puts it."""
    kept: list[dict] = []
    for word in WORDS:
        if word["i"] in cut_indexes:
            assert _peak(samples, _freq(word["i"])) < 0.03, word  # F1
            continue
        start = word["start_s"] - _removed_before(word["start_s"], removed)
        end = word["end_s"] - _removed_before(word["end_s"], removed)
        amp = _amplitude(samples, start + 0.08, end - 0.08, _freq(word["i"]))
        assert amp > 0.2, (word, start, amp)
        kept.append({"text": word["text"], "start_s": start, "end_s": end})
    return kept


def _write_artifact(raw: Path, cut: Path, removed: list, receipts: list) -> None:
    """With ``KRI889_ARTIFACT_DIR`` set, keep the evidence a human can inspect.

    ``report.json`` (per-word expected/observed tone amplitude before and after, removed
    spans, receipts) plus ``spectrogram_before.png`` / ``spectrogram_after.png`` and both
    WAVs. Run: ``KRI889_ARTIFACT_DIR=.dev/kri889 pytest
    tests/kria/test_kri889_narrated_named_cut_journey.py -k cleanup_off``.
    """
    target = os.environ.get("KRI889_ARTIFACT_DIR")
    if not target:
        return
    out = Path(target)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw, out / "voiceover_before.wav")
    shutil.copy2(cut, out / "voiceover_after.wav")
    before, after = _read(raw), _read(cut)
    rows = []
    for word in WORDS:
        freq = _freq(word["i"])
        rows.append(
            {
                "word": word["text"],
                "index": word["i"],
                "freq_hz": freq,
                "raw_window_s": [word["start_s"], word["end_s"]],
                "peak_before": round(_peak(before, freq), 4),
                "peak_after": round(_peak(after, freq), 4),
            }
        )
    report = {
        "raw_duration_s": round(len(before) / RATE, 4),
        "cut_duration_s": round(len(after) / RATE, 4),
        "removed": removed,
        "named_cuts": receipts,
        "words": rows,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    for name, src in (("before", raw), ("after", cut)):
        subprocess.run(  # noqa: S603 - fixed argv
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-lavfi",
                "showspectrumpic=s=1200x400:legend=1",
                str(out / f"spectrogram_{name}.png"),
            ],
            check=True,
        )


def _req(rid: str, description: str, lines: list[str], **facts) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope="global",
        description=description,
        facts={"speech_cleanup": True, "cut_lines": lines, **facts},
    )


SLIP = _req("r1", "Cut my slip 'with bread, no,'", ["with bread, no,"])
RETAKE = _req(
    "r2",
    "Cut my second 'thank you Karo'",
    ["thank you Karo"],
    cut_line_occurrences={"thank you Karo": "second"},
)
NEVER_SAID = _req("r3", "Cut 'the pistachio croissant'", ["the pistachio croissant"])
SLIP_INDEXES = {10, 11, 12}  # with bread, no,
SECOND_KARO = {23, 24, 25}


def _binding_for(*reqs: BriefRequirement) -> dict:
    brief = CreativeBrief(version=1, requirements=list(reqs))
    return BriefBinding.create(THREAD, brief).model_dump(mode="json")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The real local object store (`STORAGE_PROVIDER=local`), voiceover uploaded."""
    root = tmp_path / "store"
    monkeypatch.setattr(gb.settings, "storage_provider", "local")
    monkeypatch.setattr(gb.settings, "e2e_fixtures", True)
    monkeypatch.setattr(gb.settings, "local_storage_root", str(root))
    monkeypatch.setattr(gb.settings, "local_storage_base_url", "http://local.test")
    _write_voiceover(root / VOICE)
    return root


def _phone_job(monkeypatch, store: Path, *, binding: dict | None, cleanup: dict | None = None):
    from app import storage

    clip = _binding("c0", duration_s=60.0)
    snapshot = {
        PHONE_SOURCES_FIELD: [clip.model_dump(mode="json")],
        "creator_generation_id": "generation",
        **({"creator_brief_binding": binding} if binding else {}),
        **(cleanup or {}),
    }
    candidates = {
        "clip_paths": ["phone-proxies/c0.mp4"],
        "edit_format": "narrated_ready",
        "voiceover_gcs_path": VOICE,
        "language": "en",
    }
    job, session = _job_and_session(monkeypatch, assembly_plan=snapshot, all_candidates=candidates)
    item = SimpleNamespace(
        id=ITEM_ID,
        audio_mode="voiceover",
        voiceover_gcs_path=VOICE,
        voiceover_generation=storage.object_metadata(VOICE).generation,
        voiceover_duration_s=DURATION,
    )
    job.content_plan_item_id = item.id
    session.get.side_effect = lambda model, key, **_k: job if model is gb.Job else item
    monkeypatch.setattr(gb.settings, "narrated_clip_alignment_enabled", False)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c0", 60.0)],
            "clip_id_to_gcs": {"c0": clip.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": _Meta("c0", 60.0),
        },
        raising=False,
    )
    heard: list[str] = []

    def whisper(path, **_k):
        heard.append(path)
        return Transcript(
            words=_raw_words(),
            full_text=TEXT,
            language="en",
        )

    import app.pipeline.transcribe as transcribe_mod

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", whisper)
    return job, snapshot, item, heard


def _pinned(job, item, store: Path):
    record = device_record(job, "narrated")
    recipe = device_status(job, "narrated").request.recipe
    asset = next(a for a in recipe.asset_manifest.assets if isinstance(a, VoiceoverRenderAsset))
    narration = authorized_device_narration(
        record, assembly=job.assembly_plan, owner_id=job.user_id, item=item, asset=asset
    )
    return record, recipe, asset, narration


def _cue_words(variant: dict) -> list[dict]:
    return [w for cue in variant["caption_cues"] for w in cue.get("words") or []]


def test_phone_cleanup_off_plays_a_voiceover_without_the_named_line(monkeypatch, store):
    job, snapshot, item, heard = _phone_job(
        monkeypatch, store, binding=_binding_for(SLIP, RETAKE, NEVER_SAID)
    )

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    by_quote = {row["quote"]: row for row in variant["named_cuts"]}
    assert by_quote["with bread, no,"]["status"] == "applied"
    assert by_quote["thank you Karo"]["status"] == "applied"  # F6: the second one only
    assert by_quote["thank you Karo"]["occurrences_found"] == 2
    assert by_quote["the pistachio croissant"]["reason"] == "not_found"  # F7
    # F12/F20: explicit cleanup-off -- no cleanup record, no cleanup claim.
    assert variant.get("silence_cut") is None and variant.get("silence_cut_outcome") is None
    # Whisper heard the RAW recording once; the cut WAV is never re-transcribed (F16).
    assert len(heard) == 1 and "speech-cleanup" not in heard[0]

    # F2: the device grant hands over the cut derivative, never the raw voiceover.
    record, recipe, asset, narration = _pinned(job, item, store)
    assert narration is not None and narration.named_cuts is not None
    prefix = f"users/{job.user_id}/plan/{item.id}/speech-cleanup/named-cuts/"
    assert narration.gcs_path.startswith(prefix)  # F19
    assert narration.speech_cleanup is None
    removed = [tuple(span) for span in narration.named_cuts.removed]
    assert len(removed) == 2

    # F1: the bytes the device downloads lack both cut lines; everything else plays.
    played = _read(store / narration.gcs_path)
    expected_s = DURATION - sum(e - s for s, e in removed)
    assert len(played) / RATE == pytest.approx(expected_s, abs=0.002)
    kept = _assert_audio(played, removed, SLIP_INDEXES | SECOND_KARO)
    _write_artifact(store / VOICE, store / narration.gcs_path, removed, variant["named_cuts"])

    # F4: the recipe and its steps tile exactly the shorter voice.
    assert recipe.duration == pytest.approx(narration.duration_s, abs=1e-6)
    assert narration.duration_s == pytest.approx(expected_s, abs=0.002)
    assert [(t["start_s"], t["end_s"]) for t in variant["narrated_timings"]][-1][1] == (
        pytest.approx(narration.duration_s, abs=1e-6)
    )

    # F3: no cut word in the captions; every caption word sits where its tone now plays.
    caption_text = " ".join(cue["text"] for cue in variant["caption_cues"])
    assert "bread" not in caption_text
    assert caption_text.count("Karo") == 1
    words = _cue_words(variant)
    assert [w["text"] for w in words] == [w["text"] for w in kept]
    for got, want in zip(words, kept, strict=True):
        assert got["start_s"] == pytest.approx(want["start_s"], abs=0.002)
        assert got["end_s"] == pytest.approx(want["end_s"], abs=0.002)

    # F10: an editor Save pins a new revision with no binding of its own; the cut survives.
    from app.kria.device_render import make_device_request

    saved = make_device_request(job_id=job.id, variant_id="narrated", revision=2, recipe=recipe)
    assert pin_device_request(job, saved, base_generation="generation") is True
    after = device_record(job, "narrated")
    assert after["narration_binding"] == record["narration_binding"]
    _record, _recipe, _asset, reopened = _pinned(job, item, store)
    assert reopened is not None and reopened.gcs_path == narration.gcs_path

    # The finished video's receipt answers each requirement from the render's own record.
    facts = plan_facts_from_phone_variant(variant)
    out = {
        r.requirement_id: r
        for r in build_receipts([SLIP, RETAKE, NEVER_SAID], facts, include_unchecked=True)
    }
    assert out["r1"].status == "met", out["r1"]
    assert 'Cut "with bread, no,"' in (out["r1"].reason or "")
    assert out["r2"].status == "met", out["r2"]
    assert out["r3"].status == "partial"
    assert "Still in your video" in (out["r3"].reason or "")


def _cleanup_snapshot(item_generation: str) -> dict:
    """The accepted "Clean up speech" preflight snapshot of this voiceover (one long pause)."""
    pause = next(w for w in WORDS if w["text"] == PAUSE_AFTER)
    from app.services.clip_speech import SilenceDetectionResult

    fingerprint = "c" * 64
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
            words=_raw_words(),
            language="en",
            low_confidence=False,
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(
            spans=[(pause["end_s"] + 0.05, pause["end_s"] + GAP_S + PAUSE_S - 0.05)],
            status="ok",
        ),
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "voiceover",
            "media_identity": "voiceover-1",
            "storage_path": VOICE,
            "generation": item_generation,
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def test_phone_cleanup_on_cuts_the_line_and_the_pauses_in_one_verified_file(monkeypatch, store):
    from app import storage

    generation = storage.object_metadata(VOICE).generation
    payload = _cleanup_snapshot(generation)
    snapshot_view = hydrate_speech_cleanup_snapshot(payload)
    assert snapshot_view.cut_plan.removed, "fixture: cleanup must cut the long pause"
    job, snapshot, item, heard = _phone_job(
        monkeypatch,
        store,
        binding=_binding_for(SLIP),
        cleanup={
            "speech_cleanup_contract": "required_v1",
            "_speech_cleanup_internal": {"preflight_snapshot": payload},
        },
    )
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    assert [row["status"] for row in variant["named_cuts"]] == ["applied"]
    assert heard == []  # the snapshot's words, never a second ASR pass
    # F13: cleanup's record is cleanup's own; the named cut is not a cleanup removal.
    assert variant["silence_cut_outcome"] == "applied"
    assert {r["reason"] for r in variant["silence_cut"]["removed"]} == {
        r.reason for r in snapshot_view.cut_plan.removed
    }
    assert "named_cut" not in json.dumps(variant["silence_cut"])

    # F8: the device grant accepts the combined derivative (fingerprint of what was cut).
    _record, recipe, _asset, narration = _pinned(job, item, store)
    assert narration is not None
    assert narration.speech_cleanup is not None and narration.named_cuts is not None
    assert narration.gcs_path.startswith(
        f"users/{job.user_id}/plan/{item.id}/speech-cleanup/{snapshot_view.analysis_id}/"
    )
    removed_named = [tuple(span) for span in narration.named_cuts.removed]
    assert narration.speech_cleanup.cut_sha256 == effective_cut_fingerprint(
        snapshot_view, removed_named
    )
    played = _read(store / narration.gcs_path)
    all_removed = sorted(
        [(r.start_s, r.end_s) for r in snapshot_view.cut_plan.removed] + removed_named
    )
    _assert_audio(played, all_removed, SLIP_INDEXES)
    assert recipe.duration == pytest.approx(len(played) / RATE, abs=0.002)
    assert "bread" not in " ".join(cue["text"] for cue in variant["caption_cues"])


def test_phone_named_cuts_kill_switch_is_byte_identical(monkeypatch, store):
    """F18: flag off with a named line = exactly the run with no brief at all."""

    def run(binding):
        job, snapshot, _item, _heard = _phone_job(monkeypatch, store, binding=binding)
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
        variant = dict(job.assembly_plan["variants"][0])
        record = device_record(job, "narrated")
        return variant, device_status(job, "narrated").request.recipe, record

    monkeypatch.setattr(gb.settings, "narrated_named_cuts_enabled", False)
    off_variant, off_recipe, off_record = run(_binding_for(SLIP))
    monkeypatch.setattr(gb.settings, "narrated_named_cuts_enabled", True)
    plain_variant, plain_recipe, plain_record = run(None)
    assert "named_cuts" not in off_variant
    assert off_recipe == plain_recipe
    assert off_record.get("narration_binding") is None
    assert plain_record.get("narration_binding") is None
    for key in ("render_finished_at",):
        off_variant.pop(key, None), plain_variant.pop(key, None)
    assert off_variant == plain_variant


def _render_cloud(monkeypatch, tmp_path, store, **kwargs) -> dict:
    seen: dict = {}

    def fake_assemble(step_timings, _assignments, voiceover, output_path, _tmpdir, **kw):
        seen["timings"] = step_timings
        seen["voiceover"] = _read(Path(voiceover)) if voiceover.endswith(".wav") else None
        seen["voiceover_path"] = voiceover
        seen["caption_words"] = [
            {"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in kw["transcript"].words
        ]
        for path in (output_path, kw["base_output_path"]):
            Path(path).write_bytes(b"video")
        return []

    import app.pipeline.transcribe as transcribe_mod

    heard: list[str] = []

    def whisper(path, **_k):
        heard.append(path)
        return Transcript(
            words=_raw_words(),
            full_text=TEXT,
            language="en",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", whisper)
    monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", False)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_a, **_k: "signed")
    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    variant_dir = tmp_path / "variant"
    variant_dir.mkdir()
    result = gb._render_narrated_variant(
        job_id="job",
        rank=1,
        spec={"variant_id": "narrated", "voiceover_gcs_path": VOICE},
        filming_guide=[],
        narrative_order=["clip_0"],
        clip_id_to_local={"clip_0": "/tmp/clip.mp4"},
        variant_dir=str(variant_dir),
        **kwargs,
    )
    return {"result": result, "heard": heard, **seen}


def test_cloud_narrated_render_plays_the_cut_voiceover_with_synced_captions(
    monkeypatch, tmp_path, store
):
    asks = [("with bread, no,", ("r1",), None), ("thank you Karo", ("r2",), 2)]
    rendered = _render_cloud(
        monkeypatch, tmp_path, store, speech_cleanup_contract="off_v1", named_cut_asks=asks
    )

    result = rendered["result"]
    assert result["ok"] is True, result
    assert [row["status"] for row in result["named_cuts"]] == ["applied", "applied"]
    # F20: an explicit cleanup-off render never claims cleanup.
    assert result["silence_cut"] is None and result["silence_cut_outcome"] is None
    removed = [(row["start_s"], row["end_s"]) for row in result["named_cuts"]]
    removed.sort()
    played = rendered["voiceover"]
    assert played is not None, rendered["voiceover_path"]
    assert len(played) / RATE == pytest.approx(DURATION - sum(e - s for s, e in removed), abs=0.002)
    kept = _assert_audio(played, removed, SLIP_INDEXES | SECOND_KARO)
    # F3: the burned captions are the kept words, shifted exactly like the audio.
    assert [w["text"] for w in rendered["caption_words"]] == [w["text"] for w in kept]
    for got, want in zip(rendered["caption_words"], kept, strict=True):
        assert got["start_s"] == pytest.approx(want["start_s"], abs=0.002)
    # F4: the visual timeline ends where the shorter voice ends.
    assert rendered["timings"][-1].end_s == pytest.approx(len(played) / RATE, abs=0.01)
    assert len(rendered["heard"]) == 1  # F16: one ASR pass, on the raw recording


def _bed_remix(monkeypatch, variant: dict) -> dict:
    """Run the real Background Sound re-mix task on ``variant``; return what it played."""
    from tests.tasks.conftest import FakeJob, patch_job_session

    job = FakeJob(
        assembly_plan={"variants": [variant]},
        all_candidates={
            "voiceover_gcs_path": VOICE,
            "filming_guide": [],
            "clip_paths": ["slot-uploads/a.mp4"],
            "narrative_shot_count": 0,
        },
    )
    patch_job_session(monkeypatch, job)
    seen: dict = {}

    def fake_assemble(step_timings, _assignments, voiceover, output_path, _tmpdir, **kw):
        seen["voiceover"] = _read(Path(voiceover))
        seen["timings"] = step_timings
        seen["bed_level"] = kw["bed_level"]
        for path in (output_path, kw["base_output_path"]):
            Path(path).write_bytes(b"video")
        return []

    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    monkeypatch.setattr(
        "app.tasks.template_orchestrate._download_clips_parallel",
        lambda paths, tmpdir: [f"{tmpdir}/local_{i}.mp4" for i in range(len(paths))],
    )
    monkeypatch.setattr(
        gb,
        "_burn_persisted_captions_onto_base",
        lambda _base, out_local, _variant, _tmpdir: Path(out_local).write_bytes(b"video"),
    )
    monkeypatch.setattr("app.storage.delete_object_best_effort", lambda *_a, **_k: True)
    gb._run_reburn_narrated_bed_level(str(uuid.uuid4()), "narrated", 0.6)
    return seen


def test_cloud_background_sound_remix_keeps_the_named_cut(monkeypatch, tmp_path, store):
    """F21: the slider re-mix plays the same cut voice the render played, in sync."""
    asks = [("with bread, no,", ("r1",), None)]
    rendered = _render_cloud(
        monkeypatch, tmp_path, store, speech_cleanup_contract="off_v1", named_cut_asks=asks
    )
    result = rendered["result"]
    assert [row["status"] for row in result["named_cuts"]] == ["applied"]
    voice_key = result["narrated_voiceover_path"]
    assert voice_key.startswith("generative-jobs/job/")  # deleted with the job's outputs

    variant = {**result, "variant_id": "narrated", "rank": 1, "resolved_archetype": "narrated"}
    remixed = _bed_remix(monkeypatch, variant)

    assert remixed["bed_level"] == 0.6
    played = remixed["voiceover"]
    assert np.array_equal(played, rendered["voiceover"])  # byte-for-byte the render's voice
    removed = [(row["start_s"], row["end_s"]) for row in result["named_cuts"]]
    _assert_audio(played, removed, SLIP_INDEXES)
    # The persisted cut-time steps still tile the voice the re-mix plays.
    assert remixed["timings"][-1].end_s == pytest.approx(len(played) / RATE, abs=0.01)


def test_cloud_background_sound_remix_refuses_without_the_cut_voice(monkeypatch, tmp_path, store):
    """F22: no kept cut voice means no re-mix, never the raw recording."""
    asks = [("with bread, no,", ("r1",), None)]
    result = _render_cloud(
        monkeypatch, tmp_path, store, speech_cleanup_contract="off_v1", named_cut_asks=asks
    )["result"]
    variant = {**result, "variant_id": "narrated", "rank": 1, "resolved_archetype": "narrated"}
    variant.pop("narrated_voiceover_path")
    with pytest.raises(ValueError, match="cut voiceover"):
        _bed_remix(monkeypatch, variant)


def test_cloud_without_asks_is_unchanged(monkeypatch, tmp_path, store):
    rendered = _render_cloud(monkeypatch, tmp_path, store, speech_cleanup_contract="off_v1")
    assert "named_cuts" not in rendered["result"]
    assert "narrated_voiceover_path" not in rendered["result"]
    assert rendered["voiceover_path"].endswith("voiceover_src")


def test_plain_cleanup_filter_is_byte_identical():
    """F9: the snapshot cut filter every cleanup render uses is unchanged by the split."""
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "speech_cleanup"
        / "guided_voiceover_mixed_gap_v2.json"
    )
    snapshot = hydrate_speech_cleanup_snapshot(json.loads(fixture.read_text())["snapshot"])
    digest = hashlib.sha256(_cut_audio_filter(snapshot).encode()).hexdigest()
    # Computed on origin/main 449918966 before KRI-889.
    assert digest == "dbf208f95f1b685e251f8c751e723a4c9180fd5b08dd2b7669f436d72ac53197"
    assert effective_cut_fingerprint(snapshot, []) == effective_cut_fingerprint(snapshot, ())


@pytest.mark.parametrize(
    ("strategy", "enabled", "promised"),
    [
        ({"edit_format": "narrated_ready", "audio_strategy": "voiceover"}, True, True),
        ({"edit_format": "narrated", "audio_strategy": "voiceover"}, False, False),
        ({"edit_format": "narrated_ready", "audio_strategy": "original"}, True, False),
        (
            {"edit_format": "narrated", "audio_strategy": "voiceover", "render_program": "guided"},
            True,
            False,
        ),
    ],
)
def test_draft_promises_the_cut_only_where_a_render_makes_it(
    monkeypatch, strategy, enabled, promised
):
    """F18: the approval card promises a voiceover line cut only when the worker will cut it."""
    monkeypatch.setattr(gb.settings, "narrated_named_cuts_enabled", enabled)
    facts = plan_facts_from_strategy(strategy, speech_cleanup_offered=True)
    [receipt] = build_receipts([SLIP], facts, include_unchecked=True)
    assert receipt.status == "partial"
    reason = receipt.reason or ""
    assert ("I'll cut the line you named" in reason) is promised, reason
    assert ("I can't cut out one exact line yet" in reason) is (not promised), reason
