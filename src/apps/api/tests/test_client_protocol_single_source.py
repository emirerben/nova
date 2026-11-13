"""KRI-1381 guard: the app's protocol number lives in `KriaClientProtocolContract.version`
and the `client_protocol.py` constants only. A test that hard-codes the current number
turns every feature PR into a merge conflict; scripts/bump-protocol.py assigns the next one."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_bump_script():
    path = REPO_ROOT / "scripts" / "bump-protocol.py"
    spec = importlib.util.spec_from_file_location("bump_protocol", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_test_hard_codes_the_current_client_protocol():
    problems = _load_bump_script().check_hardcoded_literals(REPO_ROOT)
    assert not problems, "\n".join(problems)


def _scratch_tree(tmp_path: Path) -> Path:
    for rel in (
        "src/apps/ios/Kria/Core/Services.swift",
        "src/apps/api/app/services/client_protocol.py",
        "src/apps/api/app/config.py",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    config = tmp_path / "src/apps/api/app/config.py"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    color_moments_min_client_protocol: int = 48\n",
            "    color_moments_min_client_protocol: int = 48\n"
            "    zz_feature_min_client_protocol: int = 1\n",
            1,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _apply(script, root: Path, *args: str) -> int:
    return script.main([*args, "--root", str(root)])


def test_rerun_without_config_field_keeps_the_gate_with_the_feature(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    assert _apply(script, root, "zz-feature") == 0
    assert _apply(script, root, "zz-feature") == 0
    number = script.ios_version(script.read(root, script.IOS_FILE))
    config = script.read(root, script.CONFIG_FILE)
    assert f"zz_feature_min_client_protocol: int = {number}\n" in config
    assert f"ZZ_FEATURE_PROTOCOL = {number}\n" in script.read(root, script.PY_FILE)


def test_unnamed_config_field_holding_the_old_number_stops_the_run(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    assert _apply(script, root, "zz-feature") == 0
    number = script.ios_version(script.read(root, script.IOS_FILE))
    config = root / script.CONFIG_FILE
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"    zz_feature_min_client_protocol: int = {number}\n",
            f"    zz_feature_min_client_protocol: int = {number}\n"
            f"    zz_other_gate_client_protocol: int = {number}\n",
            1,
        ),
        encoding="utf-8",
    )
    assert _apply(script, root, "zz-feature") == 1
    assert script.ios_version(script.read(root, script.IOS_FILE)) == number


def test_guard_catches_every_form_of_hard_coding_the_current_number(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    current = script.ios_version(script.read(root, script.IOS_FILE))
    api_tests = root / "src/apps/api/tests"
    api_tests.mkdir(parents=True)
    (api_tests / "test_x.py").write_text(
        f"set_client_protocol({current})\n"
        f"assert S.model_fields['f'].default == {current}\n"
        f"d = {{'minimum_client_protocol': {current}}}\n"
        f"c = [({current}, True)]  # client_protocol\n",
        encoding="utf-8",
    )
    ios_tests = root / "src/apps/ios/Tests"
    ios_tests.mkdir(parents=True)
    (ios_tests / "T.swift").write_text(
        f"XCTAssertEqual({current}, KriaClientProtocolContract.version)\n"
        "XCTAssertEqual(r.value(forHTTPHeaderField: KriaClientProtocolContract.header), "
        f'"{current}")\n'
        f"f(appProtocol: {current + 1})\n"
        f'XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, {current}, "floor")\n',
        encoding="utf-8",
    )
    flagged = script.check_hardcoded_literals(root)
    assert len(flagged) == 7, "\n".join(flagged)
    assert not any("T.swift:4" in line for line in flagged)
