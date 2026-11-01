"""Offline grounding controls for reviewed native overlay plans."""

import pytest

from app.agents.overlay_generation import OverlayPlanOutput
from app.services.overlay_generation import materialize_overlays, speech_evidence


def proposal(**changes):
    row = dict(
        kind="image",
        depicts="generic_subject",
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
    ],
)
def test_wrong_content_control_refuses_invented_or_unowned_evidence(change):
    with pytest.raises(ValueError):
        materialize_overlays(proposal(**change), evidence(), [], duration_s=8, prompt="coffee")


def _two_rows(first, second):
    rows = proposal().overlays
    base = rows[0].model_dump()
    return OverlayPlanOutput(
        overlays=[
            {**base, **first, "start_s": 1.0, "end_s": 2.0},
            {**base, **second, "start_s": 3.0, "end_s": 4.0},
        ]
    )


LOGO_NOTICE = "Upload the real logo or choose it from your visual library."


def _coffee_second_row(raw):
    facts = evidence() + [
        dict(kind="speech", reference_id="speech-1", quote="kahve", start_s=3.25, end_s=3.7)
    ]
    raw.overlays[1].evidence[0].reference_id = "speech-1"
    return facts


# Wording is irrelevant: the model's typed declaration decides, in any language, and a
# phrasing with no "logo" word at all is refused just the same (no synonym list to outgrow).
@pytest.mark.parametrize(
    "prompt",
    [
        "minimal circular coffee-brand icon, brand identity",
        "markanın işareti kahve fincanının yanında",
        "Markenzeichen auf der Tasse",
        "Acme logo",
    ],
)
def test_brand_marks_are_refused_without_losing_the_rest_of_a_mixed_request(prompt):
    notices = []
    raw = _two_rows(
        {"prompt": prompt, "title": "Brand", "depicts": "brand_or_logo"},
        {"prompt": "A cup of coffee"},
    )
    rows = materialize_overlays(
        raw, _coffee_second_row(raw), [], duration_s=8, prompt="", notices=notices
    )
    assert [row.prompt for row in rows] == ["A cup of coffee"]
    assert notices == [LOGO_NOTICE]


@pytest.mark.parametrize(
    "prompt",
    [
        "a hoodie with no logos",
        "logout button icon",
        "a monogrammed towel",
        "emblematic landmark skyline",
        "analogous colors palette",
    ],
)
def test_generic_pictures_are_never_refused_for_their_wording(prompt):
    notices = []
    rows = materialize_overlays(
        proposal(prompt=prompt), evidence(), [], duration_s=8, prompt="", notices=notices
    )
    assert len(rows) == 1 and notices == []


@pytest.mark.parametrize("declaration", ["brand_or_logo", None])
def test_unclear_or_missing_subject_fails_safe_instead_of_generating(declaration):
    notices = []
    rows = materialize_overlays(
        proposal(depicts=declaration, prompt="Starbucks cup"),
        evidence(),
        [],
        duration_s=8,
        prompt="Show Starbucks when I say coffee",
        notices=notices,
    )
    assert rows == [] and notices == [LOGO_NOTICE]


def test_a_missing_declaration_refuses_only_that_row_not_the_whole_plan():
    import json

    from app.agents.overlay_generation import OverlayGenerationAgent, OverlayPlanInput

    wire = json.loads(proposal().model_dump_json(exclude={"overlays": {"__all__": {"depicts"}}}))
    wire["overlays"].append({**wire["overlays"][0], "kind": "text", "text": "10", "prompt": ""})
    wire["overlays"][1]["start_s"], wire["overlays"][1]["end_s"] = 3.0, 4.0
    wire["overlays"][1]["evidence"] = [dict(kind="speech", reference_id="speech-1", quote="kahve")]
    agent = OverlayGenerationAgent.__new__(OverlayGenerationAgent)
    raw = agent.parse(
        json.dumps(wire),
        OverlayPlanInput(
            language="en",
            mode="automatic",
            prompt="",
            taste_notes="",
            duration_s=8,
            evidence=[],
            assets=[],
        ),
    )
    assert [row.depicts for row in raw.overlays] == [None, None]
    facts = evidence() + [
        dict(kind="speech", reference_id="speech-1", quote="kahve", start_s=3.25, end_s=3.7)
    ]
    notices = []
    rows = materialize_overlays(raw, facts, [], duration_s=8, prompt="", notices=notices)
    assert [row.kind for row in rows] == ["text"]
    assert notices == [LOGO_NOTICE]


@pytest.mark.parametrize("declared", ["generic", "Generic_Subject", "other", ""])
def test_an_unrecognised_declaration_refuses_only_that_row_without_a_paid_retry(declared):
    import json

    from app.agents.overlay_generation import OverlayGenerationAgent, OverlayPlanInput

    wire = json.loads(proposal().model_dump_json())
    wire["overlays"][0]["depicts"] = declared
    agent = OverlayGenerationAgent.__new__(OverlayGenerationAgent)
    raw = agent.parse(
        json.dumps(wire),
        OverlayPlanInput(
            language="en",
            mode="automatic",
            prompt="",
            taste_notes="",
            duration_s=8,
            evidence=[],
            assets=[],
        ),
    )
    notices = []
    rows = materialize_overlays(raw, evidence(), [], duration_s=8, prompt="", notices=notices)
    assert rows == [] and notices == [LOGO_NOTICE]


ODD_DECLARATIONS = [
    "x" * 41,
    "This picture shows a generic cup of coffee with no brand at all, so it is fine to use.",
    " generic_subject",
    "GENERIC_SUBJECT",
    5,
    True,
    [],
    {},
    None,
]


@pytest.mark.parametrize("declared", ODD_DECLARATIONS, ids=repr)
def test_odd_declaration_is_refused_alone_while_valid_neighbours_are_kept(declared):
    """Length and type never fail the plan: parse() accepts, the one row is refused."""
    import json

    from app.agents.overlay_generation import OverlayGenerationAgent, OverlayPlanInput

    wire = json.loads(proposal().model_dump_json())
    odd = {**wire["overlays"][0], "depicts": declared, "start_s": 1.0, "end_s": 2.0}
    valid = {
        **wire["overlays"][0],
        "prompt": "A cup of coffee",
        "start_s": 3.0,
        "end_s": 4.0,
        "evidence": [dict(kind="speech", reference_id="speech-1", quote="kahve")],
    }
    text = {
        **valid,
        "kind": "text",
        "text": "10",
        "prompt": "",
        "depicts": None,
        "start_s": 5.0,
        "end_s": 6.0,
        "evidence": [dict(kind="speech", reference_id="speech-2", quote="kahve")],
    }
    wire["overlays"] = [odd, valid, text]
    agent = OverlayGenerationAgent.__new__(OverlayGenerationAgent)
    raw = agent.parse(
        json.dumps(wire),
        OverlayPlanInput(
            language="en",
            mode="automatic",
            prompt="",
            taste_notes="",
            duration_s=8,
            evidence=[],
            assets=[],
        ),
    )
    assert raw.overlays[0].depicts != "generic_subject"
    facts = evidence() + [
        dict(kind="speech", reference_id="speech-1", quote="kahve", start_s=3.25, end_s=3.7),
        dict(kind="speech", reference_id="speech-2", quote="kahve", start_s=5.25, end_s=5.7),
    ]
    notices = []
    rows = materialize_overlays(raw, facts, [], duration_s=8, prompt="", notices=notices)
    assert [(row.kind, row.prompt or row.text) for row in rows] == [
        ("image", "A cup of coffee"),
        ("text", "10"),
    ]
    assert notices == [LOGO_NOTICE]


def test_declaration_never_reaches_the_client():
    row = materialize_overlays(proposal(), evidence(), [], duration_s=8, prompt="")[0]
    assert "depicts" not in row.model_dump_json()


def test_refused_brand_row_does_not_reserve_its_window():
    raw = _two_rows(
        {"prompt": "Acme logo", "depicts": "brand_or_logo"},
        {"prompt": "A cup of coffee", "start_s": 1.0, "end_s": 2.0},
    )
    assert len(materialize_overlays(raw, evidence(), [], duration_s=8, prompt="", notices=[])) == 1


def test_brand_notice_survives_a_later_grounding_error(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import generated_media
    from app.services import overlay_generation as service

    body = service.OverlayPlanRequest(
        job_id=uuid.uuid4(),
        variant_id="v",
        base_generation="g",
        document_fingerprint="f",
        mode="automatic",
        editor_snapshot={},
    )
    owner = uuid.uuid4()
    monkeypatch.setattr(
        generated_media, "owned_item", lambda *a: SimpleNamespace(current_job_id=body.job_id)
    )
    monkeypatch.setattr(
        service,
        "plan_context",
        lambda *a, **k: SimpleNamespace(
            evidence=evidence(), duration_s=8, existing_overlays=[], insertion_base=None
        ),
    )
    db = Mock()
    db.get.return_value = SimpleNamespace(
        user_id=owner,
        status="ready",
        assembly_plan={"variants": [{"variant_id": "v", "render_generation_id": "g"}]},
    )
    db.scalars.return_value.all.return_value = []
    raw = _two_rows(
        {"prompt": "Acme logo", "depicts": "brand_or_logo"},
        {"kind": "library_asset", "asset_id": "foreign", "prompt": ""},
    )
    agent = Mock()
    agent.run.return_value = raw
    reply = service.generate_overlay_plan(
        db, owner_id=owner, item_id=uuid.uuid4(), body=body, ctx=None, agent=agent
    )
    assert reply.overlays == []
    assert LOGO_NOTICE in reply.message and "available visual" in reply.message


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


def _voiceover_montage(**changes):
    import uuid
    from types import SimpleNamespace

    from app.services.overlay_generation import OverlayPlanRequest

    slots = [
        dict(slot_id="a", clip_index=0, in_s=0, duration_s=5, transition_after="cut"),
        dict(slot_id="b", clip_index=1, in_s=0, duration_s=5, transition_after="cut"),
    ]
    variant = dict(
        variant_id="voiceover_only",
        render_generation_id="g1",
        duration_s=10,
        resolved_archetype="voiceover",
        user_timeline={"slots": slots},
        text_elements=[
            dict(id="voiceover-intro-1", role="generative_intro", start_s=3.0, end_s=9.5),
        ],
        **changes,
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
        variant_id="voiceover_only",
        base_generation="g1",
        document_fingerprint="fingerprint",
        mode="automatic",
        prompt="",
        editor_snapshot={
            "editor_payload": {"base_generation": "g1", "sections": {"timeline_slots": slots}}
        },
    )
    item = SimpleNamespace(clip_assignments=[], voiceover_gcs_path="voiceovers/me.m4a")
    return job, variant, item, body


def test_recorded_voiceover_montage_plans_from_the_spoken_narration(monkeypatch):
    from app.pipeline.transcribe import Word
    from app.services import overlay_generation
    from app.services.overlay_generation import plan_context

    words = [
        Word(text="Fresh", start_s=1.0, end_s=1.4, confidence=0.9),
        Word(text="coffee", start_s=1.4, end_s=2.0, confidence=0.9),
        Word(text="wakes", start_s=2.1, end_s=2.5, confidence=0.9),
        Word(text="me", start_s=2.5, end_s=2.7, confidence=0.9),
        Word(text="up.", start_s=2.7, end_s=3.1, confidence=0.9),
    ]
    monkeypatch.setattr(overlay_generation, "voiceover_words", lambda path: words)
    job, variant, item, body = _voiceover_montage()
    context = plan_context(job, variant, item, body, [])
    speech = [row for row in context.evidence if row["kind"] == "speech"]
    assert speech and speech[0]["words"][1] == dict(quote="coffee", start_s=1.4, end_s=2.0)


def test_unreadable_voiceover_degrades_to_no_speech_not_an_error(monkeypatch):
    from app.services import overlay_generation
    from app.services.overlay_generation import plan_context

    def boom(path):
        raise OSError("gone")

    monkeypatch.setattr(overlay_generation, "voiceover_words", boom)
    job, variant, item, body = _voiceover_montage()
    assert not [
        r for r in plan_context(job, variant, item, body, []).evidence if r["kind"] == "speech"
    ]


def test_whole_video_title_does_not_reserve_every_moment():
    from app.services.overlay_generation import occupied_intervals

    persistent = dict(id="voiceover-intro-1", role="generative_intro", start_s=3.0, end_s=9.5)
    short_card = dict(id="card", start_s=5.0, end_s=6.0)
    result = occupied_intervals(dict(text_elements=[persistent, short_card]), duration_s=10)
    assert [(r["start_s"], r["end_s"]) for r in result] == [(5.0, 6.0)]


def test_overlay_route_passes_paid_call_attribution_to_the_agent(monkeypatch):
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth import get_current_user, is_native_client, kria_client_protocol
    from app.models import User
    from app.routes import overlay_generation

    app = FastAPI()
    app.include_router(overlay_generation.router, prefix="/plan-items")
    app.dependency_overrides[get_current_user] = lambda: User(id=uuid.uuid4(), email="a@b.test")
    app.dependency_overrides[is_native_client] = lambda: True
    app.dependency_overrides[kria_client_protocol] = lambda: 99
    monkeypatch.setattr(
        overlay_generation, "generated_capabilities", lambda *a: {"generated_images": True}
    )
    seen = {}

    def planned(db, **kwargs):
        seen["ctx"] = kwargs["ctx"]
        return overlay_generation.OverlayPlanResponse(
            document_fingerprint="f", base_generation="g1", message="ok"
        )

    monkeypatch.setattr(overlay_generation, "generate_overlay_plan", planned)
    monkeypatch.setattr(
        overlay_generation, "sync_session", lambda: __import__("contextlib").nullcontext()
    )
    from app.config import settings

    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    body = dict(
        job_id=str(uuid.uuid4()),
        variant_id="v",
        mode="automatic",
        prompt="",
        base_generation="g1",
        document_fingerprint="f",
        editor_snapshot={},
    )
    headers = {
        "x-nova-usage-purpose": "live_eval",
        "x-nova-test-run-id": "run-1",
        "x-nova-estimated-max-cost-usd": "0.5",
        "x-nova-reservation-approved": "true",
    }
    TestClient(app).post(f"/plan-items/{uuid.uuid4()}/overlay-plan", json=body, headers=headers)
    assert seen["ctx"].usage_purpose == "live_eval"
    assert seen["ctx"].test_run_id == "run-1"


def _narrated_edit(client_slots, *, saved_slots=None):
    import uuid
    from types import SimpleNamespace

    from app.services.overlay_generation import OverlayPlanRequest

    cue = dict(
        id="c0",
        text="fresh coffee",
        start_s=1.0,
        end_s=2.0,
        words=[
            dict(text="fresh", start_s=1.0, end_s=1.4),
            dict(text="coffee", start_s=1.4, end_s=2.0),
        ],
    )
    variant = dict(
        variant_id="narrated",
        render_generation_id="g1",
        duration_s=10,
        resolved_archetype="narrated",
        caption_cues=[cue],
    )
    if saved_slots:
        variant["user_timeline"] = {"slots": saved_slots}
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mode="content_plan",
        assembly_plan={"variants": [variant]},
        all_candidates={},
    )
    body = OverlayPlanRequest(
        job_id=job.id,
        variant_id="narrated",
        base_generation="g1",
        document_fingerprint="f",
        mode="automatic",
        prompt="",
        editor_snapshot={
            "editor_payload": {
                "base_generation": "g1",
                "sections": {"timeline_slots": client_slots, "caption_cues": [cue]},
            }
        },
    )
    return job, variant, SimpleNamespace(clip_assignments=[], voiceover_gcs_path=None), body


def _slots(*durations):
    return [
        dict(slot_id=f"s{i}", clip_index=i, in_s=0, duration_s=d, transition_after="cut")
        for i, d in enumerate(durations)
    ]


def test_narration_timing_survives_clips_the_phone_rebuilt_locally():
    from app.services.overlay_generation import plan_context

    job, variant, item, body = _narrated_edit(_slots(5, 5))
    speech = [
        r for r in plan_context(job, variant, item, body, []).evidence if r["kind"] == "speech"
    ]
    assert speech and speech[0]["start_s"] == 1.0


def test_narration_timing_is_dropped_when_the_edit_changes_length():
    from app.services.overlay_generation import plan_context

    job, variant, item, body = _narrated_edit(_slots(5, 3))
    assert not [
        r for r in plan_context(job, variant, item, body, []).evidence if r["kind"] == "speech"
    ]


def test_a_cut_edit_still_drops_speech_for_spoken_to_camera_footage():
    from app.services.overlay_generation import plan_context

    job, variant, item, body = _narrated_edit(_slots(4, 6), saved_slots=_slots(5, 5))
    variant["resolved_archetype"] = "subtitled"
    assert not [
        r for r in plan_context(job, variant, item, body, []).evidence if r["kind"] == "speech"
    ]
