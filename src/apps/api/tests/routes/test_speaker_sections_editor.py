"""KRI-1558: the speaker's layout per section over the HTTP editor surface.

Rows 20, 21, 30 and 31 of `docs/reviews/kri-1558-section-layout-failure-modes.md`: the editor
document shows ``layout_windows`` in cut time, a Save that touches anything else keeps them, a
Save from a build below `SPEAKER_SECTIONS_PROTOCOL` never silently drops them (426, nothing
committed), a closed gate refuses a SET but never a clear, a malformed list has its own code, the
device poll tells an old build to update, and with the flag off nothing about a variant without the
field changes. Every Save goes through `prepare_editor_commit` and the real phone recompile.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    SPEAKER_SECTIONS_PROTOCOL,
    clear_request_context,
    set_client_features,
)
from app.services.client_protocol import (
    set_client_protocol as _set_protocol,
)
from app.services.device_render import device_status
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    lanes_from_recipe,
    project_phone_subtitled_editor_sections,
    variant_carries_speaker_sections,
)
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
CORNER = {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.8}
LEFT = {"width_fraction": 0.42, "x_fraction": 0.24, "y_fraction": 0.8}
WINDOWS = [
    {
        "media_id": SECOND.media_id,
        "kind": "image",
        "source_start_s": 0.0,
        "start_s": 3.0,
        "end_s": 7.0,
    }
]
LAYOUTS = [
    {"start_s": 0.0, "end_s": 3.0, "layout": "own"},
    {"start_s": 3.0, "end_s": 7.0, "layout": "small", "speaker_frame": CORNER},
    {"start_s": 7.0, "end_s": 10.0, "layout": "own"},
]
OLD = SPEAKER_SECTIONS_PROTOCOL - 1
NEW = SPEAKER_SECTIONS_PROTOCOL
# What a build with the sections renderer advertises in `X-Kria-Client-Features` (Tadic, #prs
# 2026-11-26: other branches' builds took the same protocol numbers without it).
SECTIONS = ("speakerSections",)


def set_client_protocol(protocol, features=None) -> None:
    """The calling build: a build at or past the sections protocol advertises them unless told
    otherwise (``features``); an older build advertises nothing."""
    _set_protocol(protocol)
    if features is None:
        features = SECTIONS if protocol is not None and protocol >= NEW else ()
    set_client_features(list(features))


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open(monkeypatch, *, flag=True, token=True, windows=True) -> None:
    ours = {"speakerBackgroundWindows", "speakerSections"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if windows:
        features.append("speakerBackgroundWindows")
    if token:
        features.append("speakerSections")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    monkeypatch.setattr(settings, "speaker_sections_enabled", flag)


def _with_sections(visual=PHOTO, layouts=LAYOUTS) -> dict:
    return _document(visual) | {"windows": WINDOWS, "layout_windows": layouts}


def sections_job(monkeypatch):
    """A phone Talking variant whose pinned recipe draws her own take, then small, then own."""
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(NEW)
    save(job, speaker_background=_with_sections())
    clear_request_context()
    return job


def _spans(layer) -> list[tuple[float, float, str]]:
    return [(w.start, w.end, w.layout) for w in layer.layout_windows or ()]


EXPECTED = [(0.0, 3.0, "own"), (3.0, 7.0, "small"), (7.0, 10.0, "own")]


# --- row 20: the document shows them, in cut time, and every Save keeps them -------------------


def test_the_recipe_and_the_editor_document_carry_the_layouts(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    layer = _recipe(job).speaker_background
    assert _spans(layer) == EXPECTED
    assert layer.layout_windows[1].frame.model_dump() == CORNER
    assert "speakerSections" in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"]["layout_windows"] == LAYOUTS
    assert variant_carries_speaker_sections(job.assembly_plan, _variant(job))
    # Re-projected from the pinned recipe alone (a never-saved variant), same document.
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"]["layout_windows"] == LAYOUTS
    rebuilt = lanes_from_recipe(_recipe(job), visuals=POOL)
    assert [(s.start_s, s.end_s, s.layout) for s in rebuilt.background.layout_windows] == EXPECTED


def test_a_variant_without_the_field_shows_no_key_anywhere(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(NEW)
    save(job, speaker_background=_document(PHOTO))
    assert "layout_windows" not in json.dumps(_variant(job))
    assert "layout_windows" not in json.dumps(
        _recipe(job).model_dump(mode="json")["speaker_background"]
    )
    assert not variant_carries_speaker_sections(job.assembly_plan, _variant(job))


def test_a_save_that_leaves_the_background_alone_keeps_them(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    set_client_protocol(NEW)
    _resave(job, sound_effects=[])
    assert _spans(_recipe(job).speaker_background) == EXPECTED
    assert _variant(job)["speaker_background"]["layout_windows"] == LAYOUTS


def test_a_never_saved_variant_keeps_them_through_a_save_that_touches_something_else(
    monkeypatch,
) -> None:
    job = sections_job(monkeypatch)
    # A worker-rendered variant: the layouts live only in the pinned recipe.
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    set_client_protocol(NEW)
    _resave(job, sound_effects=[])
    assert _spans(_recipe(job).speaker_background) == EXPECTED


def test_changing_the_fill_keeps_them(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    set_client_protocol(NEW)
    _resave(job, speaker_background=_with_sections(SECOND))
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)
    assert _spans(_recipe(job).speaker_background) == EXPECTED


def test_the_save_can_move_and_clear_them(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    set_client_protocol(NEW)
    moved = [
        {"start_s": 0.0, "end_s": 2.0, "layout": "own"},
        {"start_s": 2.0, "end_s": 7.0, "layout": "small", "speaker_frame": LEFT},
        {"start_s": 7.0, "end_s": 9.0, "layout": "picture"},
    ]
    _resave(job, speaker_background=_with_sections(layouts=moved))
    assert _spans(_recipe(job).speaker_background) == [
        (0.0, 2.0, "own"),
        (2.0, 7.0, "small"),
        (7.0, 9.0, "picture"),
    ]
    assert _recipe(job).speaker_background.layout_windows[1].frame.model_dump() == LEFT
    _resave(job, speaker_background=_document(PHOTO) | {"windows": WINDOWS})
    assert _recipe(job).speaker_background.layout_windows is None
    assert "speakerSections" not in _recipe(job).required_capabilities
    assert "layout_windows" not in _variant(job)["speaker_background"]


def test_remove_background_clears_them(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    set_client_protocol(NEW)
    _resave(job, speaker_background=None)
    assert _recipe(job).speaker_background is None
    assert not variant_carries_speaker_sections(job.assembly_plan, _variant(job))


# --- row 21: an old build is told to update, never silently stripped --------------------------


def test_a_save_from_an_older_build_on_a_variant_carrying_them_is_426_and_commits_nothing(
    monkeypatch,
) -> None:
    job = sections_job(monkeypatch)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    snapshot_before = json.dumps(_variant(job), sort_keys=True, default=str)
    set_client_protocol(OLD)
    with pytest.raises(HTTPException) as refused:
        _resave(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "speaker_sections",
        "minimum_client_protocol": SPEAKER_SECTIONS_PROTOCOL,
    }
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert json.dumps(_variant(job), sort_keys=True, default=str) == snapshot_before


def test_an_older_build_setting_them_is_426(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(OLD)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_with_sections())
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "speaker_sections"
    assert "layout_windows" not in json.dumps(_recipe(job).model_dump(mode="json"))


def test_an_older_build_saving_a_variant_without_them_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(OLD)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None


def test_the_device_poll_tells_an_older_build_to_update(monkeypatch) -> None:
    from app.routes import device_render

    job = sections_job(monkeypatch)
    plain = background_job(monkeypatch, background=_background(PHOTO))

    def detail(target, protocol):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(target)))
        return device_render._client_update_detail(target, {}, status)

    assert detail(job, OLD) == {
        "code": "client_update_required",
        "feature": "speaker_sections",
        "minimum_client_protocol": SPEAKER_SECTIONS_PROTOCOL,
    }
    assert detail(job, NEW) is None
    assert detail(plain, OLD) is None
    # Another branch's build with the same or a later number but no sections renderer.
    for protocol, features in ((NEW, ()), (NEW + 1, ()), (NEW + 1, ("clipDim",))):
        set_client_protocol(protocol, features)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(job)))
        assert device_render._client_update_detail(job, {}, status) == {
            "code": "client_update_required",
            "feature": "speaker_sections",
            "minimum_client_protocol": SPEAKER_SECTIONS_PROTOCOL,
        }


# --- row 30: a closed gate refuses a SET, never a clear; a bad list has its own code -----------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"flag": False}, "sections_disabled"),
        ({"token": False}, "sections_token_not_verified"),
    ],
)
def test_setting_them_with_a_closed_gate_is_422(monkeypatch, kwargs, reason) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch, **kwargs)
    set_client_protocol(NEW)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=_with_sections())
    assert dark.value.status_code == 422
    assert dark.value.detail == {"code": "speaker_sections_disabled", "reason": reason}
    assert _recipe(job).speaker_background.layout_windows is None
    # The same background without the layouts is unaffected.
    save(job, speaker_background=_document(SECOND))
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


def test_clearing_works_with_the_gate_closed(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(NEW)
    _resave(job, speaker_background=_document(PHOTO) | {"windows": WINDOWS})
    assert _recipe(job).speaker_background.layout_windows is None


def test_an_echo_of_what_the_variant_shows_is_not_regated(monkeypatch) -> None:
    job = sections_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(NEW)
    # A fill change that echoes the layouts it was shown is not a change to them.
    _resave(job, speaker_background=_with_sections(SECOND))
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)
    assert _variant(job)["speaker_background"]["layout_windows"] == LAYOUTS
    # The device recipe falls back to one layout while the gate is closed (strip, row 22).
    assert _recipe(job).speaker_background.layout_windows is None
    with pytest.raises(HTTPException) as dark:
        _resave(job, speaker_background=_with_sections(SECOND, layouts=LAYOUTS[:2]))
    assert dark.value.status_code == 422


@pytest.mark.parametrize(
    "layouts",
    [
        [
            {"start_s": 3.0, "end_s": 5.0, "layout": "own"},
            {"start_s": 1.0, "end_s": 2.0, "layout": "own"},
        ],
        [
            {"start_s": 0.0, "end_s": 4.0, "layout": "own"},
            {"start_s": 3.0, "end_s": 6.0, "layout": "own"},
        ],
        [{"start_s": 0.0, "end_s": 0.3, "layout": "own"}],
        [{"start_s": 8.0, "end_s": 40.0, "layout": "own"}],
        [{"start_s": i * 0.2, "end_s": i * 0.2 + 0.1, "layout": "own"} for i in range(41)],
        [{"start_s": 4.0, "end_s": 3.0, "layout": "own"}],
        [{"start_s": 0.0, "end_s": 3.0, "layout": "small"}],
        [{"start_s": 0.0, "end_s": 3.0, "layout": "picture", "speaker_frame": CORNER}],
    ],
    ids=[
        "unsorted",
        "overlap",
        "short",
        "past_end",
        "too_many",
        "backwards",
        "small_without_frame",
        "frame_on_picture",
    ],
)
def test_a_malformed_list_is_422_invalid(monkeypatch, layouts) -> None:
    job = sections_job(monkeypatch)
    set_client_protocol(NEW)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    with pytest.raises(HTTPException) as bad:
        _resave(job, speaker_background=_with_sections(layouts=layouts))
    assert bad.value.status_code == 422
    assert bad.value.detail == {"code": "speaker_sections_invalid"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _spans(_recipe(job).speaker_background) == EXPECTED


def test_the_save_section_rejects_unknown_span_keys_and_layouts() -> None:
    with pytest.raises(ValueError):
        gj.EditorCommitSpeakerBackground(
            **_document(PHOTO),
            layout_windows=[{"start_s": 0, "end_s": 2, "layout": "own", "preset": "full"}],
        )
    with pytest.raises(ValueError):
        gj.EditorCommitSpeakerBackground(
            **_document(PHOTO), layout_windows=[{"start_s": 0, "end_s": 2, "layout": "corner"}]
        )


# --- row 31: the capability is present only where the phone can draw it ----------------------


@pytest.mark.parametrize(
    ("kwargs", "protocol", "expected"),
    [
        ({}, NEW, True),
        ({}, OLD, False),
        ({}, None, False),
        ({"flag": False}, NEW, False),
        ({"token": False}, NEW, False),
        ({"windows": False}, NEW, False),
        # A build at or past the number that does not advertise sections (Tadic, 2026-11-26).
        ({"features": ()}, NEW, False),
        ({"features": ()}, NEW + 1, False),
        ({"features": ("clipDim",)}, NEW - 1, False),
        ({"features": SECTIONS}, NEW + 1, True),
    ],
)
def test_the_capability_key_follows_the_gate(monkeypatch, kwargs, protocol, expected) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    features = kwargs.pop("features", None)
    _open(monkeypatch, **kwargs)
    set_client_protocol(protocol, features)
    capabilities = gj._editor_capabilities(job, _variant(job))
    assert ("speaker_sections" in capabilities) is expected
    if expected:
        assert capabilities["speaker_sections"] == {"editable": True}


def test_a_cloud_variant_never_advertises_it(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(NEW)
    assert not gj._speaker_sections_capability(job, _capability_variant("cloud"))


def test_flag_off_leaves_the_capability_map_and_the_save_section_byte_identical(
    monkeypatch,
) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    set_client_protocol(NEW)
    monkeypatch.setattr(settings, "speaker_sections_enabled", False)
    assert "speaker_sections" not in json.dumps(gj._editor_capabilities(job, _variant(job)))
    save(job, speaker_background=_document(PHOTO))
    assert "layout_windows" not in json.dumps(_variant(job), default=str)
    section = gj.EditorCommitSpeakerBackground(**_document(PHOTO))
    assert "layout_windows" not in section.model_dump()
    assert section.model_dump() == _document(PHOTO)
