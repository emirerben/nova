"""KRI-824 / KRI-825: "keep the beach photo on screen for 1 second" plays that photo 1 s.

The creator's request, end to end and offline: the brief extractor's recorded output for
the turn -> the server stamps who chose the hold -> the approved length plan and brief
binding -> the unified montage plan -> the guided execution plan the cloud renders and the
phone recipe the phone renders -> the finished-video receipt. Before KRI-824 the photo got
the AI's pacing (1.2 s, or 0.65 s under the "photos very fast" profile) and the "1 second"
was read as the whole video's length.

Ways this can fail, each pinned below (written before the fix):
1. The 1 s hold is replaced by the AI cadence (default 1.2 s, very_fast 0.5-0.8 s), or the
   snapshot validator refuses a still outside the AI band.
2. The hold reaches every photo instead of the named one.
3. The clip-scoped "1 second" becomes the total video target (a ~3 s video).
4. A hold the model wrote but the creator never said (a forged ``facts`` value, an
   ``authority`` key in the model's update) is applied as creator intent.
5. A correction for photo A changes photo B, or loses A's earlier hold.
6. A removed hold comes back from the older turn.
7. Turkish and English read differently ("1,5 saniye" vs "1.5 seconds", "bir saniye" vs
   "one second"), or a photo ask with no stated length ("short") is applied as creator
   intent.
8. Exact retry of the approved binding re-plans to different values.
9. Rounding to frames makes an exact 1.0 s verify as failed (30 frames at 30 fps).
10. The creator's total length takes time from the held photo instead of default cuts.
11. A scope naming a photo by an ambiguous tail attaches the hold to the wrong photo.
12. The receipt says "met" for a hold that did not render, or "not met" for one that did.
13. A number that is not a length ("3 photos", "1000 followers") stamps a model's hold.
14. A correction typed without digits ("make it two seconds", "iki saniye olsun", "a bit
    longer") drops the creator's photo back to the AI default.
15. A creator-stamped length on a VIDEO clip is judged as a photo hold ("This photo kept
    the edit's own pacing").
16. A hold on a Visual that is not placed (removed, ambiguous tail, named twice) takes
    seconds off the creator's total.
17. A later turn about something else ("change the title") carries a model's new number
    for the creator's photo in as creator intent, or a "longer" ask that shrank it.
18. A manual Save drops the stamped hold (old clients never send it) or forges one.
19. One photo's seconds ("the beach photo 0.5 seconds") become every photo's cadence.
20. A Visual named by a unique tail is ambiguous in the worker (ref id and media id share
    it) while the planner budgets it.

Artifact: ``.dev/kri824/report.json`` (request, stamped brief, binding digest, source ids,
per-cut seconds in the cloud execution plan and the phone recipe, and each photo's frame
count in a real FFmpeg export of the cloud plan) plus ``<case>.mp4`` / ``<case>_montage.png``
when ``KRI824_ARTIFACT_DIR`` is set. Repeat with
``cd src/apps/api && KRI824_ARTIFACT_DIR=../../../.dev/kri824 .venv/bin/python -m pytest
tests/kria/test_kri824_photo_hold_e2e.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from app.kria.brief import (
    BriefRequirement,
    BriefUpdate,
    CreativeBrief,
    apply_updates,
    parse_brief_updates,
)
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import check_requirement, plan_facts_from_rendered_montage
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    compile_execution_plan,
    validate_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creator_render_contract import build_render_contract, verify_phone_recipe
from app.services.length_policy import creator_duration_s
from tests.pipeline.test_unified_montage import clip, visual
from tests.pipeline.user_song_helpers import bindings_for

THREAD = uuid.uuid5(uuid.NAMESPACE_URL, "kri-824")
CLIPS = [clip(i, duration=6.0) for i in range(3)]
BEACH, CAFE, SUNSET = visual(1), visual(2), visual(3)
PHOTOS = [BEACH, CAFE, SUNSET]
VERY_FAST = {
    "image_hold": "very_fast",
    "video_hold": "longer",
    "boundary_style": "cut",
}


def _strategy(**extra) -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        **extra,
    }


def _media_snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": c.media_id,
                "kind": "video",
                "gcs_path": c.proxy_path,
                "storage_generation": c.generation,
                "duration_s": c.duration_s,
                "analysis": {},
            }
            for c in CLIPS
        ]
    }


def _hold_update(photo, seconds: float, description: str, **extra) -> BriefUpdate:
    """What the brief extractor records for a one-photo length (recorded model shape)."""
    return BriefUpdate(
        kind="timing",
        scope=f"clip:{photo.manifest_id}",
        description=description,
        facts={"duration_s": seconds},
        **extra,
    )


def _turn(prior: CreativeBrief | None, updates, message: str, turn: str) -> CreativeBrief:
    brief = apply_updates(prior, updates, source_turn_id=turn, creator_text=message)
    return brief.model_copy(update={"version": (prior.version if prior else 0) + 1})


def _render(brief: CreativeBrief, strategy: dict, *, verify: bool = True) -> dict:
    """Approve, plan, compile for cloud and phone; return what each would play."""
    snapshot = _enrich_best_parts_snapshot(
        strategy, _media_snapshot(), brief, visual_photo_ids=[p.manifest_id for p in PHOTOS]
    )
    binding = BriefBinding.create(THREAD, brief, latest_message="Continue", media_snapshot=snapshot)
    resolved = binding.resolve()
    plan = plan_unified_montage(
        CLIPS,
        brief_view(resolved),
        strategy=strategy,
        visuals=PHOTOS,
        creator_duration_proven=creator_duration_s(brief=resolved) is not None,
        approved_length_plan=snapshot.get("length_plan"),
    )
    # The snapshot round-trips through its own validator (what approval persists).
    EditProposalSnapshot.model_validate(plan.snapshot.model_dump(mode="json"))
    guided = plan.guided_edit()
    execution = compile_execution_plan(guided, track=None)
    assert validate_execution_plan(execution, guided) == execution
    cloud = {}
    for row in execution["story_timeline"]:
        if row.get("lane") == "asset":
            cloud[row["media_id"]] = round(row["output_end_s"] - row["output_start_s"], 4)
    footage, stills = bindings_for(plan)
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), footage, stills
    )
    # The phone recipe verifies against the contract approval pinned.
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    if verify and contract is not None:
        assert verify_phone_recipe(contract, recipe, source_audio={c.media_id: True for c in CLIPS})
    return {
        "binding": binding,
        "plan": plan,
        "execution": execution,
        "recipe": recipe,
        "cloud_still_s": cloud,
        "phone_still_s": _phone_still_seconds(recipe),
        "facts": plan_facts_from_rendered_montage(
            {"story_timeline": execution["story_timeline"], "source_audio_preserved": True},
            plan.record(),
        ),
    }


def _phone_still_seconds(recipe) -> dict[str, float]:
    """Each photo's on-screen seconds in the phone recipe's story track."""
    photos = {f"visual-{p.media_id}": p.media_id for p in PHOTOS}
    out: dict[str, float] = {}
    for track in recipe.model_dump(mode="json")["tracks"]:
        for row in track["clips"] if track["kind"] == "video" else ():
            media_id = photos.get(row["source_asset_id"])
            if media_id is not None:
                out[media_id] = round(row["source_duration"] / row["rate"], 4)
    return out


def _artifact(
    name: str,
    request: str,
    brief: CreativeBrief,
    rendered: dict,
    exported_frames: dict[str, int] | None = None,
) -> None:
    root = os.environ.get("KRI824_ARTIFACT_DIR")
    if not root:
        return
    path = Path(root) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = json.loads(path.read_text()) if path.exists() else {}
    plan = rendered["plan"]
    report[name] = {
        "request": request,
        "brief": [req.model_dump(mode="json") for req in brief.requirements],
        "binding_digest": rendered["binding"].digest,
        "source_ids": {
            "clips": [c.media_id for c in CLIPS],
            "photos": {p.manifest_id: p.media_id for p in PHOTOS},
        },
        "asset_holds": plan.record().get("asset_holds"),
        "cuts": [
            {"media_id": cut.media_id, "output_s": round(cut.output_duration_s, 4)}
            for cut in plan.snapshot.fast_cuts
        ],
        "cloud_still_s": rendered["cloud_still_s"],
        "phone_still_s": rendered["phone_still_s"],
        "duration_s": plan.duration_s,
        "exported_photo_frames_at_30fps": exported_frames,
    }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


def _cloud_export(name: str, execution: dict) -> dict[str, int] | None:
    """Render the cloud execution plan with real FFmpeg and count each photo's frames.

    Opt-in (``KRI824_ARTIFACT_DIR``): lavfi stands in for the footage, solid JPEGs for the
    photos. Writes ``<name>.mp4`` and ``<name>_montage.png`` (one frame per cut).
    """
    root = os.environ.get("KRI824_ARTIFACT_DIR")
    if not root or shutil.which("ffmpeg") is None:
        return None
    from app.pipeline.guided_story import _render_moments  # noqa: PLC0415

    out = Path(root)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        local: dict[str, str] = {}
        for index, c in enumerate(CLIPS):
            path = os.path.join(tmp, f"{c.media_id}.mp4")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 f"testsrc2=size=540x960:rate=30:duration={c.duration_s}",
                 "-f", "lavfi", "-i", f"sine=frequency={300 + 100 * index}:duration={c.duration_s}",
                 "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path],
                check=True,
            )  # fmt: skip
            local[c.media_id] = path
        for photo, color in zip(PHOTOS, ("orange", "teal", "purple"), strict=True):
            path = os.path.join(tmp, f"{photo.media_id}.jpg")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:size=800x600",
                 "-frames:v", "1", path],
                check=True,
            )  # fmt: skip
            local[photo.media_id] = path
        moments, _receipts = _render_moments(execution, local, tmp)
        frames: dict[str, int] = {}
        for moment, path in zip(execution["story_timeline"], moments, strict=True):
            counted = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                check=True, capture_output=True, text=True,
            ).stdout.strip()  # fmt: skip
            if moment["kind"] == "image":
                frames[moment["media_id"]] = int(counted)
        listing = os.path.join(tmp, "list.txt")
        Path(listing).write_text("".join(f"file '{p}'\n" for p in moments))
        video = out / f"{name}.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing,
             "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
            check=True,
        )  # fmt: skip
        firsts = [os.path.join(tmp, f"first_{i:02d}.png") for i in range(len(moments))]
        for path, first in zip(moments, firsts, strict=True):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", path, "-frames:v", "1",
                 "-vf", "scale=135:240", first],
                check=True,
            )  # fmt: skip
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", os.path.join(tmp, "first_%02d.png"),
             "-vf", f"tile={len(moments)}x1", "-frames:v", "1", str(out / f"{name}_montage.png")],
            check=True,
        )  # fmt: skip
    return frames


def _req(brief: CreativeBrief, photo) -> BriefRequirement:
    return next(r for r in brief.live() if r.scope == f"clip:{photo.manifest_id}")


# -- the ask, end to end ----------------------------------------------------------------


@pytest.mark.parametrize("profile", [None, VERY_FAST], ids=["default_pacing", "photos_very_fast"])
def test_the_named_photo_plays_one_second_and_the_others_keep_ai_pacing(profile) -> None:
    message = "Montage of these, keep the beach photo on screen for 1 second"
    strategy = _strategy(**({"mixed_media_timing": profile} if profile else {}))
    baseline = _render(CreativeBrief(), strategy)
    brief = _turn(
        None, [_hold_update(BEACH, 1, "beach photo on screen for 1 second")], message, "t1"
    )

    assert _req(brief, BEACH).authority == "explicit"
    # 3: one photo's second is never the video's length.
    assert creator_duration_s(brief=brief) is None

    rendered = _render(brief, strategy)
    name = f"en_1s_{'very_fast' if profile else 'default'}"
    exported = _cloud_export(name, rendered["execution"])
    _artifact(name, message, brief, rendered, exported)
    if exported is not None:
        # The real export: 30 frames at 30 fps on the named photo.
        assert exported[BEACH.media_id] == 30
        baseline_frames = _cloud_export(f"{name}_baseline", baseline["execution"])
        for photo in (CAFE, SUNSET):
            assert exported[photo.media_id] == baseline_frames[photo.media_id]
    # 1 + 9: exactly 30 frames on the cloud plan and on the phone recipe.
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(1.0, abs=1e-6)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(1.0, abs=1e-6)
    # 2: the other photos keep exactly the AI's pacing.
    for photo in (CAFE, SUNSET):
        assert rendered["cloud_still_s"][photo.media_id] == pytest.approx(
            baseline["cloud_still_s"][photo.media_id]
        )
        assert rendered["phone_still_s"][photo.media_id] == pytest.approx(
            baseline["phone_still_s"][photo.media_id]
        )
    assert baseline["cloud_still_s"][BEACH.media_id] != pytest.approx(1.0)
    # 12: the finished video's review says it was done.
    receipt = check_requirement(_req(brief, BEACH), rendered["facts"])
    assert receipt.status == "met", receipt.reason


def test_a_correction_changes_only_its_photo_and_keeps_the_rest() -> None:
    strategy = _strategy()
    first = _turn(
        None,
        [
            _hold_update(BEACH, 1, "beach photo 1 second"),
            _hold_update(SUNSET, 2.5, "sunset photo 2.5 seconds"),
        ],
        "Keep the beach photo 1 second and the sunset photo 2.5 seconds",
        "t1",
    )
    target = _req(first, BEACH)
    second = _turn(
        first,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id=target.id,
                expected_version=first.version,
                facts={"duration_s": 2},
            )
        ],
        "actually make it 2 seconds",
        "t2",
    )
    assert _req(second, BEACH).authority == "explicit"
    rendered = _render(second, strategy)
    _artifact("en_correction", "actually make it 2 seconds", second, rendered)
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(2.0)
    # 5: the sunset hold from the earlier turn is untouched.
    assert rendered["cloud_still_s"][SUNSET.media_id] == pytest.approx(2.5)
    baseline = _render(CreativeBrief(), strategy)
    assert rendered["cloud_still_s"][CAFE.media_id] == pytest.approx(
        baseline["cloud_still_s"][CAFE.media_id]
    )

    # 6: removing the hold gives the photo back to the AI, never to the older turn.
    removed = _turn(
        second,
        [
            BriefUpdate(
                operation="remove",
                target_requirement_id=target.id,
                expected_version=second.version,
            )
        ],
        "let Kria decide the beach photo",
        "t3",
    )
    rendered = _render(removed, strategy)
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(
        baseline["cloud_still_s"][BEACH.media_id]
    )
    assert rendered["cloud_still_s"][SUNSET.media_id] == pytest.approx(2.5)


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("plaj fotoğrafı 1,5 saniye kalsın", 1.5),
        ("keep the beach photo for 1,5 seconds", 1.5),
        ("Plaj fotoğrafını 1.5 sn göster", 1.5),
        ("keep the beach photo up for 1.5 seconds", 1.5),
        ("plaj fotoğrafı bir buçuk saniye kalsın", 1.5),
        ("keep the beach photo for one and a half seconds", 1.5),
        ("plaj fotoğrafı 2 saniye", 2.0),
    ],
)
def test_turkish_and_english_phrasings_hold_the_same_photo(message: str, seconds: float) -> None:
    # 7: the creator's typed number confirms the extractor's reading, the same in EN and TR.
    brief = _turn(None, [_hold_update(BEACH, seconds, message)], message, "t1")
    assert _req(brief, BEACH).authority == "explicit"
    rendered = _render(brief, _strategy())
    _artifact(f"phrasing_{seconds}_{message[:12]}", message, brief, rendered)
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)


@pytest.mark.parametrize(
    "message", ["plaj fotoğrafı bir saniye kalsın", "keep the beach photo for one second"]
)
def test_a_length_in_words_holds_the_same_as_digits(message: str) -> None:
    # 7: "one second" / "bir saniye" is the same stated length as "1 second", in EN and TR.
    brief = _turn(None, [_hold_update(BEACH, 1, message)], message, "t1")
    assert _req(brief, BEACH).authority == "explicit"
    rendered = _render(brief, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(1.0, abs=1e-6)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(1.0, abs=1e-6)


def test_a_photo_ask_with_no_stated_length_is_never_applied_as_creator_intent() -> None:
    # The model's number for "short" is a proposal: the photo keeps the AI pacing.
    message = "keep the beach photo short"
    _never_applied(_turn(None, [_hold_update(BEACH, 1, "beach photo short")], message, "t1"))


def test_exact_retry_of_the_approved_binding_plays_the_same_cuts() -> None:
    message = "keep the beach photo on screen for 1 second"
    brief = _turn(None, [_hold_update(BEACH, 1, message)], message, "t1")
    strategy = _strategy()
    first, retry = _render(brief, strategy), _render(brief, strategy)
    assert first["binding"].digest == retry["binding"].digest
    assert first["plan"].snapshot.model_dump(mode="json") == retry["plan"].snapshot.model_dump(
        mode="json"
    )
    # The binding re-reads byte-identically, so stamped authority survives approval.
    reread = BriefBinding.model_validate(first["binding"].model_dump(mode="json")).resolve()
    assert _req(reread, BEACH).authority == "explicit"


def test_a_creator_total_takes_time_from_default_cuts_not_the_held_photo() -> None:
    message = "keep the beach photo on screen for 2 seconds, 12 seconds total"
    total = BriefUpdate(
        kind="timing", scope="global", description="12 seconds", facts={"duration_s": 12}
    )
    brief = _turn(None, [_hold_update(BEACH, 2, "beach photo 2 seconds"), total], message, "t1")
    # 3: the photo's seconds never make the total ambiguous.
    assert creator_duration_s(brief=brief) == pytest.approx(12.0)

    # The approved (phone) path: the held photo's 2 s come off the footage the length plan
    # sizes, never off the photo. (Unheld Visuals sit outside the length plan, with or
    # without a hold: a pre-existing gap, so the phone contract is not checked here.)
    without = _render(_turn(None, [total], "12 seconds total", "t0"), _strategy(), verify=False)
    rendered = _render(brief, _strategy(), verify=False)
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(2.0)

    def footage_s(result: dict) -> float:
        photos = {p.media_id for p in PHOTOS}
        cuts = result["plan"].snapshot.fast_cuts
        return sum(c.output_duration_s for c in cuts if c.media_id not in photos)

    assert footage_s(rendered) == pytest.approx(footage_s(without) - 2.0, abs=0.05)

    # Without an approved window plan the planner reconciles the total itself: it shrinks
    # the AI-sized cuts and leaves the held photo whole.
    plan = plan_unified_montage(
        CLIPS,
        brief_view(brief),
        strategy=_strategy(),
        visuals=PHOTOS,
        creator_duration_proven=True,
    )
    held = next(c for c in plan.snapshot.fast_cuts if c.media_id == BEACH.media_id)
    assert held.output_duration_s == pytest.approx(2.0)
    assert plan.duration_s == pytest.approx(12.0, abs=0.05)


# -- negative controls: nothing but the creator's words makes a hold -------------------


def _never_applied(brief: CreativeBrief) -> None:
    """The hold is not creator intent: no photo is pinned and the review never says met."""
    from app.kria.asset_holds import asset_holds  # noqa: PLC0415
    from app.kria.brief_checks import plan_facts_from_unified_montage  # noqa: PLC0415

    assert _req(brief, BEACH).authority is None
    assert asset_holds(brief, [p.manifest_id for p in PHOTOS]) == {}
    plan = plan_unified_montage(CLIPS, brief_view(brief), strategy=_strategy(), visuals=PHOTOS)
    assert plan.record().get("asset_holds") is None
    assert not plan.snapshot.creator_still_holds
    facts = plan_facts_from_unified_montage(plan.record())
    assert check_requirement(_req(brief, BEACH), facts).status != "met"


def test_a_model_number_the_creator_never_said_is_not_applied_and_is_reported() -> None:
    message = "Montage of these, make the photos quick"
    _never_applied(_turn(None, [_hold_update(BEACH, 1, "beach photo 1 second")], message, "t1"))


def test_a_model_supplied_authority_is_dropped_never_promoted() -> None:
    [update] = parse_brief_updates(
        [
            {
                "operation": "add",
                "kind": "timing",
                "scope": f"clip:{BEACH.manifest_id}",
                "description": "beach photo 1 second",
                "facts": {"duration_s": 1, "authority": "explicit"},
                "authority": "explicit",
            }
        ]
    )
    _never_applied(_turn(None, [update], "make it nice", "t1"))


def test_an_ambiguous_scope_attaches_to_no_photo() -> None:
    twin_a = dataclasses.replace(BEACH, manifest_id="asset-aaaa-sharedtail")
    twin_b = dataclasses.replace(CAFE, manifest_id="asset-bbbb-sharedtail")
    from app.kria.asset_holds import asset_holds

    brief = _turn(
        None,
        [
            BriefUpdate(
                kind="timing",
                scope="clip:sharedtail",
                description="that photo 1 second",
                facts={"duration_s": 1},
            )
        ],
        "keep that photo 1 second",
        "t1",
    )
    assert asset_holds(brief, [twin_a.manifest_id, twin_b.manifest_id]) == {}


# -- review fixes: numbers that are not lengths, worded corrections, videos, budgets ----


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("Use these 3 photos, keep the beach one short", 3),
        ("we hit 1000 followers! beach photo quick", 1),
        ("make it 15 seconds with my 3 favourite photos", 3),
    ],
)
def test_a_number_that_is_not_a_length_never_stamps_a_model_hold(message, seconds) -> None:
    # 13: only digits followed by a time unit confirm the extractor's seconds.
    _never_applied(_turn(None, [_hold_update(BEACH, seconds, "beach photo short")], message, "t1"))


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("make it two seconds", 2),
        ("iki saniye olsun", 2),
        ("a bit longer", 1.5),
        ("biraz daha kısa", 0.5),
    ],
)
def test_a_worded_correction_keeps_the_photo_creator_owned(message, seconds) -> None:
    # 14: the creator re-decided their own photo's time; it never returns to AI pacing.
    first = _turn(
        None,
        [_hold_update(BEACH, 1, "beach photo 1 second")],
        "keep the beach photo on screen for 1 second",
        "t1",
    )
    target = _req(first, BEACH)
    second = _turn(
        first,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id=target.id,
                expected_version=first.version,
                facts={"duration_s": seconds},
            )
        ],
        message,
        "t2",
    )
    assert _req(second, BEACH).authority == "explicit"
    rendered = _render(second, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    assert check_requirement(_req(second, BEACH), rendered["facts"]).status == "met"


@pytest.mark.parametrize("fact", ["duration_s", "clip_duration_s"])
def test_a_stamped_video_clip_length_is_not_judged_as_a_photo(fact) -> None:
    # 15: holds are photos only; a video clip's length keeps the clip paths.
    video = CLIPS[0]
    message = "make the surf clip 2 seconds"
    brief = _turn(
        None,
        [
            BriefUpdate(
                kind="timing",
                scope=f"clip:{video.media_id}",
                description="surf clip 2 seconds",
                facts={fact: 2},
            )
        ],
        message,
        "t1",
    )
    req = next(r for r in brief.live() if r.scope == f"clip:{video.media_id}")
    # Never the video's total, whatever the medium.
    assert creator_duration_s(brief=brief) is None
    rendered = _render(brief, _strategy(), verify=False)
    assert not rendered["plan"].record().get("asset_holds")
    receipt = check_requirement(req, rendered["facts"])
    assert "photo" not in (receipt.reason or "").casefold()
    assert "fotoğraf" not in (receipt.reason or "").casefold()


def test_only_placed_visual_holds_come_off_the_creator_total() -> None:
    # 16: a removed photo, an ambiguous tail and a doubly-named photo cost nothing extra.
    from app.kria.asset_holds import held_visual_seconds  # noqa: PLC0415

    clip_ids = [c.media_id for c in CLIPS]
    pool = [p.manifest_id for p in PHOTOS]
    removed = "asset-00000000-0000-4000-8000-000000000099"
    brief = _turn(
        None,
        [
            _hold_update(BEACH, 2, "beach photo 2 seconds"),
            BriefUpdate(
                kind="timing",
                scope=f"clip:{BEACH.manifest_id[-12:]}",
                description="beach photo 2 seconds",
                facts={"duration_s": 2},
            ),
            BriefUpdate(
                kind="timing",
                scope=f"clip:{removed}",
                description="old photo 2 seconds",
                facts={"duration_s": 2},
            ),
        ],
        "keep the beach photo 2 seconds and the old one 2 seconds",
        "t1",
    )
    assert held_visual_seconds(brief, clip_ids, pool) == pytest.approx(2.0)
    # A format that places no Visuals subtracts nothing.
    assert held_visual_seconds(brief, clip_ids, None) == 0.0
    twins = ["asset-aaaa-sharedtail", "asset-bbbb-sharedtail"]
    ambiguous = _turn(
        None,
        [
            BriefUpdate(
                kind="timing",
                scope="clip:sharedtail",
                description="that photo 1 second",
                facts={"duration_s": 1},
            )
        ],
        "keep that photo 1 second",
        "t1",
    )
    assert held_visual_seconds(ambiguous, clip_ids, twins) == 0.0


# -- review round 2: model numbers on unrelated turns, Save, one-photo asks, Visual tails --


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("change the title to Summer", 0.5),
        ("make the whole video 20 seconds", 2.5),
        ("change the music", 2),
        # The creator asked for longer; a model that shrank the hold is not them.
        ("a bit longer", 0.5),
        ("biraz daha kısa", 2),
    ],
)
def test_a_model_number_on_a_turn_about_something_else_never_becomes_the_creators(
    message, seconds
) -> None:
    # 17: the extractor re-emits the creator's photo with a number they never said.
    first = _turn(
        None,
        [_hold_update(BEACH, 1, "beach photo 1 second")],
        "keep the beach photo on screen for 1 second",
        "t1",
    )
    target = _req(first, BEACH)
    second = _turn(
        first,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id=target.id,
                expected_version=first.version,
                facts={"duration_s": seconds},
            )
        ],
        message,
        "t2",
    )
    _never_applied(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["old_client_omits", "forged"])
async def test_a_manual_save_keeps_the_server_stamped_hold(monkeypatch, client) -> None:
    # 18: Save rebuilds the snapshot from the client body; the hold is server-owned.
    from types import SimpleNamespace  # noqa: PLC0415

    import app.routes.plan_items as plan_items  # noqa: PLC0415
    from app.schemas.edit_proposal import (  # noqa: PLC0415
        EditProposal,
        ProposalBrief,
        canonical_media_digest,
        parse_edit_proposal,
    )
    from tests.routes.test_edit_proposal_routes import _patch_route_dependencies  # noqa: PLC0415

    brief = _turn(
        None,
        [_hold_update(BEACH, 1, "beach photo 1 second")],
        "keep the beach photo on screen for 1 second",
        "t1",
    )
    rendered = _render(brief, _strategy())
    draft = rendered["plan"].snapshot
    assert [h.media_id for h in draft.creator_still_holds] == [BEACH.media_id]
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[],
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id="attempt-1",
            media_digest=canonical_media_digest(draft.media, draft.narration),
            status="draft",
            brief=ProposalBrief(),
            draft=draft,
        ).model_dump(mode="json"),
    )
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    monkeypatch.setattr(plan_items, "_require_guided_edit_applicable", lambda _item: None)
    # A forgery the snapshot validator itself accepts: the cafe photo claimed as
    # creator-held at its current cut length (with no brief requirement behind it).
    cafe_s = next(c.output_duration_s for c in draft.fast_cuts if c.media_id == CAFE.media_id)
    holds = (
        []
        if client == "old_client_omits"
        else [{"media_id": CAFE.media_id, "hold_s": cafe_s, "requirement_id": "forged"}]
    )
    body_snapshot = EditProposalSnapshot.model_validate(
        {
            **draft.model_dump(mode="json"),
            "title": "Summer",
            "creator_still_holds": holds,
        }
    )
    await plan_items.update_item_edit_proposal(
        str(item.id),
        plan_items.UpdateEditProposalBody(expected_proposal_version=2, snapshot=body_snapshot),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )
    saved = parse_edit_proposal(item.edit_proposal)
    assert saved is not None and saved.draft is not None
    assert saved.draft.title == "Summer"
    assert saved.draft.creator_still_holds == draft.creator_still_holds
    # Reopen + render the saved draft: the beach photo still plays its 30 frames.
    guided = {
        **rendered["plan"].guided_edit(),
        "approved_proposal": saved.draft.model_dump(mode="json"),
    }
    execution = compile_execution_plan(guided, track=None)
    still = {
        row["media_id"]: round(row["output_end_s"] - row["output_start_s"], 4)
        for row in execution["story_timeline"]
        if row.get("lane") == "asset"
    }
    assert still[BEACH.media_id] == pytest.approx(1.0, abs=1e-6)
    assert still[CAFE.media_id] == pytest.approx(rendered["cloud_still_s"][CAFE.media_id])


@pytest.mark.parametrize(
    ("message", "global_hold_s"),
    [
        ("keep the beach photo 0.5 seconds and the clips longer", None),
        ("this photo 0.3 seconds, let the videos breathe", None),
        # A group ask still sets every photo's cadence, as before.
        ("Use photos at 0.3 seconds and let the videos hold longer", 0.3),
        ("make every photo 0.2 seconds among the clips", 0.2),
    ],
)
def test_one_photos_seconds_never_become_every_photos_cadence(message, global_hold_s) -> None:
    # 19: the legacy global parse pins all stills only for an ask about the photos.
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.routes.creator_agent import _apply_explicit_render_intent  # noqa: PLC0415

    parsed = _apply_explicit_render_intent(CreativeStrategy(), message)
    timing = parsed.mixed_media_timing
    hold = timing.image_hold_s if timing is not None else None
    assert hold == (pytest.approx(global_hold_s) if global_hold_s else None)


def test_a_visual_named_by_a_unique_tail_holds_in_the_worker_plan() -> None:
    # 20: a Visual's ref id ("asset-<uuid>") and its media id share every tail.
    tail = BEACH.manifest_id[-12:]
    assert BEACH.manifest_id.endswith(BEACH.media_id)
    brief = _turn(
        None,
        [
            BriefUpdate(
                kind="timing",
                scope=f"clip:{tail}",
                description="beach photo 2 seconds",
                facts={"duration_s": 2},
            )
        ],
        "keep the beach photo 2 seconds",
        "t1",
    )
    rendered = _render(brief, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(2.0, abs=1e-6)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(2.0, abs=1e-6)
