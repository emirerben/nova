"""On-screen text is opt-in: no generated title, captions, or labels unless asked.

Product rule (2026-09-16): when the creator's request does not ask for words on
the video, the edit carries no AI-authored text. Creator copy — their title,
labels, closing title, and thoughts they write in the editor — always renders.
Snapshots from before this contract (``on_screen_text_requested is None``) keep
their legacy projection so already-approved edits do not change.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.edit_proposal_build as proposal_build
from app.agents._runtime import SchemaError, TerminalError
from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence
from app.agents.edit_proposal import EditProposalAgent, _on_screen_text_note
from app.pipeline.guided_story import _text_elements
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MontageTextBinding,
    ProposalBrief,
    StoryBeat,
    ai_on_screen_text_allowed,
    parse_edit_proposal,
)
from app.services.edit_direction_planner import (
    _compatibility_beats,
    deterministic_guided_beats,
)
from tests.tasks.test_creator_shot_labels_contract import (
    OPENING_TITLE,
    _agent_input,
    _barcelona_strategy,
    _compile,
    _refs,
)
from tests.tasks.test_edit_proposal_build import _Db, _prod_item, _Result

AI_THOUGHTS = ["Stone arches frame the plaza", "Towers rise over the street"]
NO_TEXT_REQUEST = "13-second Barcelona trailer. Cut on the beat, warm grade."
CAPTION_REQUEST = "13-second Barcelona trailer, and add a short caption to each clip."


def _snapshot(**overrides) -> EditProposalSnapshot:
    values = {
        "direction": "guided_story",
        "pace": "fast",
        "duration_s": 6,
        "title": "Barcelona in motion",
        "media": _refs(),
        "story_beats": [
            StoryBeat(
                beat_id=f"b{index}",
                topic=topic,
                thought=thought,
                thought_source="ai_draft",
                media_ids=[media_id],
                duration_s=3,
            )
            for index, (topic, thought, media_id) in enumerate(
                zip(["Plaza", "Towers"], AI_THOUGHTS, ["ios-2850", "ios-A1DB"], strict=True)
            )
        ],
    }
    values.update(overrides)
    return EditProposalSnapshot(**values)


def _texts(snapshot: EditProposalSnapshot) -> list[str]:
    return [row["text"] for row in _compile(snapshot)["text_elements"]]


def _five_beat_output(thoughts: list[str]) -> dict:
    """An unlabeled guided draft: at most five beats covering all six sources."""

    media = [["asset-gaudi"], ["ios-6EF8"], ["ios-2850"], ["ios-A1DB"], ["ios-ABE8", "ios-975B"]]
    topics = ["Portrait", "Basilica", "Plaza", "Towers", "Streets"]
    durations = [2.5, 2.0, 2.0, 2.0, 4.5]
    return {
        "title": "Barcelona week",
        "duration_s": 13,
        "story_beats": [
            {
                "topic": topic,
                "thought": thought,
                "media_ids": media_ids,
                "layout": "fullscreen",
                "duration_s": duration_s,
            }
            for topic, thought, media_ids, duration_s in zip(
                topics, thoughts, media, durations, strict=True
            )
        ],
    }


def _unlabeled_input(**overrides):
    return _agent_input(
        opening_title=None,
        opening_title_duration_s=None,
        shot_labels=None,
        closing_title=None,
        **overrides,
    )


def _no_copy_strategy(**overrides):
    return _barcelona_strategy(
        opening_title=None,
        opening_title_duration_s=None,
        shot_labels=None,
        closing_title=None,
        **overrides,
    )


def test_ai_text_is_allowed_only_when_requested_or_legacy() -> None:
    assert ai_on_screen_text_allowed(True, "guided_story")
    assert ai_on_screen_text_allowed(None, "guided_story")
    assert not ai_on_screen_text_allowed(False, "guided_story")
    assert not ai_on_screen_text_allowed(False, "fast_montage")
    # A text explainer is a text-led format: its copy is the requested edit.
    assert ai_on_screen_text_allowed(False, "text_explainer")


# ── Renderer ──────────────────────────────────────────────────────────────────


def test_renderer_burns_no_generated_text_when_the_creator_did_not_ask() -> None:
    assert _texts(_snapshot(on_screen_text_requested=False)) == []


def test_renderer_keeps_requested_and_legacy_text() -> None:
    expected = ["Barcelona in motion", *AI_THOUGHTS]

    assert _texts(_snapshot(on_screen_text_requested=True)) == expected
    # Approved snapshots from before the contract render exactly as before.
    assert _texts(_snapshot()) == expected


def test_renderer_always_burns_creator_copy() -> None:
    beats = list(_snapshot().story_beats)
    beats[1] = beats[1].model_copy(update={"thought": "Our street", "thought_source": "user"})

    snapshot = _snapshot(
        on_screen_text_requested=False,
        title=OPENING_TITLE,
        opening_title=OPENING_TITLE,
        story_beats=beats,
    )

    assert _texts(snapshot) == [OPENING_TITLE, "Our street"]


def test_renderer_drops_generated_montage_title_and_bindings_unless_requested() -> None:
    cuts = [
        FastMontageCut(
            cut_id=f"cut-{index}",
            media_id=media_id,
            source_start_s=0,
            source_end_s=1,
            output_duration_s=1,
            role=role,
        )
        for index, (media_id, role) in enumerate(
            zip(["ios-2850", "ios-6EF8", "ios-A1DB"], ["hook", "build", "payoff"], strict=True),
            start=1,
        )
    ]
    windows = [{"start_s": float(index), "end_s": float(index + 1)} for index in range(3)]

    def texts(**overrides) -> list[str]:
        values = {
            "direction": "fast_montage",
            "duration_s": 3,
            "fast_cuts": cuts,
            "story_beats": _compatibility_beats(cuts),
            "montage_text_bindings": [MontageTextBinding(media_id="ios-2850", text="Plaza")],
            **overrides,
        }
        rows = _text_elements(
            _snapshot(**values), windows, {"text_effect": "static"}, compiler_version=6
        )
        return [row["text"] for row in rows]

    # Advisory per-cut copy and the generated hook title are both AI-authored.
    assert texts(on_screen_text_requested=False) == []
    assert texts(on_screen_text_requested=False, montage_text_bindings=[]) == []
    assert texts() == ["Plaza"]
    assert texts(montage_text_bindings=[]) == ["Barcelona in motion"]


def test_legacy_snapshots_keep_their_serialized_shape() -> None:
    assert "on_screen_text_requested" not in _snapshot().model_dump(mode="json")
    dumped = _snapshot(on_screen_text_requested=False).model_dump(mode="json")
    assert dumped["on_screen_text_requested"] is False


# ── Planner and fallback ──────────────────────────────────────────────────────


def test_planner_prompt_asks_for_no_copy_only_when_text_was_not_requested() -> None:
    assert _on_screen_text_note(_unlabeled_input()) == ""
    assert _on_screen_text_note(_unlabeled_input(on_screen_text_requested=True)) == ""

    rendered = EditProposalAgent(None).render_prompt(  # type: ignore[arg-type]
        _unlabeled_input(on_screen_text_requested=False)
    )

    assert "ON-SCREEN TEXT: the creator did not ask for text on the video" in rendered
    # Live evals: the model must keep the story structure, not swap in fast cuts.
    assert "never fast_cuts instead" in rendered
    assert "`title` only names the plan and is never shown." in rendered


def test_planner_clears_drafted_copy_when_text_was_not_requested() -> None:
    payload = _five_beat_output([f"Draft caption {index}" for index in range(5)])
    payload["montage_text_bindings"] = [{"media_id": "ios-2850", "text": "Plaza"}]

    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(payload), _unlabeled_input(on_screen_text_requested=False)
    )

    assert [beat.thought for beat in output.story_beats] == [""] * 5
    assert output.montage_text_bindings == []


def test_planner_still_requires_thoughts_when_text_is_allowed() -> None:
    payload = _five_beat_output(["", "Two", "Three", "Four", "Five"])

    for flag in (None, True):
        with pytest.raises(SchemaError, match="thoughts cannot be empty"):
            EditProposalAgent(None).parse(  # type: ignore[arg-type]
                json.dumps(payload), _unlabeled_input(on_screen_text_requested=flag)
            )


def test_fallback_never_invents_captions() -> None:
    beats = deterministic_guided_beats(_refs(), 13)

    assert beats
    assert all(beat.thought == "" for beat in beats)
    assert all(beat.topic for beat in beats)


# ── Main Creator boundary ─────────────────────────────────────────────────────


def test_main_creator_grants_ai_text_only_with_a_verbatim_request() -> None:
    from app.routes.creator_agent import _apply_explicit_render_intent

    strategy = _no_copy_strategy(on_screen_text_requested=True)
    evidence = CreatorRenderIntentEvidence(
        on_screen_text_requested="add a short caption to each clip"
    )

    granted = _apply_explicit_render_intent(
        strategy, CAPTION_REQUEST, render_intent_evidence=evidence
    )
    fabricated = _apply_explicit_render_intent(
        strategy, NO_TEXT_REQUEST, render_intent_evidence=evidence
    )
    unsupported = _apply_explicit_render_intent(strategy, CAPTION_REQUEST)

    assert granted.on_screen_text_requested is True
    assert fabricated.on_screen_text_requested is False
    assert unsupported.on_screen_text_requested is False


def test_request_without_text_renders_no_text_end_to_end(monkeypatch) -> None:
    """A plain request through Main Creator, brief, failed planner, and renderer."""

    from app.routes.creator_agent import (
        _apply_explicit_render_intent,
        _seed_guided_specialist_brief,
    )
    from app.services import creator_capabilities
    from app.services.creator_capabilities import (
        compile_strategy_to_plan,
        resolve_creator_manifest,
    )

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    refs = _refs()
    manifest = resolve_creator_manifest(
        item_id="item-barcelona",
        edit_format="montage",
        media=[
            {"media_id": ref.media_id, "kind": ref.kind, "duration_s": ref.duration_s}
            for ref in refs
        ],
    )
    # The model tried to grant text without any creator request for it.
    strategy = _apply_explicit_render_intent(
        _no_copy_strategy(on_screen_text_requested=True), NO_TEXT_REQUEST
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    clips = [ref for ref in refs if ref.kind == "video"]
    photos = [ref for ref in refs if ref.kind == "image"]
    item = _prod_item(
        item_id,
        clip_assignments=[
            {
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "duration_s": ref.duration_s,
            }
            for ref in clips
        ],
    )
    item.edit_proposal = None
    _seed_guided_specialist_brief(
        item, edit_plan, summary="Barcelona week trailer.", creator_request=NO_TEXT_REQUEST
    )
    seeded = parse_edit_proposal(item.edit_proposal)
    assert seeded is not None
    assert isinstance(seeded.brief, ProposalBrief)
    assert seeded.brief.on_screen_text_requested is False
    item.edit_proposal = seeded.model_copy(
        update={"status": "analyzing", "generation_attempt_id": "attempt-1"}
    ).model_dump(mode="json")

    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(proposal_build, "_pool_refs", lambda *_a, **_kw: photos)
    monkeypatch.setattr(
        proposal_build,
        "_analyze_clip_assignments",
        lambda assignments, *_a, **_kw: list(zip(assignments, clips, strict=True)),
    )
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda *_a, **_kw: (_ for _ in ()).throw(TerminalError("schema: selected 0 sources")),
    )

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, False
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "draft", persisted.failure
    snapshot = persisted.draft
    assert snapshot is not None
    assert snapshot.on_screen_text_requested is False
    assert _compile(snapshot)["text_elements"] == []
