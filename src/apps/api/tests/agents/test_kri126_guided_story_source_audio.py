"""KRI-126: guided_story with no shot_labels + creator-requested source audio.

Regression coverage for the prod crash (job 506d2993, replayed live 3/3): a
30-clip guided_story with `montage_audio={preserve_source_audio: true,
source_media_ids: []}` (creator chose "keep original audio", no specific
sources) fired the fast_montage-only "SOURCE-AWARE MONTAGE: author the
complete creative timeline in fast_cuts" prompt note regardless of
`direction`. The model obeyed it: empty `story_beats`, and a `montage_audio`
that echoed every clip it used (17-23 ids) -- which either blew the
<=12-item `MontageAudioPlan.source_media_ids` schema ceiling or, once that
was dodged, still left `story_beats` empty (0 distinct sources).

Fixes covered here:
  1. render_prompt is direction-aware: story-beat directions get a
     "SOURCE AUDIO" note that says story_beats (not fast_cuts) is the
     timeline, and that montage_audio.source_media_ids must equal exactly
     the requested audio sources (empty when none were requested).
  2. parse() coerces an echoed-timeline montage_audio.source_media_ids back
     to the requested (empty) list for story-beat directions *before*
     `EditProposalAgentOutput.model_validate`, so it never trips the
     <=12-item ceiling. A genuine mismatch when sources WERE requested still
     raises.
  3. (KRI-129 update) The story-beat count ceiling is no longer a taste cap
     that scales with required source count -- it is the real persisted
     limit (MAX_GUIDED_DRAFT_BEATS, today 20), so a large upload or a request
     naming many chapters is never rejected for beat count alone.
  4. (KRI-129 update) render_prompt states the beat count as advisory
     guidance ("3-5 is right for a simple request; the creator's requested
     grouping decides") instead of a computed ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)
from app.schemas.edit_proposal import MontageAudioPlan
from tests.agents.test_edit_proposal_agent import _input as _seven_media_input
from tests.agents.test_edit_proposal_agent import _raw as _seven_media_raw

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "kri126_thirty_clip_guided_story.json"
)


def _load_thirty_clip_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _load_thirty_clip_media() -> list[EditProposalMedia]:
    fixture = _load_thirty_clip_fixture()
    media: list[EditProposalMedia] = []
    for row in fixture["media"]:
        analysis = row.get("analysis") or {}
        media.append(
            EditProposalMedia(
                media_id=row["media_id"],
                lane=row["lane"],
                kind=row["kind"],
                duration_s=row.get("duration_s"),
                subject=str(analysis.get("subject") or ""),
                description=str(analysis.get("description") or ""),
                on_screen_text=str(analysis.get("on_screen_text") or ""),
                best_moments=list(analysis.get("best_moments") or []),
            )
        )
    return media


def _guided_input(count: int, **overrides: object) -> EditProposalAgentInput:
    """A guided_story input with `count` distinct single-kind video sources."""

    media = [
        EditProposalMedia(
            media_id=f"clip-{index:02d}",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject=f"subject {index}",
        )
        for index in range(count)
    ]
    defaults: dict[str, object] = {
        "direction": "guided_story",
        "pace": "balanced",
        "target_duration_s": 40.0,
        "media": media,
    }
    defaults.update(overrides)
    return EditProposalAgentInput(**defaults)


def _story_payload(
    media_ids: list[str],
    *,
    duration_s: float,
    beat_count: int,
    montage_audio: dict | None = None,
) -> dict:
    """A minimal valid guided_story payload with `beat_count` chapters."""

    beats = []
    for index in range(beat_count):
        chunk = media_ids[index::beat_count][:4] or media_ids[:1]
        beats.append(
            {
                "topic": f"Chapter {index + 1}",
                "thought": "A visible detail worth noting here today.",
                "media_ids": chunk,
                "layout": "fullscreen",
                "duration_s": round(duration_s / beat_count, 3),
            }
        )
    payload: dict = {
        "title": "KRI-126 sample story",
        "duration_s": duration_s,
        "story_beats": beats,
    }
    if montage_audio is not None:
        payload["montage_audio"] = montage_audio
    return payload


# ---------------------------------------------------------------------------
# render_prompt: montage note is direction-aware.
# ---------------------------------------------------------------------------


def test_fast_montage_source_aware_montage_note_is_unchanged() -> None:
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=10,
        montage_audio=MontageAudioPlan(
            preserve_source_audio=True,
            preview_source_beds=False,
            source_media_ids=["clip-a", "clip-b"],
        ),
        media=[
            EditProposalMedia(media_id="clip-a", lane="clip", kind="video", duration_s=5),
            EditProposalMedia(media_id="clip-b", lane="clip", kind="video", duration_s=5),
        ],
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert (
        "SOURCE-AWARE MONTAGE: author the complete creative timeline in fast_cuts. "
        "You may choose source order, cut lengths, and source windows that "
        "serve the request and fit the footage; do not follow a preset sequence unless "
        "the creator explicitly asks for one."
    ) in prompt


def test_guided_story_with_montage_audio_avoids_fast_cuts_note() -> None:
    agent_input = _guided_input(7, montage_audio=MontageAudioPlan(source_media_ids=[]))

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "author the complete creative timeline in fast_cuts" not in prompt
    # The dynamic note itself must not be headed "SOURCE-AWARE MONTAGE" (that
    # heading is fast_montage-only); the generic editorial-rules bullet
    # explaining what to do *when* it is present is unrelated boilerplate.
    assert "SOURCE AUDIO:" in prompt
    assert "authored in story_beats" in prompt
    assert "leave fast_cuts null for this direction" in prompt
    assert "none requested — return an empty list" in prompt


def test_text_explainer_with_requested_sources_states_them_exactly() -> None:
    agent_input = _guided_input(
        7,
        direction="text_explainer",
        montage_audio=MontageAudioPlan(source_media_ids=["clip-00", "clip-01"]),
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "author the complete creative timeline in fast_cuts" not in prompt
    assert "clip-00, clip-01" in prompt or "m001, m002" in prompt


# ---------------------------------------------------------------------------
# parse(): server-authoritative montage_audio identity for story-beat plans.
# ---------------------------------------------------------------------------


def test_parse_coerces_unrequested_montage_audio_sources_to_empty() -> None:
    media_ids_all = [f"clip-{index:02d}" for index in range(25)]
    agent_input = _guided_input(25, montage_audio=MontageAudioPlan(source_media_ids=[]))

    payload = _story_payload(
        media_ids_all[:7],
        duration_s=agent_input.target_duration_s,
        beat_count=3,
        montage_audio={
            "preserve_source_audio": True,
            "preview_source_beds": False,
            # The model echoes 20 real clips it used -- more than the
            # schema's <=12 ceiling, and more than the (empty) request.
            "source_media_ids": media_ids_all[:20],
        },
    )

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert output.montage_audio is not None
    assert output.montage_audio.source_media_ids == []


def test_parse_still_rejects_a_genuine_montage_audio_mismatch() -> None:
    media_ids_all = [f"clip-{index:02d}" for index in range(25)]
    agent_input = _guided_input(
        25,
        montage_audio=MontageAudioPlan(source_media_ids=["clip-00", "clip-01"]),
    )

    payload = _story_payload(
        media_ids_all[:7],
        duration_s=agent_input.target_duration_s,
        beat_count=3,
        montage_audio={
            "preserve_source_audio": True,
            "preview_source_beds": False,
            "source_media_ids": ["clip-02", "clip-03"],
        },
    )

    with pytest.raises(SchemaError, match="requested montage audio sources were not preserved"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]


def test_fast_montage_echoing_more_than_twelve_audio_sources_is_coerced_not_rejected() -> None:
    """KRI-129: the same echo discarded a 16-clip fast montage on its first
    attempt. When the creator requested no specific audio sources, the echoed
    list is coerced to the requested empty list for every direction."""

    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=10,
        video_reuse_policy="distinct_windows",
        montage_audio=MontageAudioPlan(source_media_ids=[]),
        media=[
            EditProposalMedia(media_id=f"clip-{index:02d}", lane="clip", kind="video", duration_s=2)
            for index in range(20)
        ],
    )
    fast_cuts = [
        {
            "cut_id": f"cut-{index + 1}",
            "media_id": f"clip-{index:02d}",
            "source_start_s": 0.0,
            "source_end_s": 0.5,
            "output_duration_s": 0.5,
            "role": "hook" if index == 0 else "payoff" if index == 19 else "build",
        }
        for index in range(20)
    ]
    payload = {
        "title": "Fast montage",
        "duration_s": 10,
        "story_beats": [],
        "fast_cuts": fast_cuts,
        "montage_audio": {
            "preserve_source_audio": True,
            "preview_source_beds": False,
            "source_media_ids": [f"clip-{index:02d}" for index in range(20)],
        },
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert output.montage_audio is not None
    assert output.montage_audio.source_media_ids == []
    assert len(output.fast_cuts or []) == 20


# ---------------------------------------------------------------------------
# parse(): beat-count ceiling scales with required source coverage.
# ---------------------------------------------------------------------------


def test_parse_allows_higher_beat_ceiling_for_media_scope_all_thirty_clips() -> None:
    # 30 required clips need 30 x 1.4 s = 42 s of screen time, so the target must exceed it.
    media = _load_thirty_clip_media()
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=45.0,
        media_scope="all",
        selected_media_ids=[item.media_id for item in media],
        media=media,
    )
    ids = [item.media_id for item in media]
    payload = _story_payload(ids, duration_s=45.0, beat_count=8)

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert len(output.story_beats) == 8
    used = {media_id for beat in output.story_beats for media_id in beat.media_ids}
    assert used == set(ids)


def test_parse_accepts_excess_beats_for_a_small_media_scope_all_request() -> None:
    # KRI-129: 8 beats over a 6-clip upload used to fail at the old
    # LEGACY_GUIDED_DRAFT_BEATS=5 ceiling; the real ceiling is now the
    # persisted snapshot's limit (20), so 8 beats parses fine.
    media = [
        EditProposalMedia(media_id=f"clip-{index}", lane="clip", kind="video", duration_s=5.0)
        for index in range(6)
    ]
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=40.0,
        media_scope="all",
        selected_media_ids=[item.media_id for item in media],
        video_reuse_policy="allow_repeat",
        media=media,
    )
    ids = [item.media_id for item in media]
    payload = _story_payload(ids * 2, duration_s=40.0, beat_count=8)
    # Force exactly one alias per beat regardless of the round-robin slicing
    # above (the small pool round-trips), so this only ever exercises the
    # beat-count ceiling, not an unrelated reuse violation.
    for index, beat in enumerate(payload["story_beats"]):
        beat["media_ids"] = [ids[index % len(ids)]]

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert len(output.story_beats) == 8
    used = {media_id for beat in output.story_beats for media_id in beat.media_ids}
    assert used == set(ids)


# ---------------------------------------------------------------------------
# render_prompt: beat-count guidance is now advisory, not a computed ceiling.
# ---------------------------------------------------------------------------


def test_render_prompt_states_beat_count_guidance_for_an_ordinary_request() -> None:
    agent_input = _guided_input(7)

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "3-5 beats is right for a simple request" in prompt
    assert "the creator's requested grouping decides" in prompt


def test_large_unscoped_upload_may_use_more_than_five_chapters() -> None:
    """Live replay of the prod input (no media_scope) failed 1 run in 3 with
    "story_beats: List should have at most 5 items": the request names six
    chapters. A large upload raises the ceiling without demanding coverage."""

    media = _load_thirty_clip_media()
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=42.0,
        media=media,
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    # KRI-129: beat count is advisory guidance now, not a computed ceiling --
    # the creator's requested grouping decides how many chapters to use.
    assert "the creator's requested grouping decides" in prompt
    assert "HARD CAP" not in prompt

    # Four clips are under the 1.4s moment floor and are never offered to the model.
    ids = [item.media_id for item in media if (item.duration_s or 0) >= 1.4][:18]
    payload = _story_payload(ids, duration_s=42.0, beat_count=6)
    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert len(output.story_beats) == 6


def test_render_prompt_states_concrete_beat_ceiling_for_media_scope_all() -> None:
    media = _load_thirty_clip_media()
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=40.0,
        media_scope="all",
        selected_media_ids=[item.media_id for item in media],
        media=media,
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "the creator's requested grouping decides" in prompt
    assert "HARD CAP" not in prompt


# ---------------------------------------------------------------------------
# Reuse of the existing 7-media helper/fixture pattern from
# tests/agents/test_edit_proposal_agent.py, for a smaller repro shape.
# ---------------------------------------------------------------------------


def test_seven_media_guided_story_montage_audio_note_and_beats_both_work() -> None:
    agent_input = _seven_media_input()
    agent_input.montage_audio = MontageAudioPlan(source_media_ids=[])

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "author the complete creative timeline in fast_cuts" not in prompt

    payload = json.loads(
        _seven_media_raw(
            ["media-0", "media-1", "media-2", "media-3", "media-4", "media-5", "media-6"]
        )
    )
    payload["montage_audio"] = {
        "preserve_source_audio": True,
        "preview_source_beds": False,
        "source_media_ids": [
            "media-0",
            "media-1",
            "media-2",
            "media-3",
            "media-4",
            "media-5",
            "media-6",
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]
    assert output.montage_audio is not None
    assert output.montage_audio.source_media_ids == []


def test_fast_montage_prompt_never_gets_story_beat_packing_rules() -> None:
    """The story-beat packing notes are direction-gated the same way the
    SOURCE AUDIO note is: a fast montage authors fast_cuts, so it must never
    be told to return N story beats or to cap media per beat."""

    fixture = json.loads(FIXTURE_PATH.read_text())
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=30,
        media_scope="all",
        media=[
            EditProposalMedia(
                media_id=row["media_id"],
                lane="clip",
                kind="video",
                duration_s=max(2.0, float(row["duration_s"])),
            )
            for row in fixture["media"]
        ],
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "HARD CAP" not in prompt
    assert "story beats (never more)" not in prompt
    assert "a hard cap of" not in prompt
    assert "Longer all-media edits may use up to 10 beats" in prompt
