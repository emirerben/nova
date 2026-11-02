"""Which run of a turn a planner model call belongs to.

The paid-call ledger keys a call by (feature, model, request id, attempt, prompt hash) and
refuses a key it has already settled. A planner call uses the thread id as its request id, so a
turn that is planned AGAIN (KRI-709 continuation: "clips still checking" runs the same turn as
lease epoch 2, 3, 4; a crashed run reclaimed by the lease) sends an unchanged prompt under the
unchanged key, and the ledger answers ``paid call ... already settled``: the creator saw "The AI
service ran out of time" two turns in a row (prod thread b61b7673, 2026-11-02).

A later run is a new logical call that pays again, so its request id carries the run's lease
epoch. The first run keeps the bare thread id, so its keys, retries and replays are exactly what
they were before this module existed.
"""

from __future__ import annotations

import contextvars
import uuid

# Set by the turn task from the lease epoch it claimed (`_claim` adds one per run). 1 = the
# first run, and the value for every caller outside a turn task.
turn_run_epoch: contextvars.ContextVar[int] = contextvars.ContextVar(
    "kria_turn_run_epoch", default=1
)


def paid_call_request_id(thread_id: uuid.UUID | str, *suffix: str) -> str:
    """The ledger request id for a planner model call made by the running turn run.

    ``suffix`` distinguishes a deliberate second call inside one run (the brief extractor's
    retry). Run 1: ``<thread>[:suffix]`` (unchanged). Run N > 1: ``<thread>:e<N>[:suffix]``.
    """

    epoch = turn_run_epoch.get()
    parts = [str(thread_id)]
    if epoch > 1:
        parts.append(f"e{epoch}")
    parts.extend(suffix)
    return ":".join(parts)
