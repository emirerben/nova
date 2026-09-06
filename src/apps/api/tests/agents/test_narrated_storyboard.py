from app.agents.narrated_storyboard import (
    NarratedStoryboardAgent,
    NarratedStoryboardInput,
)


def _input() -> NarratedStoryboardInput:
    return NarratedStoryboardInput(
        words=[
            {"word_id": "w000000", "text": "score", "start_s": 0.0, "end_s": 0.3},
            {"word_id": "w000001", "text": "six", "start_s": 0.3, "end_s": 0.6},
            {"word_id": "w000002", "text": "four", "start_s": 0.6, "end_s": 0.9},
        ],
        segments=[
            {
                "segment_id": "seg_0",
                "start_s": 0.0,
                "end_s": 0.9,
                "transcript": "score six four",
            }
        ],
        clips=[{"clip_id": "clip_0", "summary": "player shoots", "duration_s": 5.0}],
    )


def test_storyboard_parse_is_grounded_to_known_words_and_clips() -> None:
    output = NarratedStoryboardAgent(None).parse(
        '{"matches":[{"segment_id":"seg_0","clip_id":"clip_0"},'
        '{"segment_id":"seg_0","clip_id":"invented"},'
        '{"segment_id":"invented","clip_id":"clip_0"}],'
        '"overlays":[{"kind":"score","anchor_word_id":"w000001",'
        '"end_word_id":"w000002"},{"kind":"score","anchor_word_id":"w999999"}]}',
        _input(),
    )

    assert [match.clip_id for match in output.matches] == ["clip_0"]
    assert [(item.anchor_word_id, item.end_word_id) for item in output.overlays] == [
        ("w000001", "w000002")
    ]


def test_storyboard_prompt_version_is_pinned() -> None:
    assert NarratedStoryboardAgent.spec.prompt_version == "2026-09-06.4"


def test_storyboard_source_window_is_clamped_to_owned_clip() -> None:
    output = NarratedStoryboardAgent(None).parse(
        '{"matches":[{"segment_id":"seg_0","clip_id":"clip_0","source_start_s":99}],"overlays":[]}',
        _input(),
    )

    assert output.matches[0].source_start_s == 4.1
