"""KRI-219 Lane C: audio ops (set_mix extension, catalog-backed add_sfx)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.edit_copilot import _parse_op, _ParseState
from app.kria import planner
from app.services import kria_editor_ops as ops_mod
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from tests.services.test_kria_editor_ops import _job, _variant

CATALOG = [
    {"id": "whoosh_01", "name": "Whoosh", "category": "transitions", "duration_s": 0.8},
]


def _snap(**extra) -> dict:
    return {
        "editor_ops_version": 2,
        "allowed_op_families": ["music", "sfx"],
        "total_duration_s": 8,
        "mix": {"music_level": 0.5},
        "music": {"removable": True, "current_track_id": "t"},
        "sfx": {"placements": [], "catalog": CATALOG},
        **extra,
    }


def _parse(op: dict, snap: dict):
    state = _ParseState(0.9)
    return _parse_op(op, snap, state), state


# ---- parser: set_mix -------------------------------------------------------


def test_set_mix_music_level_is_unchanged() -> None:
    parsed, _ = _parse({"op": "set_mix", "music_level": 0.35}, _snap())
    assert parsed == {"op": "set_mix", "music_level": 0.35}


def test_set_mix_needs_at_least_one_field() -> None:
    parsed, state = _parse({"op": "set_mix"}, _snap())
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "missing_required"


def test_original_level_accepted_on_device_and_clamped() -> None:
    snap = _snap(render_destination="device")
    parsed, _ = _parse({"op": "set_mix", "original_level": 0}, snap)
    assert parsed == {"op": "set_mix", "original_level": 0.0}
    parsed, _ = _parse({"op": "set_mix", "original_level": 4}, snap)
    assert parsed == {"op": "set_mix", "original_level": 1.0}


def test_original_level_rejected_honestly_off_device() -> None:
    parsed, state = _parse({"op": "set_mix", "original_level": 0}, _snap())
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "capability_unavailable"
    assert "phone-rendered" in state.rejection_reasons[0]["detail"]


def test_original_level_rejected_on_guided_native_device_snapshot() -> None:
    """Guided saves persist only music_level; original_level would be silently dropped."""
    snap = _snap(render_destination="device", guided_revision={"revision_number": 1})
    parsed, state = _parse({"op": "set_mix", "original_level": 0}, snap)
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "capability_unavailable"
    assert "footage's own sound" in state.rejection_reasons[0]["detail"]


def test_original_level_not_reachable_from_web_drawer_snapshot() -> None:
    snap = _snap(render_destination="device")
    snap.pop("editor_ops_version")
    parsed, state = _parse({"op": "set_mix", "original_level": 0}, snap)
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "missing_required"


def test_original_level_non_numeric_is_invalid() -> None:
    parsed, state = _parse(
        {"op": "set_mix", "original_level": "loud"}, _snap(render_destination="device")
    )
    assert parsed is None and state.invalid_value_seen


def test_music_gain_db_requires_a_bed_and_clamps() -> None:
    parsed, state = _parse({"op": "set_mix", "music_gain_db": -12}, _snap())
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "capability_unavailable"
    snap = _snap()
    snap["mix"]["background_music"] = {"track_id": "t", "gain_db": -18}
    parsed, _ = _parse({"op": "set_mix", "music_gain_db": -99}, snap)
    assert parsed == {"op": "set_mix", "music_gain_db": -40.0}
    parsed, _ = _parse({"op": "set_mix", "music_level": 0.2, "music_gain_db": 6}, snap)
    assert parsed == {"op": "set_mix", "music_level": 0.2, "music_gain_db": 0.0}


# ---- parser: add_sfx -------------------------------------------------------


def test_add_sfx_valid_and_clamped_to_duration() -> None:
    parsed, _ = _parse({"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 3}, _snap())
    assert parsed == {"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 3.0, "gain": 1.0}
    parsed, _ = _parse({"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 99, "gain": 9}, _snap())
    assert parsed["at_s"] == pytest.approx(7.9) and parsed["gain"] == 2.0


def test_add_sfx_unknown_id_and_empty_catalog_fail_closed() -> None:
    parsed, state = _parse({"op": "add_sfx", "effect_id": "laser", "at_s": 1}, _snap())
    assert parsed is None and state.invalid_value_seen
    empty = _snap()
    empty["sfx"]["catalog"] = []
    parsed, state = _parse({"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 1}, empty)
    assert parsed is None and state.invalid_value_seen


def test_add_sfx_withheld_when_sfx_family_absent() -> None:
    snap = _snap(allowed_op_families=["music"])  # device variants never get "sfx"
    parsed, state = _parse({"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 1}, snap)
    assert parsed is None
    assert state.rejection_reasons[0]["reason"] == "capability_unavailable"


# ---- compiler --------------------------------------------------------------


def _compile(ops: list[dict], variant: dict | None = None):
    variant = variant or _variant()
    return compile_editor_ops(_job(variant), variant, ops)


def test_compile_set_mix_music_level_payload_identical_to_before() -> None:
    payload = _compile([{"op": "set_mix", "music_level": 0.35}]).payload
    assert payload.mix.model_dump(exclude_none=True) == {"music_level": 0.35}
    assert payload.background_music is None


def test_compile_original_level_device_only() -> None:
    variant = {**_variant(), "render_destination": "device"}
    payload = _compile([{"op": "set_mix", "original_level": 0.0}], variant).payload
    assert payload.mix.original_level == 0.0 and payload.mix.music_level is None
    with pytest.raises(KriaEditorOpError, match="phone-rendered"):
        _compile([{"op": "set_mix", "original_level": 0.0}])


def test_compile_music_gain_db_sends_current_track_and_gain() -> None:
    variant = {**_variant(), "smart_music_treatment": {"track_id": "trk", "gain_db": -18}}
    payload = _compile([{"op": "set_mix", "music_gain_db": -30}], variant).payload
    assert payload.background_music.track_id == "trk"
    assert payload.background_music.gain_db == -30
    assert payload.mix is None
    with pytest.raises(KriaEditorOpError, match="background music bed"):
        _compile([{"op": "set_mix", "music_gain_db": -30}])


def test_compile_add_sfx_uses_ios_wire_shape() -> None:
    compiled = _compile([{"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 3, "gain": 1.5}])
    row = compiled.payload.sound_effects[-1]
    assert row["sound_effect_id"] == "whoosh_01"
    assert row["at_s"] == 3 and row["gain"] == 1.5
    assert row["source"] == "user" and row["src_gcs_path"] == "" and row["id"]
    assert {"trim_start_s", "trim_end_s", "label"} <= row.keys()


def test_compile_add_sfx_rejects_bad_values() -> None:
    for op in (
        {"op": "add_sfx", "effect_id": "", "at_s": 1},
        {"op": "add_sfx", "effect_id": "x", "at_s": -1},
        {"op": "add_sfx", "effect_id": "x", "at_s": 999},
        {"op": "add_sfx", "effect_id": "x", "at_s": 1, "gain": 5},
    ):
        with pytest.raises(KriaEditorOpError):
            _compile([op])


def test_existing_audio_ops_still_compile() -> None:
    variant = {
        **_variant(),
        "sound_effects": [
            {"id": "a", "sound_effect_id": "pop", "src_gcs_path": "sound-effects/p.mp3", "at_s": 1}
        ],
    }
    payload = _compile(
        [{"op": "patch_sfx", "sfx_index": 0, "gain": 1.2}, {"op": "remove_music"}], variant
    ).payload
    assert payload.sound_effects[0]["gain"] == 1.2 and payload.remove_music is True
    assert _compile([{"op": "remove_sfx", "sfx_index": 0}], variant).payload.sound_effects == []


# ---- snapshot --------------------------------------------------------------


def _caps(**over):
    caps = {"mix": True, "sfx": True, "background_music": True, "text_elements": True}
    caps.update(over)
    return lambda _job, _variant: caps


def test_snapshot_carries_catalog_bed_and_device_original(monkeypatch) -> None:
    monkeypatch.setattr(ops_mod, "_editor_capabilities", _caps())
    variant = {**_variant(), "smart_music_treatment": {"track_id": "trk", "gain_db": -20}}
    snap = ops_mod.build_editor_snapshot(
        _job(variant), variant, clip_context={"sfx_catalog": CATALOG}
    )
    assert snap["sfx"]["catalog"] == CATALOG
    assert snap["mix"]["background_music"] == {"track_id": "trk", "gain_db": -20.0}
    assert "render_destination" not in snap and "original_level" not in snap["mix"]

    device = {**variant, "render_destination": "device", "original_audio_level": 0.4}
    snap = ops_mod.build_editor_snapshot(_job(device), device)
    assert snap["render_destination"] == "device" and snap["mix"]["original_level"] == 0.4
    assert "sfx" not in snap["allowed_op_families"]  # device keeps sfx withheld


def test_snapshot_without_context_has_empty_catalog(monkeypatch) -> None:
    monkeypatch.setattr(ops_mod, "_editor_capabilities", _caps())
    variant = _variant()
    snap = ops_mod.build_editor_snapshot(_job(variant), variant)
    assert snap["sfx"]["catalog"] == []
    assert "background_music" not in snap["mix"]


# ---- planner catalog query -------------------------------------------------


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def begin_nested(self):
        return _Nested()

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.asyncio
async def test_sfx_catalog_rows_shape_and_single_bounded_query() -> None:
    row = SimpleNamespace(id="w1", name="Whoosh", category="transitions", duration_s=0.8)
    db = _FakeDb([row])
    rows = await planner._sfx_catalog_rows(db)
    assert rows == [
        {
            "id": "w1",
            "name": "Whoosh",
            "label": "Whoosh",
            "category": "transitions",
            "duration_s": 0.8,
        }
    ]
    assert len(db.statements) == 1
    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "published_at IS NOT NULL" in compiled and "archived_at IS NULL" in compiled
    assert "LIMIT 40" in compiled


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_clip_context_carries_catalog_only_when_sfx_enabled(monkeypatch, enabled) -> None:
    async def fake_rows(_db):
        return CATALOG

    monkeypatch.setattr(planner, "_sfx_catalog_rows", fake_rows)
    monkeypatch.setattr(planner.settings, "sound_effects_enabled", enabled)
    context = await planner._copilot_clip_context(
        None,
        thread=SimpleNamespace(creator_id="c"),
        thread_id="t",
        job=SimpleNamespace(user_id="u"),
        variant={},
        item=SimpleNamespace(clip_assignments=[]),
    )
    assert context.get("sfx_catalog") == (CATALOG if enabled else None)


def test_audio_prompt_fragment_ships_under_v2_marker() -> None:
    from app.agents import edit_copilot, editor_ops_v2

    assert "add_sfx" in editor_ops_v2.prompt_fragments()
    assert "AUDIO OPS" not in edit_copilot._with_v2_fragments("base", {})
    assert "AUDIO OPS" in edit_copilot._with_v2_fragments("base", {"editor_ops_version": 2})
