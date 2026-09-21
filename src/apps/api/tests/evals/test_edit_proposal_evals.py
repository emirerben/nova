"""Structural live evals; optional paid semantic judging runs separately in replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_proposal"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no edit-proposal fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_edit_proposal_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n  error: {result.error}"
    )
    assert result.output is not None
    # README: live calls are ledger capped; unmetered judging runs separately
    # in replay. Keep the source-use invariant as an independent output check.
    if fixture.input.get("video_reuse_policy") == "once":
        video_ids = {row["media_id"] for row in fixture.input["media"] if row["kind"] == "video"}
        used = [
            cut["media_id"]
            for cut in result.output.get("fast_cuts") or []
            if cut["media_id"] in video_ids
        ]
        assert len(used) == len(set(used)), "default plans must not revisit a video"
    if (
        fixture.input.get("video_reuse_policy") == "once"
        and fixture.input.get("direction") != "fast_montage"
    ):
        # A story beat may repeat a photo only as a genuine last resort — never
        # while a distinct source is still sitting unused (KRI-115, plan item
        # 5016d555: a chapter repeated an already-shown Messi photo instead of
        # the untouched Camp Nou clip still idle at that point in the draft).
        media_kind = {row["media_id"]: row["kind"] for row in fixture.input["media"]}
        available = set(media_kind)
        seen: set[str] = set()
        for beat in result.output["story_beats"]:
            for media_id in beat["media_ids"]:
                if media_id in seen:
                    assert media_kind[media_id] != "video", "must not revisit a video"
                    assert not (available - seen), (
                        f"{media_id} repeated while an unused source was still available"
                    )
                seen.add(media_id)
    shot_labels = fixture.input.get("shot_labels")
    if shot_labels:
        # Exact creator copy is burned verbatim: labeled beats carry exactly the
        # confirmed labels, in order, live or replay (prod job ac795019).
        labeled = [beat["thought"] for beat in result.output["story_beats"] if beat["thought"]]
        assert labeled == shot_labels
    clip_intents = fixture.input.get("clip_intents")
    if clip_intents:
        # KRI-127 Lane P: resolved clip intents are binding constraints.
        # EditProposalAgent.parse() already enforces group/order/include
        # structurally (app/agents/edit_proposal.py::_validate_clip_intents),
        # so a passing run_eval() above is the primary signal -- these pin the
        # concrete shape too, so a future prompt regression that satisfies
        # parse()'s looser checks by accident (e.g. the "first" media landing
        # in beat 2 instead of beat 1 while still passing structurally) still
        # fails visibly here.
        beats = result.output["story_beats"]
        media_id_positions: dict[str, int] = {}
        for index, beat in enumerate(beats):
            for media_id in beat["media_ids"]:
                media_id_positions[media_id] = index
        for intent in clip_intents:
            if intent.get("status") != "resolved":
                continue
            ids = [a["media_id"] for a in intent["assignments"]]
            if intent["op"] == "include":
                assert all(media_id in media_id_positions for media_id in ids)
            elif intent["op"] == "group":
                positions = sorted(
                    {media_id_positions[mid] for mid in ids if mid in media_id_positions}
                )
                assert positions, f"group media {ids} missing from the plan"
                assert positions[-1] - positions[0] + 1 == len(positions), (
                    "group media must land in one beat or consecutive beats"
                )
            elif intent["op"] == "order" and intent.get("position") == "first":
                first_positions = {
                    media_id_positions[mid] for mid in ids if mid in media_id_positions
                }
                other_positions = {pos for mid, pos in media_id_positions.items() if mid not in ids}
                if first_positions and other_positions:
                    assert max(first_positions) <= min(other_positions)
            elif intent["op"] == "order" and intent.get("position") == "last":
                last_positions = {
                    media_id_positions[mid] for mid in ids if mid in media_id_positions
                }
                other_positions = {pos for mid, pos in media_id_positions.items() if mid not in ids}
                if last_positions and other_positions:
                    assert min(last_positions) >= max(other_positions)
            elif intent["op"] == "label":
                # Labels are rendered by another lane -- the planner must
                # never turn a label value into a beat's own thought text.
                for assignment in intent["assignments"]:
                    value = assignment.get("value")
                    if not value:
                        continue
                    assert all(beat["thought"].strip() != value for beat in beats)
    if eval_mode == "replay":
        # Golden cassettes pin the intended chapter vocabulary. Live outputs are
        # allowed natural synonyms; optional replay judging scores semantic coverage.
        topics = {beat["topic"].lower() for beat in result.output["story_beats"]}
        expected_topics = set(fixture.meta.get("expected_topics") or [])
        assert all(any(expected in topic for topic in topics) for expected in expected_topics)
