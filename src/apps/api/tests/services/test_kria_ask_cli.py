"""KRI-219: kria_ask harness — arg parsing, battery evaluation, dry-run (no network/DB)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import kria_ask
from app.cli.kria_ask import (
    AskResult,
    BatteryCase,
    diff_text,
    dry_run_on_target,
    evaluate_case,
    format_table,
    load_battery,
    parse_args,
    run_battery,
)
from app.services.kria_editor_ops import build_editor_snapshot
from tests.services.test_kria_editor_ops import _job, _variant

BATTERY = Path(__file__).parents[1] / "fixtures" / "kria_battery" / "asks.yaml"
THREAD = "00000000-0000-0000-0000-000000000001"
V2_OPS = {
    "rewrite_text",
    "patch_text",
    "remove_texts",
    "set_texts_timing",
    "add_text",
    "patch_slots",
    "set_total_duration",
    "set_mix",
    "add_sfx",
    "reorder_clip",
    "trim_output_start",
    "remove_clip",
    "remove_music",
    "reorder_clips_by",
    "label_each_clip",
}


def test_parse_args_modes() -> None:
    args = parse_args(["--thread", THREAD, "make it pop"])
    assert args.ask == "make it pop" and not args.commit and args.battery is None
    assert parse_args(["--thread", THREAD, "--commit", "x"]).commit
    assert parse_args(["--thread", THREAD, "--battery", "b.yaml"]).battery == Path("b.yaml")
    assert parse_args(["--thread", THREAD, "--record", "n", "x"]).record == "n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--thread", THREAD],
        ["--thread", THREAD, "--battery", "b.yaml", "--commit"],
        ["--thread", THREAD, "--commit", "--record", "n", "x"],
        ["x"],
    ],
)
def test_parse_args_rejects_bad_combinations(argv) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_battery_file_is_the_36_ask_set() -> None:
    cases = load_battery(BATTERY)
    assert len(cases) == 36
    assert len({c.id for c in cases}) == 36
    assert {op for c in cases for op in c.expect_ops} <= V2_OPS
    assert any("ö" in c.ask or "ı" in c.ask for c in cases)  # Turkish present


def test_evaluate_case_rules() -> None:
    ok = AskResult(ask="a", ops=[{"op": "rewrite_text"}, {"op": "patch_text"}])
    assert evaluate_case(BatteryCase("a", ["rewrite_text"]), ok).passed
    assert not evaluate_case(BatteryCase("a", ["remove_texts"]), ok).passed
    assert not evaluate_case(BatteryCase("a", ["rewrite_text"]), AskResult("a", error="x")).passed
    assert evaluate_case(BatteryCase("a", []), AskResult("a")).passed
    assert not evaluate_case(BatteryCase("a", []), ok).passed
    honest = AskResult("a", reply="No label says Atlantis.")
    assert evaluate_case(BatteryCase("a", [], expect_error="atlantis"), honest).passed
    assert not evaluate_case(BatteryCase("a", [], expect_error="zzz"), honest).passed
    assert evaluate_case(BatteryCase("a", [], expect_error="zzz|ATLANTIS"), honest).passed
    with_ops = AskResult(ask="a", reply="no atlantis here", ops=[{"op": "remove_texts"}])
    assert not evaluate_case(BatteryCase("a", [], expect_error="atlantis"), with_ops).passed


def test_run_battery_table_and_crash_isolation() -> None:
    cases = [BatteryCase("good", ["patch_text"], id="g"), BatteryCase("boom", ["x"], id="b")]

    async def run_one(ask: str, turns: list) -> AskResult:  # noqa: ANN001
        if ask == "boom":
            raise RuntimeError("provider down")
        return AskResult(ask, ops=[{"op": "patch_text"}])

    verdicts = asyncio.run(run_battery(cases, run_one))
    assert [v.passed for v in verdicts] == [True, False]
    table = format_table(verdicts)
    assert "1/2 passed" in table and "PASS" in table and "FAIL" in table
    assert "provider down" in table


def test_diff_text_reports_change_add_remove() -> None:
    before = [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
    after = [{"id": "a", "text": "ONE"}, {"id": "c", "text": "new", "start_s": 0, "end_s": 1}]
    lines = diff_text(before, after)
    assert any(line.startswith("~ a:") and "'ONE'" in line for line in lines)
    assert any(line.startswith("- b:") for line in lines)
    assert any(line.startswith("+ c:") for line in lines)


def test_dry_run_compiles_with_mocked_copilot(monkeypatch) -> None:
    variant = _variant()
    job = _job(variant)
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True, "timeline": True},
    )
    snapshot = build_editor_snapshot(job, variant)
    seen = {}

    async def copilot(body, *, job_id):  # noqa: ANN001, ANN202
        seen["snapshot"] = body.snapshot
        seen["message"] = body.message
        return SimpleNamespace(
            intent="edit",
            outcome="proposed",
            reply="Done",
            ops=[{"op": "edit_text", "bar_index": 0, "text": "New hook"}],
            unmet_requests=[],
        )

    result = asyncio.run(
        dry_run_on_target(
            job=job,
            variant=variant,
            snapshot=snapshot,
            conversation=[],
            ask="rename the hook",
            copilot=copilot,
        )
    )
    assert result.error is None and result.op_names == ["edit_text"]
    assert any("'Old hook' -> 'New hook'" in line for line in result.text_diff)
    assert seen["message"] == "rename the hook" and seen["snapshot"] == snapshot
    # dry run never mutates the input variant
    assert variant["text_elements"][0]["text"] == "Old hook"


def test_dry_run_surfaces_compile_error(monkeypatch) -> None:
    variant = _variant()
    job = _job(variant)

    async def copilot(body, *, job_id):  # noqa: ANN001, ANN202
        return SimpleNamespace(
            intent="edit",
            outcome="proposed",
            reply="x",
            ops=[{"op": "not_a_real_op"}],
            unmet_requests=[],
        )

    result = asyncio.run(
        dry_run_on_target(
            job=job, variant=variant, snapshot={}, conversation=[], ask="x", copilot=copilot
        )
    )
    assert "not portable to Kria yet" in (result.error or "")


def test_main_refuses_non_local_database(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.config.settings.database_url", "postgresql://u@prod-db.example.com:5432/nova"
    )
    with pytest.raises(SystemExit, match="Refusing"):
        kria_ask.main(["--thread", THREAD, "hello"])


def test_seed_guided_fixture_has_clip_label_lane() -> None:
    import uuid

    from app.cli.kria_dev import _guided_fixture

    plan, variant, paths, assignments = _guided_fixture(uuid.uuid4())
    ids = [row["id"] for row in variant["text_elements"]]
    assert ids[0] == "guided-title"
    assert sum(i.startswith("clip-label-") for i in ids) >= 3
    assert len(paths) == len(assignments) and plan["variants"] == [variant]
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=plan,
        all_candidates={"clip_paths": paths},
    )
    assert build_editor_snapshot(job, variant)["slots"]


def test_seed_guided_fixture_is_guided_native_with_repeated_places(monkeypatch) -> None:
    """Realistic seed: >=3 Arnavutkoy labels, a persisted revision, reference-only song."""
    import uuid

    from app.cli.kria_dev import _guided_fixture
    from app.config import settings
    from app.services.kria_editor_ops import _is_guided_native

    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "edit_transitions_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    plan, variant, paths, _ = _guided_fixture(uuid.uuid4())
    labels = [r["text"] for r in variant["text_elements"] if r["id"].startswith("clip-label-")]
    assert sum("Arnavutköy" in text for text in labels) >= 3
    assert variant["guided_edit_revision"]["segments"]
    assert variant["music_playback_mode"] == "reference_only"
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=plan,
        all_candidates={"clip_paths": paths},
    )
    assert _is_guided_native(job, variant)
    snapshot = build_editor_snapshot(job, variant)
    assert {"clip", "transition", "text"} <= set(snapshot["allowed_op_families"])
    # Reference-only song: no music family; the copilot is told why, and no
    # original-sound control is offered (guided saves only persist music_level).
    assert "music" not in snapshot["allowed_op_families"]
    assert any("added when the creator posts" in n for n in snapshot["audio_notes"])
    assert any("footage's own sound" in n for n in snapshot["audio_notes"])


def _commit_env(monkeypatch, *, redis: str, provider: str) -> None:
    monkeypatch.setattr(
        "app.config.settings.database_url", "postgresql://u@localhost:5432/nova_dev"
    )
    monkeypatch.setattr("app.config.settings.redis_url", redis)
    monkeypatch.setattr("app.config.settings.storage_provider", provider)
    monkeypatch.setattr("app.config.settings.storage_bucket", "some-bucket")


def test_commit_refuses_non_local_redis(monkeypatch) -> None:
    _commit_env(monkeypatch, redis="redis://prod-redis.example.com:6379", provider="local")
    with pytest.raises(SystemExit, match="REDIS_URL"):
        kria_ask.main(["--thread", THREAD, "--commit", "hello"])


def test_commit_needs_yes_storage_for_non_local_storage(monkeypatch) -> None:
    _commit_env(monkeypatch, redis="redis://localhost:6379", provider="gcs")
    with pytest.raises(SystemExit, match="--yes-storage"):
        kria_ask.main(["--thread", THREAD, "--commit", "hello"])


def test_commit_proceeds_with_yes_storage_and_dry_run_is_unrestricted(monkeypatch) -> None:
    _commit_env(monkeypatch, redis="redis://localhost:6379", provider="gcs")

    async def fake_commit(_thread, _ask):
        return {"ok": True}

    monkeypatch.setattr(kria_ask, "commit_turn", fake_commit)
    assert kria_ask.main(["--thread", THREAD, "--commit", "--yes-storage", "hello"]) == 0
    # Dry-run is read-only: a prod-ish redis/storage does not block it.
    _commit_env(monkeypatch, redis="redis://prod-redis.example.com:6379", provider="gcs")

    async def fake_dry(_thread, _turns, _ask):
        return kria_ask.AskResult(ask="hello")

    monkeypatch.setattr(kria_ask, "_dry", fake_dry)
    assert kria_ask.main(["--thread", THREAD, "hello"]) == 0
