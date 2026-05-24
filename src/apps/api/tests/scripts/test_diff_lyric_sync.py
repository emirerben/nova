"""Unit tests for cookie-aware yt-dlp subprocess use in diff_lyric_sync."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from scripts import diff_lyric_sync

COOKIE_TEXT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session\n"
)


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
