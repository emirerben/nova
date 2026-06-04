# Nova dev-loop backlog

The autonomous dev-loop mints one **build_task** per unchecked item below.

- **Add an item:** `- [ ] Title :: optional one-line spec/body`
  (everything before `::` is the title; everything after is the body the agent reads)
- **Queue them:** `scripts/queue.sh sync`
  (idempotent — skips any title already in the queue; `- [x]` items are ignored)
- **Review:** `scripts/queue.sh ls` (or `ls awaiting_approval` / `ls blocked`)
- Items stay here as a record. Dedup is by **title** against existing build_tasks,
  so re-running `sync` never double-mints. Check an item off (`- [x]`) to stop the
  loop from ever considering it.

> Priority: prefix a title with `(p<N>)` to set priority (lower = sooner), e.g.
> `- [ ] (p10) Fix the flaky upload test :: ...`. Default priority is 100.

## Backlog

<!-- Add `- [ ] Title :: body` items below. `scripts/queue.sh sync` mints them.
     Example (uncomment + edit to use):
     - [ ] (p50) Add a retry to the GCS upload helper :: wrap the upload in
       app/services/storage.py with tenacity retry(3) + a unit test; keep the signature.
-->
