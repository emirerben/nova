"""Near-duplicate dHash signal, scored against fixtures with known ground truth.

Failure modes this file is written from (listed before the code):

1. Rotated phone video (portrait stored as landscape + display matrix): hashed sideways, it would
   never match its upright re-encode.
2. Unrelated low-information frames (plain sky, blank wall, black, a symmetric vignette): their
   dHash is all-0/all-1 bits, or one fixed pattern for a vignette, so any two "match". They must
   produce no hash at all.
3. Black / near-black clips: same as 2, and a dark clip must also never be the one we keep.
4. Single-frame and very short clips: three keyframes collapse on one frame; seeking past the end
   of a wrong stored duration returns nothing.
5. HEIC photos: undecodable without pillow-heif. That must read as "try again later", not as a
   permanent empty result stored forever.
6. Huge / hostile files: a decompression bomb or a stuck ffmpeg must not take a worker down.
7. A mirrored shot, and a same-background different-subject shot, are not duplicates.
8. A burst A~B~C where A and C differ must not chain into one group.
9. Capture time: far apart means not the same moment; a missing time is stricter; naive vs aware
   datetimes must not raise.
10. The keep-one choice: never a dark or blurred twin over the sharp original.
11. Backfill: never overwrites another signal's key or a newer generation, and re-running is a
    no-op.
"""

from __future__ import annotations

import importlib.util
import itertools
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli import backfill_clip_signals as cli
from app.services import clip_signals
from app.services.clip_signals import near_dup_hash as nd
from app.services.clip_signals.registry import SignalInputs

pytestmark = [
    pytest.mark.timeout(180),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required"),
]

_BUILD = Path(__file__).resolve().parents[1] / "fixtures" / "clip_signals" / "dups" / "build.py"
_spec = importlib.util.spec_from_file_location("dup_fixture_build", _BUILD)
build = importlib.util.module_from_spec(_spec)
sys.modules["dup_fixture_build"] = build
_spec.loader.exec_module(build)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """``{id: (manifest item, stored signal data)}`` for every fixture item."""
    manifest = build.build(tmp_path_factory.mktemp("dups"))
    measured = {}
    for item in manifest["items"]:
        data = nd.compute(
            SignalInputs(media_id=item["id"], kind=item["kind"], local_path=item["path"])
        )
        assert data is not None, item["id"]
        measured[item["id"]] = (item, data)
    return {"items": measured, "ambiguous": {frozenset(p) for p in manifest["ambiguous"]}}


def _clip(world, item_id, **extra) -> nd.DupClip:
    item, data = world["items"][item_id]
    clip = nd.dup_clip(
        item_id,
        item["kind"],
        clip_signals.merge_signals({}, {nd.NAME: data}),
        **extra,
    )
    assert clip is not None, f"{item_id} has no usable hash"
    return clip


def _labeled_pairs(world):
    """Every same-kind pair of hashable clips: ``(a, b, is_duplicate)``. Ambiguous ones are out."""
    items = world["items"]
    for a, b in itertools.combinations(items, 2):
        (ia, da), (ib, db) = items[a], items[b]
        if ia["kind"] != ib["kind"] or not da["hashes"] or not db["hashes"]:
            continue
        if frozenset((ia["group"], ib["group"])) in world["ambiguous"]:
            continue
        yield a, b, ia["group"] == ib["group"]


def _score(world, limit: int) -> tuple[float, float, list]:
    """Precision, recall and the misses of the pair rule with both thresholds at ``limit``."""
    saved = (nd.MATCH_DISTANCE, nd.MATCH_DISTANCE_NO_TIME)
    nd.MATCH_DISTANCE = nd.MATCH_DISTANCE_NO_TIME = limit
    try:
        tp = fp = fn = 0
        wrong = []
        for a, b, truth in _labeled_pairs(world):
            said = nd.match_distance(_clip(world, a), _clip(world, b)) is not None
            if said and truth:
                tp += 1
            elif said:
                fp += 1
                wrong.append(("false_positive", a, b))
            elif truth:
                fn += 1
                wrong.append(("miss", a, b))
    finally:
        nd.MATCH_DISTANCE, nd.MATCH_DISTANCE_NO_TIME = saved
    return (tp / (tp + fp) if tp + fp else 1.0), (tp / (tp + fn) if tp + fn else 1.0), wrong


# ── calibration: the threshold comes from this sweep ─────────────────────────────────


def test_threshold_sweep_and_chosen_operating_point(world, capsys):
    rows = []
    for limit in (2, 4, 6, 8, 9, 10, 11, 12, 14, 16, 17, 18, 20):
        precision, recall, _ = _score(world, limit)
        rows.append((limit, precision, recall))
    positives = sum(1 for *_, truth in _labeled_pairs(world) if truth)
    negatives = sum(1 for *_, truth in _labeled_pairs(world) if not truth)
    with capsys.disabled():
        print(f"\nnear_dup_hash sweep: {positives} duplicate pairs, {negatives} different pairs")
        for limit, precision, recall in rows:
            print(f"  hamming<={limit:<2}  precision={precision:.3f}  recall={recall:.3f}")

    # The chosen limit sits on the plateau where precision and recall are both perfect: at or
    # above the first limit with full recall, below the first limit that admits a false positive.
    full_recall = min(limit for limit, _, recall in rows if recall == 1.0)
    first_false_positive = min(
        (limit for limit, precision, _ in rows if precision < 1.0), default=99
    )
    assert full_recall <= nd.MATCH_DISTANCE < first_false_positive, rows
    precision, recall, wrong = _score(world, nd.MATCH_DISTANCE)
    assert (precision, recall) == (1.0, 1.0), wrong
    # The stricter untimed threshold stays perfectly precise and keeps most of the recall.
    precision, recall, _ = _score(world, nd.MATCH_DISTANCE_NO_TIME)
    assert precision == 1.0 and recall >= 0.85
    # Headroom: the nearest unambiguous different pair is far above the chosen limit.
    nearest_negative = min(
        _worst_distance(world, a, b) for a, b, truth in _labeled_pairs(world) if not truth
    )
    assert nearest_negative >= nd.MATCH_DISTANCE + 4, nearest_negative


def _worst_distance(world, a, b) -> int:
    ha, hb = _clip(world, a).hashes, _clip(world, b).hashes
    small, large = (ha, hb) if len(ha) <= len(hb) else (hb, ha)
    return max(min(nd.hamming(h, o) for o in large) for h in small)


# ── failure modes 1-4 on real files ──────────────────────────────────────────────────


def test_rotated_phone_video_matches_its_upright_version(world):
    assert nd.match_distance(_clip(world, "v_a"), _clip(world, "v_a_rotated")) is not None


@pytest.mark.parametrize(
    "item_id",
    [
        "p_sky1",
        "p_sky2",
        "p_sky_sun",
        "p_black1",
        "p_black2",
        "p_vig_wall",
        "p_vig_sky",
        "p_vig_dark",
        "v_sky1",
        "v_black",
        "v_vig_wall",
        "v_vig_dark",
    ],
)
def test_frames_with_no_structure_produce_no_hash(world, item_id):
    _, data = world["items"][item_id]
    assert data["hashes"] == [] and data["frames"] >= 1
    assert "sharpness" in data  # still measured: only the hash is withheld


def test_structured_low_detail_frames_are_told_apart(world):
    pair = nd.match_distance(_clip(world, "p_horizon2"), _clip(world, "p_horizon3"))
    assert pair is None


def test_single_frame_clip_hashes_once_and_matches_its_source_video(world):
    one = _clip(world, "v_a_oneframe")
    assert len(one.hashes) == 1
    assert nd.match_distance(one, _clip(world, "v_a")) is not None
    assert nd.match_distance(one, _clip(world, "v_scene3")) is None


def test_wrong_stored_duration_still_hashes_the_first_frame(world):
    item, _ = world["items"]["v_a"]
    data = nd.compute(
        SignalInputs(media_id="x", kind="video", local_path=item["path"], duration_s=900)
    )
    assert data and len(data["hashes"]) >= 1


def test_mirrored_shot_is_not_a_duplicate(world):
    assert nd.match_distance(_clip(world, "p_a"), _clip(world, "p_a_flipped")) is None
    assert nd.match_distance(_clip(world, "v_a"), _clip(world, "v_a_flipped")) is None


def test_a_photo_is_never_paired_with_a_video(world):
    photo, video = _clip(world, "p_a"), _clip(world, "v_a")
    forced = nd.DupClip("v", "video", photo.hashes)
    assert nd.match_distance(photo, forced) is None
    assert nd.match_distance(photo, video) is None


# ── failure modes 5-6: files that go wrong ───────────────────────────────────────────


def test_no_local_copy_is_not_a_result():
    assert nd.compute(SignalInputs(media_id="m", kind="video", local_path=None)) is None


def test_corrupt_file_is_stored_as_empty_so_it_is_not_retried_forever(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg at all")
    data = nd.compute(SignalInputs(media_id="m", kind="image", local_path=str(broken)))
    assert data is not None and data["hashes"] == [] and data["error"]
    clip_data = clip_signals.merge_signals({}, {nd.NAME: data})
    assert clip_signals.missing_signals(clip_data, "image") == []  # done, not pending


def test_undecodable_video_is_stored_as_empty(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 2048)
    data = nd.compute(SignalInputs(media_id="m", kind="video", local_path=str(broken)))
    assert data is not None and data["hashes"] == [] and data["frames"] == 0


def test_heic_without_a_decoder_is_skipped_not_poisoned(tmp_path, monkeypatch):
    heic = tmp_path / "IMG_0001.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)
    monkeypatch.setattr(nd, "_heif_available", lambda: False)
    assert nd.compute(SignalInputs(media_id="m", kind="image", local_path=str(heic))) is None


def test_oversized_photo_is_refused_without_decoding(world, monkeypatch):
    item, _ = world["items"]["p_a"]
    monkeypatch.setattr(nd, "_PHOTO_MAX_PIXELS", 1000)
    data = nd.compute(SignalInputs(media_id="m", kind="image", local_path=item["path"]))
    assert data == {"hashes": [], "frames": 0, "error": "image too large"}


def test_stuck_ffmpeg_is_transient_not_stored(world, monkeypatch):
    item, _ = world["items"]["v_a"]

    def hang(*_a, **_k):
        raise subprocess.TimeoutExpired("ffmpeg", 20)

    monkeypatch.setattr(nd.subprocess, "run", hang)
    assert nd.compute(SignalInputs(media_id="m", kind="video", local_path=item["path"])) is None


def test_exif_rotated_photo_matches_the_upright_photo(tmp_path):
    from PIL import Image

    upright = build.scene(5)
    sideways = tmp_path / "side.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated; the viewer turns it 90 degrees clockwise to upright
    upright.rotate(90, expand=True).save(sideways, "JPEG", quality=92, exif=exif)
    flat = tmp_path / "up.jpg"
    upright.save(flat, "JPEG", quality=92)
    one = nd.compute(SignalInputs(media_id="a", kind="image", local_path=str(sideways)))
    two = nd.compute(SignalInputs(media_id="b", kind="image", local_path=str(flat)))
    distance = nd.hamming(int(one["hashes"][0], 16), int(two["hashes"][0], 16))
    assert distance <= nd.MATCH_DISTANCE


# ── groups and the keep-one choice ───────────────────────────────────────────────────


def test_photo_group_keeps_the_sharp_original_and_drops_the_rest(world):
    clips = [
        _clip(world, name, order=i)
        for i, name in enumerate(["p_a_blur", "p_a_dark", "p_a", "p_a_recompressed", "p_a_crop"])
    ]
    groups = nd.duplicate_groups(clips)
    assert len(groups) == 1
    assert groups[0].keep == "p_a"
    assert set(groups[0].drop) == {"p_a_blur", "p_a_dark", "p_a_recompressed", "p_a_crop"}
    assert groups[0].reason == "sharper"


def test_a_dark_video_twin_is_never_kept(world):
    clips = [_clip(world, "v_a_dark", order=0), _clip(world, "v_a", order=1)]
    (group,) = nd.duplicate_groups(clips)
    assert group.keep == "v_a" and group.drop == ("v_a_dark",)


def test_measured_exposure_from_kri_680_outranks_our_own_reading(world):
    item, data = world["items"]["v_a"]
    analysis = clip_signals.merge_signals(
        {"exposure": {"version": 1, "mean_luma": 5.0, "near_black_share": 0.9}}, {nd.NAME: data}
    )
    dark = nd.dup_clip("v_a", "video", analysis)
    assert dark.near_black_share == 0.9 and dark.mean_luma == 5.0
    assert nd.quality(dark) < nd.quality(_clip(world, "v_a")) / 3


def test_equally_good_clips_keep_the_earlier_attachment(world):
    a = _clip(world, "p_b1", order=3)
    b = _clip(world, "p_b2", order=1)
    (group,) = nd.duplicate_groups([a, b])
    assert group.keep == "p_b2" and group.reason == "earlier"


def test_unrelated_clips_make_no_group(world):
    clips = [_clip(world, f"p_scene{n}", order=n) for n in range(3, 15)]
    assert nd.duplicate_groups(clips) == []


def test_burst_does_not_chain_into_one_group():
    base = 0x00FF00FF00FF00FF
    t = datetime(2026, 11, 1, 10, tzinfo=UTC)
    a = nd.DupClip("a", "image", (base,), order=0, capture_time=t, sharpness=100)
    b = nd.DupClip("b", "image", (base ^ 0x3FF,), order=1, capture_time=t, sharpness=100)
    c = nd.DupClip("c", "image", (base ^ 0xFFFFF,), order=2, capture_time=t, sharpness=100)
    assert nd.match_distance(a, b) == 10 and nd.match_distance(b, c) == 10
    assert nd.match_distance(a, c) is None  # 20 bits apart
    assert [(g.keep, g.drop) for g in nd.duplicate_groups([a, b, c])] == [("a", ("b",))]


# ── capture time ─────────────────────────────────────────────────────────────────────


def test_capture_time_gates_the_pair():
    t0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    h = (0x00FF00FF00FF00FF,)
    near = (
        nd.DupClip("a", "image", h, capture_time=t0),
        nd.DupClip("b", "image", h, capture_time=t0 + timedelta(seconds=30)),
    )
    far = (
        nd.DupClip("a", "image", h, capture_time=t0),
        nd.DupClip("b", "image", h, capture_time=t0 + timedelta(hours=3)),
    )
    assert nd.match_distance(*near) == 0
    assert nd.match_distance(*far) is None  # identical hash, hours apart: not the same moment


def test_a_missing_capture_time_uses_the_stricter_limit():
    h = 0x00FF00FF00FF00FF
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    gap = nd.MATCH_DISTANCE_NO_TIME + 1  # passes timed, fails untimed
    assert gap <= nd.MATCH_DISTANCE
    other = h ^ ((1 << gap) - 1)
    timed = (
        nd.DupClip("a", "image", (h,), capture_time=t0),
        nd.DupClip("b", "image", (other,), capture_time=t0),
    )
    untimed = nd.DupClip("a", "image", (h,)), nd.DupClip("b", "image", (other,), capture_time=t0)
    assert nd.match_distance(*timed) == gap
    assert nd.match_distance(*untimed) is None


def test_naive_and_aware_capture_times_do_not_raise():
    h = (0x00FF00FF00FF00FF,)
    a = nd.DupClip("a", "image", h, capture_time=datetime(2026, 11, 1, 10))
    b = nd.DupClip("b", "image", h, capture_time=datetime(2026, 11, 1, 10, tzinfo=UTC))
    assert nd.match_distance(a, b) is None


def test_assignments_carry_capture_time_and_stay_in_attachment_order(world):
    rows = []
    for i, name in enumerate(["p_a", "p_a_recompressed", "p_scene3"]):
        _, data = world["items"][name]
        rows.append(
            {
                "media_id": name,
                "kind": "image",
                "analysis": clip_signals.merge_signals({}, {nd.NAME: data}),
                "capture": {"capture_time": f"2026-11-01T10:00:{i:02d}+00:00"},
            }
        )
    rows.insert(1, {"media_id": "no-signal", "kind": "image", "analysis": {}})
    rows.append("not a row")
    clips = nd.dup_clips_from_assignments(rows)
    assert [c.media_id for c in clips] == ["p_a", "p_a_recompressed", "p_scene3"]
    assert all(c.capture_time is not None for c in clips)
    assert [(g.keep, g.drop) for g in nd.duplicate_groups(clips)] == [
        ("p_a", ("p_a_recompressed",))
    ]


# ── storage + backfill (pure core) ───────────────────────────────────────────────────


def test_stored_signal_round_trips_and_clears_the_backfill_queue(world):
    _, data = world["items"]["v_a"]
    analysis = {"understanding": {"subject": "dog"}, "audio_marker": 1}
    stored = clip_signals.merge_signals(analysis, {nd.NAME: data})
    assert stored["understanding"] == analysis["understanding"] and stored["audio_marker"] == 1
    assert nd.NAME not in clip_signals.missing_signals(stored, "video")
    assert nd.dup_clip("v_a", "video", stored) is not None


def test_backfill_never_counts_an_unimplemented_stub_as_work():
    from app.services.clip_signals import registry
    from app.services.clip_signals.backfill import Pending

    stub = registry.register(
        registry.SignalSpec(
            name="zz_stub",
            version=0,
            kinds=frozenset({"video"}),
            compute=lambda _i: None,
            implemented=False,
        )
    )
    try:
        pending = Pending("m", "p/m", "video", tuple(clip_signals.missing_signals({}, "video")))
        assert "zz_stub" in pending.missing
        assert "zz_stub" not in cli.implemented_missing(pending, None)
        assert nd.NAME in cli.implemented_missing(pending, None)
        assert cli.implemented_missing(pending, frozenset({"zz_stub"})) == frozenset()
    finally:
        registry._REGISTRY.pop(stub.name)


def _row(**kw):
    return {"media_id": "m1", "gcs_path": "u/1/a.mp4", "kind": "video", "analysis": {}, **kw}


_COMPUTED = {nd.NAME: {"hashes": ["00ff00ff00ff00ff"], "frames": 1}}


def test_fenced_merge_writes_only_the_signals_key_of_the_matching_clip():
    rows = [
        _row(analysis={"understanding": {"s": 1}, "signals": {"other": {"v": 1, "data": {}}}}),
        _row(media_id="m2", gcs_path="u/1/b.mp4"),
    ]
    out = cli.fenced_merge(
        rows, media_id="m1", gcs_path="u/1/a.mp4", generation="7", computed=_COMPUTED
    )
    assert out[1] == rows[1]
    assert out[0]["analysis"]["understanding"] == {"s": 1}
    assert out[0]["analysis"]["signals"]["other"] == {"v": 1, "data": {}}
    assert out[0]["analysis"]["signals"][nd.NAME]["data"] == _COMPUTED[nd.NAME]
    assert rows[0]["analysis"]["signals"].keys() == {"other"}  # input untouched


@pytest.mark.parametrize(
    "row,kwargs",
    [
        (_row(storage_generation="8"), {}),  # object replaced while we measured
        (_row(), {"gcs_path": "u/1/other.mp4"}),  # the path now belongs to a different clip
        (_row(), {"media_id": "gone"}),  # the clip was removed
    ],
)
def test_fenced_merge_refuses_a_stale_target(row, kwargs):
    args = {"media_id": "m1", "gcs_path": "u/1/a.mp4", "generation": "7", **kwargs}
    assert cli.fenced_merge([row], computed=_COMPUTED, **args) is None


def test_fenced_merge_is_idempotent():
    first = cli.fenced_merge(
        [_row(storage_generation="7")],
        media_id="m1",
        gcs_path="u/1/a.mp4",
        generation="7",
        computed=_COMPUTED,
    )
    assert first is not None
    again = cli.fenced_merge(
        first, media_id="m1", gcs_path="u/1/a.mp4", generation="7", computed=_COMPUTED
    )
    assert again is None


def test_dry_run_reports_without_downloading(monkeypatch):
    rows = [_row(), _row(media_id="m2", gcs_path="u/1/b.jpg", kind="image")]
    done = _row(
        media_id="m3", gcs_path="u/1/c.mp4", analysis=clip_signals.merge_signals({}, _COMPUTED)
    )
    monkeypatch.setattr(cli, "_items", lambda *_a: [("id-1", rows + [done])])

    def no_download(*_a, **_k):
        raise AssertionError("dry run must not measure")

    monkeypatch.setattr(cli, "_measure", no_download)
    report = cli.run()
    assert report["mode"] == "dry_run"
    assert report["clips_needing_signals"] == 2
    assert report["missing_by_signal"] == {nd.NAME: 2}
    assert report["next_after_item_id"] == "id-1"


def test_write_run_is_rate_limited_resumable_and_survives_a_bad_clip(monkeypatch):
    calls: list[str] = []
    naps: list[float] = []
    rows = [_row(media_id=f"m{i}", gcs_path=f"u/{i}.mp4") for i in range(3)]
    monkeypatch.setattr(
        cli, "_items", lambda *_a: [("item-a", rows), ("item-b", [_row(media_id="z")])]
    )
    monkeypatch.setattr(cli.time, "sleep", naps.append)

    def process(item_id, row, pending, todo, max_bytes):
        calls.append(pending.media_id)
        if pending.media_id == "m1":
            return "error:RuntimeError"
        return "stored"

    monkeypatch.setattr(cli, "_process", process)
    report = cli.run(write=True, max_clips=3, rate_per_s=4)
    assert calls == ["m0", "m1", "m2"]  # item-b would overshoot the cap, so the run stops before it
    assert report["outcomes"] == {"error:RuntimeError": 1, "stored": 2}
    assert report["next_after_item_id"] == "item-a" and report["stopped_early"] is True
    assert naps == [0.25, 0.25, 0.25]


def test_write_and_dry_run_are_opposites():
    with pytest.raises(SystemExit):
        cli.main(["--write", "--dry-run"])
