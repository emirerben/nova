"""The approval card names what the draft's text pass left out (KRI-1376).

Ways this could fail, written before the code:
  1. the draft fails instead of being offered with the part the text pass could not do;
  2. a card already holding its six lines drops the text-pass line instead of an older one;
  3. on a Turkish chat the text-pass line reads in English, beside phone notes in Turkish.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.edit_copilot import EditCopilotOutput
from app.models import CreationThread
from tests.tasks import test_edit_proposal_build as drafts

_EN = "I couldn't do every part of your text request, so the video shows the parts I could"
_TR = "Yazı isteğinin her kısmını yapamadım, videoda yapabildiğim kısımlar var"


def _draft(monkeypatch, phone_notes: list[str], *, chat_language: str | None = None):
    """One auto-approved phone draft whose text composer asks instead of editing."""
    make_item = drafts._prod_item

    def item_with_request(*args, **kwargs):
        item = make_item(*args, **kwargs)
        item.edit_proposal["brief"]["creator_request"] = "Make it without these"
        return item

    class _ChatDb(drafts._Db):
        def execute(self, query):  # noqa: ANN001
            entity = (getattr(query, "column_descriptions", None) or [{}])[0].get("entity")
            if entity is CreationThread and chat_language is not None:
                state = {"reply_language": chat_language}
                return drafts._Result(rows=[SimpleNamespace(state=state)])
            return super().execute(query)

    monkeypatch.setattr(drafts, "_prod_item", item_with_request)
    monkeypatch.setattr(drafts, "_Db", _ChatDb)
    monkeypatch.setattr(
        "app.services.creation_text_composition.CreationTextComposer.run",
        lambda *_a, **_kw: EditCopilotOutput(
            intent="clarify",
            confidence=0.4,
            reply="Which text should go?",
            needs_clarification=True,
        ),
    )
    return drafts._phone_draft(monkeypatch, lambda *_a: phone_notes, phone=True)


@pytest.mark.parametrize("earlier", [0, 6], ids=["empty_card", "full_card"])
def test_a_text_ask_the_composer_could_not_finish_is_named_on_the_approval_card(
    monkeypatch, earlier
) -> None:
    phone_notes = [f"Phone repair {n}" for n in range(earlier)]

    persisted = _draft(monkeypatch, phone_notes)

    assert persisted.status == "approved"
    adjustments = persisted.last_approved.snapshot.adjustments
    assert adjustments == [*phone_notes[:5], _EN] if earlier else [_EN]


def test_the_text_pass_line_reads_in_the_chats_language(monkeypatch) -> None:
    persisted = _draft(monkeypatch, [], chat_language="tr")

    assert persisted.last_approved.snapshot.adjustments == [_TR]
