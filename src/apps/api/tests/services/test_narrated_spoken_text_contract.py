from dataclasses import replace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.services.creator_render_contract import build_render_contract
from app.services.phone_editor import _reconcile_narrated_spoken_text_receipt


def _variant() -> dict:
    return {
        "spoken_text_placement": {
            "version": 1,
            "rows": [
                {
                    "requirement_id": "r1",
                    "status": "placed",
                    "text": "RULE · 1",
                    "element_id": "spoken-text-r1",
                    "start_s": 2.0,
                    "end_s": 4.0,
                }
            ],
        }
    }


def _element(*, text="RULE · 1", start_s=2.0, end_s=4.0) -> dict:
    return {
        "id": "spoken-text-r1",
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "source_params": {"source": "spoken_text", "requirement_id": "r1"},
    }


def test_unchanged_spoken_text_receipt_survives_text_save() -> None:
    variant = _variant()

    _reconcile_narrated_spoken_text_receipt(variant, [_element()])

    assert variant["spoken_text_placement"]["rows"][0]["status"] == "placed"


def test_reworded_or_retimed_spoken_text_is_demoted_after_text_save() -> None:
    for element in (_element(text="RULE · 2"), _element(start_s=3.0), _element(end_s=5.0)):
        variant = _variant()

        _reconcile_narrated_spoken_text_receipt(variant, [element])

        row = variant["spoken_text_placement"]["rows"][0]
        assert row["status"] == "edited"
        assert row["error"] == "editor_text_changed"


def test_deleted_spoken_text_is_demoted_after_text_save() -> None:
    variant = _variant()

    _reconcile_narrated_spoken_text_receipt(variant, [])

    assert variant["spoken_text_placement"]["rows"][0]["status"] == "edited"


def _spoken_requirement() -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="text",
        scope="global",
        literal="RULE · 1",
        description="show it when I say rule one",
        facts={"triggers": ["rule one"]},
    )


def _native_facts(*, rendered: bool = False):
    facts = replace(
        plan_facts_from_strategy(
            {
                "edit_format": "narrated_planned",
                "audio_strategy": "voiceover",
                "render_program": "native",
            }
        ),
        phone_narrated=True,
        rendered_variant=rendered,
    )
    return facts


def _enable_native_spoken_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_narrated_spoken_text_enabled", True)
    monkeypatch.setattr(settings, "phone_narrated_title_enabled", True)
    monkeypatch.setattr(settings, "phone_narrated_title_edits_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def test_native_draft_promises_conditionally_without_asking_for_a_simpler_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native_spoken_text(monkeypatch)

    (receipt,) = build_receipts([_spoken_requirement()], _native_facts(), include_unchecked=True)

    assert receipt.status == "partial"
    assert "if I don't hear the word" in receipt.reason
    assert not needs_creator_choice(receipt)


def test_cloud_and_flag_off_voiceover_keep_the_old_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_native_spoken_text(monkeypatch)
    req = _spoken_requirement()
    cloud = replace(_native_facts(), phone_narrated=False)
    (cloud_receipt,) = build_receipts([req], cloud, include_unchecked=True)
    assert cloud_receipt.status == "not_possible"
    assert needs_creator_choice(cloud_receipt)

    monkeypatch.setattr(settings, "phone_narrated_spoken_text_enabled", False)
    (off_receipt,) = build_receipts([req], _native_facts(), include_unchecked=True)
    assert off_receipt.status == "not_possible"
    assert needs_creator_choice(off_receipt)


def test_render_receipt_uses_exact_text_and_reports_stale_rows_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native_spoken_text(monkeypatch)
    req = _spoken_requirement()
    for text, expected in (("RULE · 1", "met"), ("RULE · 2", "partial")):
        variant = {
            "resolved_archetype": "narrated",
            "render_destination": "device",
            "spoken_text_placement": {
                "rows": [
                    {
                        "requirement_id": "r1",
                        "status": "placed",
                        "text": text,
                        "trigger": "rule one",
                    }
                ]
            },
        }
        (receipt,) = build_receipts(
            [req], plan_facts_from_phone_variant(variant), include_unchecked=True
        )
        assert receipt.status == expected


def test_rendered_native_receipt_is_truthful_after_rollout_flag_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native_spoken_text(monkeypatch)
    monkeypatch.setattr(settings, "phone_narrated_spoken_text_enabled", False)
    variant = {
        "resolved_archetype": "narrated",
        "render_destination": "device",
        "spoken_text_placement": {
            "rows": [
                {
                    "requirement_id": "r1",
                    "status": "placed",
                    "text": "RULE · 1",
                    "trigger": "rule one",
                }
            ]
        },
    }
    (receipt,) = build_receipts(
        [_spoken_requirement()], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "met"


def test_native_and_cloud_render_contracts_differ_for_spoken_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native_spoken_text(monkeypatch)
    brief = CreativeBrief(requirements=[_spoken_requirement()])
    native = build_render_contract(
        {
            "edit_format": "narrated_planned",
            "audio_strategy": "voiceover",
            "render_program": "native",
        },
        generation_id="native",
        brief=brief,
        media_snapshot={
            "clip_assignments": [
                {"media_id": "clip-1", "gcs_path": "users/u/analysis-proxy-clip-1.mp4"}
            ]
        },
    )
    cloud = build_render_contract(
        {
            "edit_format": "narrated_planned",
            "audio_strategy": "voiceover",
            "render_program": "guided",
        },
        generation_id="cloud",
        brief=brief,
        media_snapshot={
            "clip_assignments": [{"media_id": "clip-1", "gcs_path": "users/u/cloud-original.mp4"}]
        },
    )
    assert native is not None and not native.exact_texts
    assert cloud is not None and [item.text for item in cloud.exact_texts] == ["RULE · 1"]


def test_native_render_program_without_phone_proxy_keeps_required_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native_spoken_text(monkeypatch)
    contract = build_render_contract(
        {
            "edit_format": "narrated_planned",
            "audio_strategy": "voiceover",
            "render_program": "native",
        },
        generation_id="cloud-native-label",
        brief=CreativeBrief(requirements=[_spoken_requirement()]),
        media_snapshot={
            "clip_assignments": [{"media_id": "clip-1", "gcs_path": "users/u/cloud-original.mp4"}]
        },
    )
    assert contract is not None and [item.text for item in contract.exact_texts] == ["RULE · 1"]


def test_native_conditional_reason_is_turkish(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_native_spoken_text(monkeypatch)
    token = bind_reply_language("tr")
    try:
        (receipt,) = build_receipts(
            [_spoken_requirement()], _native_facts(), include_unchecked=True
        )
    finally:
        release_reply_language(token)
    assert receipt.status == "partial"
    assert "seslendirmen" in receipt.reason
    assert "if I don't hear" not in receipt.reason
