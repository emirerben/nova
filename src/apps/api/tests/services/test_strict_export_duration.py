"""T1 request -> approved contract -> phone recipe -> finished-file receipt.

Failure modes were recorded before implementation in
docs/reviews/kri-629-export-duration.md. The 2026-10-12 measured export is an
independent acceptance source: 37.976494594 s edit + 1.6 s ending, 39.576667 s file.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import PlanFacts, check_requirement
from app.kria.device_render import DeviceRenderStatus, DeviceRenderStatusOut, make_device_request
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_subtitled_title import talking_closing_element, talking_closing_window
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import device_export_maximum, device_record, pin_device_request
from app.services.export_duration_policy import approved_export_maximum, check_export_maximum
from app.services.length_policy_state import (
    check_creator_duration,
    read_length_policy,
    stamp_length_policy,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_subtitled_plan import _binding

STRATEGY = {"edit_format": "subtitled", "audio_strategy": "original_audio"}


def requirement(text="40 saniyenin altında olsun."):
    return BriefRequirement(
        id="length", kind="timing", scope="global", description=text, facts={"duration_s": 40}
    )


def contract(text="40 saniyenin altında olsun."):
    return build_render_contract(
        STRATEGY, brief=CreativeBrief(requirements=[requirement(text)]), generation_id="t1"
    )


def recipe(duration=37.976494594):
    return compile_phone_subtitled_plan((_binding(duration_s=duration),), caption_cues=[])


def assembly_for(value, text="40 saniyenin altında olsun."):
    return {
        CONTRACT_FIELD: value.model_dump(mode="json"),
        "creator_brief_binding": {
            "brief": CreativeBrief(requirements=[requirement(text)]).model_dump(mode="json")
        },
    }


def test_original_t1_request_survives_contract_roundtrip():
    value = contract()
    assembly = assembly_for(value)
    assert approved_export_maximum(assembly, value) == 40
    restored = read_render_contract(assembly)
    assert restored == value
    assert approved_export_maximum(assembly, restored.rebind(generation_id="edited")) == 40
    assert "max_export_duration_s" not in value.model_dump()


@pytest.mark.parametrize(
    "edit_s,passes", [(20, True), (38.3, True), (38.4, False), (38.5, False), (42, False)]
)
def test_recipe_budget_includes_the_standard_ending(edit_s, passes):
    value = contract()
    policy = read_length_policy(stamp_length_policy({}, STRATEGY, value, mode="log_only"), value)
    if passes:
        verify_phone_recipe(
            value,
            recipe(edit_s),
            length_policy=policy,
            source_audio={"clip-0": True},
            max_export_duration_s=approved_export_maximum(assembly_for(value), value),
        )
    else:
        with pytest.raises(CreatorRenderContractError, match="under 40"):
            verify_phone_recipe(
                value,
                recipe(edit_s),
                length_policy=policy,
                source_audio={"clip-0": True},
                max_export_duration_s=approved_export_maximum(assembly_for(value), value),
            )


@pytest.mark.parametrize(
    "actual,passes", [(20, True), (39.576667, True), (39.999, True), (40, False), (40.001, False)]
)
def test_actual_export_bound_has_no_lower_limit_or_rounding_tolerance(actual, passes):
    value = contract()
    if passes:
        check_export_maximum(actual, approved_export_maximum(assembly_for(value), value))
    else:
        with pytest.raises(CreatorRenderContractError, match="under 40"):
            check_export_maximum(actual, approved_export_maximum(assembly_for(value), value))


@pytest.mark.parametrize("text", ["Keep it under 40 seconds", "40 saniyenin altında olsun."])
@pytest.mark.parametrize(
    "edit_s,outro,expected",
    [
        (20, 1.6, "met"),
        (37.976494594, 1.6, "met"),
        (38.4, 1.6, "partial"),
        (38.5, 1.6, "partial"),
        (42, 0, "partial"),
    ],
)
def test_finished_receipt_obeys_maximum_even_when_cleanup_ran(text, edit_s, outro, expected):
    facts = PlanFacts(
        duration_s=edit_s,
        outro_s=outro,
        rendered_variant=True,
        edit_format="subtitled",
        audio_strategy="original_audio",
        speech_cleanup_outcome="applied",
        measured_export_duration_s=edit_s + outro,
    )
    assert check_requirement(requirement(text), facts).status == expected


def test_unmeasured_phone_result_does_not_claim_verified_success():
    facts = PlanFacts(
        duration_s=38,
        outro_s=1.6,
        rendered_variant=True,
        phone_talking=True,
        edit_format="subtitled",
        audio_strategy="original_audio",
    )
    receipt = check_requirement(requirement(), facts)
    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"


def test_approximate_target_keeps_legacy_behavior():
    value = contract("Make it about 40 seconds")
    assert approved_export_maximum(assembly_for(value, "Make it about 40 seconds"), value) is None
    check_creator_duration(value, 42, state=None)


def test_t1_badge_uses_cleaned_timeline_and_adds_no_length():
    # These source intervals come from the original silence-cut evidence,
    # independently of the compiler under test.
    keeps = [
        (0.760, 2.145),
        (4.095, 5.385),
        (6.415, 8.405),
        (12.615, 13.385),
        (14.392, 16.405),
        (17.495, 18.465),
        (19.255, 22.109),
        (22.535, 31.425),
        (32.492, 36.700),
        (37.535, 39.385),
        (40.035, 42.805),
        (43.243, 44.925),
        (45.455, 52.760),
    ]
    duration = sum(end - start for start, end in keeps)
    start, end = talking_closing_window(duration)
    badge = talking_closing_element("Loy Tree", start_s=start, end_s=end, photo=None)
    compiled = compile_phone_subtitled_plan(
        (_binding(duration_s=53.4),), caption_cues=[], keep_segments=keeps, text_elements=[badge]
    )
    assert compiled.duration == pytest.approx(37.977, abs=0.001)
    assert compiled.text_layers[-1].start == pytest.approx(34.977, abs=0.001)
    assert compiled.text_layers[-1].end == pytest.approx(37.977, abs=0.001)
    verify_phone_recipe(
        contract(), compiled, source_audio={"clip-0": True}, max_export_duration_s=40
    )


def test_pinned_request_carries_server_owned_maximum():
    value = contract()
    binding = _binding(duration_s=37.976494594)
    job = SimpleNamespace(
        id=uuid4(),
        assembly_plan={
            **assembly_for(value),
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
        },
    )
    request = make_device_request(
        job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe()
    )
    assert pin_device_request(job, request, base_generation="t1")
    status = DeviceRenderStatus.model_validate(device_record(job, "subtitled")["status"])
    assert "max_export_duration_s" not in status.request.model_dump()
    response = status.model_dump()
    response["request"]["max_export_duration_s"] = device_export_maximum(job, "subtitled")
    assert (
        DeviceRenderStatusOut.model_validate(response).model_dump()["request"][
            "max_export_duration_s"
        ]
        == 40
    )
    assert not pin_device_request(job, request, base_generation="t1")


@pytest.mark.parametrize("mutation", ["missing", "changed", "malformed_generation"])
def test_strict_limit_cannot_be_lost_by_changing_approved_authority(mutation):
    value = contract()
    assembly = assembly_for(value)
    if mutation == "missing":
        assembly.pop("creator_brief_binding")
    elif mutation == "changed":
        assembly["creator_brief_binding"]["brief"]["requirements"][0]["description"] = (
            "about 40 seconds"
        )
    else:
        assembly["creator_brief_bindings"] = {"t1": None}
    with pytest.raises(CreatorRenderContractError, match="approved brief"):
        approved_export_maximum(assembly, value)


@pytest.mark.parametrize(
    "ready,generation,expected",
    [(True, "published", 39.5), (False, "published", None), (True, "old", None)],
)
def test_only_current_finished_bytes_can_verify_a_length_receipt(ready, generation, expected):
    from app.kria.brief_checks import plan_facts_from_phone_variant

    facts = plan_facts_from_phone_variant(
        {
            "ok": ready,
            "render_status": "ready" if ready else "awaiting_device",
            "render_generation_id": "published",
            "measured_export_generation_id": generation,
            "measured_export_duration_s": 39.5,
            "duration_s": 39.5,
            "resolved_archetype": "subtitled",
            "render_destination": "device",
        }
    )
    assert facts.measured_export_duration_s == expected


def test_historical_no_ending_request_can_still_publish_a_valid_39_second_file():
    from app.services.device_render import _recipe_digest, verify_device_record_contract

    value = contract()
    compiled = recipe(39)
    job = SimpleNamespace(
        id=uuid4(),
        assembly_plan={
            **assembly_for(value),
            PHONE_SOURCES_FIELD: [_binding(duration_s=39).model_dump(mode="json")],
        },
    )
    request = make_device_request(
        job_id=job.id, variant_id="subtitled", revision=1, recipe=compiled
    )
    # A stored request pinned by an earlier server, before standard-ending
    # reservation. Its uploader may truthfully declare `brand_tail=none`.
    status = DeviceRenderStatus(phase="syncing", request=request)
    record = {
        "contract_digest": value.digest,
        "base_generation": "t1",
        "recipe_digest": _recipe_digest(compiled),
    }
    verify_device_record_contract(job, record, status)
    check_export_maximum(39, 40)
    with pytest.raises(CreatorRenderContractError):
        check_export_maximum(40.6, 40)
    # Newly pinned current-client requests still reserve the standard ending.
    with pytest.raises(CreatorRenderContractError):
        pin_device_request(job, request, base_generation="t1")


@pytest.mark.parametrize(
    "actual,passes", [(39.576667, True), (39.999, True), (40.0, False), (40.001, False)]
)
def test_server_checks_probe_measurement_before_publication(monkeypatch, actual, passes):
    import hashlib
    import json
    from pathlib import Path

    from app.routes import device_render as routes
    from tests.test_device_render import _probe_with_duration

    # Recipe expectation fits; these near-boundary file lengths all fall within
    # the old verifier's 100ms tolerance. Only the new strict check distinguishes them.
    request = make_device_request(
        job_id=uuid4(),
        variant_id="subtitled",
        revision=1,
        recipe=recipe(38.35 if actual >= 39.9 else actual - 1.6),
    )
    status = DeviceRenderStatus(phase="syncing", request=request)
    data = b"transport fixture"

    def download(_path, local, *, generation):
        Path(local).write_bytes(data)

    probe = _probe_with_duration(actual)
    probe["streams"].append({"codec_type": "audio", "codec_name": "aac"})
    monkeypatch.setattr(routes.storage, "download_generation_to_file", download)
    monkeypatch.setattr(
        routes.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(probe))
    )
    measured = {}

    def verify():
        return routes._verify_export(
            "local/path",
            "42",
            len(data),
            hashlib.sha256(data).hexdigest(),
            status,
            "standard",
            export_measurements=measured,
            max_export_duration_s=40,
        )

    if passes:
        verify()
        assert measured["measured_export_duration_s"] == actual
    else:
        with pytest.raises(CreatorRenderContractError, match="under 40") as error:
            verify()
        assert error.value.decline_reason == "requirement_conflict"
        assert measured == {}


def test_real_native_t1_files_pass_the_server_probe_when_available(monkeypatch):
    import hashlib
    import json
    import os
    import shutil
    from pathlib import Path

    from app.kria.device_render import DeviceRenderRequestOut
    from app.routes import device_render as routes

    if not os.environ.get("KRIA_T1_DURATION_DIR"):
        pytest.skip("Opt-in original-media replay: set KRIA_T1_DURATION_DIR")
    root = Path(os.environ["KRIA_T1_DURATION_DIR"])
    reports = []
    for name in ("h264", "hevc10"):
        report = json.loads((root / name / "report.json").read_text())
        output = Path(report["output"])
        request = DeviceRenderRequestOut.model_validate_json(Path(report["request"]).read_text())
        status = DeviceRenderStatus(phase="syncing", request=request)

        def download(_path, local, *, generation):
            shutil.copyfile(output, local)

        monkeypatch.setattr(routes.storage, "download_generation_to_file", download)
        measurements = {}
        routes._verify_export(
            "local/native-t1",
            "42",
            output.stat().st_size,
            hashlib.file_digest(output.open("rb"), "sha256").hexdigest(),
            status,
            "standard",
            export_measurements=measurements,
            max_export_duration_s=request.max_export_duration_s,
        )
        assert measurements["measured_export_duration_s"] < 40
        reports.append({"case": name, **measurements})
    (root / "server-report.json").write_text(json.dumps(reports, indent=2))
