"""The footage and the edit each end-to-end scenario starts from (projected from replay fixtures).

The ``nermin-*`` and ``east-run-brief`` replay fixtures (``tests/fixtures/kria_turns``) record a
creator's request, clip labels and (east run) the typed brief. They carry no pixels, audio or
speech. This module turns them into a real thread on Postgres: a ready render with an editor
target, the clips with finished understanding, a strategy draft the render was made from, the
brief the fixture recorded, and the measured signals attached to every clip.

What is recorded and what is authored here, so nobody reads more into a green run than is in it:

* recorded: the creator's request, the clip labels, the edit format, the brief, the strategy;
* committed measurements, attached by hand: ``audio_quality`` is the REAL measurement of the
  committed recordings (``wind_00.m4a`` for the noisy clip, ``clean_*.m4a`` for the rest) and
  ``near_dup_hash`` is the near-duplicate lane's recorded hashes for two takes of one shot;
* authored: which clip speaks, the Turkish clip subjects (copied from
  ``suggestion_chips_harness.east_run_cases``), the music track and the render the clips were
  cut into.

Three footage profiles per scenario, because one cassette answer can only fill five cards and the
filler kind needs a single Talking take:

* ``editor``   every signal measured, none of them qualifying; a voice, no title, captions off.
* ``signals``  two takes of one shot and a clip with wind (the duplicates and mute evidence).
* ``talking``  one Talking take with a measured speech-cleanup analysis (the filler evidence).
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.database import sync_session
from app.models import CreationThread, CreationThreadEvent, Job, PlanItem, SpeechCleanupAnalysis
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h

KRIA_TURNS = Path(__file__).resolve().parents[1] / "fixtures" / "kria_turns"
AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "clip_signals" / "audio"

EDITOR, SIGNALS, TALKING = "editor", "signals", "talking"

# Four distinct shots plus the two takes of the first: the duplicates lane's recorded hashes.
_SHOTS = {
    "take_a": "m1",
    "take_b": "m2",
    "shot_c": "m3",
    "shot_d": "m4",
}
# A fifth, hand-picked hash set far from every recorded one (the east run has five clips).
_SHOT_E = ["0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0", "00ff00ff00ff00ff"]


@dataclass(frozen=True)
class Clip:
    media_id: str
    subject: str
    speaks: bool = False
    # Which committed recording the audio_quality signal is measured from.
    audio: str = "clean_00.m4a"
    # The near-duplicate record (d01 media id) whose hashes this clip carries.
    shot: str = "shot_c"
    transcript: str = ""


@dataclass(frozen=True)
class Scenario:
    key: str
    fixture: str
    language: str
    edit_format: str
    request: str
    clips: tuple[Clip, ...]
    # Typed brief updates of the fixture, one batch per recorded turn (empty = none stated).
    brief: tuple[tuple[dict[str, Any], ...], ...] = ()
    title_in_edit: str | None = None
    strategy: dict[str, Any] = field(default_factory=dict)
    # Which profiles this scenario runs and which clip index is the noisy / speaking one.
    profiles: tuple[str, ...] = (EDITOR, SIGNALS)
    note: str = ""


def _load(stem: str) -> dict[str, Any]:
    return json.loads((KRIA_TURNS / f"{stem}.json").read_text(encoding="utf-8"))


def nermin(stem: str, *, profiles: tuple[str, ...], speaker: str, transcript: str) -> Scenario:
    data = _load(stem)
    snap = data["snapshot"]
    labels = snap["media_labels"]
    # Clips 1 and 2 are the two takes of one shot in the signals profile, clip 3 the noisy one.
    shots = ("take_a", "take_b", "shot_c")
    clips = tuple(
        Clip(
            media_id=f"clip-{i + 1}",
            subject=label,
            speaks=label == speaker,
            audio="wind_00.m4a" if i == 2 else f"clean_{i:02d}.m4a",
            shot=shots[i],
            transcript=transcript if label == speaker else "",
        )
        for i, label in enumerate(labels)
    )
    return Scenario(
        key=stem,
        fixture=stem,
        language="en",
        edit_format=snap["edit_format"],
        request=data["user_message"],
        clips=clips,
        strategy={
            "edit_format": snap["edit_format"],
            "rationale": snap["editorial_decision"],
        },
        profiles=profiles,
    )


# Turkish clip subjects: hand-authored in the chips harness because the replay fixture names clips
# c1..c5 and carries no analysis.
_EAST_SUBJECTS = [
    ("köprüde koşan koşucu", False),
    ("sahil yolunda gün doğumu", False),
    ("boğazda vapur", False),
    ("bitiş çizgisinde kalabalık", False),
    ("kameraya konuşan koşucu", True),
]


def east_run(*, with_brief: bool) -> Scenario:
    data = _load("east-run-brief")
    ids = data["clip_ids"]
    shots = ("take_a", "take_b", "shot_c", "shot_d", "shot_e")
    audio = ("clean_00.m4a", "clean_01.m4a", "wind_00.m4a", "clean_03.m4a", "clean_04.m4a")
    clips = tuple(
        Clip(
            media_id=ids[i],
            subject=subject,
            speaks=speaks,
            audio=audio[i],
            shot=shots[i],
            transcript="Yirmi kilometre bitti, Eminönü'ne vardık." if speaks else "",
        )
        for i, (subject, speaks) in enumerate(_EAST_SUBJECTS)
    )
    # The recorded replan, minus the two ordering keys the strategy schema does not have.
    strategy = {
        k_: v
        for k_, v in data["replan_strategy"].items()
        if k_ not in {"ordering_basis", "ordering_fallback_clip_ids"}
    }
    return Scenario(
        key="east-run-brief" if with_brief else "east-run-brief/footage-only",
        fixture="east-run-brief",
        language="tr",
        edit_format="montage",
        request=data["turns"][0]["user_message"],
        clips=clips,
        brief=tuple(tuple(t["brief_updates"]) for t in data["turns"]) if with_brief else (),
        title_in_edit=data["replan_strategy"]["opening_title"] if with_brief else None,
        strategy={**strategy, "rationale": "20 kilometre koşunun gün batımı hikayesi."},
        profiles=(EDITOR, SIGNALS, TALKING) if with_brief else (EDITOR, SIGNALS),
        note=(
            "the recorded brief: a title, a capture-time order and per-clip text"
            if with_brief
            else "the same footage before the creator stated anything"
        ),
    )


SCENARIOS: dict[str, Scenario] = {
    s.key: s
    for s in (
        nermin(
            "nermin-matcha-update",
            profiles=(EDITOR, SIGNALS, TALKING),
            speaker="packing orders",
            transcript="Okay, the restock just landed and I am packing the first orders.",
        ),
        nermin(
            "nermin-travel-diary",
            profiles=(EDITOR, SIGNALS),
            speaker="cafe detail",
            transcript="This little place had the best flat white of the whole trip.",
        ),
        east_run(with_brief=True),
        east_run(with_brief=False),
    )
}


# ── Building the render the clips were cut into ───────────────────────────────────────


def _slots(clips: tuple[Clip, ...]) -> list[dict[str, Any]]:
    return [
        {
            "slot_id": f"slot-{i}",
            "clip_index": i,
            "media_id": clip.media_id,
            "source_gcs_path": _path(clip),
            "source_duration_s": 8.0,
            "in_s": 0.0,
            "duration_beats": None,
            "duration_s": 2.0,
            "removed": False,
            "transition_after": "cut",
        }
        for i, clip in enumerate(clips)
    ]


def _path(clip: Clip) -> str:
    return f"users/e2e/{clip.media_id}.mp4"


def variant_for(scenario: Scenario, profile: str, *, finished: bool = False) -> dict[str, Any]:
    variant = h.ready_variant()
    clips = scenario.clips[:1] if profile == TALKING else scenario.clips
    variant["ai_timeline"] = {"beat_grid": [], "slots": _slots(clips)}
    variant["caption_cues"] = [
        {
            "id": "cue-1",
            "text": "Merhaba" if scenario.language == "tr" else "Hello",
            "start_s": 0.0,
            "end_s": 1.0,
        }
    ]
    if profile == TALKING:
        variant["resolved_archetype"] = "subtitled"
    title = scenario.title_in_edit
    variant["text_elements"] = (
        [{**copy.deepcopy(h.HOOK), "text": title, "role": "generative_intro"}] if title else []
    )
    if finished:
        # A polished edit: a title, captions on, crossfades, a sound effect, a quiet song. Nothing
        # the KRI-756 chips know how to add is left, only what the new kinds measure.
        variant["captions_enabled"] = True
        variant["text_elements"] = [{**copy.deepcopy(h.HOOK), "text": "Matcha restock"}]
        for slot in variant["ai_timeline"]["slots"]:
            slot["transition_after"] = "crossfade"
        variant["mix"] = 0.2
        variant["sound_effects"] = [
            {"id": "fx-whoosh", "label": "Whoosh", "at_s": 0.5, "gain": 1.0, "duration_s": 0.6}
        ]
    return variant


# ── Measuring and attaching the signals ──────────────────────────────────────────────


def _near_dup(clip: Clip) -> dict[str, Any]:
    recorded = {c["media_id"]: c for c in k.dup_case()["clips"]}
    if clip.shot == "shot_e":
        return {"hashes": _SHOT_E, "sharpness": 100.0, "mean_luma": 120.0}
    row = recorded[_SHOTS[clip.shot]]
    return {
        "hashes": row["hashes"],
        "sharpness": row["sharpness"],
        "mean_luma": row["mean_luma"],
    }


def _audio_quality(clip: Clip) -> dict[str, Any]:
    from app.services.clip_signals import audio_quality
    from app.services.clip_signals.registry import SignalInputs

    record = audio_quality.compute(
        SignalInputs(media_id=clip.media_id, kind="video", local_path=str(AUDIO / clip.audio))
    )
    assert record is not None, f"{clip.audio} was not measured"
    return dict(record)


def _distinct_shots(clips: tuple[Clip, ...]) -> tuple[Clip, ...]:
    """The editor profile measures every signal but keeps every clip its own shot and quiet."""
    others = ("shot_c", "shot_d", "shot_e")
    out = []
    for i, clip in enumerate(clips):
        out.append(
            Clip(
                clip.media_id,
                clip.subject,
                clip.speaks,
                audio=f"clean_{i:02d}.m4a",
                shot=others[i % len(others)] if i < len(others) else "shot_e",
                transcript=clip.transcript,
            )
        )
    return tuple(out)


def assignments_for(scenario: Scenario, profile: str) -> list[dict[str, Any]]:
    from app.services import clip_signals

    clips = scenario.clips
    if profile == EDITOR:
        clips = _distinct_shots(clips)
    elif profile == TALKING:
        clips = clips[:1]
    rows = []
    for clip in clips:
        speaks = clip.speaks or profile == TALKING
        # In the signals profile nobody speaks: a take that speaks is never the one removed and a
        # noisy clip with a voice is not wind alone.
        if profile == SIGNALS:
            speaks = False
        speech: dict[str, Any] = {"has_speech": speaks, "to_camera": speaks and profile == TALKING}
        if speaks:
            speech["segments"] = [{"start_s": 0.0, "end_s": 1.0}]
            if clip.transcript:
                speech["transcript"] = clip.transcript
        analysis = {
            "duration_s": 8.0,
            "understanding": {"subject": clip.subject, "speech": speech},
        }
        analysis = clip_signals.merge_signals(
            analysis,
            {"audio_quality": _audio_quality(clip), "near_dup_hash": _near_dup(clip)},
        )
        row = {
            "media_id": clip.media_id,
            "kind": "video",
            "source_filename": f"{clip.media_id}.mov",
            "gcs_path": _path(clip),
            "duration_s": 8.0,
            "analysis": analysis,
        }
        if profile == TALKING:
            # What registration stamps on an uploaded take: the object generation the analysis pins.
            row.update({"storage_generation": "1700000000000001", "speech_coverage": 0.9})
        rows.append(row)
    return rows


# ── The thread ────────────────────────────────────────────────────────────────────────


def seed_thread(scenario: Scenario, profile: str, *, finished: bool = False) -> h.Seeded:
    """A ready render with an editor target, its plan, its clips, its signals and its brief."""
    seeded = h.seed_ready_thread(variant_for(scenario, profile, finished=finished))
    rows = assignments_for(scenario, profile)
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        item.clip_assignments = rows
        if profile == TALKING:
            item.edit_format = "subtitled"
            item.clip_gcs_paths = [rows[0]["gcs_path"]]
            item.speech_cleanup_enabled = False
            item.user_edited = False
        job = db.get(Job, seeded.job_id, with_for_update=True)
        job.all_candidates = {"clip_paths": [r["gcs_path"] for r in rows]}
        thread = db.get(CreationThread, seeded.thread_id, with_for_update=True)
        state = dict(thread.state or {})
        state["reply_language"] = scenario.language
        thread.state = state
        # The seeded exchange is generic and the event table is append-only: the replay fixture's
        # own request becomes the latest exchange (what the agent reads as the creator's request).
        db.add_all(
            [
                CreationThreadEvent(
                    thread_id=seeded.thread_id,
                    sequence=4,
                    revision=5,
                    role="user",
                    event_type="user_message",
                    content=scenario.request,
                    payload={"turn_status": "completed"},
                ),
                CreationThreadEvent(
                    thread_id=seeded.thread_id,
                    sequence=5,
                    revision=6,
                    role="assistant",
                    event_type="assistant_review",
                    content="Your video is ready."
                    if scenario.language == "en"
                    else "Videon hazır.",
                    payload={"turn_value": "review"},
                ),
            ]
        )
        thread.revision = 6
        db.commit()
    seed_strategy(seeded, scenario)
    for batch in scenario.brief:
        updates = [_brief_update(u) for u in batch]
        if updates:
            k.write_brief(seeded, *updates)
    return seeded


def _brief_update(raw: dict[str, Any]):  # noqa: ANN202
    from app.kria.brief import BriefUpdate

    return BriefUpdate.model_validate({"operation": "add", **raw})


def seed_strategy(seeded: h.Seeded, scenario: Scenario) -> uuid.UUID:
    """The strategy draft the current render was made from: the head of the thread's drafts."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot
    from app.models import CreatorEditDraft

    strategy = CreativeStrategy.model_validate(scenario.strategy).model_dump(
        mode="json", exclude_none=True
    )
    document = KriaDraftDocument(
        kind="strategy",
        intent=scenario.request[:200],
        edit_format=scenario.edit_format,
        strategy=strategy,
        changes=[],
    )
    snapshot, digest = canonical_snapshot(document)
    with sync_session() as db:
        draft = CreatorEditDraft(
            creator_id=seeded.user_id,
            thread_id=seeded.thread_id,
            item_id=seeded.item_id,
            variant_key="original_text",
            base_job_id=seeded.job_id,
            base_generation_id="generation-1",
            draft_revision=0,
            snapshot_json=snapshot,
            snapshot_hash=digest,
            is_head=True,
        )
        db.add(draft)
        db.commit()
        return draft.id


def seed_cleanup_analysis(seeded: h.Seeded, *, language: str, fillers: int = 4) -> uuid.UUID:
    """A ready speech-cleanup analysis for the item's one take, stamped with the REAL fingerprint
    of the narration the renderer would use now (so the loader treats it as current)."""
    from app.services.plan_item_media import current_detector_policy, resolve_item_narration
    from app.services.speech_cleanup_preflight import SPEECH_CLEANUP_ENGINE_VERSION
    from app.services.speech_cleanup_selection import DETECTOR_VERSION

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
        assert source is not None, "the Talking take must resolve to a narration source"
        row = SpeechCleanupAnalysis(
            plan_item_id=item.id,
            source_kind=source.source_kind,
            source_media_identity=source.media_id,
            source_storage_path=source.storage_path,
            source_generation=source.generation,
            window_start_s=source.window_start_s,
            window_end_s=source.window_end_s,
            source_policy_fingerprint=source.source_policy_fingerprint,
            engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
            detector_version=DETECTOR_VERSION,
            status="ready",
            category_counts={"filler_sounds": fillers, "long_pauses": 1, "retakes": 0},
            estimated_removed_ms=1760,
            candidate_count=fillers + 1,
            analysis_payload={"language": language, "diagnostics": {"language_crosscheck": {}}},
        )
        db.add(row)
        db.commit()
        return row.id


def seed_first_prompt(scenario: Scenario) -> h.Seeded:
    """The footage a creator has just uploaded: understood, no message sent, no render yet."""
    from app.models import CreatorAgentSession
    from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        item.clip_assignments = assignments_for(scenario, EDITOR)
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        state = dict(thread.state or {})
        state.update(
            {
                "reply_language": scenario.language,
                "edit_format": scenario.edit_format,
                "media_count": len(item.clip_assignments),
            }
        )
        thread.state = state
        # The creator answered the format prompt: the newest non-bookkeeping event is no question.
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=2,
                revision=3,
                role="user",
                event_type="action_select_format",
                content=None,
                payload={"format": scenario.edit_format},
            )
        )
        thread.revision = 3
        db.commit()
        item_id = item.id
    return h.Seeded(user_id, thread_id, session_id, item_id, uuid.uuid4())
