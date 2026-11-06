"""Qendresa item 16 x Green 5 (S86): captions in the full-screen opening are judged over her take.

Green 5 (#2099) chooses an automatic caption colour against what the phone draws behind the
captions on a speaker-background edit: the background photo or video, with the take small in its
speaker frame. Item 16 draws the speaker's own take FULL-FRAME inside
`speaker_background.full_screen_windows`: no background and no frame there. Merged as is, the
captions of the opening are judged against a background that is not on screen.

This runs the real writer on Green 5's harness (real environment sampler; downloads faked from the
real Green 5 luminance grids), with the opening asked and open (flag, `speakerFrameWindows`,
protocol 41) and her corner frame top right, clear of the captions. The backgrounds are made dark:
over them alone a plain white caption reads well, while over her take (white wall, navy shirt) no
plain colour reaches 4.5:1. Every caption is judged
against what the phone draws at its time: her take full-frame in the opening, the background with
the take in its frame after it.

Ways it could fail (written before the fix):
1. The opening is judged against the background composite: white with no outline, which sits on her
   bright wall for the first seconds (the dark-background case below).
2. The opening is judged against her take but at the wrong time base (the spans, like the windows,
   are on the take's original timeline before the cut moves both).
3. No take frame can be read: the opening's captions are then chosen over nothing that is on
   screen; sampling must count as unavailable (fail-open: the safe white + outline), never fail.
4. No opening (item 16 off, an old build, nothing asked): frames, colour and trace are exactly
   Green 5's (its own tests stay green; the control below keeps the plain white).
5. The re-pick on the compiler's second pass (background registered after the environment was
   sampled) ignores the opening.
"""

from __future__ import annotations

import base64

from app.config import settings
from app.tasks import generative_build as gb
from tests.tasks import test_green45_caption_contrast as green5
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)

DARK = base64.b64encode(bytes([16]) * (green5.W * green5.H)).decode()


def _composite(recipe, at: float) -> list[int]:
    """What the phone draws at ``at``: her take full-frame inside a full-screen window, else
    Green 5's composite (background under that time, the take in its frame)."""
    spans = recipe.speaker_background.full_screen_windows or []
    if any(span.start <= at < span.end for span in spans):
        return list(green5._grid("take"))
    return GREEN5_COMPOSITE(recipe, at)


GREEN5_COMPOSITE = green5._composite


def _job(monkeypatch, tmp_path, *, opening: bool):
    for name in ("IMG_7120", "IMG_7122", "IMG_7125"):
        monkeypatch.setitem(green5.FIXTURE["grids"], name, DARK)
    monkeypatch.setattr(green5, "_composite", _composite)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", opening)
    job = green5._job(monkeypatch, tmp_path)
    # After the harness, which sets its own verified features.
    features = [f for f in settings.phone_render_verified_features if f != "speakerFrameWindows"]
    monkeypatch.setattr(
        settings, "phone_render_verified_features", [*features, "speakerFrameWindows"]
    )
    job.all_candidates["creator_client_protocol"] = 41
    background = job.all_candidates["creator_strategy"]["background_media"]
    # Her corner frame clear of the caption band (y 0.01-0.43), so after the opening only the
    # dark room sits behind the captions.
    background["speaker_frame"] = "small_top_right"
    if opening:
        background["full_screen_opening"] = True
    return job


def _opening(job) -> tuple[float, float]:
    spans = _recipe(job).speaker_background.full_screen_windows or []
    assert len(spans) == 1 and spans[0].start == 0.0, spans
    return spans[0].start, spans[0].end


def test_captions_in_the_opening_are_readable_over_her_take(monkeypatch, tmp_path):
    """1, 2, 5: the opening's captions are judged against her take, the rest against the room."""
    job = _job(monkeypatch, tmp_path, opening=True)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    start, end = _opening(job)
    judged = green5._caption_judgements(job)
    assert any(start <= at < end for at, _ratio in judged), "no caption inside the opening"
    worst = min(judged, key=lambda item: item[1])
    variant = job.assembly_plan["variants"][0]
    assert worst[1] >= 4.5, (
        f"caption at {worst[0]:.1f}s (opening {start:.1f}-{end:.1f}s) reaches {worst[1]:.2f}:1 "
        f"(text {variant['caption_text_color']}, outline {variant.get('caption_stroke_width')})"
    )


def test_without_the_opening_the_dark_room_keeps_a_plain_caption(monkeypatch, tmp_path):
    """4 (control): the same dark background with no opening is Green 5's own choice, readable
    with no outline. The outline above is the opening's doing, not a new default."""
    job = _job(monkeypatch, tmp_path, opening=False)
    gb._run_generative_job(str(job.id))
    assert not _recipe(job).speaker_background.full_screen_windows
    assert green5._look(job)["caption_stroke_width"] == 0
    assert min(ratio for _at, ratio in green5._caption_judgements(job)) >= 4.5


def test_an_unreadable_take_with_an_opening_degrades_to_the_safe_look(monkeypatch, tmp_path):
    """3: the take proxy cannot be read, so nothing says what is behind the opening's captions:
    sampling is unavailable (trace says so), the job renders, the captions stay readable."""
    import app.services.pipeline_trace as pipeline_trace
    import app.storage as storage

    job = _job(monkeypatch, tmp_path, opening=True)
    backgrounds_only = storage.download_to_file

    def no_take(remote, local, *args, **kwargs):
        if "analysis-proxy-c0" in str(remote):
            raise TimeoutError(remote)
        return backgrounds_only(remote, local, *args, **kwargs)

    events: list[tuple[str, dict]] = []
    record = pipeline_trace.record_pipeline_event

    def capture(stage, event, data=None):
        events.append((event, dict(data or {})))
        return record(stage, event, data)

    monkeypatch.setattr(storage, "download_to_file", no_take)
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", capture)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    _opening(job)
    failed = [data for event, data in events if event == "styling_background_sample_failed"]
    assert failed, events
    assert min(ratio for _at, ratio in green5._caption_judgements(job)) >= 4.5
