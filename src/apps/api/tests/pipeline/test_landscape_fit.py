"""Tests for the landscape-fit per-clip output_fit resolution helpers.

Guards:
  1. _is_landscape — classification via real dims, fallback to aspect_ratio.
  2. resolve_output_fit — the public per-clip decision helper.
  3. _plan_slots landscape_fit integration — per-slot output_fit in template_orchestrate.
  4. build_generative_job all_candidates — stash / omit discipline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.reframe import _is_landscape, resolve_output_fit

# ── 1. _is_landscape ────────────────────────────────────────────────────────


class _Probe:
    """Minimal probe stand-in with width/height/aspect_ratio."""

    def __init__(self, width: int, height: int, aspect_ratio: str = "other"):
        self.width = width
        self.height = height
        self.aspect_ratio = aspect_ratio


def test_is_landscape_standard_16_9():
    assert _is_landscape(_Probe(1920, 1080)) is True


def test_is_landscape_portrait():
    assert _is_landscape(_Probe(1080, 1920)) is False


def test_is_landscape_square():
    assert _is_landscape(_Probe(1080, 1080)) is False


def test_is_landscape_ultrawide():
    assert _is_landscape(_Probe(2560, 1080)) is True


def test_is_landscape_fallback_aspect_16_9():
    """When dims are absent the helper falls back to aspect_ratio == '16:9'."""
    probe = MagicMock(spec=[])
    probe.aspect_ratio = "16:9"
    assert _is_landscape(probe) is True


def test_is_landscape_fallback_aspect_9_16():
    probe = MagicMock(spec=[])
    probe.aspect_ratio = "9:16"
    assert _is_landscape(probe) is False


def test_is_landscape_none_probe():
    assert _is_landscape(None) is False


def test_is_landscape_zero_dims_fallback_to_aspect_ratio():
    """When dims are present but zero, the helper falls back to aspect_ratio."""
    assert _is_landscape(_Probe(0, 0, "16:9")) is True
    assert _is_landscape(_Probe(0, 0, "other")) is False


# ── 2. resolve_output_fit ────────────────────────────────────────────────────


def test_resolve_landscape_fit_returns_letterbox_black():
    probe = _Probe(1920, 1080, "16:9")
    assert resolve_output_fit(probe, landscape_fit="fit") == "letterbox_black"


def test_resolve_landscape_fill_returns_crop():
    probe = _Probe(1920, 1080, "16:9")
    assert resolve_output_fit(probe, landscape_fit="fill") == "crop"


def test_resolve_portrait_fit_stays_crop():
    """Portrait clips must crop regardless of landscape_fit."""
    probe = _Probe(1080, 1920, "9:16")
    assert resolve_output_fit(probe, landscape_fit="fit") == "crop"


def test_resolve_square_fit_stays_crop():
    probe = _Probe(1080, 1080, "other")
    assert resolve_output_fit(probe, landscape_fit="fit") == "crop"


def test_resolve_ultrawide_fit_returns_letterbox_black():
    """Ultrawide (aspect_ratio='other' but width > height) also letterboxes on 'fit'."""
    probe = _Probe(2560, 1080, "other")
    assert resolve_output_fit(probe, landscape_fit="fit") == "letterbox_black"


def test_resolve_default_landscape_fit_is_fill():
    """Default landscape_fit must be 'fill' (byte-identical for callers that omit it)."""
    probe = _Probe(1920, 1080, "16:9")
    assert resolve_output_fit(probe) == "crop"  # landscape_fit defaults to "fill"


def test_resolve_custom_default_fit_propagates():
    probe = _Probe(1080, 1920, "9:16")  # portrait — landscape_fit ignored
    assert resolve_output_fit(probe, landscape_fit="fit", default_fit="letterbox") == "letterbox"


def test_resolve_none_probe_fallback():
    """None probe → _is_landscape is False → default_fit returned."""
    assert resolve_output_fit(None, landscape_fit="fit") == "crop"


# ── 3. _plan_slots landscape_fit integration ─────────────────────────────────


def _make_step(clip_id: str, duration_s: float = 2.0) -> object:
    slot = {"duration_s": duration_s, "position": 0, "locked": False}
    step = MagicMock()
    step.clip_id = clip_id
    step.slot = slot
    return step


def _make_probe(width: int, height: int) -> object:
    p = MagicMock()
    p.width = width
    p.height = height
    p.aspect_ratio = "16:9" if width > height else "9:16"
    p.has_audio = True
    p.duration_s = 5.0
    p.color_transfer = ""
    p.color_trc = "bt709"
    return p


@pytest.mark.parametrize(
    "width, height, landscape_fit, expected_fit",
    [
        (1920, 1080, "fit", "letterbox_black"),  # landscape + fit → bars
        (1920, 1080, "fill", "crop"),  # landscape + fill → crop
        (1080, 1920, "fit", "crop"),  # portrait + fit → crop (unaffected)
        (1080, 1080, "fit", "crop"),  # square + fit → crop (unaffected)
    ],
)
def test_plan_slots_landscape_fit(width, height, landscape_fit, expected_fit):
    from app.tasks.template_orchestrate import _plan_slots

    clip_id = "clip_a"
    step = _make_step(clip_id, duration_s=2.0)
    probe = _make_probe(width, height)
    clip_probe_map = {"/local/clip_a.mp4": probe}
    clip_id_to_local = {clip_id: "/local/clip_a.mp4"}

    with patch("os.path.exists", return_value=True):
        plans, _, _ = _plan_slots(
            steps=[step],
            clip_id_to_local=clip_id_to_local,
            clip_probe_map=clip_probe_map,
            beats=[],
            clip_metas=None,
            global_color_grade="none",
            tmpdir="/tmp",
            output_fit="crop",
            landscape_fit=landscape_fit,
        )

    assert plans[0].output_fit == expected_fit


def test_plan_slots_locked_always_letterbox():
    """is_locked=True forces letterbox_black regardless of landscape_fit."""
    from app.tasks.template_orchestrate import _plan_slots

    clip_id = "clip_a"
    step = _make_step(clip_id, duration_s=2.0)
    step.slot["locked"] = True  # type: ignore[index]
    probe = _make_probe(1080, 1920)  # portrait — would normally crop
    clip_probe_map = {"/local/clip_a.mp4": probe}
    clip_id_to_local = {clip_id: "/local/clip_a.mp4"}

    with patch("os.path.exists", return_value=True):
        plans, _, _ = _plan_slots(
            steps=[step],
            clip_id_to_local=clip_id_to_local,
            clip_probe_map=clip_probe_map,
            beats=[],
            clip_metas=None,
            global_color_grade="none",
            tmpdir="/tmp",
            output_fit="crop",
            landscape_fit="fill",  # fill preference — but locked overrides
        )

    assert plans[0].output_fit == "letterbox_black"


# ── 4. build_generative_job all_candidates discipline ────────────────────────


def test_build_generative_job_stashes_fit():
    """landscape_fit='fit' must appear in all_candidates."""
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["slot-uploads/test.mp4"],
        landscape_fit="fit",
    )
    assert job.all_candidates.get("landscape_fit") == "fit"


def test_build_generative_job_omits_fill():
    """landscape_fit='fill' (the default) must be omitted for byte-identity."""
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["slot-uploads/test.mp4"],
        landscape_fit="fill",
    )
    assert "landscape_fit" not in job.all_candidates


def test_build_generative_job_default_omits_key():
    """Default (no landscape_fit arg) must also omit the key."""
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["slot-uploads/test.mp4"],
    )
    assert "landscape_fit" not in job.all_candidates


# ── 5. narrated_assembler probe-failure fallback ─────────────────────────────


def test_narrated_assembler_probe_failure_falls_back_to_crop():
    """probe_video() exception → probe=None → resolve_output_fit returns 'crop'."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.pipeline.narrated_assembler import assemble_narrated

    clip = SimpleNamespace(
        step_id="s1",
        clip_path="/fake/clip.mp4",
        source_start_s=0.0,
    )
    timing = SimpleNamespace(step_id="s1", start_s=0.0, end_s=2.0)

    captured: list[object] = []

    def _fake_run(spec: object, _output_path: str = "", **_kw) -> None:
        captured.append(spec)

    with (
        patch("app.pipeline.narrated_assembler.probe_video", side_effect=RuntimeError("no probe")),
        patch("app.pipeline.narrated_assembler.run_single_pass", side_effect=_fake_run),
        patch("app.pipeline.narrated_assembler._mix_user_voiceover", return_value=None),
    ):
        try:
            assemble_narrated(
                clip_assignments=[clip],
                step_timings=[timing],
                voiceover_local_path="/fake/vo.m4a",
                output_path="/fake/out.mp4",
                tmpdir="/tmp/test_narrated",
                landscape_fit="fit",
            )
        except Exception:  # mix_audio stub → output path missing, etc.
            pass

    assert captured, "run_single_pass was not called"
    spec = captured[0]
    assert len(spec.inputs) == 1
    # probe failure → _is_landscape(None) = False → default_fit = "crop"
    assert spec.inputs[0].output_fit == "crop"


def test_narrated_assembler_landscape_probe_letterboxes():
    """Valid landscape probe + landscape_fit='fit' → output_fit='letterbox_black'."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from app.pipeline.narrated_assembler import assemble_narrated

    clip = SimpleNamespace(
        step_id="s1",
        clip_path="/fake/clip.mp4",
        source_start_s=0.0,
    )
    timing = SimpleNamespace(step_id="s1", start_s=0.0, end_s=2.0)

    fake_probe = MagicMock()
    fake_probe.width = 1920
    fake_probe.height = 1080
    fake_probe.aspect_ratio = "16:9"

    captured: list[object] = []

    with (
        patch("app.pipeline.narrated_assembler.probe_video", return_value=fake_probe),
        patch(
            "app.pipeline.narrated_assembler.run_single_pass",
            side_effect=lambda s, p="", **_kw: captured.append(s),
        ),
        patch("app.pipeline.narrated_assembler._mix_user_voiceover", return_value=None),
    ):
        try:
            assemble_narrated(
                clip_assignments=[clip],
                step_timings=[timing],
                voiceover_local_path="/fake/vo.m4a",
                output_path="/fake/out.mp4",
                tmpdir="/tmp/test_narrated_lb",
                landscape_fit="fit",
            )
        except Exception:
            pass

    assert captured, "run_single_pass was not called"
    spec = captured[0]
    assert spec.inputs[0].output_fit == "letterbox_black"
