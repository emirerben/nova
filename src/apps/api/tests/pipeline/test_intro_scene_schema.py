"""KRI-1610: the IntroScene schema never reaches the screen as text (S1, S4).

Skia-free on purpose: this file imports only the schema, so it runs in every CI job.
Failure modes for the whole feature are listed in test_intro_scene.py.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from app.pipeline import intro_scene as mod

EN_VOICES = ["solid"] * 4 + ["accent"] * 2


def _compose(voices, **over: Any) -> mod.IntroScene:
    base: dict[str, Any] = {
        "decision": "compose",
        "voices": list(voices),
        "font_pair": "grotesk_hand",
        "ink": "oxblood",
        "zone": "above_head",
        "entrance": "fade",
        "sound": "accent-pop",
    }
    base.update(over)
    return mod.IntroScene.model_validate(base)


# --- S1: no free text or number in the schema ---------------------------------------------


def free_leaves(model: type[BaseModel]) -> list[str]:
    """Every schema leaf that could carry free text or a number: a string, integer or number
    that is not a closed enum, and any node that says nothing about its type (``Any``,
    ``object``). Follows refs, combinators, object properties, open ``additionalProperties`` /
    ``patternProperties`` (dicts), ``items`` and ``prefixItems`` (lists, tuples)."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    found: list[str] = []
    combinators = ("anyOf", "oneOf", "allOf")
    closed = ("enum", "const", "$ref", *combinators)

    def walk(node: Any, path: str) -> None:
        if node is True or node == {}:
            found.append(f"{path}:untyped")
            return
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            walk(defs[node["$ref"].split("/")[-1]], path)
            return
        for key in combinators:
            for index, branch in enumerate(node.get(key, [])):
                walk(branch, f"{path}|{index}")
        if "enum" not in node and "const" not in node:
            kind = node.get("type")
            if kind in ("string", "integer", "number"):
                found.append(f"{path}:{kind}")
            elif kind is None and not any(key in node for key in closed):
                found.append(f"{path}:untyped")
        for name, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{name}")
        for name, child in (node.get("patternProperties") or {}).items():
            walk(child, f"{path}.{{{name}}}")
        extra = node.get("additionalProperties")
        if extra is True:
            found.append(f"{path}.*:untyped")
        elif isinstance(extra, dict):
            walk(extra, f"{path}.*")
        if "items" in node:
            walk(node["items"], f"{path}[]")
        for index, child in enumerate(node.get("prefixItems") or []):
            walk(child, f"{path}[{index}]")

    walk(schema, "scene")
    return found


def test_schema_has_no_free_text_or_number():
    assert free_leaves(mod.IntroScene) == []


class _FreeStrings(BaseModel):
    headline: str
    size: int


class _FreeDict(BaseModel):
    labels: dict[str, str] = {}


class _FreeTuple(BaseModel):
    pair: tuple[str, int]


class _FreeList(BaseModel):
    names: list[str] = []


class _FreeAny(BaseModel):
    blob: Any = None


class _FreeObject(BaseModel):
    note: object = None


class _FreeNumberInUnion(BaseModel):
    size: float | None = None


@pytest.mark.parametrize(
    ("model", "at_least"),
    [
        (_FreeStrings, 2),
        (_FreeDict, 1),
        (_FreeTuple, 2),
        (_FreeList, 1),
        (_FreeAny, 1),
        (_FreeObject, 1),
        (_FreeNumberInUnion, 1),
    ],
)
def test_schema_walker_catches_every_way_text_could_get_in(model, at_least):
    assert len(free_leaves(model)) >= at_least


def test_scene_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        mod.IntroScene.model_validate({**_compose(EN_VOICES).model_dump(), "text": "hello"})


# --- S4: decline and compose shapes -------------------------------------------------------


def test_decline_is_explicit_and_carries_only_a_reason():
    scene = mod.IntroScene.model_validate({"decision": "decline", "decline_reason": "no_good_fit"})
    assert scene.decision == "decline"
    assert scene.voices == ()


def test_decline_with_choices_is_rejected():
    with pytest.raises(ValidationError):
        mod.IntroScene.model_validate(
            {"decision": "decline", "decline_reason": "no_good_fit", "ink": "cream"}
        )


def test_decline_without_a_reason_is_rejected():
    with pytest.raises(ValidationError):
        mod.IntroScene.model_validate({"decision": "decline"})


@pytest.mark.parametrize("missing", ["font_pair", "ink", "zone", "entrance", "sound", "voices"])
def test_compose_missing_a_choice_is_rejected(missing):
    data = _compose(EN_VOICES).model_dump()
    data.pop(missing)
    with pytest.raises(ValidationError):
        mod.IntroScene.model_validate(data)


@pytest.mark.parametrize("field", ["font_pair", "ink", "zone", "entrance", "sound"])
def test_compose_with_an_unknown_id_is_rejected(field):
    with pytest.raises(ValidationError):
        _compose(EN_VOICES, **{field: "made_up"})


def test_compose_with_an_unknown_voice_is_rejected():
    with pytest.raises(ValidationError):
        _compose(["solid", "loud"])


# --- the id types and the catalogues cannot drift -----------------------------------------


@pytest.mark.parametrize(
    ("literal", "catalogue"),
    [
        (mod.FontPairId, mod.FONT_PAIRS),
        (mod.InkId, mod.INKS),
        (mod.ZoneId, mod.ZONES),
        (mod.EntranceId, mod.ENTRANCES),
        (mod.SoundId, mod.SOUNDS),
    ],
)
def test_every_issued_id_is_a_schema_id_and_back(literal, catalogue):
    assert set(get_args(literal)) == set(catalogue)
