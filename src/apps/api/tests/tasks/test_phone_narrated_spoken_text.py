from __future__ import annotations

import copy

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.transcribe import Transcript
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated, _words


def _asks():
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r2",
                kind="text",
                scope="global",
                literal="1 · leave before 11",
                facts={"triggers": ["1", "leave before 11"]},
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="global",
                literal="2 · you're all amigos",
                facts={"triggers": ["2", "you're all amigos"]},
            ),
            BriefRequirement(
                id="r4",
                kind="text",
                scope="global",
                literal="3 · 20 minutes of darkness",
                facts={"triggers": ["3", "20 minutes of darkness"]},
            ),
        ],
    )


def _run_with_rules(monkeypatch, *, words, opening_title="N2 TITLE"):
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=words,
        voiceover_s=12.0,
        n_clips=1,
        extra_candidates={"creator_strategy": {"opening_title": opening_title}},
    )
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "authoredText",
            "variableSpeed",
        ],
    )
    binding = BriefBinding.create("n2-test", _asks()).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    return job


def test_narrated_voiceover_places_exact_three_literals_on_recorded_words(monkeypatch):
    words = _words(
        ("1", 0.0, 0.2),
        ("leave", 0.5, 0.7),
        ("before", 0.7, 0.9),
        ("11", 0.9, 1.1),
        ("2", 4.0, 4.2),
        ("you're", 4.5, 4.7),
        ("all", 4.7, 4.9),
        ("amigos", 4.9, 5.1),
        ("3", 8.0, 8.2),
        ("20", 8.5, 8.7),
        ("minutes", 8.7, 8.9),
        ("of", 8.9, 9.1),
        ("darkness", 9.1, 9.3),
    )
    job = _run_with_rules(monkeypatch, words=words)
    [variant] = job.assembly_plan["variants"]

    rows = variant["narrated_title_text_elements"]
    assert [row["text"] for row in rows] == [
        "N2 TITLE",
        "1 · leave before 11",
        "2 · you're all amigos",
        "3 · 20 minutes of darkness",
    ]
    assert [row["start_s"] for row in rows[1:]] == [0.5, 4.5, 8.5]
    assert [item["status"] for item in variant["spoken_text_placement"]["rows"]] == [
        "placed",
        "placed",
        "placed",
    ]
    recipe = device_status(job, "narrated").request.recipe
    assert [layer.id for layer in recipe.text_layers[:4]] == [
        "title-0",
        "title-1",
        "title-2",
        "title-3",
    ]


def test_narrated_missing_cue_is_receipted_without_dropping_other_rules(monkeypatch):
    words = _words(
        ("1", 0.0, 0.2),
        ("leave", 0.5, 0.7),
        ("before", 0.7, 0.9),
        ("11", 0.9, 1.1),
        ("2", 4.0, 4.2),
        ("you're", 4.5, 4.7),
        ("all", 4.7, 4.9),
        ("amigos", 4.9, 5.1),
    )
    job = _run_with_rules(monkeypatch, words=words)
    [variant] = job.assembly_plan["variants"]

    assert [row["text"] for row in variant["narrated_title_text_elements"]] == [
        "N2 TITLE",
        "1 · leave before 11",
        "2 · you're all amigos",
    ]
    assert [row["status"] for row in variant["spoken_text_placement"]["rows"]] == [
        "placed",
        "placed",
        "not_heard",
    ]


def test_narrated_rules_use_recorded_words_when_caption_text_is_translated(monkeypatch):
    raw_words = _words(
        ("1", 0.0, 0.2),
        ("leave", 0.5, 0.7),
        ("before", 0.7, 0.9),
        ("11", 0.9, 1.1),
        ("2", 4.0, 4.2),
        ("you're", 4.5, 4.7),
        ("all", 4.7, 4.9),
        ("amigos", 4.9, 5.1),
        ("3", 8.0, 8.2),
        ("20", 8.5, 8.7),
        ("minutes", 8.7, 8.9),
        ("of", 8.9, 9.1),
        ("darkness", 9.1, 9.3),
    )
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=raw_words,
        voiceover_s=12.0,
        n_clips=1,
        extra_candidates={"creator_strategy": {"opening_title": "N2 TITLE"}},
    )
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "authoredText",
            "variableSpeed",
        ],
    )
    binding = BriefBinding.create("n2-test", _asks()).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    monkeypatch.setattr(
        gb,
        "_narrated_caption_transcript",
        lambda *args, **kwargs: (
            Transcript(
                words=_words(("un", 0.0, 0.4), ("deux", 4.0, 4.4), ("trois", 8.0, 8.4)),
                language="fr",
            ),
            "fr",
        ),
    )

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    assert [row["text"] for row in variant["narrated_title_text_elements"]] == [
        "N2 TITLE",
        "1 · leave before 11",
        "2 · you're all amigos",
        "3 · 20 minutes of darkness",
    ]
    assert [row["status"] for row in variant["spoken_text_placement"]["rows"]] == [
        "placed",
        "placed",
        "placed",
    ]
    assert all("un" not in row["text"] for row in variant["narrated_title_text_elements"])


def test_narrated_spoken_text_gate_off_preserves_title_only(monkeypatch):
    words = _words(("1", 0.0, 0.2), ("leave", 0.5, 0.7), ("before", 0.7, 0.9), ("11", 0.9, 1.1))
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=words,
        n_clips=1,
        extra_candidates={"creator_strategy": {"opening_title": "N2 TITLE"}},
    )
    monkeypatch.setattr(gb.settings, "phone_narrated_spoken_text_enabled", False)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "authoredText",
            "variableSpeed",
        ],
    )
    binding = BriefBinding.create("n2-test", _asks()).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    assert [row["text"] for row in variant["narrated_title_text_elements"]] == ["N2 TITLE"]
    assert "spoken_text_placement" not in variant


def test_narrated_spoken_rows_save_roundtrip_preserves_audio_video_and_captions(monkeypatch):
    words = _words(
        ("1", 0.0, 0.2),
        ("leave", 0.5, 0.7),
        ("before", 0.7, 0.9),
        ("11", 0.9, 1.1),
        ("2", 4.0, 4.2),
        ("you're", 4.5, 4.7),
        ("all", 4.7, 4.9),
        ("amigos", 4.9, 5.1),
        ("3", 8.0, 8.2),
        ("20", 8.5, 8.7),
        ("minutes", 8.7, 8.9),
        ("of", 8.9, 9.1),
        ("darkness", 9.1, 9.3),
    )
    job = _run_with_rules(monkeypatch, words=words)
    recipe = device_status(job, "narrated").request.recipe
    [variant] = job.assembly_plan["variants"]
    persisted = copy.deepcopy(variant["narrated_title_text_elements"])

    from app.pipeline.phone_narrated_plan import replace_narrated_title

    edited = replace_narrated_title(recipe, persisted)
    assert edited.tracks == recipe.tracks
    assert edited.audio == recipe.audio
    assert [layer.id for layer in edited.text_layers if layer.id.startswith("caption")] == [
        layer.id for layer in recipe.text_layers if layer.id.startswith("caption")
    ]
    assert [layer.id for layer in edited.text_layers if layer.id.startswith("title-")] == [
        "title-0",
        "title-1",
        "title-2",
        "title-3",
    ]
    assert persisted == variant["narrated_title_text_elements"]
