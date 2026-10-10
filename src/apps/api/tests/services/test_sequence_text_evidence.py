from app.pipeline.sequence_text_evidence import text_matches


def _rows(words: list[str], *, source: str = "guided-title") -> list[dict]:
    return [
        {
            "element_id": f"{source}::sequence-{index}",
            "sequence_source_id": source,
            "text": word,
            "start_s": index - 1.0,
            "end_s": float(index),
            "role": "opening",
        }
        for index, word in enumerate(words, 1)
    ]


def test_sequence_fragments_prove_the_full_phrase_and_keep_role() -> None:
    rows = _rows(["Morning", "coffee"])

    matches = text_matches(rows, "Morning coffee", role="opening")

    assert len(matches) == 1
    assert matches[0]["start_s"] == 0
    assert matches[0]["end_s"] == 2
    assert matches[0]["role"] == "opening"


def test_sequence_fragments_fail_closed_for_gap_order_and_unrelated_rows() -> None:
    rows = _rows(["Morning", "coffee"])

    gapped = [rows[0], {**rows[1], "element_id": "guided-title::sequence-3"}]
    assert not text_matches(gapped, "Morning coffee", role="opening")
    reordered = [
        {**rows[1], "start_s": 0.0, "end_s": 1.0},
        {**rows[0], "start_s": 1.0, "end_s": 2.0},
    ]
    assert not text_matches(reordered, "Morning coffee", role="opening")
    unrelated = rows + [{"element_id": "other", "text": "coffee", "start_s": 2, "end_s": 3}]
    assert text_matches(unrelated, "Morning coffee", role="opening")
    borrowed = [
        rows[0],
        {"element_id": "other", "text": "coffee", "start_s": 1, "end_s": 2},
    ]
    assert not text_matches(borrowed, "Morning coffee", role="opening")


def test_sequence_fragments_require_contiguous_timing() -> None:
    rows = _rows(["Morning", "coffee"])
    rows[1]["start_s"] = 1.3

    assert not text_matches(rows, "Morning coffee", role="opening", tolerance_s=0.1)
