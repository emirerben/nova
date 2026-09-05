from types import SimpleNamespace

import pytest

from app.services.variant_generation_guard import (
    VariantInitialRenderInProgress,
    assert_required_speech_dispatch_quiescent,
    assert_variant_generation_editable,
    required_speech_generation_lock,
)


def _job(plan):
    return SimpleNamespace(assembly_plan=plan)


def test_absent_private_state_is_editable():
    assert required_speech_generation_lock(_job({}), "subtitled") is None
    assert_variant_generation_editable(_job({}), "subtitled")


def test_exact_variant_lock_blocks_edit():
    job = _job(
        {
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"subtitled": "generation-a"}
            }
        }
    )
    assert required_speech_generation_lock(job, "subtitled") == "generation-a"
    with pytest.raises(VariantInitialRenderInProgress):
        assert_variant_generation_editable(job, "subtitled")
    assert_variant_generation_editable(job, "other")


def test_active_speech_cut_control_blocks_target_and_sibling_edits():
    job = _job(
        {
            "speech_cut_control": {
                "operation_id": "operation-a",
                "render_generation_id": "generation-a",
                "variant_id": "subtitled",
            }
        }
    )

    assert required_speech_generation_lock(job, "subtitled") is None
    assert required_speech_generation_lock(job, "other") is None
    with pytest.raises(VariantInitialRenderInProgress):
        assert_variant_generation_editable(job, "subtitled")
    with pytest.raises(VariantInitialRenderInProgress):
        assert_variant_generation_editable(job, "other")


@pytest.mark.parametrize("sibling_status", ["pending", "rendering", None, "unknown", 1])
def test_required_speech_dispatch_rejects_nonterminal_or_malformed_sibling(sibling_status):
    job = _job(
        {
            "speech_cleanup_contract": "required_v1",
            "variants": [
                {"variant_id": "subtitled", "render_status": "ready"},
                {"variant_id": "song_text", "render_status": sibling_status},
            ],
        }
    )

    with pytest.raises(VariantInitialRenderInProgress):
        assert_required_speech_dispatch_quiescent(job, "subtitled")


@pytest.mark.parametrize(
    "variants",
    [
        [{"variant_id": "subtitled", "render_status": "ready"}, {}],
        [
            {"variant_id": "subtitled", "render_status": "ready"},
            {"variant_id": "subtitled", "render_status": "ready"},
        ],
        [{"variant_id": "song_text", "render_status": "ready"}],
    ],
    ids=["missing-sibling-id", "duplicate-target", "missing-target"],
)
def test_required_speech_dispatch_rejects_malformed_variant_vector(variants):
    job = _job({"speech_cleanup_contract": "required_v1", "variants": variants})

    with pytest.raises(VariantInitialRenderInProgress):
        assert_required_speech_dispatch_quiescent(job, "subtitled")


def test_required_speech_dispatch_accepts_terminal_siblings_and_legacy_contracts():
    assert_required_speech_dispatch_quiescent(
        _job(
            {
                "speech_cleanup_contract": "required_v1",
                "variants": [
                    {"variant_id": "subtitled", "render_status": "ready"},
                    {"variant_id": "song_text", "render_status": "failed"},
                ],
            }
        ),
        "subtitled",
    )
    assert_required_speech_dispatch_quiescent(
        _job(
            {
                "speech_cleanup_contract": "legacy_auto",
                "variants": [
                    {"variant_id": "subtitled", "render_status": "ready"},
                    {"variant_id": "song_text", "render_status": "rendering"},
                ],
            }
        ),
        "subtitled",
    )


@pytest.mark.parametrize("control", [[], "operation-a", 1, {"unexpected": True}])
def test_malformed_speech_cut_control_fails_closed(control):
    with pytest.raises(VariantInitialRenderInProgress):
        assert_variant_generation_editable(_job({"speech_cut_control": control}), "subtitled")


@pytest.mark.parametrize("control", [None, {}])
def test_cleared_speech_cut_control_is_editable(control):
    job = _job({"speech_cut_control": control})

    assert required_speech_generation_lock(job, "subtitled") is None
    assert_variant_generation_editable(job, "subtitled")


@pytest.mark.parametrize(
    "plan",
    [
        [],
        "",
        {"_speech_cleanup_internal": []},
        {"_speech_cleanup_internal": {"required_speech_generation_locks": []}},
        {
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"subtitled": {"bad": True}}
            }
        },
    ],
)
def test_malformed_private_lock_state_fails_closed(plan):
    with pytest.raises(VariantInitialRenderInProgress):
        assert_variant_generation_editable(_job(plan), "subtitled")
