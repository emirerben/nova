"""KRI-736: a Save never 422s because the editor sent ``role: null`` on a text.

The iOS editor encodes a text it created itself (the "+ Text" tool, a pasted text) with
an explicit JSON ``"role": null`` (``NativeEditorDocument.encodeText``). ``TextElement``
only accepted its three literals, so editor-commit's strict validation refused the whole
Save: ``Text element <uuid>: field role has invalid value None``. Server-side fix, so
already-installed builds work.

Drives the real ``prepare_authored_editor_commit`` path on a phone guided job.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement, coerce_text_elements
from tests.routes.test_phone_guided_clip_delete_text_fit import _phone_retimed, _save, _text


def _with_null_role(variant, windows, total):
    lane = _phone_retimed(variant, windows, total)
    new = _text(variant["text_elements"][1], "7631D9DC-FFAC-47DF-8955-E31216DDEA41", 1.0, 4.0)
    new["role"] = None  # exactly what encodeText sends for a text without a role
    lane.append(new)
    return lane


def test_a_save_with_a_new_text_whose_role_is_null_is_accepted(monkeypatch):
    saved, _recipe, _total = _save(monkeypatch, texts=_with_null_role)

    by_id = {t["id"]: t for t in saved}
    created = by_id["7631D9DC-FFAC-47DF-8955-E31216DDEA41"]
    # null means "no role" -> the same default an absent role gets, never a new value.
    assert created["role"] == "generative_intro"
    assert created["text"] == "7631D9DC-FFAC-47DF-8955-E31216DDEA41"


@pytest.mark.parametrize("missing", ["absent", "none"])
def test_null_and_absent_role_parse_to_the_same_element(missing):
    raw = {"id": "a", "text": "hi", "start_s": 0.0, "end_s": 2.0}
    if missing == "none":
        raw["role"] = None
    element = TextElement.model_validate(raw)
    assert element.role == "generative_intro"
    assert (
        element.model_dump()
        == TextElement.model_validate({k: v for k, v in raw.items() if k != "role"}).model_dump()
    )


def test_an_unknown_role_is_still_rejected():
    assert coerce_text_elements([{"id": "a", "text": "hi", "end_s": 2.0, "role": "bogus"}]) is None


@pytest.mark.parametrize("field", ["role", "position", "wrap_lines", "removed", "behind_subject"])
def test_a_null_on_a_defaulted_field_is_the_default(field):
    raw = {"id": "a", "text": "hi", "start_s": 0.0, "end_s": 2.0}
    absent = TextElement.model_validate(raw)
    assert TextElement.model_validate({**raw, field: None}).model_dump() == absent.model_dump()
