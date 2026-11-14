"""KRI-1423: every capability the iPhone editor can write passes the server Save.

The founder could not save an edit built from split sections. A Save the editor wrote and the
server refused leaves the creator holding work they cannot keep, so each row here is one
capability the editor writes, sent the way the phone sends it (the whole timeline, split
sections and the rows' server-written keys), through the same function the route calls
(``prepare_editor_commit``). Every row is Saved twice: as the editor writes it, then again from
the timeline the first Save stored, because the phone re-sends what it loaded.

A new editor capability is a new ROW, never a new test file. A refusal here is a bug in the
validator or in what the phone writes, not a row to delete.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context
from app.services.device_render import device_status
from tests.routes._editor_capability_job import PHOTO, capability_job, section, slot

_COMPILE = json.loads(
    (
        Path(__file__).resolve().parents[5] / "tests/fixtures/drawing-parity/compile_v1.json"
    ).read_text()
)
_DRAWING = {
    **next(c for c in _COMPILE["cases"] if c["name"] == "identity-inside-timeline")["drawings"][0],
    "start_s": 1.0,
    "end_s": 3.0,
}
VARIANT = "original_text"


@dataclass
class Doc:
    """What the phone sends: the timeline, its split sections, and any other section."""

    slots: list[dict]
    sections: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Stable slot ids, so the fixture the phone tests replay does not change per run."""
        renamed = {
            row["slot_id"]: str(uuid.uuid5(uuid.NAMESPACE_URL, f"kri-1423/{index}"))
            for index, row in enumerate(self.slots)
        }
        for row in self.slots:
            row["slot_id"] = renamed[row["slot_id"]]
            if row.get("parent_segment_id"):
                row["parent_segment_id"] = renamed[row["parent_segment_id"]]

    def wire(self, generation: str) -> dict:
        return json.loads(
            json.dumps(
                {
                    "base_generation": generation,
                    "editor_state_version": 2,
                    "timeline_slots": self.slots,
                    "split_screen_sections": self.sections,
                    **self.extra,
                }
            )
        )


def duo() -> Doc:
    return Doc([slot(0, panel=0, in_s=1.0), slot(2, panel=1)], [section()])


@dataclass(frozen=True)
class Row:
    id: str
    editor: str
    doc: Callable[[], Doc]
    check: Callable[[dict, Any], None] | None = None
    setup: Callable[[Any, pytest.MonkeyPatch], None] | None = None


ROWS: list[Row] = []


def row(id: str, editor: str, check=None, setup=None):
    def register(build: Callable[[], Doc]) -> Callable[[], Doc]:
        ROWS.append(Row(id, editor, build, check, setup))
        return build

    return register


def panels(recipe) -> list:
    return [c for t in recipe.tracks for c in t.clips if c.split_screen]


@row("split-two-panels", "split a clip into two panels")
def _():
    return duo()


@row("split-three-panels-layout", "three panels, weights, gutter, background colour")
def _():
    return Doc(
        [slot(0, panel=0), slot(2, panel=1), slot(3, panel=2)],
        [section(4, 3, panel_weights=[2, 1, 1], gutter=0.02, background_color="#112233")],
    )


@row("split-collapse-empty-panels", "collapse ended panels")
def _():
    return Doc(
        [slot(0, panel=0, duration_s=2.0), slot(2, panel=1)],
        [section(4, 2, collapse_empty_panels=True)],
    )


@row("panel-two-clips-in-a-row", "second clip in the same panel")
def _():
    return Doc(
        [
            slot(0, panel=0, duration_s=2.0),
            slot(1, panel=0, offset=2.0, duration_s=2.0),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-gap-between-clips", "a gap inside one panel")
def _():
    return Doc(
        [
            slot(0, panel=0, duration_s=1.0),
            slot(1, panel=0, offset=2.0, duration_s=2.0),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-crossfade-between-clips", "a transition between two clips of one panel")
def _():
    return Doc(
        [
            slot(
                0, panel=0, duration_s=2.0, transition_after="crossfade", transition_duration_s=0.3
            ),
            slot(1, panel=0, offset=1.7, duration_s=2.3),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-shorter-than-section", "one panel shorter than the other")
def _():
    return Doc([slot(0, panel=0, duration_s=2.5), slot(2, panel=1)], [section()])


def _blank_ready(job, monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitBlankPanel"],
    )


def blank(
    *, panel: int, offset: float = 0.0, duration_s: float = 4.0, color: str = "#112233"
) -> dict:
    """What the phone writes for a panel's blank block: a colour and a window, no clip index."""
    return {
        "slot_id": str(uuid.uuid4()),
        "media_kind": "blank",
        "color": color,
        "in_s": 0.0,
        "duration_s": duration_s,
        "split_section_id": "s",
        "split_panel_index": panel,
        "split_offset_s": offset,
    }


def _blanks_drawn(*expected: tuple[int, float, float, str]):
    def check(variant, recipe) -> None:
        got = [
            (b.panel_index, b.start, b.end, b.color) for b in recipe.split_screen_sections[0].blanks
        ]
        assert got == list(expected)
        assert "splitBlankPanel" in recipe.required_capabilities

    return check


@row(
    "blank-in-panel",
    "fill a panel with a blank block",
    setup=_blank_ready,
    check=_blanks_drawn((1, 0.0, 4.0, "#112233")),
)
def _():
    return Doc([slot(0, panel=0), blank(panel=1)], [section()])


@row(
    "blank-cut-trimmed-and-recoloured",
    "cut, trim and recolour a blank block",
    setup=_blank_ready,
    check=_blanks_drawn((1, 0.0, 1.5, "#FF0000"), (1, 2.0, 3.0, "#00FF00")),
)
def _():
    return Doc(
        [
            slot(0, panel=0),
            blank(panel=1, duration_s=1.5, color="#FF0000"),
            blank(panel=1, offset=2.0, duration_s=1.0, color="#00FF00"),
        ],
        [section()],
    )


@row("photo-in-panel", "a photo in a panel")
def _():
    return Doc([slot(0, panel=0), slot(PHOTO, panel=1, media_kind="image")], [section()])


@row("photo-cut-in-panel", "cut a photo in a panel")
def _():
    return Doc(
        [
            slot(0, panel=0),
            slot(PHOTO, panel=1, duration_s=1.5, media_kind="image"),
            slot(
                PHOTO,
                panel=1,
                offset=1.5,
                in_s=1.5,
                duration_s=2.5,
                media_kind="image",
                source_start_locked=True,
            ),
        ],
        [section()],
    )


@row("video-cut-in-panel", "cut a video in a panel")
def _():
    first = slot(2, panel=0, duration_s=1.5, source_end_s=1.5)
    second = slot(
        2,
        panel=0,
        offset=1.5,
        in_s=1.5,
        duration_s=2.5,
        source_end_s=4.0,
        parent_segment_id=first["slot_id"],
        source_start_locked=True,
    )
    return Doc([first, second, slot(3, panel=1)], [section()])


@row("trim-to-the-source-end", "trim a panel clip to the end of its footage")
def _():
    return Doc([slot(0, panel=0, in_s=8.0, duration_s=4.0), slot(2, panel=1)], [section()])


@row("trim-start-locked", "trim a panel clip's start")
def _():
    return Doc(
        [slot(0, panel=0, in_s=3.0, source_start_locked=True), slot(2, panel=1)], [section()]
    )


@row("trim-with-source-end", "slide a panel clip's window")
def _():
    return Doc(
        [slot(0, panel=0, in_s=3.0, duration_s=4.0, source_end_s=7.0), slot(2, panel=1)],
        [section()],
    )


@row("speed-double-panel", "double speed on a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, in_s=1.0, duration_s=3.0, source_end_s=7.0, playback_rate=2.0),
            slot(2, panel=1, duration_s=3.0),
        ],
        [section(3)],
    )


@row("speed-half-panel", "half speed on a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, in_s=1.0, duration_s=4.0, source_end_s=3.0, playback_rate=0.5),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("freeze-in-panel", "freeze a frame in a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, in_s=1.0, duration_s=4.0, freeze_hold_s=2.0, freeze_frame_s=3.0),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-audio", "mute, volume and fades of a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, muted=True),
            slot(2, panel=1, source_audio_volume=0.5, audio_fade_in=0.5, audio_fade_out=0.5),
        ],
        [section()],
    )


@row("panel-source-crop", "reframe a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, source_crop={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-media-transform", "pinch and move a panel clip")
def _():
    return Doc(
        [
            slot(
                0,
                panel=0,
                media_transform={
                    "scale": 1.2,
                    "position_x": 10,
                    "position_y": -5,
                    "rotation_degrees": 3,
                },
            ),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("panel-tilt", "tilt a panel clip")
def _():
    return Doc(
        [
            slot(0, panel=0, tilt={"rotation_deg": 6, "backdrop": {"kind": "blur"}}),
            slot(2, panel=1),
        ],
        [section()],
    )


@row("full-screen-before-and-after", "full screen clips around a split section")
def _():
    return Doc(
        [slot(3, duration_s=2.0), slot(0, panel=0), slot(2, panel=1), slot(4, duration_s=2.0)],
        [section()],
    )


@row("two-sections", "two split sections")
def _():
    return Doc(
        [
            slot(0, panel=0),
            slot(2, panel=1),
            {
                **slot(3, in_s=0.0, duration_s=3.0),
                "split_section_id": "t",
                "split_panel_index": 0,
                "split_offset_s": 0.0,
            },
            {
                **slot(4, in_s=0.0, duration_s=3.0),
                "split_section_id": "t",
                "split_panel_index": 1,
                "split_offset_s": 0.0,
            },
        ],
        [section(), section(3, 2, id="t")],
    )


@row("text-over-split", "a title over a split edit")
def _():
    doc = duo()
    doc.extra["text_elements"] = [{"id": "manual-title", "text": "Hello", "start_s": 0, "end_s": 4}]
    return doc


@row("transition-between-full-screen-clips", "a transition between full screen clips")
def _():
    return Doc(
        [
            slot(3, duration_s=2.0, transition_after="crossfade", transition_duration_s=0.3),
            slot(4, duration_s=2.0),
        ]
    )


@row("photo-trim-start-in-panel", "drag the start of a photo in a panel")
def _():
    return Doc(
        [
            slot(0, panel=0),
            slot(
                PHOTO,
                panel=1,
                in_s=1.0,
                duration_s=3.0,
                media_kind="image",
                source_start_locked=True,
            ),
        ],
        [section()],
    )


@row("photo-cut-full-screen", "cut a photo that plays full screen")
def _():
    return Doc(
        [
            slot(PHOTO, duration_s=2.0, media_kind="image"),
            slot(PHOTO, in_s=2.0, duration_s=2.0, media_kind="image", source_start_locked=True),
        ]
    )


@row("pinned-text-over-split", "a corner text pinned for the whole video")
def _():
    doc = duo()
    doc.extra["text_elements"] = [
        {
            "id": "pin-1",
            "text": "Day 4",
            "start_s": 0,
            "end_s": 4,
            "position": "custom",
            "x_frac": 0.2,
            "y_frac": 0.9,
        }
    ]
    return doc


@row("caption-style-over-split", "caption look")
def _():
    doc = duo()
    doc.extra["caption_meta"] = {"enabled": True, "style": "word", "y_frac": 0.7, "size_px": 80}
    return doc


@row("original-sound-level-over-split", "the footage's own sound level")
def _():
    doc = duo()
    doc.extra["mix"] = {"original_level": 0.5}
    return doc


@row("removed-clip-keeps-its-row", "delete a clip from a split edit")
def _():
    return Doc(
        [slot(0, panel=0), slot(2, panel=1), slot(3, panel=1, offset=2.0, removed=True)],
        [section()],
    )


@row("clip-fit-card", "show a full screen clip as a supporting card")
def _():
    return Doc([slot(3, duration_s=2.0, layout="supporting_card"), slot(4, duration_s=2.0)])


@row("title-over-split", "rename the video")
def _():
    doc = duo()
    doc.extra["title"] = "A day in Lisbon"
    return doc


@row("drawing-over-split", "a finger drawing")
def _():
    doc = duo()
    doc.extra["drawings"] = [_DRAWING]
    return doc


def _speaker_ready(job, monkeypatch) -> None:
    from app.services.client_protocol import set_client_protocol

    set_client_protocol(47)
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundColor",
            "speakerBackgroundWindows",
            "speakerFrameWindows",
        ],
    )
    variant = job.assembly_plan["variants"][0]
    variant["speaker_background"] = {"kind": "color", "color": "#000000"}
    variant["editor_split_screen_origin"] = True


@row("speaker-frame-over-split", "move the speaker's frame in a panel", setup=_speaker_ready)
def _():
    return Doc(
        [
            slot(
                0,
                panel=0,
                speaker_subject=True,
                speaker_frame_override={
                    "mode": "custom",
                    "frame": {"width_fraction": 0.3, "x_fraction": 0.25, "y_fraction": 0.75},
                },
            ),
            slot(2, panel=1, speaker_subject=True, speaker_frame_override={"mode": "full"}),
        ],
        [section()],
    )


@pytest.fixture(autouse=True)
def _no_client_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _active(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("removed")]


@pytest.mark.parametrize("case", ROWS, ids=lambda case: case.id)
def test_every_editor_capability_saves_and_saves_again(monkeypatch, case):
    job = capability_job(monkeypatch)
    if case.setup is not None:
        case.setup(job, monkeypatch)
    doc = case.doc()

    def save(doc: Doc) -> dict:
        variant = job.assembly_plan["variants"][0]
        body = gj.EditorCommitRequest.model_validate(doc.wire(gj.variant_render_baseline(variant)))
        return gj.prepare_editor_commit(job, VARIANT, body)

    save(doc)
    reopened = gj.dispatch_get_timeline(job, VARIANT, sign_url=lambda *_: "https://example.test/m")
    save(
        Doc(
            copy.deepcopy(reopened["slots"]),
            copy.deepcopy(reopened.get("split_screen_sections") or []),
        )
    )
    variant = job.assembly_plan["variants"][0]
    stored = variant["user_timeline"]
    assert len(_active(stored["slots"])) == len(_active(doc.slots))
    assert [x["id"] for x in stored.get("split_screen_sections") or []] == [
        x["id"] for x in doc.sections
    ]
    recipe = device_status(job, VARIANT).request.recipe
    assert recipe.duration > 0
    # A blank block rides on its section (`blanks`), so it is a stored slot but not a panel clip.
    assert len(panels(recipe)) == sum(
        1
        for r in _active(doc.slots)
        if r.get("split_section_id") and r.get("media_kind") != "blank"
    )
    if "text_elements" in doc.extra:
        assert {t["id"] for t in doc.extra["text_elements"]} <= {
            t["id"] for t in variant.get("text_elements") or []
        }
    if "drawings" in doc.extra:
        assert [d.id for d in recipe.drawing_layers] == [d["id"] for d in doc.extra["drawings"]]
    if case.check is not None:
        case.check(variant, recipe)


# The phone's own copy of this table: `EditorSaveCapabilityTests` replays it and fails when the
# client's guard (`splitScreenIsValid`) refuses a document the server saves.
FIXTURE = Path(__file__).resolve().parents[5] / (
    "src/apps/ios/Tests/Fixtures/editor_save_capabilities.json"
)


def build_fixture() -> dict:
    rows = []
    for case in ROWS:
        wire = case.doc().wire("")
        for key in ("base_generation", "editor_state_version"):
            wire.pop(key)
        rows.append({"id": case.id, "editor": case.editor, "sections": wire})
    return {"schema_version": 1, "rows": rows}


def test_ios_fixture_is_the_server_truth():
    """Regenerate with UPDATE_EDITOR_CAPABILITY_FIXTURE=1 and ``-k ios_fixture``."""
    built = json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_EDITOR_CAPABILITY_FIXTURE"):
        FIXTURE.write_text(built)
    assert FIXTURE.read_text() == built


# Every section the phone's editor writes (`enum EditorSection`) is either a row above or named
# here with the suite that Saves it, so a new editor section cannot ship without the question
# "does it Save?" (KRI-1423). `needle` must appear in the suite, so a renamed suite fails too.
IOS_EDITOR = (
    Path(__file__).resolve().parents[5] / "src/apps/ios/Kria/Core/NativeEditorDocument.swift"
)
IN_MATRIX = {
    "timeline": "split-two-panels",
    "text": "text-over-split",
    "captionMeta": "caption-style-over-split",
    "mix": "original-sound-level-over-split",
    "title": "title-over-split",
    "drawings": "drawing-over-split",
}
OWNER_SUITE = {
    "captions": ("test_phone_subtitled_editor_commit.py", "caption_cues"),
    "music": ("test_phone_song_editor_commit.py", "music_track_id"),
    "backgroundMusic": ("test_editor_commit.py", "background_music"),
    "lyrics": ("test_editor_commit_lyrics_section.py", "lyrics"),
    "orientation": ("test_phone_render_shape_editor.py", "orientation"),
    "landscapeFit": ("test_phone_render_shape_editor.py", "landscape_fit"),
    "soundEffects": ("test_phone_guided_sfx.py", "sound_effects"),
    "mediaOverlays": ("test_phone_subtitled_editor_commit.py", "media_overlays"),
    "visualBlocks": ("test_phone_authored_editor_media.py", "visual_blocks"),
    "motionScenes": ("test_phone_voiceover_editor_lanes.py", "motion_scenes"),
    "cameraEffects": ("test_phone_editor_media.py", "camera_effects"),
    "carouselMoment": ("test_editor_commit.py", "carousel_moment"),
    "speakerBackground": ("test_authored_split_background_admission.py", "speaker_background"),
    "userSong": ("test_phone_song_editor_commit.py", "user_song"),
}


def _ios_sections() -> set[str]:
    body = re.search(r"enum EditorSection: [^{]*\{(.*?)\n\}", IOS_EDITOR.read_text(), re.S)
    assert body, "enum EditorSection moved: update this guard"
    names: set[str] = set()
    for line in body.group(1).splitlines():
        line = line.split("///")[0].strip()
        if line.startswith("case "):
            names |= {part.split("=")[0].strip() for part in line[len("case ") :].split(",")}
    return names


def test_every_editor_section_is_saved_by_a_row_or_a_named_suite():
    sections = _ios_sections()
    assert {"timeline", "text", "drawings", "userSong"} <= sections
    assert sections == set(IN_MATRIX) | set(OWNER_SUITE)
    row_ids = {case.id for case in ROWS}
    assert set(IN_MATRIX.values()) <= row_ids
    tests = Path(__file__).resolve().parents[1]
    for suite, needle in OWNER_SUITE.values():
        assert needle in next(tests.rglob(suite)).read_text(), (suite, needle)


# What a timeline row can carry: a field the server reads that no row writes would be a
# capability that never Saved under test. The few the phone never writes on this edit say why.
NOT_WRITTEN = {
    "duration_beats": "beat-grid slots, refused as `authored_seconds_required`",
    "look_preset": "the phone refuses a clip look on device (`canSetClipLook`)",
    "look_adjustments": "same: no clip look is written on a phone-rendered edit",
}


def test_every_timeline_slot_field_is_written_by_a_row():
    written = {key for case in ROWS for row in case.doc().slots for key in row}
    unwritten = set(gj.TimelineSlotEdit.model_fields) - written
    assert unwritten == set(NOT_WRITTEN), unwritten ^ set(NOT_WRITTEN)
