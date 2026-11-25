"""KRI-1633: the iOS fixture is what the server sends.

`src/apps/ios/Tests/Fixtures/KRI1633IntroStyles.json` is GENERATED here from the real offer
builder (never hand-edited) and read by the iOS unit and UI tests (`IntroStylesOfferTests`,
`IntroStylesUITests`), so the app is tested against the exact wire shape: the editor variant with
its capability, three offers (Talking EN, montage TR, narrated EN), an unavailable offer and the
editor-commit body a pick must produce.

Drift check, the way `test_kri1582_design_picker.py` does it: the stored file's SHAPE (keys, ids,
option keys, row ids, layer texts, fonts, reasons) must equal a fresh build's; pixel positions
and font metrics may differ by platform, so they are not compared. Regenerate with
`KRI1633_REGENERATE=1 pytest tests/pipeline/test_intro_styles_ios_fixture.py`.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

import pytest

from app.cli import intro_styles_fixtures as fx
from app.config import settings
from app.pipeline import intro_styles as st
from app.routes import generative_jobs as gj
from app.services.client_protocol import INTRO_STYLES_PROTOCOL, _client_protocol

# A fixed job id keeps the seeded looks (and so the fixture) the same on every run.
_JOB_ID = uuid.UUID("00000000-0000-4000-8000-000000001633")
_FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI1633IntroStyles.json"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    for key, value in fx.flag_overrides().items():
        monkeypatch.setattr(settings, key, value)


def _offer(route: str, title: str) -> tuple[dict, dict]:
    job, vid, rows = fx.BUILDERS[route](title, job_id=_JOB_ID)
    return st.build_offer(st.detach(job), vid, rows, "first"), {"job": job, "rows": rows}


def _fresh() -> dict:
    offer_en, talking = _offer("talking", fx.EN_TITLE)
    job = talking["job"]
    token = _client_protocol.set(INTRO_STYLES_PROTOCOL)
    try:
        variant = next(v for v in gj._variants_for_response(job) if v["variant_id"] == "subtitled")
    finally:
        _client_protocol.reset(token)
    offer_tr, _ = _offer("montage", fx.TR_TITLE)
    offer_narrated, _ = _offer("narrated", fx.EN_TITLE)
    pick = offer_en["options"][1]
    other_rows = [
        r
        for r in job.assembly_plan["variants"][0]["text_elements"]
        if r["id"] not in offer_en["replace_ids"]
    ]
    return {
        "editor_talking_en": {
            "variant_id": variant["variant_id"],
            "base_generation": gj.variant_render_baseline(job.assembly_plan["variants"][0]),
            "editor_capabilities": {
                key: variant["editor_capabilities"][key]
                for key in ("intro_styles", "text_elements")
                if key in variant["editor_capabilities"]
            },
            "text_elements": talking["rows"],
        },
        "offer_talking_en": offer_en,
        "offer_montage_tr": offer_tr,
        "offer_narrated_en": offer_narrated,
        "unavailable_nothing_fits": st._unavailable("nothing_fits"),
        "save_body_expected": {
            "picked_key": pick["key"],
            "base_generation": gj.variant_render_baseline(job.assembly_plan["variants"][0]),
            "text_elements": [*pick["rows"], *copy.deepcopy(other_rows)],
        },
    }


def _offer_shape(offer: dict) -> dict:
    return {
        "keys": sorted(offer),
        "available": offer["available"],
        "reason": offer["reason"],
        "route": offer.get("route"),
        "title_family": offer.get("title_family"),
        "language": offer.get("language"),
        "options": [
            {
                "key": option["key"],
                "recommended": option["recommended"],
                "current": option["current"],
                "row_ids": [row["id"] for row in option["rows"]],
                "row_texts": [row["text"] for row in option["rows"]],
                "row_keys": sorted({k for row in option["rows"] for k in row}),
                "intro_style_keys": sorted(
                    {
                        k
                        for row in option["rows"]
                        for k in ((row.get("source_params") or {}).get("title_look") or {}).get(
                            "intro_style", {}
                        )
                    }
                ),
                "preview": None
                if option["preview"] is None
                else {
                    "keys": sorted(option["preview"]),
                    "layers": [
                        " ".join(run["text"] for run in layer["runs"])
                        for layer in option["preview"]["text_layers"]
                    ],
                    "fonts": sorted(option["preview"]["fonts"].values()),
                },
            }
            for option in offer.get("options", [])
        ],
    }


def _shape(fixture: dict) -> dict:
    return {
        "editor": {
            "variant_id": fixture["editor_talking_en"]["variant_id"],
            "capabilities": fixture["editor_talking_en"]["editor_capabilities"],
            "rows": [r["id"] for r in fixture["editor_talking_en"]["text_elements"]],
        },
        "offers": {
            name: _offer_shape(fixture[name])
            for name in (
                "offer_talking_en",
                "offer_montage_tr",
                "offer_narrated_en",
                "unavailable_nothing_fits",
            )
        },
        "save": {
            "picked_key": fixture["save_body_expected"]["picked_key"],
            "ids": [r["id"] for r in fixture["save_body_expected"]["text_elements"]],
        },
    }


def test_the_ios_fixture_is_what_the_server_sends():
    fresh = _fresh()
    # the pick's rows ARE what the app must send, raw (F23): nothing stripped, nothing renamed
    assert (
        fresh["save_body_expected"]["text_elements"][
            : len(fresh["offer_talking_en"]["options"][1]["rows"])
        ]
        == fresh["offer_talking_en"]["options"][1]["rows"]
    )
    assert all(o["preview"] is not None for o in fresh["offer_montage_tr"]["options"])
    if _FIXTURE.exists() and os.environ.get("KRI1633_REGENERATE") != "1":
        stored = json.loads(_FIXTURE.read_text())
        assert _shape(stored) == _shape(fresh), (
            "regenerate: KRI1633_REGENERATE=1 pytest tests/pipeline/test_intro_styles_ios_fixture.py"  # noqa: E501
        )
        return
    _FIXTURE.write_text(json.dumps(fresh, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    if os.environ.get("KRI1633_REGENERATE") != "1":
        pytest.fail(f"wrote {_FIXTURE}; commit it")
