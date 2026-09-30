"""Small, strict HTTP client for TypeSafe Jev evaluations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
JEV_PRICE_VERSION = "2026-09"
JEV_INPUT_PRICE_USD_PER_MILLION = 0.042
JEV_MAX_QUESTIONS = 32
# Descriptive aliases kept public for callers that prefer API/price naming.
JEV_API_URL = JEV_ENDPOINT
JEV_INPUT_PRICE_USD_PER_1M = JEV_INPUT_PRICE_USD_PER_MILLION


class JevError(Exception):
    """A safe, machine-readable Jev client failure."""

    def __init__(
        self,
        code: str,
        *,
        attempts: int,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.attempts = attempts
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class JevEvaluation:
    model: str
    answers: dict[str, float]
    input_tokens: int
    output_tokens: int
    attempts: int
    latency_ms: float
    provider_request_id: str | None = None

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * JEV_INPUT_PRICE_USD_PER_MILLION / 1_000_000


class JevClient:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = JEV_ENDPOINT,
        model: str = JEV_MODEL,
        timeout_s: float = 3.0,
        max_attempts: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 2
        ):
            raise ValueError("max_attempts must be between 1 and 2")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not endpoint:
            raise ValueError("endpoint must be non-empty")
        if model != JEV_MODEL:
            raise ValueError("model must be the pinned Jev model")
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    def evaluate(
        self, state: str | Mapping[str, Any] | list[Any], questions: Mapping[str, Mapping[str, Any]]
    ) -> JevEvaluation:
        self._validate_questions(questions)
        payload = {"state": state, "model": self._model, "questions": dict(questions)}
        started = self._clock()
        attempts = 0
        with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
            while True:
                attempts += 1
                try:
                    response = client.post(
                        self._endpoint,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    if attempts < self._max_attempts:
                        self._backoff(attempts, None)
                        continue
                    raise JevError("connect_error", attempts=attempts, retryable=True) from exc
                except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                    raise JevError("timeout", attempts=attempts) from exc
                except httpx.TransportError as exc:
                    raise JevError("transport_error", attempts=attempts) from exc

                if response.status_code in (429, 529):
                    if attempts < self._max_attempts:
                        self._backoff(attempts, response)
                        continue
                    raise JevError(
                        "rate_limited",
                        attempts=attempts,
                        status_code=response.status_code,
                        retryable=True,
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    code = {
                        401: "unauthorized",
                        422: "invalid_request",
                    }.get(response.status_code, "http_error")
                    raise JevError(
                        code,
                        attempts=attempts,
                        status_code=response.status_code,
                        retryable=False,
                    )
                evaluation = self._parse_response(response, questions, attempts)
                return JevEvaluation(
                    **evaluation,
                    attempts=attempts,
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                )

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = None
        if response is not None:
            raw = response.headers.get("Retry-After", "").strip()
            try:
                candidate = float(raw)
                if 0 <= candidate <= 2:
                    delay = candidate
            except (TypeError, ValueError):
                pass
        self._sleep(delay if delay is not None else min(0.25 * (2 ** (attempt - 1)), 1.0))

    @staticmethod
    def _validate_questions(questions: Mapping[str, Mapping[str, Any]]) -> None:
        if (
            not isinstance(questions, Mapping)
            or not questions
            or len(questions) > JEV_MAX_QUESTIONS
        ):
            raise ValueError("questions must contain 1 to 32 entries")
        for question_id, question in questions.items():
            if (
                not isinstance(question_id, str)
                or not question_id.strip()
                or not isinstance(question, Mapping)
            ):
                raise ValueError("questions must be a mapping of non-empty IDs to objects")
            if question.get("type") != "noul":
                raise ValueError("every question must be a Noul")
            if not isinstance(question.get("instructions"), (str, Mapping, list)):
                raise ValueError("every question needs instructions")
            criteria = question.get("criteria")
            if not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}:
                raise ValueError("every question needs aligned true/false criteria")

    @staticmethod
    def _parse_response(
        response: httpx.Response, questions: Mapping[str, Mapping[str, Any]], attempts: int
    ) -> dict[str, Any]:
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise JevError(
                "malformed_json", attempts=attempts, status_code=response.status_code
            ) from exc
        if not isinstance(body, Mapping) or body.get("model") != JEV_MODEL:
            raise JevError("invalid_response", attempts=attempts, status_code=response.status_code)
        answers = body.get("answers")
        usage = body.get("usage")
        if not isinstance(answers, Mapping) or not isinstance(usage, Mapping):
            raise JevError("invalid_response", attempts=attempts, status_code=response.status_code)
        if set(answers) != set(questions):
            raise JevError("invalid_answer", attempts=attempts, status_code=response.status_code)
        parsed: dict[str, float] = {}
        for question_id in questions:
            answer = answers.get(question_id)
            if not isinstance(answer, Mapping) or answer.get("type") != "noul":
                raise JevError(
                    "invalid_answer", attempts=attempts, status_code=response.status_code
                )
            probability = answer.get("noul")
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not 0 <= probability <= 1
            ):
                raise JevError(
                    "invalid_answer", attempts=attempts, status_code=response.status_code
                )
            parsed[question_id] = float(probability)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (input_tokens, output_tokens)
        ):
            raise JevError("invalid_usage", attempts=attempts, status_code=response.status_code)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        return {
            "model": body["model"],
            "answers": parsed,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "provider_request_id": request_id,
        }
