#!/usr/bin/env python3
"""Operator entrypoint; read root .env without printing credentials, default offline."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src/apps/api"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(root / ".env", override=False)
from app.cli.compare_creator_models import main  # noqa: E402

if __name__ == "__main__":
    # Keep validation errors from printing private source text.
    try:
        main()
    except Exception as exc:
        print({"status": "blocked", "error_class": type(exc).__name__})
        raise SystemExit(1) from None
