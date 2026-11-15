"""KRI-1327 (Çiğdem S3, job 7416c94b): "Captions small, white, in the middle, never on my face".

The draft said "Done: … in the middle"; the phone drew the block at y 0.86 (measured 0.83), the
lower third, because the face-avoidance ladder jumps from the asked spot straight to 0.86.

Failure modes (written before the code):

D1  An asked middle spot whose block meets the chin leaves the middle row entirely (0.86)
    although a spot a few points lower in the middle row is clear.
D2  The move skips past the face upward into the top third (still "not the middle").
D3  A face that fills the whole middle row keeps the block on the face (the creator also said
    "never on my face": the face wins and the receipt must say it moved).
D4  A creator who placed nothing gets a different ladder than today (no regression).
D5  The finished check calls a block drawn out of the asked row "in the middle" (false Done).
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.caption_look_ask import caption_spot_holds, caption_spot_moved_reason
from app.kria.recipes import Canvas
from app.pipeline.phone_captions import CAPTION_FONT_FAMILY
from app.pipeline.phone_protected_text import CaptionSubject, choose_caption_look
from app.pipeline.render_geometry import NormalizedBox
from app.services.phone_text_styling import phone_text_policy_scope

CANVAS = Canvas(width=1080, height=1920)
# The first four lines of S3 take A as the render captioned them (variants[0].caption_cues).
_LINES = (
    ("Wait, the light, okay.", 0.0, 1.88),
    ("Why do brands do food marketing?", 2.52, 4.2),
    ("Because when you see a perfume,", 4.66, 6.36),
    ("you might not feel anything,", 6.36, 8.3),
)
# S3 home take: her face, chin included, on the 9:16 canvas.
_FACE = NormalizedBox(0.36, 0.28, 0.64, 0.48)
# A chin two points lower meets a 42 px "small" block centred at 0.5 (KRI-1326 shrank it from
# 52 px, which met the measured chin above).
_CHIN_ON_MIDDLE = NormalizedBox(0.36, 0.30, 0.64, 0.50)
_MIDDLE_ASK = {
    "preferences": {},
    "requested": {"caption": {"color": "#FFFFFF", "position": "middle"}},
    "selected_fonts": {"caption": CAPTION_FONT_FAMILY},
    "selection_calls": 2,
    "caption_ask": {"size": "small", "color": "#FFFFFF", "position": "middle"},
}


def _cues(lines=_LINES) -> list[dict]:
    cues = []
    for text, start, end in lines:
        words = text.split()
        step = (end - start) / len(words)
        cues.append(
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [
                    {
                        "text": w,
                        "start_s": round(start + i * step, 3),
                        "end_s": round(start + (i + 1) * step, 3),
                    }
                    for i, w in enumerate(words)
                ],
            }
        )
    return cues


def _choose(
    policy: dict | None,
    face: NormalizedBox,
    *,
    cues: list[dict] | None = None,
    caption_style: str = "sentence",
):
    cues = _cues() if cues is None else cues
    subject = CaptionSubject(
        start_s=0.0,
        end_s=float("inf"),
        objects=(),
        faces=(face,),
        face_windows=(),
        keep_segments=((0.0, 9.0),),
        display_width=1080.0,
        display_height=1920.0,
        scale=1.0,
        position_x=0.0,
    )
    kwargs = dict(
        caption_style=caption_style,
        canvas=CANVAS,
        objects=(),
        faces=(),
        keep_segments=((0.0, 9.0),),
        display_width=1080.0,
        display_height=1920.0,
        subjects=[subject],
    )
    if policy is None:
        return choose_caption_look(cues, **kwargs)
    with phone_text_policy_scope(policy):
        return choose_caption_look(cues, **kwargs)


def test_an_asked_middle_spot_moves_just_under_a_named_face_not_to_the_lower_third():
    look, receipt = _choose(_MIDDLE_ASK, _CHIN_ON_MIDDLE)

    y = receipt["position_y_frac"]
    assert receipt["evaluated"][0]["face_lines"] > 0, "the asked spot meets the chin"
    assert receipt["status"] == "moved"
    assert 0.5 <= y <= 0.64, f"middle row, just under the face (D1/D2); got {y}"
    assert look is not None and look.position_y_frac == y
    assert receipt["evaluated"][-1]["face_lines"] == 0


def test_a_small_block_clear_of_her_measured_chin_stays_on_the_asked_spot():
    _look, receipt = _choose(_MIDDLE_ASK, _FACE)

    assert receipt["position_y_frac"] == 0.5
    assert receipt["evaluated"][0]["face_lines"] == 0


def test_a_face_filling_the_middle_row_still_wins_over_the_spot():
    _look, receipt = _choose(_MIDDLE_ASK, NormalizedBox(0.25, 0.30, 0.75, 0.74))

    assert receipt["evaluated"][-1]["face_lines"] == 0, "never on her face (D3)"
    assert not caption_spot_holds("middle", receipt["position_y_frac"])


def test_no_asked_spot_keeps_todays_ladder():
    _look, receipt = _choose(None, NormalizedBox(0.30, 0.70, 0.70, 0.84))

    tried = [row["position_y_frac"] for row in receipt["evaluated"]]
    assert tried[1] == 0.86, f"default ladder unchanged (D4); tried {tried}"


def test_a_block_drawn_out_of_the_asked_row_is_not_called_middle():
    assert caption_spot_holds("middle", 0.53)
    assert not caption_spot_holds("middle", 0.86)
    assert caption_spot_holds("bottom", 0.86)
    assert caption_spot_holds(None, 0.86)
    reason = caption_spot_moved_reason("middle", 0.86)
    assert "lower" in reason and "face" in reason


def test_the_finished_check_says_partly_when_the_block_left_the_middle():
    from app.kria import brief_checks

    req = BriefRequirement(
        id="r8",
        kind="style",
        scope="global",
        description="Captions small, white, in the middle, never on my face",
        facts={
            "captions": "on",
            "caption_size": "small",
            "caption_color": "#FFFFFF",
            "caption_position": "middle",
            "keep_text_off": [{"kind": "face"}],
        },
    )
    receipt = brief_checks._caption_spot_drawn_receipt(
        req, {"position_y_frac": 0.86, "over_face": False}
    )
    assert receipt is not None and receipt.status == "partial"
    assert "lower than you asked" in receipt.reason
    assert brief_checks._caption_spot_drawn_receipt(req, {"position_y_frac": 0.53}) is None


# ------------------------------------------------ KRI-1476: Qendresa Q1 run 3 (job 3ef3cbe5)
#
# "Captions word by word in the middle of the screen, white, two words at a time" draws the
# two-word block under her face (y 0.62, `_apply_caption_shape`). A face ask on the same brief
# found that block on her chin; the KRI-1327 ladder stops at 0.62 (its last step), so it could
# only climb onto her face and then fell to 0.86 (kit rule: median 0.50-0.75 -> FAIL).
#
# F2  An asked middle drawn at 0.62 has no ladder step below it.
# F4  A face that fills the frame under the middle still pushes the block out, and says so.
# F5  The top and bottom rows (Q2's "near the bottom") keep today's ladder.
# F6  A creator who placed nothing keeps today's ladder (D4 above).
# F8  The finished check never says "in the middle" for a block outside the row.
# OFF The switch off is today's ladder, today's row and today's 0.86.

_SWITCH = "keep_text_off_caption_spot_enabled"
# variants[0].caption_cues of the job: her first two-word pieces.
_Q1_LINES = (
    ("Five projects", 0.12, 0.86),
    ("the AI", 0.86, 1.48),
    ("builder community", 1.48, 2.3),
    ("can't stop", 2.3, 2.9),
    ("talking about.", 2.9, 3.6),
)
# opening_title_placement.face_boxes of the job, united: chin at 0.6175 of the height.
_Q1_FACE = NormalizedBox(0.2067, 0.2113, 0.8452, 0.6175)
_Q1_ASK = {
    "preferences": {},
    "requested": {},
    "selected_fonts": {"caption": CAPTION_FONT_FAMILY},
    "selection_calls": 2,
    "caption_ask": {"color": "#FFFFFF", "words": 2, "position": "middle"},
}
_TODAYS_MIDDLE_LADDER = (0.62, 0.59, 0.56, 0.53, 0.5, 0.47, 0.44)


def _q1(face: NormalizedBox = _Q1_FACE):
    return _choose(_Q1_ASK, face, cues=_cues(_Q1_LINES), caption_style="word")


def test_a_two_word_middle_block_on_her_chin_steps_just_under_it_not_to_the_lower_third():
    look, receipt = _q1()

    tried = [row["position_y_frac"] for row in receipt["evaluated"]]
    y = receipt["position_y_frac"]
    assert tried[0] == 0.62 and receipt["evaluated"][0]["face_lines"] > 0
    assert receipt["status"] == "moved"
    assert 0.62 < y <= 0.68, f"just under her chin (F2); got {y}, tried {tried}"
    assert 0.86 not in tried
    assert look is not None and look.position_y_frac == y
    assert receipt["evaluated"][-1]["face_lines"] == 0
    assert caption_spot_holds("middle", y), "still reads as the middle (F8)"


def test_a_face_that_fills_the_frame_under_the_middle_still_pushes_the_block_out():
    _look, receipt = _q1(NormalizedBox(0.2067, 0.2113, 0.8452, 0.80))

    y = receipt["position_y_frac"]
    assert not caption_spot_holds("middle", y), f"never called the middle (F4/F8); got {y}"
    assert "face" in caption_spot_moved_reason("middle", y)


def test_the_top_and_bottom_rows_keep_todays_ladder():
    from app.kria.caption_look_ask import caption_spot_band
    from app.pipeline.phone_protected_text import _asked_spot_ladder

    assert caption_spot_band("bottom") == (0.70, 0.90)
    assert caption_spot_band("top") == (0.08, 0.30)
    assert _asked_spot_ladder("bottom", 0.79) == (0.79, 0.82, 0.76, 0.85, 0.73, 0.88, 0.7)
    assert _asked_spot_ladder("top_left", 0.12) == (0.11, 0.14, 0.08, 0.17, 0.2, 0.23, 0.26, 0.29)
    assert _asked_spot_ladder(None, 0.62) == ()


def test_the_middle_row_ends_under_her_chin_and_before_the_bottom_row():
    from app.kria import brief_checks

    assert caption_spot_holds("middle", 0.66) and caption_spot_holds("middle", 0.68)
    assert not caption_spot_holds("middle", 0.7)
    assert not caption_spot_holds("bottom", 0.68)
    req = BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description="Captions word by word in the middle of the screen, white, two words",
        facts={
            "captions": "word_by_word",
            "caption_color": "#FFFFFF",
            "caption_words": 2,
            "caption_position": "middle",
        },
    )
    assert brief_checks._caption_spot_drawn_receipt(req, {"position_y_frac": 0.66}) is None
    lower = brief_checks._caption_spot_drawn_receipt(req, {"position_y_frac": 0.86})
    assert lower is not None and lower.status == "partial"


def test_switch_off_the_middle_block_falls_to_the_lower_third_as_today(monkeypatch):
    from app.config import settings
    from app.kria.caption_look_ask import caption_spot_band
    from app.pipeline.phone_protected_text import _asked_spot_ladder

    monkeypatch.setattr(settings, _SWITCH, False)
    assert caption_spot_band("middle") == (0.44, 0.64)
    assert not caption_spot_holds("middle", 0.66)
    assert _asked_spot_ladder("middle", 0.62) == _TODAYS_MIDDLE_LADDER
    assert _asked_spot_ladder("middle", 0.5) == (0.5, 0.53, 0.47, 0.44, 0.56, 0.59, 0.62)
    _look, receipt = _q1()
    tried = [row["position_y_frac"] for row in receipt["evaluated"]]
    assert tried == [*_TODAYS_MIDDLE_LADDER, 0.86]
    assert (receipt["status"], receipt["position_y_frac"]) == ("moved", 0.86)
