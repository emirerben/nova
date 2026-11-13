"""Researched facts reach word sequences and translated layers only through review.

A creator asked for an explanation of a public date shown word by word in the middle, with the
same words translated, word by word, at the bottom. The footage-window caption writer can make
neither, so research now writes reviewed text layers (one per requested layer, cited by fact
ID), deterministic evidence checks and the claim review gate them, and the text composer only
shapes them. Provider calls are stubbed; parse, op coercion, replay and compile are the real
pipeline. A stubbed reviewer verdict exercises the orchestration, never the semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.text_composition import TextCompositionProgram
from app.services.creation_text_composition import (
    CreationTextComposer,
    CreationTextCompositionError,
    compose_creation_text,
)
from app.services.researched_text import (
    GroundedResearchAgent,
    ResearchCopyAgent,
    ResearchedLayer,
    ResearchLayersAgent,
    ResearchLayersOutput,
    ResearchReviewAgent,
    ResearchReviewOutput,
    evidence_from_response,
)

REQUEST = (
    "I want a text that explains why Founders Day matters in Exampleland. One large text in "
    "the middle, word by word. At the bottom, the same in English."
)
FOUNDER_FACTS = [
    "Founders Day on 12 May marks the day in 1921 when Exampleland's first parliament opened.",
    "Each year people in Exampleland pause at noon to remember the founder.",
]
MUSEUM_FACTS = ["The museum contains regional finds."]
LOCAL = "Her yıl öğlen kurucumuzu anıyoruz"
ENGLISH = "Every year at noon we remember our founder"


def _response(segments: list[str]):
    chunk = SimpleNamespace(web=SimpleNamespace(uri="https://archive.example.gov/day", title="Gov"))
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[chunk],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text=text), grounding_chunk_indices=[0]
                        )
                        for text in segments
                    ],
                    search_entry_point=SimpleNamespace(rendered_content="<div>Search</div>"),
                )
            )
        ]
    )


def _plan():
    clips = [
        UnifiedClip(
            media_id=f"archive-{index}",
            proxy_path=f"private/archive-{index}.mp4",
            generation="1",
            duration_s=5,
            analysis={"understanding": {"summary": "Archival footage of a crowd"}},
        )
        for index in range(3)
    ]
    return plan_unified_montage(clips, BriefView(target_duration_s=12))


def _sequence(req_id: str, position: str, description: str) -> BriefRequirement:
    return BriefRequirement(
        id=req_id,
        kind="text_sequence",
        scope="global",
        description=description,
        facts={"mode": "agent_choice", "unit": "word", "position": position},
    )


REQUIREMENTS = [
    _sequence("r1", "middle", "A text that explains Founders Day, word by word in the middle"),
    _sequence("r2", "bottom", "The same in English at the bottom, word by word"),
]


def _layer(text: str, **fields) -> ResearchedLayer:
    return ResearchedLayer(
        text=text,
        **{
            "language": "tr",
            "position": "middle",
            "start_s": 0,
            "end_s": 12,
            "evidence_ids": ["fact-1"],
            **fields,
        },
    )


def _layers(local=LOCAL, english=ENGLISH, **english_fields) -> list[ResearchedLayer]:
    return [
        _layer(local),
        _layer(english, language="en", position="bottom", translation_of=0, **english_fields),
    ]


def _bar(snapshot: dict, text: str) -> dict | None:
    return next((bar for bar in snapshot.get("text_bars") or [] if bar["text"] == text), None)


class Provider:
    """Stub provider answers; the real orchestration, parse and compiler do everything else."""

    def __init__(self):
        self.facts = FOUNDER_FACTS
        self.layers = _layers()
        self.unmet: list[str] = []
        self.approve = True
        self.shape_ops = None
        self.searches = 0
        self.written: list[str] = []
        self.reviewed: list[dict] = []

    def compose(self, agent, input):
        snapshot = input.variant_snapshot
        if snapshot.get("researched_copy"):
            self.shaping_input = input
        if not snapshot.get("researched_copy"):
            reply = {
                "research_request": {
                    "topic": "Founders Day in Exampleland",
                    "request_quote": "explains why Founders Day matters in Exampleland",
                }
            }
        else:
            originals = [layer.text for layer in self.layers if layer.translation_of is None]
            local, english = _bar(snapshot, originals[0]), _bar(snapshot, ENGLISH)
            reply = {
                "intent": "edit",
                "confidence": 0.9,
                "reply": "Each layer shows one word at a time.",
                "ops": self.shape_ops(local, english)
                if self.shape_ops
                else [
                    {
                        "op": "replace_text_sequence",
                        "selector": {"ids": [row["id"]]},
                        "segments": row["text"].split(),
                    }
                    for row in (local, english)
                ],
            }
        return agent.parse(json.dumps(reply), input)

    def search(self, agent, input):
        self.searches += 1
        return evidence_from_response(_response(self.facts))

    def write(self, agent, input):
        self.written.append(input.correction)
        return ResearchLayersOutput(layers=self.layers, unmet_requests=self.unmet)

    def review(self, agent, input):
        self.reviewed.append(
            {
                "layers": [layer.text for layer in input.layers],
                "facts": [fact.text for fact in input.context.evidence.facts],
                "request": input.context.request,
            }
        )
        return ResearchReviewOutput(
            approved=self.approve, reason="Supported" if self.approve else "Not in the facts"
        )


def _never(*args, **kwargs):
    raise AssertionError("footage-window caption writer must not run for this ask")


@pytest.fixture
def provider(monkeypatch):
    stub = Provider()
    for agent, method in (
        (CreationTextComposer, stub.compose),
        (GroundedResearchAgent, stub.search),
        (ResearchLayersAgent, stub.write),
        (ResearchReviewAgent, stub.review),
    ):
        monkeypatch.setattr(
            agent, "run", lambda self, input, *, ctx=None, _m=method: _m(self, input)
        )
    monkeypatch.setattr(ResearchCopyAgent, "run", _never)
    return stub


def _compose():
    return compose_creation_text(
        _plan().snapshot, creator_request=REQUEST, text_requirements=REQUIREMENTS
    )


def _rows(rows: list[dict], words: str) -> list[dict]:
    wanted = words.split()
    return sorted((row for row in rows if row["text"] in wanted), key=lambda row: row["start_s"])


def test_reviewed_layers_become_two_concurrent_cited_word_sequences(provider) -> None:
    plan = _plan()
    original = compile_proposal_execution_plan(plan.snapshot)

    composed = compose_creation_text(
        plan.snapshot, creator_request=REQUEST, text_requirements=REQUIREMENTS
    )

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    local, english = _rows(rows, LOCAL), _rows(rows, ENGLISH)
    assert [row["text"] for row in local] == LOCAL.split()
    assert [row["text"] for row in english] == ENGLISH.split()
    assert {row["position"] for row in local} == {"middle"}
    assert {row["position"] for row in english} == {"bottom"}
    assert local[0]["start_s"] < english[-1]["end_s"] and english[0]["start_s"] < local[-1]["end_s"]
    for row in local + english:
        research = row["source_params"]["research"]
        assert [fact["id"] for fact in research["facts"]] == ["fact-1"]
        assert research["sources"] and research["search_entry_html"]
    assert {row["source_params"]["research"]["language"] for row in english} == {"en"}
    assert provider.searches == 1
    assert provider.reviewed[0]["layers"] == [LOCAL, ENGLISH]
    assert compose_creation_text(composed, creator_request="same") == composed
    assert compile_proposal_execution_plan(composed)["story_timeline"] == original["story_timeline"]


def test_unrelated_facts_rejected_by_review_fail_the_render(provider) -> None:
    provider.facts = MUSEUM_FACTS
    provider.layers = _layers(evidence_ids=["fact-0"])
    provider.layers[0] = _layer(LOCAL, evidence_ids=["fact-0"])
    provider.approve = False

    with pytest.raises(CreationTextCompositionError, match="Not in the facts"):
        _compose()
    assert provider.reviewed[0]["facts"] == MUSEUM_FACTS
    assert provider.reviewed[0]["layers"] == [LOCAL, ENGLISH]
    assert provider.written == ["", "Research needs correction: Not in the facts"]


def test_invented_date_is_rejected_before_review(provider) -> None:
    provider.layers = _layers(local="1922'de kurucumuzu anıyoruz", english="In 1922 we remember")

    with pytest.raises(CreationTextCompositionError, match="numbers its facts do not: 1922"):
        _compose()
    assert provider.reviewed == []


def test_uncited_fact_is_rejected(provider) -> None:
    provider.layers = [_layer(LOCAL, evidence_ids=["fact-9"]), *_layers()[1:]]

    with pytest.raises(CreationTextCompositionError, match="unavailable evidence"):
        _compose()


def test_naming_who_is_pictured_needs_the_creator_or_a_label(provider) -> None:
    provider.layers = [
        _layer("Mara Ostin'i anıyoruz", subject_name="Mara Ostin", identity_quote="Mara Ostin"),
        _layer(ENGLISH, language="en", position="bottom", translation_of=0),
    ]

    with pytest.raises(CreationTextCompositionError, match="label or creator identification"):
        _compose()
    assert provider.reviewed == []


def test_translation_that_changes_a_number_is_rejected(provider) -> None:
    provider.layers = [
        _layer("1921'den beri anıyoruz", evidence_ids=["fact-0"]),
        _layer(
            "Remembered since 1912",
            language="en",
            position="bottom",
            translation_of=0,
            evidence_ids=["fact-0"],
        ),
    ]

    with pytest.raises(CreationTextCompositionError, match="changes the numbers"):
        _compose()


def test_mistranslation_rejected_by_review_fails_the_render(provider) -> None:
    provider.layers = _layers(english="Every year at noon we celebrate a victory")
    provider.approve = False

    with pytest.raises(CreationTextCompositionError, match="Not in the facts"):
        _compose()
    assert provider.reviewed[0]["layers"] == [LOCAL, "Every year at noon we celebrate a victory"]


def test_translation_citing_other_facts_is_rejected(provider) -> None:
    provider.layers = _layers(evidence_ids=["fact-0"])

    with pytest.raises(CreationTextCompositionError, match="facts of the text it translates"):
        _compose()


def test_a_missing_translation_layer_fails_instead_of_silently_dropping(provider) -> None:
    provider.layers = _layers()[:1]

    with pytest.raises(CreationTextCompositionError, match="no researched text for The same"):
        _compose()


def test_unmet_layer_is_reported_not_rendered(provider) -> None:
    provider.unmet = ["The facts do not say why the day matters"]

    with pytest.raises(CreationTextCompositionError, match="do not say why the day matters"):
        _compose()
    assert provider.reviewed == []


def test_shaping_cannot_write_its_own_text(provider) -> None:
    provider.shape_ops = lambda local, english: [
        {"op": "add_text", "text": "Since 1066", "start_s": 0, "end_s": 2}
    ]

    # Without its added line the shaping split nothing, so the asked word sequences are not met.
    with pytest.raises(CreationTextCompositionError, match="only be shaped|unrequested text"):
        _compose()


def test_shaping_cannot_reword_researched_text(provider) -> None:
    provider.shape_ops = lambda local, english: [
        {"op": "rewrite_text", "selector": {"ids": [local["id"]]}, "text": "Bugün kutluyoruz"}
    ]

    with pytest.raises(CreationTextCompositionError, match="only be shaped"):
        _compose()


def test_shaping_cannot_move_the_translation_off_its_place(provider) -> None:
    provider.shape_ops = lambda local, english: [
        {
            "op": "replace_text_sequence",
            "selector": {"ids": [local["id"]]},
            "segments": LOCAL.split(),
        },
        {
            "op": "replace_text_sequence",
            "selector": {"ids": [english["id"]]},
            "segments": ENGLISH.split(),
            "patch": {"position": "middle"},
        },
    ]

    with pytest.raises(CreationTextCompositionError, match="moved off its requested bottom"):
        _compose()


def test_an_unsplit_layer_is_incomplete(provider) -> None:
    provider.shape_ops = lambda local, english: [
        {
            "op": "replace_text_sequence",
            "selector": {"ids": [local["id"]]},
            "segments": LOCAL.split(),
        }
    ]

    with pytest.raises(CreationTextCompositionError, match="en text is not shown a piece"):
        _compose()


def test_plain_explanation_ask_keeps_footage_window_research(monkeypatch, provider) -> None:
    written = []

    def write(agent, input, *, ctx=None):  # noqa: ARG001
        written.append(input.request)
        raise AssertionError("reached the footage-window writer")

    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    plain = [BriefRequirement(id="r1", kind="text", scope="global", description="Explain it")]

    with pytest.raises(AssertionError, match="footage-window writer"):
        compose_creation_text(_plan().snapshot, creator_request=REQUEST, text_requirements=plain)
    assert written == [REQUEST]


def test_a_one_bundle_program_serializes_as_before() -> None:
    program = TextCompositionProgram(base_digest="0" * 64, operations=[{"op": "remove_text"}])

    assert json.loads(program.model_dump_json()) == {
        "version": 1,
        "base_digest": "0" * 64,
        "operations": [{"op": "remove_text"}],
    }


def test_a_second_bundle_alone_is_refused() -> None:
    from app.services.creation_text_composition import replay_text_composition

    plan = compile_proposal_execution_plan(_plan().snapshot)
    from app.services.creation_text_composition import _digest, _lanes

    texts, slots = _lanes(plan)
    program = TextCompositionProgram(
        base_digest=_digest(texts, slots), then=[{"op": "remove_text", "bar_index": 0}]
    )
    with pytest.raises(CreationTextCompositionError, match="second bundle only"):
        replay_text_composition(plan, program)


def _retime_then_split(local, english):
    return sum(
        (
            [
                {
                    "op": "set_texts_timing",
                    "selector": {"ids": [r["id"]]},
                    "start_s": 6,
                    "end_s": 12,
                },
                {
                    "op": "replace_text_sequence",
                    "selector": {"ids": [r["id"]]},
                    "segments": r["text"].split(),
                },
            ]
            for r in (local, english)
        ),
        [],
    )


def test_shaping_cannot_move_research_onto_unreviewed_footage(provider) -> None:
    provider.layers[0].end_s = provider.layers[1].end_s = 6
    provider.shape_ops = _retime_then_split
    drops = []

    # KRI-1376: the retimes are dropped; the reviewed words stay on their reviewed footage.
    composed = compose_creation_text(
        _plan().snapshot, creator_request=REQUEST, text_requirements=REQUIREMENTS, drops=drops
    )
    rows = compile_proposal_execution_plan(composed)["text_elements"]
    for words in (LOCAL, ENGLISH):
        pieces = _rows(rows, words)
        assert [row["text"] for row in pieces] == words.split()
        assert pieces[0]["start_s"] >= 0 and pieces[-1]["end_s"] <= 6 + 1 / 30
    assert [(drop.reason, drop.op) for drop in drops] == [
        ("layer_shape", "set_texts_timing"),
        ("layer_shape", "set_texts_timing"),
    ]


def test_a_translation_shown_after_its_text_is_not_simultaneous(provider) -> None:
    provider.layers[0].end_s = 6
    provider.layers[1].start_s = 6

    with pytest.raises(CreationTextCompositionError, match="on screen together with"):
        _compose()


def test_shaping_cannot_make_the_translation_follow_its_text(provider) -> None:
    def halves(local, english):
        return [
            op
            for row, start, end in ((local, 0, 6), (english, 6, 12))
            for op in (
                {
                    "op": "set_texts_timing",
                    "selector": {"ids": [row["id"]]},
                    "start_s": start,
                    "end_s": end,
                },
                {
                    "op": "replace_text_sequence",
                    "selector": {"ids": [row["id"]]},
                    "segments": row["text"].split(),
                },
            )
        ]

    provider.shape_ops = halves

    with pytest.raises(CreationTextCompositionError, match="translation is not on screen together"):
        _compose()


def test_word_unit_may_build_up_one_word_per_step(monkeypatch, provider) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", True)
    provider.shape_ops = lambda local, english: [
        {
            "op": "replace_text_sequence",
            "selector": {"ids": [row["id"]]},
            "segments": row["text"].split(),
            "mode": "cumulative",
        }
        for row in (local, english)
    ]

    composed = _compose()

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert LOCAL in {row["text"] for row in rows} and LOCAL.split()[0] in {
        row["text"] for row in rows
    }


def _two_chunks(local, english):
    return [
        {
            "op": "replace_text_sequence",
            "selector": {"ids": [r["id"]]},
            "segments": [" ".join(r["text"].split()[:2]), " ".join(r["text"].split()[2:])],
        }
        for r in (local, english)
    ]


def test_word_unit_needs_one_word_per_piece(provider) -> None:
    provider.shape_ops = _two_chunks

    with pytest.raises(CreationTextCompositionError, match="one word at a time"):
        _compose()


def test_phrase_unit_keeps_multiword_pieces(provider) -> None:
    provider.shape_ops = _two_chunks
    phrases = [_sequence(req.id, req.facts["position"], req.description) for req in REQUIREMENTS]
    for req in phrases:
        req.facts["unit"] = "phrase"

    composed = compose_creation_text(
        _plan().snapshot, creator_request=REQUEST, text_requirements=phrases
    )

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert " ".join(LOCAL.split()[:2]) in {row["text"] for row in rows}


# -- approval paths: what the render reads from the approved brief binding ---------------------

RESEARCH_ASK = BriefRequirement(
    id="r0",
    kind="text",
    scope="global",
    description="Explain why Founders Day matters, from sources",
    facts={
        "research": {
            "topic": "Founders Day in Exampleland",
            "request_quote": "explains why Founders Day matters in Exampleland",
        }
    },
)


def _approved(prepared) -> dict:
    from app.kria.brief import CreativeBrief
    from app.kria.brief_binding import BriefBinding

    binding = BriefBinding.create(
        "thread-1",
        CreativeBrief(requirements=[RESEARCH_ASK, *REQUIREMENTS]),
        latest_message=REQUEST,
        prepared_research=None if prepared is None else prepared.model_dump(mode="json"),
    )
    return {"creator_brief_binding": binding.model_dump(mode="json")}


def _prepare(monkeypatch, plan):
    from app.kria import research_preview
    from app.kria.brief import CreativeBrief
    from app.services import phone_montage_preflight

    monkeypatch.setattr(phone_montage_preflight, "preflight_montage_plan", lambda **_: (plan, {}))
    inputs = research_preview.ResearchInputs(
        creator_id="creator-1",
        thread_id="thread-1",
        item_id="item-1",
        edit_format="montage",
        brief=CreativeBrief(requirements=[RESEARCH_ASK, *REQUIREMENTS]),
        strategy={},
        media_snapshot={},
        creator_request=REQUEST,
    )
    return research_preview.prepare(inputs, visuals=[])


def _forbid_research(monkeypatch):
    for agent in (GroundedResearchAgent, ResearchLayersAgent, ResearchReviewAgent):
        monkeypatch.setattr(
            agent,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("approved research re-ran")),
        )


def test_incident_shape_approval_has_no_prepared_research_so_render_researches(provider) -> None:
    from app.tasks.generative_build import _approved_research

    snapshot = _approved(None)

    assert _approved_research(snapshot) is None
    composed = compose_creation_text(
        _plan().snapshot,
        creator_request=REQUEST,
        text_requirements=REQUIREMENTS,
        prepared_research=_approved_research(snapshot),
    )
    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert [row["text"] for row in _rows(rows, ENGLISH)] == ENGLISH.split()
    assert provider.searches == 1


def test_preview_prepares_reviewed_layers_and_render_replays_them_unchanged(
    monkeypatch, provider
) -> None:
    from app.kria import research_preview
    from app.tasks.generative_build import _approved_research

    plan = _plan()
    prepared = _prepare(monkeypatch, plan)

    assert prepared.status == "ready" and prepared.texts == []
    assert [(layer.text, layer.position) for layer in prepared.layers] == [
        (LOCAL, "middle"),
        (ENGLISH, "bottom"),
    ]
    assert [[fact.id for fact in layer.facts] for layer in prepared.layers] == [["fact-1"]] * 2
    lines = research_preview.preview_lines(prepared)
    assert LOCAL in lines and ENGLISH in lines
    assert provider.searches == 1 and len(provider.reviewed) == 1

    approved = _approved_research(_approved(prepared))
    assert approved == prepared
    _forbid_research(monkeypatch)
    composed = compose_creation_text(
        plan.snapshot,
        creator_request=REQUEST,
        text_requirements=REQUIREMENTS,
        prepared_research=approved,
    )

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    assert [row["text"] for row in _rows(rows, LOCAL)] == LOCAL.split()
    assert {row["position"] for row in _rows(rows, ENGLISH)} == {"bottom"}
    for row in _rows(rows, LOCAL) + _rows(rows, ENGLISH):
        assert [fact["id"] for fact in row["source_params"]["research"]["facts"]] == ["fact-1"]


def test_preview_rejection_holds_the_draft_back_before_approval(monkeypatch, provider) -> None:
    provider.approve = False

    prepared = _prepare(monkeypatch, _plan())

    assert prepared.status == "failed"
    assert "Not in the facts" in prepared.reason
    assert prepared.layers == []


def test_approved_layers_on_changed_footage_fail_instead_of_researching_again(
    monkeypatch, provider
) -> None:
    prepared = _prepare(monkeypatch, _plan())
    from app.tasks.generative_build import _approved_research

    approved = _approved_research(_approved(prepared))
    _forbid_research(monkeypatch)
    other = plan_unified_montage(
        [
            UnifiedClip(
                media_id=f"other-{index}",
                proxy_path=f"private/other-{index}.mp4",
                generation="1",
                duration_s=5,
                analysis={"understanding": {"summary": "A crowd"}},
            )
            for index in range(3)
        ],
        BriefView(target_duration_s=12),
    )

    with pytest.raises(CreationTextCompositionError, match="no longer matches its approved clips"):
        compose_creation_text(
            other.snapshot,
            creator_request=REQUEST,
            text_requirements=REQUIREMENTS,
            prepared_research=approved,
        )


def test_an_approval_without_layers_keeps_its_digest() -> None:
    from app.schemas.research import PreparedResearch

    prepared = PreparedResearch(
        status="failed",
        requirement_id="r0",
        topic="Founders Day",
        request_quote="explains why",
        basis_digest="0" * 64,
        reason="No source",
    )

    assert "layers" not in prepared.model_dump(mode="json")


def test_an_older_footage_explanation_approval_fails_plainly_for_authored_sequences(
    monkeypatch, provider
) -> None:
    """Approved before text layers existed (persisted, or a cached preview): no re-search."""
    from app.schemas.research import PreparedResearch, PreparedResearchText
    from app.services.creation_text_composition import _lanes

    plan = _plan()
    _texts, slots = _lanes(compile_proposal_execution_plan(plan.snapshot))
    slot = next(row for row in slots if row["media_id"] == "archive-1")
    source = {"title": "Gov", "url": "https://archive.example.gov/day"}
    prepared = PreparedResearch(
        status="ready",
        requirement_id="r0",
        topic="Founders Day in Exampleland",
        request_quote="explains why Founders Day matters in Exampleland",
        basis_digest="0" * 64,
        texts=[
            PreparedResearchText(
                text="Exampleland: The founder is remembered",
                media_ids=["archive-1"],
                start_s=slot["output_start_s"],
                end_s=slot["output_end_s"],
                sources=[source],
                facts=[{"id": "fact-1", "text": FOUNDER_FACTS[1], "sources": [source]}],
            )
        ],
    )
    _forbid_research(monkeypatch)

    with pytest.raises(CreationTextCompositionError, match="cannot be shown word by word"):
        compose_creation_text(
            plan.snapshot,
            creator_request=REQUEST,
            text_requirements=REQUIREMENTS,
            prepared_research=prepared,
        )


def test_a_literal_title_sequence_keeps_footage_window_research(monkeypatch, provider) -> None:
    written = []

    def write(agent, input, *, ctx=None):  # noqa: ARG001
        written.append(input.request)
        raise AssertionError("reached the footage-window writer")

    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    asks = [
        BriefRequirement(
            id="r1",
            kind="text_sequence",
            scope="title",
            literal="Founders Day",
            facts={"mode": "replace", "unit": "word"},
        ),
        BriefRequirement(id="r2", kind="text", scope="global", description="Explain it"),
    ]

    with pytest.raises(AssertionError, match="footage-window writer"):
        compose_creation_text(_plan().snapshot, creator_request=REQUEST, text_requirements=asks)
    assert written == [REQUEST]


# -- second review: forward translation, saved-replay invariants, approved source cuts ----------


def test_a_translation_listed_before_its_original_is_supported(provider) -> None:
    english, local = _layers()[1], _layers()[0]
    provider.layers = [english.model_copy(update={"translation_of": 1}), local]

    rows = compile_proposal_execution_plan(_compose())["text_elements"]

    assert [row["text"] for row in _rows(rows, ENGLISH)] == ENGLISH.split()
    assert [row["text"] for row in _rows(rows, LOCAL)] == LOCAL.split()


def _tampered(provider, edit) -> None:
    """Compose a valid snapshot, then change only its saved ``then`` bundle and replay it."""
    from app.agents._model_client import default_client
    from app.schemas.edit_proposal import EditProposalSnapshot

    composed = _compose()
    shaping = provider.shaping_input
    local, english = _bar(shaping.variant_snapshot, LOCAL), _bar(shaping.variant_snapshot, ENGLISH)
    reply = {"intent": "edit", "confidence": 0.9, "reply": "x", "ops": edit(local, english)}
    then = CreationTextComposer(default_client()).parse(json.dumps(reply), shaping).ops
    data = json.loads(composed.model_dump_json())
    data["text_composition"]["then"] = then
    with pytest.raises(CreationTextCompositionError):
        compile_proposal_execution_plan(EditProposalSnapshot.model_validate(data))


def test_saved_replay_refuses_multiword_pieces_for_a_word_ask(provider) -> None:
    _tampered(provider, _two_chunks)


def test_saved_replay_refuses_research_moved_off_its_reviewed_window(provider) -> None:
    provider.layers[0].end_s = provider.layers[1].end_s = 6
    _tampered(provider, _retime_then_split)


def test_saved_replay_refuses_layers_moved_to_another_place(provider) -> None:
    _tampered(
        provider,
        lambda local, english: [
            {
                "op": "replace_text_sequence",
                "selector": {"ids": [row["id"]]},
                "segments": row["text"].split(),
                "patch": {"position": "top"},
            }
            for row in (local, english)
        ],
    )


def test_a_program_without_reviewed_layers_serializes_as_before() -> None:
    program = TextCompositionProgram(
        base_digest="0" * 64, operations=[{"op": "remove_text"}], then=[{"op": "remove_text"}]
    )

    assert set(json.loads(program.model_dump_json())) == {
        "version",
        "base_digest",
        "operations",
        "then",
    }


def test_approved_layers_on_recut_source_footage_fail(monkeypatch, provider) -> None:
    from app.tasks.generative_build import _approved_research

    plan = _plan()
    approved = _approved_research(_approved(_prepare(monkeypatch, plan)))
    _forbid_research(monkeypatch)
    recut = plan.snapshot.model_copy(
        update={
            "fast_cuts": [
                cut.model_copy(
                    update={
                        "source_start_s": cut.source_start_s + 0.25,
                        "source_end_s": cut.source_end_s + 0.25,
                    }
                )
                for cut in plan.snapshot.fast_cuts
            ]
        }
    )
    before = compile_proposal_execution_plan(plan.snapshot)["story_timeline"]
    after = compile_proposal_execution_plan(recut)["story_timeline"]
    assert [row["media_id"] for row in before] == [row["media_id"] for row in after]
    assert [row["output_start_s"] for row in before] == [row["output_start_s"] for row in after]
    assert [row["source_start_s"] for row in before] != [row["source_start_s"] for row in after]

    with pytest.raises(CreationTextCompositionError, match="no longer matches its approved clips"):
        compose_creation_text(
            recut,
            creator_request=REQUEST,
            text_requirements=REQUIREMENTS,
            prepared_research=approved,
        )


EXPECTATION = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "kria_replay"
    / "kri1352_bilingual_research"
    / "expectation.json"
)


def test_offline_result_meets_the_kri1352_expectation(provider) -> None:
    expected = json.loads(EXPECTATION.read_text(encoding="utf-8"))
    assert [
        {key: req[key] for key in ("id", "kind", "scope", "literal", "facts")}
        for req in expected["brief"]
    ] == [
        {key: getattr(req, key) for key in ("id", "kind", "scope", "literal", "facts")}
        for req in REQUIREMENTS
    ]

    composed = _compose()

    rows = compile_proposal_execution_plan(composed)["text_elements"]
    shapes = composed.text_composition.research_layers
    spans = []
    for shape, want in zip(shapes, expected["expected"]["layers"], strict=True):
        pieces = sorted(
            (
                row
                for row in rows
                if row["id"] == shape.bar_id
                or (row.get("source_params") or {}).get("sequence_source_id") == shape.bar_id
            ),
            key=lambda row: row["start_s"],
        )
        assert shape.position == want["position"] and shape.unit == want["unit"]
        assert shape.translation_of == want["translation_of"]
        if want["language"] == "not en":
            assert shape.language != "en"
        else:
            assert shape.language == want["language"]
        assert all(len(piece["text"].split()) == 1 for piece in pieces)
        assert {piece["position"] for piece in pieces} == {want["position"]}
        for piece in pieces:
            research = piece["source_params"]["research"]
            assert research["facts"] and research["sources"]
        spans.append((pieces[0]["start_s"], pieces[-1]["end_s"]))
    assert spans[0] == spans[1]
    assert [name for name in expected["must_fail"] if not callable(globals().get(name))] == []


def test_caption_review_prompt_is_unchanged_without_layers() -> None:
    from app.services.researched_text import (
        ResearchCopyInput,
        ResearchCopyOutput,
        ResearchReviewInput,
    )

    review = ResearchReviewInput(
        context=ResearchCopyInput(
            request=REQUEST,
            snapshot={"slots": []},
            evidence=evidence_from_response(_response(FOUNDER_FACTS)),
        ),
        proposed=ResearchCopyOutput(),
    )
    prompt = ResearchReviewAgent.__new__(ResearchReviewAgent).render_prompt(review)

    assert "object identification. Return ONE JSON OBJECT for the ENTIRE revision" in prompt
    assert "text layers" not in prompt
    assert '"layers"' not in prompt
