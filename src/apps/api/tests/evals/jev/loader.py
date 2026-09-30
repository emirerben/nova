"""JSONL loading; deliberately no network or application imports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .models import Case, Dataset, Prediction

T = TypeVar("T", bound=BaseModel)


def load_jsonl(path: str | Path, model: type[T]) -> list[T]:
    rows: list[T] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            rows.append(model.model_validate(value))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row {number}: {exc}") from exc
    return rows


def load_cases(path: str | Path) -> Dataset:
    return Dataset(cases=load_jsonl(path, Case))


def load_predictions(path: str | Path) -> list[Prediction]:
    return load_jsonl(path, Prediction)
