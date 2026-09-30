from __future__ import annotations  # noqa: I001

import httpx
import pytest

from app.services.jev_client import JEV_MODEL, JevClient, JevError

QUESTIONS = {
    "q1": {
        "type": "noul",
        "instructions": "Is it useful?",
        "criteria": {"true": "yes", "false": "no"},
    }
}


def test_evaluate_parses_response_and_sends_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer secret-key"
        assert request.read()
        return httpx.Response(
            200,
            json={
                "model": JEV_MODEL,
                "answers": {"q1": {"type": "noul", "noul": 0.75}},
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    result = JevClient("secret-key", transport=httpx.MockTransport(handler)).evaluate(
        "hello", QUESTIONS
    )
    assert result.answers == {"q1": 0.75}
    assert result.input_tokens == 10
    assert result.cost_usd == pytest.approx(0.042 * 10 / 1_000_000)
    assert seen


@pytest.mark.parametrize("status", [401, 422])
def test_terminal_http_errors_are_not_retried(status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with pytest.raises(JevError) as exc_info:
        JevClient("key", max_attempts=2, transport=httpx.MockTransport(handler)).evaluate(
            {}, QUESTIONS
        )
    assert calls == 1
    assert exc_info.value.status_code == status
    assert "key" not in str(exc_info.value)


def test_rate_limit_then_success_uses_retry() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.2"})
        return httpx.Response(
            200,
            json={
                "model": JEV_MODEL,
                "answers": {"q1": {"type": "noul", "noul": 1}},
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )

    result = JevClient("key", sleep=delays.append, transport=httpx.MockTransport(handler)).evaluate(
        [], QUESTIONS
    )
    assert result.attempts == 2
    assert delays == [0.2]


def test_529_then_success_and_connect_failure_then_success() -> None:
    for first_error in (529, "connect"):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1 and first_error == "connect":
                raise httpx.ConnectError("offline", request=request)
            if calls == 1:
                return httpx.Response(529)
            return httpx.Response(
                200,
                json={
                    "model": JEV_MODEL,
                    "answers": {"q1": {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )

        assert (
            JevClient("key", sleep=lambda _: None, transport=httpx.MockTransport(handler))
            .evaluate(  # noqa: E501
                "state", QUESTIONS
            )
            .attempts
            == 2
        )


def test_read_timeout_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(JevError) as exc_info:
        JevClient("key", max_attempts=2, transport=httpx.MockTransport(handler)).evaluate(
            "state", QUESTIONS
        )
    assert calls == 1
    assert exc_info.value.code == "timeout"


@pytest.mark.parametrize("error_type", [httpx.WriteTimeout, httpx.PoolTimeout])
def test_other_timeouts_are_terminal(error_type: type[httpx.TimeoutException]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error_type("slow", request=request)

    with pytest.raises(JevError) as exc_info:
        JevClient("key", max_attempts=2, transport=httpx.MockTransport(handler)).evaluate(
            "state", QUESTIONS
        )
    assert calls == 1
    assert exc_info.value.code == "timeout"


def test_generic_transport_error_is_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("bad framing", request=request)

    with pytest.raises(JevError) as exc_info:
        JevClient("key", transport=httpx.MockTransport(handler)).evaluate("state", QUESTIONS)
    assert exc_info.value.code == "transport_error"


def test_generic_http_error_is_terminal() -> None:
    with pytest.raises(JevError) as exc_info:
        JevClient("key", transport=httpx.MockTransport(lambda _: httpx.Response(500))).evaluate(
            "state", QUESTIONS
        )
    assert exc_info.value.code == "http_error"
    assert exc_info.value.status_code == 500


def test_invalid_retry_after_uses_bounded_default() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "not-a-number"})
        return httpx.Response(
            200,
            json={
                "model": JEV_MODEL,
                "answers": {"q1": {"type": "noul", "noul": 0.5}},
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        )

    JevClient("key", sleep=delays.append, transport=httpx.MockTransport(handler)).evaluate(
        "state", QUESTIONS
    )
    assert delays == [0.25]


def test_exhausted_retry_is_safe() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(529, text="secret response body")

    with pytest.raises(JevError) as exc_info:
        JevClient(
            "secret-key", sleep=lambda _: None, transport=httpx.MockTransport(handler)
        ).evaluate(  # noqa: E501
            "state", QUESTIONS
        )
    assert calls == 2
    assert exc_info.value.code == "rate_limited"
    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    "body",
    [
        {"model": "wrong", "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}},
        {"model": JEV_MODEL, "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}},
        {
            "model": JEV_MODEL,
            "answers": {"q1": {"type": "wrong", "noul": 0.5}},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },  # noqa: E501
        {
            "model": JEV_MODEL,
            "answers": {"q1": {"type": "noul", "noul": 1.5}},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },  # noqa: E501
        {
            "model": JEV_MODEL,
            "answers": {
                "q1": {"type": "noul", "noul": 0.5},
                "extra": {"type": "noul", "noul": 0.1},
            },
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },  # noqa: E501
        {
            "model": JEV_MODEL,
            "answers": {"q1": {"type": "noul", "noul": 0.5}},
            "usage": {"input_tokens": -1, "output_tokens": 0},
        },  # noqa: E501
    ],
)
def test_invalid_response_shapes_are_rejected(body: dict[str, object]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(JevError):
        JevClient("key", transport=httpx.MockTransport(handler)).evaluate("state", QUESTIONS)


@pytest.mark.parametrize(
    "bad_questions",
    [
        {},
        {"": QUESTIONS["q1"]},
        {"q1": []},
        {"q1": {"type": "choice", "instructions": "x", "criteria": {}}},
        {"q1": {"type": "noul", "instructions": "x", "criteria": {"true": "yes"}}},
    ],
)
def test_constructor_and_question_validation(bad_questions: object) -> None:
    with pytest.raises(ValueError):
        JevClient("", transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(ValueError):
        JevClient("key", max_attempts=3)
    with pytest.raises(ValueError):
        JevClient("key", timeout_s=0)
    with pytest.raises(ValueError):
        JevClient("key", endpoint="")
    with pytest.raises(ValueError):
        JevClient("key", model="jev-latest")
    with pytest.raises(ValueError):
        JevClient("key", transport=httpx.MockTransport(lambda _: httpx.Response(200))).evaluate(
            "state",
            bad_questions,  # type: ignore[arg-type]
        )


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(JevError) as exc_info:
        JevClient(
            "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not json"))
        ).evaluate(  # noqa: E501
            "state", QUESTIONS
        )
    assert exc_info.value.code == "malformed_json"


def test_malformed_answer_is_rejected_without_body_in_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": JEV_MODEL,
                "answers": {"q1": {"type": "wrong", "noul": 2}},
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        )

    with pytest.raises(JevError) as exc_info:
        JevClient("key", transport=httpx.MockTransport(handler)).evaluate("state", QUESTIONS)
    assert exc_info.value.code == "invalid_answer"
