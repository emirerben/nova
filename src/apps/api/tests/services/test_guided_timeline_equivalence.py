"""KRI-219 Lane B: the extracted guided-timeline math is byte-identical to the
pre-extraction route implementation.

``tests/fixtures/guided_timeline_equivalence.json`` was produced by running the
ORIGINAL ``_guided_v2_revision_for_write`` / ``_project_guided_revision_lanes``
(git 2cf05f959) over the scenarios below; the route wrappers must reproduce it.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

import app.routes.generative_jobs as gj
from tests.services._guided_timeline_fixtures import guided_bars, guided_job, guided_revision

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "guided_timeline_equivalence.json"


def _slot(sid, clip, dur, *, in_s=0.0, parent=None, **kw):
    return gj.TimelineSlotEdit(
        slot_id=sid, parent_segment_id=parent, clip_index=clip, in_s=in_s, duration_s=dur, **kw
    )


def scenarios(revision):
    return {
        "identity": [_slot(f"s{i + 1}", i, 2.0) for i in range(4)],
        "reorder": [_slot(f"s{i + 1}", i, 2.0) for i in (2, 0, 1, 3)],
        "trim": [_slot("s1", 0, 1.0, in_s=1.0)] + [_slot(f"s{i + 1}", i, 2.0) for i in range(1, 4)],
        "remove": [
            _slot("s1", 0, 2.0),
            _slot("s2", 1, 2.0, removed=True),
            _slot("s3", 2, 2.0),
            _slot("s4", 3, 2.0),
        ],
        "split": [
            _slot("s1", 0, 0.8),
            _slot("s1b", 0, 1.2, in_s=0.8, parent="s1"),
            *[_slot(f"s{i + 1}", i, 2.0) for i in range(1, 4)],
        ],
        "transitions": [
            _slot("s1", 0, 2.0, transition_after="crossfade", transition_duration_s=0.3),
            _slot("s2", 1, 2.0, transition_after="dip_to_black", transition_duration_s=0.2),
            _slot("s3", 2, 2.0, transition_after="flash", transition_duration_s=0.1),
            _slot("s4", 3, 2.0, transition_after="crossfade", transition_duration_s=0.3),
        ],
        "retime_crop_look": [
            _slot("s1", 0, 1.0, playback_rate=2.0),
            _slot(
                "s2",
                1,
                2.0,
                source_crop={"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
                look_preset="golden_hour",
            ),
            _slot("s3", 2, 2.0),
            _slot("s4", 3, 2.0),
        ],
        "errors_bounds": [_slot("s1", 0, 9.0, in_s=5.0)],
        "errors_parent": [_slot("s1", 0, 2.0, parent="nope")],
        "errors_clip": [_slot("s1", 9, 2.0)],
        "errors_duration": [_slot("s1", 0, 0.05)],
        "errors_empty": [],
        "errors_all_removed": [_slot("s1", 0, 2.0, removed=True)],
    }


def _lane_raw(revision, bars):
    total = revision["segments"][-1]["output_end_s"]
    return {
        **copy.deepcopy(revision),
        "text_elements": copy.deepcopy(bars),
        "sound_effects": [{"id": "sfx-1", "at_s": 3.0}, {"id": "sfx-2", "at_s": 7.0}],
        "media_overlays": [{"id": "ov-1", "start_s": 2.0, "end_s": 4.0}],
        "visual_blocks": [{"id": "vb-1", "start_s": 0.0, "end_s": total}],
        "motion_scenes": [{"id": "ms-1", "start_frame": 60, "end_frame_exclusive": 120}],
        "tombstones": [],
    }


def _run(module, revision, bars):
    out = {}
    job_id = str(uuid.uuid4())
    for name, slots in scenarios(revision).items():
        rev = guided_revision(job_id)
        job, variant = guided_job(rev, bars, job_id)
        payload = gj.TimelineEditRequest.model_construct(
            slots=slots,
            revision_number=1,
            base_generation=gj.variant_render_baseline(variant),
            guided_revision=None,
        )
        module._guided_v2_revision = lambda *_a, _r=rev: _r
        try:
            new = module._guided_v2_revision_for_write(job, variant, payload)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None)
            out[name] = {"error": detail if detail is not None else repr(exc)}
            continue
        new["sources"] = [{**s, "gcs_path": "<path>"} for s in new["sources"]]
        out[name] = {"segments": new["segments"], "state_hash_set": bool(new["state_hash"])}
        raw = _lane_raw(rev, bars)
        baseline = copy.deepcopy(raw)
        module._project_guided_revision_lanes(
            raw,
            old_segments=list(rev["segments"]),
            new_segments=list(new["segments"]),
            baseline_lanes=baseline,
            authored_lanes=set(),
        )
        out[name]["lanes"] = {
            key: raw[key]
            for key in ("text_elements", "sound_effects", "media_overlays", "visual_blocks")
        } | {"motion_scenes": raw["motion_scenes"], "tombstones": raw["tombstones"]}
    return json.loads(json.dumps(out, sort_keys=True))


def _bars():
    return guided_bars(guided_revision(str(uuid.uuid4())))


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "edit_transitions_enabled", True, raising=False)


def test_route_wrappers_match_pre_extraction_output(monkeypatch):
    monkeypatch.setattr(gj, "_guided_v2_revision", gj._guided_v2_revision)
    rev = guided_revision(str(uuid.uuid4()))
    result = _run(gj, rev, _bars())
    # Segment ids are deterministic in every scenario (no uuid fallback exercised).
    expected = json.loads(FIXTURE.read_text())
    assert result == expected
