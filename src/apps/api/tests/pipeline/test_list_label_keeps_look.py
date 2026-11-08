"""KRI-1284: a list label keeps its own look and spot through the text-style restyle.

The restyle gave every `label`-role row the account default (grey auto fill, thin outline) and
slid it under the captions; the list label's own placement already avoids the face and captions.
"""

from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_list_labels import (
    LABEL_COLOR,
    LABEL_STROKE_WIDTH,
    ListLabelAsk,
    _label_element,
)
from app.services.phone_text_styling import PHONE_TEXT_STYLE_VERSION, phone_text_policy_scope
from app.tasks.generative_build import _protected_style_rows


def test_restyle_keeps_list_label_look_and_spot():
    ask = ListLabelAsk(requirement_id="r2", text="2. Impeccable", item="Impeccable")
    row = _label_element(ask, start_s=1.0, end_s=4.0, canvas=PORTRAIT)
    policy = {
        "version": PHONE_TEXT_STYLE_VERSION,
        "preferences": {},
        "requested": {},
        "selection_roles": ["label"],
    }
    with phone_text_policy_scope(policy):
        styled = _protected_style_rows([row], job_id="j", protected=None, canvas=PORTRAIT)[0]
    assert styled["color"] == LABEL_COLOR
    assert styled["stroke_width"] == LABEL_STROKE_WIDTH
    assert styled["y_frac"] == row["y_frac"]
    assert styled["size_px"] == row["size_px"]


def test_list_label_is_bigger_than_captions():
    from app.pipeline.phone_captions import _CAPTION_TEXT_SIZE_PX

    for text in (
        "2. Impeccable",
        "4. A fairly long project name here",
        "5. Superpowers for agents",
    ):
        ask = ListLabelAsk(requirement_id="r", text=text, item=text)
        row = _label_element(ask, start_s=1.0, end_s=4.0, canvas=PORTRAIT)
        assert row["size_px"] > _CAPTION_TEXT_SIZE_PX, text
