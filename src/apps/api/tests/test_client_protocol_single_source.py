"""KRI-1381 guard: the app's protocol number lives in `KriaClientProtocolContract.version`
and the `client_protocol.py` constants only. A test that hard-codes the current number
turns every feature PR into a merge conflict; scripts/bump-protocol.py assigns the next one."""

from __future__ import annotations

import importlib.util
import re
import subprocess
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


def _scratch_tree(tmp_path: Path, *, git: bool = True) -> Path:
    for rel in (
        "src/apps/ios/Kria/Core/Services.swift",
        "src/apps/api/app/services/client_protocol.py",
        "src/apps/api/app/config.py",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    if git:
        _as_origin_main(tmp_path)
    config = tmp_path / "src/apps/api/app/config.py"
    text = config.read_text(encoding="utf-8")
    anchor = re.search(
        r"^[ \t]+\w*client_protocol:[ \t]*int[ \t]*=[ \t]*\d+[ \t]*\n", text, re.MULTILINE
    )
    assert anchor, (
        "config.py has no `*client_protocol: int = N` field to anchor the scratch field on"
    )
    config.write_text(
        text[: anchor.end()]
        + "    zz_feature_min_client_protocol: int = 1\n"
        + text[anchor.end() :],
        encoding="utf-8",
    )
    return tmp_path


def _as_origin_main(root: Path) -> None:
    """Commit the scratch tree and point origin/main at it, so its contents count as shipped."""
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    if not (root / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)


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


def test_shipped_config_gate_is_never_moved_by_a_feature_name_collision(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    _as_origin_main(root)
    config_before = script.read(root, script.CONFIG_FILE)
    ios_before = script.read(root, script.IOS_FILE)
    assert _apply(script, root, "zz-feature") == 1
    assert (
        _apply(script, root, "zz-feature", "--config-field", "zz_feature_min_client_protocol") == 1
    )
    assert script.read(root, script.CONFIG_FILE) == config_before
    assert script.read(root, script.IOS_FILE) == ios_before
    assert _apply(script, root, "zz-feature", "--force") == 0


def test_a_new_gate_alongside_shipped_ones_reports_exactly_what_moved(tmp_path, capsys):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    _as_origin_main(root)
    config = root / script.CONFIG_FILE
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    zz_feature_min_client_protocol: int = 1\n",
            "    zz_feature_min_client_protocol: int = 1\n"
            "    zz_new_min_client_protocol: int = 1\n",
            1,
        ),
        encoding="utf-8",
    )
    assert _apply(script, root, "zz-new") == 0
    number = script.ios_version(script.read(root, script.IOS_FILE))
    assert f"config zz_new_min_client_protocol: 1 -> {number}" in capsys.readouterr().out
    assert "zz_feature_min_client_protocol: int = 1\n" in script.read(root, script.CONFIG_FILE)


def test_colliding_features_rebase_with_one_plain_command(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    config = root / script.CONFIG_FILE
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    zz_feature_min_client_protocol: int = 1\n",
            "    zz_feature_min_client_protocol: int = 1\n"
            "    zz_beta_min_client_protocol: int = 1\n",
            1,
        ),
        encoding="utf-8",
    )
    assert (
        _apply(script, root, "zz-feature", "--leave-config-field", "zz_beta_min_client_protocol")
        == 0
    )
    taken = script.ios_version(script.read(root, script.IOS_FILE))
    py = root / script.PY_FILE
    py.write_text(
        py.read_text(encoding="utf-8") + f"\nZZ_BETA_PROTOCOL = {taken}\n", encoding="utf-8"
    )
    cfg_text = config.read_text(encoding="utf-8")
    config.write_text(
        cfg_text.replace(
            "    zz_beta_min_client_protocol: int = 1\n",
            f"    zz_beta_min_client_protocol: int = {taken}\n",
        ),
        encoding="utf-8",
    )
    assert _apply(script, root, "zz-feature") == 0
    assert script.ios_version(script.read(root, script.IOS_FILE)) == taken + 1
    assert f"zz_beta_min_client_protocol: int = {taken}\n" in script.read(root, script.CONFIG_FILE)


def test_check_fails_when_a_config_gate_is_above_the_app(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    current = script.ios_version(script.read(root, script.IOS_FILE))
    config = root / script.CONFIG_FILE
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    zz_feature_min_client_protocol: int = 1\n",
            f"    zz_feature_min_client_protocol: int = {current + 5}\n",
            1,
        ),
        encoding="utf-8",
    )
    assert any(
        "below the server's highest" in line for line in script.check_hardcoded_literals(root)
    )


def test_guard_flags_assertion_assignment_kwarg_and_quoted_forms_but_not_prose(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    current = script.ios_version(script.read(root, script.IOS_FILE))
    api_tests = root / "src/apps/api/tests"
    api_tests.mkdir(parents=True)
    (api_tests / "test_y.py").write_text(
        f"assert NEW_PROTOCOL == {current}\n"
        f'job["creator_client_protocol"] = {current}\n'
        f"make(client_protocol={current})\n"
        f"PROTOCOL = {current}\n"
        f'assert detail["minimum_client_protocol"] == {current}\n'
        f"h = str({current})  # client protocol header\n"
        f"# an app build at protocol {current} sends this\n"
        f"x = {current}  # unrelated number\n"
        f"PROTOCOL = {current}  # {script.ALLOW_LITERAL}\n",
        encoding="utf-8",
    )
    ios_tests = root / "src/apps/ios/Tests"
    ios_tests.mkdir(parents=True)
    (ios_tests / "T.swift").write_text(
        f"XCTAssertTrue(KriaClientProtocolContract.version == {current})\n"
        f'XCTAssertEqual(String(KriaClientProtocolContract.version), "{current}")\n',
        encoding="utf-8",
    )
    flagged = script.check_hardcoded_literals(root)
    assert sorted(line.split(": ", 1)[0] for line in flagged) == sorted(
        [f"src/apps/api/tests/test_y.py:{n}" for n in (1, 2, 3, 4, 5, 6)]
        + [f"src/apps/ios/Tests/T.swift:{n}" for n in (1, 2)]
    ), "\n".join(flagged)


def test_a_gate_whose_name_does_not_match_the_feature_stops_the_first_run(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    config = root / script.CONFIG_FILE
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    zz_feature_min_client_protocol: int = 1\n",
            "    zz_feature_min_client_protocol: int = 1\n"
            "    zz_card_style_min_client_protocol: int = 1\n",
            1,
        ),
        encoding="utf-8",
    )
    ios_before = script.read(root, script.IOS_FILE)
    assert _apply(script, root, "zz-widget") == 1
    assert script.read(root, script.IOS_FILE) == ios_before
    assert (
        _apply(
            script,
            root,
            "zz-widget",
            "--config-field",
            "zz_card_style_min_client_protocol",
            "--leave-config-field",
            "zz_feature_min_client_protocol",
        )
        == 0
    )
    number = script.ios_version(script.read(root, script.IOS_FILE))
    assert f"zz_card_style_min_client_protocol: int = {number}\n" in script.read(
        root, script.CONFIG_FILE
    )


def test_without_origin_main_nothing_is_renumbered_unless_forced(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path, git=False)
    before = script.read(root, script.PY_FILE)
    assert _apply(script, root, "split-blank-panel") == 1
    assert script.read(root, script.PY_FILE) == before
    assert _apply(script, root, "split-blank-panel", "--force") == 0


def test_guard_flags_the_twin_and_stand_in_forms(tmp_path):
    script = _load_bump_script()
    root = _scratch_tree(tmp_path)
    current = script.ios_version(script.read(root, script.IOS_FILE))
    api_tests = root / "src/apps/api/tests"
    api_tests.mkdir(parents=True)
    (api_tests / "test_z.py").write_text(
        f"token = bind_sending_build({current})\n"
        f"APP_BUILD = {current}\n"
        f"gates = {{{current - 1}: None, {current}: 'x'}}\n",
        encoding="utf-8",
    )
    ios_tests = root / "src/apps/ios/Tests"
    ios_tests.mkdir(parents=True)
    (ios_tests / "T.swift").write_text(
        "let header = r.value(forHTTPHeaderField: KriaClientProtocolContract.header)\n"
        f'XCTAssertEqual(header, "{current}")\n',
        encoding="utf-8",
    )
    flagged = sorted(line.split(": ", 1)[0] for line in script.check_hardcoded_literals(root))
    assert flagged == [
        "src/apps/api/tests/test_z.py:1",
        "src/apps/api/tests/test_z.py:2",
        "src/apps/api/tests/test_z.py:3",
        "src/apps/ios/Tests/T.swift:2",
    ]
