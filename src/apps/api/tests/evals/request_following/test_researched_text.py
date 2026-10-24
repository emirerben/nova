"""Research journeys: provider evidence, footage binding and durable editor replay.

Failures declared before implementation: forged URLs, absent grounding, invented object
identity, unrelated/missing clips, unreadable/overlapping copy, changed audio/order,
lost citations after serialization and edits mislabeled as freshly verified.
"""

import json
from types import SimpleNamespace

import pytest

from app.services.researched_text import (
    ResearchRequest,
    ResearchUnavailable,
    evidence_from_response,
    research_operations,
)


def response(url="https://museum.example.org/collection"):
    return SimpleNamespace(
        text="The museum contains regional finds.",
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    web_search_queries=["museum regional collection"],
                    grounding_chunks=[
                        SimpleNamespace(web=SimpleNamespace(uri=url, title="Museum"))
                    ],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text="The museum contains regional finds."),
                            grounding_chunk_indices=[0],
                        )
                    ],
                    search_entry_point=SimpleNamespace(
                        rendered_content="<div>Search results</div>"
                    ),
                )
            )
        ],
    )


def snapshot():
    return {
        "slots": [
            {
                "media_id": "horse",
                "output_start_s": 0,
                "output_end_s": 3,
                "seen": {"text": "A wooden horse"},
            },
            {
                "media_id": "car",
                "output_start_s": 3,
                "output_end_s": 6,
                "seen": {"text": "Driving a car"},
            },
            {
                "media_id": "gallery",
                "output_start_s": 6,
                "output_end_s": 10,
                "seen": {"text": "Museum galleries and display cases"},
            },
        ],
        "text_bars": [],
        "copy_constraints": {},
    }


def caption(**changes):
    return {
        "text": "Museum: Regional finds",
        "context_label": "Museum",
        "media_ids": ["gallery"],
        "evidence_ids": ["fact-0"],
        "visual_quote": "Museum galleries",
        "scope": "context",
        **changes,
    }


def test_only_provider_citations_are_evidence():
    evidence = evidence_from_response(response())
    assert evidence.facts[0].sources[0].url == "https://museum.example.org/collection"
    assert evidence.facts[0].text == "The museum contains regional finds."
    assert evidence.search_entry_html


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/a",
        "https://127.0.0.1/a",
        "https://169.254.169.254/",
        "https://private.local/x",
        "javascript:alert(1)",
    ],
)
def test_non_public_citations_are_rejected(url):
    with pytest.raises(ResearchUnavailable):
        evidence_from_response(response(url))


def test_ungrounded_generated_answer_never_becomes_evidence():
    with pytest.raises(ResearchUnavailable):
        evidence_from_response(
            SimpleNamespace(text="Trust https://museum.example.org", candidates=[])
        )


def test_preserves_opening_car_order_audio_and_cites_only_bound_window():
    from app.services.kria_editor_ops import apply_text_lane_ops

    evidence = evidence_from_response(response())
    snap = snapshot()
    original = json.dumps(snap, sort_keys=True)
    ops = research_operations([caption()], evidence, snap, "Explain the museum collection")
    assert [op["op"] for op in ops] == ["add_text"]
    assert (ops[0]["start_s"], ops[0]["end_s"]) == (6, 10)
    assert json.dumps(snap, sort_keys=True) == original
    # Real text applier, then JSON persistence: no bespoke render path.
    rows = apply_text_lane_ops([], snap["slots"], ops).text
    restored = json.loads(json.dumps(rows))
    assert restored[0]["source_params"]["research"]["original_text"] == "Museum: Regional finds"
    assert restored[0]["source_params"]["research"]["media_ids"] == ["gallery"]
    assert restored[0]["source_params"]["research"]["sources"][0]["url"].startswith("https://")


@pytest.mark.parametrize(
    "change",
    [
        {"evidence_ids": ["invented"]},
        {"media_ids": ["missing"]},
        {"media_ids": ["horse", "gallery"]},
        {"visual_quote": "sarcophagus label"},
        {"text": "A" * 200},
        {"scope": "object", "subject_name": "Polyxena", "identity_quote": "Polyxena"},
    ],
)
def test_unsupported_or_unreadable_binding_fails_closed(change):
    with pytest.raises(ResearchUnavailable):
        research_operations(
            [caption(**change)], evidence_from_response(response()), snapshot(), "Explain this"
        )


def test_overlapping_research_and_protected_title_are_not_silently_replaced():
    snap = snapshot()
    snap["text_bars"] = [{"id": "creator", "text": "My exact title", "start_s": 0, "end_s": 9}]
    snap["copy_constraints"] = {"creator": "My exact title"}
    with pytest.raises(ResearchUnavailable):
        research_operations([caption()], evidence_from_response(response()), snap, "Explain this")
    with pytest.raises(ResearchUnavailable):
        research_operations(
            [caption(), caption()], evidence_from_response(response()), snapshot(), "Explain this"
        )


def test_research_request_requires_actual_creator_words_in_any_language():
    request = ResearchRequest(topic="Troya tarihi", request_quote="İnternetten araştır")
    request.authorize("Troya için İnternetten araştır ve açıkla")
    with pytest.raises(ResearchUnavailable):
        request.authorize("Keep my music")


def test_grounded_search_charge_is_settled_in_addition_to_tokens():
    from app.services.ai_cost_control import UsageMeter, calculate_cost_usd

    ordinary = calculate_cost_usd("gemini-2.5-flash", UsageMeter(tokens_in=100, tokens_out=100))
    grounded = calculate_cost_usd(
        "gemini-2.5-flash",
        UsageMeter(
            tokens_in=100,
            tokens_out=100,
            token_details={"grounded_search_requests": 1},
        ),
    )
    assert grounded - ordinary == pytest.approx(0.035)


def test_public_topic_cannot_carry_private_media_urls():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResearchRequest(
            topic="https://storage.googleapis.com/private/video.mp4?token=abc",
            request_quote="research",
        )


def test_followup_enters_real_research_path_and_retains_sources(monkeypatch):
    import asyncio
    import uuid

    from app.agents.edit_copilot import EditCopilotAgent
    from app.routes._copilot import CopilotTurnBody, run_copilot_turn
    from app.services.researched_text import (
        GroundedResearchAgent,
        ResearchCopyAgent,
        ResearchCopyOutput,
        ResearchReviewAgent,
        ResearchReviewOutput,
    )

    calls = []

    def route(self, input, *, ctx=None):
        return self.parse(
            json.dumps(
                {
                    "research_request": {
                        "topic": "museum collection",
                        "request_quote": "research online",
                    }
                }
            ),
            input,
        )

    def search(self, input, *, ctx=None):
        calls.append(input.topic)
        return evidence_from_response(response())

    def write(self, input, *, ctx=None):
        assert input.evidence.facts
        return ResearchCopyOutput(captions=[caption()])

    monkeypatch.setattr(EditCopilotAgent, "run", route)
    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    monkeypatch.setattr(
        ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(approved=True, reason="Supported"),
    )
    result = asyncio.run(
        run_copilot_turn(
            CopilotTurnBody(
                message="Please research online and explain the museum collection",
                snapshot=snapshot(),
                client_contract_version=2,
            ),
            job_id=uuid.uuid4(),
        )
    )
    assert calls == ["museum collection"]
    assert result.ops[-1]["research"]["sources"]
    assert result.outcome == "proposed"


@pytest.mark.parametrize("limited", [False, True, "informational"])
def test_initial_creation_research_replays_without_searching_again(monkeypatch, limited):
    from app.pipeline.guided_story import compile_proposal_execution_plan
    from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
    from app.schemas.edit_proposal import EditProposalSnapshot
    from app.services.creation_text_composition import CreationTextComposer, compose_creation_text
    from app.services.researched_text import (
        GroundedResearchAgent,
        ResearchCopyAgent,
        ResearchCopyOutput,
        ResearchReviewAgent,
        ResearchReviewOutput,
    )

    calls = []
    plan = plan_unified_montage(
        [
            UnifiedClip(
                media_id="gallery",
                proxy_path="private/gallery.mp4",
                generation="1",
                duration_s=8,
                analysis={"understanding": {"summary": "Museum galleries and display cases"}},
            )
        ],
        BriefView(target_duration_s=6),
    )

    def route(self, input, *, ctx=None):
        return self.parse(
            json.dumps(
                {
                    "research_request": {
                        "topic": "museum collection",
                        "request_quote": "research online",
                    }
                }
            ),
            input,
        )

    def search(self, input, *, ctx=None):
        calls.append(input.topic)
        return evidence_from_response(response())

    def write(self, input, *, ctx=None):
        return ResearchCopyOutput(
            captions=[caption()],
            limitations="An artifact label is needed" if limited else "",
            unmet_requests=["An artifact label is needed"] if limited is True else [],
        )

    monkeypatch.setattr(CreationTextComposer, "run", route)
    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    monkeypatch.setattr(
        ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(approved=True, reason="Supported"),
    )
    original = compile_proposal_execution_plan(plan.snapshot)
    if limited is True:
        from app.services.creation_text_composition import CreationTextCompositionError

        with pytest.raises(CreationTextCompositionError, match="artifact label"):
            compose_creation_text(
                plan.snapshot, creator_request="Please research online and explain this museum"
            )
        return
    edited = compose_creation_text(
        plan.snapshot, creator_request="Please research online and explain this museum"
    )
    restored = EditProposalSnapshot.model_validate_json(edited.model_dump_json())
    compiled = compile_proposal_execution_plan(restored)
    assert compiled["story_timeline"] == original["story_timeline"]
    assert compiled["text_elements"][-1]["source_params"]["research"]["sources"]
    assert compose_creation_text(restored, creator_request="same") == restored
    assert len(calls) == 1


def test_research_cannot_overwrite_existing_opening_without_copy_constraints():
    snap = snapshot()
    snap["text_bars"] = [{"id": "opening", "text": "Come inside", "start_s": 0, "end_s": 3}]
    with pytest.raises(ResearchUnavailable, match="opening"):
        research_operations(
            [caption(media_ids=["horse"], visual_quote="wooden horse")],
            evidence_from_response(response()),
            snap,
            "Research the site",
        )


def test_missing_output_window_returns_actionable_failure():
    snap = snapshot()
    snap["slots"][-1]["output_start_s"] = None
    with pytest.raises(ResearchUnavailable):
        research_operations([caption()], evidence_from_response(response()), snap, "Research this")


def test_primary_evidence_excludes_secondary_only_claims_and_spoofed_domains():
    from app.services.researched_text import (
        ResearchEvidence,
        ResearchFact,
        ResearchSource,
        primary_evidence,
    )

    def fact(key, host, title=None):
        return ResearchFact(
            id=key,
            text=key,
            sources=[ResearchSource(title=title or host, url=f"https://{host}/page")],
        )

    evidence = ResearchEvidence(
        facts=[
            fact("official", "museum.gov.tr"),
            fact("university", "research.ac.uk"),
            fact("blog", "travel.example.com"),
            fact("spoof", "gov.tr.evil.com"),
            fact("title-spoof", "evil.com", "museum.gov.tr"),
        ],
        retrieved_at="2026-10-19",
        search_entry_html="<div>Search</div>",
    )
    assert [row.id for row in primary_evidence(evidence).facts] == ["official", "university"]
    evidence.facts = evidence.facts[2:]
    with pytest.raises(ResearchUnavailable):
        primary_evidence(evidence)


def test_primary_evidence_accepts_provider_display_domain_only_for_google_redirects():
    from app.services.researched_text import primary_evidence

    result = response("https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc")
    result.candidates[0].grounding_metadata.grounding_chunks[0].web.title = "museum.gov.tr"
    assert primary_evidence(evidence_from_response(result)).facts


def test_copy_input_does_not_recycle_existing_caption_as_artifact_identity():
    from app.services.researched_text import ResearchCopyAgent, ResearchCopyInput

    snap = snapshot()
    snap["text_bars"] = [{"text": "Polyxena sarcophagus 520 BCE", "start_s": 6, "end_s": 10}]
    prompt = ResearchCopyAgent(None).render_prompt(
        ResearchCopyInput(
            request="Research this", snapshot=snap, evidence=evidence_from_response(response())
        )
    )
    assert "Polyxena sarcophagus 520 BCE" not in prompt


def test_failed_claim_to_footage_review_leaves_draft_unchanged(monkeypatch):
    from app.services.researched_text import (
        GroundedResearchAgent,
        ResearchCopyAgent,
        ResearchCopyOutput,
        ResearchReviewAgent,
        ResearchReviewOutput,
        run_researched_revision,
    )

    monkeypatch.setattr(
        GroundedResearchAgent, "run", lambda *a, **k: evidence_from_response(response())
    )
    monkeypatch.setattr(
        ResearchCopyAgent,
        "run",
        lambda *a, **k: ResearchCopyOutput(
            captions=[caption(text="Museum: Includes a statue of Hadrian")]
        ),
    )
    monkeypatch.setattr(
        ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(
            approved=False, reason="The pictured statue is not identified"
        ),
    )
    snap = snapshot()
    before = json.dumps(snap)
    with pytest.raises(ResearchUnavailable, match="statue is not identified"):
        run_researched_revision(
            ResearchRequest(topic="museum", request_quote="Research"),
            utterance="Research the collection",
            snapshot=snap,
        )
    assert json.dumps(snap) == before


def test_context_caption_names_its_subject_instead_of_implying_object_age():
    from app.services.researched_text import ResearchCopyAgent, ResearchCopyInput, research_windows

    snap = snapshot()
    snap["research_windows"] = research_windows(snap)
    value = caption(text="Regional finds", window_id="window-2", context_label="Museum")
    parsed = ResearchCopyAgent(None).parse(
        json.dumps({"captions": [value]}),
        ResearchCopyInput(
            request="Research the museum",
            snapshot=snap,
            evidence=evidence_from_response(response()),
        ),
    )
    assert parsed.captions[0].text == "Museum: Regional finds"
    with pytest.raises(ResearchUnavailable, match="context"):
        research_operations(
            [caption(text="Spans 5,000 years", context_label="")],
            evidence_from_response(response()),
            snap,
            "Research the museum",
        )


def test_plain_copilot_cannot_forge_research_provenance():
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

    result = EditCopilotAgent(None).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 1,
                "reply": "Prepared text",
                "ops": [
                    {
                        "op": "add_text",
                        "text": "Hello",
                        "start_s": 6,
                        "end_s": 10,
                        "research": {"sources": [{"url": "https://invented.example.org"}]},
                    }
                ],
            }
        ),
        EditCopilotInput(utterance='Add the exact text "Hello"', variant_snapshot=snapshot()),
    )
    assert result.ops
    assert all("research" not in op for op in result.ops)


def test_title_only_edit_still_offers_later_readable_research_windows():
    from app.services.researched_text import research_windows

    snap = snapshot()
    snap["text_bars"] = [{"id": "intro", "text": "Our visit", "start_s": 0, "end_s": 3}]
    windows = research_windows(snap)
    assert any(row["media_ids"] == ["gallery"] for row in windows)
    assert all(row["start_s"] >= 3 for row in windows)


def beat_cut_montage(count=27, clip_s=0.621):
    # Prod 2026-10-23 "Add some titles and facts about assos": 27 beat-cut clips,
    # no text. Each clip alone is below the readable minimum.
    return {
        "slots": [
            {
                "media_id": f"clip-{i}",
                "output_start_s": round(i * clip_s, 3),
                "output_end_s": round((i + 1) * clip_s, 3),
                "seen": {"text": "Ancient ruins above the harbor"},
            }
            for i in range(count)
        ],
        "text_bars": [],
        "copy_constraints": {},
    }


def test_beat_cut_montage_offers_windows_across_consecutive_clips():
    """Failure modes: no window at all (the prod refusal), a window shorter than the
    readable minimum, a window over the opening once text sits on it, a window whose
    clips are not consecutive, and a long-clip edit changing its windows."""
    from app.services.researched_text import research_windows

    snap = beat_cut_montage()
    windows = research_windows(snap)
    assert windows
    ids = [s["media_id"] for s in snap["slots"]]
    for row in windows:
        assert row["end_s"] - row["start_s"] >= 2
        first = ids.index(row["media_ids"][0])
        assert row["media_ids"] == ids[first : first + len(row["media_ids"])]

    snap["text_bars"] = [{"id": "intro", "text": "Assos", "start_s": 0, "end_s": 0.6}]
    assert all(row["start_s"] >= snap["slots"][0]["output_end_s"] for row in research_windows(snap))

    long_clips = [(row["start_s"], row["end_s"]) for row in research_windows(snapshot())]
    assert long_clips == [(0, 3), (3, 6), (6, 10)]


def test_research_caption_binds_to_a_grouped_montage_window():
    from app.services.researched_text import research_windows

    snap = beat_cut_montage()
    window = research_windows(snap)[0]
    ops = research_operations(
        [
            caption(
                media_ids=window["media_ids"],
                window_id=window["id"],
                visual_quote="Ancient ruins",
            )
        ],
        evidence_from_response(response()),
        snap,
        "Add some titles and facts about the museum",
    )
    assert [op["op"] for op in ops] == ["add_text"]
    assert ops[0]["end_s"] - ops[0]["start_s"] >= 2
