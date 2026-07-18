from __future__ import annotations

from app.agents.visual_treatment_planner import RawVisualTreatment
from app.services.visual_treatment_planner import build_visual_treatments


def _assets(count: int = 5) -> dict[str, dict]:
    return {
        f"asset-{index}": {
            "id": f"asset-{index}",
            "gcs_path": f"users/u/plan/p/pool/{index}.jpg",
            "kind": "image",
        }
        for index in range(count)
    }


def test_materializes_montage_and_transcript_card_as_editable_objects() -> None:
    proposals = [
        RawVisualTreatment(
            kind="montage",
            purpose="hook",
            start_s=0,
            end_s=3,
            asset_ids=list(_assets()),
            rationale="Hook evidence",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="key_argument",
            start_s=8,
            end_s=10,
            text="Talent alone was not enough.",
            rationale="Isolate the argument",
            confidence="high",
        ),
    ]
    blocks, elements = build_visual_treatments(
        proposals,
        assets_by_id=_assets(),
        duration_s=30,
        transcript_text="Talent alone was not enough. The work mattered too.",
    )
    assert [block["kind"] for block in blocks] == ["montage", "text_card"]
    assert blocks[0]["origin"] == "ai"
    assert len(blocks[0]["shots"]) == 5
    assert elements[0]["visual_block_id"] == blocks[1]["id"]
    assert elements[0]["text"] == "Talent alone was not enough."


def test_insufficient_assets_drops_montage() -> None:
    proposal = RawVisualTreatment(
        kind="montage",
        purpose="context",
        start_s=0,
        end_s=3,
        asset_ids=["asset-0", "asset-1"],
        confidence="high",
    )
    blocks, elements = build_visual_treatments([proposal], assets_by_id=_assets(2), duration_s=20)
    assert blocks == []
    assert elements == []


def test_caps_repetitive_text_cards() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="transition",
            start_s=float(index * 4),
            end_s=float(index * 4 + 1),
            text=f"Card {index}",
            confidence="high",
        )
        for index in range(8)
    ]
    blocks, _elements = build_visual_treatments(
        proposals,
        assets_by_id={},
        duration_s=60,
        transcript_text=" ".join(f"Card {index}" for index in range(8)),
    )
    assert len(blocks) <= 4
    assert sum(block["end_s"] - block["start_s"] for block in blocks) <= 60 * 0.18


def test_drops_text_card_copy_not_supported_by_transcript() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="statistic",
        start_s=2,
        end_s=4,
        text="Revenue grew by 200 percent",
        confidence="high",
    )
    blocks, elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_text="Revenue grew, but no exact figure was provided.",
    )
    assert blocks == []
    assert elements == []


def test_accepts_concise_numeric_formatting_of_spoken_statistic() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="statistic",
        start_s=2,
        end_s=4,
        text="Retention rose 30%",
        confidence="high",
    )
    blocks, _elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_text="Retention rose by thirty percent.",
    )
    assert len(blocks) == 1
