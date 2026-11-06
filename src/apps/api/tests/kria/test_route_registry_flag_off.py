"""ROUTE_REGISTRY_ENABLED=false is the card before the route registry, and stays it.

The golden (`tests/fixtures/kria_replay/assos/registry_off_golden.json`) was recorded on origin/main
ff5f2adbe, before the registry existed, for the Assos draft as recorded and for the same draft with
every legacy field set plus three typed asks. With the switch off the draft must say exactly that,
whatever later lanes add to the unified montage table or reword in the shared sentences.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[5]
GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "kria_replay"
    / "assos"
    / "registry_off_golden.json"
)


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_assos", REPO / "scripts" / "kria" / "score_assos.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drafts():
    card = _scorecard()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    patcher = pytest.MonkeyPatch()

    def blocked(*_a, **_k):
        raise card._BASE.OfflineViolation("network access is blocked in the flag-off replay")

    patcher.setattr(socket.socket, "connect", blocked)
    patcher.setattr(socket, "create_connection", blocked)
    try:
        fx = card.load_fixture()
        rich_brief = copy.deepcopy(fx["brief"])
        rich_brief["requirements"] += golden["extra_asks"]
        with card._prod_flags():
            job = card.build_job(fx)
            yield (
                golden,
                {
                    "assos": (job.strategy, fx["brief"], job.media),
                    "assos_every_field": (
                        {**job.strategy, **golden["every_field"]},
                        rich_brief,
                        job.media,
                    ),
                },
            )
    finally:
        patcher.undo()


def _card(strategy, brief_json, media) -> dict:
    from app.kria import route_disclosure as rd
    from app.kria.brief import CreativeBrief

    live = CreativeBrief.model_validate(brief_json).live()
    route = rd.UNIFIED_MONTAGE
    return {
        "strategy_notices": [
            d.as_record()
            for d in rd.strategy_notices(strategy, route=route, requirements=live, media=media)
        ],
        "ask_declines": [d.as_record() for d in rd.ask_declines(live, strategy, route=route)],
        "card_text": rd.card_text(rd.card_disclosures(live, strategy, route=route, media=media)),
    }


@pytest.mark.parametrize("case", ["assos", "assos_every_field"])
def test_registry_off_says_what_origin_main_said(drafts, case) -> None:
    from app.config import settings

    golden, inputs = drafts
    with patch.object(settings, "route_registry_enabled", False):
        assert _card(*inputs[case]) == golden["cases"][case]


def test_registry_off_ignores_what_a_lane_adds_to_the_unified_table(drafts) -> None:
    from app.config import settings
    from app.kria.route_capabilities import NotApplied, unified_montage

    golden, inputs = drafts
    louder = {
        field: NotApplied(lambda _v, _s, field=field: f"I can't apply the {field} yet")
        for field in unified_montage.FIELD_HANDLING
    }
    with (
        patch.object(settings, "route_registry_enabled", False),
        patch.object(unified_montage, "FIELD_HANDLING", louder),
        patch.object(unified_montage, "ASK_DECLINES", ()),
    ):
        assert _card(*inputs["assos_every_field"]) == golden["cases"]["assos_every_field"]
