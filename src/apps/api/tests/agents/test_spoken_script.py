"""Shared voiceover-script decision used by both edit planners."""

import pytest

from app.agents.spoken_script import is_spoken_script_copy, narration_word_keys, word_keys
from tests.agents.test_semantic_edit_proposal import (
    SAGRADA_ASR_TRANSCRIPT,
    SAGRADA_VO_REQUEST,
    sagrada_narration_words,
)

_SPOKEN = narration_word_keys(sagrada_narration_words())


def test_narration_word_keys_match_per_word_copy_keys() -> None:
    assert narration_word_keys([{"text": "It's"}, {"text": "  "}, {}, {"text": "172.5"}]) == [
        "its",
        "1725",
    ]
    assert _SPOKEN == word_keys(SAGRADA_ASR_TRANSCRIPT)


@pytest.mark.parametrize(
    "text, request_text, spoken, expected",
    [
        pytest.param(
            "Would you wait 144 years?", SAGRADA_VO_REQUEST, [], True, id="vo_quote_empty_asr"
        ),
        pytest.param(
            # ClipIntent.creator_text truncation: 60 chars, cut mid-word.
            "In 1936, his workshop is set on fire. Plans burned, models s",
            SAGRADA_VO_REQUEST,
            _SPOKEN,
            True,
            id="truncated_vo_quote",
        ),
        pytest.param(
            "Building since 1882", SAGRADA_VO_REQUEST, _SPOKEN, False, id="title_quote_is_copy"
        ),
        pytest.param(
            "The sea keeps its own hours",
            'VO: "The sea keeps its own hours" on screen over the pub clip.',
            "the sea keeps its own hours".split(),
            False,
            id="display_instruction_keeps_copy",
        ),
        pytest.param(
            "The sea keeps its own hours",
            "Caption the pub clips.",
            "and the sea keeps its own hours".split(),
            True,
            id="unquoted_copy_the_transcript_speaks",
        ),
        pytest.param(
            "post match pub",
            "Caption the pub clips.",
            "we went to the post match pub".split(),
            False,
            id="short_unquoted_copy_stays_a_caption",
        ),
        pytest.param(
            "post match pub",
            'Say "post match pub" for the pub clips. VO: "we went to the post match pub"',
            [],
            False,
            id="short_copy_inside_a_vo_line_stays_a_caption",
        ),
    ],
)
def test_is_spoken_script_copy(
    text: str, request_text: str, spoken: list[str], expected: bool
) -> None:
    assert is_spoken_script_copy(text, request_text, spoken) is expected
