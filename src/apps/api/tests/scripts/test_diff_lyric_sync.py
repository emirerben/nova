"""Unit tests for cookie-aware yt-dlp subprocess use in diff_lyric_sync."""

from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import diff_lyric_sync

COOKIE_TEXT = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session\n"


def _cookie_b64(text: str = COOKIE_TEXT) -> str:
    return base64.b64encode(text.encode()).decode()


def test_yt_dlp_download_without_cookie_omits_cookie_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("YTDLP_COOKIES_PATH", raising=False)
    monkeypatch.delenv("YTDLP_COOKIES_B64", raising=False)
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["pass_fds"] = kwargs.get("pass_fds")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"video")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(diff_lyric_sync, "_run", fake_run)

    diff_lyric_sync._yt_dlp_download("https://www.youtube.com/watch?v=abc", tmp_path / "yt.mp4")

    assert "--cookies" not in captured["cmd"]
    assert captured["pass_fds"] == ()


def test_yt_dlp_download_passes_b64_cookie_to_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("YTDLP_COOKIES_PATH", raising=False)
    monkeypatch.setenv("YTDLP_COOKIES_B64", _cookie_b64())
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        cookie_arg = Path(cmd[cmd.index("--cookies") + 1])
        captured["cookie_text"] = cookie_arg.read_text()
        captured["pass_fds"] = kwargs.get("pass_fds")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"video")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(diff_lyric_sync, "_run", fake_run)

    diff_lyric_sync._yt_dlp_download("https://www.youtube.com/watch?v=abc", tmp_path / "yt.mp4")

    assert captured["cookie_text"] == COOKIE_TEXT
    assert isinstance(captured["pass_fds"], tuple)


def test_yt_dlp_download_rejects_bad_cookie_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(COOKIE_TEXT)
    monkeypatch.setenv("YTDLP_COOKIES_PATH", str(cookie_file))
    monkeypatch.setenv("YTDLP_COOKIES_B64", _cookie_b64())

    with pytest.raises(RuntimeError, match="cookie configuration is invalid"):
        diff_lyric_sync._yt_dlp_download(
            "https://www.youtube.com/watch?v=abc",
            tmp_path / "yt.mp4",
        )


# ── .env loading must be a main()-time action, never an import side effect ───
#
# Regression guard for the 2026-07 cross-test pollution incident: importing
# this module at pytest collection used to merge the developer's real .env
# into os.environ for the whole session, so any pydantic-invalid value there
# (e.g. `FLAG=true  # inline comment`) failed every test that builds a fresh
# Settings() — tests/test_config.py + test_feature_flag_default_is_false —
# in full-tree runs only. The tests below import a copy of the script next to
# a canary .env so they stay hermetic (independent of the machine's real .env).

_CANARY_KEY = "NOVA_DIFF_LYRIC_SYNC_LEAK_CANARY"
_COMMENT_KEY = "NOVA_DIFF_LYRIC_SYNC_COMMENT_CANARY"
_QUOTED_KEY = "NOVA_DIFF_LYRIC_SYNC_QUOTED_CANARY"
_SQUOTE_KEY = "NOVA_DIFF_LYRIC_SYNC_SQUOTE_CANARY"
_GLUED_KEY = "NOVA_DIFF_LYRIC_SYNC_GLUED_CANARY"
_UNCLOSED_KEY = "NOVA_DIFF_LYRIC_SYNC_UNCLOSED_CANARY"
_UNCLOSED_COMMENT_KEY = "NOVA_DIFF_LYRIC_SYNC_UNCLOSED_COMMENT_CANARY"
_MIDQUOTE_KEY = "NOVA_DIFF_LYRIC_SYNC_MIDQUOTE_CANARY"
_EMPTY_KEY = "NOVA_DIFF_LYRIC_SYNC_EMPTY_CANARY"
_COMMENT_ONLY_KEY = "NOVA_DIFF_LYRIC_SYNC_COMMENT_ONLY_CANARY"
_HASH_KEY = "NOVA_DIFF_LYRIC_SYNC_HASH_CANARY"
_ALL_CANARY_KEYS = (
    _CANARY_KEY,
    _COMMENT_KEY,
    _QUOTED_KEY,
    _SQUOTE_KEY,
    _GLUED_KEY,
    _UNCLOSED_KEY,
    _UNCLOSED_COMMENT_KEY,
    _MIDQUOTE_KEY,
    _EMPTY_KEY,
    _COMMENT_ONLY_KEY,
    _HASH_KEY,
)

# Everything the guard helper registers/mutates, so _canary_cleanup restores
# exactly what was created — no hardcoded name coupling with the test bodies.
_GUARD_MODULE_NAMES: list[str] = []


def _import_script_copy_with_canary_env(tmp_path: Path, module_name: str):
    """Copy the real script under tmp_path/scripts/ with a canary .env above it.

    The loader walks up from the script file's own location, so the copy can
    only ever find the canary .env — never the developer's real one.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    copy = scripts_dir / f"{module_name}.py"
    copy.write_text(Path(diff_lyric_sync.__file__).read_text())
    (tmp_path / ".env").write_text(
        f"{_CANARY_KEY}=tripped\n"
        f"{_COMMENT_KEY}=true   # inline comment must be stripped\n"
        f'{_QUOTED_KEY}="quoted value"  # comment after close quote\n'
        f"{_SQUOTE_KEY}='single quoted'  # single-quote arm\n"
        f'{_GLUED_KEY}="s3cr3t"# comment glued to the close quote\n'
        f'{_UNCLOSED_KEY}="unclosed\n'
        f'{_UNCLOSED_COMMENT_KEY}="unclosed # with a comment\n'
        f'{_MIDQUOTE_KEY}="abc"def\n'
        f"{_EMPTY_KEY}=\n"
        f"{_COMMENT_ONLY_KEY}= # only a comment after the =\n"
        f"{_HASH_KEY}=abc#def\n"
    )
    spec = importlib.util.spec_from_file_location(module_name, copy)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # dataclass creation resolves via sys.modules
    _GUARD_MODULE_NAMES.append(module_name)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def _canary_cleanup():
    """The module-under-copy mutates os.environ/sys.* outside monkeypatch's
    view, so restore by hand."""
    saved_path = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for key in _ALL_CANARY_KEYS:
            os.environ.pop(key, None)
        for name in _GUARD_MODULE_NAMES:
            sys.modules.pop(name, None)
        _GUARD_MODULE_NAMES.clear()


@pytest.mark.usefixtures("_canary_cleanup")
def test_import_does_not_mutate_environ(tmp_path: Path) -> None:
    """Importing the module (as pytest collection does) must not load .env."""
    _import_script_copy_with_canary_env(tmp_path, "diff_lyric_sync_import_guard")

    assert _CANARY_KEY not in os.environ, (
        "importing scripts/diff_lyric_sync loaded a .env into os.environ — "
        "this poisons every fresh Settings() in full-tree pytest runs"
    )


@pytest.mark.usefixtures("_canary_cleanup")
def test_main_loads_env_and_strips_inline_comments(tmp_path: Path) -> None:
    """CLI behavior preserved: main() loads .env, dotenv comment semantics."""
    mod = _import_script_copy_with_canary_env(tmp_path, "diff_lyric_sync_main_guard")

    with pytest.raises(SystemExit):
        mod.main(["--help"])  # env load happens before argparse exits

    assert os.environ.get(_CANARY_KEY) == "tripped"
    assert os.environ.get(_COMMENT_KEY) == "true"
    # Quoted value: comment after the close quote dropped, quotes stripped.
    assert os.environ.get(_QUOTED_KEY) == "quoted value"
    assert os.environ.get(_SQUOTE_KEY) == "single quoted"
    # A "#" glued to the close quote still starts a comment.
    assert os.environ.get(_GLUED_KEY) == "s3cr3t"
    # An unclosed quote is malformed — the value is kept verbatim...
    assert os.environ.get(_UNCLOSED_KEY) == '"unclosed'
    # ...but a whitespace-preceded "#" after it is still cut as a comment.
    assert os.environ.get(_UNCLOSED_COMMENT_KEY) == '"unclosed'
    # A close quote glued to more text does not end the value — kept verbatim,
    # matching scripts/admin.py::load_env (python-dotenv skips the line).
    assert os.environ.get(_MIDQUOTE_KEY) == '"abc"def'
    assert os.environ.get(_EMPTY_KEY) == ""
    # `KEY= # note` keeps the literal text: once the value is stripped the "#"
    # has no whitespace before it. Odd, but byte-identical to python-dotenv —
    # and therefore to what pydantic-settings feeds the API from the same file.
    assert os.environ.get(_COMMENT_ONLY_KEY) == "# only a comment after the ="
    # "#" with no preceding whitespace is part of the value, never a comment.
    assert os.environ.get(_HASH_KEY) == "abc#def"
