"""KRI-849: both brief readers are taught the "ask me instead of guessing" facts.

The Main Creator (planning turn) and the narrow brief extractor (a snapshot-bound edit) share one
brief section (`_BRIEF_PROMPT_SECTION`). The server never reads the creator's words for this ask
(KRI-667), so the rule has to be in BOTH prompts or one route files the sentence as prose and the
gate, the writer and the receipts see nothing. Replay of the model's reply is in
`tests/evals/test_main_creator_evals.py` (cassettes ``kri849_dj_t1_*``).
"""

from __future__ import annotations

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.main_creator import _BRIEF_PROMPT_SECTION


def test_the_shared_brief_section_teaches_the_ask_and_the_answer() -> None:
    for fact in ("ask_if_unclear", "unclear_answer"):
        assert fact in _BRIEF_PROMPT_SECTION
    assert '{"unclear_answer": "omit"}' in _BRIEF_PROMPT_SECTION


def test_the_narrow_extractor_prompt_carries_the_same_rule() -> None:
    prompt = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )
    assert "ask_if_unclear" in prompt and "unclear_answer" in prompt
