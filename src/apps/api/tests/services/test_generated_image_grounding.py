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
    long_prompt = "a frosty glass of beer on a pub table " * 30
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


def test_admitted_analysis_keeps_the_prompt_out_of_the_matcher_text_fields():
    analysis = generated_image_analysis(
        width=1024,
        height=1536,
        model="gpt-image-1",
        prompt="a cold glass of beer on a pub table",
        title="Beer",
    )
    assert analysis["source"] == "generated"
    # `subject` / `description` are read by the no-AI matcher, the wishlist filter, the client
    # and the edit proposal; the image-making prompt must not sit in them.
    assert analysis["subject"] == analysis["description"] == "Beer"
    assert analysis["generation_prompt"] == "a cold glass of beer on a pub table"
    assert (analysis["width"], analysis["height"]) == (1024, 1536)
    # Gemini/Nova-written metadata never becomes on-screen text: nothing here fills the
    # on-screen-text or brand fields that overlay text and sponsor checks read.
    assert "on_screen_text" not in analysis and "brands" not in analysis


def test_a_title_alone_or_a_prompt_alone_still_grounds():
    only_title = generated_image_analysis(width=1, height=1, model="m", prompt="", title="Beer")
    assert only_title["subject"] == only_title["description"] == "Beer"
    assert "generation_prompt" not in only_title
    only_prompt = generated_image_analysis(
        width=1, height=1, model="m", prompt="a cold beer. Photo style.", title=""
    )
    assert only_prompt["generation_prompt"] == "a cold beer. Photo style."
    assert "subject" not in only_prompt and "description" not in only_prompt
    nothing = generated_image_analysis(width=1, height=1, model="m", prompt="", title="")
    assert "subject" not in nothing and "description" not in nothing


def test_existing_generated_rows_ground_lazily_from_their_title():
    """The two prod assets are not rewritten; the matcher input is derived on read."""
    out = with_generated_grounding(PROD_ANALYSIS, "Beer")
    assert out["subject"] == out["description"] == "Beer"
    assert PROD_ANALYSIS == {
        "width": 1024,
        "height": 1536,
        "source": "generated",
        "model": "gpt-image-1",
    }


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
    assert grounding_label("", {**PROD_ANALYSIS, "generation_prompt": "a cold beer, photo"}) == (
        "a cold beer, photo"
    )
    assert grounding_label("", PROD_ANALYSIS) == "Generated image"
    assert len(grounding_label("x" * 200, PROD_ANALYSIS)) <= 60


COFFEE_PROMPT = (
    "A photorealistic cup of coffee on a wooden table, soft natural light, shallow depth of "
    "field, realistic textures"
)


def _coffee_asset() -> dict:
    return {
        "id": "gen-coffee",
        "kind": "image",
        "user_context": "Coffee",
        "source_filename": "generated.png",
        "analysis": generated_image_analysis(
            width=1024, height=1536, model="m", prompt=COFFEE_PROMPT, title="Coffee"
        ),
    }


def _words(text: str) -> list[dict]:
    return [
        {"word": w, "start_s": 1.0 + i * 0.3, "end_s": 1.2 + i * 0.3}
        for i, w in enumerate(text.split())
    ]


def test_no_ai_fallback_does_not_match_image_making_words():
    """The stored prompt is image-making text; it must not place the image on speech that only
    shares style words with it ('photorealistic', 'soft natural light')."""
    from app.services.overlay_autoplace import heuristic_match

    words = _words("in a realistic way with soft natural light and shallow depth")
    assert heuristic_match(words, [_coffee_asset()], duration_s=30.0) == []


def test_no_ai_fallback_still_places_on_the_title_exactly_as_before():
    from app.services.overlay_autoplace import heuristic_match

    asset = _coffee_asset()
    words = _words("my morning coffee coffee ritual")
    (placement,) = heuristic_match(words, [asset], duration_s=30.0)
    assert placement.asset_id == "gen-coffee"
    title_only = {
        **asset,
        "analysis": {k: v for k, v in asset["analysis"].items() if k != "generation_prompt"},
    }
    assert heuristic_match(words, [title_only], duration_s=30.0)[0].start_s == placement.start_s


def test_wishlist_filter_ignores_the_image_making_prompt():
    from app.services.overlay_autoplace import filter_wishlist_against_assets

    wish = "Add a photorealistic shot with soft natural light"
    assert filter_wishlist_against_assets([wish], [_coffee_asset()]) == [wish]


def test_the_prompt_text_is_redacted_from_agent_logs_and_admin_traces():
    """`OverlayPlacementAgent` is not `sensitive_io`, so its whole input lands in AgentRun and
    Langfuse; the stored prompt must be redacted there, while the title is not."""
    import json

    from app.agents.overlay_placement import (
        OverlayPlacementAgent,
        OverlayPlacementInput,
        PlacementAsset,
    )

    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    inp = OverlayPlacementInput(
        words=[{"word": "kahve", "start_s": 1.0, "end_s": 1.4}],
        assets=[
            PlacementAsset(
                asset_id="a1",
                origin="generated",
                user_context="Coffee",
                subject="Coffee",
                description="Coffee",
                generation_prompt=COFFEE_PROMPT,
            )
        ],
        duration_s=30.0,
        spoken_language="tr",
    )
    assert inp.assets[0].generation_prompt == COFFEE_PROMPT
    projected = agent.project_input_for_observability(inp.model_dump())
    dumped = json.dumps(projected)
    assert "photorealistic" not in dumped and COFFEE_PROMPT not in dumped
    assert "Coffee" in dumped


def test_generation_prompt_never_fills_on_screen_text_or_brands():
    analysis = _coffee_asset()["analysis"]
    from app.services.generated_image_grounding import with_generated_grounding as ground

    grounded = ground(analysis, "Coffee")
    assert not grounded.get("on_screen_text") and not grounded.get("brands")
    assert COFFEE_PROMPT not in (grounded["subject"], grounded["description"])
