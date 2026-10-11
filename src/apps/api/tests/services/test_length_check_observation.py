import io
import json

from app.services import length_check_observation


def test_emitter_writes_one_sanitized_collector_event(monkeypatch) -> None:
    stream = io.StringIO()
    handler = length_check_observation._logger.handlers[0]
    monkeypatch.setattr(handler, "stream", stream)
    monkeypatch.setenv("CREATOR_DURATION_CHECK_REVISION", "b" * 40)
    monkeypatch.setattr(length_check_observation.time, "time", lambda: 123.9)

    event = length_check_observation.emit_length_check_observation(
        check_mode="log_only", outcome="declined"
    )

    assert json.loads(stream.getvalue()) == event
    assert event == {
        "event": "creator_length_check",
        "timestamp_epoch_s": 123,
        "check_mode": "log_only",
        "check_observations": 1,
        "check_declines": 1,
        "api_revision": "b" * 40,
        "worker_revision": "b" * 40,
    }
