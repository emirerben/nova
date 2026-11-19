"""A Sol Main Creator call streams its reasoning summary to the creation-thread thinking row.

Real model (gpt-6.1-sol via the ledger), real ThoughtSummaryPublisher on the local DB, and the
running local API serving GET /creation-threads/{id}/thought-summaries.
"""

import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.database import sync_engine
from app.services.thought_summaries import (
    bind_thought_publisher,
    current_thought_publisher,
    publisher_for_creation,
)

API, OUT = sys.argv[1], Path(sys.argv[2])
fixture = json.loads(
    Path("tests/fixtures/agent_evals/main_creator/kri178_talking_reaction_beats.json").read_text()
)
user_id = uuid.uuid4()
with sync_engine.begin() as conn:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:id, :email)"),
        {"id": str(user_id), "email": f"kri1384-thinking-{user_id.hex[:8]}@local.test"},
    )
headers = {"Authorization": f"Bearer {settings.internal_api_key}", "X-User-Id": str(user_id)}
created = httpx.post(f"{API}/creation-threads", json={}, headers=headers, timeout=30)
created.raise_for_status()
thread_id = uuid.UUID(created.json()["id"])

request_id = f"kri1384-thinking-{uuid.uuid4().hex[:8]}"
publisher = publisher_for_creation(
    creator_id=user_id, thread_id=thread_id, client_request_id=request_id
)
assert publisher is not None, "THOUGHT_SUMMARIES_ENABLED must be on"
agent = MainCreatorAgent(default_client())
assert agent.spec.model == "gpt-6.1-sol", agent.spec.model
polls = []
started = time.monotonic()
with bind_thought_publisher(publisher):
    output = agent.run(
        fixture["input"],
        ctx=RunContext(
            request_id=request_id,
            creator_id=str(user_id),
            usage_purpose="live_eval",
            test_run_id="kri1384-thinking-row-20261113",
            estimated_max_cost_usd=2.0,
            reservation_approved=True,
            thought_summary_callback=current_thought_publisher(),
        ),
    )
publisher.complete()
elapsed = time.monotonic() - started
listed = httpx.get(
    f"{API}/creation-threads/{thread_id}/thought-summaries",
    params={"client_request_id": request_id},
    headers=headers,
    timeout=30,
)
listed.raise_for_status()
body = listed.json()
history = httpx.get(
    f"{API}/creation-threads/{thread_id}/thought-summaries", headers=headers, timeout=30
).json()
summaries = body["summaries"]
assert summaries and summaries[0]["status"] == "completed" and summaries[0]["text"].strip()
assert history["summaries"], "a completed summary must also appear in reopened history"
result = {
    "model": agent.spec.model,
    "fallback_models": list(agent.spec.fallback_models),
    "thinking_level": agent.spec.thinking_level,
    "action_kind": output.action.kind,
    "elapsed_s": round(elapsed, 1),
    "thread_id": str(thread_id),
    "client_request_id": request_id,
    "get_status": listed.status_code,
    "summaries": [
        {k: row.get(k) for k in ("status", "started_at", "completed_at")}
        | {"chars": len(row["text"]), "text": row["text"]}
        for row in summaries
    ],
    "history_count": len(history["summaries"]),
}
OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({k: v for k, v in result.items() if k != "summaries"}, indent=2))
print("summary chars:", [s["chars"] for s in result["summaries"]])
print(result["summaries"][0]["text"][:600])
