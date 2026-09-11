"""Compare numeric oracles without treating Python/libm rounding as drift."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest


def assert_reference_matches(actual: Any, expected: Any, path: str = "reference") -> None:
    if actual == expected:
        return
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert actual.keys() == expected.keys(), path
        for key in expected:
            assert_reference_matches(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), path
        assert len(actual) == len(expected), path
        for index, (value, reference) in enumerate(zip(actual, expected, strict=True)):
            assert_reference_matches(value, reference, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), path
    else:
        assert actual == expected, path


def write_linux_reference(path: Path, value: Any, *, compact: bool = False) -> None:
    """Keep font oracles canonical and large pixel arrays scanner-readable."""
    if sys.platform != "linux":
        raise RuntimeError("Generate font references inside the production Linux Docker image")
    if not compact:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    with path.open("w") as output:
        column = 0
        for chunk in encoder.iterencode(value):
            if column and column + len(chunk) > 160:
                output.write("\n")
                column = 0
            output.write(chunk)
            column += len(chunk)
        output.write("\n")
