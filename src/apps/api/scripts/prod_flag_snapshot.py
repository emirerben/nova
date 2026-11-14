"""Machine-written snapshot of the effective production settings (KRI-1375).

``tests/prod_flags.snapshot.json`` records, for every boolean ``Settings`` field and for the
non-secret list settings that gate rollouts, the value production runs with and where that
value came from. Tests (``tests/_prod_profile.py``), the Kria scorecards and
``replay_job.py`` all read it, so a flag that production has on cannot be missing from one
hand-copied list.

Nothing here reads a Fly secret value. A value is one of:

* ``default``: the code default. Production may override it; that is not known.
* a recorded evidence source (``legacy-fly-digest-2026-10-08``, ``live:phone-render-config``
  ...): who established the value and when, written with ``set`` or ``refresh --live``.

Commands (run from ``src/apps/api``)::

    python scripts/prod_flag_snapshot.py check            # snapshot matches Settings; exit 1 if not
    python scripts/prod_flag_snapshot.py refresh          # re-derive from Settings, keep evidence
    python scripts/prod_flag_snapshot.py refresh --live   # also read prod phone-render-config
    python scripts/prod_flag_snapshot.py set NAME=VALUE ... --source "why and when"
    python scripts/prod_flag_snapshot.py unverified       # flags still at an unconfirmed default
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

for _name, _value in (
    ("STORAGE_BUCKET", "snapshot-placeholder"),
    ("DATABASE_URL", "postgresql://snapshot@localhost/snapshot"),
):
    os.environ.setdefault(_name, _value)

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
SNAPSHOT_PATH = API_ROOT / "tests" / "prod_flags.snapshot.json"
SCHEMA_VERSION = 1
DEFAULT_SOURCE = "default"
SOURCES: dict[str, str] = {
    DEFAULT_SOURCE: (
        "Code default. Production may override it through a Fly secret that was not read; "
        "treat the value as unconfirmed."
    ),
}
LIVE_SOURCE = "live:phone-render-config"
LIVE_FIELDS = {
    "phone_rendering_enabled": "phone_rendering_enabled",
    "phone_render_verified_features": "verified_features",
}


def _setting_fields() -> dict[str, Any]:
    from app.config import Settings

    fields: dict[str, Any] = {}
    for name, field in Settings.model_fields.items():
        if (
            field.annotation is bool
            or name.endswith(("_user_ids", "_rollout_percent"))
            or name == "phone_render_verified_features"
        ):
            fields[name] = field
    return fields


def _default_of(field: Any) -> Any:
    value = field.get_default(call_default_factory=True)
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_jsonable(item) for item in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def load(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write(snapshot: dict[str, Any], path: Path = SNAPSHOT_PATH) -> None:
    path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build(previous: dict[str, Any] | None) -> dict[str, Any]:
    """Re-derive from ``Settings``: defaults everywhere, recorded evidence carried forward."""

    prior = (previous or {}).get("settings", {})
    sources = dict(SOURCES)
    sources.update(
        {k: v for k, v in (previous or {}).get("sources", {}).items() if k != DEFAULT_SOURCE}
    )
    settings: dict[str, dict[str, Any]] = {}
    for name, field in _setting_fields().items():
        kept = prior.get(name)
        if kept and kept.get("source") != DEFAULT_SOURCE:
            settings[name] = {"value": kept["value"], "source": kept["source"]}
        else:
            settings[name] = {"value": _default_of(field), "source": DEFAULT_SOURCE}
    return {"schema": SCHEMA_VERSION, "sources": sources, "settings": settings}


def apply_evidence(
    snapshot: dict[str, Any], values: dict[str, Any], *, source: str, description: str | None
) -> None:
    known = _setting_fields()
    for name, value in values.items():
        if name not in known:
            raise SystemExit(f"unknown or out-of-scope setting: {name}")
        snapshot["settings"][name] = {"value": _jsonable(value), "source": source}
    if description:
        snapshot["sources"][source] = description


def read_live_phone_config() -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "admin.py"),
            "--prod",
            "GET",
            "kria/phone-render-config",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"prod phone-render-config read failed: {completed.stdout}{completed.stderr}"
        )
    return json.loads(completed.stdout)


CLAIM_SOURCES = ("CLAUDE.md", "agents/DECISIONS.md", "docs/runbooks", "docs/pipelines")
ON_IN_PROD = re.compile(r"\b(?:ON|on|LIVE|live) in prod(?:uction)?\b")
FLAG_NAME = re.compile(r"`([A-Z][A-Z0-9_]{3,})`")


def documented_on_in_prod() -> dict[str, str]:
    """Settings the repo's own docs say are ON in production, with the line that says so.

    Matches the "ON in prod" phrasing on a line that names the flag in backticks. Other
    wordings (an applied ``fly secrets set`` in a decision record) are recorded by hand with
    ``set``; this catches the common case so a documented value cannot be ignored.
    """

    names = set(_setting_fields())
    claims: dict[str, str] = {}
    for entry in CLAIM_SOURCES:
        root = REPO_ROOT / entry
        files = [root] if root.is_file() else sorted(root.glob("*.md"))
        for path in files:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not ON_IN_PROD.search(line):
                    continue
                for flag in FLAG_NAME.findall(line):
                    if flag.lower() in names:
                        claims.setdefault(flag.lower(), f"{path.relative_to(REPO_ROOT)}:{number}")
    return claims


def problems(snapshot: dict[str, Any]) -> list[str]:
    """Why the committed snapshot no longer describes the code, empty when it does."""

    found: list[str] = []
    if snapshot.get("schema") != SCHEMA_VERSION:
        found.append(f"schema {snapshot.get('schema')!r} != {SCHEMA_VERSION}")
    fields = _setting_fields()
    entries = snapshot.get("settings", {})
    for name in sorted(set(fields) - set(entries)):
        found.append(f"{name}: new setting, missing from the snapshot")
    for name in sorted(set(entries) - set(fields)):
        found.append(f"{name}: removed or out of scope, still in the snapshot")
    for name, field in fields.items():
        entry = entries.get(name)
        if not entry:
            continue
        if entry.get("source") not in snapshot.get("sources", {}):
            found.append(f"{name}: source {entry.get('source')!r} is not described")
        if entry.get("source") == DEFAULT_SOURCE and entry.get("value") != _default_of(field):
            found.append(
                f"{name}: code default is now {_default_of(field)!r}, snapshot has "
                f"{entry.get('value')!r}"
            )
        if field.annotation is bool and not isinstance(entry.get("value"), bool):
            found.append(f"{name}: expected a boolean, found {entry.get('value')!r}")
    for name, where in documented_on_in_prod().items():
        entry = entries.get(name)
        if entry and entry.get("value") is not True:
            found.append(
                f"{name}: {where} says it is ON in prod, snapshot has {entry.get('value')!r} "
                f"(source {entry.get('source')!r}); record it with `set {name.upper()}=true`"
            )
    return found


def unverified(snapshot: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, entry in snapshot["settings"].items()
        if entry["source"] == DEFAULT_SOURCE and isinstance(entry["value"], bool)
    )


def _parse_assignment(text: str) -> tuple[str, Any]:
    name, sep, raw = text.partition("=")
    if not sep:
        raise SystemExit(f"expected NAME=VALUE, got {text!r}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return name.strip().lower(), value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("unverified")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--live", action="store_true", help="read prod phone-render-config")
    setter = sub.add_parser("set")
    setter.add_argument("assignments", nargs="+")
    setter.add_argument("--source", required=True)
    setter.add_argument("--describe", default=None)
    args = parser.parse_args(argv)

    previous = load() if SNAPSHOT_PATH.exists() else None
    if args.command == "check":
        if previous is None:
            print(f"missing {SNAPSHOT_PATH}", file=sys.stderr)
            return 1
        found = problems(previous)
        for line in found:
            print(line, file=sys.stderr)
        if found:
            print(
                "refresh: python scripts/prod_flag_snapshot.py refresh [--live]  "
                "(tests/_prod_profile.py docstring explains evidence)",
                file=sys.stderr,
            )
        return 1 if found else 0
    if args.command == "unverified":
        names = unverified(previous or build(None))
        print("\n".join(names))
        print(
            f"{len(names)} boolean flags are still at an unconfirmed code default", file=sys.stderr
        )
        return 0
    snapshot = build(previous)
    if args.command == "refresh" and args.live:
        live = read_live_phone_config()
        apply_evidence(
            snapshot,
            {name: live[key] for name, key in LIVE_FIELDS.items()},
            source=LIVE_SOURCE,
            description=(
                "GET /admin/kria/phone-render-config on the production API (names only, "
                "no secret value); re-read on every `refresh --live`."
            ),
        )
    if args.command == "set":
        apply_evidence(
            snapshot,
            dict(_parse_assignment(item) for item in args.assignments),
            source=args.source,
            description=args.describe,
        )
    write(snapshot)
    print(f"wrote {SNAPSHOT_PATH} ({len(snapshot['settings'])} settings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
