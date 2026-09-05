"""V2 golden and allocator invariants for tokenless mixed-gap fillers."""

from __future__ import annotations

import array
import math
import random
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import app.pipeline.silence_cut as silence_cut
from app.config import settings
from app.pipeline.probe import probe_video
from app.pipeline.reframe import reframe_and_export
from app.pipeline.silence_cut import (
    ACOUSTIC_GAP_MAX_S,
    ACOUSTIC_GAP_MIN_S,
    MIN_CUT_S,
    AtomicDisposition,
    Removal,
    build_cut_plan,
    build_cut_plan_comparison,
    plan_event_payload,
    plan_summary,
    remap_words,
)
from app.services.clip_speech import detect_silences_with_status


def w(text: str, start: float, end: float) -> dict:
    return {"text": text, "start_s": start, "end_s": end}


def overlap(lo: float, hi: float, plan) -> float:
    return sum(
        max(0.0, min(hi, removal.end_s) - max(lo, removal.start_s)) for removal in plan.removed
    )


DURATION_S = 10.0
EXPECTED_ACOUSTIC_ISLANDS = [
    (5.778866, 6.209660),
    (7.406100, 7.977846),
]
INCIDENT_ISLAND = EXPECTED_ACOUSTIC_ISLANDS[1]
INCIDENT_WORDS = [
    w("başla", 1.214694, 1.757506),
    w("devam", 1.875034, 2.529025),
    w("konuşma", 3.255057, 4.358934),
    w("şimdi", 8.293356, 9.677755),
]
INCIDENT_SILENCES = [
    (0.0, 1.214694),
    (1.757506, 1.875034),
    (2.529025, 3.255057),
    (4.358934, 5.778866),
    (6.209660, 7.406100),
    (7.977846, 8.293356),
    (9.677755, 10.0),
]


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_synthetic_mixed_gap_source(path: Path) -> None:
    """Create a tiny private fixture with real-word and filler-only tones."""
    real_word_gate = "+".join(
        f"between(t\\,{lo:.6f}\\,{hi:.6f})"
        for lo, hi in [
            (1.214694, 1.757506),
            (1.875034, 2.529025),
            (3.255057, 4.358934),
            (8.293356, 9.677755),
        ]
    )
    audio = (
        f"aevalsrc=0.3*sin(2*PI*440*t)*({real_word_gate})"
        "+0.3*sin(2*PI*880*t)*between(t\\,5.778866\\,6.209660)"
        "+0.3*sin(2*PI*990*t)*between(t\\,7.406100\\,7.977846)"
        ":s=48000:d=10"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x284:r=10:d=10",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _decoded_pcm(path: Path) -> array.array:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "f32le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    samples = array.array("f")
    samples.frombytes(result.stdout)
    return samples


def _tone_amplitude(samples: array.array, frequency_hz: float) -> float:
    """Normalized Goertzel amplitude for one exact synthetic tone."""
    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency_hz / 8000.0)
    previous = 0.0
    previous_two = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_two
        previous_two = previous
        previous = current
    power = previous**2 + previous_two**2 - coefficient * previous * previous_two
    return math.sqrt(max(0.0, power)) / max(1, len(samples))


class TestMixedGapIncidentGolden:
    def test_exact_two_island_geometry_is_atomic_and_output_safe(self):
        comparison = build_cut_plan_comparison(
            INCIDENT_WORDS,
            INCIDENT_SILENCES,
            DURATION_S,
            over_budget_policy="clamp",
        )

        assert comparison.candidate_status == "ready"
        assert comparison.candidate is not None
        assert comparison.baseline.version == 1
        assert comparison.baseline.diagnostics is None
        candidate = comparison.candidate
        assert candidate.version == 2

        diagnostics = candidate.diagnostics
        assert diagnostics is not None
        assert [
            (removal.start_s, removal.end_s, removal.reason)
            for removal in diagnostics.acoustic_candidates
        ] == [(lo, hi, "filler_acoustic") for lo, hi in EXPECTED_ACOUSTIC_ISLANDS]
        assert [
            (decision.island_start_s, decision.island_end_s)
            for decision in diagnostics.acoustic_decisions
        ] == EXPECTED_ACOUSTIC_ISLANDS

        first, incident = diagnostics.acoustic_decisions
        assert first.left_silence_s == pytest.approx(1.419932)
        assert first.right_silence_s == pytest.approx(1.196440)
        assert incident.left_silence_s == pytest.approx(1.196440)
        assert incident.right_silence_s == pytest.approx(0.315510)
        assert {decision.detection for decision in (first, incident)} == {"eligible"}
        assert {decision.reason for decision in (first, incident)} == {"bilateral_silence"}

        dispositions = diagnostics.atomic_dispositions
        assert [
            (record.atom_start_s, record.atom_end_s) for record in dispositions
        ] == EXPECTED_ACOUSTIC_ISLANDS
        assert {record.atom_kind for record in dispositions} == {"filler_acoustic"}
        assert {record.disposition for record in dispositions} == {"selected_full"}

        for island_lo, island_hi in EXPECTED_ACOUSTIC_ISLANDS:
            assert overlap(island_lo, island_hi, comparison.baseline) == pytest.approx(0.0)
            assert overlap(island_lo, island_hi, candidate) == pytest.approx(island_hi - island_lo)
            boundaries = [
                value for removal in candidate.removed for value in (removal.start_s, removal.end_s)
            ]
            assert not any(island_lo < boundary < island_hi for boundary in boundaries)

        assert candidate.time_saved_s <= 5.499 + 1e-9
        assert DURATION_S - candidate.time_saved_s >= 3.0
        kept_s = sum(hi - lo for lo, hi in candidate.keep_segments)
        removed_s = sum(removal.end_s - removal.start_s for removal in candidate.removed)
        assert kept_s + removed_s == pytest.approx(DURATION_S)

        for word in INCIDENT_WORDS:
            assert overlap(word["start_s"], word["end_s"], candidate) == pytest.approx(0.0)
        remapped = remap_words(INCIDENT_WORDS, candidate)
        assert [item["start_s"] for item in remapped] == sorted(
            item["start_s"] for item in remapped
        )
        assert [item["end_s"] - item["start_s"] for item in remapped] == pytest.approx(
            [item["end_s"] - item["start_s"] for item in INCIDENT_WORDS]
        )

    def test_default_entry_point_remains_v1(self):
        implicit = build_cut_plan(
            INCIDENT_WORDS,
            INCIDENT_SILENCES,
            DURATION_S,
            over_budget_policy="clamp",
        )
        explicit = build_cut_plan(
            INCIDENT_WORDS,
            INCIDENT_SILENCES,
            DURATION_S,
            over_budget_policy="clamp",
            mixed_gap_enabled=False,
        )
        assert implicit == explicit
        assert implicit.version == 1
        assert overlap(*INCIDENT_ISLAND, implicit) == 0.0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_synthetic_ffmpeg_detect_plan_and_render_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Real FFmpeg proves both tokenless tones are detected and absent after render."""
    source = tmp_path / "mixed-gap.mov"
    output = tmp_path / "mixed-gap-cut.mp4"
    _make_synthetic_mixed_gap_source(source)

    detection = detect_silences_with_status(
        str(source),
        noise_db=-30.0,
        min_silence_s=0.1,
    )
    assert detection.status == "ok"
    assert len(detection.spans) == len(INCIDENT_SILENCES)
    for actual, expected in zip(detection.spans, INCIDENT_SILENCES, strict=True):
        assert actual == pytest.approx(expected, abs=5e-5)

    candidate = build_cut_plan(
        INCIDENT_WORDS,
        list(detection.spans),
        DURATION_S,
        mixed_gap_enabled=True,
        over_budget_policy="clamp",
    )
    assert candidate.version == 2
    diagnostics = candidate.diagnostics
    assert diagnostics is not None
    assert len(diagnostics.acoustic_candidates) == 2
    for actual, expected in zip(
        diagnostics.acoustic_candidates,
        EXPECTED_ACOUSTIC_ISLANDS,
        strict=True,
    ):
        assert (actual.start_s, actual.end_s) == pytest.approx(expected, abs=5e-5)
        assert actual.reason == "filler_acoustic"
    assert [record.disposition for record in diagnostics.atomic_dispositions] == [
        "selected_full",
        "selected_full",
    ]

    removed_s = sum(removal.end_s - removal.start_s for removal in candidate.removed)
    kept_s = sum(hi - lo for lo, hi in candidate.keep_segments)
    assert removed_s == pytest.approx(candidate.time_saved_s)
    assert removed_s <= candidate.clamp_budget_s + 1e-9  # type: ignore[operator]
    assert kept_s + removed_s == pytest.approx(DURATION_S)
    assert kept_s >= 3.0
    remapped = remap_words(INCIDENT_WORDS, candidate)
    assert [item["start_s"] for item in remapped] == sorted(item["start_s"] for item in remapped)
    assert [item["end_s"] - item["start_s"] for item in remapped] == pytest.approx(
        [item["end_s"] - item["start_s"] for item in INCIDENT_WORDS]
    )

    monkeypatch.setattr(settings, "output_width", 160)
    monkeypatch.setattr(settings, "output_height", 284)
    reframe_and_export(
        input_path=str(source),
        start_s=0.0,
        end_s=DURATION_S,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(output),
        keep_segments=candidate.keep_segments,
        has_audio=True,
        color_trc="bt709",
    )
    rendered_probe = probe_video(str(output))
    assert rendered_probe.has_audio is True
    assert rendered_probe.duration_s == pytest.approx(kept_s, abs=0.05)

    source_pcm = _decoded_pcm(source)
    output_pcm = _decoded_pcm(output)
    # The four fabricated ASR words (440 Hz) survive.  Both tokenless filler
    # signatures (880/990 Hz) fall below two percent of their source strength.
    assert _tone_amplitude(output_pcm, 440.0) >= 0.5 * _tone_amplitude(source_pcm, 440.0)
    for filler_hz in (880.0, 990.0):
        assert _tone_amplitude(output_pcm, filler_hz) < 0.02 * _tone_amplitude(
            source_pcm,
            filler_hz,
        )


class TestMixedGapDetector:
    @pytest.mark.parametrize(
        ("duration", "expected"),
        [
            (ACOUSTIC_GAP_MIN_S, "eligible"),
            (ACOUSTIC_GAP_MAX_S, "eligible"),
            (ACOUSTIC_GAP_MIN_S - 0.001, "rejected"),
            (ACOUSTIC_GAP_MAX_S + 0.001, "rejected"),
        ],
    )
    def test_exact_duration_boundaries(self, duration, expected):
        island_lo = 2.0
        island_hi = island_lo + duration
        decisions = silence_cut._interior_soundful_islands(
            1.0,
            island_hi + 1.0,
            [(1.0, island_lo), (island_hi, island_hi + 1.0)],
        )
        assert len(decisions) == 1
        assert decisions[0].detection == expected

    def test_unsorted_touching_duplicate_silences_are_normalized(self):
        decisions = silence_cut._interior_soundful_islands(
            1.0,
            4.0,
            [(3.0, 4.0), (1.0, 1.5), (1.5, 2.0), (1.0, 2.0), (-3.0, -1.0)],
        )
        assert [(item.island_start_s, item.island_end_s) for item in decisions] == [(2.0, 3.0)]
        assert decisions[0].detection == "eligible"

    def test_invalid_silence_records_are_ignored_without_fabricating_islands(self):
        plan = build_cut_plan(
            [w("one", 1.0, 1.5), w("two", 3.0, 3.5)],
            [(float("nan"), 2.0), (None, 2.5), (2.0, float("inf")), (3.0, 2.0)],
            8.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        assert plan.diagnostics.acoustic_candidates == ()  # type: ignore[union-attr]
        assert plan.removed == []

    @pytest.mark.parametrize(
        "silences",
        [
            [(1.0, 2.0)],
            [(3.0, 4.0)],
            [(1.0, 1.05), (3.0, 4.0)],
            [(1.0, 2.0), (3.0, 3.05)],
        ],
    )
    def test_one_sided_or_short_flank_is_rejected(self, silences):
        decisions = silence_cut._interior_soundful_islands(1.0, 4.0, silences)
        assert decisions
        assert all(item.detection == "rejected" for item in decisions)

    def test_multiple_islands_are_independent_and_ordered(self):
        decisions = silence_cut._interior_soundful_islands(
            0.5,
            5.0,
            [(0.5, 1.0), (1.4, 2.0), (2.2, 3.0), (4.3, 5.0)],
        )
        assert [(item.island_start_s, item.island_end_s) for item in decisions] == [
            (1.0, 1.4),
            (2.0, 2.2),
            (3.0, 4.3),
        ]
        assert [item.detection for item in decisions] == [
            "eligible",
            "eligible",
            "rejected",
        ]

    def test_zero_silence_gate_disables_v2_acoustic_but_not_lexical(self):
        plan = build_cut_plan(
            [w("hello", 1.0, 1.4), w("um", 2.0, 2.3), w("world", 3.0, 3.4)],
            [],
            8.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        diagnostics = plan.diagnostics
        assert diagnostics is not None
        assert diagnostics.acoustic_candidates == ()
        assert diagnostics.acoustic_decisions == ()
        assert diagnostics.lexical_candidates

    def test_wholly_soundful_gap_keeps_v1_acoustic_padding(self):
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8), w("three", 3.0, 3.4)]
        inert_silence = [(1.05, 1.1)]
        baseline = build_cut_plan(words, inert_silence, 8.0)
        candidate = build_cut_plan(
            words,
            inert_silence,
            8.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        acoustic = candidate.diagnostics.acoustic_candidates  # type: ignore[union-attr]
        assert acoustic == (Removal(start_s=1.65, end_s=2.15, reason="filler_acoustic"),)
        assert overlap(1.65, 2.15, baseline) == pytest.approx(0.5)
        assert overlap(1.65, 2.15, candidate) == pytest.approx(0.5)


class TestAtomicAllocator:
    def test_budget_drops_atom_whole_and_carves_it_from_carrier(self, monkeypatch):
        monkeypatch.setattr(silence_cut, "MAX_REMOVAL_FRAC_REQUIRED", 0.2)
        words = [
            w("one", 2.3, 2.8),
            w("two", 3.0, 3.3),
            w("three", 4.0, 4.4),
        ]
        silences = [(0.0, 2.3), (3.3, 3.55), (3.75, 4.0)]
        atom = (3.55, 3.75)
        plan = build_cut_plan(
            words,
            silences,
            10.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )

        assert overlap(*atom, plan) == 0.0
        disposition = next(
            record
            for record in plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]
            if record.atom_start_s == pytest.approx(atom[0])
        )
        assert disposition.disposition == "dropped_budget"

    def test_filler_is_selected_before_long_interior_silence(self, monkeypatch):
        monkeypatch.setattr(silence_cut, "MAX_REMOVAL_FRAC_REQUIRED", 0.2)
        words = [w("one", 0.5, 1.0), w("two", 2.0, 2.4), w("three", 8.0, 8.4)]
        atom = (1.4, 1.7)
        silences = [(1.0, atom[0]), (atom[1], 2.0), (2.4, 8.0)]
        plan = build_cut_plan(
            words,
            silences,
            10.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )

        assert overlap(*atom, plan) == pytest.approx(atom[1] - atom[0])
        assert plan.clamped is True
        assert plan.time_saved_s <= plan.clamp_budget_s + 1e-9  # type: ignore[operator]
        disposition = next(
            record
            for record in plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]
            if record.atom_start_s == pytest.approx(atom[0])
        )
        assert disposition.disposition == "selected_full"

    def test_forced_span_inside_filler_promotes_full_connected_group(self):
        plan = build_cut_plan(
            [w("hello", 1.0, 1.4), w("um", 2.0, 2.4), w("world", 3.0, 3.4)],
            [],
            8.0,
            forced_removals=[{"start_s": 2.1, "end_s": 2.2, "reason": "manual_review"}],
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        record = plan.diagnostics.atomic_dispositions[0]  # type: ignore[union-attr]
        assert record.priority == "protected"
        assert record.disposition == "promoted_protected"
        assert record.group_start_s == pytest.approx(1.88)
        assert record.group_end_s == pytest.approx(2.52)
        assert overlap(record.group_start_s, record.group_end_s, plan) == pytest.approx(0.64)

    def test_short_island_merges_with_selected_silence_or_drops_whole(self):
        adjacent = build_cut_plan(
            [w("one", 1.0, 1.5), w("two", 2.5, 3.0)],
            [(1.5, 2.0), (2.16, 2.5)],
            8.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        assert overlap(2.0, 2.16, adjacent) == pytest.approx(0.16)
        assert all(
            removal.end_s - removal.start_s >= MIN_CUT_S - 1e-9 for removal in adjacent.removed
        )

        isolated = build_cut_plan(
            [w("one", 1.0, 1.5), w("two", 2.06, 2.5)],
            [(1.5, 1.7), (1.86, 2.06)],
            8.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        assert overlap(1.7, 1.86, isolated) == 0.0
        record = isolated.diagnostics.atomic_dispositions[0]  # type: ignore[union-attr]
        assert record.disposition == "dropped_min_cut"

    def test_max_removal_count_drops_later_atom_before_flexible_priority_loss(self, monkeypatch):
        monkeypatch.setattr(silence_cut, "MAX_REMOVALS", 1)
        words = [
            w("a", 0.4, 0.8),
            w("b", 1.8, 2.2),
            w("c", 3.2, 3.6),
            w("d", 8.0, 8.4),
        ]
        silences = [
            (0.8, 1.1),
            (1.4, 1.8),
            (2.2, 2.5),
            (2.8, 3.2),
            (3.6, 8.0),
        ]
        plan = build_cut_plan(
            words,
            silences,
            12.0,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        records = plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]
        assert len(plan.removed) == 1
        assert records[0].disposition == "selected_full"
        assert records[1].disposition == "dropped_max_removals"

    def test_bounded_diagnostics_do_not_weaken_full_atom_validation(self):
        words: list[dict] = []
        for index in range(130):
            base = index * 2.0
            words.append(w(f"word{index}", base, base + 0.5))
            words.append(w("um", base + 1.0, base + 1.3))
        duration = 262.0
        words.append(w("tail", 260.5, 261.0))

        comparison = build_cut_plan_comparison(
            words,
            [],
            duration,
            over_budget_policy="clamp",
        )
        assert comparison.candidate_status == "ready"
        plan = comparison.candidate
        assert plan is not None
        assert len(plan.removed) == silence_cut.MAX_REMOVALS
        assert plan.diagnostics.atomic_dispositions_total == 130  # type: ignore[union-attr]
        assert plan.diagnostics.atomic_dispositions_omitted == 66  # type: ignore[union-attr]

    def test_every_atom_has_one_terminal_disposition_and_group_members_agree(self):
        words = [
            w("start", 1.0, 1.4),
            w("um", 2.0, 2.4),
            w("restart", 2.45, 2.8),
            w("final", 3.4, 3.8),
        ]
        plan = build_cut_plan(
            words,
            [],
            8.0,
            retake_spans=[(1, 2)],
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        records = plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]
        assert len(records) == 2
        assert {(record.group_start_s, record.group_end_s) for record in records} == {
            (records[0].group_start_s, records[0].group_end_s)
        }
        assert {record.disposition for record in records} == {"selected_full"}
        assert all(isinstance(record, AtomicDisposition) for record in records)

    def test_unaffordable_micro_bridge_evicts_budgeted_atom_whole(self, monkeypatch):
        # Protected [1,2], 100ms keep flash, then a 400ms acoustic atom. The
        # budget fits the atom exactly but not the flash, so the atom—not a
        # slice of it—must be evicted.
        monkeypatch.setattr(silence_cut, "MAX_REMOVAL_FRAC_REQUIRED", 0.0401)
        plan = build_cut_plan(
            [w("one", 0.0, 0.5), w("two", 3.0, 3.5)],
            [(0.5, 2.1), (2.5, 3.0)],
            10.0,
            forced_removals=[{"start_s": 1.0, "end_s": 2.0, "reason": "manual"}],
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        atom = (2.1, 2.5)
        assert overlap(*atom, plan) == 0.0
        record = plan.diagnostics.atomic_dispositions[0]  # type: ignore[union-attr]
        assert record.disposition == "dropped_micro_gap"

    def test_safety_bailout_overwrites_every_provisional_disposition(self):
        plan = build_cut_plan(
            [w("hello", 1.0, 1.4), w("um", 2.0, 2.4), w("world", 7.2, 7.6)],
            [],
            8.0,
            forced_removals=[{"start_s": 0.0, "end_s": 7.0, "reason": "manual"}],
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        assert plan.bailout_reason == silence_cut.BAILOUT_OUTPUT_TOO_SHORT
        assert plan.removed == []
        assert {
            record.disposition
            for record in plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]
        } == {"dropped_safety_bailout"}


def _incident_v2_plan():
    plan = build_cut_plan(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        DURATION_S,
        mixed_gap_enabled=True,
        over_budget_policy="clamp",
    )
    assert plan.version == 2
    assert plan.diagnostics is not None
    assert plan.bailout_reason is None
    return plan


def _replace_plan_removals(plan, removals: list[Removal]):
    ordered = sorted(removals, key=lambda removal: (removal.start_s, removal.end_s))
    return replace(
        plan,
        removed=ordered,
        keep_segments=silence_cut._complement(ordered, DURATION_S),
        time_saved_s=sum(removal.end_s - removal.start_s for removal in ordered),
    )


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("partition", "partition"),
        ("saved_duration", "saved_duration"),
        ("group_disposition", "group_disposition_mismatch"),
        ("partial_selected_atom", "partial_selected_atom"),
        ("overlapped_dropped_atom", "overlapped_dropped_atom"),
        ("budget", "budget"),
        ("output_floor", "output_floor"),
        ("word_intrusion", "word_intrusion"),
    ],
)
def test_real_v2_validator_rejects_corrupt_candidate_invariants(
    corruption: str,
    expected_error: str,
) -> None:
    plan = _incident_v2_plan()
    budget_s = plan.clamp_budget_s
    records = plan.diagnostics.atomic_dispositions  # type: ignore[union-attr]

    if corruption == "partition":
        plan = replace(plan, keep_segments=[(0.0, DURATION_S)])
    elif corruption == "saved_duration":
        plan = replace(plan, time_saved_s=plan.time_saved_s + 0.1)
    elif corruption == "group_disposition":
        contradictory = replace(
            records[0],
            atom_start_s=records[0].atom_start_s + 0.01,
            disposition="dropped_budget",
        )
        plan = replace(
            plan,
            diagnostics=replace(
                plan.diagnostics,
                atomic_dispositions=(records[0], contradictory, *records[1:]),
            ),
        )
    elif corruption == "partial_selected_atom":
        group = records[0]
        containing = next(
            removal
            for removal in plan.removed
            if removal.start_s <= group.group_start_s and removal.end_s >= group.group_end_s
        )
        partial = replace(containing, start_s=group.group_start_s + 0.05)
        plan = _replace_plan_removals(
            plan,
            [partial if removal is containing else removal for removal in plan.removed],
        )
    elif corruption == "overlapped_dropped_atom":
        plan = replace(
            plan,
            diagnostics=replace(
                plan.diagnostics,
                atomic_dispositions=(
                    replace(records[0], disposition="dropped_budget"),
                    *records[1:],
                ),
            ),
        )
    elif corruption == "budget":
        budget_s = plan.time_saved_s - 0.1
    elif corruption == "output_floor":
        plan = _replace_plan_removals(
            plan,
            [Removal(start_s=0.0, end_s=7.1, reason="silence")],
        )
    elif corruption == "word_intrusion":
        word = INCIDENT_WORDS[0]
        plan = _replace_plan_removals(
            plan,
            [
                *plan.removed,
                Removal(
                    start_s=word["start_s"],
                    end_s=word["end_s"],
                    reason="silence",
                ),
            ],
        )
        budget_s = None
    else:  # pragma: no cover - the parametrization is the closed mutation set
        raise AssertionError(f"unknown corruption: {corruption}")

    with pytest.raises(ValueError, match=rf"^{expected_error}$"):
        silence_cut._validate_v2_candidate(
            plan,
            duration_s=DURATION_S,
            words=silence_cut._normalize_words(INCIDENT_WORDS),
            forced=[],
            budget_s=budget_s,
        )


def test_comparison_candidate_exception_preserves_baseline(monkeypatch):
    expected = build_cut_plan(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        10.0,
        over_budget_policy="clamp",
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret exception text")

    monkeypatch.setattr(silence_cut, "_build_v2_cut_plan_normalized", explode)
    comparison = build_cut_plan_comparison(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        10.0,
        over_budget_policy="clamp",
    )

    assert comparison.baseline == expected
    assert comparison.candidate is None
    assert comparison.candidate_status == "build_failed"
    assert comparison.candidate_error_class == "RuntimeError"


def test_comparison_validation_exception_has_distinct_bounded_status(monkeypatch):
    expected = build_cut_plan(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        10.0,
        over_budget_policy="clamp",
    )

    def reject(*_args, **_kwargs):
        raise AssertionError("do not expose this text")

    monkeypatch.setattr(silence_cut, "_validate_v2_candidate", reject)
    comparison = build_cut_plan_comparison(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        10.0,
        over_budget_policy="clamp",
    )

    assert comparison.baseline == expected
    assert comparison.candidate is None
    assert comparison.candidate_status == "validation_failed"
    assert comparison.candidate_error_class == "AssertionError"


def test_diagnostics_never_leak_through_legacy_serializers():
    plan = build_cut_plan(
        INCIDENT_WORDS,
        INCIDENT_SILENCES,
        10.0,
        mixed_gap_enabled=True,
        over_budget_policy="clamp",
    )
    assert "diagnostics" not in plan_summary(plan, original_duration_s=10.0)
    assert "diagnostics" not in plan_event_payload(
        plan,
        variant_id="v",
        retake_spans=0,
        applied=False,
    )


def test_seeded_v2_layouts_preserve_partition_budget_and_atomicity():
    rng = random.Random(20260901)
    for _case in range(50):
        duration = rng.uniform(8.0, 30.0)
        words: list[dict] = []
        silences: list[tuple[float, float]] = []
        cursor = rng.uniform(0.2, 0.8)
        while cursor < duration - 2.0 and len(words) < 20:
            end = cursor + rng.uniform(0.15, 0.5)
            words.append(w(rng.choice(["hello", "world", "um", "ııı"]), cursor, end))
            next_start = min(duration - 0.5, end + rng.uniform(0.3, 2.0))
            gap = next_start - end
            if gap >= 0.5 and rng.random() < 0.8:
                island_size = min(rng.uniform(0.15, 0.45), gap - 0.2)
                if island_size >= ACOUSTIC_GAP_MIN_S:
                    island_lo = end + (gap - island_size) / 2.0
                    silences.extend([(end, island_lo), (island_lo + island_size, next_start)])
            cursor = next_start
        if not words:
            continue

        plan = build_cut_plan(
            words,
            silences,
            duration,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
        )
        diagnostics = plan.diagnostics
        assert diagnostics is not None
        if plan.bailout_reason is not None:
            assert plan.removed == []
            assert all(
                record.disposition == "dropped_safety_bailout"
                for record in diagnostics.atomic_dispositions
            )
            continue

        total = sum(removal.end_s - removal.start_s for removal in plan.removed)
        kept = sum(hi - lo for lo, hi in plan.keep_segments)
        assert kept + total == pytest.approx(duration)
        assert total <= plan.clamp_budget_s + 1e-8  # type: ignore[operator]
        assert [word["start_s"] for word in remap_words(words, plan)] == sorted(
            word["start_s"] for word in remap_words(words, plan)
        )

        by_group: dict[tuple[float, float], list[AtomicDisposition]] = {}
        for record in diagnostics.atomic_dispositions:
            by_group.setdefault((record.group_start_s, record.group_end_s), []).append(record)
        for (lo, hi), records in by_group.items():
            covered = overlap(lo, hi, plan)
            if records[0].disposition in {"selected_full", "promoted_protected"}:
                assert covered == pytest.approx(hi - lo)
            else:
                assert covered == pytest.approx(0.0)
            assert len({record.disposition for record in records}) == 1
