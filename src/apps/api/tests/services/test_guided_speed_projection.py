"""Source-anchored timing survives a speed change, including cut ends."""

import pytest

from app.services.guided_timeline import make_time_projector, project_lane_times


def segment(sid, start, duration, rate=1):
    return dict(
        segment_id=sid,
        media_id="same",
        source_start_s=2,
        source_end_s=8,
        duration_s=duration,
        output_start_s=start,
        output_end_s=start + duration,
        playback_rate=rate,
    )


@pytest.mark.parametrize("old_rate,new_rate", [(1, 2), (2, 0.5), (0.25, 4), (4, 1)])
def test_projection_uses_both_source_rates(old_rate, new_rate):
    old = [segment("a", 0, 6 / old_rate, old_rate)]
    new = [segment("a", 0, 6 / new_rate, new_rate)]
    assert make_time_projector(old, new)(2 / old_rate) == (round(2 / new_rate, 6), "a")


def test_interval_end_uses_preceding_occurrence_after_speed_change():
    old = [segment("a", 0, 6), segment("b", 6, 6)]
    new = [segment("a", 0, 12, 0.5)]
    raw = {"text_elements": [{"id": "label", "start_s": 1, "end_s": 6}]}
    project_lane_times(raw, old_segments=old, new_segments=new)
    assert raw["text_elements"][0]["start_s"] == 2
    assert raw["text_elements"][0]["end_s"] == 12
    assert not raw.get("tombstones")


@pytest.mark.parametrize("rate", [0.5, 2])
def test_chat_free_text_exact_end_follows_source_clock(rate):
    from app.services.kria_editor_timeline import _project_span

    old = [segment("a", 0, 6)]
    new = [segment("a", 0, 6 / rate, rate)]
    span = _project_span(
        make_time_projector(old, new),
        old,
        1,
        5,
        project_end=make_time_projector(old, new, interval_end=True),
    )
    assert span == ((1 / rate, "a"), (5 / rate, "a"))


def test_source_preserving_revision_roundtrip_keeps_exact_edges():
    from app.schemas.guided_edit_revision import normalize_guided_editor_revision
    from tests.services._guided_timeline_fixtures import guided_revision

    revision = guided_revision("00000000-0000-0000-0000-000000000001")
    row = revision["segments"][0]
    row.update(
        source_start_s=0.013,
        source_end_s=1.513,
        playback_rate=4,
        duration_s=0.375,
        preserve_source_window=True,
    )
    for rate in [4, 2, 0.5, 1, 4]:
        row.update(playback_rate=rate, duration_s=1.5 / rate)
        revision["state_hash"] = ""
        revision = normalize_guided_editor_revision(revision)
        row = revision["segments"][0]
        assert row["source_start_s"] == 0.013
        assert row["source_end_s"] == 1.513
        assert row["duration_s"] == pytest.approx(round(1.5 / rate * 30) / 30, abs=1e-6)


def test_selected_end_save_reflows_frame_clock_and_unrelated_save_preserves_it():
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.schemas.guided_edit_revision import normalize_guided_editor_revision
    from app.services.guided_timeline import build_guided_segments
    from tests.services._guided_timeline_fixtures import guided_revision

    revision = guided_revision("00000000-0000-0000-0000-000000000001", duration_s=7.5)
    slots = [
        TimelineSlotEdit(slot_id=f"s{i + 1}", clip_index=i, in_s=0, duration_s=7.5)
        for i in range(4)
    ]
    slots[0] = slots[0].model_copy(
        update={"source_end_s": 7.5, "duration_s": 3.75, "playback_rate": 2}
    )

    def save(rows):
        segments = build_guided_segments(
            revision, rows, transitions_enabled=True, max_slots=120, max_total_s=120
        )
        return normalize_guided_editor_revision(
            {**revision, "segments": segments, "state_hash": ""}
        )

    revision = save(slots)
    assert revision["segments"][0]["source_end_s"] == 7.5
    for previous, following in zip(revision["segments"], revision["segments"][1:]):
        assert previous["output_end_s"] == following["output_start_s"]
    slots[0] = TimelineSlotEdit(
        slot_id="s1", clip_index=0, in_s=0, duration_s=revision["segments"][0]["duration_s"]
    )
    revision = save(slots)
    assert revision["segments"][0]["source_end_s"] == 7.5
    assert revision["segments"][0]["preserve_source_window"] is True
    # Older clients may trim without the new source-edge field. That is still
    # an intentional new selection and must not resurrect the previous end.
    slots[0] = slots[0].model_copy(update={"duration_s": 1.0})
    revision = save(slots)
    assert revision["segments"][0]["source_end_s"] == 2
    assert not revision["segments"][0].get("preserve_source_window")


def test_chat_old_clock_uses_exact_selected_edge_after_frame_rounding():
    from app.services.kria_editor_timeline import _old_segments

    rows = [
        {
            "slot_id": "a",
            "media_id": "source",
            "in_s": 0.013,
            "source_end_s": 1.513,
            "duration_s": 0.366667,
            "playback_rate": 4,
        }
    ]
    old = _old_segments(rows, {})
    assert old[0]["source_end_s"] == 1.513
