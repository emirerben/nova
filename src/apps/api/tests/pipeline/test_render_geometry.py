"""measure_caption line layout: keep-together + widow penalties (plan 011, B).

These pin the fit-first contract: with no keep_together pairs and no widow
penalty the wrapping is byte-identical to the pre-feature scoring; when active,
a split that FITS always beats one that overflows (so honoring a pair never
forces an extra shrink), and among fitting splits the layout avoids breaking a
kept pair or stranding a lone short word.
"""

from __future__ import annotations

import pytest

from app.pipeline.render_geometry import (
    NormalizedBox,
    ProtectedRegion,
    _is_widow,
    _split_breaks_pair,
    _valid_keep_together,
    choose_caption_y_frac,
    measure_caption,
)

_KW = dict(
    font_family="Montserrat Bold",
    font_size_px=64,
    width_frac=0.88,
    y_frac=0.705,
    max_lines=2,
)


# ── kill switch: bare call is byte-identical ──────────────────────────────────


def test_no_penalties_is_byte_identical() -> None:
    text = "number one is Messi the goat"
    a = measure_caption(text, **_KW)
    b = measure_caption(text, keep_together=None, penalize_widows=False, **_KW)
    assert a.lines == b.lines
    assert a.font_size_px == b.font_size_px
    assert a.box.as_dict() == b.box.as_dict()


def test_only_stale_pairs_is_byte_identical() -> None:
    text = "number one is Messi the goat"
    baseline = measure_caption(text, **_KW)
    # All pairs out of range / degenerate → filtered to empty → no penalties.
    stale = measure_caption(text, keep_together=[(10, 11), (3, 3), (-1, 2)], **_KW)
    assert stale.lines == baseline.lines


# ── keep_together ─────────────────────────────────────────────────────────────


def test_keep_together_pair_not_split_across_lines() -> None:
    text = "number one is Messi the goat"
    measured = measure_caption(text, keep_together=[(0, 1)], **_KW)
    if len(measured.lines) == 2:
        joined = " | ".join(measured.lines)
        assert "number one" in measured.lines[0] or "number one" in measured.lines[1], joined


def test_overflow_still_wins_over_a_kept_pair() -> None:
    # A pair too wide to share a line with anything must not blank the output —
    # a fitting (necessarily split) layout wins over an overflowing kept one.
    measured = measure_caption(
        "supercalifragilistic expialidocious wordsmith", keep_together=[(0, 1)], **_KW
    )
    assert measured.lines  # produced a real layout, did not crash or blank


# ── widow penalty ─────────────────────────────────────────────────────────────


def test_widow_penalty_avoids_lone_short_word() -> None:
    # "I" would be a one-char widow line; the penalty prefers a balanced split.
    text = "I am the greatest"
    with_widow = measure_caption(text, penalize_widows=True, **_KW)
    for line in with_widow.lines:
        if len(with_widow.lines) == 2:
            assert not (len(line.split()) == 1 and len(line.strip()) <= 3), with_widow.lines


# ── helpers ───────────────────────────────────────────────────────────────────


def test_valid_keep_together_drops_out_of_range_and_degenerate() -> None:
    assert _valid_keep_together([(0, 1), (1, 3)], 4) == [(0, 1), (1, 3)]
    assert _valid_keep_together([(3, 3), (2, 1), (-1, 0), (2, 9)], 4) == []
    assert _valid_keep_together(None, 4) == []


def test_split_breaks_pair_only_inside_the_span() -> None:
    assert _split_breaks_pair(1, [(0, 1)]) is True  # break between w0|w1 splits (0,1)
    assert _split_breaks_pair(2, [(0, 1)]) is False  # break after the pair is fine
    assert _split_breaks_pair(1, [(1, 3)]) is False  # break before the pair is fine
    assert _split_breaks_pair(2, [(1, 3)]) is True  # break inside (1,3)


def test_is_widow_requires_three_plus_words_and_a_short_lone_line() -> None:
    assert _is_widow(("I", "really love it")) is True
    assert _is_widow(("I really", "love it")) is False  # no lone line
    assert _is_widow(("a", "b")) is False  # fewer than 3 words total
    assert _is_widow(("hello", "there friend")) is False  # lone line is not short


# ── Feature C: face-aware caption placement (plan 011) ────────────────────────

_CANDIDATES = (0.705, 0.62, 0.78, 0.55, 0.86)


def _face(box: NormalizedBox) -> ProtectedRegion:
    return ProtectedRegion(0.0, 10.0, box, kind="face")


def _receipt(**kw) -> dict:
    base = {"attempted": 5, "detected": kw.get("detected", 5), "timed_out": False}
    base.update(kw)
    return base


def test_coverage_by_is_fraction_of_this_box_not_iou() -> None:
    """OV-6: coverage denominator is the caption box's own area, so a big face
    band reports HIGH overlap where IoU would wrongly dilute it to 'clear'."""
    caption = NormalizedBox(0.4, 0.4, 0.6, 0.6)  # area 0.04
    big_face = NormalizedBox(0.0, 0.0, 1.0, 1.0)  # swallows the caption
    assert caption.coverage_by(big_face) == pytest.approx(1.0)
    # IoU would read 0.04 (< the 5% gate) and certify the caption 'clear' — wrong.
    assert caption.iou(big_face) == pytest.approx(0.04)


def test_coverage_by_hits_the_five_percent_boundary_exactly() -> None:
    caption = NormalizedBox(0.0, 0.0, 0.5, 0.2)  # area 0.1
    protector = NormalizedBox(0.0, 0.0, 0.05, 0.1)  # intersection 0.005
    assert caption.coverage_by(protector) == pytest.approx(0.05)


def test_well_framed_video_keeps_the_preset() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.05, 0.65, 0.30))] * 5  # high in frame
    chosen, receipt = choose_caption_y_frac(faces, _receipt(), [probe], [], _CANDIDATES)
    assert chosen == pytest.approx(0.705)
    assert receipt["status"] == "well_framed"
    assert receipt["coverage"] == pytest.approx(0.0)
    assert receipt["candidate_index"] == 0


def test_low_face_moves_the_caption_up() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.60, 0.65, 0.88))] * 5  # low, on the band
    chosen, receipt = choose_caption_y_frac(faces, _receipt(), [probe], [], _CANDIDATES)
    assert chosen == pytest.approx(0.55)  # first clear candidate
    assert chosen < 0.705  # moved UP off the face
    assert receipt["status"] == "moved"
    assert receipt["coverage"] <= 0.05


def test_five_percent_coverage_is_accepted_at_the_boundary() -> None:
    probe = NormalizedBox(0.25, 0.6, 0.75, 0.8)  # area 0.1, bottom == candidate #0
    band = NormalizedBox(0.25, 0.79, 0.75, 0.90)  # 0.01 tall inside probe → 5%
    chosen, receipt = choose_caption_y_frac([_face(band)] * 5, _receipt(), [probe], [], (0.8,))
    assert chosen == pytest.approx(0.8)
    assert receipt["status"] == "well_framed"
    assert receipt["coverage"] == pytest.approx(0.05)


def test_no_safe_candidate_returns_least_coverage_best_effort() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)  # area 0.05, full-width overlap
    band = NormalizedBox(0.30, 0.40, 0.70, 0.80)  # swallows every low candidate
    chosen, receipt = choose_caption_y_frac([_face(band)] * 5, _receipt(), [probe], [], _CANDIDATES)
    assert receipt["status"] == "best_effort"
    # 0.86 clips only the band's lower edge → strictly least coverage.
    assert chosen == pytest.approx(0.86)
    assert receipt["candidate_index"] == 4


def test_boundary_candidates_respect_platform_chrome() -> None:
    probe = NormalizedBox(0.3, 0.05, 0.7, 0.30)  # 0.25 tall
    band = NormalizedBox(0.35, 0.35, 0.65, 0.55)  # mid-frame
    chosen, receipt = choose_caption_y_frac(
        [_face(band)] * 5, _receipt(), [probe], [], (0.30, 0.90)
    )
    # 0.30 puts the box top at 0.05 (< the 0.10 top-chrome margin) → rejected;
    # 0.90 is inclusive at the bottom edge and clear of the mid band.
    assert chosen == pytest.approx(0.90)
    assert receipt["status"] == "moved"


def test_title_box_overlap_also_blocks_a_candidate() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.05, 0.65, 0.30))] * 5  # face is clear
    # A title parked exactly where the preset caption would sit forces a move.
    title = ProtectedRegion(0.0, 10.0, NormalizedBox(0.2, 0.55, 0.8, 0.72), kind="title")
    chosen, receipt = choose_caption_y_frac(faces, _receipt(), [probe], [title], _CANDIDATES)
    assert chosen != pytest.approx(0.705)
    assert receipt["status"] == "moved"


def test_no_face_returns_preset_with_reason() -> None:
    chosen, receipt = choose_caption_y_frac(
        [], _receipt(detected=0), [NormalizedBox(0.3, 0.58, 0.7, 0.705)], [], _CANDIDATES
    )
    assert chosen == pytest.approx(0.705)
    assert receipt["status"] == "preset"
    assert receipt["reason"] == "no_face"


def test_transient_face_below_presence_floor_is_no_face() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.60, 0.65, 0.88))] * 2  # 2 / 8 decoded = 0.25
    chosen, receipt = choose_caption_y_frac(
        faces, _receipt(attempted=8, decoded=8, detected=2), [probe], [], _CANDIDATES
    )
    assert chosen == pytest.approx(0.705)
    assert receipt["reason"] == "no_face"
    assert receipt["face_presence"] == pytest.approx(0.25)


def test_timeout_and_error_map_to_distinct_reasons() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    _, timed = choose_caption_y_frac(
        [], {"timed_out": True, "attempted": 8}, [probe], [], _CANDIDATES
    )
    assert timed["reason"] == "sampler_timeout"
    _, errored = choose_caption_y_frac(
        [_face(NormalizedBox(0.3, 0.6, 0.7, 0.9))] * 5,
        {"timed_out": False, "worker_error": "rc_1:boom", "attempted": 8, "decoded": 8},
        [probe],
        [],
        _CANDIDATES,
    )
    assert errored["reason"] == "sampler_error"


def test_insufficient_anchors_uses_the_decoded_denominator() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.60, 0.65, 0.88))] * 2
    chosen, receipt = choose_caption_y_frac(
        faces, {"timed_out": False, "attempted": 2, "decoded": 2}, [probe], [], _CANDIDATES
    )
    assert chosen == pytest.approx(0.705)
    assert receipt["reason"] == "insufficient_anchors"
    assert receipt["decoded"] == 2


def test_decoded_falls_back_to_attempted_when_absent() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    faces = [_face(NormalizedBox(0.35, 0.05, 0.65, 0.30))] * 5  # 5 / 8 attempted = 0.625
    chosen, receipt = choose_caption_y_frac(
        faces, {"timed_out": False, "attempted": 8}, [probe], [], _CANDIDATES
    )
    assert receipt["decoded"] == 8  # denominator fell back to attempted
    assert chosen == pytest.approx(0.705)


def test_receipt_embeds_the_raw_sampler_receipt() -> None:
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)
    sampler = {"timed_out": False, "attempted": 5, "decoded": 5, "elapsed_ms": 42}
    _, receipt = choose_caption_y_frac([], sampler, [probe], [], _CANDIDATES)
    assert receipt["face_sampler"] == sampler  # QUAL-2: broken worker stays visible


def test_best_effort_never_prefers_a_chrome_unsafe_candidate() -> None:
    """Adversarial regression: when NO candidate is both clear and chrome-safe,
    the fallback must not hand back a position the chrome gate just rejected —
    a caption under the platform UI is worse than one overlapping the face."""
    probe = NormalizedBox(0.3, 0.05, 0.7, 0.30)  # 0.25 tall → y=0.30 tops out at 0.05
    band = NormalizedBox(0.30, 0.35, 0.70, 0.95)  # swallows the whole low zone
    chosen, receipt = choose_caption_y_frac(
        [_face(band)] * 5, _receipt(), [probe], [], (0.705, 0.62, 0.78, 0.55, 0.30)
    )
    assert receipt["status"] == "best_effort"
    # y=0.30 has the LOWEST coverage (0.0) but fails chrome — it must not win.
    assert chosen != pytest.approx(0.30)
    picked = receipt["evaluated"][receipt["candidate_index"]]
    assert picked["clears_chrome"] is True
    # Among the chrome-safe candidates it still takes the least-covered one.
    safe = [e for e in receipt["evaluated"] if e["clears_chrome"]]
    assert picked["coverage"] == min(e["coverage"] for e in safe)


def test_best_effort_falls_back_to_coverage_when_nothing_clears_chrome() -> None:
    """If NO candidate clears chrome, coverage alone decides (still deterministic)."""
    probe = NormalizedBox(0.3, 0.0, 0.7, 0.30)  # 0.30 tall → every low y tops out < 0.10
    band = NormalizedBox(0.30, 0.20, 0.70, 0.28)
    chosen, receipt = choose_caption_y_frac(
        [_face(band)] * 5, _receipt(), [probe], [], (0.30, 0.32)
    )
    assert receipt["status"] == "best_effort"
    assert all(not e["clears_chrome"] for e in receipt["evaluated"])
    assert chosen == pytest.approx(receipt["evaluated"][receipt["candidate_index"]]["y_frac"])


def test_short_cue_box_can_veto_a_candidate_the_tall_probe_would_pass() -> None:
    """Red-team regression: the gate divides by the probe's OWN area, so a tall
    two-line box under-reports a face band sitting near the caption's bottom edge.
    Probing every distinct cue shape must let the SHORT one-line cue veto it."""
    y = 0.55
    tall = NormalizedBox(0.3, y - 0.14, 0.7, y)  # 2-line cue
    short = NormalizedBox(0.3, y - 0.05, 0.7, y)  # 1-line cue at the SAME y
    band = NormalizedBox(0.3, 0.522, 0.7, 0.528)  # thin sliver near the bottom edge

    # The inversion itself: tall says "clear", short says "on the face".
    assert tall.coverage_by(band) <= 0.05
    assert short.coverage_by(band) > 0.05

    faces = [_face(band)] * 5
    # Tall probe ALONE would accept y=0.55 …
    tall_only, _ = choose_caption_y_frac(faces, _receipt(), [tall], [], (y, 0.86))
    assert tall_only == pytest.approx(y)
    # … but the full probe set must reject it and move on.
    chosen, receipt = choose_caption_y_frac(faces, _receipt(), [tall, short], [], (y, 0.86))
    assert chosen != pytest.approx(y)
    assert receipt["status"] == "moved"


def test_probe_set_reports_the_worst_coverage_across_shapes() -> None:
    y = 0.55
    tall = NormalizedBox(0.3, y - 0.14, 0.7, y)
    short = NormalizedBox(0.3, y - 0.05, 0.7, y)
    band = NormalizedBox(0.3, 0.522, 0.7, 0.528)
    _, receipt = choose_caption_y_frac([_face(band)] * 5, _receipt(), [tall, short], [], (y,))
    # The recorded coverage is the SHORT cue's (worst), not the tall probe's.
    assert receipt["evaluated"][0]["coverage"] == pytest.approx(short.coverage_by(band), abs=1e-5)


def test_single_spurious_detection_cannot_inflate_the_face_band() -> None:
    """Red-team regression: Haar occasionally fires once on background texture.
    A blind union of every detection would stretch the band across the frame and
    drag the caption off a face that never moved."""
    real = NormalizedBox(0.35, 0.10, 0.65, 0.34)  # the recurring face, up high
    spurious = NormalizedBox(0.02, 0.60, 0.22, 0.80)  # one-off, bottom-left
    faces = [_face(real)] * 5 + [_face(spurious)]
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)

    chosen, receipt = choose_caption_y_frac(
        faces, _receipt(attempted=6, decoded=6, detected=6), [probe], [], _CANDIDATES
    )

    band = NormalizedBox(**receipt["face_band"])
    # The band is the recurring face only — the outlier is excluded entirely.
    assert band.bottom < 0.5
    assert band.left >= 0.3
    # And the preset still wins, because the real face never touched the band.
    assert chosen == pytest.approx(0.705)
    assert receipt["status"] == "well_framed"


def test_presence_counts_the_recurring_face_not_every_detection() -> None:
    """Two scattered one-off detections must not add up to a 'dominant' face."""
    a = NormalizedBox(0.05, 0.10, 0.20, 0.25)
    b = NormalizedBox(0.75, 0.70, 0.92, 0.88)
    probe = NormalizedBox(0.3, 0.58, 0.7, 0.705)

    _, receipt = choose_caption_y_frac(
        [_face(a), _face(b)], _receipt(attempted=3, decoded=3, detected=2), [probe], [], _CANDIDATES
    )

    # 2 detections / 3 decoded would clear 60% as a raw count, but the largest
    # CLUSTER is 1/3 → no dominant face.
    assert receipt["reason"] == "no_face"
    assert receipt["face_presence"] == pytest.approx(1 / 3, abs=0.01)
    assert receipt["detections"] == 2


def test_dominant_face_cluster_tie_breaks_to_the_earliest_cluster() -> None:
    """Two equal-size clusters must resolve deterministically to the first one,
    or the same footage could place captions differently run to run."""
    from app.pipeline.render_geometry import _dominant_face_cluster

    left = NormalizedBox(0.05, 0.10, 0.25, 0.30)
    right = NormalizedBox(0.70, 0.60, 0.90, 0.80)
    cluster = _dominant_face_cluster([left, left, right, right])

    assert len(cluster) == 2
    assert cluster[0] == left  # earliest cluster wins the tie
