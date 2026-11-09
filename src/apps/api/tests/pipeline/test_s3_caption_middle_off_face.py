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


def _cues() -> list[dict]:
    cues = []
    for text, start, end in _LINES:
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


def _choose(policy: dict | None, face: NormalizedBox):
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
        caption_style="sentence",
        canvas=CANVAS,
        objects=(),
        faces=(),
        keep_segments=((0.0, 9.0),),
        display_width=1080.0,
        display_height=1920.0,
        subjects=[subject],
    )
    if policy is None:
        return choose_caption_look(_cues(), **kwargs)
    with phone_text_policy_scope(policy):
        return choose_caption_look(_cues(), **kwargs)


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
