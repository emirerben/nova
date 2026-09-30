from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import BriefRequirement
from app.services.jev_brief_shadow import (
    build_jev_brief_payload,
    build_jev_questions,
    map_jev_evaluation,
)


def req(identifier: str, kind: str = "text", **kwargs) -> BriefRequirement:
    return BriefRequirement(id=identifier, kind=kind, scope="global", **kwargs)


def test_projection_filters_unsafe_and_preserves_turkish_nfc() -> None:
    payload = build_jev_brief_payload(
        [
            req("r0", description="İstanbul’da sıcak bir giriş"),
            req("r1", description="literal excluded", literal="exact"),
            req("r2", "timing", description="10 seconds"),
            req("r3", "order", description="first"),
            req("r4", "style", description="cinematic"),
            req("r5", "audio", description="quiet music"),
            req("r6", description="gone", status="superseded"),
        ],
        CreativeStrategy(intro_hook="İlk bakışta", story_structure=["Giriş", "Giriş"]),
        [
            "Kitchen",
            "https://example.com/x",
            "/tmp/a.mp4",
            "gs:bucket/a",
            "uploads/private-object",
            r"clips\private-object",
            "Kitchen",
        ],
    )
    assert payload is not None
    assert [item.id for item in payload.brief.requirements] == ["r0", "r4", "r5"]
    assert payload.brief.requirements[0].text == "İstanbul’da sıcak bir giriş"
    assert payload.media == ["Kitchen"]
    assert payload.proposed_script.story_structure == ["Giriş"]
    assert len(payload.model_dump_json().encode()) <= 24_000


def test_questions_are_separate_and_deterministic() -> None:
    payload = build_jev_brief_payload(
        [req("r0", description="Keep the opening clear")],
        CreativeStrategy(opening_title="Hello", closing_title="Bye"),
        [],
    )
    assert payload is not None
    questions = build_jev_questions(payload)
    assert list(questions) == ["required_0", "claim_0", "claim_1"]
    assert questions["required_0"].keys() == {"type", "instructions", "criteria"}
    assert questions["claim_0"].keys() == {"type", "instructions", "criteria"}
    assert questions["required_0"]["type"] == "noul"
    assert "`brief.requirements[0]`" in questions["required_0"]["instructions"]
    assert "`proposed_script`" in questions["required_0"]["instructions"]
    assert "unsupported" in questions["claim_0"]["criteria"]["true"]


def test_empty_projection_returns_none_and_mapping_preserves_ids() -> None:
    assert build_jev_brief_payload([req("r0", description="")], CreativeStrategy(), []) is None
    payload = build_jev_brief_payload([], CreativeStrategy(intro_hook="Hook"), [])
    assert payload is not None
    evaluation = {
        "answers": {"claim_0": {"probability": 0.25}},
        "model": "jev-test",
        "input_tokens": 2,
        "output_tokens": 3,
        "attempts": 2,
        "latency_ms": 12,
        "cost_usd": 0.01,
        "provider_request_id": "req-1",
    }
    judgment = map_jev_evaluation(payload, evaluation)
    assert judgment.claims[0].id == "claim_0"
    assert judgment.claims[0].probability == 0.25
    assert judgment.model == "jev-test"
    assert judgment.provider_request_id == "req-1"
    assert judgment.input_tokens == 2
    assert judgment.cost_usd == 0.01


def test_mapping_rejects_missing_answers() -> None:
    payload = build_jev_brief_payload([], CreativeStrategy(intro_hook="Hook"), [])
    assert payload is not None
    try:
        map_jev_evaluation(payload, {"answers": {}})
    except ValueError as exc:
        assert "claim_0" in str(exc)
    else:
        raise AssertionError("missing answers should fail closed")


def test_projection_caps_required_and_claim_questions_at_provider_limit() -> None:
    requirements = [req(f"r{i}", description=f"Requirement {i}") for i in range(40)]
    strategy = CreativeStrategy(
        intro_hook="Hook claim",
        opening_title="Opening claim",
        closing_title="Closing claim",
        story_structure=[f"Story claim {i}" for i in range(8)],
        shot_labels=[f"Shot claim {i}" for i in range(8)],
    )
    payload = build_jev_brief_payload(requirements, strategy, [])
    assert payload is not None
    assert len(payload.brief.requirements) == 16
    assert len(payload.candidate_claims) == 16
    assert len(build_jev_questions(payload)) == 32


def test_question_state_references_scale_near_linearly() -> None:
    def size(count: int) -> int:
        payload = build_jev_brief_payload(
            [req(f"r{i}", description=f"Requirement {i}") for i in range(count)],
            CreativeStrategy(),
            [],
        )
        assert payload is not None
        questions = build_jev_questions(payload)
        return len(str(questions).encode())

    small, large = size(5), size(10)
    assert large < small * 3
