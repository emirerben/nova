"""Unit tests for load_env in the repo-root admin CLI (scripts/admin.py).

Pins the dotenv inline-comment semantics: an unquoted value is cut at a
whitespace-preceded `#`, so `ADMIN_API_KEY=abc  # note` yields `abc` instead of
the auth-breaking `abc  # note`. Quoted values keep `#` literally.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_ADMIN_PATH = _REPO_ROOT / "scripts" / "admin.py"
_SPEC = importlib.util.spec_from_file_location("admin_cli", _ADMIN_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
admin_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(admin_cli)


def _load(tmp_path: Path, text: str) -> dict[str, str]:
    env_file = tmp_path / ".env"
    env_file.write_text(text)
    return admin_cli.load_env(env_file)


def test_unquoted_value_cut_at_inline_comment(tmp_path: Path) -> None:
    env = _load(tmp_path, "ADMIN_API_KEY=abc  # my note\n")
    assert env["ADMIN_API_KEY"] == "abc"


def test_real_world_worktree_env_line(tmp_path: Path) -> None:
    env = _load(
        tmp_path,
        "GENERATIVE_FAST_REBURN_ENABLED=true   # set to false to disable fast reburn\n",
    )
    assert env["GENERATIVE_FAST_REBURN_ENABLED"] == "true"


def test_hash_without_preceding_whitespace_is_kept(tmp_path: Path) -> None:
    env = _load(tmp_path, "PASSWORD=abc#def\n")
    assert env["PASSWORD"] == "abc#def"


def test_quoted_value_keeps_hash_literally(tmp_path: Path) -> None:
    env = _load(tmp_path, 'TOKEN="abc # not a comment"\n')
    assert env["TOKEN"] == "abc # not a comment"


def test_single_quoted_value_keeps_hash_literally(tmp_path: Path) -> None:
    env = _load(tmp_path, "TOKEN='abc # not a comment'\n")
    assert env["TOKEN"] == "abc # not a comment"


def test_quoted_value_with_trailing_comment_drops_quotes_and_comment(tmp_path: Path) -> None:
    env = _load(tmp_path, 'ADMIN_API_KEY="s3cr3t" # rotated 2026-07\n')
    assert env["ADMIN_API_KEY"] == "s3cr3t"


def test_single_quoted_value_with_trailing_comment(tmp_path: Path) -> None:
    env = _load(tmp_path, "TOKEN='abc'  # note\n")
    assert env["TOKEN"] == "abc"


def test_comment_containing_quotes_does_not_extend_value(tmp_path: Path) -> None:
    env = _load(tmp_path, 'TOKEN="abc" # see "docs"\n')
    assert env["TOKEN"] == "abc"


def test_comment_glued_to_closing_quote(tmp_path: Path) -> None:
    env = _load(tmp_path, 'ADMIN_API_KEY="s3cr3t"# rotated 2026-07\n')
    assert env["ADMIN_API_KEY"] == "s3cr3t"


def test_tab_before_hash_counts_as_comment(tmp_path: Path) -> None:
    env = _load(tmp_path, "KEY=abc\t# note\n")
    assert env["KEY"] == "abc"


def test_crlf_lines_parse_cleanly(tmp_path: Path) -> None:
    env = _load(tmp_path, "KEY=abc  # note\r\nOTHER=plain\r\n")
    assert env == {"KEY": "abc", "OTHER": "plain"}


def test_plain_values_and_full_line_comments_unchanged(tmp_path: Path) -> None:
    env = _load(
        tmp_path,
        "# full-line comment\n\nADMIN_API_KEY=plain\nOTHER=  spaced  \n",
    )
    assert env == {"ADMIN_API_KEY": "plain", "OTHER": "spaced"}


def test_missing_env_file_returns_empty(tmp_path: Path) -> None:
    assert admin_cli.load_env(tmp_path / "nope.env") == {}


def test_unterminated_quote_falls_back_to_comment_cut(tmp_path: Path) -> None:
    env = _load(tmp_path, 'KEY="abc # note\n')
    assert env["KEY"] == '"abc'


def test_quote_followed_by_non_whitespace_kept_verbatim(tmp_path: Path) -> None:
    env = _load(tmp_path, 'KEY="abc"def\n')
    assert env["KEY"] == '"abc"def'
