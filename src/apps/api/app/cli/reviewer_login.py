"""Mint a `REVIEWER_LOGIN_PASSWORD_HASH` value for the Apple Beta App Review
demo account (KRI-111).

    python -m app.cli.reviewer_login hash                 # interactive prompt
    echo -n 'the password' | python -m app.cli.reviewer_login hash --stdin

Prints ONLY the hash to stdout (never the plaintext) so it can be piped
straight into `fly secrets set REVIEWER_LOGIN_PASSWORD_HASH=$(...)`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import TextIO

from app.services.reviewer_login import hash_password


def _read_password(*, use_stdin: bool, stdin: TextIO) -> str:
    if use_stdin:
        raw = stdin.readline()
        # Strip exactly one trailing newline, matching shell `echo` / heredoc
        # input — a password that genuinely ends in "\n" is not a realistic
        # case here and stripping more would silently mangle it.
        if raw.endswith("\n"):
            raw = raw[:-1]
        return raw
    return getpass.getpass("Reviewer account password: ")


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    hash_parser = commands.add_parser("hash", help="Hash a password and print it to stdout")
    hash_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the password from stdin instead of an interactive prompt",
    )
    args = parser.parse_args(argv)

    password = _read_password(use_stdin=args.stdin, stdin=stdin or sys.stdin)
    if not password:
        print("Refusing to hash an empty password", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
