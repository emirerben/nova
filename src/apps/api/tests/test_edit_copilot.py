from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "copilot-ops"


def _snapshot(*, allowed=None) -> dict:
    return {
        "has_narrated_captions": False,
        "allowed_op_families": allowed if allowed is not None else ["text", "style", "timeline"],
        "text_bars": [
            {
                "text": "old hook",
                "start_s": 0.0,
                "end_s": 3.0,
                "font_family": "Inter",
                "size_px": 64,
                "color": "#FFFFFF",
            },
            {"text": "second", "start_s": 3.0, "end_s": 5.0, "size_px": 44},
        ],
        "slots": [
            {"output_start_s": 0.0, "output_end_s": 3.0, "duration_s": 3.0},
            {"output_start_s": 3.0, "output_end_s": 7.0, "duration_s": 4.0},
            {"output_start_s": 7.0, "output_end_s": 10.0, "duration_s": 3.0},
        ],
        "total_duration_s": 10.0,
    }


def _agent() -> EditCopilotAgent:
    return EditCopilotAgent(ModelClient())


def _parse(raw_ops: list[dict], *, confidence: float = 0.9, allowed=None):
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": raw_ops,
            "confidence": confidence,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    return _agent().parse(
        raw,
        EditCopilotInput(
            utterance="change it",
            prior_turns=[],
            variant_snapshot=_snapshot(allowed=allowed),
        ),
    )


def test_copilot_valid_op_fixtures_parse() -> None:
    data = json.loads((FIXTURE_DIR / "valid.json").read_text())
    for case in data["cases"]:
        out = _parse([case["op"]])
        assert len(out.ops) == 1, case["name"]


def test_copilot_invalid_op_fixtures_drop() -> None:
    data = json.loads((FIXTURE_DIR / "invalid.json").read_text())
    for case in data["cases"]:
        out = _parse([case["op"]])
        assert out.ops == [], case["name"]


def test_copilot_unknown_op_dropped() -> None:
    out = _parse([{"op": "restyle_all", "preset": "x"}])
    assert out.ops == []
    assert out.confidence == 0.9


def test_copilot_bad_font_drops_and_caps_confidence() -> None:
    out = _parse([{"op": "patch_text_style", "bar_index": 0, "patch": {"font_family": "Papyrus"}}])
    assert out.ops == []
    assert out.confidence == 0.4
    assert out.needs_clarification


def test_copilot_required_field_drop() -> None:
    out = _parse([{"op": "edit_text", "bar_index": 0}])
    assert out.ops == []


def test_copilot_eight_op_cap() -> None:
    ops = [{"op": "remove_text", "bar_index": 0} for _ in range(10)]
    out = _parse(ops)
    assert len(out.ops) == 8


def test_copilot_capability_family_drop() -> None:
    out = _parse(
        [
            {"op": "edit_text", "bar_index": 0, "text": "new"},
            {"op": "set_clip_duration", "slot_index": 0, "duration_s": 2.0},
        ],
        allowed=["text"],
    )
    assert [op["op"] for op in out.ops] == ["edit_text"]


def test_copilot_clip_duration_seconds_only() -> None:
    out = _parse([{"op": "set_clip_duration", "slot_index": 0, "duration_beats": 4}])
    assert out.ops == []
    out2 = _parse([{"op": "set_clip_duration", "slot_index": 0, "duration_s": 0.2}])
    assert out2.ops == [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 0.6}]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()
    settings.edit_copilot_enabled = False


def _user(user_id: uuid.UUID | None = None) -> MagicMock:
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    return user


def _result(value) -> MagicMock:  # noqa: ANN001
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _item_and_plan(user_id: uuid.UUID, *, owner_id: uuid.UUID | None = None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = "variants_ready"
    job.assembly_plan = {"variants": [{"variant_id": "v1", "render_status": "ready"}]}
    item.current_job = job
    plan = MagicMock()
    plan.user_id = owner_id or user_id
    return item, plan


def _install_route_deps(user, item, plan) -> AsyncMock:  # noqa: ANN001
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(item))
    db.get = AsyncMock(return_value=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return db


def _payload() -> dict:
    return {"message": "make it smaller", "turns": [], "snapshot": _snapshot()}


def test_copilot_route_flag_off_404(client: TestClient) -> None:
    user = _user()
    item, plan = _item_and_plan(user.id)
    _install_route_deps(user, item, plan)

    resp = client.post(f"/plan-items/{item.id}/variants/v1/copilot/turn", json=_payload())
    assert resp.status_code == 404


def test_copilot_route_foreign_item_404(client: TestClient) -> None:
    settings.edit_copilot_enabled = True
    user = _user()
    item, plan = _item_and_plan(user.id, owner_id=uuid.uuid4())
    _install_route_deps(user, item, plan)

    resp = client.post(f"/plan-items/{item.id}/variants/v1/copilot/turn", json=_payload())
    assert resp.status_code == 404


def test_copilot_route_oversized_snapshot_422(client: TestClient) -> None:
    settings.edit_copilot_enabled = True
    user = _user()
    item, plan = _item_and_plan(user.id)
    _install_route_deps(user, item, plan)

    body = _payload()
    body["snapshot"] = {"text_bars": [{"text": "x" * (21 * 1024)}], "slots": []}
    resp = client.post(f"/plan-items/{item.id}/variants/v1/copilot/turn", json=body)
    assert resp.status_code == 422


def test_copilot_route_clarification_empties_ops(client: TestClient, monkeypatch) -> None:
    settings.edit_copilot_enabled = True
    user = _user()
    item, plan = _item_and_plan(user.id)
    _install_route_deps(user, item, plan)

    from app.routes import _copilot as copilot_route

    class _FakeAgent:
        def __init__(self, client) -> None:  # noqa: ANN001
            pass

        def run(self, inp, *, ctx=None):  # noqa: ANN001
            from app.agents.edit_copilot import EditCopilotOutput

            return EditCopilotOutput(
                intent="clarify",
                ops=[{"op": "remove_text", "bar_index": 0}],
                confidence=0.4,
                reply="Which text?",
                suggestions=["First text"],
                needs_clarification=True,
            )

    monkeypatch.setattr(copilot_route, "EditCopilotAgent", _FakeAgent)
    resp = client.post(f"/plan-items/{item.id}/variants/v1/copilot/turn", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["ops"] == []
    assert resp.json()["needs_clarification"] is True


def test_copilot_route_rate_limit_429(client: TestClient, monkeypatch) -> None:
    settings.edit_copilot_enabled = True
    user = _user()
    item, plan = _item_and_plan(user.id)
    _install_route_deps(user, item, plan)

    from app.routes import _copilot as copilot_route

    class _FakeAgent:
        def __init__(self, client) -> None:  # noqa: ANN001
            pass

        def run(self, inp, *, ctx=None):  # noqa: ANN001
            from app.agents.edit_copilot import EditCopilotOutput

            return EditCopilotOutput(
                intent="edit",
                ops=[],
                confidence=0.9,
                reply="Done.",
                suggestions=[],
                needs_clarification=False,
            )

    monkeypatch.setattr(copilot_route, "EditCopilotAgent", _FakeAgent)
    url = f"/plan-items/{item.id}/variants/v1/copilot/turn"
    headers = {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 200 + 1}"}
    statuses = [client.post(url, json=_payload(), headers=headers).status_code for _ in range(21)]
    assert statuses[-1] == 429
