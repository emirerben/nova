"""KRI-1614: the reference teardowns (`prompts/intro_teardowns.json`) as a validated, versioned
file a person approves into. Dev-time only: nothing at runtime imports this module.

A teardown is evidence of taste, not a template: the footage it suits, why it works, an EN and a TR
`IntroScene` (option ids and one voice per title word, nothing else) and the moves today's engine
lacks. The example titles are fixtures for the render check and never reach a screen at runtime.

Ways this could go wrong, and the guard for each (tests/pipeline/test_intro_intake.py):

* T1 a teardown that does not hold together reaches the file: an "expressible" one that names a
  missing move, a partial one that names none, an example scene the server would not issue for its
  title. `Teardown` refuses all of them.
* T2 the approved file is reformatted or reordered: `dumps` writes the exact bytes the file has
  today, so an approval changes only its own entry and the version.
* T3 an id is reused or skipped: `add_teardown` takes the next free ``refN`` only.
* T4 the same missing move is filed as a new engine ticket every time: `new_engine_gaps` drops a
  move the file already names (in any teardown or in ``engine_limits``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pipeline import intro_scene as scene_mod

TEARDOWNS_PATH = Path(__file__).resolve().parents[2] / "prompts" / "intro_teardowns.json"
ID_PATTERN = re.compile(r"^ref(\d+)$")
LANGUAGES = ("en", "tr")


class Move(BaseModel):
    model_config = ConfigDict(extra="forbid")

    piece: str = Field(min_length=1, max_length=200)
    move: str = Field(min_length=1, max_length=500)


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    scene: scene_mod.IntroScene


class Teardown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=80)
    reference_image: str = Field(min_length=1, max_length=200)
    expressible: Literal["yes", "partial", "no"]
    footage_fit: str = Field(min_length=1, max_length=400)
    footage_plate: Literal["light_wall", "dark_room"]
    why_it_works: str = Field(min_length=1, max_length=400)
    missing_moves: list[Move]
    approximations: list[Move] | None = None
    examples: dict[Literal["en", "tr"], Example]
    approved_by: str | None = Field(default=None, max_length=80)
    approved_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def _holds_together(self) -> Teardown:
        if self.expressible == "yes" and self.missing_moves:
            raise ValueError("an expressible reference names no missing move")
        if self.expressible != "yes" and not self.missing_moves:
            raise ValueError("a partial or inexpressible reference names the missing move")
        if self.expressible == "no":
            if self.examples:
                raise ValueError("an inexpressible reference has no example scene")
            return self
        if set(self.examples) != set(LANGUAGES):
            raise ValueError("an expressible reference has an EN and a TR example")
        for language, example in self.examples.items():
            reason = scene_mod.check_scene(
                example.scene, example.title, scene_mod.issue_menu(example.title)
            )
            if reason:
                raise ValueError(f"{language} example: {reason}")
        return self


# --- the file --------------------------------------------------------------------------------

_SCALAR_ARRAY = re.compile(r'\[\n((?:[ ]+"[^"\n]*",?\n)+)[ ]*\]')


def _inline(match: re.Match[str]) -> str:
    return "[" + ", ".join(line.strip().rstrip(",") for line in match.group(1).splitlines()) + "]"


def dumps(data: dict[str, Any]) -> str:
    """The file's own format: two-space indent, real UTF-8, arrays of strings on one line."""
    return _SCALAR_ARRAY.sub(_inline, json.dumps(data, indent=2, ensure_ascii=False)) + "\n"


def load(path: Path = TEARDOWNS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(data: dict[str, Any]) -> list[Teardown]:
    """Every teardown, validated. Raises on the first that does not hold together."""
    teardowns = [Teardown.model_validate(t) for t in data["teardowns"]]
    ids = [t.id for t in teardowns]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate teardown id")
    return teardowns


def next_id(data: dict[str, Any]) -> str:
    numbers = [int(ID_PATTERN.match(t["id"]).group(1)) for t in data["teardowns"]]  # type: ignore[union-attr]
    return f"ref{max(numbers, default=0) + 1}"


def teardown_dict(teardown: Teardown) -> dict[str, Any]:
    """The entry as the file holds it: key order of the model, absent optionals left out."""
    return teardown.model_dump(mode="json", exclude_none=True)


def add_teardown(data: dict[str, Any], teardown: Teardown, *, today: str) -> dict[str, Any]:
    """A copy of ``data`` with ``teardown`` appended and the version bumped. Refuses an id that is
    not the next free ``refN`` (T3)."""
    if teardown.id != next_id(data):
        raise ValueError(f"the next free id is {next_id(data)}, not {teardown.id}")
    version = f"{today}-intake-{teardown.id}"
    return {**data, "version": version, "teardowns": [*data["teardowns"], teardown_dict(teardown)]}


# --- engine gaps -----------------------------------------------------------------------------


def _key(piece: str) -> str:
    return " ".join(piece.casefold().split())


def known_gaps(data: dict[str, Any]) -> set[str]:
    keys = {_key(limit["piece"]) for limit in data.get("engine_limits", [])}
    for teardown in data["teardowns"]:
        keys.update(_key(move["piece"]) for move in teardown.get("missing_moves", []))
    return keys


def new_engine_gaps(data: dict[str, Any], teardown: Teardown) -> list[Move]:
    """The moves ``teardown`` names that no teardown or engine limit in ``data`` names yet (T4)."""
    known = known_gaps(data)
    seen: set[str] = set()
    fresh = []
    for move in teardown.missing_moves:
        key = _key(move.piece)
        if key not in known and key not in seen:
            seen.add(key)
            fresh.append(move)
    return fresh
