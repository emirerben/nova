"""Unit tests for persona_build task — locks the footage_type_bias preservation.

generate_persona overwrites row.persona with persona.to_dict() (the generated
Persona schema output). Because footage_type_bias is an onboarding preference
stored in the same JSONB column but NOT part of the Persona schema, the
overwrite would silently wipe it without the preservation block.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch


def _make_session_ctx(row: MagicMock) -> MagicMock:
    """Context manager that yields a session whose get() returns the row for
    Persona lookups and a dummy User for User lookups."""
    user = MagicMock()
    user.onboarding_status = "pending"

    session = MagicMock()

    def _get(model_cls, _pk):
        from app.models import Persona, User

        if model_cls is Persona:
            return row
        if model_cls is User:
            return user
        return None

    session.get = MagicMock(side_effect=_get)
    session.commit = MagicMock()

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _generated_persona_dict() -> dict:
    """Schema-valid generated Persona fields — no footage_type_bias."""
    return {
        "summary": "Bold creator focused on morning routines",
        "content_pillars": ["fitness"],
        "tone": "direct",
        "audience": "gym-goers",
        "posting_cadence": "4/week",
        "sample_topics": [],
        "rationale": "",
        "goal": "",
        "content_mode": None,
        "current_situation": "",
    }


def test_generate_persona_preserves_footage_type_bias() -> None:
    """footage_type_bias set by 'What you make' step must survive generate_persona's
    persona.to_dict() overwrite — it is NOT in the Persona schema but lives in the
    same JSONB column, so explicit preservation is required.

    Locks persona_build.py lines 185-188: the preservation block.
    """
    from app.tasks.persona_build import generate_persona

    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = uuid.uuid4()
    row.persona = {"footage_type_bias": ["vlogs", "talking-head"]}
    row.persona_status = "generating"
    row.questionnaire = {}  # empty questionnaire is fine for PersonaQuestionnaire(**{})
    row.tiktok_profile = None

    generated_persona = MagicMock()
    generated_persona.to_dict.return_value = _generated_persona_dict()

    ctx = _make_session_ctx(row)

    with (
        patch("app.tasks.persona_build.sync_session", return_value=ctx),
        patch("app.tasks.persona_build.PersonaGeneratorAgent") as MockAgent,
        patch("app.tasks.persona_build.default_client"),
        patch("app.tasks.persona_build.settings") as mock_settings,
    ):
        mock_settings.user_style_enabled = False
        MockAgent.return_value.run.return_value = generated_persona

        # Celery bound task — call directly (Task.__call__ handles self binding)
        generate_persona(str(row.id))

    # footage_type_bias must be in the final written persona dict
    written = row.persona
    assert isinstance(written, dict), "row.persona was never written"
    assert "footage_type_bias" in written, (
        "footage_type_bias was wiped by generate_persona overwrite — "
        "preservation block at persona_build.py:185-188 not working"
    )
    assert written["footage_type_bias"] == ["vlogs", "talking-head"]
    # Sanity: generated content is also present
    assert written["summary"] == "Bold creator focused on morning routines"


def test_generate_persona_no_footage_bias_injected_when_absent() -> None:
    """When footage_type_bias was never set, the generated persona must NOT
    get a spurious key injected — only preserve when previously present."""
    from app.tasks.persona_build import generate_persona

    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = uuid.uuid4()
    row.persona = {"summary": ""}  # no footage_type_bias
    row.persona_status = "generating"
    row.questionnaire = {}
    row.tiktok_profile = None

    generated_persona = MagicMock()
    generated_persona.to_dict.return_value = _generated_persona_dict()

    ctx = _make_session_ctx(row)

    with (
        patch("app.tasks.persona_build.sync_session", return_value=ctx),
        patch("app.tasks.persona_build.PersonaGeneratorAgent") as MockAgent,
        patch("app.tasks.persona_build.default_client"),
        patch("app.tasks.persona_build.settings") as mock_settings,
    ):
        mock_settings.user_style_enabled = False
        MockAgent.return_value.run.return_value = generated_persona

        generate_persona(str(row.id))

    written = row.persona
    assert isinstance(written, dict)
    # Key must NOT be injected when it wasn't in the prior persona
    assert "footage_type_bias" not in written
