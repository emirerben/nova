"""Shared yt-dlp option helpers.

YouTube occasionally challenges server-side requests with "sign in to confirm
you're not a bot". yt-dlp supports cookie files for that path, but those cookies
are account credentials. Keep the handling centralized so every caller gets the
same cleanup and logging posture.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NETSCAPE_COOKIE_HEADERS = (
    b"# HTTP Cookie File",
    b"# Netscape HTTP Cookie File",
)


class YtDlpCookieConfigError(ValueError):
    """Raised when the configured yt-dlp cookie secret cannot be used."""


@dataclass(frozen=True)
class YtDlpCookieFile:
    """Cookie file handle for native yt-dlp and subprocess callers."""

    path: Path
    pass_fds: tuple[int, ...] = ()


@contextmanager
def with_yt_dlp_options(
    base_options: Mapping[str, Any],
    *,
    temp_dir: str | os.PathLike[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield yt-dlp options with configured cookies attached.

    `YTDLP_COOKIES_PATH` is passed through directly for local/mounted-secret
    deployments. `YTDLP_COOKIES_B64` is decoded into a 0600 NamedTemporaryFile
    and removed as soon as the caller leaves this context, including exception
    paths from yt-dlp.
    """
    options = dict(base_options)
    with with_yt_dlp_cookiefile(temp_dir=temp_dir) as cookie_file:
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file.path)
        yield options


@contextmanager
def with_yt_dlp_cookiefile(
    *,
    cookie_path: str | os.PathLike[str] | None = None,
    cookie_b64: str | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
    subprocess_safe: bool = False,
    use_settings: bool = True,
) -> Iterator[YtDlpCookieFile | None]:
    """Yield a configured yt-dlp cookie file, or None when cookies are unset.

    Subprocess callers should set `subprocess_safe=True` and pass the returned
    `pass_fds` tuple into `subprocess.run`. On Linux/Fly this lets us unlink the
    named temp file before the child starts while still giving yt-dlp a readable
    `/proc/self/fd/<fd>` path.
    """
    if use_settings and cookie_path is None and cookie_b64 is None:
        cookie_path, cookie_b64 = _settings_cookie_sources()

    cookie_path_value = str(cookie_path or "").strip()
    cookie_b64_value = (cookie_b64 or "").strip()

    if cookie_path_value and cookie_b64_value:
        raise YtDlpCookieConfigError(
            "set only one of YTDLP_COOKIES_PATH or YTDLP_COOKIES_B64"
        )

    if cookie_path_value:
        path = Path(cookie_path_value).expanduser()
        if not path.is_file():
            raise YtDlpCookieConfigError(f"cookie file not found: {path}")
        yield YtDlpCookieFile(path=path)
        return

    if not cookie_b64_value:
        yield None
        return

    cookie_bytes = _decode_cookie_secret(cookie_b64_value)
    with _temporary_cookie_file(
        cookie_bytes,
        temp_dir=temp_dir,
        subprocess_safe=subprocess_safe,
    ) as cookie_file:
        yield cookie_file


def _settings_cookie_sources() -> tuple[str, str]:
    from app.config import settings

    return settings.ytdlp_cookies_path, settings.ytdlp_cookies_b64


def _decode_cookie_secret(cookie_b64: str) -> bytes:
    """Decode and lightly validate a base64-encoded Netscape cookie file."""
    try:
        cookie_bytes = base64.b64decode(cookie_b64, validate=True)
    except binascii.Error as exc:
        raise YtDlpCookieConfigError("YTDLP_COOKIES_B64 is not valid base64") from exc

    stripped = cookie_bytes.lstrip()
    if not stripped.startswith(_NETSCAPE_COOKIE_HEADERS):
        raise YtDlpCookieConfigError(
            "YTDLP_COOKIES_B64 must decode to a Netscape cookies.txt file"
        )
    return cookie_bytes


@contextmanager
def _temporary_cookie_file(
    cookie_bytes: bytes,
    *,
    temp_dir: str | os.PathLike[str] | None = None,
    subprocess_safe: bool = False,
) -> Iterator[YtDlpCookieFile]:
    """Write cookie bytes to a 0600 temp file and delete it on exit.

    On Linux, unlink the named file before yielding and hand yt-dlp the
    `/proc/self/fd/<fd>` path instead. That keeps the cookie readable by this
    process while removing the filesystem entry immediately, so even a killed
    worker does not leave a named cookie file behind.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="nova_ytdlp_cookies_",
        suffix=".txt",
        dir=temp_dir,
        delete=False,
    )
    path = Path(tmp.name)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        tmp.write(cookie_bytes)
        tmp.flush()
        try:
            os.fsync(tmp.fileno())
        except OSError:
            pass

        cookie_file = _cookie_file_for_open_file(
            tmp.fileno(),
            path,
            subprocess_safe=subprocess_safe,
        )
        yield cookie_file
    finally:
        try:
            tmp.close()
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _cookie_file_for_open_file(
    fd: int,
    path: Path,
    *,
    subprocess_safe: bool,
) -> YtDlpCookieFile:
    proc_path = Path(f"/proc/self/fd/{fd}")
    if proc_path.exists():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return YtDlpCookieFile(
            path=proc_path,
            pass_fds=(fd,) if subprocess_safe else (),
        )
    return YtDlpCookieFile(path=path)
