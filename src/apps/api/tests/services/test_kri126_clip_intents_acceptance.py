"""KRI-127 acceptance, offline: the KRI-126 request replayed on the real 30 clips.

The resolver's recorded answer (the eval golden) runs through the REAL
resolution service and the REAL worker-edge grounding over the REAL clip
records. Only the two model calls are replayed/faked. Proves, without any
sport list: every sport clip gets the right name (incl. volleyball and the
"field sport" clip), the pub clip is grouped under the creator's words, the
not-playing park clips are ordered first, and the talk-to-camera clip is found.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
from app.agents.clip_request_resolver import ClipRequestResolverAgent
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_resolution import (
    IntentClip,
    grounded_labels,
    resolve_clip_intents_for_turn,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
GOLDEN = (
    FIXTURES
    / "agent_evals"
    / "clip_request_resolver"
    / "golden"
    / "kri126_sports_pub_park_speech.json"
)


def _load():
    footage = json.loads((FIXTURES / "kri126_thirty_clip_guided_story.json").read_text())
    golden = json.loads(GOLDEN.read_text())
    clips = [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis=m["analysis"],
            gcs_path=f"users/u/{m['media_id']}",
        )
        for m in footage["media"]
    ]
    intents = [ClipIntent(**i) for i in golden["input"]["intents"]]
    return footage, golden, clips, intents


def _replay_models(monkeypatch: pytest.MonkeyPatch, golden: dict, vision: dict[str, tuple]):
    """Replay the resolver golden; ``vision`` maps a clip name to (answer, confidence)."""

    def _resolver_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(golden["raw_text"], input)

    source_of: dict[str, str] = {}

    def _download(object_path, local_path):  # noqa: ANN001
        source_of[local_path] = object_path
        Path(local_path).write_bytes(b"fake")

    def _upload(path, timeout=120):  # noqa: ANN001
        # Carry the source clip through so the fake vision model knows what it "sees".
        return SimpleNamespace(uri=source_of[path], mime_type="video/mp4")

    asked: list[tuple[str, str]] = []

    def _vision_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        clip_name = input.file_uri.rsplit("/", 1)[-1]
        asked.append((clip_name, input.question))
        answer, confidence = vision.get(clip_name, ("", 0.0))
        return ClipQuestionOutput(answer=answer, confidence=confidence, evidence="seen")

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolver_run)
    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _upload)
    monkeypatch.setattr(ClipQuestionAgent, "run", _vision_run)
    return asked


async def test_kri126_request_resolves_on_the_real_clips(monkeypatch) -> None:
    footage, golden, clips, intents = _load()
    # What the vision model says when re-asked about the clips the record left vague.
    asked = _replay_models(
        monkeypatch,
        golden,
        vision={
            "clip-08.mp4": ("volleyball", 0.92),
            "clip-20.mp4": ("football", 0.9),
            "clip-27.mp4": ("football", 0.88),
            # "park clips with people NOT playing sports": this one IS a playing clip.
            "clip-09.mp4": ("no", 0.9),
        },
    )

    resolution = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=footage["creator_request"],
        clips=clips,
        run_context=RunContext(),
    )
    assert not resolution.needs_creator, resolution.question
    by_id = {i.intent_id: i for i in resolution.intents}
    subject = {m["media_id"]: m["analysis"]["subject"] for m in footage["media"]}
    labels = {label.media_id: label for label in grounded_labels(resolution.intents)}
    label_by_subject = {subject[mid]: lab.text for mid, lab in labels.items()}

    # Sport names come from what the vision model wrote about each clip. No sport list.
    assert label_by_subject["people playing field sport"] in {"Soccer", "Football"}
    assert label_by_subject["people playing volleyball"] == "Volleyball"
    assert {lab.grounding for lab in labels.values()} == {"record_span", "vision_verified"}
    assert all(lab.confidence >= 0.8 for lab in labels.values())
    # The clip the old subject called "playing with balls" is named by re-asking the viewer.
    assert labels["clip-08.mp4"].text == "Volleyball"
    assert labels["clip-08.mp4"].grounding == "vision_verified"
    assert resolution.vision_answers["clip-08.mp4"], "new answers must be returned for caching"

    # "the pub videos" -> the one cafe/restaurant-interior clip, titled in the creator's words.
    pub = by_id["i_pub"]
    assert [subject[m] for m in pub.media_ids()] == ["woman making funny faces"]
    assert pub.creator_text == "post match pub"

    # "where I talk to the camera" is found from speech.to_camera, which no old subject had.
    assert by_id["i_speech"].media_ids() == pub.media_ids()

    # Park clips where nobody plays go first; no playing clip is among them.
    first = {subject[m] for m in by_id["i_park_order"].media_ids()}
    assert first
    assert not any("playing" in s for s in first)
    assert by_id["i_park_order"].position == "first"
    # Membership is re-asked as a closed yes/no question; a confident "no" excludes the clip.
    park_questions = [q for clip, q in asked if clip == "clip-09.mp4"]
    assert park_questions and all('"yes" or "no"' in q for q in park_questions)
    assert "clip-09.mp4" not in by_id["i_park_order"].media_ids()


async def test_vague_clips_are_asked_about_never_guessed(monkeypatch) -> None:
    """Clips the record cannot name go to vision; still-unknown ones become a question."""
    footage, golden, clips, intents = _load()
    seen = _replay_models(monkeypatch, golden, vision={})  # the vision model cannot tell either

    resolution = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=footage["creator_request"],
        clips=clips,
        run_context=RunContext(),
    )

    assert seen, "the vision model was never asked about the vague clips"
    assert resolution.needs_creator
    assert "clip" in (resolution.question or "").lower()
    # Nothing ungrounded slipped into the render lane.
    for label in grounded_labels(resolution.intents):
        assert label.grounding in {"record_span", "creator_text", "vision_verified"}
