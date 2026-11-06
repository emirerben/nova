"""Leverkusen M1 run 2 #1 replay: a montage's length is counted with its Visuals photos.

The run (2026-10-30, thread 1a2189cc, job 93f662a7): 8 videos and 3 Visuals photos.
"Make it 25 seconds" was answered "This draft is about 21.4s; you asked for 25s.", and
"Make it 21 seconds instead" with "about 17.4s; you asked for 21s.": always 3.6 s short,
three photos at 1.2 s. The ten-percent bar never passed, so the chat repeated the same card.

The plan was right. Since KRI-824/825 the length plan sizes only the footage and the placed
Visuals keep their own seconds (25 s - 3.6 s = 21.4 s of video windows); the render played
both (job 93f662a7: 17.4 s windows, a 21.0 s edit, final.mp4 22.6 s with the 1.6 s outro).
The pre-approval count (``approval_readiness``) summed the windows alone.

This drives the real boundaries offline with the run's own data
(``tests/fixtures/lev_m1_run2_length_photo_holds.json``): the planner step that builds the
approved length plan -> the pre-approval readiness check prod ran -> the worker's montage
planner on the same approved plan. No model call.

Ways this can fail (written before the fix):
1. The count still leaves the Visuals photos out (25 s -> "about 21.4s").
2. A photo is counted twice: thread-media photos are already length-plan windows.
3. A draft without Visuals changes: its plan or its count.
4. A photo with its own seconds ("show it for 3 seconds") is counted at the 1.2 s default.
5. Visuals the format does not place (none selected, a voiceover edit) are counted.
6. Photos the length cannot also fit (the budget is not taken off the total) are hidden:
   the count says "met" while the file runs longer.
7. A plan saved before the fix (no recorded Visuals seconds) fails to read.
8. The approval-time re-check disagrees with the turn's check and blocks approval.
9. The count and the planned render differ by more than 0.1 s (frame rounding).
"""

from __future__ import annotations

import copy
import json
import pathlib
from datetime import datetime

import pytest

from app.kria.approval_readiness import assess_readiness, planned_length_s
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import _attached_visual_ids, _enrich_best_parts_snapshot, _held_visual_pool
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creator_capabilities import resolve_creator_manifest
from tests.pipeline.test_unified_montage import clip, visual

FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures"
        / "lev_m1_run2_length_photo_holds.json"
    ).read_text()
)
OBSERVED = FIXTURE["observed_on_prod"]
FIRST_TURN = "78855290-993b-4696-ada2-dc121510ced2"
TOLERANCE_S = 0.1


def _brief(seconds: int | None, *extra: BriefRequirement) -> CreativeBrief:
    """The run's brief as the length turns had it: r1 and r5 live, r5 asking ``seconds``."""
    raw = copy.deepcopy(FIXTURE["brief"])
    requirements = []
    for req in raw["requirements"]:
        req["status"] = "open"
        if req["id"] == "r5":
            if seconds is None:
                continue
            req["facts"] = {"duration_s": seconds}
            req["description"] = f"Make it {seconds} seconds"
        requirements.append(req)
    raw["requirements"] = requirements
    brief = CreativeBrief.model_validate({**raw, "version": 1})
    return brief.model_copy(update={"requirements": [*brief.requirements, *extra]})


def _strategy(seconds: int | None, *, photos: bool = True) -> dict:
    strategy = copy.deepcopy(FIXTURE["strategy"])
    strategy["creator_duration_s"] = strategy["target_duration_s"] = seconds
    if seconds is None:
        del strategy["creator_duration_s"], strategy["target_duration_s"]
    if not photos:
        strategy["selected_media_ids"] = [
            media_id
            for media_id in strategy["selected_media_ids"]
            if not media_id.startswith("asset-")
        ]
    return strategy


def _manifest():
    rows = FIXTURE["clip_assignments"]
    media = [
        {"media_id": r["media_id"], "kind": "video", "duration_s": r["duration_s"]} for r in rows
    ]
    media += [{"media_id": v["media_id"], "kind": "image"} for v in FIXTURE["visuals"]]
    return resolve_creator_manifest(
        item_id="6f4f30ae-2bbe-4485-b7e8-b6721e2db0e2",
        edit_format="montage",
        media=media,
        phone_source_media_ids=[r["media_id"] for r in rows],
        phone_rendering_allowed=True,
        phone_visuals_only=True,
        guided_capability_enabled=True,
        runs_guided_voiceover=False,
    )


def _worker_visuals(placed) -> list[UnifiedClip]:
    """The ready Visuals rows the worker places (``unified_visual``), by manifest id."""
    out = []
    for row in FIXTURE["visuals"]:
        if row["media_id"] not in set(placed or ()):
            continue
        taken = row["capture"]["capture_time"]
        out.append(
            UnifiedClip(
                media_id=row["media_id"].removeprefix("asset-"),
                proxy_path=f"users/u/plan/i/pool/{row['media_id']}.jpg",
                generation="1",
                duration_s=0.0,
                capture_time=datetime.fromisoformat(taken.replace("Z", "+00:00")),
                lane="asset",
                kind="image",
                manifest_id=row["media_id"],
                aspect=4 / 3,
                source_filename=row["source_filename"],
            )
        )
    return out


def _worker_clips() -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=r["media_id"],
            proxy_path=r["gcs_path"],
            generation=str(r["storage_generation"]),
            duration_s=float(r["duration_s"]),
            analysis=dict(r["analysis"]),
        )
        for r in FIXTURE["clip_assignments"]
    ]


def _draft(seconds: int | None, *extra: BriefRequirement, photos: bool = True) -> dict:
    """One length turn of the thread: approved plan, readiness, and the worker's plan."""
    brief = _brief(seconds, *extra)
    strategy = _strategy(seconds, photos=photos)
    manifest = _manifest()
    placed = _held_visual_pool(manifest, strategy)
    snapshot = {
        "clip_assignments": copy.deepcopy(FIXTURE["clip_assignments"]),
        "visuals": copy.deepcopy(FIXTURE["visuals"]),
    }
    media = _enrich_best_parts_snapshot(
        strategy,
        snapshot,
        brief,
        visual_photo_ids=placed,
        attached_visual_ids=_attached_visual_ids(manifest),
    )
    report = assess_readiness(brief, strategy, media=media, manifest=manifest, creator_id="u")
    plan = plan_unified_montage(
        _worker_clips(),
        brief_view(brief),
        strategy=strategy,
        visuals=_worker_visuals(placed),
        creator_duration_proven=seconds is not None,
        approved_length_plan=media["length_plan"],
    )
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    return {
        "brief": brief,
        "strategy": strategy,
        "manifest": manifest,
        "media": media,
        "report": report,
        "length": rows.get("r5"),
        "plan": plan,
    }


@pytest.fixture(autouse=True)
def _prod(prod_profile):
    return prod_profile


@pytest.mark.parametrize("seconds", [25, 21])
def test_the_asked_length_passes_with_the_photos_counted(seconds: int) -> None:
    draft = _draft(seconds)

    assert draft["report"]["policy_problem"] is None
    assert draft["length"]["disposition"] == "executable", draft["length"]["reason"]
    assert draft["length"]["reason"] is None
    # The worker plays the asked length (77.8 s of video is there to fill it) ...
    assert draft["plan"].duration_s == pytest.approx(seconds, abs=TOLERANCE_S)
    # ... and the count before approval is that same length.
    assert planned_length_s(draft["media"]) == pytest.approx(
        draft["plan"].duration_s, abs=TOLERANCE_S
    )
    # The approval-time re-check reads the same draft the same way. (The draft as a whole
    # still waits on r1, the filming-order question: Leverkusen M1 run 2 #2, not this.)
    again = assess_readiness(
        draft["brief"],
        draft["strategy"],
        media=draft["media"],
        manifest=draft["manifest"],
        creator_id="u",
    )
    assert again["digest"] == draft["report"]["digest"]


def test_the_21_second_plan_is_the_one_prod_rendered() -> None:
    """The replay is anchored on the run: same windows, same edit length as job 93f662a7."""
    draft = _draft(21)
    plan = draft["media"]["length_plan"]

    assert plan["target_duration_s"] == OBSERVED["approved_length_plan_target_s"] == 17.4
    # Prod approved "Make it without these" (upload order); here the filming order is live,
    # so the same windows come in another order.
    by_clip = lambda rows: sorted(rows, key=lambda w: w["media_id"])  # noqa: E731
    assert by_clip(plan["windows"]) == by_clip(OBSERVED["approved_windows"])
    assert draft["plan"].duration_s == pytest.approx(OBSERVED["rendered_edit_s"], abs=1e-6)
    assert sorted(draft["plan"].visual_ids) == sorted(OBSERVED["rendered_visual_ids"])
    assert planned_length_s(draft["media"]) == pytest.approx(
        OBSERVED["rendered_edit_s"], abs=TOLERANCE_S
    )


def test_without_photos_the_plan_and_its_count_are_unchanged() -> None:
    draft = _draft(25, photos=False)

    assert "visual_budget_s" not in draft["media"]["length_plan"]
    assert draft["plan"].visual_ids == []
    assert draft["length"]["disposition"] == "executable", draft["length"]["reason"]
    windows_s = sum(
        w["source_end_s"] - w["source_start_s"] for w in draft["media"]["length_plan"]["windows"]
    )
    assert planned_length_s(draft["media"]) == pytest.approx(windows_s, abs=1e-6)
    assert planned_length_s(draft["media"]) == pytest.approx(
        draft["plan"].duration_s, abs=TOLERANCE_S
    )


def test_a_photo_with_its_own_seconds_is_counted_at_those_seconds() -> None:
    """M5's shape: "show the line-up sheet for 3 seconds" on one Visual, a 25 s total."""
    held = FIXTURE["strategy"]["selected_media_ids"][1]
    assert held.startswith("asset-")
    hold = BriefRequirement(
        id="r7",
        kind="timing",
        scope=f"clip:{held}",
        description="show that photo for 3 seconds",
        facts={"duration_s": 3},
        authority="explicit",
        source_turn_id=FIRST_TURN,
    )
    draft = _draft(25, hold)

    held_cut = next(
        c for c in draft["plan"].snapshot.fast_cuts if c.media_id == held.removeprefix("asset-")
    )
    assert held_cut.output_duration_s == pytest.approx(3.0, abs=1 / 30)
    assert draft["plan"].duration_s == pytest.approx(25, abs=TOLERANCE_S)
    assert planned_length_s(draft["media"]) == pytest.approx(
        draft["plan"].duration_s, abs=TOLERANCE_S
    )
    assert draft["length"]["disposition"] == "executable", draft["length"]["reason"]


def test_photos_the_length_cannot_also_fit_are_counted_not_hidden() -> None:
    """Too short for the photos beside the footage: the count says so, never "met"."""
    draft = _draft(6)

    assert planned_length_s(draft["media"]) == pytest.approx(
        draft["plan"].duration_s, abs=TOLERANCE_S
    )
    assert draft["plan"].duration_s > 6 * 1.1
    assert draft["length"]["disposition"] == "needs_choice"
    assert draft["length"]["reason"].startswith("This draft is about ")


def test_a_plan_saved_without_the_photos_seconds_still_reads() -> None:
    media = _draft(25)["media"]
    legacy = {**media, "length_plan": dict(media["length_plan"])}
    legacy["length_plan"].pop("visual_budget_s", None)
    windows_s = sum(
        w["source_end_s"] - w["source_start_s"] for w in legacy["length_plan"]["windows"]
    )

    assert planned_length_s(legacy) == pytest.approx(windows_s, abs=1e-6)


def test_thread_photos_are_counted_once() -> None:
    """Photos in the clip record are length-plan windows already; only Visuals are added."""
    clips = [clip(i, duration=8.0) for i in range(3)]
    stills = [
        UnifiedClip(
            media_id=f"still-{k}",
            proxy_path=f"users/u/still-{k}.jpg",
            generation="1",
            duration_s=0.0,
            kind="image",
        )
        for k in range(2)
    ]
    photos = [visual(1), visual(2)]
    rows = [
        {
            "media_id": c.media_id,
            "kind": c.kind,
            "gcs_path": c.proxy_path,
            "storage_generation": c.generation,
            "duration_s": c.duration_s,
            "analysis": {},
        }
        for c in [*clips, *stills]
    ]
    strategy = {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "balanced",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "creator_duration_s": 20,
        "target_duration_s": 20,
    }
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Make it 20 seconds",
                facts={"duration_s": 20},
                source_turn_id=FIRST_TURN,
            )
        ],
    )
    media = _enrich_best_parts_snapshot(
        strategy,
        {"clip_assignments": rows},
        brief,
        visual_photo_ids=[p.manifest_id for p in photos],
        attached_visual_ids=[p.manifest_id for p in photos],
    )
    plan = plan_unified_montage(
        [*clips, *stills],
        brief_view(brief),
        strategy=strategy,
        visuals=photos,
        creator_duration_proven=True,
        approved_length_plan=media["length_plan"],
    )

    assert media["length_plan"]["visual_budget_s"] == pytest.approx(2.4)
    assert plan.duration_s == pytest.approx(20, abs=TOLERANCE_S)
    assert planned_length_s(media) == pytest.approx(plan.duration_s, abs=TOLERANCE_S)
