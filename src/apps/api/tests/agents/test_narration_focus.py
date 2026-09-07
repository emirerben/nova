import pytest

from app.agents._runtime import SchemaError
from app.agents.narration_focus import (
    FocusFrame,
    NarrationFocusAgent,
    NarrationFocusInput,
    NarrationFocusOutput,
    classify_narration_focus,
)


def _input() -> NarrationFocusInput:
    return NarrationFocusInput(
        asset_id="asset-a",
        source_instance_id="upload-a",
        frame_contact_sheet_uri="files/contact-sheet-a",
        frame_samples=[
            {"sample_id": "frame-0", "source_time_s": 4.0},
            {"sample_id": "frame-1", "source_time_s": 5.0},
        ],
        source_window_start_s=3.5,
        source_window_end_s=5.5,
    )


def test_input_requires_samples_inside_pinned_source_window() -> None:
    with pytest.raises(ValueError, match="outside"):
        NarrationFocusInput(
            asset_id="asset-a",
            frame_contact_sheet_uri="files/contact-sheet-a",
            frame_samples=[FocusFrame(sample_id="frame-0", source_time_s=7.0)],
            source_window_start_s=3.5,
            source_window_end_s=5.5,
        )


def test_parse_rejects_unknown_evidence_frame_instead_of_dropping_it() -> None:
    with pytest.raises(SchemaError, match="unknown evidence_frame_ids"):
        NarrationFocusAgent(None).parse(
            '{"focus":"single_subject","primary_subject_count":1,'
            '"visible_subject_count":2,'
            '"evidence_frame_ids":["not-supplied"],"evidence":"one subject"}',
            _input(),
        )


def test_parse_rejects_untyped_count_and_evidence() -> None:
    with pytest.raises(SchemaError, match="unknown focus"):
        NarrationFocusAgent(None).parse(
            '{"focus":"unknown","primary_subject_count":1,"visible_subject_count":1,'
            '"evidence_frame_ids":["frame-0"],"evidence":"one subject"}',
            _input(),
        )


def test_prompt_forbids_identity_matching() -> None:
    prompt = NarrationFocusAgent(None).render_prompt(_input())
    assert "Never identify, name, compare, track, or match" in prompt
    assert "frame-0" in prompt


def test_prompt_version_is_pinned() -> None:
    assert NarrationFocusAgent.spec.prompt_version == "2026-09-07.3"


def test_single_primary_player_allows_background_people() -> None:
    output = NarrationFocusAgent(None).parse(
        '{"focus":"single_subject","primary_subject_count":1,'
        '"visible_subject_count":3,"evidence_frame_ids":["frame-0"],'
        '"evidence":"one player is the clear foreground focus"}',
        _input(),
    )

    assert output.focus == "single_subject"
    assert output.visible_subject_count == 3


def test_callable_maps_typed_focus_to_materializer_fields(monkeypatch) -> None:
    def fake_run(self, input, *, ctx=None):  # noqa: ARG001
        return NarrationFocusOutput(
            focus="single_subject",
            primary_subject_count=1,
            visible_subject_count=2,
            evidence_frame_ids=["frame-0"],
            evidence="one foreground player; background spectator",
        )

    monkeypatch.setattr(NarrationFocusAgent, "run", fake_run)
    fields = classify_narration_focus(
        None,
        asset_id="asset-a",
        source_instance_id="upload-a",
        frame_contact_sheet_uri="files/contact-sheet-a",
        frame_samples=[FocusFrame(sample_id="frame-0", source_time_s=4.0)],
        source_window_start_s=3.5,
        source_window_end_s=5.5,
    )

    assert fields["single_subject"] is True
    assert fields["primary_subject_count"] == 1
    assert fields["visible_subject_count"] == 2
    assert fields["focus_evidence"]["frame_ids"] == ["frame-0"]


def test_unknown_focus_can_explain_an_empty_field_with_frame_evidence():
    output = NarrationFocusAgent(None).parse(
        '{"focus":"unknown","primary_subject_count":null,"visible_subject_count":null,'
        '"evidence_frame_ids":["frame-1"],"evidence":"An empty sports field; no players."}',
        _input(),
    )
    assert output.focus == "unknown" and output.primary_subject_count is None
    assert output.evidence_frame_ids == ["frame-1"]
