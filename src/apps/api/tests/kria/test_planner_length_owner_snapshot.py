from app.kria.planner import _enrich_best_parts_snapshot


def test_planner_persists_selected_footage_receipts_before_length_questioning():
    snapshot = {
        "clip_assignments": [
            {
                "media_id": "first",
                "duration_s": 8,
                "analysis": {"best_moments": [{"start_s": 1, "end_s": 5, "energy": 0.9}]},
            },
            {
                "media_id": "second",
                "duration_s": 8,
                "analysis": {"best_moments": [{"start_s": 0, "end_s": 6, "energy": 0.8}]},
            },
        ]
    }

    enriched = _enrich_best_parts_snapshot({}, snapshot, None)

    assert enriched is not snapshot
    assert enriched["best_parts_capacity_s"] == 10
    assert [part["media_id"] for part in enriched["best_parts"]] == ["first", "second"]
    assert [(part["source_start_s"], part["source_end_s"]) for part in enriched["best_parts"]] == [
        (1, 5),
        (0, 6),
    ]
    envelope = enriched["length_plan"]
    assert envelope["version"] == 1
    assert envelope["owner"] == envelope["natural_owner"] == "footage"
    assert envelope["conflicts"] == []
    assert [source["media_id"] for source in envelope["sources"]] == ["first", "second"]
