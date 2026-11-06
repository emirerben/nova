"""Item 21 (Qendresa): one line of text pinned in the MIDDLE in TikTok's native look.

Flag: ``CLIP_TEXT_TIKTOK_LOOK_ENABLED`` (``settings.clip_text_tiktok_look_enabled``, default off).
Failure modes covered are numbered as in ``docs/reviews/qendresa-item21-clip-text-look.md``:

* 1  flag off changes anything            -> the two golden tests (rows, prompt bytes)
* 3  a model writes the new values while the flag is off -> flag-off rows equal the golden
* 4  "in the middle" lands on her face    -> moved-off test + receipt test
* 6/7/8  style pass resets the look / paid typography agent picks another font
* 9  the same words as opening_title are drawn twice
* 12 two middle lines overlap
* 17 receipt says "Done" after moving it  -> receipt tests
* 22 a size / colour she names wins over the look

Deterministic: the pure planner + the placement pass (Skia measurement, no model, no network).
``selected_fonts={"pinned": "Unbounded"}`` simulates the paid typography agent choosing another
font; the look's own font must win.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

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

from app.agents._schemas.creator_agent import (  # noqa: E402
    CapabilityAvailability,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import (  # noqa: E402
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
    MainCreatorInput,
)
from app.config import settings  # noqa: E402
from app.kria.brief import BriefRequirement  # noqa: E402
from app.kria.brief_checks import PlanFacts, _text_style_rows, check_text_look  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.guided_story import compile_execution_plan  # noqa: E402
from app.pipeline.phone_text_style import (  # noqa: E402
    animation_box,
    measure_box,
    resolve_phone_text_style,
    text_role,
)
from app.pipeline.text_stack_layout import measure_text_block  # noqa: E402
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage  # noqa: E402
from app.services.phone_text_styling import requested_style  # noqa: E402

TEXT = "The prompt I run at the end of every Claude session"
PROMPT = (
    f'Just put this text in the middle for the whole video: "{TEXT}". Small white text like '
    "TikTok's, no captions and nothing else. Make it 11 seconds. Mute my sound, "
    "I'll add a song on TikTok."
)
FIXTURE = Path(__file__).parent / "fixtures" / "clip_text_look_flag_off_rows.json"
# sha256 of the flag-off Main Creator prompt for ``_prompt_input()``, recorded on the code before
# the feature. The version string is not part of the rendered prompt, so the bump is invisible.
PROMPT_GOLDEN_SHA256 = "c6da2a4c5408c9037406ce2e7d7d9b290a83a65bdb622889a6694b3aad14d761"
FACE_V2 = (0.12, 0.29, 0.79, 0.67)


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    # The field does not exist until the feature lands and pydantic refuses setattr on unknown
    # names, so write the instance dict (works before and after the field exists).
    monkeypatch.setitem(settings.__dict__, "clip_text_tiktok_look_enabled", on)


def _clip() -> UnifiedClip:
    return UnifiedClip(
        media_id="clip-1",
        proxy_path="users/u/analysis-proxy-clip-1.mp4",
        generation="1",
        duration_s=11.0,
        width=1080,
        height=1920,
        analysis={"best_moments": []},
    )


def _strategy(pins: list[dict], **extra) -> dict:
    return {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "direction": "fast_montage",
        "pinned_texts": pins,
        **extra,
    }


def _middle_pin(**extra) -> dict:
    return {"text": TEXT, "corner": "middle_center", "look": "tiktok", **extra}


def _compiled_rows(strategy: dict) -> list[dict]:
    plan = plan_unified_montage([_clip()], brief_view(None), strategy=strategy)
    return compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]


def _style(
    strategy: dict,
    *,
    selected_fonts: dict | None = None,
    excluded_boxes: list | None = None,
) -> list[dict]:
    rows = _compiled_rows(strategy)
    return resolve_phone_text_style(
        rows,
        preferences={"all_text": {}},
        requested=requested_style(strategy, PROMPT),
        selected_fonts=selected_fonts if selected_fonts is not None else {},
        excluded_boxes=excluded_boxes,
    )


def _pin(styled: list[dict], index: int = 0) -> dict:
    return next(row for row in styled if row["id"] == f"guided-pinned-{index}")


def _simulated_agent_fonts(strategy: dict) -> dict:
    """A paid typography agent that picks a different font for the pinned role."""
    role = text_role({"id": "guided-pinned-0"})
    assert role == "pinned"
    return {role: "Unbounded"}


def _block(row: dict) -> tuple[float, float, float, float]:
    return animation_box(row, measure_box(row, PORTRAIT), PORTRAIT)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _assert_tiktok_look(row: dict) -> None:
    assert row["color"].upper() == "#FFFFFF"
    assert row["font_family"] == "TikTok Sans Bold"
    assert row["size_px"] == 56
    assert row["shadow_enabled"] is True
    assert row["shadow_color"].upper() == "#000000"
    assert row["shadow_opacity"] == pytest.approx(0.2)


# 1 ---------------------------------------------------------------------------------------


def test_middle_tiktok_pin_is_centred_white_tiktok_sans_for_the_whole_video(monkeypatch) -> None:
    """Failure modes 6, 7, 8: her line is centred, small white TikTok Sans, two lines, 0-11 s,
    even when the typography agent picked another font and the parsed ask says "no captions"."""
    _flag(monkeypatch, True)
    strategy = _strategy([_middle_pin()])
    styled = _style(strategy, selected_fonts=_simulated_agent_fonts(strategy))

    rows = [row for row in styled if row["text"] == TEXT]
    assert len(rows) == 1
    pin = rows[0]
    assert pin["start_s"] == 0 and pin["end_s"] == pytest.approx(11.0)
    assert pin["x_frac"] == pytest.approx(0.5, abs=0.01)
    assert pin["y_frac"] == pytest.approx(0.5, abs=0.02)
    _assert_tiktok_look(pin)
    # Her answer-key rule (Q3 text_place).
    assert 0.4 <= pin["x_frac"] <= 0.6 and 0.25 <= pin["y_frac"] <= 0.65
    # Her V3 line breaks exactly "The prompt I run at the end / of every Claude session".
    assert len(measure_text_block(pin)["lines"]) == 2


# 2 / 3 -----------------------------------------------------------------------------------


def test_middle_pin_moves_off_her_face_and_says_so(monkeypatch) -> None:
    """Failure mode 4: V2's face spans y 0.29-0.67; the line moves to the nearest clear spot
    (here above it: below, the Kria wordmark keep-out pushes it to y ~0.85, under TikTok's own
    caption), keeps the look, and leaves the receipt fact ``moved_off == ["face"]``."""
    _flag(monkeypatch, True)
    strategy = _strategy([_middle_pin()])
    face = {"start_s": 0.0, "end_s": 11.0, "box": FACE_V2, "kind": "face"}
    styled = _style(
        strategy, selected_fonts=_simulated_agent_fonts(strategy), excluded_boxes=[face]
    )
    pin = _pin(styled)

    swept = _block(pin)
    assert not _overlap(swept, FACE_V2)
    assert swept[3] < FACE_V2[1] and swept[1] >= 0.08, f"not just above her face: {swept}"
    assert pin["source_params"]["moved_off"] == ["face"]
    assert pin["x_frac"] == pytest.approx(0.5, abs=0.01)
    assert pin["color"].upper() == "#FFFFFF" and pin["font_family"] == "TikTok Sans Bold"


def test_her_look_survives_the_workers_provisional_font(monkeypatch) -> None:
    """Failure mode 7/8 as the worker runs it: the phone job plans under the text policy with
    real glyph coverage, so the planner fills the snapshot's ``font_family`` with a provisional
    curated face (KRI-1024) although she named no font. Her look's font must still win."""
    from app.pipeline.unified_montage import skia_font_covers
    from app.services.phone_text_styling import phone_text_policy_scope

    _flag(monkeypatch, True)
    strategy = _strategy([_middle_pin()])
    policy = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": requested_style(strategy, PROMPT),
    }
    with phone_text_policy_scope(policy):
        plan = plan_unified_montage(
            [_clip()], brief_view(None), strategy=strategy, font_covers=skia_font_covers
        )
        rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
        styled = resolve_phone_text_style(
            rows, preferences={"all_text": {}}, requested=policy["requested"], selected_fonts={}
        )
    pin = _pin(styled)
    assert pin["font_family"] == "TikTok Sans Bold", pin["font_family"]
    assert pin["color"].upper() == "#FFFFFF"


def test_middle_pin_stays_centred_when_the_face_is_elsewhere(monkeypatch) -> None:
    """Failure mode 4 (control): a face away from the middle does not move the line and
    leaves no ``moved_off`` fact."""
    _flag(monkeypatch, True)
    face = {"start_s": 0.0, "end_s": 11.0, "box": (0.15, 0.12, 0.45, 0.32), "kind": "face"}
    styled = _style(_strategy([_middle_pin()]), excluded_boxes=[face])
    pin = _pin(styled)

    assert pin["y_frac"] == pytest.approx(0.5, abs=0.02)
    assert "moved_off" not in (pin.get("source_params") or {})


# 4 ---------------------------------------------------------------------------------------


def test_two_middle_lines_stack_without_overlapping(monkeypatch) -> None:
    """Failure mode 12: two middle_center pins are both centred in x and never overlap."""
    _flag(monkeypatch, True)
    second = {"text": "Second line of her text", "corner": "middle_center", "look": "tiktok"}
    styled = _style(_strategy([_middle_pin(), second]))
    first, other = _pin(styled, 0), _pin(styled, 1)

    assert first["x_frac"] == pytest.approx(0.5, abs=0.01)
    assert other["x_frac"] == pytest.approx(0.5, abs=0.01)
    box_a = measure_box(first, PORTRAIT)
    box_b = measure_box(other, PORTRAIT)
    assert not _overlap(box_a, box_b)


# 5 ---------------------------------------------------------------------------------------


def test_opening_title_with_the_same_words_is_not_drawn_twice(monkeypatch) -> None:
    """Failure mode 9: a confirmed ``opening_title`` equal to a whole-video pin's words is
    dropped; today both draw (the title for 2.2 s on top of the pin)."""
    _flag(monkeypatch, True)
    strategy = _strategy([_middle_pin()], opening_title=TEXT)
    rows = [row for row in _compiled_rows(strategy) if TEXT in str(row.get("text"))]
    assert [row["id"] for row in rows] == ["guided-pinned-0"]


# 6 ---------------------------------------------------------------------------------------


def test_pin_own_size_wins_over_the_look(monkeypatch) -> None:
    """Failure mode 22: "big TikTok text" - the pin's own ``size_px`` beats the look's 56."""
    _flag(monkeypatch, True)
    styled = _style(_strategy([_middle_pin(size_px=80)]))
    pin = _pin(styled)
    assert pin["size_px"] == 80
    assert pin["font_family"] == "TikTok Sans Bold"


def test_strategy_text_colour_wins_over_the_look(monkeypatch) -> None:
    """Failure mode 21: a named colour beats the look's white."""
    _flag(monkeypatch, True)
    styled = _style(_strategy([_middle_pin()], text_color="#FFFF00"))
    assert _pin(styled)["color"].upper() == "#FFFF00"


# 7 ---------------------------------------------------------------------------------------


def _golden_rows() -> list[dict]:
    """Today's styled rows for the top_center pin (no look), JSON-normalised."""
    styled = _style(_strategy([{"text": TEXT, "corner": "top_center"}]))
    return json.loads(json.dumps(styled, sort_keys=True))


def test_flag_off_rows_match_the_recorded_golden(monkeypatch) -> None:
    """Failure mode 1: with the flag off the styled rows are byte-identical to today's."""
    _flag(monkeypatch, False)
    assert _golden_rows() == json.loads(FIXTURE.read_text())


def test_flag_off_middle_look_pin_is_drawn_like_top_center(monkeypatch) -> None:
    """Failure mode 3: a pin written with middle_center + look while the flag is off is drawn
    exactly like top_center with no look (fails today: the schema rejects the corner)."""
    _flag(monkeypatch, False)
    styled = _style(_strategy([_middle_pin()]))
    assert json.loads(json.dumps(styled, sort_keys=True)) == json.loads(FIXTURE.read_text())


# 8 / 9 -----------------------------------------------------------------------------------


def _prompt_input() -> MainCreatorInput:
    available = CapabilityAvailability(available=True)
    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id="clip-00-11111111-1111-1111-1111-111111111111", kind="video")
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    return MainCreatorInput(
        user_message="Put my text in the middle.",
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
    )


def test_flag_off_main_creator_prompt_bytes_are_unchanged(monkeypatch) -> None:
    """Failure mode 1: the flag-off prompt is byte-identical to today's."""
    _flag(monkeypatch, False)
    prompt = MainCreatorAgent(None).render_prompt(_prompt_input())  # type: ignore[arg-type]
    assert hashlib.sha256(prompt.encode()).hexdigest() == PROMPT_GOLDEN_SHA256


def test_flag_on_main_creator_prompt_teaches_the_middle_look(monkeypatch) -> None:
    """The model must be told about ``middle_center`` and ``"look": "tiktok"``; version bumped."""
    _flag(monkeypatch, True)
    prompt = MainCreatorAgent(None).render_prompt(_prompt_input())  # type: ignore[arg-type]
    assert "middle_center" in prompt
    assert '"look": "tiktok"' in prompt
    assert MAIN_CREATOR_PROMPT_VERSION == "2026-11-06-v136"  # v128 + v131 + v133 (16) + v135 (20) + v136 (18)


# 10 --------------------------------------------------------------------------------------


def _requirement() -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="text",
        scope="global",
        literal=TEXT,
        description="Put this text in the middle for the whole video",
        facts={"position": "middle"},
    )


def test_receipt_is_met_when_the_line_is_centred(monkeypatch) -> None:
    """Failure mode 17: centred row -> "met"."""
    _flag(monkeypatch, True)
    rows = _style(_strategy([_middle_pin()]))
    facts = PlanFacts(text_styles=_text_style_rows(rows))
    receipt = check_text_look(_requirement(), facts)
    assert receipt is not None and receipt.status == "met"


def test_receipt_is_partial_and_names_her_face_when_the_line_moved(monkeypatch) -> None:
    """Failure mode 17: a row moved off a face is "partial" with a reason that mentions the
    face - never "Done" after moving it."""
    _flag(monkeypatch, True)
    face = {"start_s": 0.0, "end_s": 11.0, "box": FACE_V2, "kind": "face"}
    rows = _style(_strategy([_middle_pin()]), excluded_boxes=[face])
    facts = PlanFacts(text_styles=_text_style_rows(rows))
    receipt = check_text_look(_requirement(), facts)
    assert receipt is not None and receipt.status == "partial"
    assert "face" in (receipt.reason or "").casefold()


# Review fixes ----------------------------------------------------------------------------

HUGE_FACES = [(0.30, 0.12, 0.70, 0.88), (0.02, 0.02, 0.98, 0.98)]


def _receipt_for(rows: list[dict], position: str = "middle"):
    facts = PlanFacts(text_styles=_text_style_rows(rows))
    req = BriefRequirement(
        id="r1",
        kind="text",
        scope="global",
        literal=TEXT,
        description="Put this text in the middle for the whole video",
        facts={"position": position},
    )
    return check_text_look(req, facts)


@pytest.mark.parametrize("box", HUGE_FACES)
def test_no_clear_spot_says_it_covers_the_face(monkeypatch, box) -> None:
    """Fix 1: a face over the whole middle column leaves the line on it; the row says
    ``covers == ["face"]`` (no moved_off) and the receipt is partial naming the face."""
    _flag(monkeypatch, True)
    face = {"start_s": 0.0, "end_s": 11.0, "box": box, "kind": "face"}
    rows = _style(_strategy([_middle_pin()]), excluded_boxes=[face])
    source = _pin(rows)["source_params"]
    assert source["covers"] == ["face"]
    assert "moved_off" not in source
    receipt = _receipt_for(rows)
    assert receipt is not None and receipt.status == "partial"
    assert "face" in (receipt.reason or "").casefold()


def test_stale_moved_off_is_dropped_on_the_fallback_path(monkeypatch) -> None:
    """Fix 4: a row arriving with ``moved_off=["face"]`` from an earlier pass, now with no
    clear spot, ends without ``moved_off``."""
    _flag(monkeypatch, True)
    rows = _compiled_rows(_strategy([_middle_pin()]))
    for row in rows:
        if row["id"] == "guided-pinned-0":
            row["source_params"] = {**(row.get("source_params") or {}), "moved_off": ["face"]}
    face = {"start_s": 0.0, "end_s": 11.0, "box": HUGE_FACES[0], "kind": "face"}
    styled = resolve_phone_text_style(
        rows,
        preferences={"all_text": {}},
        requested=requested_style(_strategy([_middle_pin()]), PROMPT),
        selected_fonts={},
        excluded_boxes=[face],
    )
    assert "moved_off" not in _pin(styled)["source_params"]


def test_authored_position_on_a_middle_pin_is_kept(monkeypatch) -> None:
    """Fix 2: a later chat edit stored position fields; the middle pin pass must not snap
    the row back to (0.5, 0.5)."""
    _flag(monkeypatch, True)
    strategy = _strategy([_middle_pin()])
    rows = _compiled_rows(strategy)
    for row in rows:
        if row["id"] == "guided-pinned-0":
            row.update(position="custom", x_frac=0.3, y_frac=0.2)
            row["source_params"] = {
                **(row.get("source_params") or {}),
                "creator_style_fields": ["position", "x_frac", "y_frac"],
            }
    styled = resolve_phone_text_style(
        rows,
        preferences={"all_text": {}},
        requested=requested_style(strategy, PROMPT),
        selected_fonts={},
    )
    pin = _pin(styled)
    assert pin["x_frac"] == pytest.approx(0.3, abs=0.01)
    assert pin["y_frac"] == pytest.approx(0.2, abs=0.02)


def test_small_face_move_is_still_told(monkeypatch) -> None:
    """Fix 3: a face at y 0.44-0.50 moves the line only ~0.07 (inside _row_at's tolerance);
    the receipt must still be partial and name the face."""
    _flag(monkeypatch, True)
    face = {"start_s": 0.0, "end_s": 11.0, "box": (0.30, 0.44, 0.70, 0.50), "kind": "face"}
    rows = _style(_strategy([_middle_pin()]), excluded_boxes=[face])
    assert _pin(rows)["source_params"].get("moved_off") == ["face"]
    receipt = _receipt_for(rows)
    assert receipt is not None and receipt.status == "partial"
    assert "face" in (receipt.reason or "").casefold()


def test_corner_pin_without_look_keeps_the_duplicate_title(monkeypatch) -> None:
    """Fix 5: with the flag on, only a middle/look pin suppresses the identical opening
    title; a plain top_left pin behaves as with the flag off (both drawn)."""
    pin = {"text": TEXT, "corner": "top_left"}
    counts = {}
    for on in (False, True):
        _flag(monkeypatch, on)
        rows = _compiled_rows(_strategy([pin], opening_title=TEXT))
        counts[on] = sorted(r["id"] for r in rows if TEXT in str(r.get("text")))
    assert counts[True] == counts[False]
    assert len(counts[True]) == 2
