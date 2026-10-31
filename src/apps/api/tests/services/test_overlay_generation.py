"""Offline grounding controls for reviewed native overlay plans."""

import pytest

from app.agents.overlay_generation import OverlayPlanOutput
from app.services.overlay_generation import materialize_overlays, speech_evidence


def proposal(**changes):
    row = dict(
        kind="image",
        title="Coffee",
        prompt="A cup of coffee",
        start_s=1.0,
        end_s=3.0,
        reason="The speaker mentions coffee",
        evidence=[dict(kind="speech", reference_id="speech-0", quote="kahve")],
    )
    return OverlayPlanOutput(overlays=[{**row, **changes}])


def evidence():
    return [dict(kind="speech", reference_id="speech-0", quote="kahve", start_s=1.25, end_s=1.7)]


def test_exact_turkish_trigger_uses_word_clock_not_model_guess():
    rows = materialize_overlays(proposal(), evidence(), [], duration_s=8, prompt="kahve dediğimde")
    assert rows[0].start_s == 1.25
    assert rows[0].end_s == 3


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": [dict(kind="speech", reference_id="made-up", quote="kahve")]},
        {"evidence": [dict(kind="speech", reference_id="speech-0", quote="tea")]},
        {"kind": "library_asset", "asset_id": "foreign"},
        {"end_s": 20},
        {"kind": "image", "prompt": "Invent an Acme logo"},
    ],
)
def test_wrong_content_control_refuses_invented_or_unowned_evidence(change):
    with pytest.raises(ValueError):
        materialize_overlays(proposal(**change), evidence(), [], duration_s=8, prompt="coffee")


def test_silent_visual_window_cannot_escape_the_source_slot():
    facts = [
        dict(kind="visual", reference_id="slot-1", quote="A red bridge", start_s=2.0, end_s=4.0)
    ]
    raw = proposal(
        start_s=1.0, evidence=[dict(kind="visual", reference_id="slot-1", quote="A red bridge")]
    )
    with pytest.raises(ValueError):
        materialize_overlays(raw, facts, [], duration_s=8, prompt="")


def test_caption_correction_cannot_invent_spoken_words():
    original = [
        {"id": "a", "text": "coffee", "words": [{"word": "coffee", "start_s": 1, "end_s": 2}]}
    ]
    edited = [{"id": "a", "text": "tea", "words": [{"word": "tea", "start_s": 1, "end_s": 2}]}]
    assert speech_evidence(original, edited) == []


def test_sentence_without_word_timings_does_not_invent_exact_word_time():
    cue = [{"id": "a", "text": "a long sentence", "start_s": 1, "end_s": 5}]
    assert speech_evidence(cue, cue) == []


def test_repeated_trigger_requires_specific_evidence_occurrence():
    fact = dict(
        kind="speech",
        reference_id="speech-0",
        quote="coffee coffee",
        start_s=1,
        end_s=4,
        words=[dict(quote="coffee", start_s=1, end_s=2), dict(quote="coffee", start_s=3, end_s=4)],
    )
    raw = proposal(evidence=[dict(kind="speech", reference_id="speech-0", quote="coffee")])
    with pytest.raises(ValueError, match="ambiguous"):
        materialize_overlays(raw, [fact], [], duration_s=8, prompt="coffee")


def test_snapshot_context_uses_native_caption_words():
    import uuid
    from types import SimpleNamespace

    from app.services.overlay_generation import OverlayPlanRequest, plan_context

    cue = dict(
        id="cue",
        text="coffee",
        start_s=1.0,
        end_s=2.0,
        words=[dict(word="coffee", start_s=1.0, end_s=2.0)],
    )
    variant = dict(
        variant_id="original_text",
        render_generation_id="g1",
        duration_s=5,
        caption_cues=[cue],
        resolved_archetype="subtitled",
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mode="generative",
        assembly_plan={"variants": [variant]},
        all_candidates={},
    )
    body = OverlayPlanRequest(
        job_id=job.id,
        variant_id="original_text",
        base_generation="g1",
        document_fingerprint="fingerprint",
        mode="prompt",
        prompt="when I say coffee",
        editor_snapshot={
            "editor_payload": {"base_generation": "g1", "sections": {"caption_cues": [cue]}}
        },
    )
    context = plan_context(job, variant, SimpleNamespace(clip_assignments=[]), body, [])
    assert context.evidence[0]["words"][0]["start_s"] == 1
    assert context.duration_s == 5


def test_client_cannot_move_original_spoken_word_timestamp():
    original = [
        {"id": "a", "text": "coffee", "words": [{"word": "coffee", "start_s": 1, "end_s": 2}]}
    ]
    edited = [
        {"id": "a", "text": "coffee", "words": [{"word": "coffee", "start_s": 5, "end_s": 6}]}
    ]
    assert speech_evidence(original, edited) == []


def test_shot_description_does_not_establish_gesture_timestamp():
    facts = [dict(kind="visual", reference_id="slot-1", quote="A person waves", start_s=0, end_s=8)]
    raw = proposal(evidence=[dict(kind="visual", reference_id="slot-1", quote="A person waves")])
    with pytest.raises(ValueError, match="cannot time that action"):
        materialize_overlays(raw, facts, [], duration_s=8, prompt="Show a star when I wave")


def test_explicit_start_time_is_not_confused_with_hold_duration():
    from app.services.overlay_generation import explicit_time_evidence

    assert explicit_time_evidence("show it for 3 seconds", 8) == []
    assert explicit_time_evidence("3 saniye göster", 8) == []
    assert explicit_time_evidence("at 3 seconds for 2 seconds", 8)[0]["start_s"] == 3
    assert explicit_time_evidence("3. saniyede yıldız göster", 8)[0]["start_s"] == 3
    assert explicit_time_evidence("show it at 00:03", 8)[0]["start_s"] == 3


def test_stale_job_refuses_before_calling_model(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import generated_media
    from app.services.overlay_generation import OverlayPlanRequest, generate_overlay_plan

    body = OverlayPlanRequest(
        job_id=uuid.uuid4(),
        variant_id="v",
        base_generation="g",
        document_fingerprint="f",
        mode="automatic",
        editor_snapshot={},
    )
    monkeypatch.setattr(
        generated_media, "owned_item", lambda *args: SimpleNamespace(current_job_id=uuid.uuid4())
    )
    agent = Mock()
    with pytest.raises(ValueError, match="current edit changed"):
        generate_overlay_plan(
            Mock(), owner_id=uuid.uuid4(), item_id=uuid.uuid4(), body=body, ctx=None, agent=agent
        )
    agent.run.assert_not_called()


def test_new_overlays_cannot_overlap_each_other_or_existing_visuals():
    raw = proposal()
    duplicate = OverlayPlanOutput(overlays=[raw.overlays[0], raw.overlays[0]])
    with pytest.raises(ValueError, match="overlap"):
        materialize_overlays(duplicate, evidence(), [], duration_s=8, prompt="")
    with pytest.raises(ValueError, match="overlap"):
        materialize_overlays(
            raw,
            evidence(),
            [],
            duration_s=8,
            prompt="",
            existing_overlays=[dict(start_s=2, end_s=4)],
        )
    assert materialize_overlays(
        raw, evidence(), [], duration_s=8, prompt="", existing_overlays=[dict(start_s=3, end_s=4)]
    )


def test_safe_zone_is_finite_normalized_source_metadata():
    from app.services.overlay_generation import safe_zone

    assert safe_zone({"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.2}) == {
        "x": 0.1,
        "y": 0.1,
        "w": 0.5,
        "h": 0.2,
    }
    assert safe_zone({"x": 0.9, "y": 0.1, "w": 0.5, "h": 0.2}) is None
    assert safe_zone({"x": float("nan"), "y": 0.1, "w": 0.5, "h": 0.2}) is None


def test_existing_native_lanes_reserve_windows_but_not_spoken_caption_text():
    from app.services.overlay_generation import occupied_intervals

    row = dict(id="existing", start_s=1, end_s=3)
    value = dict(
        visual_blocks=[row],
        media_overlays=[row],
        text_elements=[row, {**row, "id": "narration-caption-1"}],
        motion_scenes=[dict(id="motion", start_frame=120, end_frame_exclusive=180)],
    )
    result = occupied_intervals(value)
    assert len(result) == 4
    assert result[-1]["start_s"] == 4
    assert result[-1]["end_s"] == 6


def test_source_marked_clock_matches_native_frame_boundaries():
    from app.services.overlay_generation import overlay_clock

    slots = [
        dict(
            slot_id="a",
            clip_index=0,
            in_s=0,
            source_end_s=3,
            playback_rate=4,
            duration_s=22 / 30,
            transition_after="crossfade",
            transition_duration_s=0.15,
        ),
        dict(slot_id="b", clip_index=1, in_s=0, duration_s=1.3, transition_after="cut"),
    ]
    clock = overlay_clock(slots, None)
    assert clock.windows[1]["output_start_s"] == pytest.approx(0.6)
    assert clock.base_duration == pytest.approx(1.9)


def test_carousel_output_time_maps_to_base_without_rejecting_whole_video():
    from app.services.overlay_generation import overlay_clock

    slots = [
        dict(slot_id="a", clip_index=0, in_s=0, duration_s=4, transition_after="cut"),
        dict(slot_id="b", clip_index=1, in_s=0, duration_s=4, transition_after="cut"),
    ]
    clock = overlay_clock(slots, dict(position="intro", duration_s=3))
    assert clock.output_duration == 11
    assert clock.to_base(7) == 4
    with pytest.raises(ValueError, match="Carousel"):
        clock.to_base(2)
    middle = overlay_clock(slots, dict(position="middle", duration_s=3))
    assert middle.to_base(2) == 2
    assert middle.to_base(8) == 5
    outro = overlay_clock(slots, dict(position="outro", duration_s=3))
    assert outro.to_base(2) == 2
    with pytest.raises(ValueError, match="Carousel"):
        outro.to_base(9)


def test_snapshot_carousel_explicit_time_evidence_is_on_editor_base_clock():
    import uuid
    from types import SimpleNamespace

    from app.services.overlay_generation import OverlayPlanRequest, plan_context

    slots = [
        dict(slot_id="a", clip_index=0, in_s=0, duration_s=4, transition_after="cut"),
        dict(slot_id="b", clip_index=1, in_s=0, duration_s=4, transition_after="cut"),
    ]
    variant = dict(
        variant_id="original_text",
        render_generation_id="g1",
        duration_s=8,
        user_timeline={"slots": slots},
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mode="generative",
        assembly_plan={"variants": [variant]},
        all_candidates={},
    )
    body = OverlayPlanRequest(
        job_id=job.id,
        variant_id="original_text",
        base_generation="g1",
        document_fingerprint="fingerprint",
        mode="prompt",
        prompt="at 7 seconds add a star",
        editor_snapshot={
            "editor_payload": {
                "base_generation": "g1",
                "sections": {
                    "timeline_slots": slots,
                    "carousel_moment": {"position": "intro", "duration_s": 3},
                },
            }
        },
    )
    context = plan_context(job, variant, SimpleNamespace(clip_assignments=[]), body, [])
    assert context.duration_s == 8
    assert context.evidence[0]["start_s"] == 4
    assert context.evidence[0]["output_time_s"] == 7


def test_carousel_boundary_crossfades_match_native_splice():
    from app.services.overlay_generation import overlay_clock

    slots = [
        dict(
            slot_id="a",
            clip_index=0,
            in_s=0,
            duration_s=4,
            transition_after="crossfade",
            transition_duration_s=0.3,
        ),
        dict(slot_id="b", clip_index=1, in_s=0, duration_s=4, transition_after="cut"),
    ]
    clock = overlay_clock(
        slots,
        dict(
            position="middle",
            duration_s=3,
            transition_in="crossfade",
            transition_in_duration_s=0.2,
            transition_out="crossfade",
            transition_out_duration_s=0.4,
        ),
    )
    assert clock.insertion_base == pytest.approx(3.7)
    assert clock.insertion_start == pytest.approx(3.8)
    assert clock.shift == pytest.approx(2.7)
    assert clock.output_duration == pytest.approx(10.4)
    assert clock.to_base(8) == pytest.approx(5.3)


def test_proposal_cannot_stretch_across_a_middle_carousel():
    facts = [dict(kind="explicit_time", reference_id="time-0", quote="00:03", start_s=3, end_s=8)]
    raw = proposal(
        start_s=3,
        end_s=5,
        evidence=[dict(kind="explicit_time", reference_id="time-0", quote="00:03")],
    )
    with pytest.raises(ValueError, match="Carousel"):
        materialize_overlays(
            raw,
            facts,
            [],
            duration_s=8,
            prompt="At 00:03 show a star for 2 seconds",
            insertion_base=4,
        )


@pytest.mark.parametrize("kind", ["explicit_time", "visual", "speech"])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_references_cannot_move_an_overlay_outside_another_anchor(kind, reverse):
    speech = dict(kind="speech", reference_id="speech-0", quote="kahve")
    other = dict(kind=kind, reference_id="other", quote="other")
    refs = [other, speech] if not reverse else [speech, other]
    facts = evidence() + [dict(kind=kind, reference_id="other", quote="other", start_s=3, end_s=5)]
    raw = proposal(start_s=3, end_s=4, evidence=refs)
    with pytest.raises(ValueError):
        materialize_overlays(raw, facts, [], duration_s=8, prompt="show coffee at 3 seconds")


@pytest.mark.parametrize("reverse", [False, True])
def test_matching_references_validate_the_final_speech_start(reverse):
    refs = [
        dict(kind="explicit_time", reference_id="time", quote="1.25"),
        dict(kind="visual", reference_id="shot", quote="A cup"),
        dict(kind="speech", reference_id="speech-0", quote="kahve"),
    ]
    facts = evidence() + [
        dict(kind="explicit_time", reference_id="time", start_s=1.25, end_s=8),
        dict(kind="visual", reference_id="shot", quote="A cup", start_s=1, end_s=4),
    ]
    rows = materialize_overlays(
        proposal(start_s=0, evidence=list(reversed(refs)) if reverse else refs),
        facts,
        [],
        duration_s=8,
        prompt="",
    )
    assert rows[0].start_s == 1.25
