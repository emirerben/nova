"""Exact creator copy beyond the opening title: shot labels and closing title.

Production regression (job ac795019, plan item 35a3681b, 2026-09-15): a Kria
chat request asked for an opening title held 2s, six day-labelled shots, and a
closing line. Only ``opening_title`` was a typed field; the labels rode along as
prose, the guided planner failed schema validation on every attempt, and the
metadata-free fallback burned five generic captions ("A few moments,
together.", ...) over the edit. These tests pin the typed contract end to end:
Main Creator grounding -> brief -> planner parse -> fallback -> renderer.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.edit_proposal_build as proposal_build
from app.agents._runtime import SchemaError, TerminalError
from app.agents._schemas.creator_agent import CreativeStrategy, CreatorRenderIntentEvidence
from app.agents.edit_proposal import (
    MAX_GUIDED_DRAFT_BEATS,
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    MontageCadenceConstraint,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.edit_direction_planner import GUIDED_STORY_MAX_BEATS
from tests.tasks.test_edit_proposal_build import (
    _Db,
    _prepare_terminal_agent_attempt,
    _prod_item,
    _Result,
)

PROMPT = (
    "13-second trailer. Open with '1 CITY. 7 DAYS. 7 VIDEOS.' for 2 seconds, then 6 shots of "
    "1.5 seconds each with a small day label (MON · 5 LEGENDS, TUE · SAGRADA FAMÍLIA, "
    "WED · HIDDEN IN PLAIN SIGHT, THU · HUMAN TOWERS, FRI · MESSI'S NAPKIN, "
    "SAT · TOURIST MISTAKES). End on 'BARCELONA WEEK STARTS MONDAY'. Cut on the beat, warm grade."
)
OPENING_TITLE = "1 CITY. 7 DAYS. 7 VIDEOS."
CLOSING_TITLE = "BARCELONA WEEK STARTS MONDAY"
LABELS = [
    "MON · 5 LEGENDS",
    "TUE · SAGRADA FAMÍLIA",
    "WED · HIDDEN IN PLAIN SIGHT",
    "THU · HUMAN TOWERS",
    "FRI · MESSI'S NAPKIN",
    "SAT · TOURIST MISTAKES",
]
GENERIC_FALLBACK_COPY = {
    "A few moments, together.",
    "Details worth noticing.",
    "One last look.",
    "A different angle on the moment.",
    "A final frame to remember.",
}
# The exact prod media set: five portrait videos and one photo.
BARCELONA_MEDIA = [
    ("ios-2850", "video", 10.05424, "public square with palm trees and people"),
    ("ios-6EF8", "video", 14.326712, "Sagrada Familia"),
    ("ios-ABE8", "video", 16.695147, "interior of Sagrada Familia cathedral"),
    ("ios-A1DB", "video", 12.863855, "human tower (Castell)"),
    ("ios-975B", "video", 17.995465, "crowded street"),
    ("asset-gaudi", "image", None, "Portrait of a bearded man"),
]


def _refs() -> list[MediaRef]:
    return [
        MediaRef(
            lane="asset" if kind == "image" else "clip",
            media_id=media_id,
            gcs_path=f"users/u/{media_id}.{'jpg' if kind == 'image' else 'mp4'}",
            generation="1",
            kind=kind,
            duration_s=duration_s,
            analysis={"subject": subject},
        )
        for media_id, kind, duration_s, subject in BARCELONA_MEDIA
    ]


def _agent_input(**overrides) -> EditProposalAgentInput:
    values = {
        "direction": "guided_story",
        "goal": "Barcelona week trailer",
        "creator_request": PROMPT,
        "pace": "fast",
        "target_duration_s": 13,
        "video_reuse_policy": "once",
        "opening_title": OPENING_TITLE,
        "opening_title_duration_s": 2.0,
        "shot_labels": LABELS,
        "closing_title": CLOSING_TITLE,
        "media": [
            EditProposalMedia(
                media_id=media_id,
                lane="asset" if kind == "image" else "clip",
                kind=kind,
                duration_s=duration_s,
                subject=subject,
            )
            for media_id, kind, duration_s, subject in BARCELONA_MEDIA
        ],
    }
    values.update(overrides)
    return EditProposalAgentInput(**values)


def _labeled_output(thoughts: list[str], media_ids: list[str] | None = None) -> dict:
    media_ids = media_ids or [
        "asset-gaudi",
        "ios-6EF8",
        "ios-2850",
        "ios-A1DB",
        "ios-ABE8",
        "ios-975B",
    ]
    durations = [3.5, 1.5, 1.5, 1.5, 1.5, 3.5]
    return {
        "title": "Barcelona week",
        "duration_s": 13,
        "story_beats": [
            {
                "topic": f"Day {index + 1}",
                "thought": thought,
                "media_ids": [media_id],
                "layout": "fullscreen",
                "duration_s": duration_s,
            }
            for index, (thought, media_id, duration_s) in enumerate(
                zip(thoughts, media_ids, durations, strict=True)
            )
        ],
    }


def _compile(snapshot: EditProposalSnapshot) -> dict:
    from app.pipeline.guided_story import compile_execution_plan

    return compile_execution_plan(
        {
            "proposal_version": 1,
            "media_digest": canonical_media_digest(snapshot.media),
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                {
                    "lane": ref.lane,
                    "media_id": ref.media_id,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "kind": ref.kind,
                }
                for ref in snapshot.media
            ],
        },
        track=None,
    )


# ── Planner (EditProposalAgent) ───────────────────────────────────────────────


def test_planner_binds_exact_labels_even_when_model_echo_drifts() -> None:
    """Six labeled beats are valid; the stored copy is the creator's, verbatim."""

    payload = _labeled_output(
        [label.lower().replace("·", "-").replace("Í", "i") for label in LABELS]
    )
    # The prod model also sketched fast-cut fields this direction ignores.
    payload["fast_cuts"] = [{"cut_id": "c1", "media_id": "ios-2850", "transition": "hard_cut"}]
    payload["montage_text_bindings"] = [{"media_id": "ios-2850", "text": OPENING_TITLE}]

    output = EditProposalAgent(None).parse(json.dumps(payload), _agent_input())  # type: ignore[arg-type]

    assert [beat.thought for beat in output.story_beats] == LABELS
    assert output.fast_cuts is None
    assert output.montage_text_bindings == []


def test_planner_rejects_reworded_or_reordered_labels() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    reworded = [*LABELS[:-1], "SAT · COMMON TOURIST ERRORS"]
    with pytest.raises(SchemaError, match="reworded or reordered"):
        agent.parse(json.dumps(_labeled_output(reworded)), _agent_input())
    with pytest.raises(SchemaError, match="expected 5 labeled beats"):
        agent.parse(json.dumps(_labeled_output(LABELS)), _agent_input(shot_labels=LABELS[:5]))


def test_planner_allows_unlabeled_beats_only_as_title_holds() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = _labeled_output(["", *LABELS[:4], ""], media_ids=None)
    labels = LABELS[:4]

    output = agent.parse(json.dumps(payload), _agent_input(shot_labels=labels))

    assert [beat.thought for beat in output.story_beats] == ["", *labels, ""]
    middle_gap = _labeled_output([LABELS[0], "", *LABELS[1:5]])
    with pytest.raises(SchemaError, match="only an opening or closing title hold beat"):
        agent.parse(json.dumps(middle_gap), _agent_input(shot_labels=LABELS[:5]))


def test_planner_without_labels_keeps_the_five_beat_cap() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = _labeled_output([f"Stone detail {index} catches the light." for index in range(6)])
    with pytest.raises(SchemaError, match="at most 5 items"):
        agent.parse(json.dumps(payload), _agent_input(shot_labels=None, closing_title=None))


def test_prompt_carries_labels_as_a_contract() -> None:
    prompt = EditProposalAgent(None).render_prompt(_agent_input())  # type: ignore[arg-type]

    assert "CREATOR TEXT CONTRACT (exact creator-authored" in prompt
    assert json.dumps(LABELS, ensure_ascii=False) in prompt
    assert "Return exactly 6 labeled story beats" in prompt
    assert "over the first 2s" in prompt
    legacy = EditProposalAgent(None).render_prompt(  # type: ignore[arg-type]
        _agent_input(shot_labels=None, closing_title=None)
    )
    assert "CREATOR TEXT CONTRACT (exact creator-authored" not in legacy


# ── Deterministic recovery ────────────────────────────────────────────────────


def test_labeled_fallback_never_uses_generic_copy_and_matches_subjects() -> None:
    from app.services.edit_direction_planner import deterministic_labeled_beats

    beats = deterministic_labeled_beats(
        _refs(),
        13,
        shot_labels=LABELS,
        opening_title=OPENING_TITLE,
        opening_title_duration_s=2.0,
        closing_title=CLOSING_TITLE,
    )

    assert [beat.thought for beat in beats] == LABELS
    assert all(beat.thought_source == "user" for beat in beats)
    assert not GENERIC_FALLBACK_COPY & {beat.thought for beat in beats}
    by_label = {beat.thought: beat.media_ids for beat in beats}
    assert by_label["THU · HUMAN TOWERS"] == ["ios-A1DB"]
    assert by_label["TUE · SAGRADA FAMÍLIA"][0] in {"ios-6EF8", "ios-ABE8"}
    used_videos = [media_id for beat in beats for media_id in beat.media_ids if "ios-" in media_id]
    assert len(used_videos) == len(set(used_videos))
    # Holds merge into the first/last shot when no spare source exists.
    assert [beat.duration_s for beat in beats] == [3.5, 1.5, 1.5, 1.5, 1.5, 3.5]


def test_labeled_fallback_gives_titles_their_own_shot_when_sources_spare() -> None:
    from app.services.edit_direction_planner import deterministic_labeled_beats

    beats = deterministic_labeled_beats(
        _refs(),
        13,
        shot_labels=LABELS[:4],
        opening_title=OPENING_TITLE,
        opening_title_duration_s=2.0,
        closing_title=CLOSING_TITLE,
    )

    assert [beat.thought for beat in beats] == ["", *LABELS[:4], ""]
    assert [beat.duration_s for beat in beats] == [2.0, 2.25, 2.25, 2.25, 2.25, 2.0]
    assert len({beat.media_ids[0] for beat in beats}) == 6


def test_labeled_fallback_fails_visibly_when_labels_cannot_fit() -> None:
    from app.services.edit_direction_planner import (
        CreatorTextInfeasibleError,
        deterministic_labeled_beats,
    )

    videos_only = [ref for ref in _refs() if ref.kind == "video"][:2]
    with pytest.raises(CreatorTextInfeasibleError):
        deterministic_labeled_beats(videos_only, 13, shot_labels=LABELS)
    with pytest.raises(CreatorTextInfeasibleError):
        deterministic_labeled_beats(_refs(), 6, shot_labels=LABELS)


def test_draft_attempt_failure_keeps_labels_instead_of_generic_captions(monkeypatch) -> None:
    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None
    item.edit_proposal = proposal.model_copy(
        update={
            "brief": proposal.brief.model_copy(
                update={"shot_labels": ["ACROPOLIS · DAY 1"], "closing_title": "SEE YOU THERE"}
            )
        }
    ).model_dump(mode="json")

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "approved", persisted.failure
    snapshot = persisted.last_approved.snapshot
    assert [beat.thought for beat in snapshot.story_beats] == ["ACROPOLIS · DAY 1"]
    assert snapshot.shot_labels == ["ACROPOLIS · DAY 1"]
    texts = [element["text"] for element in _compile(snapshot)["text_elements"]]
    assert texts == ["ACROPOLIS · DAY 1", "SEE YOU THERE"]


def test_draft_attempt_fails_visibly_when_labels_cannot_fit(monkeypatch) -> None:
    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None
    item.edit_proposal = proposal.model_copy(
        update={"brief": proposal.brief.model_copy(update={"shot_labels": LABELS[:3]})}
    ).model_dump(mode="json")

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "creator_text_infeasible"
    assert persisted.last_approved is None


def test_direction_replan_to_fast_montage_refuses_labeled_snapshot() -> None:
    from app.services.edit_direction_planner import (
        CreatorTextInfeasibleError,
        plan_direction_snapshot,
    )

    snapshot = EditProposalSnapshot(
        direction="guided_story",
        duration_s=13,
        title=OPENING_TITLE,
        opening_title=OPENING_TITLE,
        shot_labels=LABELS[:1],
        media=_refs(),
        story_beats=[
            StoryBeat(
                beat_id="b1",
                topic="Day 1",
                thought=LABELS[0],
                thought_source="user",
                media_ids=["ios-975B"],
                duration_s=12,
            )
        ],
    )
    with pytest.raises(CreatorTextInfeasibleError):
        plan_direction_snapshot(
            snapshot, direction="fast_montage", goal="", pace="fast", duration_s=13
        )


# ── Renderer ──────────────────────────────────────────────────────────────────


def test_renderer_burns_title_labels_and_closing_without_overlap() -> None:
    from app.services.edit_direction_planner import deterministic_labeled_beats

    refs = _refs()
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        pace="fast",
        duration_s=13,
        title=OPENING_TITLE,
        opening_title=OPENING_TITLE,
        opening_title_duration_s=2.0,
        shot_labels=LABELS,
        closing_title=CLOSING_TITLE,
        media=refs,
        story_beats=deterministic_labeled_beats(
            refs,
            13,
            shot_labels=LABELS,
            opening_title=OPENING_TITLE,
            opening_title_duration_s=2.0,
            closing_title=CLOSING_TITLE,
        ),
        video_reuse_policy="once",
    )

    elements = _compile(snapshot)["text_elements"]
    windows = [(row["text"], row["start_s"], row["end_s"]) for row in elements]

    assert windows[0] == (OPENING_TITLE, 0.0, 2.0)
    assert windows[-1][0] == CLOSING_TITLE
    assert windows[-1][1:] == (11.0, 13.0)
    assert elements[-1]["id"] == "guided-closing-title"
    assert [text for text, _start, _end in windows[1:-1]] == LABELS
    assert [(start, end) for _text, start, end in windows[1:-1]] == [
        (2.0, 3.5),
        (3.5, 5.0),
        (5.0, 6.5),
        (6.5, 8.0),
        (8.0, 9.5),
        (9.5, 11.0),
    ]
    assert not GENERIC_FALLBACK_COPY & {text for text, _start, _end in windows}


def test_renderer_shows_no_generated_title_on_a_labeled_edit_without_one() -> None:
    refs = _refs()
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        pace="fast",
        duration_s=6,
        title="A few moments",
        shot_labels=LABELS[:2],
        media=refs,
        story_beats=[
            StoryBeat(
                beat_id=f"b{index}",
                topic="Day",
                thought=label,
                thought_source="user",
                media_ids=[media_id],
                duration_s=3,
            )
            for index, (label, media_id) in enumerate(
                zip(LABELS[:2], ["ios-975B", "ios-ABE8"], strict=True)
            )
        ],
    )

    texts = [row["text"] for row in _compile(snapshot)["text_elements"]]

    assert texts == LABELS[:2]


# ── Main Creator boundary ─────────────────────────────────────────────────────


def _barcelona_strategy(**overrides) -> CreativeStrategy:
    values = {
        "direction": "guided_story",
        "edit_format": "montage",
        "render_program": "guided",
        "pacing": "fast",
        "target_duration_s": 13,
        "opening_title": OPENING_TITLE,
        "opening_title_duration_s": 2,
        "shot_labels": LABELS,
        "closing_title": CLOSING_TITLE,
    }
    values.update(overrides)
    return CreativeStrategy(**values)


def _barcelona_evidence(**overrides) -> CreatorRenderIntentEvidence:
    values = {
        "opening_title": "Open with '1 CITY. 7 DAYS. 7 VIDEOS.' for 2 seconds",
        "opening_title_duration_s": "Open with '1 CITY. 7 DAYS. 7 VIDEOS.' for 2 seconds",
        "shot_labels": (
            "(MON · 5 LEGENDS, TUE · SAGRADA FAMÍLIA, WED · HIDDEN IN PLAIN SIGHT, "
            "THU · HUMAN TOWERS, FRI · MESSI'S NAPKIN, SAT · TOURIST MISTAKES)"
        ),
        "closing_title": "End on 'BARCELONA WEEK STARTS MONDAY'",
    }
    values.update(overrides)
    return CreatorRenderIntentEvidence(**values)


def test_main_creator_grounds_barcelona_labels_as_typed_fields() -> None:
    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        _barcelona_strategy(), PROMPT, render_intent_evidence=_barcelona_evidence()
    )

    assert parsed.opening_title == OPENING_TITLE
    assert parsed.opening_title_duration_s == 2
    assert parsed.shot_labels == LABELS
    assert parsed.closing_title == CLOSING_TITLE


def test_main_creator_drops_invented_or_unverifiable_copy() -> None:
    from app.routes.creator_agent import _apply_explicit_render_intent

    invented = _apply_explicit_render_intent(
        _barcelona_strategy(
            shot_labels=[*LABELS[:5], "SAT · SUNSET SECRETS"],
            closing_title="SEE YOU IN BARCELONA",
            opening_title_duration_s=3,
        ),
        PROMPT,
        render_intent_evidence=_barcelona_evidence(),
    )
    no_evidence = _apply_explicit_render_intent(_barcelona_strategy(), PROMPT)

    assert invented.shot_labels is None
    assert invented.closing_title is None
    assert invented.opening_title_duration_s is None
    assert no_evidence.shot_labels is None
    assert no_evidence.closing_title is None
    assert no_evidence.opening_title_duration_s is None


def test_labels_compile_to_a_story_direction_and_fail_where_they_cannot_render(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities
    from app.services.creator_capabilities import (
        CreatorStrategyError,
        compile_strategy_to_plan,
        resolve_creator_manifest,
    )

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-barcelona",
        edit_format="montage",
        media=[
            {"media_id": media_id, "kind": kind, "duration_s": duration_s}
            for media_id, kind, duration_s, _subject in BARCELONA_MEDIA
        ],
    )

    plan = compile_strategy_to_plan(manifest, _barcelona_strategy(direction="fast_montage"))
    assert plan.strategy.direction == "guided_story"
    assert plan.strategy.shot_labels == LABELS
    with pytest.raises(CreatorStrategyError, match="shot_labels cannot be combined"):
        compile_strategy_to_plan(
            manifest,
            _barcelona_strategy(
                montage_cadence=MontageCadenceConstraint(
                    source_media_ids=["ios-2850", "ios-6EF8"], cut_duration_s=1
                )
            ),
        )


def test_barcelona_request_survives_main_creator_to_rendered_text(monkeypatch) -> None:
    """The production repro, end to end, with the planner forced to fail."""

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
    strategy = _apply_explicit_render_intent(
        _barcelona_strategy(), PROMPT, render_intent_evidence=_barcelona_evidence()
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
        item, edit_plan, summary="Barcelona week trailer.", creator_request=PROMPT
    )
    seeded = parse_edit_proposal(item.edit_proposal)
    assert seeded is not None
    assert isinstance(seeded.brief, ProposalBrief)
    assert seeded.brief.shot_labels == LABELS
    assert seeded.brief.closing_title == CLOSING_TITLE
    assert seeded.brief.opening_title_duration_s == 2
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
    assert snapshot.direction == "guided_story"
    texts = [row["text"] for row in _compile(snapshot)["text_elements"]]
    assert texts == [OPENING_TITLE, *LABELS, CLOSING_TITLE]
    assert not GENERIC_FALLBACK_COPY & set(texts)


def test_label_contract_fits_specialist_beat_limit() -> None:
    # One beat per label plus both title-hold beats must stay inside the guided
    # specialist's output limit; otherwise label drafts fail parsing and quietly
    # fall back to the deterministic path.
    assert MAX_GUIDED_DRAFT_BEATS <= GUIDED_STORY_MAX_BEATS
