"""KRI-455 / KRI-465: the iOS editor shows a phone Voiceover edit's opening
title, and (KRI-465) lets the creator edit or delete it.

The preview compiles text from the variant's ``text_elements``, never from the
pinned device recipe, so the worker keeps the title element beside the recipe
(``narrated_title_text_elements``) and the status route adds it for app builds
that understand the element (protocol >= 4). The read path decides whether it is
editable: with ``phone_narrated_title_edits_enabled`` it comes out unmarked and
``text_elements`` opens; off, it is ``read_only`` and ``text_elements`` is closed
(KRI-455 behaviour, including for rows an older worker persisted with the
marker). A text Save recompiles the recipe's ``title-`` layers from the
submitted element and moves it back into ``narrated_title_text_elements``, the
title's one store.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
import tests.routes.test_phone_voiceover_editor_lanes as lanes
from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.pipeline.phone_narrated_plan import (
    compile_phone_narrated_plan,
    narrated_title_text_elements,
)
from app.services.client_protocol import set_client_protocol
from app.services.device_render import device_status
from app.services.editor_deletions import canonical_caption_rows
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job

_TITLE = "Cacio e pepe in 10 minutes"
_TITLE_END_S = 1.6
# The first app build that understands the title element (read-only or editable).
_TITLE_PROTOCOL = 4


def _titled_recipe():
    return compile_phone_narrated_plan(
        [
            lanes._step("s0", "c0", start_s=0.0, end_s=4.0, source_start_s=1.5),
            lanes._step("s1", "c1", start_s=4.0, end_s=8.0),
            lanes._step("s2", "c2", start_s=8.0, end_s=12.0),
        ],
        lanes._bindings(),
        lanes._narration(duration_s=12.0),
        voiceover_duration_s=12.0,
        mix=0.7,
        caption_cues=[*lanes._CUES, {"text": "then we drive", "start_s": 4.5, "end_s": 6.0}],
        opening_title=_TITLE,
        opening_title_end_s=_TITLE_END_S,
    )


def titled_job(monkeypatch, *, protocol: int | None = _TITLE_PROTOCOL):
    """A phone Narrated variant exactly as `_run_phone_narrated_job` leaves it
    when the creator confirmed a title."""
    monkeypatch.setattr(lanes, "_narrated_recipe", _titled_recipe)
    _enable(monkeypatch)
    set_client_protocol(protocol)
    titles = narrated_title_text_elements(_titled_recipe(), _TITLE, end_s=_TITLE_END_S)
    job, vid = voiceover_job(
        persisted={
            "caption_cues": [
                *lanes._CUES,
                {"text": "then we drive", "start_s": 4.5, "end_s": 6.0},
            ],
            "narrated_title_text_elements": titles,
        }
    )
    return job, vid


def _status(job) -> dict:
    (variant,) = gj._variants_for_response(job)
    return variant


def _is_caption(row: dict) -> bool:
    return (row.get("source_params") or {}).get("source") == CAPTION_CUE_SOURCE


def _title_layers(job, vid):
    recipe = device_status(job, vid).request.recipe
    return [layer for layer in recipe.text_layers if layer.id.startswith("title-")]


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _save(job, vid, **sections) -> dict:
    return gj.prepare_editor_commit(
        job,
        vid,
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(_variant(job)), **sections
        ),
        user_id="owner",
        plan_item_id="item",
    )


# --- read ------------------------------------------------------------------------------


def test_status_shows_the_editable_title_beside_the_captions(monkeypatch):
    job, _vid = titled_job(monkeypatch)

    variant = _status(job)

    title, *captions = variant["text_elements"]
    assert title["text"] == _TITLE
    assert (title["start_s"], title["end_s"]) == (0.0, pytest.approx(_TITLE_END_S))
    # Everything the preview needs to draw it where the phone exports it.
    assert title["font_family"] == "Playfair Display"
    assert (title["position"], title["x_frac"], title["y_frac"]) == ("custom", 0.5, 0.15)
    assert title["size_px"] == 120
    assert title["effect"] == "fade-in"
    assert "read_only" not in title["source_params"]
    # The caption mirrors are untouched: no caption is shown twice.
    assert [row["text"] for row in captions] == ["First we pack", "then we drive"]
    assert all(_is_caption(row) for row in captions)
    # KRI-465: the narrated editor has a text lane for the title now.
    assert variant["editor_capabilities"]["text_elements"] is True
    assert "narrated_title_text_elements" not in variant
    # The stored row is never rewritten by a read.
    assert "read_only" not in _variant(job)["narrated_title_text_elements"][0]["source_params"]


@pytest.mark.parametrize("persisted_marker", [False, True], ids=["new-row", "kri-455-row"])
def test_kill_switch_shows_the_title_read_only_and_closes_text(monkeypatch, persisted_marker):
    job, _vid = titled_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_narrated_title_edits_enabled", False)
    if persisted_marker:
        [row] = _variant(job)["narrated_title_text_elements"]
        row["source_params"] = {**row["source_params"], "read_only": True}

    variant = _status(job)

    title, *captions = variant["text_elements"]
    assert title["text"] == _TITLE
    assert title["source_params"]["read_only"] is True
    assert [row["text"] for row in captions] == ["First we pack", "then we drive"]
    assert variant["editor_capabilities"]["text_elements"] is False
    assert (
        variant["editor_capabilities"].get(
            "text_elements_reason", gj._PHONE_EDIT_UNSUPPORTED_REASON
        )
        == gj._PHONE_EDIT_UNSUPPORTED_REASON
    )


def test_a_row_persisted_with_the_marker_is_editable_while_the_flag_is_on(monkeypatch):
    job, _vid = titled_job(monkeypatch)
    [row] = _variant(job)["narrated_title_text_elements"]
    row["source_params"] = {**row["source_params"], "read_only": True}

    title = _status(job)["text_elements"][0]

    assert "read_only" not in title["source_params"]
    assert title["source_params"]["narrated_storyboard"] == "intro"
    assert _variant(job)["narrated_title_text_elements"][0]["source_params"]["read_only"] is True


@pytest.mark.parametrize("protocol", [3, None], ids=["protocol-3-build", "no-header"])
def test_older_app_builds_never_see_the_title(monkeypatch, protocol):
    """They would offer Delete on it, and that Save 422s."""
    job, _vid = titled_job(monkeypatch, protocol=protocol)

    variant = _status(job)

    assert [row["text"] for row in variant["text_elements"]] == [
        "First we pack",
        "then we drive",
    ]
    assert "narrated_title_text_elements" not in variant


def test_untitled_and_cloud_variants_read_exactly_as_before(monkeypatch):
    job, _vid = titled_job(monkeypatch)
    untitled = dict(_variant(job))
    untitled.pop("narrated_title_text_elements")
    cloud = {**_variant(job), "render_destination": "cloud"}

    assert gj._with_phone_narrated_title(untitled, [{"id": "a"}]) == [{"id": "a"}]
    assert gj._with_phone_narrated_title(cloud, [{"id": "a"}]) == [{"id": "a"}]


def test_a_title_already_in_the_list_is_not_repeated(monkeypatch):
    job, _vid = titled_job(monkeypatch)
    [title] = _variant(job)["narrated_title_text_elements"]

    assert gj._with_phone_narrated_title(_variant(job), [title]) == [title]


# --- Saves that don't touch the title keep it ------------------------------------------


def test_a_caption_save_keeps_the_title_and_still_shows_it(monkeypatch):
    job, vid = titled_job(monkeypatch)
    before = _title_layers(job, vid)

    _save(job, vid, caption_cues=[dict(lanes._CUES[0], text="First we pack light")])

    assert _title_layers(job, vid) == before
    assert _status(job)["text_elements"][0]["text"] == _TITLE


def test_deleting_a_caption_keeps_the_title(monkeypatch):
    """The app sends a caption deletion as `deletions` + the surviving cues; the
    server-side text baseline it materializes from carries no title."""
    job, vid = titled_job(monkeypatch)
    before = _title_layers(job, vid)
    cues = canonical_caption_rows(_variant(job)["caption_cues"])

    _save(
        job,
        vid,
        editor_state_version=1,
        deletions=[{"kind": "caption_cue", "id": cues[1]["id"]}],
        caption_cues=cues[:1],
    )

    assert _title_layers(job, vid) == before
    assert [row["text"] for row in _status(job)["text_elements"]] == [_TITLE, "First we pack"]


def test_a_cut_save_keeps_the_title(monkeypatch):
    from tests.routes.test_phone_voiceover_cut_edits import _slots

    job, vid = titled_job(monkeypatch)
    before = _title_layers(job, vid)
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0

    _save(job, vid, timeline_slots=slots)

    assert _title_layers(job, vid) == before
    assert _status(job)["text_elements"][0]["text"] == _TITLE


# --- editing the title (KRI-465) -------------------------------------------------------


def _document(job, **edits) -> list[dict]:
    """The `text_elements` the app sends: the status response's list (title +
    one caption mirror per cue), with ``edits`` applied to the title."""
    rows = [dict(row) for row in _status(job)["text_elements"]]
    rows[0].update(edits)
    return rows


def _mirrors(job) -> list[dict]:
    return [dict(row) for row in _status(job)["text_elements"] if _is_caption(row)]


def _no_text_store(job) -> None:
    assert "text_elements" not in _variant(job)
    assert "text_elements_user_edited" not in _variant(job)


def test_a_text_save_recompiles_the_title_from_the_edited_element(monkeypatch):
    from app.agents._schemas.text_element import TextElement

    job, vid = titled_job(monkeypatch)
    captions_before = device_status(job, vid).request.recipe.text_layers[1:]
    elements = _document(
        job, text="Pasta in ten", start_s=0.5, end_s=2.4, x_frac=0.4, y_frac=0.3, size_px=96
    )

    prep = _save(job, vid, text_elements=elements)

    assert prep["render_destination"] == "device"
    recipe = device_status(job, vid).request.recipe
    [layer] = _title_layers(job, vid)
    [stored] = _variant(job)["narrated_title_text_elements"]
    assert stored["text"] == "Pasta in ten"
    assert (stored["start_s"], stored["end_s"]) == (0.5, pytest.approx(2.4))
    assert (stored["x_frac"], stored["y_frac"], stored["size_px"]) == (0.4, 0.3, 96)
    assert [layer] == _compile_elements([TextElement.model_validate(stored)], recipe)
    assert " ".join(run.text for run in layer.runs) == "Pasta in ten"
    assert (layer.start, layer.end) == (0.5, pytest.approx(2.4))
    # Captions are the caption lane's: untouched.
    assert recipe.text_layers[1:] == captions_before
    assert recipe.text_layers[0] is not None and recipe.text_layers[0].id == "title-0"
    assert _variant(job)["render_status"] == "awaiting_device"
    _no_text_store(job)


def _compile_elements(elements, recipe):
    from app.pipeline.phone_narrated_plan import _compile_title_layers
    from app.pipeline.phone_recipe_shared import timeline_end_s

    video = next(track for track in recipe.tracks if track.id == "narrated")
    return _compile_title_layers(
        elements, canvas=recipe.canvas, timeline_duration_s=timeline_end_s(video.clips)
    )


def test_status_shows_the_edited_title_exactly_once(monkeypatch):
    job, vid = titled_job(monkeypatch)

    _save(job, vid, text_elements=_document(job, text="Pasta in ten"))

    texts = [row["text"] for row in _status(job)["text_elements"]]
    assert texts == ["Pasta in ten", "First we pack", "then we drive"]
    assert _status(job)["editor_capabilities"]["text_elements"] is True
    # A second, unrelated Save does not resurrect or duplicate it.
    _save(job, vid, caption_cues=[dict(lanes._CUES[0], text="First we pack light")])
    assert [row["text"] for row in _status(job)["text_elements"]].count("Pasta in ten") == 1


def test_an_unchanged_text_save_keeps_the_title_layers(monkeypatch):
    job, vid = titled_job(monkeypatch)
    before = _title_layers(job, vid)

    _save(job, vid, text_elements=_document(job))

    assert _title_layers(job, vid) == before
    _no_text_store(job)


@pytest.mark.parametrize("media_on", [False, True], ids=["media-off", "media-on"])
def test_deleting_the_title_removes_its_layers_and_its_row(monkeypatch, media_on):
    job, vid = titled_job(monkeypatch)
    # A text deletion materializes the (empty) visual-block lane beside it, which
    # the generic validator only accepts while the Visuals lane is on.
    monkeypatch.setattr(gj.settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", media_on)
    captions_before = device_status(job, vid).request.recipe.text_layers[1:]

    _save(
        job,
        vid,
        editor_state_version=1,
        deletions=[{"kind": "text", "id": "narrated-title"}],
        text_elements=_mirrors(job),
    )

    assert not _title_layers(job, vid)
    assert device_status(job, vid).request.recipe.text_layers == captions_before
    assert "narrated_title_text_elements" not in _variant(job)
    assert [row["text"] for row in _status(job)["text_elements"]] == [
        "First we pack",
        "then we drive",
    ]
    _no_text_store(job)


def test_a_caption_save_after_a_title_edit_keeps_the_edited_title(monkeypatch):
    job, vid = titled_job(monkeypatch)
    _save(job, vid, text_elements=_document(job, text="Pasta in ten", end_s=2.0))
    edited = _title_layers(job, vid)

    _save(job, vid, caption_cues=[dict(lanes._CUES[0], text="First we pack light")])

    assert _title_layers(job, vid) == edited
    assert _status(job)["text_elements"][0]["text"] == "Pasta in ten"


def test_a_cut_save_after_a_title_edit_keeps_the_edited_title(monkeypatch):
    from tests.routes.test_phone_voiceover_cut_edits import _slots

    job, vid = titled_job(monkeypatch)
    _save(job, vid, text_elements=_document(job, text="Pasta in ten", end_s=2.0))
    edited = _title_layers(job, vid)
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0

    _save(job, vid, timeline_slots=slots)

    assert _title_layers(job, vid) == edited
    assert _status(job)["text_elements"][0]["text"] == "Pasta in ten"


def test_a_save_adding_a_second_authored_element_compiles_title_1(monkeypatch):
    job, vid = titled_job(monkeypatch)
    extra = {
        "id": "added-1",
        "text": "Part two",
        "start_s": 3.0,
        "end_s": 5.0,
        "role": "generative_intro",
        "position": "middle",
        "effect": "fade-in",
    }

    _save(job, vid, text_elements=[*_document(job), extra])

    assert [layer.id for layer in _title_layers(job, vid)] == ["title-0", "title-1"]
    assert [row["text"] for row in _variant(job)["narrated_title_text_elements"]] == [
        _TITLE,
        "Part two",
    ]
    assert [row["text"] for row in _status(job)["text_elements"]][:2] == [_TITLE, "Part two"]
    _no_text_store(job)


def test_a_long_word_style_document_is_not_capped_at_fifty(monkeypatch):
    """The document carries one caption mirror per cue, so a word-style voiceover
    passes the cloud's 50-element cap with no new text of its own."""
    cues = [{"text": f"w{i}", "start_s": i * 0.1, "end_s": i * 0.1 + 0.09} for i in range(60)]
    job, vid = titled_job(monkeypatch)
    _variant(job)["caption_cues"] = cues

    elements = _document(job)

    assert len(elements) > 50
    _save(job, vid, text_elements=elements)
    assert len(_title_layers(job, vid)) == 1


def test_a_title_edit_that_ends_past_the_video_is_clamped(monkeypatch):
    job, vid = titled_job(monkeypatch)

    _save(job, vid, text_elements=_document(job, start_s=11.0, end_s=14.0))

    recipe = device_status(job, vid).request.recipe
    [layer] = _title_layers(job, vid)
    assert layer.end <= recipe.duration


def test_kill_switch_refuses_a_text_save_but_not_a_caption_save(monkeypatch):
    job, vid = titled_job(monkeypatch)
    elements = _document(job, text="Pasta in ten")
    monkeypatch.setattr(gj.settings, "phone_narrated_title_edits_enabled", False)
    before = _title_layers(job, vid)

    with pytest.raises(HTTPException) as exc:
        _save(job, vid, text_elements=elements)

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "unsupported_phone_edit"
    assert _title_layers(job, vid) == before
    # The caption lane is unaffected.
    _save(job, vid, caption_cues=[dict(lanes._CUES[0], text="First we pack light")])
    assert _title_layers(job, vid) == before


def test_the_title_edit_save_needs_no_app_build_header(monkeypatch):
    """Save is judged on the server gates alone (chat edits carry no header)."""
    job, vid = titled_job(monkeypatch)
    elements = _document(job, text="Pasta in ten")
    set_client_protocol(None)

    _save(job, vid, text_elements=elements)

    assert " ".join(run.text for run in _title_layers(job, vid)[0].runs) == "Pasta in ten"


def test_a_montage_voiceover_still_refuses_text_elements(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")

    with pytest.raises(HTTPException) as exc:
        _save(job, vid, text_elements=[])

    assert exc.value.status_code == 422
