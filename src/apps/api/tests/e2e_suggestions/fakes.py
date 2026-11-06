"""The two things the end-to-end run replaces: the Redis behind the suggestion store and the model.

Both are narrow on purpose. ``FakeRedis`` implements exactly the commands ``suggestion_store`` uses,
so every key, lock, freeze and counter in the store is the real code. ``CassetteClient`` answers the
real ``SuggestionAgent`` call the way a strict-schema model does: it can only pick a kind and a clip
handle that the call's own ``response_schema`` lists, so a cassette written for a full thread keeps
working after a card is applied (its kind is no longer issued) without a separate answer per state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class FakeRedis:
    """``get / set(nx, ex) / incr / expire / delete`` over a dict, with a clock the test advances.

    Expiry matters here: the freshness debounce and the single-flight locks are TTL keys, and a
    creator taps seconds apart, not microseconds. ``advance`` is "time passed".
    """

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expires: dict[str, float] = {}
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key: str) -> bool:
        if key in self.expires and self.expires[key] <= self.now:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def get(self, key: str) -> bytes | None:
        return self.data[key] if self._live(key) else None

    def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and self._live(key):
            return None
        self.data[key] = value if isinstance(value, bytes) else str(value).encode()
        if ex is not None:
            self.expires[key] = self.now + ex
        else:
            self.expires.pop(key, None)
        return True

    def incr(self, key: str) -> int:
        value = int(self.get(key) or b"0") + 1
        self.data[key] = str(value).encode()
        return value

    def expire(self, key: str, seconds: int) -> bool:
        if not self._live(key):
            return False
        self.expires[key] = self.now + seconds
        return True

    def delete(self, key: str) -> int:
        existed = self._live(key)
        self.data.pop(key, None)
        self.expires.pop(key, None)
        return 1 if existed else 0


@dataclass
class CassetteClient:
    """A model that answers with a fixed preference list, restricted to what the call allows.

    ``preferences`` is the cassette: ``{kind, targets, why, copy}`` in the order the model
    prefers. The call's strict ``response_schema`` closes the kinds and clip handles, so a
    preference outside it is simply not a possible answer, exactly as under constrained decoding.
    """

    preferences: list[dict[str, Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def invoke(self, **kwargs: Any):  # noqa: ANN201
        from app.agents._runtime import ModelInvocation  # noqa: PLC0415

        self.calls.append(kwargs)
        item = kwargs["response_schema"]["properties"]["ranked"]["items"]["properties"]
        kinds = set(item["kind"]["enum"])
        handles = set(item["target_ids"]["items"]["enum"])
        ranked: list[dict[str, Any]] = []
        for pref in self.preferences:
            targets = list(pref.get("targets") or [])
            if pref["kind"] not in kinds or not set(targets) <= handles:
                continue
            ranked.append(
                {
                    "kind": pref["kind"],
                    "target_ids": targets,
                    "why": pref.get("why", ""),
                    "copy": pref.get("copy"),
                    "rank": len(ranked) + 1,
                }
            )
        return ModelInvocation(
            raw_text=json.dumps({"ranked": ranked[:5]}, ensure_ascii=False),
            tokens_in=0,
            tokens_out=0,
            raw_response=None,
        )
