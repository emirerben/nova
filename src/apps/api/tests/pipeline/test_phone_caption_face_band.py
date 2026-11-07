"""KRI-632 E2E: a long Turkish Talking take through the real caption path.

Cue builder (`build_plain_cues` with `PHONE_CUE_BUDGET`) -> the face-band chooser the
Talking worker runs (`_phone_face_band_caption_look`, with the real `_face_boxes`
geometry; only the face detector's answer is replayed) -> the phone compiler
(`compile_caption_layers`). The sentence is cue 10 of Tigre T1 (2026-10-12, thread
0DD3FF4E): 16 words, 84 characters before the last two words. On prod it wrapped to
4 rows over the speaker's chest and chin.

Ways this can fail, written before the code:
- the watermark re-wrap adds a third row (the pre-fix behaviour, 5 of 102 budget-sized
  Turkish cues at the default look);
- a cue split in time loses or reorders a word, leaves a gap or an overlap, or leaves
  a piece still on the mark;
- a split leaves a 1-2 word orphan where a 3-word piece was possible;
- a caption block crosses the speaker's face band, or the chooser moves captions when
  the band is clear / the detector failed;
- the face band moves a caption spot the creator placed (an asked position on the brief,
  a saved caption position, a position named this turn): the creator's spot wins even
  over a face, and only unplaced captions move;
- the kill switch (PHONE_CAPTION_ROW_BUDGET_ENABLED) off still changes anything;
- a 4-5 word cue is split into 1-2 word pieces, or a two-words-at-a-time cue into 1+1.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.captions import PHONE_CUE_BUDGET, Word, build_plain_cues
from app.pipeline.phone_captions import (
    CAPTION_FONT_FAMILY,
    caption_ink_box,
    compile_caption_layers,
    watermark_keepout_rect,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_text_styling import phone_text_policy_scope
from app.tasks import generative_build
from app.tasks.generative_build import _phone_face_band_caption_look
from tests.pipeline.test_phone_captions import _ink_pixels_under_the_mark

T1_CUE_10 = (
    "Ben her videoda, işte abone olun, like atın falan diyeceksem bu işi zaten yapmayalım abi biz."
)
T2_CUES = [
    "İşte hiçbir şey yapmadan kurucu ekipteyse bize de bir şeyler yapsalar ne olur düşünün.",
    "Sen bu dünyadaki her ülkede, her şehirde, her köyde en güzel odalarda kalmaya layık",
    "Kendisi bu arada bir çekingen takılıyordu ama yaklaşık tam 20-25 dakika önce bir kano",
]
# The T1 job's own sample (closing_title_placement.face_band): top 0.5175, bottom 0.736.
T1_FACE_BAND = (0.5175, 0.736)
_CANVAS = SimpleNamespace(width=1080, height=1920)
_BINDING = SimpleNamespace(
    media_id="speaker",
    original=SimpleNamespace(width=1080, height=1920, orientation_degrees=0),
)


def _words(sentence: str, start_s: float = 14.28, per_word_s: float = 0.28) -> list[Word]:
    return [
        Word(
            text=token,
            start_s=start_s + k * per_word_s,
            end_s=start_s + (k + 1) * per_word_s - 0.03,
            confidence=1.0,
        )
        for k, token in enumerate(sentence.split())
    ]


def _sampler(top: float | None, bottom: float | None = None, *, fail: bool = False):
    def sample(_clip, anchors, **_kwargs):
        if fail:
            raise RuntimeError("detector unavailable")
        regions = (
            []
            if top is None
            else [ProtectedRegion(0.0, 99.0, NormalizedBox(0.3, top, 0.7, bottom), "face")]
        )
        return regions, {"decoded": len(anchors)}

    return sample


def _face_band_look(monkeypatch, cues, sampler):
    monkeypatch.setattr(title_mod, "sample_face_regions", sampler)
    return _phone_face_band_caption_look(
        binding=_BINDING,
        clip_path="speaker.mp4",
        keep_segments=[(0.0, 40.0)],
        landscape_fit="fill",
        position_x=0.0,
        cues=cues,
        caption_style="word",
        canvas=_CANVAS,
    )


def _rows(layer) -> list[str]:
    lines: dict[float, list[str]] = {}
    for run in layer.runs:
        lines.setdefault(run.baseline_y, []).append(run.text)
    return [" ".join(lines[baseline]) for baseline in sorted(lines)]


def _ink(layer) -> tuple[float, float, float, float]:
    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    boxes = [caption_ink_box(run, typeface) for run in layer.runs]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _crosses(layer, band: tuple[float, float]) -> bool:
    _left, top, _right, bottom = _ink(layer)
    return top < band[1] * 1920 and bottom > band[0] * 1920


def _compile(cues, look=None):
    return compile_caption_layers(
        cues, canvas_width=1080, canvas_height=1920, style="word", look=look
    )


def test_t1_cue_10_is_short_unorphaned_and_clear_of_the_face(monkeypatch):
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    look, receipt = _face_band_look(monkeypatch, cues, _sampler(0.5175, 0.736))
    assert receipt is not None and receipt["faces"] == "detected"
    layers = _compile(cues, look)

    assert " ".join(" ".join(_rows(layer)) for layer in layers) == T1_CUE_10
    for layer in layers:
        rows = _rows(layer)
        assert len(rows) <= 2, rows
        assert len(" ".join(rows).split()) >= 3, rows  # no orphan cue
        assert not _crosses(layer, T1_FACE_BAND), rows
    # Contiguous in time: no flicker between the pieces of one sentence.
    assert all(a.end == b.start for a, b in zip(layers, layers[1:], strict=False))
    # One steady band: every cue sits at the same height.
    assert len({round(layer.anchor_y, 3) for layer in layers}) == 1


@pytest.mark.parametrize("sentence", T2_CUES)
def test_t2_long_cues_stay_two_rows_clear_of_the_mark(monkeypatch, sentence):
    cues = build_plain_cues(_words(sentence), attach_words=True, budget=PHONE_CUE_BUDGET)
    layers = _compile(cues, _face_band_look(monkeypatch, cues, _sampler(0.5175, 0.736))[0])
    assert " ".join(" ".join(_rows(layer)) for layer in layers) == sentence
    assert all(len(_rows(layer)) <= 2 for layer in layers), [_rows(layer) for layer in layers]
    assert _ink_pixels_under_the_mark(layers) == 0


def test_a_budget_sized_cue_that_met_the_mark_is_split_in_time_not_grown(monkeypatch):
    """Pre-fix, the mark re-wrap grew these 33-40 character cues to 3 rows."""
    texts = [
        "ben günde 4 kahve içiyorum, o yüzden bu",
        "hiçbir şey yapmadan kurucu ekipteyse",
        "yaklaşık tam 20-25 dakika önce bir kano",
        "ülkede, her şehirde, her köyde en güzel",
    ]
    cues = [
        {"text": text, "start_s": 4.0 * k, "end_s": 4.0 * k + 2.4} for k, text in enumerate(texts)
    ]
    layers = _compile(cues)
    assert all(len(_rows(layer)) <= 2 for layer in layers)
    assert _ink_pixels_under_the_mark(layers) == 0
    for k, text in enumerate(texts):
        pieces = [layer for layer in layers if 4.0 * k <= layer.start < 4.0 * k + 2.4]
        assert len(pieces) == 2, text
        assert " ".join(" ".join(_rows(piece)) for piece in pieces) == text
        assert (pieces[0].start, pieces[1].end) == (4.0 * k, 4.0 * k + 2.4)
        assert pieces[0].end == pieces[1].start


def test_a_low_face_moves_the_caption_band_off_it_and_a_clear_face_leaves_it(monkeypatch):
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)

    # Negative control: a face sitting where the default band is, captions cross it.
    low_band = (0.70, 0.86)
    assert any(_crosses(layer, low_band) for layer in _compile(cues))
    look, receipt = _face_band_look(monkeypatch, cues, _sampler(*low_band))
    assert receipt["status"] == "moved" and receipt["face_collision"] is False
    assert not any(_crosses(layer, low_band) for layer in _compile(cues, look))

    # The T1 band is already clear: nothing moves.
    look, receipt = _face_band_look(monkeypatch, cues, _sampler(0.5175, 0.736))
    assert look is None and receipt["status"] == "preset"


@pytest.mark.parametrize(
    "sampler", [_sampler(None), _sampler(0.7, 0.86, fail=True)], ids=["no-face", "detector-down"]
)
def test_no_face_or_a_failed_detector_never_moves_the_captions(monkeypatch, sampler):
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    look, _receipt = _face_band_look(monkeypatch, cues, sampler)
    assert look is None


def test_the_split_pieces_keep_every_word_time():
    words = _words(T1_CUE_10)
    cue = {
        "text": " ".join(w.text for w in words[:6]),
        "start_s": words[0].start_s,
        "end_s": words[5].end_s,
        "words": [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in words[:6]],
    }
    layers = _compile([cue])
    karaoke = [t for layer in layers for t in layer.karaoke.starts]
    assert len(karaoke) == 6
    # Absolute word starts survive the split (layer start + the layer-relative offset).
    absolute = [layer.start + start for layer in layers for start in layer.karaoke.starts]
    for got, word in zip(absolute, words[:6], strict=True):
        assert got == pytest.approx(word.start_s, abs=0.002)


def test_the_keepout_is_the_one_the_engine_draws():
    # Guards the assumption behind "clear of the mark": the keep-out rect used above.
    left, top, right, bottom = watermark_keepout_rect(1080, 1920)
    assert (left, right) == (48.0, 205.0) and top < bottom


def _policy(**fields):
    return {
        "preferences": {},
        "requested": {},
        "selected_fonts": {"caption": CAPTION_FONT_FAMILY},
        "selection_calls": 2,
        **fields,
    }


def _band_over(position: str) -> tuple[float, float]:
    """A face band over the block the asked position draws (the look's own y +/- 0.1)."""
    from app.services.phone_text_styling import generation_caption_look

    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    with phone_text_policy_scope(_policy(caption_ask={"position": position})):
        y = generation_caption_look(cues, canvas=_CANVAS).position_y_frac
    return (max(0.0, y - 0.1), min(1.0, y + 0.1))


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_a_creator_placed_spot_is_kept_even_over_a_face(monkeypatch, position):
    """KRI-1177 / KRI-129: an asked caption spot wins over a face; nothing moves it silently."""
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    band = _band_over(position)
    with phone_text_policy_scope(_policy(caption_ask={"position": position})):
        look, receipt = _face_band_look(monkeypatch, cues, _sampler(*band))
    assert look is None
    assert receipt == {"status": "creator_spot_kept"}


@pytest.mark.parametrize(
    "policy",
    [
        _policy(preferences={"caption": {"position": "top"}}),
        _policy(preferences={"all_text": {"position": "middle"}}),
        _policy(requested={"caption": {"position": "bottom"}}),
    ],
    ids=["saved-caption-spot", "saved-all-text-spot", "named-this-turn"],
)
def test_a_saved_or_named_caption_spot_is_kept_too(monkeypatch, policy):
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    with phone_text_policy_scope(policy):
        look, receipt = _face_band_look(monkeypatch, cues, _sampler(0.0, 1.0))
    assert look is None and receipt == {"status": "creator_spot_kept"}


@pytest.mark.parametrize(
    "policy",
    [None, _policy(), _policy(caption_ask={"color": "#FFFF00", "size": "small"})],
    ids=["no-policy", "no-ask", "ask-without-a-spot"],
)
def test_captions_nobody_placed_still_move_off_a_face(monkeypatch, policy):
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    from app.services.phone_text_styling import generation_caption_look

    with phone_text_policy_scope(policy):
        drawn_y = generation_caption_look(cues, canvas=_CANVAS)
        drawn_y = 0.78 if drawn_y is None else drawn_y.position_y_frac
        band = (drawn_y - 0.1, drawn_y + 0.1)
        look, receipt = _face_band_look(monkeypatch, cues, _sampler(*band))
    assert look is not None and receipt["status"] == "moved"
    assert look.position_y_frac != pytest.approx(drawn_y)


def test_the_protected_path_only_limits_to_faces_for_captions_nobody_placed(monkeypatch):
    """`_phone_protected_plan` newly limits captions to the face band (KRI-632); a creator's
    asked spot keeps the old rule: faces are a limit only when the creator named a face."""
    from app.pipeline import phone_protected_text as protected_mod

    seen: list[list] = []

    def fake_choose(_cues, **kwargs):
        seen.append(list(kwargs["faces"]))
        return None, {"status": "preset"}

    plan = protected_mod.ProtectedPlan(targets=(), objects=(object(),), protect_all_faces=False)
    monkeypatch.setattr(protected_mod, "resolve_protected_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(protected_mod, "choose_caption_look", fake_choose)
    monkeypatch.setattr(title_mod, "sample_face_regions", _sampler(0.5175, 0.736))
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)

    def run(policy):
        with phone_text_policy_scope(policy):
            generative_build._phone_protected_plan(
                {"creator_brief_binding": {}},
                binding=_BINDING,
                clip_path="speaker.mp4",
                keep_segments=[(0.0, 40.0)],
                landscape_fit="fill",
                position_x=0.0,
                cues=cues,
                caption_style="word",
                canvas=_CANVAS,
            )

    run(_policy())
    run(_policy(caption_ask={"position": "bottom"}))
    assert [bool(faces) for faces in seen] == [True, False]


def test_the_kill_switch_off_changes_nothing(monkeypatch):
    """PHONE_CAPTION_ROW_BUDGET_ENABLED=false: no face-band pass, and the compiler is the
    one that shipped before KRI-632 (a cue the mark meets gains rows, it is never split)."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_caption_row_budget_enabled", False)
    cues = build_plain_cues(_words(T1_CUE_10), attach_words=True, budget=PHONE_CUE_BUDGET)
    assert _face_band_look(monkeypatch, cues, _sampler(0.70, 0.86)) == (None, None)
    layers = _compile(cues)
    assert len(layers) == len(cues)  # one layer per cue, ids caption-0..n-1
    assert [layer.id for layer in layers] == [f"caption-{k}" for k in range(len(cues))]


def test_a_cue_is_never_split_into_a_one_word_piece_nor_a_short_orphan_when_it_can_merge():
    """The ticket's rule: no cue under 3 words when it can merge. A cue of 6+ words splits
    into pieces of 3+; a 4-5 word cue (a third row was the filed bug) into 2+; fewer words
    stay whole."""
    from app.pipeline import phone_captions

    def pieces(text):
        cue = {"text": text, "start_s": 0.0, "end_s": 3.0}
        pair = phone_captions._split_cue_in_time(cue)
        return None if pair is None else [len(p["text"].split()) for p in pair]

    assert pieces("iki kelime") is None
    assert pieces("üç kelime burada") is None
    assert pieces("dört kelime burada yazıyor") == [2, 2]
    assert pieces("beş kelime burada yazıyor işte") in ([2, 3], [3, 2])
    assert pieces("bir iki üç dört beş altı") == [3, 3]
    for count in range(6, 16):
        sizes = pieces(" ".join(f"k{n}" for n in range(count)))
        assert sizes is not None and min(sizes) >= 3, (count, sizes)

    texts = ["ben günde 4 kahve içiyorum, o yüzden bu", "hiçbir şey yapmadan kurucu ekipteyse"]
    layers = _compile(
        [{"text": t, "start_s": 4.0 * k, "end_s": 4.0 * k + 2.4} for k, t in enumerate(texts)]
    )
    assert all(len(" ".join(_rows(layer)).split()) >= 2 for layer in layers)
    assert all(len(_rows(layer)) <= 2 for layer in layers)


def test_a_two_word_cue_the_mark_meets_stays_whole():
    """ "Two words at a time" cues are not cut into single words."""
    cues = [{"text": "kahve içiyorum", "start_s": 0.0, "end_s": 2.0}]
    assert len(_compile(cues)) == 1
