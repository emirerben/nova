"""V2 golden and allocator invariants for tokenless mixed-gap fillers."""

from __future__ import annotations

import array
import math
import random
import shutil
import subprocess
from collections import Counter
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
    MAX_REMOVALS,
    MIN_CUT_S,
    MIN_OUTPUT_S,
    TOKEN_PIECE_MIN_S,
    TOKEN_SILENCE_MIN_OVERLAP_S,
    TOKEN_SILENCE_MIN_S,
    AtomicDisposition,
    Removal,
    _normalize_silences,
    _normalize_words,
    _reconcile_words_with_silence,
    _subtract_intervals,
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

        # Bound against the plan's OWN consent budget, never a hardcoded
        # fraction: the 0.55 rail that produced 5.499 here is gone (2026-09-08),
        # so a literal would silently re-pin a cap the engine no longer has.
        assert candidate.clamp_budget_s == pytest.approx(
            DURATION_S - MIN_OUTPUT_S - silence_cut.CLAMP_BUDGET_SLACK_S
        )
        assert candidate.time_saved_s <= candidate.clamp_budget_s + 1e-9
        assert candidate.clamped is False  # the incident set fits without trimming
        assert DURATION_S - candidate.time_saved_s >= MIN_OUTPUT_S
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


# ---------------------------------------------------------------------------------
# Rule 0 property test: silence spans that start/end INSIDE tokens or span whole
# tokens (whisper-1 timestamps are not speech truth; prod 2026-09-08).
# ---------------------------------------------------------------------------------

_RULE0_FILLER_TEXTS = ["um", "Uh,", "ııı,", "Iıı,", "eee", "hmm"]
_RULE0_REAL_TEXTS = ["hello", "world", "deneme", "video", "şimdi", "test."]
_RULE0_SEEDS = 200
_RULE0_EPS = silence_cut._EPS
_RULE0_ALLOWED_STATUSES = {"ready", "validation_failed"}


def _rule0_layout(rng: random.Random) -> tuple[float, list[dict], list[tuple[float, float]]]:
    """Whisper-like tokens plus silencedetect spans anchored INSIDE those tokens.

    The seeded generator above never places silence inside or across a word
    span, so rule 0 is unexercised there.  Here every token draws one of:
    an interior hole (split), a silence ending inside it (head trim), a span
    from inside it to inside a later token (whole tokens swallowed), a long
    silence around the whole token (ghost: never dropped), or a SHORT interior
    silence (< TOKEN_SILENCE_MIN_S: must be ignored).  Ordinary inter-word gap
    silences, soundful islands, lead/trail silence, touching tokens and sliver
    tokens are mixed in so every other rule runs on the reconciled list.
    """
    duration = rng.uniform(5.0, 40.0)
    words: list[dict] = []
    cursor = rng.uniform(0.0, 1.5)
    while cursor < duration - 0.3 and len(words) < 48:
        text = (
            rng.choice(_RULE0_FILLER_TEXTS) if rng.random() < 0.3 else rng.choice(_RULE0_REAL_TEXTS)
        )
        roll = rng.random()
        if roll < 0.35:
            length = rng.uniform(0.05, 0.6)  # ordinary token
        elif roll < 0.8:
            length = rng.uniform(0.7, 2.6)  # stretched token (can hold a hole)
        else:
            length = rng.uniform(0.0, 0.05)  # sliver token
        end = min(duration, cursor + length)
        words.append(w(text, cursor, end))
        gap_roll = rng.random()
        if gap_roll < 0.2:
            gap = 0.0  # touching tokens
        elif gap_roll < 0.6:
            gap = rng.uniform(0.01, 0.5)
        else:
            gap = rng.uniform(0.5, 2.5)
        cursor = end + gap

    silences: list[tuple[float, float]] = []
    count = len(words)
    for index, word in enumerate(words):
        word_lo, word_hi = word["start_s"], word["end_s"]
        roll = rng.random()
        if roll < 0.3:
            # Interior hole: starts inside, ends inside or past the token end.
            lo = rng.uniform(word_lo, word_hi)
            hi = lo + rng.uniform(TOKEN_SILENCE_MIN_S - 0.1, 2.5)
            if rng.random() < 0.6:
                hi = min(hi, word_hi - rng.uniform(0.0, 0.3))
            if hi > lo:
                silences.append((lo, min(duration, hi)))
        elif roll < 0.5:
            # Head: starts before the token (gap or previous token), ends inside.
            hi = rng.uniform(word_lo, word_hi)
            lo = hi - rng.uniform(TOKEN_SILENCE_MIN_S - 0.1, 2.5)
            silences.append((max(0.0, lo), hi))
        elif roll < 0.65:
            # Whole tokens swallowed: from inside token i to inside token j > i.
            later = min(count - 1, index + rng.randint(1, 3))
            lo = rng.uniform(word_lo, word_hi)
            later_lo, later_hi = words[later]["start_s"], words[later]["end_s"]
            if rng.random() < 0.7:
                hi = rng.uniform(later_lo, later_hi)
            else:
                hi = later_hi + rng.uniform(0.0, 0.5)
            if hi > lo:
                silences.append((lo, min(duration, hi)))
        elif roll < 0.75:
            # Ghost: the whole token sits inside one long silence.
            lo = word_lo - rng.uniform(0.0, 1.0)
            hi = word_hi + rng.uniform(0.0, 1.0)
            if hi - lo < TOKEN_SILENCE_MIN_S:
                hi = lo + TOKEN_SILENCE_MIN_S + rng.uniform(0.0, 0.5)
            silences.append((max(0.0, lo), min(duration, hi)))
        elif roll < 0.85:
            # Short interior silence: below TOKEN_SILENCE_MIN_S, rule 0 must skip it.
            lo = rng.uniform(word_lo, word_hi)
            hi = lo + rng.uniform(0.1, TOKEN_SILENCE_MIN_S - 0.01)
            silences.append((lo, min(duration, hi)))
        # Ordinary gap silence after the token, sometimes with a soundful island.
        next_lo = words[index + 1]["start_s"] if index + 1 < count else duration
        gap = next_lo - word_hi
        if gap > 0.2 and rng.random() < 0.6:
            gap_lo = word_hi + rng.uniform(0.0, min(0.3, gap / 3))
            gap_hi = next_lo - rng.uniform(0.0, min(0.3, gap / 3))
            if rng.random() < 0.5 and gap_hi - gap_lo > 0.6:
                island = rng.uniform(ACOUSTIC_GAP_MIN_S, min(0.45, (gap_hi - gap_lo) / 2))
                mid = (gap_lo + gap_hi) / 2
                silences.append((gap_lo, mid - island / 2))
                silences.append((mid + island / 2, gap_hi))
            elif gap_hi > gap_lo:
                silences.append((gap_lo, gap_hi))
    if rng.random() < 0.7 and words[0]["start_s"] > 0.05:
        silences.append((0.0, rng.uniform(0.0, words[0]["start_s"])))
    if rng.random() < 0.7 and words[-1]["end_s"] < duration - 0.05:
        silences.append((rng.uniform(words[-1]["end_s"], duration), duration))
    rng.shuffle(silences)  # normalization must not depend on input order
    return duration, words, silences


def _assert_rule0_candidate_invariants(
    candidate,
    *,
    duration: float,
    words: list[dict],
    silences: list[tuple[float, float]],
) -> Counter:
    """Every invariant one READY V2 candidate must satisfy; returns coverage."""
    coverage: Counter = Counter()
    diagnostics = candidate.diagnostics
    assert diagnostics is not None
    cut_words = _normalize_words(words)
    normalized_silences = _normalize_silences(silences, duration)
    reconciled, adjustments = _reconcile_words_with_silence(cut_words, normalized_silences)
    long_spans = [
        (lo, hi) for lo, hi in normalized_silences if hi - lo >= TOKEN_SILENCE_MIN_S - _RULE0_EPS
    ]

    # Diagnostics carry exactly the rule-0 result the plan was built on.
    assert diagnostics.token_adjustments_total == len(adjustments)
    assert diagnostics.token_adjustments == tuple(
        adjustments[: silence_cut._MAX_DIAGNOSTIC_TOKEN_ADJUSTMENTS]
    )
    assert diagnostics.token_adjustments_omitted == max(
        0, len(adjustments) - silence_cut._MAX_DIAGNOSTIC_TOKEN_ADJUSTMENTS
    )
    assert diagnostics.token_carved_s == pytest.approx(
        sum(item.carved_s for item in adjustments), abs=1e-9
    )

    # Token count only grows: no token is ever dropped, order is kept, and
    # every original token survives as at least one remnant of itself.
    assert len(reconciled) == len(cut_words) + sum(len(item.pieces) - 1 for item in adjustments)
    assert len(reconciled) >= len(cut_words)
    assert [(item.start, item.end) for item in reconciled] == sorted(
        (item.start, item.end) for item in reconciled
    )
    for original in cut_words:
        assert any(
            piece.text == original.text
            and piece.start >= original.start - _RULE0_EPS
            and piece.end <= original.end + _RULE0_EPS
            for piece in reconciled
        ), original
        if any(
            lo <= original.start + _RULE0_EPS and original.end <= hi + _RULE0_EPS
            for lo, hi in long_spans
        ):
            coverage["ghost_token"] += 1
            assert original in reconciled, original

    for item in adjustments:
        coverage[item.kind] += 1
        assert item.pieces, item
        assert all(hi - lo >= TOKEN_PIECE_MIN_S - _RULE0_EPS for lo, hi in item.pieces), item
        assert all(
            lo >= item.original_start_s - _RULE0_EPS and hi <= item.original_end_s + _RULE0_EPS
            for lo, hi in item.pieces
        ), item
        assert list(item.pieces) == sorted(item.pieces)
        assert all(nxt[0] > prev[1] + _RULE0_EPS for prev, nxt in zip(item.pieces, item.pieces[1:]))
        expected_carved = (item.original_end_s - item.original_start_s) - sum(
            hi - lo for lo, hi in item.pieces
        )
        assert item.carved_s == pytest.approx(expected_carved, abs=1e-9)
        assert item.carved_s >= TOKEN_SILENCE_MIN_OVERLAP_S - 1e-6, item
        if len(item.pieces) > 1:
            assert item.kind == "split", item
        else:
            # A single piece means the carve was still interior and the sliver
            # on one side fell under TOKEN_PIECE_MIN_S; the kind names the end
            # that survived. Both ends trimmed is impossible: rule 0 performs
            # exactly one interior carve.
            ((piece_lo, piece_hi),) = item.pieces
            trimmed_head = piece_lo > item.original_start_s + _RULE0_EPS
            trimmed_tail = piece_hi < item.original_end_s - _RULE0_EPS
            assert trimmed_head != trimmed_tail, item
            assert item.kind == ("trim_head" if trimmed_head else "trim_tail"), item
        # Every carved region lies inside a silence span: it is a union of
        # >= TOKEN_SILENCE_MIN_OVERLAP_S overlaps with >= TOKEN_SILENCE_MIN_S
        # spans, plus only absorbed voiced remnants shorter than
        # TOKEN_PIECE_MIN_S.
        carved_regions = _subtract_intervals(
            item.original_start_s, item.original_end_s, item.pieces
        )
        assert carved_regions, item
        for region_lo, region_hi in carved_regions:
            soundful = _subtract_intervals(region_lo, region_hi, normalized_silences)
            assert all(hi - lo < TOKEN_PIECE_MIN_S for lo, hi in soundful), (item, soundful)
            assert any(
                min(region_hi, span_hi) - max(region_lo, span_lo)
                >= TOKEN_SILENCE_MIN_OVERLAP_S - 1e-6
                for span_lo, span_hi in long_spans
            ), (item, (region_lo, region_hi))

    # Plan geometry: exact partition, time_saved, ordering, floors, budget.
    if candidate.bailout_reason is not None:
        assert candidate.removed == []
        assert candidate.keep_segments == [(0.0, duration)]
    removed = candidate.removed
    assert all(nxt.start_s > prev.end_s + _RULE0_EPS for prev, nxt in zip(removed, removed[1:]))
    assert all(removal.end_s - removal.start_s >= MIN_CUT_S - _RULE0_EPS for removal in removed)
    assert all(
        removal.start_s >= -_RULE0_EPS and removal.end_s <= duration + _RULE0_EPS
        for removal in removed
    )
    assert len(removed) <= MAX_REMOVALS
    cursor = 0.0
    for _kind, lo, hi in sorted(
        [
            *(("keep", lo, hi) for lo, hi in candidate.keep_segments),
            *(("cut", removal.start_s, removal.end_s) for removal in removed),
        ],
        key=lambda entry: entry[1],
    ):
        assert abs(lo - cursor) < 1e-6
        assert hi > lo
        cursor = hi
    assert abs(cursor - duration) < 1e-6
    total = sum(removal.end_s - removal.start_s for removal in removed)
    assert total == pytest.approx(candidate.time_saved_s, abs=1e-7)
    assert candidate.clamp_budget_s is not None
    assert total <= candidate.clamp_budget_s + 1e-8
    if removed:
        coverage["seeds_with_removals"] += 1

    # No removal overlaps a RECONCILED word except inside a selected
    # filler/retake group (the validator contract, recomputed independently).
    assert diagnostics.atomic_dispositions_omitted == 0, diagnostics.atomic_dispositions_total
    by_group: dict[tuple[float, float], list[AtomicDisposition]] = {}
    for record in diagnostics.atomic_dispositions:
        by_group.setdefault((record.group_start_s, record.group_end_s), []).append(record)
    allowed_word_spans: list[tuple[float, float]] = []
    for (group_lo, group_hi), records in by_group.items():
        assert len({record.disposition for record in records}) == 1
        if records[0].disposition in {"selected_full", "promoted_protected"} and any(
            record.atom_kind in {"filler_lexical", "retake"} for record in records
        ):
            allowed_word_spans.append((group_lo, group_hi))
    for removal in removed:
        for word in reconciled:
            if min(removal.end_s, word.end) - max(removal.start_s, word.start) <= _RULE0_EPS:
                continue
            assert any(
                word.start >= allowed_lo - _RULE0_EPS and word.end <= allowed_hi + _RULE0_EPS
                for allowed_lo, allowed_hi in allowed_word_spans
            ), (removal, word)
        # A carve is FFmpeg silence by construction: whatever is cut INSIDE an
        # original token outside those groups is silence up to absorbed slivers.
        for original in cut_words:
            inside_lo = max(removal.start_s, original.start)
            inside_hi = min(removal.end_s, original.end)
            if inside_hi - inside_lo <= _RULE0_EPS:
                continue
            coverage["removals_through_carves"] += 1
            for lo, hi in _subtract_intervals(inside_lo, inside_hi, allowed_word_spans):
                for sound_lo, sound_hi in _subtract_intervals(lo, hi, normalized_silences):
                    assert sound_hi - sound_lo < TOKEN_PIECE_MIN_S, (removal, original)

    # Remap on the ORIGINAL tokens: a cut through a carve shrinks the token,
    # never inverts it, and start order survives.
    remapped = remap_words(words, candidate)
    assert all(entry["end_s"] >= entry["start_s"] - _RULE0_EPS for entry in remapped)
    starts = [entry["start_s"] for entry in remapped]
    assert starts == sorted(starts)
    return coverage


def test_seeded_silence_inside_token_layouts_hold_rule0_invariants():
    coverage: Counter = Counter()
    statuses: Counter = Counter()
    for seed in range(_RULE0_SEEDS):
        rng = random.Random(seed)
        duration, words, silences = _rule0_layout(rng)
        layout = f"seed={seed} duration={duration!r} words={words!r} silences={silences!r}"

        comparison = build_cut_plan_comparison(
            words, silences, duration, over_budget_policy="clamp"
        )
        statuses[comparison.candidate_status] += 1
        assert comparison.candidate_status in _RULE0_ALLOWED_STATUSES, (
            comparison.candidate_status,
            comparison.candidate_error_class,
            layout,
        )
        # V1 is untouched by rule 0: the comparison baseline IS the default
        # entry point's plan, byte for byte.
        assert comparison.baseline == build_cut_plan(
            words, silences, duration, over_budget_policy="clamp"
        ), layout
        assert comparison.baseline.version == 1
        assert comparison.baseline.diagnostics is None
        if comparison.candidate_status != "ready":
            continue
        candidate = comparison.candidate
        assert candidate is not None
        assert candidate.version == 2
        try:
            coverage.update(
                _assert_rule0_candidate_invariants(
                    candidate, duration=duration, words=words, silences=silences
                )
            )
        except AssertionError as exc:
            raise AssertionError(f"{layout}\n{exc}") from exc

    # The corpus must actually exercise rule 0, or the invariants are vacuous.
    assert statuses["ready"] >= _RULE0_SEEDS // 2, statuses
    # Rule 0 only ever performs a dominated interior carve, so "split" (and the
    # single-sliver variants of it) are the only kinds it can produce.
    # "trim_both" would mean two carves in one token, which is refused.
    # ghost_token counts tokens lying wholly inside silence: the loop above
    # already asserts each of those is returned untouched, and the generator
    # must keep producing them.
    assert coverage["split"] > 0, coverage
    assert coverage["ghost_token"] > 0, coverage
    assert coverage["trim_both"] == 0, coverage
    assert coverage["seeds_with_removals"] >= _RULE0_SEEDS // 2, coverage
    assert coverage["removals_through_carves"] > 0, coverage
