"""KRI-455 follow-up: the iOS editor preview shows a phone Voiceover edit's
opening title.

The preview compiles text from the variant's ``text_elements``, never from the
pinned device recipe, so the worker keeps the title element beside the recipe
(``narrated_title_text_elements``) and the status route adds it, read-only, for
app builds that keep a ``read_only`` element out of every editing control. The
narrated editor still has no text lane: ``text_elements`` stays closed and no
Save may come to depend on the title.
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
# The first app build that keeps a read-only element out of the editor.
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


def test_status_shows_the_title_read_only_beside_the_captions(monkeypatch):
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
    assert title["source_params"]["read_only"] is True
    # The caption mirrors are untouched: no caption is shown twice.
    assert [row["text"] for row in captions] == ["First we pack", "then we drive"]
    assert all(_is_caption(row) for row in captions)
    # Read-only: the narrated editor still has no text lane.
    assert variant["editor_capabilities"]["text_elements"] is False
    assert "narrated_title_text_elements" not in variant


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


# --- Saves never depend on it ----------------------------------------------------------


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


def test_the_title_cannot_be_deleted_from_the_editor(monkeypatch):
    """It is not part of the editor's deletable baseline, so a hand-crafted
    deletion is stale rather than a text Save the narrated compiler refuses."""
    job, vid = titled_job(monkeypatch)
    [title] = _variant(job)["narrated_title_text_elements"]

    with pytest.raises(HTTPException) as exc:
        _save(job, vid, editor_state_version=1, deletions=[{"kind": "text", "id": title["id"]}])

    assert exc.value.status_code == 409
    assert _title_layers(job, vid)
