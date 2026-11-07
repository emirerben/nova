"""KRI-1295 offline Q3 replay; acceptance/failure matrix in the linked review.

Sanitized recorded brief/media/strategy, with the wrongly superseded r3 restored.
Real planner, phone source binder, route, preflight and readiness, no paid calls.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.approval_readiness import assess_readiness, readiness_reply
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    FINAL_SOURCE_CUTS,
    build_receipts,
    montage_final_source_cuts,
    plan_facts_from_unified_montage,
)
from app.kria.planner import _enrich_best_parts_snapshot, keeps_whole_take
from app.kria.reply_language import reply_language_for
from app.services import phone_montage_preflight as preflight
from app.services.creator_capabilities import resolve_creator_manifest

CREATOR = "q3-replay"
CLIP = "analysis-proxy-q3.mp4"
PATH = f"users/{CREATOR}/{CLIP}"
TEXT = "The prompt I run at the end of every Claude session"
REQUEST = (
    f'Just put this text in the middle for the whole video: "{TEXT}". '
    "Small white text like TikTok's, no captions and nothing else. "
    "Keep the whole clip, don't cut it. Mute my sound, I'll add a song on TikTok."
)
STRATEGY = {
    "pacing": "balanced",
    "direction": "guided_story",
    "rationale": "One silent shot with the requested middle text.",
    "edit_format": "montage",
    "pinned_texts": [{"look": "tiktok", "text": TEXT, "corner": "middle_center"}],
    "caption_style": "none",
    "montage_audio": {
        "source_media_ids": [],
        "preview_source_beds": False,
        "preserve_source_audio": True,
    },
    "audio_strategy": "original_audio",
    "render_program": "guided",
    "story_structure": [],
    "target_duration_s": 4.5,
    "selected_media_ids": [],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}
BRIEF = CreativeBrief(
    version=1,
    requirements=[
        BriefRequirement(
            id="r1",
            kind="text",
            scope="global",
            literal=TEXT,
            description=(
                f'Just put this text in the middle for the whole video: "{TEXT}". '
                "Small white text like TikTok's"
            ),
            facts={"position": "middle"},
        ),
        BriefRequirement(
            id="r2",
            kind="style",
            scope="global",
            description="no captions and nothing else",
            facts={"captions": "off"},
        ),
        BriefRequirement(
            id="r3",
            kind="style",
            scope="global",
            description="Keep the whole clip, don't cut it.",
            facts={"keep_whole_take": True},
        ),
        BriefRequirement(
            id="r4",
            kind="audio",
            scope="global",
            description="Mute my sound",
            facts={"audio_intent": "source_muted"},
        ),
        BriefRequirement(
            id="r5",
            kind="audio",
            scope="global",
            description="I'll add a song on TikTok.",
            facts={"audio_intent": "no_music"},
        ),
    ],
)
MEDIA = {
    "clip_paths": [PATH],
    "clip_assignments": [
        {
            "media_id": CLIP,
            "manifest_identity": CLIP,
            "gcs_path": PATH,
            "storage_generation": "1",
            "kind": "video",
            "duration_s": 11.0,
            "has_audio": True,
            "analysis": {
                "width": 320,
                "height": 568,
                "transcript": "",
                "has_speech": False,
                "best_moments": [
                    {"start_s": 0.8, "end_s": 3.5, "energy": 5.0, "description": "handles laptop"},
                    {"start_s": 5.0, "end_s": 9.5, "energy": 6.0, "description": "opens laptop"},
                ],
            },
            "upload_contract": {
                "purpose": "analysis_proxy",
                "proxy": {
                    "width": 320,
                    "height": 568,
                    "duration_s": 11.0,
                    "frame_rate": 30.0,
                    "timing_version": 1,
                    "orientation_degrees": 0,
                    "original": {
                        "kind": "video",
                        "width": 1080,
                        "height": 1920,
                        "sha256": "1" * 64,
                        "has_audio": True,
                        "byte_count": 100,
                        "duration_s": 11.0,
                        "orientation_degrees": 0,
                    },
                },
            },
        }
    ],
}


@pytest.fixture(autouse=True)
def phone(prod_profile, monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [CREATOR])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_phone_montage_preflight_enabled", True)
    monkeypatch.setattr(settings, "keep_whole_take_plan_enabled", True)
    # Only storage lookup is replaced: this incident has no Visuals.
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_kw: [])


def _brief(kind="style", **changes):
    brief = BRIEF.model_copy(deep=True)
    brief.requirements[2] = brief.requirements[2].model_copy(update={"kind": kind, **changes})
    return brief


def _replay(brief=BRIEF, media=MEDIA):
    media = _enrich_best_parts_snapshot(STRATEGY, copy.deepcopy(media), brief)
    manifest = resolve_creator_manifest(
        item_id="q3-item",
        edit_format="montage",
        media=[
            {"media_id": row["media_id"], "kind": row["kind"], "duration_s": row["duration_s"]}
            for row in media["clip_assignments"]
        ],
    )
    blockers = preflight.readiness_plan_blockers(
        None,
        creator_id=CREATOR,
        item=SimpleNamespace(id="q3-item", edit_format="montage"),
        brief=brief,
        strategy=STRATEGY,
        media_snapshot=media,
        client_protocol=44,
    )
    report = assess_readiness(
        brief,
        STRATEGY,
        media=media,
        manifest=manifest,
        creator_id=CREATOR,
        creator_request=REQUEST,
        plan_blockers=blockers,
    )
    return media, manifest, report


def _plan(media, brief=BRIEF):
    planned = preflight.preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=STRATEGY,
        media_snapshot=media,
        visuals=[],
        client_protocol=44,
    )
    assert isinstance(planned, tuple), planned
    return planned[0]


def _keep(report):
    return next(r for r in report["requirements"] if r["requirement_id"] == "r3")


@pytest.mark.parametrize("kind", ["style", "timing", "audio", "select", "text"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_q3_has_no_false_hold_and_keeps_all_eleven_seconds(kind, language):
    brief = _brief(kind)
    with reply_language_for(language):
        media, manifest, report = _replay(brief)
        assert report["ready"], report
        assert _keep(report)["disposition"] == "unverified"  # not a completed-export claim
        assert "Talking" not in readiness_reply(brief, report)
        assert "Konuşmalı" not in readiness_reply(brief, report)
        plan = _plan(media, brief)
        assert [
            (c.media_id, c.source_start_s, c.source_end_s) for c in plan.snapshot.fast_cuts
        ] == [(CLIP, 0.0, 11.0)]
        record = {**plan.record(), FINAL_SOURCE_CUTS: montage_final_source_cuts(plan)}
        receipt = next(
            r
            for r in build_receipts(
                brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
            )
            if r.requirement_id == "r3"
        )
        assert receipt.status == "met"
        recheck = assess_readiness(
            brief,
            STRATEGY,
            media=media,
            manifest=manifest,
            creator_id=CREATOR,
            creator_request=REQUEST,
        )
        assert recheck["digest"] == report["digest"]


@pytest.mark.parametrize(
    "changes",
    [
        {"facts": {}},
        {"facts": {"keep_whole_take": False}},
        {"status": "superseded"},
        {"scope": "title"},
        {"scope": "clip:1"},
        {"facts": {"keep_whole_take": True, "triggers": ["a spoken line"]}},
        {"facts": {"keep_whole_take": True, "duration_s": 5}},
    ],
)
def test_only_a_live_global_typed_whole_clip_ask_changes_the_plan(changes):
    brief = _brief(**changes)
    assert not keeps_whole_take(brief)
    media = _enrich_best_parts_snapshot(STRATEGY, copy.deepcopy(MEDIA), brief)
    assert media["length_plan"]["windows"][0]["source_start_s"] > 0


def test_shorter_length_still_holds_the_whole_clip_requirement():
    brief = _brief()
    brief.requirements.append(
        BriefRequirement(
            id="r6",
            kind="timing",
            scope="global",
            description="Make it 5 seconds",
            facts={"duration_s": 5},
        )
    )
    _media, _manifest, report = _replay(brief)
    assert not report["ready"]
    assert _keep(report)["disposition"] == "needs_choice"
    assert "so it's cut" in _keep(report)["reason"]


def test_switch_off_does_not_approve_a_known_cut(monkeypatch):
    monkeypatch.setattr(settings, "keep_whole_take_plan_enabled", False)
    _media, _manifest, report = _replay()
    assert not report["ready"]
    assert _keep(report)["disposition"] == "needs_choice"
    assert "4.5 s of your 11 s clip" in _keep(report)["reason"]


def test_wrong_content_repeating_one_subset_is_not_whole():
    media, _manifest, _report = _replay()
    record = _plan(media).record()
    record[FINAL_SOURCE_CUTS] = [
        {"media_id": CLIP, "source_start_s": 2.0, "source_end_s": 7.5},
        {"media_id": CLIP, "source_start_s": 2.0, "source_end_s": 7.5},
    ]  # 11 seconds total, but only half the source plays.
    receipt = next(
        r
        for r in build_receipts(
            BRIEF.live(), plan_facts_from_unified_montage(record), include_unchecked=True
        )
        if r.requirement_id == "r3"
    )
    assert receipt.status == "partial"
    assert "5.5 s of your 11 s clip" in receipt.reason


def test_several_clips_are_not_silently_kept_whole_to_exceed_a_stated_length():
    brief = _brief()
    brief.requirements.append(
        BriefRequirement(
            id="r6",
            kind="timing",
            scope="global",
            description="Make it 5 seconds",
            facts={"duration_s": 5},
        )
    )
    media = copy.deepcopy(MEDIA)
    second = copy.deepcopy(media["clip_assignments"][0])
    second.update(
        media_id="analysis-proxy-second.mp4",
        manifest_identity="analysis-proxy-second.mp4",
        gcs_path=f"users/{CREATOR}/analysis-proxy-second.mp4",
    )
    second["upload_contract"]["proxy"]["original"]["sha256"] = "2" * 64
    media["clip_assignments"].append(second)
    media["clip_paths"].append(second["gcs_path"])
    _media, _manifest, report = _replay(brief, media)
    assert not report["ready"]
    assert _keep(report)["disposition"] == "needs_choice"


@pytest.mark.parametrize("broken", ["missing", "stale", "conflict"])
def test_unvalidated_length_plan_cannot_release_the_hold(broken):
    media, manifest, _report = _replay()
    if broken == "missing":
        media.pop("length_plan")
    elif broken == "stale":
        media["clip_assignments"][0]["storage_generation"] = "new-generation"
    else:
        media["length_plan"]["conflicts"] = [
            {"code": "cannot_fit", "message": "Not enough footage"}
        ]
    report = assess_readiness(
        BRIEF, STRATEGY, media=media, manifest=manifest, creator_id=CREATOR, creator_request=REQUEST
    )
    assert not report["ready"]
    assert _keep(report)["disposition"] == "needs_choice"


def test_length_correction_recovers_without_waiving_keep_whole():
    brief = _brief()
    brief.requirements.append(
        BriefRequirement(
            id="r6",
            kind="timing",
            scope="global",
            description="Make it 5 seconds",
            facts={"duration_s": 5},
        )
    )
    assert not _replay(brief)[2]["ready"]
    brief.requirements[-1] = brief.requirements[-1].model_copy(update={"status": "superseded"})
    brief.requirements.append(
        BriefRequirement(
            id="r7",
            kind="timing",
            scope="global",
            description="Make it 11 seconds",
            facts={"duration_s": 11},
        )
    )
    media, _manifest, report = _replay(brief)
    assert report["ready"], report
    assert brief.requirements[2].status == "open"
    assert [(c.source_start_s, c.source_end_s) for c in _plan(media, brief).snapshot.fast_cuts] == [
        (0.0, 11.0)
    ]


@pytest.mark.parametrize("kind", ["style", "timing"])
@pytest.mark.parametrize("flag", [False, "false", "maybe", None, {}, []])
def test_malformed_or_false_fact_never_enables_whole_clip_from_words(kind, flag):
    assert not keeps_whole_take(_brief(kind, facts={"keep_whole_take": flag}))
