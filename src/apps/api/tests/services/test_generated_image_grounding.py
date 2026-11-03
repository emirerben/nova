"""A creator-generated image must reach Visuals autoplace with real text.

Prod 2026-11-03: the 'Beer' image Kria generated was stored with
`analysis = {width, height, source, model}` only, so the overlay placement agent saw an
empty description and could not tie it to the spoken Turkish "alkol". These tests pin the
text the image carries and the plain label the creator sees.
"""

from __future__ import annotations

import uuid

from app.services.generated_image_grounding import (
    GROUNDING_PROMPT_MAX_CHARS,
    generated_image_analysis,
    grounding_label,
    prompt_metadata,
    with_generated_grounding,
)
from app.services.generated_media import (
    GeneratedMediaRequest,
    _request_metadata,
    operation_response,
)

PROD_ANALYSIS = {"width": 1024, "height": 1536, "source": "generated", "model": "gpt-image-1"}


def _body(**kw) -> GeneratedMediaRequest:
    base = {"client_event_id": uuid.uuid4(), "capability": "image", "prompt": "a cold beer"}
    return GeneratedMediaRequest(**(base | kw))


def test_image_request_keeps_a_trimmed_prompt_for_grounding():
    long_prompt = "a frosty glass of beer on a pub table " * 60
    meta = _request_metadata(_body(prompt=long_prompt))
    assert 0 < len(meta["image_prompt"]) <= GROUNDING_PROMPT_MAX_CHARS
    assert "\n" not in meta["image_prompt"]


def test_only_a_fresh_image_prompt_is_kept():
    edit = _body(
        capability="image_edit",
        prompt="make the hat red",
        source_asset_id=uuid.uuid4(),
        source_generation="3",
    )
    assert "image_prompt" not in _request_metadata(edit)
    assert prompt_metadata("narration", "hello there") == {}


def test_the_prompt_is_never_echoed_to_the_client():
    from types import SimpleNamespace

    op = SimpleNamespace(
        state="pending",
        capability="image",
        request_metadata={"image_prompt": "a cold beer"},
        output_path=None,
        output_generation=None,
        asset_id=None,
        failure_code=None,
        id=uuid.uuid4(),
    )
    assert "a cold beer" not in repr(operation_response(op))


def test_admitted_analysis_carries_prompt_and_title_but_no_screen_text():
    analysis = generated_image_analysis(
        width=1024,
        height=1536,
        model="gpt-image-1",
        prompt="a cold glass of beer on a pub table",
        title="Beer",
    )
    assert analysis["source"] == "generated"
    assert analysis["subject"] == "Beer"
    assert analysis["description"] == "a cold glass of beer on a pub table"
    assert (analysis["width"], analysis["height"]) == (1024, 1536)
    # Gemini/Nova-written metadata never becomes on-screen text; the prompt is the creator's
    # own and only grounds matching, so it fills no on-screen-text or brand field.
    assert "on_screen_text" not in analysis and "brands" not in analysis


def test_a_title_alone_or_a_prompt_alone_still_grounds():
    only_title = generated_image_analysis(
        width=1, height=1, model="m", prompt="", title="Beer"
    )
    assert only_title["subject"] == only_title["description"] == "Beer"
    only_prompt = generated_image_analysis(
        width=1, height=1, model="m", prompt="a cold beer. Photo style.", title=""
    )
    assert only_prompt["description"] == "a cold beer. Photo style."
    assert only_prompt["subject"].startswith("a cold beer")
    nothing = generated_image_analysis(width=1, height=1, model="m", prompt="", title="")
    assert "subject" not in nothing and "description" not in nothing


def test_existing_generated_rows_ground_lazily_from_their_title():
    """The two prod assets are not rewritten; the matcher input is derived on read."""
    out = with_generated_grounding(PROD_ANALYSIS, "Beer")
    assert out["subject"] == out["description"] == "Beer"
    assert PROD_ANALYSIS == {"width": 1024, "height": 1536, "source": "generated", "model": "gpt-image-1"}


def test_lazy_grounding_leaves_everything_else_alone():
    upload = {"subject": "dog", "description": "a dog"}
    assert with_generated_grounding(upload, "ctx") is upload
    assert with_generated_grounding(PROD_ANALYSIS, "") == PROD_ANALYSIS
    assert with_generated_grounding(None, "Beer") == {}
    saved = {**PROD_ANALYSIS, "description": "saved text"}
    assert with_generated_grounding(saved, "Beer")["description"] == "saved text"


def test_the_label_is_human_not_generated_png():
    assert grounding_label("Beer", PROD_ANALYSIS) == "Beer"
    assert grounding_label("", {**PROD_ANALYSIS, "subject": "a cold glass of beer"}) == (
        "a cold glass of beer"
    )
    assert grounding_label("", PROD_ANALYSIS) == "Generated image"
    assert len(grounding_label("x" * 200, PROD_ANALYSIS)) <= 60
