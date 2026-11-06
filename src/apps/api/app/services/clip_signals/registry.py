"""Registry of per-clip measured signals.

A signal is a small pure function over a clip's local media plus what understanding already
knows, registered under a name. Each signal owns exactly one key under the clip's
``analysis["signals"]`` (stored NEXT TO ``analysis["understanding"]``, never inside
``ClipUnderstanding``), so a new signal never rewrites another's data and a threshold change is a
version bump on one signal.

Pairs (near-duplicates) are not stored: only the per-clip hashes are, and pairs are computed when
a suggestion set is served.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SIGNALS_KEY = "signals"


@dataclass(frozen=True)
class SignalInputs:
    media_id: str
    kind: str  # "video" | "image"
    # A local copy of the clip (the analysis proxy or the original); None when the caller did
    # not materialize one, in which case a signal that needs media returns None.
    local_path: str | None = None
    duration_s: float | None = None
    # Read-only: what understanding already knows about this clip.
    analysis: Mapping[str, Any] | None = None


Compute = Callable[[SignalInputs], "Mapping[str, Any] | None"]


@dataclass(frozen=True)
class SignalSpec:
    name: str
    # Bump when the measurement or its thresholds change: stored entries of an older version are
    # treated as missing and the backfill recomputes them.
    version: int
    kinds: frozenset[str]
    compute: Compute
    implemented: bool = True


_REGISTRY: dict[str, SignalSpec] = {}


def register(spec: SignalSpec) -> SignalSpec:
    if spec.name in _REGISTRY:
        raise RuntimeError(f"signal already registered: {spec.name}")
    if not spec.name.isidentifier():
        raise ValueError("signal names are identifiers (they are stored keys)")
    _REGISTRY[spec.name] = spec
    return spec


def specs() -> tuple[SignalSpec, ...]:
    return tuple(_REGISTRY.values())


def names() -> frozenset[str]:
    return frozenset(_REGISTRY)


def get(name: str) -> SignalSpec | None:
    return _REGISTRY.get(name)


def signals_version() -> str:
    """Changes whenever any registered signal is added or bumped, so cards recompute."""
    raw = "|".join(
        f"{s.name}@{s.version}" for s in sorted(_REGISTRY.values(), key=lambda s: s.name)
    )
    return f"sv_{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


def read_signals(analysis: object) -> dict[str, Mapping[str, Any]]:
    """The current-version signals stored on an analysis, ``{name: data}``. Stale or unknown
    entries are left out: a reader never trusts a measurement taken under old thresholds."""
    stored = analysis.get(SIGNALS_KEY) if isinstance(analysis, Mapping) else None
    if not isinstance(stored, Mapping):
        return {}
    out: dict[str, Mapping[str, Any]] = {}
    for name, entry in stored.items():
        spec = _REGISTRY.get(name)
        if (
            spec is not None
            and isinstance(entry, Mapping)
            and entry.get("v") == spec.version
            and isinstance(entry.get("data"), Mapping)
        ):
            out[name] = entry["data"]
    return out


def missing_signals(analysis: object, kind: str) -> list[str]:
    """Registered signals that apply to ``kind`` and have no current-version entry."""
    have = read_signals(analysis)
    return [s.name for s in specs() if kind in s.kinds and s.name not in have]


def merge_signals(analysis: object, computed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A new analysis dict with ``computed`` merged under ``signals``.

    Each signal owns its key: an entry already stored for another signal is carried over
    untouched and no other analysis key changes.
    """
    base = dict(analysis) if isinstance(analysis, Mapping) else {}
    stored = dict(base.get(SIGNALS_KEY) or {}) if isinstance(base.get(SIGNALS_KEY), Mapping) else {}
    for name, data in computed.items():
        spec = _REGISTRY.get(name)
        if spec is None:
            raise KeyError(f"unregistered signal: {name}")
        stored[name] = {"v": spec.version, "data": dict(data)}
    base[SIGNALS_KEY] = stored
    return base


def compute_signals(inputs: SignalInputs, *, only: frozenset[str] | None = None) -> dict[str, Any]:
    """Run every applicable registered signal. A signal that raises or returns None is skipped:
    one bad measurement never takes the others (or the caller) down."""
    out: dict[str, Any] = {}
    for spec in specs():
        if inputs.kind not in spec.kinds or (only is not None and spec.name not in only):
            continue
        try:
            data = spec.compute(inputs)
        except Exception:  # noqa: BLE001 - a signal is an enhancement, never a gate
            continue
        if data is not None:
            out[spec.name] = dict(data)
    return out
