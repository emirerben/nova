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
    # Starts are grid-snapped DOWN from the raw pad (0.75 → 22/30, 99.75 →
    # 2992/30) so mask_at offsets stay frame-integral.
    assert windows[0].start_s == pytest.approx(22 / 30)
    assert windows[0].end_s == pytest.approx(3.25)
    assert windows[1].start_s == pytest.approx(2992 / 30)
    assert windows[1].end_s == 120.0


def test_behind_subject_windows_empty_when_no_overlay_requests_occlusion():
    overlays = [{"behind_subject": False, "start_s": 0.0, "end_s": 2.0}]
    assert gb._behind_subject_windows(overlays, duration_s=10.0) == []


def test_behind_subject_windows_starts_are_frame_aligned():
    """Any overlay start must yield a window start that is an integral number
    of matte frames — a half-frame offset (the raw 0.25s pad = 7.5 frames)
    made mask_at's rounding repeat/skip mask indices every ~3 frames."""
    for start in (0.0, 0.4, 1.0, 3.337, 12.517):
        overlays = [{"behind_subject": True, "start_s": start, "end_s": start + 2.0}]
        windows = gb._behind_subject_windows(overlays, duration_s=120.0)
        assert len(windows) == 1
        ticks = windows[0].start_s * 30
        assert abs(ticks - round(ticks)) < 1e-6, f"start {start} → non-integral {ticks}"
        # Effective pad never shrinks below the nominal 0.25s.
        assert windows[0].start_s <= max(0.0, start - 0.25) + 1e-9


# ── _resolve_subject_matte_for_burn: cache / compute / strip-on-failure ─────────


def _patch_matte_module(monkeypatch, *, compute=None, sane=True, provider="PROVIDER"):
    import app.pipeline.subject_matte as sm_mod

    calls: dict = {"compute": [], "compute_kw": [], "downloads": [], "uploads": [], "deletes": []}

    def _fake_compute(video_path, windows, out_path, **kw):
        calls["compute"].append((video_path, windows, out_path))
        calls["compute_kw"].append(kw)
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
    monkeypatch.setattr(
        storage,
        "delete_object_best_effort",
        lambda gcs: calls["deletes"].append(gcs) or True,
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
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
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
    assert matte_path == "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    assert len(calls["compute"]) == 1
    assert calls["uploads"] == [
        "generative-jobs/j/base_1_x.mp4.matte.v2.mp4",
        "generative-jobs/j/base_1_x.mp4.matte.v2.mp4.json",
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


def test_matte_insane_stats_strips_and_persists_unstable_sentinel(monkeypatch, tmp_path):
    """A DEFINITIVE sanity-gate rejection (stats computed, matte_is_sane
    False, cut hints provided) persists the unstable sentinel so later
    reburns of the same base skip straight to plain text instead of
    recomputing every time."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch, sane=False)
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
        cut_boundaries_s=[1.0],
    )
    assert provider is None
    assert matte_path == "generative-jobs/j/base_1_x.mp4.matte.v2.unstable"
    assert "behind_subject" not in out_overlays[0]
    assert calls["uploads"] == []  # sentinel is a marker, not an object


def test_matte_insane_without_cut_hints_never_mints_sentinel(monkeypatch, tmp_path):
    """Gate rejection WITHOUT cut hints is ambiguous (legacy variants,
    subtitled silence-cut joins: real cuts count as jumps) — fall back for
    this burn only, keep the path retryable, never the permanent sentinel."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, sane=False)
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path is None  # unchanged — retries next burn
    assert "behind_subject" not in out_overlays[0]


def test_matte_cached_unstable_sentinel_short_circuits(monkeypatch, tmp_path):
    """A persisted unstable sentinel burns plain text with no download and
    no recompute — the fix for the pay-90s-per-reburn migration retry loop."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    sentinel = "generative-jobs/j/base_1_x.mp4.matte.v2.unstable"
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=overlays,
        tmpdir=str(tmp_path),
        cached_matte_path=sentinel,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path == sentinel  # persists unchanged
    assert "behind_subject" not in out_overlays[0]
    assert calls["compute"] == [] and calls["downloads"] == []


def test_matte_insane_migration_deletes_v1_and_persists_sentinel(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch, sane=False)
    v1 = "generative-jobs/j/base_1_x.mp4.matte.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=v1,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
        cut_boundaries_s=[1.0],
    )
    assert provider is None
    assert matte_path == "generative-jobs/j/base_1_x.mp4.matte.v2.unstable"
    assert calls["deletes"] == [v1, f"{v1}.json"]  # glitchy v1 freed


def test_matte_delete_guard_blocks_foreign_paths():
    """The migration cleanup may only ever delete job-scoped matte blobs —
    curated music/* and templates/* live in the same bucket."""
    assert gb._matte_delete_allowed("generative-jobs/j/base_1_x.mp4.matte.mp4") is True
    assert gb._matte_delete_allowed("music/curated-track.mp3") is False
    assert gb._matte_delete_allowed("templates/tpl.mp4") is False
    assert gb._matte_delete_allowed("generative-jobs/j/base.mp4") is False  # not a matte


def test_matte_cache_open_failure_falls_back_keeps_old_cache_path(monkeypatch, tmp_path):
    """A bad cached blob (corrupt/missing) must never clobber the persisted key with
    None — the caller might just be hitting a transient GCS blip."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, provider=None)  # SubjectMatteProvider.open → None
    overlays = [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}]
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
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

    def _fake_burn(
        base_path,
        overlays,
        out_path,
        tmpdir,
        *,
        matte=None,
        input_probe=None,
    ):
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
    assert res["subject_matte_path"] == "generative-jobs/j/base_3_original_text.mp4.matte.v2.mp4"
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
    assert not any(".matte." in key for key in calls["uploads"])


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
        return types.SimpleNamespace(duration_s=5.0, width=1080, height=1920)

    def _fake_overlays(**kwargs):
        bs = bool(kwargs.get("behind_subject"))
        ov = {"type": "text", "text": kwargs.get("text", "hi"), "start_s": 0.0, "end_s": 2.0}
        if bs:
            ov["behind_subject"] = True
        return [ov]

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None, input_probe=None):
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
    assert result["subject_matte_path"] == "generative-jobs/j/base_1_song_text.mp4.matte.v2.mp4"
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


# ── _compose_subtitled_final: matte threading (prod job 1e768d5b regression) ──
#
# Every subtitled render (first render + every reburn flavor) routes through
# this compositor; before the fix it burned with matte=None at every call
# site, so behind_subject on a subtitled text element was a silent no-op.


def _patch_subtitled_compose(monkeypatch, burn_seen: dict):
    import app.pipeline.probe as probe_mod
    import app.pipeline.text_overlay_skia as skia_mod

    monkeypatch.setattr(
        gb,
        "_text_element_burn_dicts",
        lambda variant: [dict(d) for d in variant.get("_burn_dicts") or []],
    )

    def _fake_burn(
        base,
        overlays,
        out,
        tmpdir,
        *,
        matte=None,
        canvas=None,
        input_probe=None,
    ):
        burn_seen.update({"overlays": overlays, "matte": matte, "canvas": canvas, "out": out})
        with open(out, "wb") as f:
            f.write(b"text")

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn, raising=False)

    def _fake_captions(input_path, output_path, variant, tmpdir):
        burn_seen["captions_input"] = input_path
        with open(output_path, "wb") as f:
            f.write(b"cap")

    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", _fake_captions)
    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda p: types.SimpleNamespace(duration_s=5.0, width=1080, height=1920),
        raising=False,
    )


def _subtitled_variant(**kw) -> dict:
    base = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "subject_matte_path": None,
        "orientation": None,
        "caption_cues": [{"text": "hi", "start_s": 0.0, "end_s": 1.0}],
        "_burn_dicts": [{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
    }
    base.update(kw)
    return base


def test_compose_subtitled_resolves_matte_and_passes_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)
    base_local = str(tmp_path / "base.mp4")
    with open(base_local, "wb") as f:
        f.write(b"\x00")

    final, matte_path = gb._compose_subtitled_final(
        base_local,
        _subtitled_variant(),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert len(calls["compute"]) == 1
    assert burn_seen["matte"] == "PROVIDER"
    assert matte_path == "generative-jobs/j/variant_1_subtitled_base.mp4.matte.v2.mp4"
    assert final.endswith("subtitled_final.mp4")
    # Subtitled variants are single-clip — the resolver forwards no cut hints.
    assert calls["compute_kw"][-1].get("cut_boundaries_s") is None
    # Captions burn onto the text-burned underlay, never with a matte.
    assert burn_seen["captions_input"] == burn_seen["out"]


def test_compose_subtitled_cached_matte_reused_no_recompute(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)
    cached = "generative-jobs/j/variant_1_subtitled_base.mp4.matte.v2.mp4"

    _final, matte_path = gb._compose_subtitled_final(
        str(tmp_path / "base.mp4"),
        _subtitled_variant(subject_matte_path=cached),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert calls["compute"] == []
    assert cached in calls["downloads"]
    assert burn_seen["matte"] == "PROVIDER"
    assert matte_path == cached


def test_compose_subtitled_flag_off_burns_plain_and_keeps_cached_path(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", False, raising=False)
    calls = _patch_matte_module(monkeypatch)
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)

    _final, matte_path = gb._compose_subtitled_final(
        str(tmp_path / "base.mp4"),
        _subtitled_variant(subject_matte_path="old/matte.mp4"),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert calls["compute"] == [] and calls["downloads"] == []
    assert burn_seen["matte"] is None
    # Overlays keep the key (renderer logs the no-matte fallback — montage
    # semantics) and the cached path survives untouched for a later flag-on.
    assert burn_seen["overlays"][0].get("behind_subject") is True
    assert matte_path == "old/matte.mp4"


def test_compose_subtitled_resolver_failure_strips_and_completes(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, compute="none")
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)

    final, matte_path = gb._compose_subtitled_final(
        str(tmp_path / "base.mp4"),
        _subtitled_variant(),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert final.endswith("subtitled_final.mp4"), "matte failure must never fail the render"
    assert burn_seen["matte"] is None
    assert all("behind_subject" not in ov for ov in burn_seen["overlays"])
    assert matte_path is None  # cached was None; failure never mints a path


def test_compose_subtitled_no_behind_elements_skips_resolver(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)

    _final, matte_path = gb._compose_subtitled_final(
        str(tmp_path / "base.mp4"),
        _subtitled_variant(_burn_dicts=[{"start_s": 0.0, "end_s": 2.0}]),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert calls["compute"] == [] and calls["downloads"] == []
    assert burn_seen["matte"] is None
    assert matte_path is None


def test_compose_subtitled_landscape_variant_gets_landscape_canvas(monkeypatch, tmp_path):
    from app.pipeline.canvas import LANDSCAPE

    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch)
    burn_seen: dict = {}
    _patch_subtitled_compose(monkeypatch, burn_seen)

    gb._compose_subtitled_final(
        str(tmp_path / "base.mp4"),
        _subtitled_variant(orientation="landscape"),
        str(tmp_path),
        job_id="j",
        variant_id="subtitled",
        upload_key_base="generative-jobs/j/variant_1_subtitled_base.mp4",
    )

    assert burn_seen["canvas"] is LANDSCAPE


# ── subject_matte_resolved trace event (admin job-debug observability) ────────
#
# Prod job 1e768d5b had ZERO matte visibility in the pipeline trace — the
# resolver is the single chokepoint, so one event there covers montage,
# lyrics, text-element and subtitled call sites alike.


def _capture_trace_events(monkeypatch) -> list:
    import app.services.pipeline_trace as trace_mod

    events: list = []
    monkeypatch.setattr(
        trace_mod,
        "record_pipeline_event",
        lambda stage, event, data: events.append((stage, event, data)),
        raising=False,
    )
    return events


def test_resolver_records_computed_trace_event(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch)
    events = _capture_trace_events(monkeypatch)
    gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert events == [
        (
            "overlay",
            "subject_matte_resolved",
            {
                "variant_id": "v",
                "source": "computed",
                "matte_path": "generative-jobs/j/base_1_x.mp4.matte.v2.mp4",
            },
        )
    ]


def test_resolver_records_cache_trace_event(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch)
    events = _capture_trace_events(monkeypatch)
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert events[0][2]["source"] == "cache"
    assert events[0][2]["matte_path"] == cached


def test_resolver_records_fallback_trace_event(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_matte_module(monkeypatch, compute="none")
    events = _capture_trace_events(monkeypatch)
    gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("overlay", "subject_matte_resolved")
    assert data["outcome"] == "fallback_stripped"
    assert "error" in data


def test_resolver_flag_off_records_no_trace_event(monkeypatch, tmp_path):
    _patch_matte_module(monkeypatch)
    events = _capture_trace_events(monkeypatch)
    gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert events == []


# ── v1 → v2 matte-cache migration + cut-boundary plumbing ─────────────────────


def test_stale_v1_cache_triggers_recompute_under_v2_key(monkeypatch, tmp_path):
    """A persisted `.matte.mp4` path predates the beach-glitch fix and may be
    a glitching matte the old gate accepted — it must be treated as a cache
    miss, recomputed under the v2 key, and the v1 blob freed."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    v1 = "generative-jobs/j/base_1_x.mp4.matte.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=v1,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider == "PROVIDER"
    assert matte_path == "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    assert len(calls["compute"]) == 1  # v1 cache did NOT satisfy the burn
    assert v1 not in calls["downloads"]
    assert calls["deletes"] == [v1, f"{v1}.json"]


def test_stale_v1_cache_recompute_failure_keeps_v1_path_and_strips(monkeypatch, tmp_path):
    """Failed migration: burn falls back to plain text for THIS render but the
    persisted v1 path survives untouched, so the next burn retries."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch, compute="none")
    v1 = "generative-jobs/j/base_1_x.mp4.matte.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=v1,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert provider is None
    assert matte_path == v1  # NOT clobbered — migration retries next burn
    assert "behind_subject" not in out_overlays[0]
    assert calls["deletes"] == []  # never delete what we couldn't replace


def test_resolver_forwards_cut_boundaries_to_compute(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=None,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
        cut_boundaries_s=[2.0, 5.0],
    )
    assert calls["compute_kw"] == [{"cut_boundaries_s": [2.0, 5.0]}]


# ── boundary derivation helpers ───────────────────────────────────────────────


def test_cut_boundaries_from_durations_cumulative_interior_cuts():
    assert gb._cut_boundaries_from_durations([2.0, 3.0, 2.5]) == [2.0, 5.0]


def test_cut_boundaries_from_durations_single_slot_is_none():
    assert gb._cut_boundaries_from_durations([4.0]) is None
    assert gb._cut_boundaries_from_durations([]) is None


def test_variant_slot_boundaries_from_ai_timeline():
    existing = {
        "ai_timeline": {
            "slots": [
                {"order": 1, "duration_s": 3.0},
                {"order": 0, "duration_s": 2.0},
                {"order": 2, "duration_s": 2.5},
            ]
        }
    }
    assert gb._variant_slot_boundaries(existing) == [2.0, 5.0]


def test_variant_slot_boundaries_user_timeline_wins_and_skips_removed():
    existing = {
        "user_timeline": {
            "slots": [
                {"order": 0, "duration_s": 1.0},
                {"order": 1, "duration_s": 2.0, "removed": True},
                {"order": 2, "duration_s": 4.0},
            ]
        },
        "ai_timeline": {"slots": [{"order": 0, "duration_s": 9.0}]},
    }
    assert gb._variant_slot_boundaries(existing) == [1.0]


def test_variant_slot_boundaries_collage_and_legacy_are_none():
    assert gb._variant_slot_boundaries({}) is None
    assert (
        gb._variant_slot_boundaries(
            {
                "montage_preset_rendered": "masonry_percent_flash",
                "ai_timeline": {"slots": [{"order": 0, "duration_s": 2.0}]},
            }
        )
        is None
    )


def test_variant_slot_boundaries_garbage_timeline_is_none():
    """Boundary hints are best-effort by design — a malformed persisted
    timeline (non-dict slots) must resolve to None, never raise into the
    burn path."""
    assert (
        gb._variant_slot_boundaries({"user_timeline": {"slots": [{"order": 0}, "garbage"]}}) is None
    )


def test_cut_boundaries_from_durations_zero_duration_slots_skipped():
    """A leading zero-duration slot produces no t=0 'cut'; None durations
    coerce to 0.0 instead of raising."""
    assert gb._cut_boundaries_from_durations([0.0, 2.0, 3.0]) == [2.0]
    assert gb._cut_boundaries_from_durations([None, 4.0]) is None  # type: ignore[list-item]


# ── call-site wiring: cut boundaries actually reach compute ───────────────────


def test_render_generative_variant_forwards_slot_cut_boundaries(monkeypatch, tmp_path):
    """First-render path (the beach-glitch scenario): a classic cut-only
    assembly must forward its slot joins — cumulative resolved-plan durations,
    last slot excluded — into compute_subject_matte."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)

    # The shared fake assembler ignores resolved_plans_out; this variant fills
    # it like the real _assemble_clips does, so the boundary derivation at the
    # matte call site has real slot durations to work from.
    import app.tasks.template_orchestrate as to

    def _fake_assemble(steps, c2l, probe, out_path, tmpdir, **kw):
        sink = kw.get("resolved_plans_out")
        if sink is not None:
            sink.extend([{"duration_s": 2.0}, {"duration_s": 3.0}, {"duration_s": 2.5}])
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)

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
    assert calls["compute_kw"] == [{"cut_boundaries_s": [2.0, 5.0]}]


def test_reburn_forwards_variant_slot_boundaries_from_existing(monkeypatch):
    """Reburn path: cut hints come from the persisted timeline on `existing`
    (user_timeline precedence, same as the cut-preserving reburn) and must
    reach compute_subject_matte."""
    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    _patch_reburn_with_real_overlays(monkeypatch)
    calls = _patch_matte_module(monkeypatch)

    existing = {
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
        "video_path": "generative-jobs/j/variant_1_song_text.mp4",
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "intro_layout": "linear",
        "subject_matte_path": None,
        "user_timeline": {
            "slots": [
                {"order": 0, "duration_s": 1.5},
                {"order": 1, "duration_s": 2.0},
                {"order": 2, "duration_s": 3.0},
            ]
        },
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
    assert calls["compute_kw"] == [{"cut_boundaries_s": [1.5, 3.5]}]


def test_v2_cache_coverage_mismatch_triggers_recompute(monkeypatch, tmp_path):
    """A cached v2 matte whose stored windows don't span the requested
    overlay windows (text timing moved since compute) must be treated as a
    miss — otherwise mask_at returns None mid-overlay and occlusion silently
    drops out."""
    import app.pipeline.subject_matte as sm_mod

    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    narrow = types.SimpleNamespace(window_spans=lambda: [(5.0, 6.0)])

    def _open(path):
        return narrow if "cached" in path else "FRESH"

    monkeypatch.setattr(sm_mod.SubjectMatteProvider, "open", staticmethod(_open), raising=False)
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert len(calls["compute"]) == 1  # coverage miss → recompute
    assert provider == "FRESH"
    assert matte_path == cached  # same v2 key, overwritten in place
    assert out_overlays[0].get("behind_subject") is True


def test_v2_cache_matching_coverage_reused(monkeypatch, tmp_path):
    """Control: a cached matte whose spans cover the requested windows is
    reused — no recompute (the steady-state fast reburn)."""
    import app.pipeline.subject_matte as sm_mod

    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)
    covering = types.SimpleNamespace(window_spans=lambda: [(0.0, 4.0)])
    monkeypatch.setattr(
        sm_mod.SubjectMatteProvider, "open", staticmethod(lambda path: covering), raising=False
    )
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    provider, matte_path, _ = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.5, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert calls["compute"] == []
    assert provider is covering
    assert matte_path == cached


def test_v2_cache_broken_blob_recomputes_instead_of_poisoning(monkeypatch, tmp_path):
    """A corrupt v2 blob/sidecar must be treated as a cache miss (recompute
    under the same key), not returned as a permanent failure on every burn."""
    import app.pipeline.subject_matte as sm_mod

    monkeypatch.setattr(gb.settings, "text_behind_subject_enabled", True, raising=False)
    calls = _patch_matte_module(monkeypatch)

    def _open(path):
        return None if "cached" in path else "FRESH"

    monkeypatch.setattr(sm_mod.SubjectMatteProvider, "open", staticmethod(_open), raising=False)
    cached = "generative-jobs/j/base_1_x.mp4.matte.v2.mp4"
    provider, matte_path, out_overlays = gb._resolve_subject_matte_for_burn(
        video_path="/local/base.mp4",
        overlays=[{"start_s": 0.0, "end_s": 2.0, "behind_subject": True}],
        tmpdir=str(tmp_path),
        cached_matte_path=cached,
        upload_key_base="generative-jobs/j/base_1_x.mp4",
        duration_s=5.0,
        job_id="j",
        variant_id="v",
    )
    assert len(calls["compute"]) == 1
    assert provider == "FRESH"
    assert matte_path == cached
    assert out_overlays[0].get("behind_subject") is True
