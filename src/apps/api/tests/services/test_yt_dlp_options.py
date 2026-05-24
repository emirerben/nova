"""Unit tests for shared yt-dlp option handling."""

from __future__ import annotations

import base64
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.services.yt_dlp_options import (
    YtDlpCookieConfigError,
    with_yt_dlp_cookiefile,
    with_yt_dlp_options,
)

COOKIE_TEXT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session\n"
)


@pytest.fixture(autouse=True)
def _clear_ytdlp_cookie_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", "")
    monkeypatch.setattr(settings, "ytdlp_cookies_path", "")


def _cookie_b64(text: str = COOKIE_TEXT) -> str:
    return base64.b64encode(text.encode()).decode()


def test_no_cookie_config_leaves_options_unchanged() -> None:
    base = {"quiet": True}

    with with_yt_dlp_options(base) as opts:
        assert opts == base
        assert opts is not base


def test_cookiefile_context_yields_none_without_config() -> None:
    with with_yt_dlp_cookiefile() as cookie_file:
        assert cookie_file is None


def test_cookie_path_is_passed_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(COOKIE_TEXT)
    monkeypatch.setattr(settings, "ytdlp_cookies_path", str(cookie_file))

    with with_yt_dlp_options({"quiet": True}) as opts:
        assert opts["cookiefile"] == str(cookie_file)


def test_b64_cookie_writes_0600_temp_file_and_deletes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", _cookie_b64())

    with with_yt_dlp_options({"quiet": True}, temp_dir=tmp_path) as opts:
        cookie_path = Path(opts["cookiefile"])
        assert cookie_path.exists()
        assert cookie_path.read_text() == COOKIE_TEXT
        assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600

    assert not cookie_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_b64_cookie_subprocess_safe_path_is_readable_by_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", _cookie_b64())

    with with_yt_dlp_cookiefile(temp_dir=tmp_path, subprocess_safe=True) as cookie_file:
        assert cookie_file is not None
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "import sys; "
                    "sys.stdout.write(Path(sys.argv[1]).read_text())"
                ),
                str(cookie_file.path),
            ],
            check=True,
            capture_output=True,
            text=True,
            pass_fds=cookie_file.pass_fds,
        )

    assert result.stdout == COOKIE_TEXT
    assert list(tmp_path.iterdir()) == []


def test_b64_cookie_temp_file_is_deleted_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", _cookie_b64())

    with pytest.raises(RuntimeError, match="boom"):
        with with_yt_dlp_options({"quiet": True}, temp_dir=tmp_path) as opts:
            cookie_path = Path(opts["cookiefile"])
            raise RuntimeError("boom")

    assert not cookie_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_rejects_invalid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", "not valid base64")

    with pytest.raises(YtDlpCookieConfigError, match="not valid base64"):
        with with_yt_dlp_options({}):
            pass


def test_rejects_non_netscape_cookie_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", _cookie_b64("plain text"))

    with pytest.raises(YtDlpCookieConfigError, match="Netscape"):
        with with_yt_dlp_options({}):
            pass


def test_rejects_both_cookie_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(COOKIE_TEXT)
    monkeypatch.setattr(settings, "ytdlp_cookies_path", str(cookie_file))
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", _cookie_b64())

    with pytest.raises(YtDlpCookieConfigError, match="only one"):
        with with_yt_dlp_options({}):
            pass
