from types import SimpleNamespace

import pytest

from app.pipeline.transcribe import Transcript, Word
from app.tasks import generative_build as gb


def test_storyboard_text_elements_keep_voiceover_timeline_and_spoken_score() -> None:
    transcript = Transcript(
        words=[
            Word("Intro", 0.0, 0.4, 1.0),
            Word("score", 1.0, 1.2, 1.0),
            Word("six", 1.2, 1.5, 1.0),
            Word("four", 1.5, 1.8, 1.0),
        ],
        language="en",
    )
    timings = [
        SimpleNamespace(step_id="step_0", start_s=0.0, end_s=2.0),
        SimpleNamespace(step_id="step_1", start_s=2.0, end_s=4.0),
    ]
    assignments = [SimpleNamespace(step_id="step_0", clip_path="/tmp/source-a.mp4")]
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=timings,
        clip_assignments=assignments,
        clip_id_by_path={"/tmp/source-a.mp4": "clip_0"},
        creator_request="Add intro text, player names, and scores",
        explicit_opening_title="Match Day",
        storyboard={
            "overlays": [
                {
                    "kind": "score",
                    "anchor_word_id": "w000002",
                    "end_word_id": "w000003",
                }
            ]
        },
    )

    by_source = {item["source_params"]["narrated_storyboard"]: item for item in elements}
    assert by_source["intro"]["start_s"] == 0.0
    assert by_source["intro"]["text"] == "Match Day"
    assert by_source["narrated_storyboard:placeholder:1"]["start_s"] == 0.0
    assert by_source["narrated_storyboard:score:2"]["text"] == "six four"
    assert by_source["narrated_storyboard:score:2"]["start_s"] == 1.2
    assert (
        by_source["intro"]["id"] == gb.hashlib.sha256(b"narrated_storyboard:intro").hexdigest()[:32]
    )


def test_narrated_text_reburn_uses_caption_compositor() -> None:
    assert gb._should_compose_subtitled_final(
        {
            "resolved_archetype": "narrated",
            "text_elements": [{"text": "PLAYER 1"}],
            "caption_cues": [{"text": "spoken", "start_s": 0, "end_s": 1}],
        }
    )


def test_storyboard_provider_failure_returns_upload_order_fallback(monkeypatch) -> None:
    from app.agents.narrated_storyboard import NarratedStoryboardAgent

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        NarratedStoryboardAgent,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )
    receipt = gb._narrated_storyboard_plan(
        transcript=Transcript(words=[Word("Serve", 0.0, 0.4, 1.0)], language="en"),
        clip_metas=[
            SimpleNamespace(
                clip_id="clip_0",
                hook_text="tennis serve",
                detected_subject="player",
                transcript="",
                content_type="broll",
                best_moments=[],
            )
        ],
        clip_ids=["clip_0"],
        clip_durations_s={"clip_0": 4.0},
        step_timings=[SimpleNamespace(step_id="seg_0", start_s=0.0, end_s=1.0)],
        filming_guide=[],
        creator_request="Match the storyline",
        job_id="job",
    )

    assert receipt["status"] == "fallback"
    assert receipt["matches"] == []
    assert receipt["failure_code"] == "storyboard_provider_error"
    assert "error" not in receipt


def test_storyboard_receives_filming_guide_context(monkeypatch) -> None:
    from app.agents.narrated_storyboard import NarratedStoryboardAgent, NarratedStoryboardOutput

    captured = {}
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    def fake_run(_self, input_value, **_kwargs):
        captured["input"] = input_value
        return NarratedStoryboardOutput(matches=[], overlays=[])

    monkeypatch.setattr(NarratedStoryboardAgent, "run", fake_run)
    receipt = gb._narrated_storyboard_plan(
        transcript=Transcript(words=[Word("Serve", 0.0, 0.4, 1.0)], language="en"),
        clip_metas=[
            SimpleNamespace(
                clip_id="clip_0",
                hook_text="tennis serve",
                detected_subject="player",
                transcript="",
                content_type="broll",
                best_moments=[],
            )
        ],
        clip_ids=["clip_0"],
        clip_durations_s={"clip_0": 4.0},
        step_timings=[SimpleNamespace(step_id="shot_1", start_s=0.0, end_s=1.0)],
        filming_guide=[{"shot_id": "shot_1", "what": "Show the opening serve"}],
        creator_request="Match the storyline",
        job_id="job",
    )

    assert receipt["status"] == "ready"
    assert captured["input"].segments[0].guidance == "Show the opening serve"


def test_storyboard_intro_overlay_requires_creator_intro_intent() -> None:
    transcript = Transcript(words=[Word("spoken", 0.0, 0.4, 1.0)], language="en")
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=1.0)],
        clip_assignments=[],
        clip_id_by_path={},
        creator_request="Match the clips to the voiceover.",
        explicit_opening_title=None,
        storyboard={"overlays": [{"kind": "intro", "text": "Hallucinated title"}]},
    )

    assert not any(item["role"] == "generative_intro" for item in elements)


def test_negated_storyboard_treatments_do_not_create_text() -> None:
    transcript = Transcript(
        words=[
            Word("score", 0.0, 0.2, 1.0),
            Word("six", 0.2, 0.4, 1.0),
            Word("four", 0.4, 0.6, 1.0),
        ],
        language="en",
    )
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=1.0)],
        clip_assignments=[SimpleNamespace(step_id="step_0", clip_path="/a.mp4")],
        clip_id_by_path={"/a.mp4": "clip_a"},
        creator_request="Do not add intro text, player names, or scores.",
        explicit_opening_title=None,
        storyboard={
            "overlays": [
                {"kind": "intro", "text": "Unwanted"},
                {
                    "kind": "score",
                    "anchor_word_id": "w000001",
                    "end_word_id": "w000002",
                },
            ]
        },
    )

    assert elements == []


def test_against_context_does_not_turn_unrelated_numbers_into_score() -> None:
    transcript = Transcript(
        words=[
            Word("against", 0.0, 0.2, 1.0),
            Word("two", 0.2, 0.4, 1.0),
            Word("players", 0.4, 0.6, 1.0),
            Word("three", 0.6, 0.8, 1.0),
            Word("attempts", 0.8, 1.0, 1.0),
        ],
        language="en",
    )
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=1.0)],
        clip_assignments=[],
        clip_id_by_path={},
        creator_request="Add the scores mentioned in the audio.",
        explicit_opening_title=None,
        storyboard={"overlays": []},
    )

    assert not any(
        "score:" in item["source_params"].get("narrated_storyboard", "") for item in elements
    )


def test_requested_scores_still_require_transcript_score_context() -> None:
    transcript = Transcript(
        words=[
            Word("We", 0.0, 0.2, 1.0),
            Word("had", 0.2, 0.4, 1.0),
            Word("two", 0.4, 0.6, 1.0),
            Word("players", 0.6, 0.8, 1.0),
            Word("and", 0.8, 1.0, 1.0),
            Word("three", 1.0, 1.2, 1.0),
            Word("attempts", 1.2, 1.4, 1.0),
        ],
        language="en",
    )
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=2.0)],
        clip_assignments=[],
        clip_id_by_path={},
        creator_request="Please add the scores mentioned in the audio.",
        explicit_opening_title=None,
        storyboard={"overlays": []},
    )

    assert not any(
        "score:" in item["source_params"].get("narrated_storyboard", "") for item in elements
    )


def test_spoken_numbers_without_score_context_are_not_promoted() -> None:
    transcript = Transcript(
        words=[
            Word("I", 0.0, 0.2, 1.0),
            Word("saw", 0.2, 0.4, 1.0),
            Word("six", 0.4, 0.6, 1.0),
            Word("four", 0.6, 0.8, 1.0),
        ],
        language="en",
    )
    elements = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=1.0)],
        clip_assignments=[],
        clip_id_by_path={},
        creator_request="Add intro text",
        explicit_opening_title=None,
        storyboard={"overlays": []},
    )
    assert not any(
        "score:" in item["source_params"].get("narrated_storyboard", "") for item in elements
    )


def test_player_placeholders_are_stable_by_canonical_clip_order() -> None:
    timings = [
        SimpleNamespace(step_id="s0", start_s=0.0, end_s=1.0),
        SimpleNamespace(step_id="s1", start_s=1.0, end_s=2.0),
        SimpleNamespace(step_id="s2", start_s=2.0, end_s=3.0),
    ]
    assignments = [
        SimpleNamespace(step_id="s0", clip_path="/b.mp4"),
        SimpleNamespace(step_id="s1", clip_path="/a.mp4"),
        SimpleNamespace(step_id="s2", clip_path="/b.mp4"),
    ]
    elements = gb._narrated_storyboard_text_elements(
        transcript=Transcript(
            words=[Word("Players", 0.0, 0.4, 1.0), Word("compete", 1.0, 1.4, 1.0)],
            language="en",
        ),
        step_timings=timings,
        clip_assignments=assignments,
        clip_id_by_path={"/a.mp4": "clip_a", "/b.mp4": "clip_b"},
        canonical_clip_ids=["clip_a", "clip_b"],
        creator_request="Add a placeholder name for everyone involved",
        explicit_opening_title=None,
        storyboard={"overlays": []},
    )

    placeholders = [item for item in elements if item["text"].startswith("PLAYER")]
    # Storyboard order is b, a, b, but labels stay tied to upload/clip order.
    assert [item["text"] for item in placeholders] == ["PLAYER 2", "PLAYER 1"]
    assert [item["start_s"] for item in placeholders] == [0.0, 1.0]
    assert [item["source_params"]["participant_key"] for item in placeholders] == [
        "clip:clip_b",
        "clip:clip_a",
    ]


@pytest.mark.parametrize(
    "words",
    [
        [],
        [Word("Hello", 0.1, 0.5, 1.0)],
    ],
    ids=["empty_transcript", "one_word"],
)
def test_short_transcript_still_renders_one_scene(monkeypatch, tmp_path, words) -> None:
    transcript = Transcript(words=words, language="en")
    seen: dict = {}

    def fake_download(_gcs_path, local_path):
        tmp_path.joinpath(local_path.rsplit("/", 1)[-1]).write_bytes(b"voice")

    def fake_assemble(step_timings, clip_assignments, _voiceover, output_path, _tmpdir, **kw):
        seen["timings"] = step_timings
        seen["assignments"] = clip_assignments
        tmp_path.joinpath(output_path.rsplit("/", 1)[-1]).write_bytes(b"video")
        tmp_path.joinpath(kw["base_output_path"].rsplit("/", 1)[-1]).write_bytes(b"base")
        return []

    monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", False)
    monkeypatch.setattr("app.storage.download_to_file", fake_download)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_args, **_kwargs: "signed")
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", lambda *_a, **_k: transcript)
    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    monkeypatch.setattr("app.tasks.template_orchestrate._probe_duration", lambda _path: 2.0)

    result = gb._render_narrated_variant(
        job_id="job",
        rank=1,
        spec={"variant_id": "narrated", "voiceover_gcs_path": "voiceover/file"},
        filming_guide=[],
        narrative_order=["clip_0"],
        clip_id_to_local={"clip_0": "/tmp/clip.mp4"},
        variant_dir=str(tmp_path),
    )

    assert result["ok"] is True
    assert len(seen["timings"]) == 1
    assert seen["timings"][0].start_s == 0.0
    assert seen["timings"][0].end_s == 2.0
    assert len(seen["assignments"]) == 1


def test_initial_narrated_render_semantically_assigns_clips_and_composes_text(
    monkeypatch, tmp_path
) -> None:
    transcript = Transcript(
        words=[
            Word("first", 0.0, 0.4, 1.0),
            Word("play", 0.4, 0.8, 1.0),
            Word("score", 1.0, 1.2, 1.0),
            Word("six", 1.2, 1.5, 1.0),
            Word("four", 1.5, 1.8, 1.0),
        ],
        language="en",
    )
    seen: dict = {}

    def fake_download(_gcs_path, local_path):
        with open(local_path, "wb") as handle:
            handle.write(b"voice")

    def fake_assemble(step_timings, clip_assignments, _voiceover, output_path, _tmpdir, **kw):
        seen["assignments"] = clip_assignments
        with open(output_path, "wb") as handle:
            handle.write(b"captions")
        with open(kw["base_output_path"], "wb") as handle:
            handle.write(b"base")
        return [{"text": "first play", "start_s": 0.0, "end_s": 0.8}]

    def fake_compose(_base_path, variant, tmpdir, **_kwargs):
        seen["composed"] = variant
        out = f"{tmpdir}/composed.mp4"
        with open(out, "wb") as handle:
            handle.write(b"composed")
        return out, None

    monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", True)
    monkeypatch.setattr("app.storage.download_to_file", fake_download)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_args, **_kwargs: "signed")
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", lambda *_a, **_k: transcript)
    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    monkeypatch.setattr(gb, "_rendered_duration_s", lambda _path: 2.0)
    monkeypatch.setattr(gb, "_compose_subtitled_final", fake_compose)
    monkeypatch.setattr(
        gb,
        "_narrated_storyboard_plan",
        lambda **_kwargs: {
            "enabled": True,
            "status": "ready",
            "prompt_version": "test",
            "matches": [
                {"segment_id": "shot_1", "clip_id": "clip_1"},
                {"segment_id": "shot_2", "clip_id": "clip_0"},
            ],
            "overlays": [
                {"kind": "intro", "text": "Match Story"},
                {
                    "kind": "score",
                    "anchor_word_id": "w000003",
                    "end_word_id": "w000004",
                },
            ],
        },
    )

    result = gb._render_narrated_variant(
        job_id="job",
        rank=1,
        spec={"variant_id": "narrated", "voiceover_gcs_path": "voiceover/file"},
        filming_guide=[
            {"shot_id": "shot_1", "what": "first play"},
            {"shot_id": "shot_2", "what": "score six four"},
        ],
        narrative_order=["clip_0", "clip_1"],
        clip_id_to_local={"clip_0": "/a.mp4", "clip_1": "/b.mp4"},
        clip_durations_s={"clip_0": 4.0, "clip_1": 4.0},
        creator_request="Add intro texts, player names, and scores",
        variant_dir=str(tmp_path),
    )

    assert result["ok"] is True
    assert [item.clip_path for item in seen["assignments"]] == ["/b.mp4", "/a.mp4"]
    assert {item["text"] for item in result["text_elements"]} >= {
        "Match Story",
        "PLAYER 1",
        "PLAYER 2",
        "six four",
    }
    assert result["narrated_clip_assignments"] == [
        {
            "step_id": "shot_1",
            "clip_id": "clip_1",
            "participant_key": "clip:clip_1",
            "source_start_s": 0.0,
        },
        {
            "step_id": "shot_2",
            "clip_id": "clip_0",
            "participant_key": "clip:clip_0",
            "source_start_s": 0.0,
        },
    ]
    assert seen["composed"]["caption_cues"]
    assert result["narrated_storyboard"]["status"] == "ready"


def test_storyboard_failure_renders_without_generated_text(monkeypatch, tmp_path) -> None:
    transcript = Transcript(words=[Word("Hello", 0.0, 0.5, 1.0)], language="en")

    def fake_assemble(_timings, _assignments, _voiceover, output_path, _tmpdir, **kwargs):
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        with open(kwargs["base_output_path"], "wb") as handle:
            handle.write(b"base")
        return [{"text": "Hello", "start_s": 0.0, "end_s": 0.5}]

    def fake_download(_source, target):
        with open(target, "wb") as handle:
            handle.write(b"voice")

    monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", True)
    monkeypatch.setattr("app.storage.download_to_file", fake_download)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_args, **_kwargs: "signed")
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", lambda *_a, **_k: transcript)
    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    monkeypatch.setattr("app.tasks.template_orchestrate._probe_duration", lambda _path: 1.0)
    monkeypatch.setattr(
        gb,
        "_narrated_storyboard_plan",
        lambda **_kwargs: {
            "enabled": True,
            "status": "fallback",
            "matches": [],
            "overlays": [],
            "failure_code": "storyboard_provider_error",
        },
    )

    result = gb._render_narrated_variant(
        job_id="job",
        rank=1,
        spec={"variant_id": "narrated", "voiceover_gcs_path": "voiceover/file"},
        filming_guide=[],
        narrative_order=["clip_0"],
        clip_id_to_local={"clip_0": "/tmp/clip.mp4"},
        clip_metas=[SimpleNamespace(clip_id="clip_0")],
        clip_durations_s={"clip_0": 3.0},
        creator_request="Add intro text, player names, and scores",
        variant_dir=str(tmp_path),
    )

    assert result["ok"] is True
    assert result["text_elements"] == []
    assert result["caption_cues"]
