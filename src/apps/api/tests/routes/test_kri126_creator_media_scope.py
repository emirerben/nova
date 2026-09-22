"""KRI-126 Part 1: explicit-count "all clips" scope + graceful sub-floor capacity.

Incident: a creator attached 30 clips and wrote "Continue with 30 clips ..."
with no ``all``/``every`` keyword. The main creator agent never resolved this
as an explicit all-media scope, so ``media_scope`` stayed unset. When the
guided specialist crashed, the deterministic fallback silently used only the
7 longest clips out of 30.

These tests cover:
  (a) an explicit clip/video/photo count that equals the whole manifest is
      treated the same as the word "all"; a count that is a duration or that
      is less than the manifest is not;
  (b) once such a request resolves to ``media_scope == "all"``, a target
      duration that is genuinely too tight is handled by the existing
      all-media-capacity question rather than raising or silently dropping
      clips. A short clip (here: 4 of 30 clips are shorter than
      ``GUIDED_STORY_MIN_MOMENT_S``) is never, by itself, a reason to ask or
      to exclude that clip -- it plays at its own length
      (``guided_source_floor_s``, product mandate KRI-129);
  (c) KRI-129 fix: an explicit ``media_scope == "selected"`` now survives
      ``normalize_creator_strategy_media`` for a guided program too, so
      ``_seed_guided_specialist_brief`` can forward the creator's real
      selection instead of always widening to the whole manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import normalize_creator_strategy_media
from app.routes import creator_agent as creator_routes
from app.routes.creator_agent import (
    _apply_explicit_render_intent,
    _explicit_media_scope,
    _has_explicit_media_scope,
    _seed_guided_specialist_brief,
)
from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest
from app.services.edit_direction_planner import GUIDED_STORY_MIN_MOMENT_S

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "kri126_thirty_clip_guided_story.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _manifest_of(count: int, *, duration_s: float = 4.0):
    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": duration_s}
            for index in range(count)
        ],
        guided_capability_enabled=True,
    )


def _fixture_manifest():
    fixture = _load_fixture()
    return fixture, resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": item["media_id"], "kind": "video", "duration_s": item["duration_s"]}
            for item in fixture["media"]
        ],
        guided_capability_enabled=True,
    )


# --- (a) explicit stated-count phrasing -------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "Continue with 30 clips and group by sport.",
        "use the 30 videos please",
        "please use all 30 of my clips",
    ],
)
def test_stated_count_equal_to_manifest_resolves_all(request_text: str) -> None:
    manifest = _manifest_of(30)

    assert _has_explicit_media_scope(request_text, manifest)
    assert _explicit_media_scope(request_text, manifest) == "all"


@pytest.mark.parametrize(
    "request_text",
    [
        "render this in 30 seconds",
        "finish it in 30s",
        "wrap the edit up in 30 sec",
        "make it about 30 minutes... just kidding, keep it short",
    ],
)
def test_duration_phrasing_never_resolves_all(request_text: str) -> None:
    manifest = _manifest_of(30)

    assert _explicit_media_scope(request_text, manifest) == "selected"


@pytest.mark.parametrize(
    "request_text",
    [
        # The count names the upload, but the creator asks for LESS than all.
        "I uploaded 30 clips, pick the best 5",
        "30 clips is too many, use fewer",
        "use 30 of my videos, group by sport, but leave out the boring ones",
        "here are 30 clips, only use the football ones",
        "make a top 30 clips countdown",
        # Two different media counts: never guess which one is the scope.
        "use 30 clips and 2 photos",
    ],
)
def test_stated_count_with_a_narrowing_cue_is_not_all(request_text: str) -> None:
    manifest = _manifest_of(30)

    assert _explicit_media_scope(request_text, manifest) == "selected"
    assert not _has_explicit_media_scope(request_text, manifest)


def test_stated_count_below_manifest_is_not_all() -> None:
    manifest = _manifest_of(30)

    # 20 clips requested against a 30-clip manifest must not become "all".
    assert _explicit_media_scope("continue with 20 clips", manifest) == "selected"


def test_stated_count_without_manifest_is_conservative() -> None:
    # No manifest supplied: cannot prove the count covers everything.
    assert _explicit_media_scope("continue with 30 clips") == "selected"
    assert not _has_explicit_media_scope("continue with 30 clips")


def test_negated_stated_count_is_not_all() -> None:
    manifest = _manifest_of(30)

    assert _explicit_media_scope("do not use all 30 clips", manifest) == "selected"


def test_prod_creator_request_resolves_all_media_scope() -> None:
    """The redacted KRI-126 prod request against its real 30-clip manifest."""

    fixture, manifest = _fixture_manifest()
    request = fixture["creator_request"]

    assert _has_explicit_media_scope(request, manifest)
    assert _explicit_media_scope(request, manifest) == "all"

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(direction="guided_story", audio_strategy="original_audio"),
        request,
        manifest=manifest,
    )
    assert strategy.media_scope == "all"


# --- (c) brief seeding from an "all"-scoped plan ----------------------------


def test_seed_guided_specialist_brief_carries_all_media_scope() -> None:
    fixture, manifest = _fixture_manifest()
    plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            selected_media_ids=[ref.media_id for ref in manifest.media],
            target_duration_s=fixture["duration_s"],
            audio_strategy="original_audio",
        ),
    )
    assert plan.strategy.render_program == "guided"
    # compile_strategy_to_plan fills selected_media_ids with the whole
    # manifest for an "all"-scoped guided plan (creator_capabilities.py
    # ~507-515), but the specialist brief still derives its required-media
    # set purely from media_scope="all" downstream (edit_proposal.py
    # _required_media_ids), so nothing is lost by not forwarding these ids.
    assert set(plan.strategy.selected_media_ids) == {ref.media_id for ref in manifest.media}

    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(
        item, plan, summary="Group by sport", creator_request=fixture["creator_request"]
    )

    assert item.edit_proposal["brief"]["media_scope"] == "all"
    assert "selected_media_ids" not in item.edit_proposal["brief"]


def test_guided_selected_scope_now_carries_real_ids_to_the_brief() -> None:
    """(c) finding, now fixed by KRI-129: a real "selected" id list survives.

    This test used to pin the opposite: normalize_creator_strategy_media
    emptied selected_media_ids whenever the effective render_program was
    guided and media_scope == "selected" (creator_policy.py previously only
    populated it for the native branch), so _seed_guided_specialist_brief
    never had a non-empty list to forward alongside "selected" scope and the
    guided specialist received required_media_ids=None -- free to pick any
    media it liked, silently discarding an explicit creator selection. That
    was a bug, not a design decision; both are fixed now.
    """

    fixture, manifest = _fixture_manifest()
    selected_ids = [ref.media_id for ref in manifest.media][:5]
    plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            # render_program="guided" forces the guided branch here since,
            # unlike media_scope=="all", a plain "selected" scope on its own
            # is not one of effective_render_program's guided triggers.
            render_program="guided",
            media_scope="selected",
            selected_media_ids=selected_ids,
            target_duration_s=fixture["duration_s"],
            audio_strategy="licensed_music",
        ),
    )
    assert plan.strategy.render_program == "guided"
    assert plan.strategy.selected_media_ids == selected_ids

    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(
        item, plan, summary="Group by sport", creator_request=fixture["creator_request"]
    )

    assert item.edit_proposal["brief"]["media_scope"] == "selected"
    assert item.edit_proposal["brief"]["selected_media_ids"] == selected_ids


def test_guided_selected_scope_drops_unknown_ids_only_under_repair() -> None:
    """Unknown ids in an explicit guided "selected" scope: strict by default.

    Matches the native branch's existing contract -- an unresolved id from
    the model raises unless repair_model_output=True repairs it away.
    """

    fixture, manifest = _fixture_manifest()
    valid_ids = [ref.media_id for ref in manifest.media][:3]
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        media_scope="selected",
        selected_media_ids=[*valid_ids, "not-a-real-id"],
        target_duration_s=fixture["duration_s"],
        audio_strategy="licensed_music",
    )

    with pytest.raises(ValueError, match="selected_media_ids must reference manifest media"):
        normalize_creator_strategy_media(manifest, strategy)

    repaired = normalize_creator_strategy_media(manifest, strategy, repair_model_output=True)
    assert repaired.selected_media_ids == valid_ids


# --- (b) graceful handling of an infeasible all-media guided request -------


def test_all_media_capacity_question_no_longer_fires_for_short_clips_alone() -> None:
    """A short clip plays at its own length -- it is never, by itself, a
    reason to ask a capacity question or to leave it out of an explicit
    "all" (product mandate, KRI-129). ``assess_all_media_capacity`` now
    counts each source's floor at its own duration when it is shorter than
    the guided minimum moment (``guided_source_floor_s``), so this fixture's
    4 sub-floor clips no longer make the full 30-clip request infeasible at
    its original 45s target."""

    fixture, manifest = _fixture_manifest()
    short_ids = {
        item["media_id"]
        for item in fixture["media"]
        if item["duration_s"] < GUIDED_STORY_MIN_MOMENT_S
    }
    assert len(short_ids) == 4  # pins the fixture's known shape

    strategy = CreativeStrategy(
        direction="guided_story",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=fixture["duration_s"],
        audio_strategy="original_audio",
    )

    assert creator_routes._all_media_capacity_question(manifest, strategy) is None
    # No resolution needed: the explicit "all" scope stands untouched, short
    # clips included (a guided "all" scope takes every manifest media --
    # `normalize_creator_strategy_media` never narrows it).
    normalized = normalize_creator_strategy_media(manifest, strategy)
    assert normalized.media_scope == "all"


def test_all_media_capacity_choice_can_include_short_clips_when_still_infeasible() -> None:
    """When "all" is genuinely infeasible for a reason OTHER than clip
    shortness (here: too many clips for a tighter 30s target), the
    strength-ranked recommendation may legitimately include a short clip --
    ranking by strength is fine, excluding for shortness is not."""

    fixture, manifest = _fixture_manifest()
    short_ids = {
        item["media_id"]
        for item in fixture["media"]
        if item["duration_s"] < GUIDED_STORY_MIN_MOMENT_S
    }
    strategy = CreativeStrategy(
        direction="guided_story",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=30,
        audio_strategy="original_audio",
    )
    question = creator_routes._all_media_capacity_question(manifest, strategy)
    assert question is not None  # still infeasible at this tighter target

    chosen = creator_routes._all_media_capacity_choice(
        question["all_media_capacity"], question["options"][0], manifest
    )

    assert chosen is not None
    # The recommended subset is no longer required to exclude short clips.
    assert set(chosen.selected_media_ids) & short_ids
    assert creator_routes._all_media_capacity_question(manifest, chosen) is None
