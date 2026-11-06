"""Qendresa case 3: "no captions" in the same message moves her text's place and colour to captions.

Her Q3 message: 'Just put this text in the middle for the whole video: "..." Small white text like
TikTok's, no captions and nothing else. ...'. The phone text-style pass reads style words from the
job's request (`schemas.phone_text_style.explicit_text_rule`, read by
`services.phone_text_styling.requested_style`). It filed "in the middle" + "white" under the
CAPTION role because "captions" appears in "no captions", and stripped the strategy's white
`text_color` from `all_text`; her pinned line stayed at the top.

The first four tests are the kit's `test_q3_no_captions_steals_text_style.py`, ported with the same
assertions. The rest are the neighbours from `docs/reviews/no-captions-text-style-failure-modes.md`
(rows D4-D16). Deterministic: pure parser, the deterministic placement pass (Skia measurement, no
model; `selected_fonts={}` = the typography agent's fallback) and the dispatch stamp.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "x",
    "DATABASE_URL": "postgresql://localhost:5432/nova_test",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}.items():
    os.environ.setdefault(key, value)

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: E402
from app.kria.brief_binding import BriefBinding  # noqa: E402
from app.pipeline.guided_story import compile_execution_plan  # noqa: E402
from app.pipeline.phone_text_style import resolve_phone_text_style  # noqa: E402
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage  # noqa: E402
from app.schemas.phone_text_style import explicit_text_rule  # noqa: E402
from app.services.phone_text_styling import (  # noqa: E402
    PHONE_TEXT_STYLE_FIELD,
    requested_style,
    stamp_phone_text_style,
)

TEXT = "The prompt I run at the end of every Claude session"
PROMPT = (
    f'Just put this text in the middle for the whole video: "{TEXT}". Small white text like '
    "TikTok's, no captions and nothing else. Keep the whole clip, don't cut it. Mute my sound, "
    "I'll add a song on TikTok."
)
HER_TEXT_LOOK = {"position": "middle", "color": "#FFFFFF"}


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    # Written to the instance dict so the test also runs (red) before the setting exists.
    monkeypatch.setitem(settings.__dict__, "text_style_segment_role_enabled", on)


# ------------------------------------------------------------ the kit tests (ported, same asserts)


def test_middle_and_white_style_her_text_not_the_captions() -> None:
    requested = requested_style({}, PROMPT)
    text_rule = requested.get("all_text", {})
    assert text_rule.get("position") == "middle" and text_rule.get("color") == "#FFFFFF", (
        f"requested_style filed her text's look as {requested}"
    )


def test_strategy_white_text_colour_survives_a_no_captions_ask() -> None:
    requested = requested_style({"text_color": "#FFFFFF"}, PROMPT)
    assert requested.get("all_text", {}).get("color") == "#FFFFFF", (
        f"the strategy's white text_color was dropped: {requested}"
    )


def test_her_pinned_line_lands_centred_in_the_middle_band() -> None:
    clip = UnifiedClip(
        media_id="clip-1",
        proxy_path="users/u/analysis-proxy-clip-1.mp4",
        generation="1",
        duration_s=11.0,
        width=1080,
        height=1920,
        analysis={"best_moments": []},
    )
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "direction": "fast_montage",
        # The nearest corner the Main Creator can pick for "in the middle" (corners only).
        "pinned_texts": [{"text": TEXT, "corner": "top_center"}],
        "text_color": "#FFFFFF",
    }
    plan = plan_unified_montage([clip], brief_view(None), strategy=strategy)
    rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
    styled = resolve_phone_text_style(
        rows,
        preferences={"all_text": {}},
        requested=requested_style(strategy, PROMPT),
        selected_fonts={},
    )
    pin = next(row for row in styled if row["id"] == "guided-pinned-0")
    # answer_key.json Q3 rule text_place: centred x 0.4-0.6 at 0.25-0.65 height.
    assert 0.4 <= pin["x_frac"] <= 0.6 and 0.25 <= pin["y_frac"] <= 0.65, (
        f"her line sits at x={pin['x_frac']} y={pin['y_frac']} (asked: the middle)"
    )
    assert pin["color"] == "#FFFFFF", f"her line is {pin['color']} (asked: white)"


def test_control_without_the_caption_word_the_line_is_centred_and_white() -> None:
    requested = requested_style({}, PROMPT.replace("no captions and nothing else", "nothing else"))
    assert requested == {"all_text": {"position": "middle", "color": "#FFFFFF"}}


# ------------------------------------------------------------------- neighbours (failure modes)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # D4: plain "text" words right after the caption words describe the captions.
        (
            "Captions word by word, black bold text in a light grey box near the bottom.",
            ("caption", {"color": "#000000"}),
        ),
        # D4b (review): plain "text" words in a later sentence describe the role named before
        # them, as before this fix.
        ("Make the title bold. The text should be white.", ("title", {"color": "#FFFFFF"})),
        ("Captions please. Make the text yellow.", ("caption", {"color": "#FFFF00"})),
        (
            "Captions with no outline. White text.",
            ("caption", {"color": "#FFFFFF", "stroke_width": 0}),
        ),
        # D5: the caption segment holds a place word, so the plain colour before it follows it.
        ("White text, captions in the middle", ("caption", HER_TEXT_LOOK)),
        # D8: a shadow word is read from the whole message; its role is unknown -> nothing.
        ("Small white text in the middle, no captions, no shadows", None),
        # D9: a role word inside her quoted line is her text, not a role.
        ('Put "My captions diary" in the middle in white', ("all_text", HER_TEXT_LOOK)),
        # D10: Turkish, "put this text in the middle, white, no subtitles".
        ("Bu yazıyı ortaya koy, beyaz olsun, altyazı olmasın.", ("all_text", HER_TEXT_LOOK)),
        # D1 on the parser itself.
        (PROMPT, ("all_text", HER_TEXT_LOOK)),
    ],
)
def test_style_words_follow_the_text_they_sit_next_to(text, expected) -> None:
    assert explicit_text_rule(text) == expected


def test_switch_off_is_the_old_whole_message_role(monkeypatch) -> None:
    """D16: TEXT_STYLE_SEGMENT_ROLE_ENABLED=false files her look under captions, as before."""
    _switch(monkeypatch, False)
    assert explicit_text_rule(PROMPT) == ("caption", HER_TEXT_LOOK)
    assert requested_style({"text_color": "#FFFFFF"}, PROMPT) == {"caption": HER_TEXT_LOOK}


# ------------------------------------------------------- the typed "captions off" fact (D6/D7/D12)


def _stamped(monkeypatch, request: str, captions: str | None) -> dict:
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    facts = {"captions": captions} if captions else {}
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1", kind="style", scope="global", description=request, facts=facts
            )
        ],
    )
    job = SimpleNamespace(
        assembly_plan={
            "creator_brief_binding": BriefBinding.create(uuid.uuid4(), brief).model_dump(
                mode="json"
            )
        },
        all_candidates={"creator_request": request, "creator_strategy": {}},
    )
    stamp_phone_text_style(job)
    return job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["requested"]


@pytest.mark.parametrize(
    "request_text",
    [
        # D6: the caption words come first, so the words alone hand her look to the captions.
        "No captions, white text in the middle.",
        "No captions. White text in the middle.",
        # D7: one segment names both.
        "White text in the middle with no captions.",
    ],
)
def test_a_brief_with_captions_off_gives_the_look_to_her_text(monkeypatch, request_text) -> None:
    assert _stamped(monkeypatch, request_text, "off") == {"all_text": HER_TEXT_LOOK}


def test_a_brief_with_captions_on_keeps_the_caption_look(monkeypatch) -> None:
    """D12 negative control: captions on (or no caption fact) keeps the caption rule."""
    request = "No captions, white text in the middle."
    assert _stamped(monkeypatch, request, "on") == {"caption": HER_TEXT_LOOK}
    assert _stamped(monkeypatch, request, None) == {"caption": HER_TEXT_LOOK}


def test_switch_off_skips_the_typed_backstop(monkeypatch) -> None:
    _switch(monkeypatch, False)
    request = "No captions, white text in the middle."
    assert _stamped(monkeypatch, request, "off") == {"caption": HER_TEXT_LOOK}
