"""Kill-switch tests for the T3 TextElement snapshot + _reburn_text_on_base early branch.

Two lightweight tests:
  1. _maybe_add_text_elements_snapshot is a no-op when _TEXT_ELEMENTS_ENABLED=False.
  2. _reburn_text_on_base does NOT take the text_elements early branch when
     _TEXT_ELEMENTS_ENABLED=False (even if text_elements_user_edited=True on the
     variant — the function falls through to the legacy overlay path).
"""

from __future__ import annotations

import types

import app.tasks.generative_build as gb

# ── Test 1: snapshot helper respects kill switch ────────────────────────────────


def test_snapshot_helper_no_op_when_kill_switch_off(monkeypatch):
    """_maybe_add_text_elements_snapshot must not add text_elements when disabled."""
    monkeypatch.setattr(gb, "_TEXT_ELEMENTS_ENABLED", False)

    # A result dict that would normally yield text_elements (intro_text is set).
    result = {
        "ok": True,
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "intro_text": "the mountains called",
        "intro_mode": "linear",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#FFFFFF",
    }

    gb._maybe_add_text_elements_snapshot(result)

    assert "text_elements" not in result, (
        "text_elements must NOT be added when _TEXT_ELEMENTS_ENABLED=False"
    )


def test_snapshot_helper_no_op_on_failed_render(monkeypatch):
    """_maybe_add_text_elements_snapshot is a no-op when ok=False."""
    monkeypatch.setattr(gb, "_TEXT_ELEMENTS_ENABLED", True)

    result = {
        "ok": False,
        "intro_text": "the mountains called",
    }
    gb._maybe_add_text_elements_snapshot(result)
    assert "text_elements" not in result


def test_snapshot_helper_adds_text_elements_when_enabled(monkeypatch):
    """Sanity: snapshot IS added (as a list of dicts) when enabled and ok."""
    monkeypatch.setattr(gb, "_TEXT_ELEMENTS_ENABLED", True)

    result = {
        "ok": True,
        "variant_id": "original_text",
        "text_mode": "agent_text",
        "intro_text": "golden hour",
        "intro_mode": "linear",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#FFFFFF",
    }
    gb._maybe_add_text_elements_snapshot(result)

    # intro_text is set → text_elements_for_variant should produce at least one element.
    assert "text_elements" in result
    assert isinstance(result["text_elements"], list)
    # Each entry must be a dict (model_dump output).
    for entry in result["text_elements"]:
        assert isinstance(entry, dict)
        assert "text" in entry


def test_snapshot_helper_does_not_clobber_user_edited_elements(monkeypatch):
    """Snapshot synthesis must never overwrite the authoritative editor list."""
    monkeypatch.setattr(gb, "_TEXT_ELEMENTS_ENABLED", True)

    saved = [
        {
            "id": "qa-live",
            "text": "QA live text",
            "start_s": 0.0,
            "end_s": 2.0,
            "role": "generative_intro",
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.4,
        }
    ]
    result = {
        "ok": True,
        "variant_id": "original_text",
        "text_mode": "agent_text",
        "intro_text": "projected sequence text",
        "intro_mode": "sequence",
        "scenes": [{"text": "Projected", "start_s": 0.0, "end_s": 1.0}],
        "text_elements_user_edited": True,
        "text_elements": list(saved),
    }

    gb._maybe_add_text_elements_snapshot(result)

    assert result["text_elements"] == saved
    assert result["text_elements_user_edited"] is True


# ── Test 2: _reburn_text_on_base early branch respects kill switch ──────────────


def _patch_reburn_helpers(monkeypatch, tmp_path):
    """Minimal stubs so _reburn_text_on_base can run through the legacy path
    without real FFmpeg, GCS, or the Skia renderer."""
    import app.pipeline.probe as probe_mod
    import app.pipeline.text_overlay_skia as skia_mod
    import app.storage as storage

    # GCS download: copy a dummy 16-byte "base" into the local path.
    def _fake_download(gcs_path, local_path):
        with open(local_path, "wb") as fh:
            fh.write(b"\x00" * 16)

    monkeypatch.setattr(storage, "download_to_file", _fake_download)

    # Probe: short video, so reveal_window_s = MAX_INTRO_S = 3.0.
    monkeypatch.setattr(probe_mod, "probe_video", lambda _: types.SimpleNamespace(duration_s=6.0))

    # Skia burn: write a differently-sized file so the copy-through guard passes.
    burn_calls: list = []

    def _fake_burn(base, overlays, out, tmpdir, *, matte=None):
        burn_calls.append({"base": base, "overlays": overlays})
        with open(out, "wb") as fh:
            fh.write(b"\x01" * 24)

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn)

    # Upload: just return a signed URL without touching GCS.
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://cdn.example/{gcs}"
    )

    return burn_calls


def test_reburn_early_branch_not_taken_when_kill_switch_off(monkeypatch, tmp_path):
    """With _TEXT_ELEMENTS_ENABLED=False, text_elements_user_edited=True is ignored
    and _reburn_text_on_base falls through to the legacy overlay path."""
    monkeypatch.setattr(gb, "_TEXT_ELEMENTS_ENABLED", False)

    # Spy: if the early branch fires it will call coerce_text_elements.
    # We patch it to raise so any accidental early-branch execution is loud.
    import app.agents._schemas.text_element as te_mod

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            "coerce_text_elements was called — the early branch fired "
            "despite _TEXT_ELEMENTS_ENABLED=False"
        )

    monkeypatch.setattr(te_mod, "coerce_text_elements", _should_not_be_called)

    burn_calls = _patch_reburn_helpers(monkeypatch, tmp_path)

    # Minimal existing variant with text_elements_user_edited=True.
    existing = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "text_elements_user_edited": True,
        "text_elements": [
            {
                "id": "aabbcc",
                "text": "user authored",
                "start_s": 0.0,
                "end_s": 3.0,
                "role": "generative_intro",
                "position": "middle",
                "effect": "static",
            }
        ],
        "video_path": "generative-jobs/fake-job/final.mp4",
        "base_video_path": "generative-jobs/fake-job/base.mp4",
        "intro_text": "original intro",
        "intro_mode": "linear",
        "intro_layout": "linear",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#FFFFFF",
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "intro_highlight_word": None,
        "intro_word_roles": None,
    }

    # A minimal agent_text with text + highlight_word so the legacy burn path runs.
    agent_text = types.SimpleNamespace(
        text="original intro",
        highlight_word=None,
        word_roles=None,
    )
    agent_form = {"effect": "karaoke-line", "layout": "linear"}

    result = gb._reburn_text_on_base(
        job_id="fake-job",
        variant_id="song_text",
        existing=existing,
        agent_text=agent_text,
        agent_form=agent_form,
        text_mode="agent_text",
        resolved_style_set_id="default",
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=True,
        language="en",
    )

    # Verify the function completed via the LEGACY path, not the early branch.
    assert result.get("ok") is True, f"Expected ok=True, got: {result}"
    assert result.get("render_status") == "ready"
    # The early branch would have set text_elements_user_edited=True in the result;
    # the legacy path does NOT set it.
    # (It may be absent OR False — what matters is it didn't come from the early branch.)
    assert len(burn_calls) == 1, (
        "Expected exactly one burn call from the legacy path "
        f"(got {len(burn_calls)}). Early branch would have called burn too, "
        "but the spy on coerce_text_elements would have raised first."
    )
