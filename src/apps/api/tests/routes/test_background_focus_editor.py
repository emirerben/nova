"""Qendresa item 18: aiming a background (`focus`) over the HTTP editor surface.

Failure modes F1, F12-F15 (server half) of docs/reviews/background-focus-failure-modes.md:
the editor document shows the focus on the fill and on every window, a Save sets and keeps it, a
Save that does not touch the background keeps every focus the recipe already holds (the measured
ones too -- the measure lives only in the recipe), a build below protocol 42 is never silently
stripped (426) and never asked to update for a focus it merely echoes, a closed gate refuses a SET
only, a colour fill takes no focus, and the capability follows the gate.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.pipeline.phone_subtitled_lanes import SubtitledBackgroundFocus
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.phone_speaker_background import BACKGROUND_REFUSALS
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    lanes_from_recipe,
    project_phone_subtitled_editor_sections,
    variant_carries_background_focus,
)
from tests.kria.test_speaker_background_color import _open as _open_colour
from tests.pipeline.test_phone_subtitled_plan import _photo_visual
from tests.routes.test_phone_subtitled_editor_commit import _resave, save
from tests.routes.test_speaker_background_editor import (
    _background,
    _capability_variant,
    _document,
    _recipe,
    _variant,
    background_job,
)

PHOTO = _photo_visual()
SECOND = _photo_visual(
    media_id="photo-2", gcs_path="users/owner/plan/item/pool/photo-2.jpg", generation="78"
)
POOL = (PHOTO, SECOND)
CORNER = {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.80}
LEFT = {"x": 0.0, "y": 0.5, "zoom": 1.6}
RIGHT = {"x": 1.0, "y": 0.5, "zoom": 0.8}
FILL_FOCUS = {"x": 0.24, "y": 0.5, "zoom": 2.0}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open(monkeypatch, *, flag=True, token=True, windows=True) -> None:
    ours = {"speakerBackgroundWindows", "backgroundFocus"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if windows:
        features.append("speakerBackgroundWindows")
    if token:
        features.append("backgroundFocus")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    monkeypatch.setattr(settings, "background_focus_enabled", flag)


def _window(visual, start_s, end_s, **changes) -> dict:
    return {
        "media_id": visual.media_id,
        "kind": visual.kind,
        "source_start_s": 0.0,
        "start_s": start_s,
        "end_s": end_s,
    } | changes


def _aimed(**section) -> dict:
    """A document with a focus on the fill and on each of two windows, in the corner."""
    return (
        _document(PHOTO)
        | {"focus": FILL_FOCUS}
        | {
            "windows": [
                _window(PHOTO, 0.0, 3.0, focus=LEFT),
                _window(SECOND, 3.0, 6.0, focus=RIGHT),
            ],
            "speaker_frame": CORNER,
        }
        | section
    )


def focus_job(monkeypatch):
    """A phone Talking variant whose pinned recipe already holds a fill and two windows, aimed."""
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    save(job, speaker_background=_aimed())
    clear_request_context()
    return job


def _focus(entry) -> dict | None:
    return None if entry is None else entry.model_dump(mode="json")


def _layer_focus(job):
    layer = _recipe(job).speaker_background
    return _focus(layer.focus), [_focus(window.focus) for window in layer.windows or ()]


# --- the document shows it, the recipe carries it ---------------------------------------------


def test_save_sets_a_focus_on_the_fill_and_every_window_and_it_survives_the_round_trip(
    monkeypatch,
) -> None:
    job = focus_job(monkeypatch)
    assert _layer_focus(job) == (FILL_FOCUS, [LEFT, RIGHT])
    assert "backgroundFocus" in _recipe(job).required_capabilities
    document = _variant(job)["speaker_background"]
    assert document["focus"] == FILL_FOCUS
    assert [w["focus"] for w in document["windows"]] == [LEFT, RIGHT]
    assert variant_carries_background_focus(job.assembly_plan, _variant(job))
    # Projected again from the pinned recipe alone (a never-saved variant): the same document.
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == document
    # And the lanes rebuilt from the recipe hold the focus (F12).
    rebuilt = lanes_from_recipe(_recipe(job), visuals=POOL).background
    assert rebuilt.focus == SubtitledBackgroundFocus(**FILL_FOCUS)
    assert [w.focus for w in rebuilt.windows] == [
        SubtitledBackgroundFocus(**LEFT),
        SubtitledBackgroundFocus(**RIGHT),
    ]


def test_a_variant_without_a_focus_shows_no_key_anywhere(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    save(job, speaker_background=_document(PHOTO))
    assert "focus" not in json.dumps(_variant(job), default=str)
    assert "focus" not in json.dumps(_recipe(job).model_dump(mode="json")["speaker_background"])
    assert "backgroundFocus" not in _recipe(job).required_capabilities
    assert not variant_carries_background_focus(job.assembly_plan, _variant(job))


def test_the_save_can_change_and_clear_a_focus(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    set_client_protocol(42)
    moved = {"x": 0.5, "y": 0.5, "zoom": 1.25}
    _resave(
        job,
        speaker_background=_aimed(
            focus=moved, windows=[_window(PHOTO, 0.0, 3.0), _window(SECOND, 3.0, 6.0, focus=LEFT)]
        ),
    )
    assert _layer_focus(job) == (moved, [None, LEFT])
    _resave(
        job,
        speaker_background=_document(PHOTO)
        | {"windows": [_window(PHOTO, 0.0, 3.0)], "speaker_frame": CORNER},
    )
    assert _layer_focus(job) == (None, [None])
    assert "backgroundFocus" not in _recipe(job).required_capabilities
    assert "focus" not in json.dumps(_variant(job)["speaker_background"])


# --- F12/F14: a Save that does not touch the background keeps every focus ---------------------


def test_a_save_that_leaves_the_background_alone_keeps_every_focus(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    set_client_protocol(42)
    _resave(job, sound_effects=[])
    assert _layer_focus(job) == (FILL_FOCUS, [LEFT, RIGHT])
    assert _variant(job)["speaker_background"]["focus"] == FILL_FOCUS


def test_a_measured_focus_that_only_the_recipe_holds_survives_an_untouched_save(
    monkeypatch,
) -> None:
    """The first render writes the measured focus into the recipe; the persisted lanes (from an
    earlier Save or the worker) never held it. A Save of another lane must not lose it."""
    job = focus_job(monkeypatch)
    set_client_protocol(42)
    _resave(job, sound_effects=[])  # persists the lanes
    lanes = _variant(job)[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["lanes"]
    assert "focus" in json.dumps(lanes["background"])
    stripped = copy.deepcopy(lanes["background"])
    stripped.pop("focus", None)
    for window in stripped.get("windows") or ():
        window.pop("focus", None)
    lanes["background"] = stripped
    _resave(job, sound_effects=[])
    assert _layer_focus(job) == (FILL_FOCUS, [LEFT, RIGHT])
    assert _variant(job)["speaker_background"]["focus"] == FILL_FOCUS


def test_a_never_saved_recipe_focus_survives_the_first_untouched_save(monkeypatch) -> None:
    from app.services import phone_rollout

    set_client_protocol(42)
    # The first render wrote a focus into the recipe that no editor Save ever held.
    with monkeypatch.context() as early:
        early.setattr(
            phone_rollout, "background_focus_unavailable_reason", lambda **_k: None, raising=False
        )
        job = background_job(
            monkeypatch,
            background=_background(PHOTO, focus=SubtitledBackgroundFocus(**LEFT)),
            pinned=POOL,
        )
    _open(monkeypatch)
    assert _layer_focus(job)[0] == LEFT
    assert PHONE_SUBTITLED_EDITOR_LANES_FIELD not in _variant(job)
    save(job, sound_effects=[])
    assert _layer_focus(job)[0] == LEFT
    assert _variant(job)["speaker_background"]["focus"] == LEFT


# --- F1: an old build is told to update, never silently stripped ------------------------------


def test_save_from_build_38_on_a_variant_carrying_a_focus_is_426_and_commits_nothing(
    monkeypatch,
) -> None:
    job = focus_job(monkeypatch)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    snapshot = json.dumps(_variant(job), sort_keys=True, default=str)
    set_client_protocol(41)
    with pytest.raises(HTTPException) as refused:
        _resave(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "background_focus",
        "minimum_client_protocol": 42,
    }
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert json.dumps(_variant(job), sort_keys=True, default=str) == snapshot


def test_an_echo_of_the_stored_focus_is_not_a_set_and_a_set_from_38_is_426(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    request = gj.EditorCommitRequest(base_generation="x", speaker_background=_aimed())
    set_client_protocol(41)
    # Unchanged: the section validator does not refuse the echo (the carrying-variant 426 above
    # is what protects the old build).
    assert gj._validate_speaker_background_section(job, _variant(job), request) == (False, None)
    changed = _aimed(focus={"x": 0.5, "y": 0.5, "zoom": 1.1})
    with pytest.raises(HTTPException) as refused:
        gj._validate_speaker_background_section(
            job,
            _variant(job),
            gj.EditorCommitRequest(base_generation="x", speaker_background=changed),
        )
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "background_focus"
    assert refused.value.detail["minimum_client_protocol"] == 42


def test_a_build_below_39_setting_one_on_a_plain_variant_is_426(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_document(PHOTO) | {"focus": LEFT})
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "background_focus"
    assert "focus" not in json.dumps(_recipe(job).model_dump(mode="json")["speaker_background"])


def test_an_old_build_saving_a_variant_without_a_focus_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None


# --- a closed gate refuses a SET, never a clear -----------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"flag": False}, "focus_disabled"), ({"token": False}, "focus_token_not_verified")],
)
def test_setting_one_with_a_closed_gate_is_422(monkeypatch, kwargs, reason) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch, **kwargs)
    set_client_protocol(42)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=_document(PHOTO) | {"focus": LEFT})
    assert dark.value.status_code == 422
    assert dark.value.detail == {"code": "background_focus_disabled", "reason": reason}
    assert _recipe(job).speaker_background.focus is None
    save(job, speaker_background=_document(SECOND))  # the same Save without a focus is fine
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


def test_clearing_works_with_the_gate_closed(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    _resave(
        job,
        speaker_background=_document(PHOTO)
        | {"windows": [_window(PHOTO, 0.0, 3.0)], "speaker_frame": CORNER},
    )
    assert _layer_focus(job) == (None, [None])


def test_echoing_unchanged_focuses_is_not_a_set_so_a_closed_gate_does_not_refuse_it(
    monkeypatch,
) -> None:
    job = focus_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    _resave(job, speaker_background=_aimed() | {"media_id": SECOND.media_id})
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)
    # The kill switch is closed, so the compile (not the Save) leaves the focus out.
    assert _layer_focus(job) == (None, [None, None])


def test_a_colour_fill_with_a_focus_is_422_invalid(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open_colour(monkeypatch)
    _open(monkeypatch)
    set_client_protocol(42)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    with pytest.raises(HTTPException) as bad:
        save(job, speaker_background={"kind": "color", "color": "#112233", "focus": LEFT})
    assert bad.value.status_code == 422
    assert bad.value.detail == {"code": "background_focus_invalid"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    # A colour fill with a focused window is fine.
    save(
        job,
        speaker_background={
            "kind": "color",
            "color": "#112233",
            "windows": [_window(PHOTO, 0.0, 3.0, focus=LEFT)],
        },
    )
    assert _layer_focus(job) == (None, [LEFT])


@pytest.mark.parametrize(
    "focus",
    [
        {"x": 1.2, "y": 0.5, "zoom": 1.0},
        {"x": 0.5, "y": -0.1, "zoom": 1.0},
        {"x": 0.5, "y": 0.5, "zoom": 0.1},
        {"x": 0.5, "y": 0.5, "zoom": 4.5},
        {"x": 0.5, "y": 0.5},
        {"x": 0.5, "y": 0.5, "zoom": 1.0, "extra": 1},
    ],
)
def test_a_focus_out_of_bounds_is_a_schema_error(focus) -> None:
    with pytest.raises(ValueError):
        gj.EditorCommitSpeakerBackground(**_document(PHOTO), focus=focus)


def test_the_save_section_dumps_exactly_as_before_without_a_focus() -> None:
    section = gj.EditorCommitSpeakerBackground(**_document(PHOTO))
    assert section.model_dump() == _document(PHOTO)
    windowed = gj.EditorCommitSpeakerBackground(
        **_document(PHOTO), windows=[_window(SECOND, 0.0, 3.0)]
    )
    assert "focus" not in json.dumps(windowed.model_dump())
    aimed = gj.EditorCommitSpeakerBackground(**_aimed())
    assert aimed.model_dump()["focus"] == FILL_FOCUS


def test_the_refusal_codes_have_creator_words() -> None:
    for code in ("background_focus_disabled", "background_focus_invalid"):
        english, turkish = BACKGROUND_REFUSALS[code]
        assert english.startswith("Couldn't") and turkish


# --- capability --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "protocol", "expected"),
    [
        ({}, 42, True),
        ({}, 41, False),
        ({}, None, False),
        ({"flag": False}, 42, False),
        ({"token": False}, 42, False),
    ],
)
def test_the_capability_key_follows_the_gate(monkeypatch, kwargs, protocol, expected) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open(monkeypatch)
    variant = _variant(job)
    _open(monkeypatch, **kwargs)
    set_client_protocol(protocol)
    capabilities = gj._editor_capabilities(job, variant)
    present = capabilities.get("background_focus")
    assert (present == {"editable": True}) is expected
    assert (present is not None) is expected
    assert gj._background_focus_capability(job, variant) is expected


def test_a_cloud_variant_never_advertises_it(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    assert not gj._background_focus_capability(job, _capability_variant("cloud"))


def test_flag_off_leaves_the_capability_map_and_the_variant_dump_byte_identical(
    monkeypatch,
) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    set_client_protocol(42)
    monkeypatch.setattr(settings, "background_focus_enabled", False)
    off = json.dumps(gj._editor_capabilities(job, _variant(job)), sort_keys=True)
    assert "background_focus" not in off
    save(job, speaker_background=_document(PHOTO))
    assert "focus" not in json.dumps(_variant(job), default=str)
