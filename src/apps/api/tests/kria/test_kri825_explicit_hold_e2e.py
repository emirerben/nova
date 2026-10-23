"""KRI-825: a photo hold the creator stated plays exactly, outside the AI's 0.5-0.8 s band.

KRI-824 made one named photo's seconds ("keep the beach photo 1 second") creator-owned and
exact. This file covers what was still lost, end to end and offline, through the same
harness (creator words -> stamped brief / server-read profile -> approved length plan ->
unified montage plan -> cloud execution plan + phone recipe -> render-contract verify):

Ways this can fail, each pinned below (written before the fix):
1. "every photo 1 second" (or 1,5 sn, one second, a second and a half, 3.5 s) is thrown
   away because the photos' exact length was only kept between 0.1 and 0.8 s, and the
   photos fall back to the AI's 0.5/0.65/0.8 s (or 1.2 s) pacing.
2. A photo-only ask (no word about the videos) never builds a timing at all.
3. A group length above the 3 s still cap, or a non-integer one (1.25 s = 38 frames), is
   clipped or drifts by more than one frame, or cloud and phone disagree.
4. The videos' own windows move when the photos get longer (no creator total).
5. A correction ("photos 1 second" ... "actually photos 2 seconds") keeps the stale value.
6. A negated ask ("don't make the photos 1 second", "don't keep the beach photo 1 second")
   or seconds inside on-screen copy ('title "1 second to midnight"') ground a hold.
7. A creator total is not one budget: unheld photos sit outside it, so the file runs
   longer than the approved total (and the phone contract fails).
8. A creator total the held photos cannot fit is silently clamped or ignored instead of
   one clear, feasible question; answering it asks again.
9. Save/reopen of the approved draft loses the photos' group length.
10. A hold at the start / middle / end of the timeline is not honored, or a freeze of a
    video is read as a photo hold.
11. Old proposals (0.65 s profile) change bytes.

Artifact: set ``KRI825_ARTIFACT_DIR`` (e.g. ``../../../.dev/kri825``) to write, per case,
``report.json`` (request, profile, per-photo cloud/phone seconds, totals, frame counts of a
real FFmpeg export) plus ``<case>.mp4`` / ``<case>_montage.png``. Repeat with
``cd src/apps/api && KRI825_ARTIFACT_DIR=../../../.dev/kri825 .venv/bin/python -m pytest
tests/kria/test_kri825_explicit_hold_e2e.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import tests.kria.test_kri824_photo_hold_e2e as kri824
from app.kria.asset_holds import HOLD_TOLERANCE_S
from app.kria.brief import BriefUpdate, CreativeBrief
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.guided_story import compile_execution_plan
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MixedMediaTimingProfile,
    recognize_mixed_media_timing,
)
from app.services.choice_questions import answered_brief, collect_conflicts

BEACH, CAFE, SUNSET = kri824.BEACH, kri824.CAFE, kri824.SUNSET
PHOTOS = kri824.PHOTOS
FRAME = 1 / 30
_render, _strategy, _turn, _hold_update = (
    kri824._render,
    kri824._strategy,
    kri824._turn,
    kri824._hold_update,
)


def _profile_strategy(*messages: str) -> dict:
    """The strategy the server builds from the creator's own turns (oldest first)."""
    profile = recognize_mixed_media_timing("\n".join(messages))
    assert profile is not None, messages
    return _strategy(mixed_media_timing=profile.model_dump(mode="json"))


def _video_windows(rendered: dict) -> list[tuple[str, float, float, float]]:
    photos = {p.media_id for p in PHOTOS}
    return [
        (c.media_id, c.source_start_s, c.source_end_s, c.output_duration_s)
        for c in rendered["plan"].snapshot.fast_cuts
        if c.media_id not in photos
    ]


def _reopened_still_seconds(rendered: dict) -> dict[str, float]:
    """Save/reopen: compile the approved draft from its own persisted JSON."""
    saved = EditProposalSnapshot.model_validate(
        json.loads(json.dumps(rendered["plan"].snapshot.model_dump(mode="json")))
    )
    guided = {**rendered["plan"].guided_edit(), "approved_proposal": saved.model_dump(mode="json")}
    execution = compile_execution_plan(guided, track=None)
    return {
        row["media_id"]: round(row["output_end_s"] - row["output_start_s"], 4)
        for row in execution["story_timeline"]
        if row.get("lane") == "asset"
    }


def _artifact(name: str, request: str, rendered: dict, approved_total_s: float | None) -> None:
    root = os.environ.get("KRI825_ARTIFACT_DIR")
    if not root:
        return
    frames = None
    if shutil.which("ffmpeg") is not None:
        previous = os.environ.get("KRI824_ARTIFACT_DIR")
        os.environ["KRI824_ARTIFACT_DIR"] = root
        try:
            frames = kri824._cloud_export(name, rendered["execution"])
        finally:
            if previous is None:
                os.environ.pop("KRI824_ARTIFACT_DIR", None)
            else:
                os.environ["KRI824_ARTIFACT_DIR"] = previous
    video = Path(root) / f"{name}.mp4"
    file_s = None
    if video.exists():
        file_s = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                 "csv=p=0", str(video)],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        )  # fmt: skip
    path = Path(root) / "report.json"
    report = json.loads(path.read_text()) if path.exists() else {}
    plan = rendered["plan"]
    report[name] = {
        "request": request,
        "mixed_media_timing": (
            plan.snapshot.mixed_media_timing.model_dump(mode="json")
            if plan.snapshot.mixed_media_timing
            else None
        ),
        "creator_still_holds": [
            h.model_dump(mode="json") for h in plan.snapshot.creator_still_holds
        ],
        "cuts": [
            {"media_id": c.media_id, "output_s": round(c.output_duration_s, 4)}
            for c in plan.snapshot.fast_cuts
        ],
        "cloud_still_s": rendered["cloud_still_s"],
        "phone_still_s": rendered["phone_still_s"],
        "plan_total_s": plan.duration_s,
        "approved_total_s": approved_total_s,
        "export_file_s": file_s,
        "exported_photo_frames_at_30fps": frames,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    if frames is not None:
        for photo, seconds in rendered["cloud_still_s"].items():
            assert frames[photo] == round(seconds * 30), (name, photo)


# -- 1-4, 9: the photos' own length, as a group --------------------------------------


@pytest.mark.parametrize(
    ("message", "seconds", "mentions_videos"),
    [
        ("Use photos at 1 second each and let the videos hold longer", 1.0, True),
        ("every photo 1 second", 1.0, False),
        ("photos one second each", 1.0, False),
        ("every photo a second and a half", 1.5, False),
        ("fotoğraflar 1 saniye kalsın, videolar daha uzun kalsın", 1.0, True),
        ("fotoğraflar 1,5 sn kalsın", 1.5, False),
        ("make every photo 3.5 seconds", 3.5, False),
        ("photos 1.25 seconds each", 1.25, False),
    ],
)
def test_every_photo_plays_the_seconds_the_creator_gave_the_photos(
    message: str, seconds: float, mentions_videos: bool
) -> None:
    strategy = _profile_strategy(message)
    assert strategy["mixed_media_timing"]["image_hold_s"] == pytest.approx(seconds)
    baseline = _render(CreativeBrief(), _strategy())
    rendered = _render(CreativeBrief(), strategy)
    name = f"group_{seconds}_{'mixed' if mentions_videos else 'photos_only'}"
    _artifact(name, message, rendered, None)
    frames = round(seconds * 30)
    for photo in PHOTOS:
        # Exactly the asked frames (1.25 s -> 38 frames), the same on cloud and phone.
        assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(frames / 30, abs=1e-3)
        assert rendered["phone_still_s"][photo.media_id] == pytest.approx(frames / 30, abs=1e-3)
        assert abs(rendered["cloud_still_s"][photo.media_id] - seconds) <= HOLD_TOLERANCE_S
    if not mentions_videos:
        # 4: an ask about the photos never moves the videos' windows.
        assert _video_windows(rendered) == _video_windows(baseline)
    # 9: Save/reopen of the approved draft plays the same photo lengths.
    assert _reopened_still_seconds(rendered) == pytest.approx(rendered["cloud_still_s"])


def test_the_photos_latest_length_wins() -> None:
    # 5: the conversation is read oldest to newest; the correction wins.
    strategy = _profile_strategy("every photo 1 second", "actually make the photos 2 seconds")
    rendered = _render(CreativeBrief(), strategy)
    for photo in PHOTOS:
        assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(2.0)
        assert rendered["phone_still_s"][photo.media_id] == pytest.approx(2.0)


def test_the_v1_route_keeps_a_photos_length_above_the_ai_band() -> None:
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.routes.creator_agent import _apply_explicit_render_intent  # noqa: PLC0415

    for message, seconds in (
        ("photos 1 second among the clips", 1.0),
        ("photos 1,5 sn between the videos", 1.5),
        ("every photo 2 seconds, videos longer", 2.0),
    ):
        parsed = _apply_explicit_render_intent(CreativeStrategy(), message)
        assert parsed.mixed_media_timing is not None, message
        assert parsed.mixed_media_timing.image_hold_s == pytest.approx(seconds), message


# -- 6: only the creator's own affirmative words ground a hold ---------------------


@pytest.mark.parametrize(
    "message",
    [
        "don't make the photos 1 second",
        "do not make every photo 2 seconds",
        "fotoğraflar 1 saniye olmasın",
    ],
)
def test_a_negated_photos_length_sets_no_length(message: str) -> None:
    profile = recognize_mixed_media_timing(message)
    assert profile is None or profile.image_hold_s is None


@pytest.mark.parametrize(
    "message",
    [
        "don't keep the beach photo 1 second",
        "plaj fotoğrafı 1 saniye kalmasın",
        'put the title "1 second to midnight" on the beach photo',
    ],
)
def test_negated_or_on_screen_seconds_never_stamp_one_photo(message: str) -> None:
    kri824._never_applied(_turn(None, [_hold_update(BEACH, 1, message)], message, "t1"))


def test_seconds_said_outside_the_copy_still_stamp_the_photo() -> None:
    message = 'title "1 second to midnight", keep the beach photo 1 second'
    brief = _turn(None, [_hold_update(BEACH, 1, message)], message, "t1")
    assert kri824._req(brief, BEACH).authority == "explicit"


# -- 7: one total budget -----------------------------------------------------------


def _total(seconds: float) -> BriefUpdate:
    return BriefUpdate(
        kind="timing",
        scope="global",
        description=f"{seconds} seconds",
        facts={"duration_s": seconds},
    )


@pytest.mark.parametrize("held", [False, True], ids=["no_hold", "beach_held"])
def test_a_creator_total_is_the_files_length_with_held_and_unheld_photos(held: bool) -> None:
    updates = [_total(12)]
    message = "12 seconds total"
    if held:
        updates.insert(0, _hold_update(BEACH, 2, "beach photo 2 seconds"))
        message = "keep the beach photo on screen for 2 seconds, 12 seconds total"
    brief = _turn(None, updates, message, "t1")
    baseline = _render(_turn(None, [_total(12)], "12 seconds total", "t0"), _strategy())
    # The phone contract verifies inside ``_render``: the approved total is the file's.
    rendered = _render(brief, _strategy())
    _artifact(f"total_12_{'held' if held else 'plain'}", message, rendered, 12.0)
    assert rendered["plan"].duration_s == pytest.approx(12.0, abs=FRAME + 1e-6)
    if held:
        assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(2.0)
        assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(2.0)
        for photo in (CAFE, SUNSET):
            assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(
                baseline["cloud_still_s"][photo.media_id]
            )


# -- 8: a total the holds cannot fit is one clear, feasible question --------------------


def _hold_conflict(strategy: dict, brief: CreativeBrief):
    snapshot = _enrich_best_parts_snapshot(
        strategy,
        kri824._media_snapshot(),
        brief,
        visual_photo_ids=[p.manifest_id for p in PHOTOS],
    )
    return [c for c in collect_conflicts(strategy, brief, snapshot) if c.kind == "holds_vs_length"]


def _answer(strategy: dict, choice, option: str) -> dict:
    answered = {**strategy, **(choice.effects.get(option) or {}).get("strategy", {})}
    answered["choice_answers"] = [
        {
            "conflict": choice.conflict_id,
            "kind": choice.kind,
            "option": option,
            "input_digest": choice.input_digest,
            "requirement_ids": list(choice.requirement_ids),
            "source": "creator",
        }
    ]
    return answered


def _over_budget() -> tuple[str, CreativeBrief]:
    message = "beach, cafe and sunset photos 3 seconds each, the video 8 seconds"
    updates = [_hold_update(p, 3, f"{p.media_id} photo 3 seconds") for p in PHOTOS]
    return message, _turn(None, [*updates, _total(8)], message, "t1")


def test_a_total_the_holds_cannot_fit_asks_once_and_extending_plays_both() -> None:
    message, brief = _over_budget()
    strategy = _strategy()
    [choice] = _hold_conflict(strategy, brief)
    recommended = choice.recommended()
    assert recommended.key == "extend"
    answered = _answer(strategy, choice, "extend")
    new_total = answered["creator_duration_s"]
    # Three 3 s photos and three readable footage cuts cannot fit 8 s.
    assert new_total > 9.0
    final = answered_brief(brief, answered)
    # Answered: never asked again.
    assert _hold_conflict(answered, final) == []
    rendered = _render(final, answered)
    _artifact("conflict_extend", message, rendered, float(new_total))
    assert rendered["plan"].duration_s == pytest.approx(new_total, abs=FRAME + 1e-6)
    for photo in PHOTOS:
        assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(3.0)
        assert rendered["phone_still_s"][photo.media_id] == pytest.approx(3.0)


def test_keeping_the_total_gives_the_photos_back_to_ai_pacing() -> None:
    message, brief = _over_budget()
    strategy = _strategy()
    [choice] = _hold_conflict(strategy, brief)
    answered = _answer(strategy, choice, "keep_length")
    final = answered_brief(brief, answered)
    assert _hold_conflict(answered, final) == []
    rendered = _render(final, answered)
    baseline = _render(_turn(None, [_total(8)], "8 seconds", "t0"), _strategy())
    assert rendered["plan"].duration_s == pytest.approx(8.0, abs=FRAME + 1e-6)
    assert rendered["cloud_still_s"] == pytest.approx(baseline["cloud_still_s"])


def test_a_total_the_holds_fit_asks_nothing() -> None:
    brief = _turn(
        None,
        [_hold_update(BEACH, 2, "beach photo 2 seconds"), _total(12)],
        "keep the beach photo 2 seconds, 12 seconds total",
        "t1",
    )
    assert _hold_conflict(_strategy(), brief) == []


# -- 10: positions and freeze ----------------------------------------------------------


@pytest.mark.parametrize(
    ("photo", "clips", "position"),
    [(BEACH, 3, "middle"), (SUNSET, 2, "end")],
)
def test_a_held_photo_in_the_middle_or_at_the_end_plays_its_seconds(photo, clips, position):
    message = f"keep the {position} photo 1 second"
    brief = _turn(None, [_hold_update(photo, 1, message)], message, "t1")
    original = kri824.CLIPS
    kri824.CLIPS = original[:clips]
    try:
        rendered = _render(brief, _strategy())
    finally:
        kri824.CLIPS = original
    cuts = rendered["plan"].snapshot.fast_cuts
    index = next(i for i, c in enumerate(cuts) if c.media_id == photo.media_id)
    assert (index == len(cuts) - 1) == (position == "end")
    assert 0 < index
    assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(1.0)
    assert rendered["phone_still_s"][photo.media_id] == pytest.approx(1.0)


def test_a_photo_opening_the_v1_edit_holds_the_photos_length() -> None:
    """The guided (v1) route: a photo at the very start plays the photos' exact second."""
    from app.schemas.edit_proposal import FastMontageCut, canonical_media_digest  # noqa: PLC0415
    from tests.pipeline.test_guided_story import _guided_snapshot  # noqa: PLC0415

    raw = _guided_snapshot(direction="fast_montage")
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    keep = {"food-photo", "coast-video"}
    snapshot.media = [ref for ref in snapshot.media if ref.media_id in keep]
    snapshot.story_beats = [b for b in snapshot.story_beats if set(b.media_ids) <= keep]
    snapshot.duration_s = 3
    snapshot.mixed_media_timing = recognize_mixed_media_timing(
        "photos 1 second each, videos hold longer"
    )
    snapshot.fast_cuts = [
        FastMontageCut(
            cut_id="photo-hook",
            media_id="food-photo",
            source_start_s=0.0,
            source_end_s=1.0,
            output_duration_s=1.0,
            role="hook",
        ),
        FastMontageCut(
            cut_id="video-payoff",
            media_id="coast-video",
            source_start_s=0.0,
            source_end_s=2.0,
            output_duration_s=2.0,
            role="payoff",
        ),
    ]
    raw["approved_proposal"] = EditProposalSnapshot.model_validate(
        snapshot.model_dump(mode="json")
    ).model_dump(mode="json")
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["media_identities"] = [
        {
            "lane": ref.lane,
            "media_id": ref.media_id,
            "gcs_path": ref.gcs_path,
            "generation": ref.generation,
            "kind": ref.kind,
        }
        for ref in snapshot.media
    ]
    plan = compile_execution_plan(raw, track=None)
    first = plan["story_timeline"][0]
    assert first["kind"] == "image"
    assert first["duration_s"] == pytest.approx(1.0)
    assert plan["resolved_duration_s"] == pytest.approx(3.0)


def test_a_video_freeze_beside_a_photo_hold_stays_a_freeze() -> None:
    from app.kria.asset_holds import asset_holds, hold_seconds  # noqa: PLC0415

    surf = kri824.CLIPS[0]
    message = (
        "freeze the last frame of the surf clip for 1 second and keep the beach photo 1 second"
    )
    brief = _turn(
        None,
        [
            BriefUpdate(
                kind="timing",
                scope=f"clip:{surf.media_id}",
                description="freeze the last frame of the surf clip for 1 second",
                facts={"duration_s": 1},
            ),
            _hold_update(BEACH, 1, "beach photo 1 second"),
        ],
        message,
        "t1",
    )
    freeze = next(r for r in brief.live() if r.scope == f"clip:{surf.media_id}")
    assert hold_seconds(freeze) is None
    held = asset_holds(brief, [*(c.media_id for c in kri824.CLIPS), BEACH.manifest_id])
    assert list(held) == [BEACH.manifest_id]
    rendered = _render(brief, _strategy(), verify=False)
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(1.0)


# -- 11: old proposals ------------------------------------------------------------------


def test_an_old_profile_keeps_its_bytes() -> None:
    stored = {
        "image_hold": "very_fast",
        "image_hold_s": 0.65,
        "video_hold": "longer",
        "boundary_style": "cut",
    }
    profile = MixedMediaTimingProfile.model_validate(stored)
    assert json.dumps(profile.model_dump(mode="json")) == json.dumps(stored)
    # A plan with the AI's own photo pacing (no number) records no creator holds.
    rendered = _render(CreativeBrief(), _strategy(mixed_media_timing=kri824.VERY_FAST))
    assert "creator_still_holds" not in rendered["plan"].snapshot.model_dump(mode="json")


@pytest.mark.parametrize(
    ("message", "seconds"),
    [("every photo 2 seconds", 2.0), ("photos 3.5 seconds each, videos hold longer", 3.5)],
)
def test_the_v1_proposal_keeps_every_photo_at_the_creators_length(message, seconds) -> None:
    """The guided (v1) specialist's cuts: the photos' length is never split or capped."""
    from app.agents.edit_proposal import (  # noqa: PLC0415
        EditProposalAgent,
        EditProposalAgentInput,
        EditProposalMedia,
    )

    profile = recognize_mixed_media_timing(message)
    media = [
        EditProposalMedia(media_id="video-a", lane="clip", kind="video", duration_s=8),
        EditProposalMedia(media_id="photo-a", lane="asset", kind="image"),
        EditProposalMedia(media_id="video-b", lane="clip", kind="video", duration_s=8),
        EditProposalMedia(media_id="photo-b", lane="asset", kind="image"),
    ]
    video_s = 2.0 if profile.video_hold == "longer" else 1.0
    order = ["video-a", "photo-a", "video-b", "photo-b"]
    cuts = [
        {
            "cut_id": f"cut-{index}",
            "media_id": media_id,
            "source_start_s": 0.0,
            "source_end_s": seconds if media_id.startswith("photo") else video_s,
            "output_duration_s": seconds if media_id.startswith("photo") else video_s,
            "role": "hook" if index == 0 else "payoff" if index == 3 else "build",
        }
        for index, media_id in enumerate(order)
    ]
    total = 2 * seconds + 2 * video_s
    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps({"title": "Photos", "duration_s": total, "story_beats": [], "fast_cuts": cuts}),
        EditProposalAgentInput(
            direction="fast_montage",
            pace="fast",
            target_duration_s=total,
            mixed_media_timing=profile,
            media=media,
        ),
    )
    photos = [c for c in output.fast_cuts or [] if c.media_id.startswith("photo")]
    assert [c.output_duration_s for c in photos] == pytest.approx([seconds, seconds])


@pytest.mark.parametrize(
    "message",
    [
        "use all my photos, make it 8 seconds",
        "use the photos in a 8 second video",
        "fotoğraflarla 8 saniyelik video yap",
        "photos quick, videos 2 seconds",
    ],
)
def test_the_videos_length_is_never_the_photos_length(message: str) -> None:
    profile = recognize_mixed_media_timing(message)
    assert profile is None or profile.image_hold_s is None


# -- Review round 2: seconds that are not the photos' own (written before the fix) ------
# 12. A per-photo hold, a total, a title/transition/music length, or a video's seconds
#     after a photo mention became an exact hold for EVERY photo.
# 13. A whole-video length right after the photos ("montage of my photos 10 seconds",
#     "use my photos for 8 seconds") pinned every photo to it.
# 14. Seconds in a later turn attached to a photo mention that ended an earlier turn.
# 15. A negation of another verb in the clause ("don't crop ... and hold it 2 seconds",
#     "2 saniye kalsın yazı olmasın") dropped a real hold.
# 16. A photos-only length ("show every photo 1 second") on a voiceover item was refused
#     or rerouted as if it were a photo-and-video timing contract.


@pytest.mark.parametrize(
    "message",
    [
        "use all my photos and hold the sunset photo for 2 seconds",
        "photos should take 10 seconds in total",
        "add the photos with a 3 second title at the start",
        "photos with a 1 second crossfade",
        "use my photos and add music for 5 seconds",
        "fotoğrafları kullan ve 9 saniyelik müzik ekle",
        "photos first then 2 seconds of the beach video",
        "Show the photos then 3 seconds of the beach video",
        "add all my photos and 2 seconds of the drone shot",
        "put the photos first then 3 seconds of the beach clip",
        "use all my photos\n3 seconds for the sunset clip at the end",
        "make a montage of my photos 10 seconds",
        "use my photos for 8 seconds",
        "show all my photos in 9 seconds",
        "a quick photo dump of my photos under 10 seconds",
    ],
)
def test_seconds_that_belong_to_something_else_never_time_every_photo(message: str) -> None:
    profile = recognize_mixed_media_timing(message)
    assert profile is None or profile.image_hold_s is None


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("don't crop the photos and keep each 1 second", 1.0),
        ("fotoğraflar 1 saniye olsun ama müzik olmasın", 1.0),
        ("photos 4 seconds each", 4.0),
        ("photos 1 second\nmake the photos 2 seconds", 2.0),
    ],
)
def test_another_verbs_negation_keeps_the_photos_length(message: str, seconds: float) -> None:
    profile = recognize_mixed_media_timing(message)
    assert profile is not None and profile.image_hold_s == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("don't crop the beach photo and hold it 2 seconds", 2),
        ("do not add text and keep the beach photo 3 seconds", 3),
        ("plaj fotoğrafı 2 saniye kalsın yazı olmasın", 2),
    ],
)
def test_another_verbs_negation_still_stamps_the_photo(message: str, seconds: int) -> None:
    brief = _turn(None, [_hold_update(BEACH, seconds, message)], message, "t1")
    assert kri824._req(brief, BEACH).authority == "explicit"


def test_a_photos_only_length_is_not_a_photo_and_video_contract() -> None:
    from app.schemas.edit_proposal import binds_mixed_media_timing  # noqa: PLC0415

    photos_only = recognize_mixed_media_timing("show every photo 1 second")
    mixed = recognize_mixed_media_timing("photos 1 second, videos longer")
    assert photos_only is not None and not binds_mixed_media_timing(photos_only)
    assert mixed is not None and binds_mixed_media_timing(mixed)
