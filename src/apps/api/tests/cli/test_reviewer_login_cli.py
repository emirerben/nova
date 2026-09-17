"""Tests for app.cli.reviewer_login — the KRI-111 demo-account hash minter."""

from __future__ import annotations

import io

from app.cli import reviewer_login as cli
from app.services.reviewer_login import verify_password


def test_stdin_path_strips_one_trailing_newline_and_hashes(capsys):
    rc = cli.main(["hash", "--stdin"], stdin=io.StringIO("correct horse\n"))
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    assert verify_password("correct horse", printed) is True
    # Never leaks the plaintext.
    assert "correct horse" not in printed


def test_stdin_path_without_trailing_newline(capsys):
    rc = cli.main(["hash", "--stdin"], stdin=io.StringIO("no-newline-pw"))
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    assert verify_password("no-newline-pw", printed) is True


def test_empty_password_refused(capsys):
    rc = cli.main(["hash", "--stdin"], stdin=io.StringIO("\n"))
    assert rc == 2
    assert capsys.readouterr().out.strip() == ""


def test_interactive_prompt_reads_via_getpass(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_a, **_k: "prompted-pw")
    rc = cli.main(["hash"])
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    assert verify_password("prompted-pw", printed) is True
