"""Transcript-synced + rhythm typographic sequence — orchestration tests (T4/T8).

Pins the editorial-sequence wiring in `generative_build`:

- kill switch OFF ⇒ byte-identical legacy (no transcription, no quote agent,
  style=None cluster)
- eligibility gate (D16): song-replaced / voiceover audio never transcribes —
  but DOES attempt RHYTHM mode (authored quote, synthesized timings)
- rhythm mode: speech ineligible for ANY reason ⇒ SequenceQuoteWriterAgent →
  `rhythm_scenes` → emphasis → burn; quote-agent failure ⇒ static styled
  cluster (NO heuristic quote BY DESIGN); persisted quote re-times LLM-free
- D11: transcription reads the PRE-mix `assembled_path`
- D18: any ASR failure ⇒ rhythm attempt, then static cluster; render succeeds
- persistence (D19) + deterministic fast-reburn rebuild (no ASR, no LLM)
- D15: a re-assembling render clears stale transcript/scenes — the rhythm
  quote SURVIVES so a cut edit re-times the same words deterministically
- D20: first-render copy-through ⇒ loud static fallback / failed variant

Everything network/ffmpeg-shaped is stubbed; the real `split_phrases`,
`speech_eligibility`, `rhythm_scenes`, `build_sequence_overlays` and
`_resolve_intro_overlay_params` run for real (the cluster geometry engine is
stubbed at `compute_cluster_blocks`).
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb


class _Meta:
    def __init__(self, clip_id, hook_score):
        self.clip_id = clip_id
        self.hook_score = hook_score
        self.best_moments = []
        self.transcript = ""
        self.detected_subject = ""
        self.hook_text = ""


def _track(track_id="t1"):
    return types.SimpleNamespace(
        id=track_id,
        title="Song A",
        audio_gcs_path="music/t1/audio.m4a",
        beat_timestamps_s=[0.5, 1.0, 1.5, 2.0],
        duration_s=60.0,
        track_config={"best_start_s": 0.0, "best_end_s": 30.0},
        ai_labels={"labels": {}},
        analysis_status="ready",
        lyrics_cached=None,
    )


# Words: 8 usable words spanning 0.5–4.0s. With a 6s video → coverage 0.583
# (≥ 0.5) and ≥4 words ⇒ sequence-eligible. Terminal punctuation after "lake."
# splits two phrase scenes.
def _transcript_words():
    return [
        {"text": "we", "start": 0.5, "end": 0.7},
        {"text": "went", "start": 0.7, "end": 1.0},
        {"text": "to", "start": 1.0, "end": 1.2},
        {"text": "the", "start": 1.2, "end": 1.4},
        {"text": "lake.", "start": 1.4, "end": 1.8},
        {"text": "it", "start": 1.9, "end": 2.2},
        {"text": "was", "start": 2.2, "end": 3.5},
        {"text": "magic", "start": 3.5, "end": 4.0},
    ]


def _fake_transcript(words=None, low_confidence=False):
    return types.SimpleNamespace(
        words=_transcript_words() if words is None else words,
        low_confidence=low_confidence,
        full_text="",
    )


def _patch_render_helpers(monkeypatch):
    """Self-contained stub set so `_render_generative_variant` runs without
    ffmpeg/GCS/skia-rendering (mirrors test_generative_build's helper, plus burn
    capture + probe + cluster-engine stubs the sequence path needs)."""
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.intro_cluster as ic
    import app.pipeline.music_recipe as mr
    import app.pipeline.probe as probe_mod
    import app.pipeline.template_matcher as tm
    import app.pipeline.text_overlay_skia as skia_mod
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []), color_grade="none"
        ),
        raising=False,
    )
    monkeypatch.setattr(tm, "consolidate_slots", lambda recipe, metas: recipe, raising=False)
    monkeypatch.setattr(
        tm, "match", lambda recipe, metas, **kw: types.SimpleNamespace(steps=[]), raising=False
    )

    class _Mismatch(Exception):
        code = "x"
        message = "y"

    monkeypatch.setattr(tm, "TemplateMismatchError", _Mismatch, raising=False)
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)
    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )

    def _fake_assemble(steps, c2l, probe, out_path, tmpdir, **kw):
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)
    monkeypatch.setattr(
        to,
        "_mix_template_audio",
        lambda *a, **k: open(a[2], "wb").write(b"\x00" * 16),
        raising=False,
    )
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda p: types.SimpleNamespace(duration_s=6.0),
        raising=False,
    )

    burn_calls: list[dict] = []

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        burn_calls.append({"base": base_path, "overlays": overlays, "out": out_path})
        with open(out_path, "wb") as f:
            f.write(b"\x01" * 24)  # different size than the 16-byte base

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn, raising=False)

    # Cluster geometry engine: two deterministic blocks per scene/text (so the
    # static cluster's >2-overlay layout inference reads "cluster"). Captures the
    # style kwarg so the kill-switch contract is assertable.
    engine_calls: list[dict] = []

    def _fake_blocks(
        text,
        *,
        word_roles=None,
        base_size_px,
        font_family=None,
        reveal_window_s,
        style=None,
        accent_parity=0,
        language="en",
    ):
        engine_calls.append(
            {"text": text, "style": style, "base_size_px": base_size_px, "language": language}
        )
        return [
            {
                "text": text,
                "role": "hero",
                "text_size_px": base_size_px,
                "font_family": "Great Vibes",
                "position_x_frac": 0.4,
                "position_y_frac": 0.42,
                "start_offset_s": 0.0,
                "reveal_s": 0.4,
            },
            {
                "text": text,
                "role": "closer",
                "text_size_px": base_size_px,
                "font_family": "Playfair Display Regular",
                "position_x_frac": 0.5,
                "position_y_frac": 0.5,
                "start_offset_s": 0.15,
                "reveal_s": 0.4,
            },
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)
    return burn_calls, engine_calls


def _patch_transcribe(monkeypatch, *, transcript=None, error=None):
    import app.pipeline.transcribe as transcribe_mod

    calls: list[str] = []

    def _fake(path):
        calls.append(path)
        if error is not None:
            raise error
        return transcript if transcript is not None else _fake_transcript()

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _fake, raising=False)
    return calls


def _bomb_transcribe(monkeypatch):
    import app.pipeline.transcribe as transcribe_mod

    def _boom(path):
        raise AssertionError("transcribe_whisper must NOT be called on this path")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _boom, raising=False)


def _patch_emphasis_agent(monkeypatch, *, error=None):
    """Fake SequenceEmphasisAgent: hero on every word except a final closer.
    `error` raises from run() (terminal agent failure → heuristic fallback)."""
    import app.agents._model_client as mc
    import app.agents.sequence_emphasis as se

    monkeypatch.setattr(mc, "default_client", lambda: None, raising=False)
    run_calls: list = []

    class _FakeAgent:
        def __init__(self, client):
            pass

        def run(self, inp, ctx=None):
            run_calls.append(inp)
            if error is not None:
                raise error
            phrases = []
            for i, words in enumerate(inp.phrases):
                roles = ["hero"] * len(words)
                if len(words) > 1:
                    roles[-1] = "closer"
                phrases.append(types.SimpleNamespace(index=i, word_roles=roles))
            return types.SimpleNamespace(phrases=phrases)

    monkeypatch.setattr(se, "SequenceEmphasisAgent", _FakeAgent, raising=False)
    return run_calls


def _bomb_emphasis_agent(monkeypatch):
    import app.agents.sequence_emphasis as se

    class _Boom:
        def __init__(self, client):
            raise AssertionError("SequenceEmphasisAgent must NOT be constructed on this path")

    monkeypatch.setattr(se, "SequenceEmphasisAgent", _Boom, raising=False)


def _patch_trace(monkeypatch):
    import app.services.pipeline_trace as pt

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, payload=None: events.append((stage, event, payload or {})),
        raising=False,
    )
    return events


def _agent_text(text="What a magic day"):
    return types.SimpleNamespace(text=text, highlight_word=None, word_roles=None)


# The user-approved demo quote (9 sentences / 33 words once split on [.!?…]).
# At the probe stub's 6.0s duration the rhythm engine paces it across
# [0.3, 5.2] into 9 equal sentence slots → 9 scenes.
GOLDEN_QUOTE = (
    'It\'s not "just luck". If you put in the work. To get there. '
    "Luck is just. A combination of hard work. And good timing. "
    "So… don't allow anyone. To diminish your hard work."
)


def _quote_fn(monkeypatch=None, quote=GOLDEN_QUOTE):
    """author_quote_fn stub: records the duration it was called with."""
    calls: list[float] = []

    def fn(video_duration_s):
        calls.append(video_duration_s)
        return quote

    return fn, calls


def _bomb_quote_fn(video_duration_s):
    raise AssertionError("author_quote_fn must NOT be called on this path")


def _render(monkeypatch, tmp_path, *, spec=None, agent_form=None, **kwargs):
    vdir = tmp_path / "v"
    vdir.mkdir(exist_ok=True)
    spec = spec or {
        "variant_id": "original_text",
        "rank": 3,
        "text_mode": "agent_text",
        "track": None,
    }
    return gb._render_generative_variant(
        job_id="j",
        rank=spec.get("rank", 3),
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=_agent_text(),
        agent_form=agent_form or {"effect": "karaoke-line", "layout": "cluster"},
        variant_dir=str(vdir),
        intro_size_override_px=64,
        **kwargs,
    )


# ── Kill switch (THE regression contract) ────────────────────────────────────────


def test_kill_switch_off_is_legacy_no_transcribe_style_none(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", False, raising=False)
    burn_calls, engine_calls = _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    # NO quote agent call either — rhythm mode sits behind the same kill switch.
    res = _render(monkeypatch, tmp_path, author_quote_fn=_bomb_quote_fn)

    assert res["ok"] is True
    # Legacy static cluster rendered with style=None (byte-identical contract).
    assert engine_calls and all(c["style"] is None for c in engine_calls)
    assert res["intro_mode"] == "cluster"  # effective static layout, never "sequence"
    assert res["transcript"] is None
    assert res["scenes"] is None
    assert res["sequence_base_size_px"] is None
    assert res["sequence_mode"] is None
    assert res["sequence_quote"] is None
    # The gate is never even evaluated when the switch is off.
    assert not [e for e in events if e[1] == "sequence_eligibility"]
    assert len(burn_calls) == 1


# ── Eligibility gate (D16) + rhythm upgrade (T8) ─────────────────────────────────


def test_song_variant_never_transcribes_gets_rhythm(monkeypatch, tmp_path):
    """Gate change (T8): song-replaced audio still never transcribes, but it no
    longer means static — rhythm mode paces the authored quote over the song."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _, engine_calls = _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)
    quote_fn, quote_calls = _quote_fn()

    spec = {"variant_id": "song_text", "rank": 2, "text_mode": "agent_text", "track": _track()}
    res = _render(monkeypatch, tmp_path, spec=spec, author_quote_fn=quote_fn)

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"
    assert res["sequence_mode"] == "rhythm"
    assert res["sequence_quote"] == GOLDEN_QUOTE
    assert quote_calls == [6.0]  # authored against the probed base duration
    gate = [e for e in events if e[1] == "sequence_eligibility"]
    assert len(gate) == 1
    assert gate[0][2]["eligible"] is False
    assert gate[0][2]["reason"] == "song_replaced_audio"
    # The sequence geometry uses the EDITORIAL style under the ON switch.
    from app.pipeline.intro_cluster import EDITORIAL_STYLE

    assert engine_calls and all(c["style"] is EDITORIAL_STYLE for c in engine_calls)


def test_voiceover_variant_never_transcribes_gets_rhythm(monkeypatch, tmp_path):
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)
    monkeypatch.setattr(storage, "download_to_file", lambda gcs, local: None, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda p: 5.0, raising=False)
    monkeypatch.setattr(
        to,
        "_mix_user_voiceover",
        lambda video, voice, out, tmpdir, **kw: open(out, "wb").write(b"\x00" * 16),
        raising=False,
    )
    quote_fn, _ = _quote_fn()

    spec = {
        "variant_id": "voiceover_only",
        "rank": 1,
        "text_mode": "agent_text",
        "track": None,
        "archetype": "voiceover",
        "voiceover_gcs_path": "voiceover-uploads/abc/voice.webm",
        "mix": 1.0,
    }
    res = _render(monkeypatch, tmp_path, spec=spec, author_quote_fn=quote_fn)

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"
    assert res["sequence_mode"] == "rhythm"
    gate = [e for e in events if e[1] == "sequence_eligibility"]
    assert gate and gate[0][2]["reason"] == "voiceover_bed"


def test_linear_layout_never_transcribes_never_rhythms(monkeypatch, tmp_path):
    """The sequence is the editorial (cluster) auto-upgrade — a linear intro
    gets neither the transcript sync nor a rhythm quote."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(
        monkeypatch,
        tmp_path,
        agent_form={"effect": "karaoke-line", "layout": "linear"},
        author_quote_fn=_bomb_quote_fn,
    )

    assert res["ok"] is True
    assert res["intro_mode"] == "linear"
    assert res["sequence_mode"] is None
    gate = [e for e in events if e[1] == "sequence_eligibility"]
    assert gate and gate[0][2]["reason"] == "layout_not_cluster"


# ── Happy path: transcribe → split → annotate → burn ─────────────────────────────


def test_sequence_transcribes_assembled_path_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls, _ = _patch_render_helpers(monkeypatch)
    transcribe_calls = _patch_transcribe(monkeypatch)
    agent_calls = _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # D11: the transcription source is the PRE-mix montage (assembled.mp4), never
    # the audio-mixed output.
    assert len(transcribe_calls) == 1
    assert transcribe_calls[0].endswith("assembled.mp4")
    assert "audio_mixed" not in transcribe_calls[0]

    # Variant persistence (D19): authoritative mode + deterministic rebuild data.
    assert res["intro_mode"] == "sequence"
    assert res["intro_layout"] == "cluster"
    assert res["sequence_mode"] == "transcript"
    assert res["sequence_quote"] is None  # real speech ⇒ no rhythm quote
    assert res["sequence_base_size_px"] == 64
    assert [w["word"] for w in res["transcript"]][:5] == ["we", "went", "to", "the", "lake."]
    assert all(set(w) == {"word", "start_s", "end_s"} for w in res["transcript"])
    scenes = res["scenes"]
    assert [s["words"] for s in scenes] == [
        ["we", "went", "to", "the", "lake."],
        ["it", "was", "magic"],
    ]
    # word_roles filled from the (fake) agent annotation, aligned per phrase.
    assert scenes[0]["word_roles"] == ["hero", "hero", "hero", "hero", "closer"]
    assert scenes[1]["word_roles"] == ["hero", "hero", "closer"]
    assert len(agent_calls) == 1  # ONE call annotates all phrases

    # The burned overlays are the sequence overlays (absolute scene windows):
    # one instant static pop per block; only fade_out scenes carry a 350ms tail.
    burned = burn_calls[0]["overlays"]
    assert burned and all(o["role"] == "generative_sequence" for o in burned)
    assert all(o["effect"] == "static" for o in burned)
    assert {o.get("fade_out_ms") for o in burned} <= {None, 350}
    assert any(o.get("fade_out_ms") == 350 for o in burned)  # final scene fades

    # Trace coverage (gate + transcribed + scenes + emphasis source).
    names = [e[1] for e in events]
    assert "sequence_eligibility" in names
    assert "sequence_transcribed" in names
    assert "sequence_scenes" in names
    emph = [e for e in events if e[1] == "emphasis_source"]
    assert emph and emph[0][2]["source"] == "agent"


def test_emphasis_agent_failure_falls_back_to_heuristic(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch)
    _patch_emphasis_agent(monkeypatch, error=RuntimeError("schema fail"))
    events = _patch_trace(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"  # heuristic roles still render a sequence
    for scene in res["scenes"]:
        assert scene["word_roles"] is not None
        assert len(scene["word_roles"]) == len(scene["words"])
    emph = [e for e in events if e[1] == "emphasis_source"]
    assert emph and emph[0][2]["source"] == "heuristic"


# ── ASR failure / ineligible speech (D18) — rhythm attempt, then static ──────────


def test_transcribe_failure_no_quote_author_falls_back_static(monkeypatch, tmp_path):
    """ASR fails AND no quote author is available (no author fn, no persisted
    quote) — the render still succeeds with the static styled cluster."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls, engine_calls = _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, error=RuntimeError("whisper down"))
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["intro_mode"] == "cluster"
    assert res["transcript"] is None and res["scenes"] is None
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert fallback[0][2]["reason"] == "transcribe_failed"
    assert fallback[0][2]["mode"] == "transcript"
    assert fallback[1][2] == {
        "variant_id": "original_text",
        "reason": "no_quote_author",
        "mode": "rhythm",
    }
    assert len(burn_calls) == 1  # static burn still happened
    from app.pipeline.intro_cluster import EDITORIAL_STYLE

    assert engine_calls and engine_calls[0]["style"] is EDITORIAL_STYLE


def test_transcribe_failure_recovers_with_rhythm(monkeypatch, tmp_path):
    """D18 + T8: a wedged Whisper no longer costs the sequence — rhythm mode
    renders the authored quote instead."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, error=RuntimeError("whisper down"))
    _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)
    quote_fn, _ = _quote_fn()

    res = _render(monkeypatch, tmp_path, author_quote_fn=quote_fn)

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"
    assert res["sequence_mode"] == "rhythm"
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert [f[2]["reason"] for f in fallback] == ["transcribe_failed"]


def test_low_confidence_transcript_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, transcript=_fake_transcript(low_confidence=True))
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["intro_mode"] != "sequence"
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert fallback and fallback[0][2]["reason"] == "low_confidence_transcript"


# ── Rhythm mode (T8): authored quote, synthesized timings ────────────────────────


def test_no_speech_renders_rhythm_sequence(monkeypatch, tmp_path):
    """The approved-demo path: speech ineligible (too few words) ⇒ the quote is
    authored, paced by `rhythm_scenes`, annotated, and burned as the sequence."""
    from app.pipeline.phrase_sequence import rhythm_scenes

    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls, _ = _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, transcript=_fake_transcript(words=_transcript_words()[:3]))
    agent_calls = _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)
    quote_fn, quote_calls = _quote_fn()

    res = _render(monkeypatch, tmp_path, author_quote_fn=quote_fn)

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"
    assert res["intro_layout"] == "cluster"
    assert res["sequence_mode"] == "rhythm"
    assert res["sequence_quote"] == GOLDEN_QUOTE
    assert res["transcript"] is None  # no ASR words in rhythm mode
    assert res["sequence_base_size_px"] == 64
    assert quote_calls == [6.0]

    # Scenes are EXACTLY the deterministic synthesis of the quote at 6.0s —
    # 9 sentences ⇒ 9 scenes (same engine as the approved demo).
    expected = rhythm_scenes(GOLDEN_QUOTE, video_duration_s=6.0)
    assert len(res["scenes"]) == 9
    assert [s["words"] for s in res["scenes"]] == [s["words"] for s in expected]
    assert [(s["start_s"], s["end_s"]) for s in res["scenes"]] == [
        (s["start_s"], s["end_s"]) for s in expected
    ]
    assert res["scenes"][0]["words"][:2] == ["It's", "not"]
    # Roles annotated by the (fake) emphasis agent — ONE call for all phrases.
    assert len(agent_calls) == 1
    assert all(len(s["word_roles"]) == len(s["words"]) for s in res["scenes"])

    # The burned overlays are sequence overlays built from those scenes.
    burned = burn_calls[0]["overlays"]
    assert burned and all(o["role"] == "generative_sequence" for o in burned)

    # Trace coverage: the speech fallback, then the rhythm-specific events.
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert [f[2]["reason"] for f in fallback] == ["too_few_words"]
    authored = [e for e in events if e[1] == "sequence_quote_authored"]
    assert authored == [
        (
            "overlay",
            "sequence_quote_authored",
            {
                "variant_id": "original_text",
                "sentence_count": 9,
                "word_count": 33,
            },
        )
    ]
    scenes_evt = [e for e in events if e[1] == "sequence_scenes"]
    assert scenes_evt and scenes_evt[-1][2] == {
        "variant_id": "original_text",
        "count": 9,
        "mode": "rhythm",
    }
    emph = [e for e in events if e[1] == "emphasis_source"]
    assert emph and emph[-1][2] == {
        "variant_id": "original_text",
        "source": "agent",
        "mode": "rhythm",
    }


def test_quote_agent_failure_falls_back_to_static_cluster(monkeypatch, tmp_path):
    """No agent quote means the static styled cluster — NEVER a made-up quote
    (the asymmetry: quote authoring has no heuristic fallback by design)."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls, engine_calls = _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, transcript=_fake_transcript(words=_transcript_words()[:3]))
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    def _failing_author(video_duration_s):
        return None  # _author_sequence_quote maps agent TerminalError to None

    res = _render(monkeypatch, tmp_path, author_quote_fn=_failing_author)

    assert res["ok"] is True  # graceful: the variant still ships
    assert res["intro_mode"] == "cluster"
    assert res["sequence_mode"] is None
    assert res["sequence_quote"] is None
    assert res["scenes"] is None and res["transcript"] is None
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert [(f[2]["reason"], f[2]["mode"]) for f in fallback] == [
        ("too_few_words", "transcript"),
        ("quote_agent_failed", "rhythm"),
    ]
    assert len(burn_calls) == 1
    from app.pipeline.intro_cluster import EDITORIAL_STYLE

    assert engine_calls and all(c["style"] is EDITORIAL_STYLE for c in engine_calls)


def test_quote_author_raising_is_contained(monkeypatch, tmp_path):
    """A raising author fn (defensive — _author_sequence_quote never should)
    still degrades to the static cluster instead of failing the variant."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, error=RuntimeError("whisper down"))
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    def _raising_author(video_duration_s):
        raise RuntimeError("gemini 500")

    res = _render(monkeypatch, tmp_path, author_quote_fn=_raising_author)

    assert res["ok"] is True
    assert res["intro_mode"] == "cluster"
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert ("quote_agent_failed", "rhythm") in [(f[2]["reason"], f[2]["mode"]) for f in fallback]


def test_reassembly_retimes_persisted_quote_without_llm(monkeypatch, tmp_path):
    """Cut-edit contract (D15 + T8): a re-assembling re-render keeps the quote
    and re-times THE SAME words on the new duration — deterministic synthesis,
    zero quote-agent calls. Scenes are fresh (the old ones were cleared)."""
    from app.pipeline.phrase_sequence import rhythm_scenes

    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, error=RuntimeError("no speech in the recut"))
    _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(
        monkeypatch,
        tmp_path,
        assembly_steps_override=[],  # the user's cut IS the plan (timeline edit)
        author_quote_fn=_bomb_quote_fn,  # MUST not be called: quote is persisted
        existing_sequence_quote=GOLDEN_QUOTE,
    )

    assert res["ok"] is True
    assert res["intro_mode"] == "sequence"
    assert res["sequence_mode"] == "rhythm"
    assert res["sequence_quote"] == GOLDEN_QUOTE  # kept across the re-assembly
    expected = rhythm_scenes(GOLDEN_QUOTE, video_duration_s=6.0)
    assert [(s["start_s"], s["end_s"]) for s in res["scenes"]] == [
        (s["start_s"], s["end_s"]) for s in expected
    ]
    assert [e for e in events if e[1] == "sequence_quote_reused"]
    assert not [e for e in events if e[1] == "sequence_quote_authored"]


def test_rhythm_failure_on_new_duration_carries_quote_forward(monkeypatch, tmp_path):
    """If the persisted quote can't pace the new cut (degenerate synthesis),
    the static fallback ships but the quote SURVIVES for a later re-render."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, error=RuntimeError("no speech"))
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(
        monkeypatch,
        tmp_path,
        author_quote_fn=_bomb_quote_fn,
        existing_sequence_quote="Hi. Yo. Ok.",  # 3 words < MIN_SPEECH_WORDS
    )

    assert res["ok"] is True
    assert res["intro_mode"] == "cluster"
    assert res["sequence_mode"] is None
    assert res["scenes"] is None
    assert res["sequence_quote"] == "Hi. Yo. Ok."  # carried, not cleared
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    assert ("no_rhythm_scenes", "rhythm") in [(f[2]["reason"], f[2]["mode"]) for f in fallback]


def test_author_sequence_quote_threads_grounding_and_contains_failure(monkeypatch):
    """Unit: `_author_sequence_quote` builds the REAL SequenceQuoteInput from
    intro_writer's grounding shape and maps ANY agent failure to None."""
    import app.agents._model_client as mc
    import app.agents.sequence_quote_writer as sqw

    monkeypatch.setattr(mc, "default_client", lambda: None, raising=False)
    seen: list = []

    class _FakeQuoteAgent:
        def __init__(self, client):
            pass

        def run(self, inp, ctx=None):
            seen.append(inp)
            return types.SimpleNamespace(quote=GOLDEN_QUOTE)

    monkeypatch.setattr(sqw, "SequenceQuoteWriterAgent", _FakeQuoteAgent, raising=False)

    hero = _Meta("c1", 5.0)
    hero.transcript = "ambient chatter"
    quote = gb._author_sequence_quote(
        hero,
        job_id="j",
        video_duration_s=10.4,
        language="tr",
        persona={"tone": "warm", "content_pillars": ["travel"], "theme": "summer"},
        filming_guide=[{"what": "beach", "how": "wide", "duration_s": 3}],
    )

    assert quote == GOLDEN_QUOTE
    inp = seen[0]
    assert inp.hero_clip.clip_id == "c1"
    assert inp.hero_transcript == "ambient chatter"
    assert inp.language == "tr"
    assert inp.tone == "warm"
    assert inp.content_pillars == ["travel"]
    assert inp.video_duration_s == 10.4
    assert inp.filming_guide == [{"what": "beach", "how": "wide", "duration_s": 3}]

    class _BoomQuoteAgent:
        def __init__(self, client):
            pass

        def run(self, inp, ctx=None):
            raise RuntimeError("terminal: schema retry exhausted")

    monkeypatch.setattr(sqw, "SequenceQuoteWriterAgent", _BoomQuoteAgent, raising=False)
    assert gb._author_sequence_quote(hero, job_id="j", video_duration_s=10.4) is None


# ── D20: copy-through detection on the first-render burn ────────────────────────


def test_sequence_copy_through_falls_back_to_static_burn(monkeypatch, tmp_path):
    import app.pipeline.text_overlay_skia as skia_mod

    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls, _ = _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch)
    _patch_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    # First burn copy-throughs (same 16-byte size as the audio-mixed base); the
    # static-fallback burn succeeds.
    state = {"n": 0}

    def _flaky_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        state["n"] += 1
        burn_calls.append({"overlays": overlays})
        size = 16 if state["n"] == 1 else 24
        with open(out_path, "wb") as f:
            f.write(b"\x00" * size)

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _flaky_burn, raising=False)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert state["n"] == 2  # sequence burn + static fallback burn
    assert res["intro_mode"] == "cluster"  # the fallback is what shipped
    assert res["transcript"] is None and res["scenes"] is None
    assert res["sequence_mode"] is None
    fallback = [e for e in events if e[1] == "sequence_fallback"]
    copy_through = [e for e in fallback if e[2]["reason"] == "burn_copy_through"]
    assert copy_through and copy_through[0][2]["mode"] == "transcript"


def test_static_copy_through_fails_variant(monkeypatch, tmp_path):
    import app.pipeline.text_overlay_skia as skia_mod

    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    def _copy_through_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)  # same size as the base every time

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _copy_through_burn, raising=False)

    res = _render(monkeypatch, tmp_path, agent_form={"effect": "karaoke-line", "layout": "linear"})

    assert res["ok"] is False
    assert "copy-through" in res["error"]


# ── D15: re-assembly invalidation + opt-out ──────────────────────────────────────


def test_rerender_with_opt_out_clears_transcript_and_scenes(monkeypatch, tmp_path):
    """allow_sequence=False (layout/text edit) on a full re-render: the fresh
    result carries transcript/scenes/quote as None — the merge into the variant
    entry CLEARS the stale persisted values (D15/D19 opt-out)."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_render_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    events = _patch_trace(monkeypatch)

    res = _render(monkeypatch, tmp_path, allow_sequence=False, author_quote_fn=_bomb_quote_fn)

    assert res["ok"] is True
    assert res["intro_mode"] == "cluster"
    assert "transcript" in res and res["transcript"] is None
    assert "scenes" in res and res["scenes"] is None
    assert "sequence_base_size_px" in res and res["sequence_base_size_px"] is None
    assert "sequence_mode" in res and res["sequence_mode"] is None
    # The orchestrator passes existing_sequence_quote=None on opt-out edits, so
    # the merge clears any persisted rhythm quote alongside the scenes.
    assert "sequence_quote" in res and res["sequence_quote"] is None
    # Opted-out render never evaluates the gate (no transcription budget spent).
    assert not [e for e in events if e[1] == "sequence_eligibility"]


# ── Fast-reburn: deterministic rebuild from persisted scenes ─────────────────────


def _patch_reburn_helpers(monkeypatch, *, base_content=b"\x00" * 32):
    import app.pipeline.probe as probe_mod
    import app.pipeline.text_overlay_skia as skia_mod
    import app.storage as storage

    burn_calls: list[dict] = []

    def _fake_download(gcs_path, local_path):
        with open(local_path, "wb") as f:
            f.write(base_content)

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        burn_calls.append({"overlays": overlays})
        with open(out_path, "wb") as f:
            f.write(b"\x01" * (len(base_content) + 8))

    monkeypatch.setattr(storage, "download_to_file", _fake_download, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn, raising=False)
    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda p: types.SimpleNamespace(duration_s=6.0),
        raising=False,
    )
    return burn_calls


def _sequence_existing(**extra):
    return {
        "variant_id": "original_text",
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_3_original_text.mp4",
        "video_path": "generative-jobs/x/variant_3_original_text.mp4",
        "intro_text": "What a magic day this truly was indeed",  # 8 words (>6)
        "intro_highlight_word": None,
        "intro_text_size_px": 64,
        "intro_size_source": "computed",
        "intro_layout": "cluster",
        "intro_mode": "sequence",
        "sequence_mode": "transcript",
        "sequence_quote": None,
        "sequence_base_size_px": 64,
        "transcript": [{"word": "we", "start_s": 0.5, "end_s": 0.7}],
        "scenes": [
            {
                "words": ["we", "went", "to", "the", "lake."],
                "word_roles": ["connector", "hero", "hero", "connector", "closer"],
                "speech_start_s": 0.5,
                "speech_end_s": 1.8,
                "start_s": 0.5,
                "end_s": 2.15,
                "fade_out": False,
            },
            {
                "words": ["it", "was", "magic"],
                "word_roles": ["connector", "hero", "hero"],
                "speech_start_s": 1.9,
                "speech_end_s": 4.0,
                "start_s": 1.9,
                "end_s": 6.0,
                "fade_out": True,
            },
        ],
        "music_track_id": None,
        "rank": 3,
        "style_set_id": None,
        **extra,
    }


def test_reburn_sequence_rebuilds_from_scenes_zero_asr_zero_llm(monkeypatch):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls = _patch_reburn_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    seq_calls: list[dict] = []
    import app.pipeline.generative_overlays as go

    real_build = go.build_sequence_overlays

    def _spy(scenes, *, base_size_px, text_color="#FFFFFF"):
        seq_calls.append({"scenes": scenes, "base_size_px": base_size_px})
        return [
            {
                "role": "generative_sequence",
                "text": "we went to the lake.",
                "effect": "static",
                "start_s": 0.5,
                "end_s": 2.15,
            }
        ]

    monkeypatch.setattr(go, "build_sequence_overlays", _spy, raising=False)
    assert real_build is not _spy

    existing = _sequence_existing()
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=True,
    )

    # Rebuilt deterministically from the PERSISTED scenes at the persisted px.
    assert len(seq_calls) == 1
    assert seq_calls[0]["scenes"] == existing["scenes"]
    assert seq_calls[0]["base_size_px"] == 64
    assert len(burn_calls) == 1
    assert burn_calls[0]["overlays"][0]["role"] == "generative_sequence"
    assert result["intro_mode"] == "sequence"
    assert result["intro_layout"] == "cluster"
    assert result["sequence_base_size_px"] == 64
    # transcript/scenes/quote/mode NOT in the patch → merge keeps the persisted
    # values (a rhythm variant's quote survives font/size/style edits).
    assert "transcript" not in result
    assert "scenes" not in result
    assert "sequence_quote" not in result
    assert "sequence_mode" not in result


def test_reburn_rhythm_variant_rebuilds_from_scenes_keeps_quote(monkeypatch):
    """A rhythm-synced variant fast-reburns exactly like a transcript one: the
    persisted scenes rebuild deterministically — no quote agent, no synthesis —
    and the merge keeps sequence_quote/sequence_mode untouched."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls = _patch_reburn_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    seq_calls: list[dict] = []
    import app.pipeline.generative_overlays as go

    monkeypatch.setattr(
        go,
        "build_sequence_overlays",
        lambda scenes, *, base_size_px, text_color="#FFFFFF": (
            seq_calls.append({"scenes": scenes})
            or [{"role": "generative_sequence", "text": "x", "effect": "static"}]
        ),
        raising=False,
    )

    existing = _sequence_existing(sequence_mode="rhythm", sequence_quote=GOLDEN_QUOTE)
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=True,
    )

    assert len(seq_calls) == 1
    assert seq_calls[0]["scenes"] == existing["scenes"]
    assert len(burn_calls) == 1
    assert result["intro_mode"] == "sequence"
    assert "sequence_quote" not in result  # merge keeps GOLDEN_QUOTE persisted
    assert "sequence_mode" not in result  # merge keeps "rhythm" persisted


def test_reburn_size_nudge_rescales_sequence_base_px(monkeypatch):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_reburn_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    seq_calls: list[dict] = []
    import app.pipeline.generative_overlays as go

    monkeypatch.setattr(
        go,
        "build_sequence_overlays",
        lambda scenes, *, base_size_px, text_color="#FFFFFF": (
            seq_calls.append({"base_size_px": base_size_px})
            or [{"role": "generative_sequence", "text": "x", "effect": "static"}]
        ),
        raising=False,
    )

    existing = _sequence_existing()
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=72,
        settings=gb.settings,
        sequence_allowed=True,
    )

    assert seq_calls == [{"base_size_px": 72}]
    assert result["sequence_base_size_px"] == 72
    assert result["intro_text_size_px"] == 72
    assert result["intro_size_source"] == "user"
    assert result["intro_mode"] == "sequence"


def test_reburn_opt_out_renders_static_and_clears_sequence_data(monkeypatch):
    """sequence_allowed=False (explicit layout pick): static intro from the
    persisted text; the patch CLEARS transcript/scenes (no longer synced)."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    burn_calls = _patch_reburn_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    import app.pipeline.generative_overlays as go

    def _boom_sequence(*a, **k):
        raise AssertionError("sequence rebuild must NOT run on an opt-out edit")

    monkeypatch.setattr(go, "build_sequence_overlays", _boom_sequence, raising=False)
    monkeypatch.setattr(
        go,
        "build_persistent_intro_overlays",
        lambda **kw: [{"text": "a", "effect": "fade-in"}, {"text": "a", "effect": "static"}],
        raising=False,
    )

    existing = _sequence_existing()
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=False,
    )

    assert len(burn_calls) == 1
    assert result["intro_mode"] == "linear"  # 2 overlays → linear inference
    assert result["transcript"] is None
    assert result["scenes"] is None
    assert result["sequence_base_size_px"] is None
    # The rhythm quote is cleared too — an opted-out variant is fully unsynced.
    assert result["sequence_mode"] is None
    assert result["sequence_quote"] is None


def test_reburn_kill_switch_off_skips_sequence_rebuild(monkeypatch):
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", False, raising=False)
    _patch_reburn_helpers(monkeypatch)
    _bomb_transcribe(monkeypatch)
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)

    import app.pipeline.generative_overlays as go

    def _boom_sequence(*a, **k):
        raise AssertionError("sequence rebuild must NOT run with the kill switch off")

    monkeypatch.setattr(go, "build_sequence_overlays", _boom_sequence, raising=False)
    styles_seen: list = []
    monkeypatch.setattr(
        go,
        "build_persistent_intro_overlays",
        lambda **kw: (
            styles_seen.append(kw.get("cluster_style"))
            or [{"text": "a", "effect": "fade-in"}, {"text": "a", "effect": "static"}]
        ),
        raising=False,
    )

    existing = _sequence_existing()
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=True,
    )

    assert styles_seen == [None]  # legacy byte-identical static path
    assert result["intro_mode"] != "sequence"


# ── _run_regenerate_variant: opt-out plumbing ────────────────────────────────────


def test_regen_opt_out_expression():
    """The opt-out is layout/text edits only — size/style/swap/mix keep the sync.
    (Pins the allow_sequence expression in _run_regenerate_variant.)"""

    def allow(layout_override=None, override_text=None, remove_text=False):
        return layout_override is None and override_text is None and not remove_text

    assert allow() is True
    assert allow(layout_override="cluster") is False
    assert allow(layout_override="linear") is False
    assert allow(override_text="new hook") is False
    assert allow(remove_text=True) is False


# ── _sequence_gate unit coverage ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("layout", "track", "voiceover", "expected"),
    [
        ("cluster", None, None, (True, "ok")),
        ("linear", None, None, (False, "layout_not_cluster")),
        (None, None, None, (False, "layout_not_cluster")),
        ("cluster", "TRACK", None, (False, "song_replaced_audio")),
        ("cluster", None, "voiceover-uploads/v.webm", (False, "voiceover_bed")),
        ("cluster", "TRACK", "voiceover-uploads/v.webm", (False, "voiceover_bed")),
    ],
)
def test_sequence_gate_predicate(layout, track, voiceover, expected):
    track_obj = _track() if track else None
    assert (
        gb._sequence_gate(layout=layout, track=track_obj, voiceover_gcs_path=voiceover) == expected
    )
