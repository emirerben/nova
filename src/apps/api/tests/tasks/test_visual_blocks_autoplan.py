from __future__ import annotations

from contextlib import nullcontext

from app.config import settings
from app.tasks import autoplace


def test_asset_preparation_happens_once_before_variant_fanout(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(
        autoplace,
        "_materialize_extracted_frames",
        lambda job_id: events.append(("extract", job_id)),
    )
    monkeypatch.setattr(
        autoplace.plan_visual_blocks,
        "apply_async",
        lambda *, args, queue: events.append(("plan", (args, queue))),
    )
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: nullcontext(),
    )

    autoplace.prepare_visual_block_assets.run("job-1", ["variant-a", "variant-b"])

    assert events[0] == ("extract", "job-1")
    assert [event[0] for event in events] == ["extract", "plan", "plan"]
