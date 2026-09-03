# Local development isolation

Each `dev-auto.sh` / `dev-no-docker.sh` launch derives a deterministic
`NOVA_CELERY_QUEUE_NAMESPACE` from the absolute worktree path and exports it
to both the API and Celery processes. Kombu's Redis `global_keyprefix` then
prefixes broker and result keys, including explicit `queue="plan-jobs"` and
`queue="overlay-jobs"` dispatches. The logical Celery queue names remain
canonical, so task routing and admin introspection stay unchanged.

This prevents a worker in one worktree from consuming a Job published by a
different worktree while they share local Redis. The launchers also assign
unique worker node names and print the namespace in their startup summary.

Production leaves the namespace variable unset. In that mode no Redis prefix
or result-backend override is added and the canonical queue configuration is
unchanged.

`dev-stop.sh` only terminates PIDs tracked in that worktree's `.dev/pids`; it
does not kill processes on shared ports or stop shared Redis/Postgres
containers. This keeps stopping one worktree from disrupting another.

