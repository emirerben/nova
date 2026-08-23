from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from app.cli.export_edit_training import main


@contextmanager
def _session():
    yield MagicMock()


def test_export_dry_run_never_writes_or_uploads(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.cli.export_edit_training.sync_session", _session)
    monkeypatch.setattr(
        "app.cli.export_edit_training.build_training_records",
        lambda *_args, **_kwargs: ([], set()),
    )
    output = tmp_path / "dataset.jsonl"

    assert main(["--format", "jsonl", "--dry-run", "--out", str(output)]) == 0
    assert not output.exists()
