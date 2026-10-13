# Local hook proof

The final automation uses local Git/agent hooks. Its real simulator proof will
be recorded here after executing the hook from an isolated clone. The earlier
native UI/persistence proof remains in [proof.md](proof.md); the removed CI
experiment is retained as [historical evidence](automatic-proof.md).

Lifecycle acceptance covers asynchronous enqueue, commit deduplication, a frozen
snapshot with dirty source files, pull/rebase/push events, non-current pushed
branches, existing hooks/stdin/status, explicit retry, failure cleanup, and
interrupted worker recovery. These orchestration tests stub the simulator
boundary; only the separate native result bundle establishes UI behavior.
