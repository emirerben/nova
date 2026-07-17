"""Text-behind-subject orchestration: kill-switch gating, sticky precedence,
matte compute/cache/strip lifecycle, and the burn-dict shape contract.

Mirrors the mocking style of test_generative_build.py: heavy IO (ffmpeg, GCS,
mediapipe) is stubbed so these pin ORCHESTRATION logic, not real segmentation.
`app.pipeline.subject_matte.compute_subject_matte` is NEVER exercised for
real here — see docs/runbooks/local-render.md for real-segmentation
verification.
"""

from __future__ import annotations

import types

import pytest

import app.pipeline.generative_overlays as go
import app.tasks.generative_build as gb
from tests.tasks.test_generative_build import _Meta, _patch_render_helpers

# ── generative_overlays.py: burn-dict shape (byte-identical off / key-present on) ──


def test_build_intro_overlay_flag_false_omits_key():
    overlay = go.build_intro_overlay("hi", effect="static", start_s=0.0, end_s=1.0)
    assert "behind_subject" not in overlay


def test_build_intro_overlay_flag_true_sets_key():
    overlay = go.build_intro_overlay(
        "hi", effect="static", start_s=0.0, end_s=1.0, behind_subject=True
    )
    assert overlay["behind_subject"] is True


def test_build_persistent_intro_overlays_flag_false_no_key_anywhere():
    overlays = go.build_persistent_intro_overlays(
        text="hi", effect="karaoke-line", reveal_window_s=1.0
    )
    assert overlays
    assert all("behind_subject" not in ov for ov in overlays)


def test_build_persistent_intro_overlays_threads_flag_to_every_overlay():
    overlays = go.build_persistent_intro_overlays(
        text="hi", effect="karaoke-line", reveal_window_s=1.0, behind_subject=True
    )
    assert len(overlays) == 2  # reveal + hold (linear)
    assert all(ov.get("behind_subject") is True for ov in overlays)


def test_build_persistent_intro_overlays_cluster_threads_flag_to_all_blocks(monkeypatch):
    """Cluster layout: EVERY block overlay (reveal + hold, per block) carries the flag."""
    import app.pipeline.intro_cluster as ic

    def _fake_blocks(text, **kw):
        return [
            {
                "text": "a",
                "font_family": "Inter-Bold",
                "text_color": "#FFF",
                "text_size_px": 60,
                "position_x_frac": 0.5,
                "position_y_frac": 0.4,
                "start_offset_s": 0.0,
                "reveal_s": 0.3,
            },
            {
                "text": "b",
                "font_family": "Inter-Bold",
                "text_color": "#FFF",
                "text_size_px": 60,
                "position_x_frac": 0.5,
                "position_y_frac": 0.6,
                "start_offset_s": 0.3,
                "reveal_s": 0.3,
            },
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks, raising=False)
    overlays = go.build_persistent_intro_overlays(
        text="a b",
        effect="karaoke-line",
        reveal_window_s=1.0,
        layout="cluster",
        behind_subject=True,
    )
    assert len(overlays) == 4  # 2 blocks x [reveal, hold]
    assert all(ov.get("behind_subject") is True for ov in overlays)


def test_build_overlays_from_text_elements_threads_per_element_flag():
    """getattr(elem, 'behind_subject', False) — works whether or not Lane E's
    TextElement field is on disk yet."""
    from app.agents._schemas.text_element import TextElement

    on = TextElement(text="occluded", start_s=0.0, end_s=2.0, effect="static")
    on.behind_subject = True  # simulates Lane E's field once landed
    off = TextElement(text="plain", start_s=2.0, end_s=4.0, effect="static")

    overlays = go.build_overlays_from_text_elements([on, off], video_duration_s=10.0)
    by_text = {ov["text"]: ov for ov in overlays}
    assert by_text["occluded"].get("behind_subject") is True
    assert "behind_subject" not in by_text["plain"]


def test_build_overlays_from_text_elements_missing_field_defaults_false():
    """Field absent entirely (Lane E not landed yet) → no key, no crash."""
    from app.agents._schemas.text_element import TextElement

    elem = TextElement(text="hi", start_s=0.0, end_s=2.0, effect="static")
    overlays = go.build_overlays_from_text_elements([elem], video_duration_s=10.0)
    assert "behind_subject" not in overlays[0]


# ── _resolve_intro_overlay_params: gating + precedence ──────────────────────────


def _agent_text():
    return types.SimpleNamespace(text="hi", highlight_word=None, word_roles=None)


def test_resolve_intro_overlay_params_flag_off_golden():
    """Agent form REQUESTS behind_subject=True but the flag is off (default) →
    gated params key is False and the pre-gate decision is still captured for
    persistence. build_intro_overlay fed the gated value emits NO key at all —
    byte-identical to pre-feature output."""
    agent_form = {"effect": "karaoke-line", "behind_subject": True}
    params, _, _ = gb._resolve_intro_overlay_params(
        _agent_text(), agent_form, None, size_override_px=100
    )
    assert params["behind_subject"] is False
    assert params.pop("_bs_pregate") is True  # pre-gate: what the AI actually wanted
    overlay = go.build_intro_overlay(
        "hi", effect="static", start_s=0.0, end_s=1.0, behind_subject=params["behind_subject"]
    )
    assert "behind_subject" not in overlay


def test_resolve_intro_overlay_params_precedence_kwarg_wins(monkeypatch):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    agent_form = {"effect": "karaoke-line", "behind_subject": False}  # persisted-folded False
    params, _, _ = gb._resolve_intro_overlay_params(
        _agent_text(),
        agent_form,
        None,
        size_override_px=100,
        behind_subject_override=True,  # explicit task kwarg
    )
    assert params["behind_subject"] is True


def test_resolve_intro_overlay_params_precedence_kwarg_none_falls_back_to_persisted(monkeypatch):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    agent_form = {"effect": "karaoke-line", "behind_subject": False}
    params, _, _ = gb._resolve_intro_overlay_params(
        _agent_text(), agent_form, None, size_override_px=100, behind_subject_override=None
    )
    assert params["behind_subject"] is False


def test_resolve_intro_overlay_params_precedence_absent_falls_back_to_agent_form(monkeypatch):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    agent_form = {"effect": "karaoke-line"}  # no "behind_subject" key at all
    params, _, _ = gb._resolve_intro_overlay_params(
        _agent_text(), agent_form, None, size_override_px=100, behind_subject_override=None
    )
    assert params["behind_subject"] is False


def test_resolve_intro_overlay_params_flag_on_agent_decision_survives(monkeypatch):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    agent_form = {"effect": "karaoke-line", "behind_subject": True}
    params, _, _ = gb._resolve_intro_overlay_params(
        _agent_text(), agent_form, None, size_override_px=100
    )
    assert params["behind_subject"] is True
    assert params.pop("_bs_pregate") is True


# ── _behind_subject_windows: padding / clamping / merge ─────────────────────────


def test_behind_subject_windows_pads_clamps_and_merges_overlaps():
    from app.pipeline.subject_matte import MatteWindow

    overlays = [
        {"behind_subject": True, "start_s": 1.0, "end_s": 2.0},
        {"behind_subject": True, "start_s": 2.1, "end_s": 3.0},  # merges after padding
        {"behind_subject": False, "start_s": 10.0, "end_s": 11.0},  # ignored
        {"behind_subject": True, "start_s": 100.0, "end_s": 3600.0},  # clamped to duration
    ]
    windows = gb._behind_subject_windows(overlays, duration_s=120.0)
    assert len(windows) == 2
    assert all(isinstance(w, MatteWindow) for w in windows)
    assert windows[0].start_s == pytest.approx(0.75)
    assert windows[0].end_s == pytest.approx(3.25)
    assert windows[1].start_s == pytest.approx(99.75)
    assert windows[1].end_s == 120.0


def test_behind_subject_windows_empty_when_no_overlay_requests_occlusion():
    overlays = [{"behind_subject": False, "start_s": 0.0, "end_s": 2.0}]
    assert gb._behind_subject_windows(overlays, duration_s=10.0) == []


# ── _resolve_subject_matte_for_burn: cache / compute / strip-on-failure ─────────


def _patch_matte_module(monkeypatch, *, compute=None, sane=True, provider="PROVIDER"):
    import app.pipeline.subject_matte as sm_mod

    calls: dict = {"compute": [], "downloads": [], "uploads": []}

    def _fake_compute(video_path, windows, out_path, **kw):
        calls["compute"].append((video_path, windows, out_path))
        if compute == "none":
            return None
        return types.SimpleNamespace(mean_coverage=0.3, max_coverage=0.5)

    monkeypatch.setattr(sm_mod, "compute_subject_matte", _fake_compute, raising=False)
    monkeypatch.setattr(sm_mod, "matte_is_sane", lambda stats: sane, raising=False)
    monkeypatch.setattr(
        sm_mod.SubjectMatteProvider, "open", staticmethod(lambda path: provider), raising=False
    )

    import app.storage as storage

    def _fake_download(gcs, local):
        calls["downloads"].append(gcs)
        # Write a dummy file so a caller that also downloads the BASE through this
        # same mock (e.g. _reburn_text_on_base's base_gcs_path download, which
        # runs before matte resolution) doesn't hit a FileNotFoundError later.
        with open(local, "wb") as f:
            f.write(b"\x00" * 8)

    monkeypatch.setattr(storage, "download_to_file", _fake_download, raising=False)
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda local, gcs: calls["uploads"].append(gcs) or f"https://signed/{gcs}",
        raising=False,
    )
    return calls


def test_matte_flag_off_never_computes(monkeypatch, tmp_path):
    calls = _patch_matte_module(monkeypatch)
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path is None
    assert out_overlays is overlays  # untouched (flag-off early return)
    assert calls["compute"] == []


def test_matte_no_behind_subject_overlays_never_computes(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    overlays = [{"start_s": 0.0, "end_s": 2.0}]  # no behind_subject key at all
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert calls["compute"] == []
    assert out_overlays is overlays


def test_matte_reburn_with_cache_downloads_never_recomputes(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    cached = "generative-jobs/j/base_1_x.mp4.matte.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider == "PROVIDER"
    assert matte_path == cached  # unchanged — reused, not recomputed
    assert calls["compute"] == []  # the whole point of the cache
    assert calls["downloads"] == [cached, f"{cached}.json"]
    assert out_overlays is overlays


def test_matte_reburn_toggle_on_without_cache_computes_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=None,  # no cache — "toggle ON for an old variant" path
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider == "PROVIDER"
    assert matte_path == "generative-jobs/j/base_1_x.mp4.matte.mp4"
    assert len(calls["compute"]) == 1
    assert calls["uploads"] == [
        "generative-jobs/j/base_1_x.mp4.matte.mp4",
        "generative-jobs/j/base_1_x.mp4.matte.mp4.json",
    ]
    assert out_overlays is overlays


def test_matte_compute_none_strips_keys_falls_back_no_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, compute="none")
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True, "text": "hi"}]
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path is None
    assert out_overlays is not overlays  # a stripped COPY, not the original
    assert "behind_subject" not in out_overlays[0]
    assert overlays[0]["behind_subject"] is True  # original never mutated


def test_matte_insane_stats_strips_keys_falls_back_no_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, sane=False)
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert "behind_subject" not in out_overlays[0]


def test_matte_cache_open_failure_falls_back_keeps_old_cache_path(monkeypatch, tmp_path):
    """A bad cached blob (corrupt/missing) must never clobber the persisted key with
    None — the caller might just be hitting a transient GCS blip."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, provider=None)  # SubjectMatteProvider.open → None
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    cached = "generative-jobs/j/base_1_x.mp4.matte.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path == cached  # NOT clobbered to None
    assert "behind_subject" not in out_overlays[0]


# ── First-render integration: _render_generative_variant end to end ─────────────


def test_render_generative_variant_flag_on_computes_matte_and_burns_with_provider(
    monkeypatch, tmp_path
):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch)

    # Override the skia burn stub installed by _patch_render_helpers so we can
    # capture the `matte=` kwarg it was actually called with.
    import app.pipeline.text_overlay_skia as skia_mod

    burn_calls: list = []

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        burn_calls.append({"overlays": overlays, "matte": matte})
        with open(out_path, "wb") as f:
            f.write(b"\x01" * 24)

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn, raising=False)

    vdir = tmp_path / "v"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "agent_text", "track": None}
    agent_text = types.SimpleNamespace(text="My hook", highlight_word=None, word_roles=None)
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=agent_text,
        agent_form={"effect": "karaoke-line", "behind_subject": True},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["intro_behind_subject"] is True
    assert res["subject_matte_path"] == "generative-jobs/j/base_3_original_text.mp4.matte.mp4"
    assert burn_calls, "burn_text_overlays_skia was never called"
    assert burn_calls[-1]["matte"] == "PROVIDER"
    assert any(ov.get("behind_subject") for ov in burn_calls[-1]["overlays"])


def test_render_generative_variant_flag_off_no_matte_no_key(monkeypatch, tmp_path):
    """Kill-switch guarantee: flag off ⇒ no compute, no extra GCS object, no burn-dict
    key (byte-identical render). `intro_behind_subject` still persists the PRE-GATE
    decision (what the AI wanted) so a later flag flip-on can re-enable it without
    re-running the AI — same "pre-gate persistence" contract as `intro_layout`."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    calls = _patch_matte_module(monkeypatch)  # flag stays default False

    vdir = tmp_path / "v"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "agent_text", "track": None}
    agent_text = types.SimpleNamespace(text="My hook", highlight_word=None, word_roles=None)
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=agent_text,
        agent_form={"effect": "karaoke-line", "behind_subject": True},  # AI wants it; flag off
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["intro_behind_subject"] is True  # pre-gate decision, not the render outcome
    assert res["subject_matte_path"] is None
    assert calls["compute"] == []
    # No extra GCS object: `calls["uploads"]` captures ALL upload_public_read calls
    # (matte-specific and the ordinary base_video_path upload alike, since this
    # fixture's mock is installed globally) — assert none of them is a matte key.
    assert not any(
        key.endswith(".matte.mp4") or key.endswith(".matte.mp4.json") for key in calls["uploads"]
    )


# ── _reburn_text_on_base: wiring smoke test (matte param threads through) ───────


def _patch_reburn_with_real_overlays(monkeypatch, *, base_content=b"\x00" * 32):
    """Like test_generative_build._patch_reburn_helpers, but build_persistent_intro_overlays
    echoes `behind_subject` from its kwargs instead of returning a fixed stub dict —
    needed to exercise the matte-resolution branch inside _reburn_text_on_base."""
    import app.pipeline.probe as probe_mod
    import app.pipeline.text_overlay_skia as skia
    import app.storage as storage

    burn_calls: list = []

    def _fake_download(gcs_path, local_path):
        with open(local_path, "wb") as f:
            f.write(base_content)

    def _fake_probe(path):
        return types.SimpleNamespace(duration_s=5.0)

    def _fake_overlays(**kwargs):
        bs = bool(kwargs.get("behind_subject"))
        ov = {"type": "text", "text": kwargs.get("text", "hi"), "start_s": 0.0, "end_s": 2.0}
        if bs:
            ov["behind_subject"] = True
        return [ov]

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        burn_calls.append({"overlays": overlays, "matte": matte})
        with open(out_path, "wb") as f:
            f.write(b"\x01" * (len(base_content) + 8))

    monkeypatch.setattr(storage, "download_to_file", _fake_download, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(go, "build_persistent_intro_overlays", _fake_overlays, raising=False)
    monkeypatch.setattr(skia, "burn_text_overlays_skia", _fake_burn, raising=False)
    monkeypatch.setattr(probe_mod, "probe_video", _fake_probe, raising=False)
    return burn_calls


def test_reburn_text_on_base_toggle_on_computes_matte_and_persists_path(monkeypatch):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    burn_calls = _patch_reburn_with_real_overlays(monkeypatch)
    _patch_matte_module(monkeypatch)

    existing = {
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
        "video_path": "generative-jobs/j/variant_1_song_text.mp4",
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "intro_layout": "linear",
        "subject_matte_path": None,
    }
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="song_text",
        existing=existing,
        agent_text=types.SimpleNamespace(text="Hi", highlight_word=None),
        agent_form={"effect": "karaoke-line"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        text_behind_subject=True,  # explicit task kwarg — the toggle-on request
    )
    assert result["render_status"] == "ready"
    assert result["intro_behind_subject"] is True
    assert result["subject_matte_path"] == "generative-jobs/j/base_1_song_text.mp4.matte.mp4"
    assert burn_calls[-1]["matte"] == "PROVIDER"


def test_reburn_text_on_base_flag_off_no_matte_key_present(monkeypatch):
    """Kill-switch: even with text_behind_subject=True requested, flag-off means the
    reburn never computes a matte and the burn dict never carries the key."""
    burn_calls = _patch_reburn_with_real_overlays(monkeypatch)
    calls = _patch_matte_module(monkeypatch)  # flag stays default False

    existing = {
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
        "video_path": "generative-jobs/j/variant_1_song_text.mp4",
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "intro_layout": "linear",
    }
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="song_text",
        existing=existing,
        agent_text=types.SimpleNamespace(text="Hi", highlight_word=None),
        agent_form={"effect": "karaoke-line"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        text_behind_subject=True,
    )
    assert result["render_status"] == "ready"
    assert result["intro_behind_subject"] is True  # pre-gate: the requested decision
    assert result["subject_matte_path"] is None
    assert calls["compute"] == []
    assert all("behind_subject" not in ov for ov in burn_calls[-1]["overlays"])
