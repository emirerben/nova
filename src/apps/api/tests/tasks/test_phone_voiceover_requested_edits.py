"""Worker replay for creator-requested word text on a phone Voiceover edit.

The setup stubs storage/transcription transport only.  The worker, brief
binding, spoken-word helper, narrated compiler and device-request pinning all
run normally, so these tests cover the request-to-output boundary.
"""

from __future__ import annotations

import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated, _words


def _bind_word_text(snapshot: dict, job, *, trigger: str = "yirmi dakika") -> None:
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r6",
                kind="text",
                scope="global",
                literal="20 dk",
                description="Yirmi dakika dediğimde ekrana 20 dk yazısı gelsin",
                facts={"triggers": [trigger]},
            )
        ],
    )
    binding = BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = binding


def _replay_bound_alignment(monkeypatch) -> None:
    from app.pipeline.narrated_alignment import StepTiming

    # A bound brief normally asks the alignment agent to seat the B-roll first.
    # Replay that independent plan boundary so this worker test reaches the
    # real word-text compiler without making a model call.
    monkeypatch.setattr(gb.settings, "narrated_clip_alignment_enabled", True)
    monkeypatch.setattr(
        gb,
        "_narrated_clip_alignment_steps",
        lambda *, clip_ids, **_kwargs: (
            clip_ids,
            [
                StepTiming(
                    step_id=f"step-{index}",
                    start_s=index * 4.0,
                    end_s=(index + 1) * 4.0,
                    confidence=1.0,
                )
                for index in range(len(clip_ids))
            ],
            [],
        ),
    )


def _enable_requested_word_text(monkeypatch) -> None:
    monkeypatch.setattr(gb.settings, "phone_voiceover_spoken_text_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_narrated_title_edits_enabled", True)
    _replay_bound_alignment(monkeypatch)


def test_voiceover_word_text_reaches_recipe_variant_and_selected_camera_sound(monkeypatch) -> None:
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=_words(("Yirmi", 4.2, 4.4), ("dakika", 4.4, 4.7)),
        extra_candidates={
            "creator_strategy": {
                "opening_title": "Night boat rules",
                "montage_audio": {
                    "preserve_source_audio": True,
                    "source_media_ids": ["c1"],
                },
            }
        },
    )
    _enable_requested_word_text(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_source_audio_enabled", True)
    _bind_word_text(snapshot, job)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    assert "request_recovery" not in job.assembly_plan
    variant = job.assembly_plan["variants"][0]
    [receipt] = variant["spoken_text_placement"]["rows"]
    assert receipt["status"] == "placed"
    assert receipt["start_s"] == pytest.approx(4.2, abs=1 / 30)
    assert receipt["placement"]["face_protection"] == "fallback"
    rows = variant["narrated_title_text_elements"]
    assert any(row["id"] == "spoken-text-r6" and row["text"] == "20 dk" for row in rows)
    recipe = device_status(job, "narrated").request.recipe
    assert any(layer.id.startswith("title-") for layer in recipe.text_layers)
    video = next(track for track in recipe.tracks if track.id == "narrated")
    assert [clip.volume for clip in video.clips] == [0.0, 1.0, 0.0]
    assert variant["source_audio_media_ids"] == ["c1"]


@pytest.mark.parametrize(
    ("words", "expected_message"),
    [
        (_words(("başka", 4.2, 4.4)), "couldn't safely place"),
        (
            _words(
                ("Yirmi", 2.0, 2.2),
                ("dakika", 2.2, 2.5),
                ("Yirmi", 4.2, 4.4),
                ("dakika", 4.4, 4.7),
            ),
            "repeated trigger words",
        ),
    ],
)
def test_unheard_or_ambiguous_voiceover_text_recovers_before_device_pin(
    monkeypatch, words, expected_message
) -> None:
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch, spoken_words=words)
    _enable_requested_word_text(monkeypatch)
    _bind_word_text(snapshot, job)

    with pytest.raises(UnsupportedPhonePlan):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert "_device_render_v1" not in job.assembly_plan
    assert "request_recovery" in job.assembly_plan
    assert expected_message in job.assembly_plan["request_recovery"]["message"]


def test_voiceover_word_text_flag_off_keeps_the_existing_narrated_recipe(monkeypatch) -> None:
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, spoken_words=_words(("Yirmi", 4.2, 4.4), ("dakika", 4.4, 4.7))
    )
    _bind_word_text(snapshot, job)
    monkeypatch.setattr(gb.settings, "phone_voiceover_spoken_text_enabled", False)
    _replay_bound_alignment(monkeypatch)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    variant = job.assembly_plan["variants"][0]
    assert "spoken_text_placement" not in variant
    assert not any(
        row["id"] == "spoken-text-r6" for row in variant.get("narrated_title_text_elements", [])
    )
