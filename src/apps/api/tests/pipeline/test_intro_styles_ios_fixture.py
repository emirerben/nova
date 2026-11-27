"""KRI-1633: the iOS fixture is what the server sends.

`src/apps/ios/Tests/Fixtures/KRI1633IntroStyles.json` is GENERATED here from the real offer
builder (never hand-edited) and read by the iOS unit and UI tests (`IntroStylesOfferTests`,
`IntroStylesUITests`), so the app is tested against the exact wire shape: the editor variant with
its capability and three editor variants with their offers (Talking EN, montage TR, narrated EN),
an unavailable offer and the editor-commit body a pick must produce. KRI-1615 adds a rendered
Talking edit framed with room for cards, its offer with card looks (four of her photos) and the
body a card look must produce: her own card later in the video first, then the look's cards.

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
from app.services.client_protocol import (
    INTRO_CARDS_PROTOCOL,
    INTRO_STYLES_PROTOCOL,
    _client_protocol,
)

# A fixed job id keeps the seeded looks (and so the fixture) the same on every run.
_JOB_ID = uuid.UUID("00000000-0000-4000-8000-000000001633")
# A Turkish title whose caps look draws "TRENDİ" (dotted capital): the iOS live preview must
# produce the same capitals as the server's preview layers (F10).
_TR_TITLE = "bu yılın trendi şu an"
_CARD_JOB_ID = uuid.UUID("00000000-0000-4000-8000-000000001615")
_FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI1633IntroStyles.json"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    for key, value in fx.flag_overrides().items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "intro_cards_enabled", True)


def _offer(route: str, title: str) -> tuple[dict, dict]:
    job, vid, rows = fx.BUILDERS[route](title, job_id=_JOB_ID)
    return st.build_offer(st.detach(job), vid, rows, "first"), {"job": job, "rows": rows}


def _editor(
    route: str,
    job,
    rows: list[dict],
    *,
    protocol: int = INTRO_STYLES_PROTOCOL,
    capabilities: tuple[str, ...] = ("intro_styles", "text_elements"),
) -> dict:
    """The editor variant the app loads for ``route``: its capability map and the title rows."""
    token = _client_protocol.set(protocol)
    try:
        variant_id = fx.VARIANT_IDS[route]
        variant = next(v for v in gj._variants_for_response(job) if v["variant_id"] == variant_id)
    finally:
        _client_protocol.reset(token)
    saved = next(v for v in job.assembly_plan["variants"] if v["variant_id"] == variant_id)
    return {
        "variant_id": variant_id,
        "base_generation": gj.variant_render_baseline(saved),
        "editor_capabilities": {
            key: variant["editor_capabilities"][key]
            for key in capabilities
            if key in variant["editor_capabilities"]
        },
        "text_elements": rows,
    }


def _card_blocks() -> dict:
    """A rendered phone Talking edit with room for cards (so its `overlays` lane is open), her
    four photos, and one card of hers outside the title's window (it never stops a card look,
    `test_intro_cards_e2e`). Her card is in the editor projection's shape for a saved card."""
    job, _vid, rows = fx.talking_job(fx.EN_TITLE, face=fx.ROOMY_FACE, job_id=_CARD_JOB_ID)
    saved = job.assembly_plan["variants"][0]
    saved["output_url"] = f"users/{fx.USER_ID}/plan/{fx.CARD_ITEM}/out.mp4"
    offer = st.build_offer(
        st.detach(job),
        saved["variant_id"],
        rows,
        "first",
        photos=fx.card_photos(),
        save_arguments={"user_id": fx.USER_ID, "plan_item_id": fx.CARD_ITEM},
    )
    hers = {
        "id": "her-card",
        "kind": "image",
        "src_gcs_path": fx.card_photos()[0].gcs_path,
        "display_mode": "pip",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.3,
        "scale": 0.3,
        "start_s": 4.0,
        "end_s": 5.5,
        "z": 0,
        "entrance_token": "none",
        "exit_token": "none",
        "source": "phone_lane",
    }
    editor = _editor(
        "talking",
        job,
        rows,
        protocol=INTRO_CARDS_PROTOCOL,
        capabilities=("intro_styles", "text_elements", "overlays"),
    )
    editor["media_overlays"] = [hers]
    pick = next(option for option in offer["options"] if option.get("cards"))
    return {
        "editor_talking_cards": editor,
        "offer_talking_cards": offer,
        "save_body_cards_expected": {
            "picked_key": pick["key"],
            "base_generation": editor["base_generation"],
            "text_elements": pick["rows"],
            "media_overlays": [
                hers,
                *(st.IntroCard.model_validate(card).media_overlay() for card in pick["cards"]),
            ],
        },
    }


def _fresh() -> dict:
    offer_en, talking = _offer("talking", fx.EN_TITLE)
    job = talking["job"]
    offer_tr, montage = _offer("montage", _TR_TITLE)
    offer_narrated, narrated = _offer("narrated", fx.EN_TITLE)
    pick = offer_en["options"][1]
    saved = job.assembly_plan["variants"][0]
    other_rows = [r for r in saved["text_elements"] if r["id"] not in offer_en["replace_ids"]]
    return {
        "editor_talking_en": _editor("talking", job, talking["rows"]),
        "editor_montage_tr": _editor("montage", montage["job"], montage["rows"]),
        "editor_narrated_en": _editor("narrated", narrated["job"], narrated["rows"]),
        "offer_talking_en": offer_en,
        "offer_montage_tr": offer_tr,
        "offer_narrated_en": offer_narrated,
        "unavailable_nothing_fits": st._unavailable("nothing_fits"),
        "save_body_expected": {
            "picked_key": pick["key"],
            "base_generation": gj.variant_render_baseline(saved),
            "text_elements": [*pick["rows"], *copy.deepcopy(other_rows)],
        },
        **_card_blocks(),
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
                    "cards": [
                        (card["media_id"], sorted(card), sorted(card["box"]))
                        for card in option["preview"].get("cards") or []
                    ],
                },
                "cards": [
                    (card["id"], card["slot"], card["media_id"], card["gcs_path"], sorted(card))
                    for card in option.get("cards") or []
                ],
            }
            for option in offer.get("options", [])
        ],
    }


def _shape(fixture: dict) -> dict:
    return {
        "editors": {
            name: {
                "variant_id": fixture[name]["variant_id"],
                "capabilities": fixture[name]["editor_capabilities"],
                "rows": [r["id"] for r in fixture[name]["text_elements"]],
                "overlays": [o["id"] for o in fixture[name].get("media_overlays", [])],
            }
            for name in (
                "editor_talking_en",
                "editor_montage_tr",
                "editor_narrated_en",
                "editor_talking_cards",
            )
        },
        "offers": {
            name: _offer_shape(fixture[name])
            for name in (
                "offer_talking_en",
                "offer_montage_tr",
                "offer_narrated_en",
                "offer_talking_cards",
                "unavailable_nothing_fits",
            )
        },
        "save": {
            "picked_key": fixture["save_body_expected"]["picked_key"],
            "ids": [r["id"] for r in fixture["save_body_expected"]["text_elements"]],
        },
        "save_cards": {
            "picked_key": fixture["save_body_cards_expected"]["picked_key"],
            "ids": [r["id"] for r in fixture["save_body_cards_expected"]["text_elements"]],
            "overlays": [
                (o["id"], sorted(o)) for o in fixture["save_body_cards_expected"]["media_overlays"]
            ],
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
    # F10: a Turkish caps look in the fixture draws the dotted capital the iOS preview must match
    drawn = [
        run["text"]
        for option in fresh["offer_montage_tr"]["options"]
        for layer in option["preview"]["text_layers"]
        for run in layer["runs"]
    ]
    assert any("TRENDİ" in text for text in drawn), drawn
    # KRI-1615: card looks after the plain looks, "none" last, each with its photos in its preview
    options = fresh["offer_talking_cards"]["options"]
    kinds = ["card" if option.get("cards") else "plain" for option in options[:-1]]
    assert "card" in kinds and kinds == sorted(kinds, key=lambda kind: kind == "card"), kinds
    assert options[-1]["key"] == "none" and "cards" not in options[-1]
    for option in options:
        if option.get("cards"):
            assert [c["media_id"] for c in option["preview"]["cards"]] == [
                c["media_id"] for c in option["cards"]
            ]
    if _FIXTURE.exists() and os.environ.get("KRI1633_REGENERATE") != "1":
        stored = json.loads(_FIXTURE.read_text())
        assert _shape(stored) == _shape(fresh), (
            "regenerate: KRI1633_REGENERATE=1 pytest tests/pipeline/test_intro_styles_ios_fixture.py"  # noqa: E501
        )
        return
    _FIXTURE.write_text(json.dumps(fresh, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    if os.environ.get("KRI1633_REGENERATE") != "1":
        pytest.fail(f"wrote {_FIXTURE}; commit it")
