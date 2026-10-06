"""KRI-467: the opening title of a phone Talking (subtitled) edit is an
ordinary, editable text row.

The worker writes it to the variant's ``text_elements``; the status route
serves it beside the caption mirrors with the text lane open; a text Save
recompiles it into the device recipe (text, timing, position, delete, new
creator text); and every other Save (captions, sound effects, a cut variant)
keeps it. ``PHONE_SUBTITLED_TITLE_ENABLED=false`` closes the lane again but
never drops a title already on a variant.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.kria.device_render import make_device_request
from app.kria.recipes import Canvas
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_subtitled_title import TALKING_TITLE_ELEMENT_ID, talking_title_element
from app.pipeline.silence_cut import CutPlan, Removal
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor

_TITLE = "3 sourdough mistakes"
_CANVAS = Canvas(width=1080, height=1920)


def _title_row(*, timeline_duration_s: float) -> dict:
    row = talking_title_element(
        _TITLE,
        duration_s=2.0,
        first_word_end_s=None,
        timeline_duration_s=timeline_duration_s,
        canvas=_CANVAS,
    )
    assert row is not None
    return row


def titled_job(monkeypatch, *, cut: bool = False, title: bool = True, cues: list | None = None):
    """A phone Talking variant exactly as `_run_phone_subtitled_job` leaves it
    when the creator confirmed a hook title (``cut``: with a speech-cleanup cut
    of [4, 5) s, so the speaker plays 9 s)."""
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    monkeypatch.setattr(gj.settings, "phone_subtitled_title_enabled", True)
    bindings = (_binding(duration_s=10.0),)
    cut_plan = (
        CutPlan(
            keep_segments=[(0.0, 4.0), (5.0, 10.0)],
            removed=[Removal(start_s=4.0, end_s=5.0, reason="test")],
            time_saved_s=1.0,
        )
        if cut
        else None
    )
    rows = [_title_row(timeline_duration_s=9.0 if cut else 10.0)] if title else []
    cues = _CUES if cues is None else cues
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=cues,
        cut_plan=cut_plan,
        text_elements=rows,
        text_elements_user_edited=bool(rows),
    )
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "duration_s": recipe.duration,
        "caption_cues": cues,
        "voiceover_caption_style": "sentence",
    }
    if rows:
        variant |= {
            "text_elements": rows,
            "text_elements_user_edited": True,
            "text_elements_materialized_from": "opening_title",
        }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        all_candidates={},
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [],
            "variants": [variant],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _status(job) -> dict:
    (variant,) = gj._variants_for_response(job)
    return variant


def _save(job, **sections) -> dict:
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(_variant(job)), **sections
        ),
        user_id="owner",
        plan_item_id="item",
    )


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _text_layers(job):
    return [layer for layer in _recipe(job).text_layers if layer.id.startswith("text-")]


def _layer_text(layer) -> str:
    return " ".join(run.text for run in layer.runs)


def _is_caption(row: dict) -> bool:
    return (row.get("source_params") or {}).get("source") == CAPTION_CUE_SOURCE


def _served_rows(job) -> list[dict]:
    """What the app Saves back: every served row, captions mirrors included."""
    return copy.deepcopy(_status(job)["text_elements"])


# --- read --------------------------------------------------------------------------------


def test_status_serves_the_title_as_an_editable_row_beside_the_captions(monkeypatch):
    job = titled_job(monkeypatch)

    variant = _status(job)

    title, *captions = variant["text_elements"]
    assert title["id"] == TALKING_TITLE_ELEMENT_ID
    assert title["text"] == _TITLE
    assert (title["start_s"], title["end_s"]) == (0.0, 2.0)
    assert "read_only" not in (title.get("source_params") or {})
    assert [row["text"] for row in captions] == [cue["text"] for cue in _CUES]
    assert all(_is_caption(row) for row in captions)
    assert variant["editor_capabilities"]["text_elements"] is True


def test_the_text_lane_closes_when_switched_off_but_the_title_still_shows(monkeypatch):
    job = titled_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_subtitled_title_enabled", False)

    variant = _status(job)

    assert variant["editor_capabilities"]["text_elements"] is False
    assert variant["text_elements"][0]["text"] == _TITLE


def test_an_untitled_talking_edit_opens_the_text_lane_with_only_its_captions(monkeypatch):
    job = titled_job(monkeypatch, title=False)

    variant = _status(job)

    assert variant["editor_capabilities"]["text_elements"] is True
    assert all(_is_caption(row) for row in variant["text_elements"])
    assert _text_layers(job) == []


# --- text Saves --------------------------------------------------------------------------


def test_editing_the_title_text_timing_and_position_recompiles_it(monkeypatch):
    job = titled_job(monkeypatch)
    [caption_layers_before] = [
        [layer for layer in _recipe(job).text_layers if layer.id.startswith("caption-")]
    ]
    rows = _served_rows(job)
    rows[0].update(text="Stop making these 3 mistakes", end_s=1.5, y_frac=0.5)

    result = _save(job, text_elements=rows)

    assert result["render_destination"] == "device"
    assert device_status(job, "subtitled").request.identity.recipe_revision == 2
    [title] = _text_layers(job)
    assert _layer_text(title) == "Stop making these 3 mistakes"
    assert (title.start, title.end) == (0.0, 1.5)
    assert title.anchor_y > 0.4 * 1920
    # Captions are untouched, drawn once, and on top of the title.
    captions = [layer for layer in _recipe(job).text_layers if layer.id.startswith("caption-")]
    assert captions == caption_layers_before
    assert [layer.id for layer in _recipe(job).text_layers][0] == "text-0"
    [saved_title] = [row for row in _variant(job)["text_elements"] if not _is_caption(row)]
    assert saved_title["text"] == "Stop making these 3 mistakes"


def test_saving_the_served_rows_unchanged_never_draws_a_caption_twice(monkeypatch):
    job = titled_job(monkeypatch)
    before = _text_layers(job)

    _save(job, text_elements=_served_rows(job))

    assert _text_layers(job) == before
    assert len(_recipe(job).text_layers) == len(before) + len(_CUES)


def test_creator_text_added_in_the_editor_renders_next_to_the_title(monkeypatch):
    job = titled_job(monkeypatch)
    rows = _served_rows(job)
    rows.append(
        {
            "id": "creator-text-1",
            "text": "Mistake #1",
            "start_s": 3.0,
            "end_s": 5.0,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "effect": "static",
        }
    )

    _save(job, text_elements=rows)

    assert [_layer_text(layer) for layer in _text_layers(job)] == [_TITLE, "Mistake #1"]


def test_deleting_the_title_removes_it_from_the_render(monkeypatch):
    """The app sends a text deletion as `deletions` + the surviving text lane.
    (A deletion also materializes the empty Visual-block lane, which needs
    `VISUAL_BLOCKS_ENABLED`, on in prod.)"""
    job = titled_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "visual_blocks_enabled", True)
    survivors = [row for row in _served_rows(job) if row["id"] != TALKING_TITLE_ELEMENT_ID]

    _save(
        job,
        editor_state_version=1,
        deletions=[{"kind": "text", "id": TALKING_TITLE_ELEMENT_ID}],
        text_elements=survivors,
    )

    assert _text_layers(job) == []
    assert [row["text"] for row in _status(job)["text_elements"]] == [cue["text"] for cue in _CUES]


def test_a_long_talk_with_more_caption_lines_than_the_text_cap_still_saves_its_title(monkeypatch):
    """The app echoes every caption mirror back in the text lane; a 2-3 minute
    talk has more cues than the 50-row text cap. Only real text counts."""
    cues = [
        {"text": f"Line {index}", "start_s": index * 0.15, "end_s": index * 0.15 + 0.14}
        for index in range(60)
    ]
    job = titled_job(monkeypatch, cues=cues)
    rows = _served_rows(job)
    assert len(rows) == 61
    rows[0]["text"] = "Stop making these 3 mistakes"

    _save(job, text_elements=rows)

    assert [_layer_text(layer) for layer in _text_layers(job)] == ["Stop making these 3 mistakes"]


def test_a_text_save_never_persists_caption_mirrors(monkeypatch):
    """A persisted mirror would outlive a later caption edit: deleting every
    caption must not bring the old lines back into the editor's text lane."""
    job = titled_job(monkeypatch)
    _save(job, text_elements=_served_rows(job))
    assert [row["id"] for row in _variant(job)["text_elements"]] == [TALKING_TITLE_ELEMENT_ID]

    _save(job, caption_cues=[])

    assert [row["text"] for row in _status(job)["text_elements"]] == [_TITLE]
    assert [layer.id for layer in _recipe(job).text_layers] == ["text-0"]


def test_a_text_save_is_refused_when_switched_off(monkeypatch):
    job = titled_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_subtitled_title_enabled", False)
    rows = _served_rows(job)
    rows[0]["text"] = "Something else"

    with pytest.raises(HTTPException) as error:
        _save(job, text_elements=rows)

    assert error.value.status_code in (404, 422)
    assert [_layer_text(layer) for layer in _text_layers(job)] == [_TITLE]


# --- every other Save keeps the title ----------------------------------------------------


def test_a_caption_save_keeps_the_title(monkeypatch):
    job = titled_job(monkeypatch)
    before = _text_layers(job)

    _save(job, caption_cues=[dict(_CUES[0], text="Hello everybody"), _CUES[1]])

    assert _text_layers(job) == before
    assert _status(job)["text_elements"][0]["text"] == _TITLE


def test_a_caption_save_keeps_the_title_even_when_switched_off(monkeypatch):
    """The kill switch stops new titles and closes the lane; it never strips
    a title the creator already approved."""
    job = titled_job(monkeypatch)
    before = _text_layers(job)
    monkeypatch.setattr(gj.settings, "phone_subtitled_title_enabled", False)

    _save(job, caption_cues=[dict(_CUES[0], text="Hello everybody"), _CUES[1]])

    assert _text_layers(job) == before


def test_a_caption_style_save_keeps_the_title(monkeypatch):
    job = titled_job(monkeypatch)
    before = _text_layers(job)

    _save(job, caption_meta=gj.EditorCommitCaptionMeta(style="word", size_px=96))

    assert _text_layers(job) == before


def test_turning_captions_off_keeps_only_the_title(monkeypatch):
    job = titled_job(monkeypatch)
    before = _text_layers(job)

    _save(job, caption_meta=gj.EditorCommitCaptionMeta(enabled=False))

    assert _recipe(job).text_layers == before


def test_a_cut_variant_keeps_its_cut_and_the_title_window(monkeypatch):
    job = titled_job(monkeypatch, cut=True)

    _save(job, caption_cues=[dict(_CUES[0], text="Hello everybody"), _CUES[1]])

    recipe = _recipe(job)
    [main] = [track for track in recipe.tracks if track.id == "subtitled"]
    assert [(clip.source_start, clip.source_duration) for clip in main.clips] == [
        (0.0, 4.0),
        (5.0, 5.0),
    ]
    [title] = _text_layers(job)
    assert (title.start, title.end) == (0.0, 2.0)
