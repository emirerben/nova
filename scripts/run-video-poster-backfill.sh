#!/usr/bin/env bash
# Serialize Fly deploys and the one-time poster repair with one app-unique,
# unmanaged Machine name. Fly enforces Machine-name uniqueness, so even two
# runners that both observed an empty list cannot both acquire the guard.

set -euo pipefail

app_name="${FLY_APP_NAME:-nova-video}"
region="${FLY_REGION:-iad}"
guard_name="${app_name}-production-mutation-guard"
revision_label="org.opencontainers.image.revision"
operation_key="nova_operation"
backfill_operation="video-poster-backfill"
deploy_operation="fly-deploy-guard"
expected_sha="${EXPECTED_SHA:-}"
retry_failed_machine_id="${RETRY_FAILED_MACHINE_ID:-}"
acknowledge_failed_backfill_machine_id="${ACKNOWLEDGE_FAILED_BACKFILL_MACHINE_ID:-}"
verified_deploy_digest="${VERIFIED_DEPLOY_DIGEST:-}"
poll_interval_s="${POSTER_BACKFILL_POLL_INTERVAL_S:-20}"
max_wait_s="${POSTER_BACKFILL_MAX_WAIT_S:-18600}"
guard_resolve_attempts="${FLY_GUARD_RESOLVE_ATTEMPTS:-5}"
production_settle_attempts="${FLY_PRODUCTION_SETTLE_ATTEMPTS:-20}"
guard_reclaim_grace_s="${FLY_GUARD_RECLAIM_GRACE_S:-300}"
deploy_guard_lease_s=2700
backfill_guard_lease_s=18900
mode="${1:-run}"
run_id="${GITHUB_RUN_ID:-manual}"
run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
guard_owner="${run_id}:${run_attempt}"
github_event_name="${GITHUB_EVENT_NAME:-}"
github_ref="${GITHUB_REF:-}"
health_url="${NOVA_HEALTH_URL:-https://nova-video.fly.dev/health}"
backfill_command='cd /app && python -m scripts.backfill_video_posters --exclude-synthetic --strict --batch-size 25 && python -m scripts.backfill_video_posters --dry-run --exclude-synthetic --strict --batch-size 25'
deploy_guard_command='/bin/false'

fail() {
  echo "::error::$*" >&2
  exit 1
}

case "$mode" in
  run|--reconcile-only|--acquire-deploy-guard|--release-deploy-guard) ;;
  *) fail "Usage: $0 [--reconcile-only|--acquire-deploy-guard|--release-deploy-guard]" ;;
esac
[[ "$poll_interval_s" =~ ^[0-9]+$ && "$max_wait_s" =~ ^[1-9][0-9]*$ ]] || \
  fail "Poster backfill poll timing must use non-negative integer seconds."
[[ "$guard_resolve_attempts" =~ ^[1-9][0-9]*$ \
  && "$production_settle_attempts" =~ ^[1-9][0-9]*$ \
  && "$guard_reclaim_grace_s" =~ ^[0-9]+$ ]] || \
  fail "Fly guard timing must use non-negative integer values."
[[ "$run_id" == "manual" || "$run_id" =~ ^[0-9]+$ ]] || \
  fail "GITHUB_RUN_ID must be numeric when present."
[[ "$run_attempt" =~ ^[1-9][0-9]*$ ]] || \
  fail "GITHUB_RUN_ATTEMPT must be a positive integer."
if [[ -n "$retry_failed_machine_id" && ! "$retry_failed_machine_id" =~ ^[0-9a-f]+$ ]]; then
  fail "RETRY_FAILED_MACHINE_ID must be an exact lowercase hexadecimal Fly Machine ID."
fi
if [[ -n "$acknowledge_failed_backfill_machine_id" ]]; then
  [[ "$acknowledge_failed_backfill_machine_id" =~ ^[0-9a-f]+$ ]] || \
    fail "ACKNOWLEDGE_FAILED_BACKFILL_MACHINE_ID must be an exact lowercase hexadecimal Fly Machine ID."
  [[ "$mode" == "--acquire-deploy-guard" \
    && "$github_event_name" == "workflow_dispatch" \
    && "$github_ref" == "refs/heads/main" ]] || \
    fail "A failed backfill may be acknowledged only by a manual main-branch deploy."
fi
[[ -z "$retry_failed_machine_id" || -z "$acknowledge_failed_backfill_machine_id" ]] || \
  fail "A failed backfill cannot be retried and acknowledged for removal in the same invocation."
if [[ "$mode" != "--reconcile-only" ]]; then
  [[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || \
    fail "EXPECTED_SHA must be the exact lowercase 40-character commit SHA."
fi

machine_list() {
  flyctl machine list --app "$app_name" --json
}

machine_by_id() {
  local machine_id="$1"
  jq -cer --arg id "$machine_id" '
    [.[] | select(.id == $id)]
    | if length == 1 then .[0] else empty end
  '
}

machine_by_guard_name() {
  jq -cer --arg name "$guard_name" '
    [.[] | select(.name == $name)]
    | if length == 1 then .[0] else empty end
  '
}

machine_operation() {
  jq -r --arg key "$operation_key" \
    '((.config.metadata // .incomplete_config.metadata // {})[$key]) // empty'
}

managed_fleet_has_required_processes() {
  jq -e '
    [.[]
      | select(
          ((.config.metadata // .incomplete_config.metadata // {})
            | has("fly_process_group"))
        )] as $managed
    | any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "api")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "worker")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "light")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "autoplace")
  ' >/dev/null
}

managed_fleet_has_safe_lifecycle_states() {
  jq -e '
    [.[]
      | select(
          ((.config.metadata // .incomplete_config.metadata // {})
            | has("fly_process_group"))
        )]
    | all(
        .state == "started" or .state == "stopped"
        or .state == "starting" or .state == "stopping"
      )
  ' >/dev/null
}

managed_fleet_has_transitional_states() {
  jq -e '
    [.[]
      | select(
          ((.config.metadata // .incomplete_config.metadata // {})
            | has("fly_process_group"))
        )]
    | any(.state == "starting" or .state == "stopping")
  ' >/dev/null
}

managed_fleet_is_operationally_stable() {
  jq -e '
    [.[]
      | select(
          ((.config.metadata // .incomplete_config.metadata // {})
            | has("fly_process_group"))
        )] as $managed
    | any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "api"
          and .state == "started")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "worker")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "light"
          and .state == "started")
      and any($managed[];
        ((.config.metadata // .incomplete_config.metadata).fly_process_group) == "autoplace"
          and .state == "started")
      and all($managed[];
        (.config // .incomplete_config // {}) as $config
        | (($config.metadata // {}).fly_process_group) as $group
        | ([.events[]? | select(.type == "start" or .type == "exit")]) as $lifecycle_events
        | ([$lifecycle_events[] | select(.type == "exit")]
            | sort_by(.timestamp) | last | .request.exit_event) as $last_exit
        | ($group | type == "string" and length > 0)
          and (
            .state == "started"
            or (
              .state == "stopped"
              and (
                ($group == "api"
                  and any(($config.services // [])[]?;
                    .autostop == true or .autostop == "stop"))
                or ($group == "worker"
                  and (
                    ($lifecycle_events | length == 0)
                    or ($last_exit.requested_stop == true
                      and ($last_exit.oom_killed // false) == false
                      and ($last_exit.restarting // false) == false)
                  ))
              )
            )
          )
      )
  ' >/dev/null
}

show_machine_logs() {
  local machine_id="$1"
  if ! flyctl logs --app "$app_name" --machine "$machine_id" --no-tail; then
    echo "::warning::Could not retrieve logs for poster backfill Machine $machine_id." >&2
    return 1
  fi
}

prove_acknowledged_backfill_guard_absent() {
  local machine_id="$1"
  local attempt list_json
  for ((attempt = 1; attempt <= guard_resolve_attempts; attempt++)); do
    list_json="$(machine_list)" || \
      fail "Could not verify removal of acknowledged failed backfill Machine $machine_id."
    validate_reserved_inventory "$list_json"
    if ! machine_by_id "$machine_id" <<<"$list_json" >/dev/null \
      && ! machine_by_guard_name <<<"$list_json" >/dev/null; then
      return 0
    fi
    if (( attempt < guard_resolve_attempts )); then
      sleep "$poll_interval_s"
    fi
  done
  fail "Acknowledged failed backfill Machine $machine_id is still present after destroy; refusing deploy."
}

# Same contract as prove_acknowledged_backfill_guard_absent, for the guard the
# deploy sweeps when it was parked before ever running. Kept separate so the
# failure message names the sweep rather than an acknowledgement the operator
# never made.
prove_swept_backfill_guard_absent() {
  local machine_id="$1"
  local attempt list_json
  for ((attempt = 1; attempt <= guard_resolve_attempts; attempt++)); do
    list_json="$(machine_list)" || \
      fail "Could not verify removal of swept poster backfill Machine $machine_id."
    validate_reserved_inventory "$list_json"
    if ! machine_by_id "$machine_id" <<<"$list_json" >/dev/null \
      && ! machine_by_guard_name <<<"$list_json" >/dev/null; then
      return 0
    fi
    if (( attempt < guard_resolve_attempts )); then
      sleep "$poll_interval_s"
    fi
  done
  fail "Swept poster backfill Machine $machine_id is still present after destroy; refusing deploy."
}

# New guards carry an owner plus a fixed-duration metadata lease. The lease is
# validated structurally before it is trusted for reclaim decisions.
common_guard_contract_is_valid() {
  local machine_json="$1"
  local expected_operation="$2"
  local expected_lease_s="$3"
  jq -e \
    --arg guard_name "$guard_name" \
    --arg operation_key "$operation_key" \
    --arg operation "$expected_operation" \
    --argjson lease_s "$expected_lease_s" '
      (.config // .incomplete_config // {}) as $config
      | ($config.metadata // {}) as $metadata
      | ($metadata.nova_guard_created_epoch | tonumber?) as $created
      | ($metadata.nova_guard_deadline_epoch | tonumber?) as $deadline
      | .name == $guard_name
        and (.id | type == "string" and test("^[0-9a-f]+$"))
        and (.image_ref.digest | type == "string" and test("^sha256:[0-9a-f]{64}$"))
        and ($metadata[$operation_key] == $operation)
        and ($metadata.nova_guard_owner
          | type == "string" and test("^(manual|[0-9]+):[1-9][0-9]*$"))
        and ($metadata.nova_revision
          | type == "string" and test("^[0-9a-f]{40}$"))
        and ($metadata.nova_image_digest == .image_ref.digest)
        and ($created != null and $deadline != null)
        and ($created >= 1000000000 and ($deadline - $created) == $lease_s)
        and (($metadata | has("fly_process_group")) | not)
        and ($config.restart.policy == "no")
        and (($config.services // []) | type == "array" and length == 0)
        and (($config.env // {}) | type == "object" and length == 0)
        and (($config.mounts // []) | type == "array" and length == 0)
        and (($config.files // []) | type == "array" and length == 0)
    ' <<<"$machine_json" >/dev/null
}

backfill_contract_is_valid() {
  local machine_json="$1"
  common_guard_contract_is_valid \
    "$machine_json" "$backfill_operation" "$backfill_guard_lease_s" &&
    jq -e --arg command "$backfill_command" '
      (.config // .incomplete_config // {}) as $config
      | (.state == "created" or .state == "starting" or .state == "started"
          or .state == "stopping" or .state == "replacing" or .state == "stopped")
        and ($config.guest == {"cpu_kind":"shared","cpus":4,"memory_mb":8192})
        and ($config.init.cmd == [
          "/usr/bin/timeout", "--signal=TERM", "--kill-after=300s", "18000s",
          "/bin/bash", "-lc", $command
        ])
    ' <<<"$machine_json" >/dev/null
}

# Transitional support for a guard created by the immediately previous
# launcher. It can only be reconciled to its durable receipt; new creates never
# use a per-run name.
legacy_backfill_contract_is_valid() {
  local machine_json="$1"
  jq -e \
    --arg operation_key "$operation_key" \
    --arg operation "$backfill_operation" \
    --arg command "$backfill_command" '
      (.config // .incomplete_config // {}) as $config
      | ($config.metadata // {}) as $metadata
      | (.name | type == "string" and test("^poster-backfill-(manual|[0-9]+)-[0-9]+$"))
        and (.id | type == "string" and test("^[0-9a-f]+$"))
        and (.image_ref.digest | type == "string" and test("^sha256:[0-9a-f]{64}$"))
        and ($metadata[$operation_key] == $operation)
        and ($metadata.nova_revision
          | type == "string" and test("^[0-9a-f]{40}$"))
        and ($metadata.nova_image_digest == .image_ref.digest)
        and (($metadata | has("fly_process_group")) | not)
        and ($config.restart.policy == "no")
        and ($config.guest == {"cpu_kind":"shared","cpus":4,"memory_mb":8192})
        and ($config.init.cmd == [
          "/usr/bin/timeout", "--signal=TERM", "--kill-after=300s", "18000s",
          "/bin/bash", "-lc", $command
        ])
        and (($config.services // []) | type == "array" and length == 0)
        and (($config.env // {}) | type == "object" and length == 0)
        and (($config.mounts // []) | type == "array" and length == 0)
        and (($config.files // []) | type == "array" and length == 0)
        and (.state == "created" or .state == "starting" or .state == "started"
          or .state == "stopping" or .state == "replacing" or .state == "stopped")
    ' <<<"$machine_json" >/dev/null
}

deploy_guard_contract_is_valid() {
  local machine_json="$1"
  common_guard_contract_is_valid \
    "$machine_json" "$deploy_operation" "$deploy_guard_lease_s" &&
    jq -e --arg command "$deploy_guard_command" '
      (.config // .incomplete_config // {}) as $config
      | ((.state == "created") or (
            .state == "stopped"
            and ([.events[]? | select(.type == "start" or .type == "exit")]
              | length == 0)
          ))
        and ($config.guest == {"cpu_kind":"shared","cpus":1,"memory_mb":256})
        and ($config.init.cmd == [$command])
    ' <<<"$machine_json" >/dev/null
}

validate_reserved_inventory() {
  local list_json="$1"
  local invalid
  if ! jq -e 'type == "array"' <<<"$list_json" >/dev/null; then
    fail "Fly Machine inventory is not a JSON array."
  fi
  invalid="$({
    jq -r \
      --arg guard_name "$guard_name" \
      --arg operation_key "$operation_key" \
      --arg backfill_operation "$backfill_operation" \
      --arg deploy_operation "$deploy_operation" '
        .[]
        | (.config.metadata // .incomplete_config.metadata // {}) as $metadata
        | select(
            (.name == $guard_name)
            or ((.name // "") | startswith("poster-backfill-"))
            or ($metadata[$operation_key] == $backfill_operation)
            or ($metadata[$operation_key] == $deploy_operation)
          )
        | select(
            if .name == $guard_name then
              ($metadata[$operation_key] != $backfill_operation
                and $metadata[$operation_key] != $deploy_operation)
            elif ((.name // "") | startswith("poster-backfill-")) then
              $metadata[$operation_key] != $backfill_operation
            else
              true
            end
          )
        | .id
      ' <<<"$list_json"
  } || true)"
  if [[ -n "$invalid" ]]; then
    fail "Reserved Fly mutation Machine name/metadata is unverifiable ($invalid); refusing to mutate or deploy."
  fi
  if [[ "$(jq --arg name "$guard_name" '[.[] | select(.name == $name)] | length' <<<"$list_json")" -gt 1 ]]; then
    fail "Stable Fly mutation guard name resolves to more than one Machine; refusing all mutation."
  fi
}

latest_exit_receipt() {
  jq -cer '
    [.events[]? | select(.type == "exit")]
    | sort_by(.timestamp)
    | last
  '
}

exit_receipt_is_well_formed() {
  jq -e '
    def optional_integer($key):
      (has($key) | not)
      or (.[$key] | type == "number" and floor == .);
    def optional_boolean($key):
      (has($key) | not)
      or (.[$key] | type == "boolean");
    .type == "exit"
      and (.timestamp
        | (type == "number") or (type == "string" and length > 0))
      and (.request | type == "object")
      and (.request.exit_event | type == "object")
      and (.request.exit_event
        | optional_integer("exit_code")
          and optional_integer("guest_exit_code")
          and optional_integer("signal")
          and optional_integer("guest_signal")
          and optional_boolean("oom_killed")
          and optional_boolean("requested_stop")
          and optional_boolean("restarting"))
  ' >/dev/null
}

exit_event_is_clean() {
  jq -e '
    (if has("exit_code") then .exit_code else 0 end) == 0
      and (if has("guest_exit_code") then .guest_exit_code else 0 end) == 0
      and (if has("signal") then (.signal == 0 or .signal == -1) else true end)
      and (if has("guest_signal") then
        (.guest_signal == 0 or .guest_signal == -1)
      else true end)
      and (.oom_killed // false) == false
      and (.requested_stop // false) == false
      and (.restarting // false) == false
  ' >/dev/null
}

describe_exit_event() {
  jq -r '
    "exit=\(if has("exit_code") then .exit_code else 0 end) "
    + "guest_exit=\(if has("guest_exit_code") then .guest_exit_code else 0 end) "
    + "signal=\(if has("signal") then .signal else 0 end) "
    + "guest_signal=\(if has("guest_signal") then .guest_signal else 0 end) "
    + "oom=\(.oom_killed // false) requested_stop=\(.requested_stop // false) "
    + "restarting=\(.restarting // false)"
  '
}

RECONCILED_REVISION=""
prove_backfill_guard_image_is_live() {
  local machine_json="$1"
  local guard_revision guard_digest saved_expected_sha
  guard_revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$machine_json")"
  guard_digest="$(jq -r '.image_ref.digest' <<<"$machine_json")"
  saved_expected_sha="$expected_sha"
  expected_sha="$guard_revision"
  resolve_production_image true
  expected_sha="$saved_expected_sha"
  [[ "$digest" == "$guard_digest" ]] || \
    fail "Poster backfill guard image is no longer the exact production digest; it was retained and never started."
}

# A freshly created Machine is not startable until Fly finishes preparing its
# image. Fly answers `machine start` during that window with a
# failed_precondition naming the 'created' state; the Machine becomes startable
# on its own moments later. Only that exact transient is retryable — the
# reconciliation loop re-reads state, re-proves the contract, and retries under
# its own deadline. Every other start failure still fails closed.
start_is_retryable_precondition() {
  local start_output="$1"
  [[ "$start_output" == *"unable to start machine from current state: 'created'"* ]]
}

# Start a guard Machine, tolerating only the image-preparation precondition
# above. Returns non-zero when the caller should wait and retry.
start_backfill_machine() {
  local machine_id="$1"
  local context="$2"
  local start_output start_status
  start_output="$(flyctl machine start --app "$app_name" "$machine_id" 2>&1)"
  start_status=$?
  printf '%s\n' "$start_output"
  if (( start_status == 0 )); then
    return 0
  fi
  if start_is_retryable_precondition "$start_output"; then
    echo "Machine $machine_id is still preparing its image; will retry the start."
    return 1
  fi
  fail "Could not start $context poster backfill Machine $machine_id; its guard was retained."
}

# A Machine that reached 'stopped' having never published a start or exit event
# never executed its command, so no mutation can have occurred and there is
# nothing to inspect. `deploy_guard_contract_is_valid` already encodes exactly
# this "never ran" predicate for the dormant deploy guard; without the same
# allowance here a Machine parked during image preparation is a permanent dead
# end that also blocks the next deploy, because deploys CAS this same name.
machine_never_ran() {
  jq -e '[.events[]? | select(.type == "start" or .type == "exit")] | length == 0' \
    >/dev/null
}

reconcile_backfill_machine() {
  local machine_id="$1"
  local legacy="${2:-false}"
  local deadline=$((SECONDS + max_wait_s))
  local list_json machine_json state revision exit_receipt exit_event exit_summary
  local exit_timestamp retry_exit_timestamp=""
  local retry_started=false logs_retrieved=true
  local never_ran_restarts=0
  local never_ran_restart_limit="${POSTER_BACKFILL_NEVER_RAN_RESTARTS:-3}"

  while true; do
    list_json="$(machine_list)" || fail "Could not list Fly Machines."
    validate_reserved_inventory "$list_json"
    if ! machine_json="$(machine_by_id "$machine_id" <<<"$list_json")"; then
      fail "Poster backfill Machine $machine_id could not be resolved exactly once; it was retained."
    fi
    if [[ "$legacy" == "true" ]]; then
      legacy_backfill_contract_is_valid "$machine_json" || \
        fail "Legacy poster backfill Machine $machine_id violates its bounded execution contract; it was retained."
    else
      backfill_contract_is_valid "$machine_json" || \
        fail "Poster backfill Machine $machine_id violates its bounded execution contract; it was retained."
    fi
    state="$(jq -r '.state' <<<"$machine_json")"
    revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$machine_json")"
    if [[ -n "$acknowledge_failed_backfill_machine_id" ]]; then
      [[ "$mode" == "--acquire-deploy-guard" \
        && "$legacy" == "false" \
        && "$(jq -r '.name' <<<"$machine_json")" == "$guard_name" \
        && "$acknowledge_failed_backfill_machine_id" == "$machine_id" ]] || \
        fail "Failed backfill acknowledgement does not exactly match the stable Machine ID; it was retained."
      [[ "$state" == "stopped" ]] || \
        fail "Acknowledged poster backfill Machine $machine_id is '$state', not stopped; it was retained."
    fi

    case "$state" in
      created)
        prove_backfill_guard_image_is_live "$machine_json"
        echo "Resuming created poster backfill Machine $machine_id..."
        start_backfill_machine "$machine_id" "created" || true
        ;;
      starting|started|stopping|replacing)
        echo "Waiting for poster backfill Machine $machine_id ($state)..."
        ;;
      stopped)
        logs_retrieved=true
        if ! show_machine_logs "$machine_id"; then
          logs_retrieved=false
        fi
        if ! exit_receipt="$(latest_exit_receipt <<<"$machine_json")"; then
          # A deploy must never be made to wait out a five-hour repair it did
          # not ask for. It also must not be permanently deadlocked by debris:
          # a guard with no start and no exit event ran nothing, holds no
          # receipt, and has no forensic value — but it does hold the stable
          # name deploys CAS. The acknowledgement recovery cannot clear this
          # shape (it requires a non-clean exit receipt), so the deploy sweeps
          # it. A guard that actually RAN is still retained, below.
          if [[ "$mode" == "--acquire-deploy-guard" ]] && machine_never_ran <<<"$machine_json"; then
            echo "Removing poster backfill guard $machine_id: parked before it ever ran, so it holds no receipt to preserve."
            flyctl machine destroy --app "$app_name" "$machine_id" || \
              fail "Never-started poster backfill guard $machine_id could not be destroyed; it was retained."
            # Absence must be PROVEN before returning. The caller re-CASes this
            # exact stable name moments later, and Fly's list can still report
            # the destroyed Machine (or report it `destroying`, which is not a
            # valid contract state): the create then conflicts on a name whose
            # Machine no longer resolves, and acquire_deploy_guard fails hard
            # instead of retrying. The acknowledged-failure path proves absence
            # for the same reason.
            prove_swept_backfill_guard_absent "$machine_id"
            # Deliberately no RECONCILED_REVISION: nothing was repaired, so a
            # later `run` must still perform the backfill.
            return 0
          fi
          # Restarting is only ever right for a run that WANTS the backfill.
          if [[ "$mode" != "--acquire-deploy-guard" ]] && machine_never_ran <<<"$machine_json"; then
            if (( never_ran_restarts >= never_ran_restart_limit )); then
              fail "Poster backfill Machine $machine_id was parked before it ever started $never_ran_restarts times; it was retained for inspection."
            fi
            never_ran_restarts=$((never_ran_restarts + 1))
            prove_backfill_guard_image_is_live "$machine_json"
            echo "Starting poster backfill Machine $machine_id, parked before it ever ran (attempt $never_ran_restarts/$never_ran_restart_limit)..."
            start_backfill_machine "$machine_id" "never-started" || true
            if (( SECONDS >= deadline )); then
              fail "Poster backfill Machine $machine_id exceeded the reconciliation deadline; it was retained for inspection."
            fi
            sleep "$poll_interval_s"
            continue
          fi
          fail "Poster backfill Machine $machine_id stopped without an exit receipt; it was retained for inspection."
        fi
        exit_receipt_is_well_formed <<<"$exit_receipt" || \
          fail "Poster backfill Machine $machine_id has a malformed exit receipt; it was retained for inspection."
        exit_event="$(jq -cer '.request.exit_event' <<<"$exit_receipt")" || \
          fail "Poster backfill Machine $machine_id has a malformed exit receipt; it was retained for inspection."
        exit_timestamp="$(jq -r '.timestamp | tostring' <<<"$exit_receipt")"
        if exit_event_is_clean <<<"$exit_event"; then
          [[ -z "$acknowledge_failed_backfill_machine_id" ]] || \
            fail "Acknowledged poster backfill Machine $machine_id exited cleanly; rerun deploy without the failure acknowledgement."
          flyctl machine destroy --app "$app_name" "$machine_id" || \
            fail "Verified poster backfill Machine $machine_id could not be destroyed."
          echo "Verified and removed poster backfill Machine $machine_id (revision $revision)."
          RECONCILED_REVISION="$revision"
          return 0
        fi

        exit_summary="$(describe_exit_event <<<"$exit_event")"
        if [[ -n "$acknowledge_failed_backfill_machine_id" ]]; then
          [[ "$logs_retrieved" == "true" ]] || \
            fail "Poster backfill Machine $machine_id logs could not be preserved; it was retained."
          prove_backfill_guard_image_is_live "$machine_json"
          curl --fail --silent --show-error --retry 5 --retry-all-errors \
            --connect-timeout 10 --max-time 30 "$health_url" >/dev/null || \
            fail "Production health failed before releasing acknowledged backfill Machine $machine_id; it was retained."
          jq -c '
            {
              id,
              name,
              state,
              digest: .image_ref.digest,
              revision: (.config.metadata // .incomplete_config.metadata).nova_revision,
              latest_exit: ([.events[]? | select(.type == "exit")]
                | sort_by(.timestamp) | last)
            }
          ' <<<"$machine_json"
          echo "Explicitly acknowledging incomplete poster repair on Machine $machine_id ($exit_summary); preserved logs and receipt are printed above."
          flyctl machine destroy --app "$app_name" "$machine_id" || \
            fail "Acknowledged failed poster backfill Machine $machine_id could not be destroyed; it was retained."
          prove_acknowledged_backfill_guard_absent "$machine_id"
          echo "Removed acknowledged failed poster backfill guard $machine_id so merged revision $expected_sha can deploy its fix."
          return 0
        fi
        if [[ "$retry_started" == "true" && "$exit_timestamp" == "$retry_exit_timestamp" ]]; then
          echo "Waiting for acknowledged poster backfill retry $machine_id to publish a newer state or exit receipt..."
          if (( SECONDS >= deadline )); then
            fail "Poster backfill Machine $machine_id exceeded the reconciliation deadline; it was retained for inspection."
          fi
          sleep "$poll_interval_s"
          continue
        fi
        if [[ -z "$retry_failed_machine_id" ]]; then
          fail "Poster backfill Machine $machine_id did not exit cleanly ($exit_summary); explicit retry acknowledgement is required and the Machine was retained."
        fi
        if [[ "$retry_started" == "true" ]]; then
          fail "Acknowledged poster backfill Machine $machine_id failed again ($exit_summary); it was retained for inspection."
        fi
        [[ "$legacy" == "false" \
          && "$(jq -r '.name' <<<"$machine_json")" == "$guard_name" \
          && "$retry_failed_machine_id" == "$machine_id" \
          && "$revision" == "$expected_sha" ]] || \
          fail "Failed poster backfill retry acknowledgement does not exactly match the stable Machine ID and EXPECTED_SHA; it was retained."
        prove_backfill_guard_image_is_live "$machine_json"
        echo "Explicitly retrying acknowledged failed poster backfill Machine $machine_id..."
        flyctl machine start --app "$app_name" "$machine_id" || \
          fail "Acknowledged poster backfill Machine $machine_id could not be restarted; it was retained."
        retry_started=true
        retry_exit_timestamp="$exit_timestamp"
        ;;
      *)
        fail "Poster backfill Machine $machine_id has unexpected state '$state'; refusing to continue."
        ;;
    esac

    if (( SECONDS >= deadline )); then
      fail "Poster backfill Machine $machine_id exceeded the reconciliation deadline; it was retained for inspection."
    fi
    sleep "$poll_interval_s"
  done
}

deploy_guard_is_expired() {
  local machine_json="$1"
  local now deadline
  now="$(date -u +%s)" || fail "Could not read current UTC epoch for guard validation."
  deadline="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_guard_deadline_epoch' <<<"$machine_json")"
  (( now > deadline + guard_reclaim_grace_s ))
}

reclaim_or_block_deploy_guard() {
  local machine_json="$1"
  local allow_owned="${2:-false}"
  local machine_id owner revision
  deploy_guard_contract_is_valid "$machine_json" || \
    fail "Stable deploy guard violates its exact dormant contract; it was retained."
  machine_id="$(jq -r '.id' <<<"$machine_json")"
  owner="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_guard_owner' <<<"$machine_json")"
  revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$machine_json")"
  if [[ "$allow_owned" == "true" && "$owner" == "$guard_owner" && "$revision" == "$expected_sha" ]]; then
    echo "Deploy guard $machine_id is already held by $guard_owner for revision $expected_sha."
    ACQUIRED_DEPLOY_GUARD=true
    return 0
  fi
  if ! deploy_guard_is_expired "$machine_json"; then
    fail "Deploy guard $machine_id is owned by $owner and has not passed its validated deadline plus grace; it was retained."
  fi
  # A cancelled deploy may have left process Machines mixed or unhealthy. Keep
  # the mutex until the managed fleet is again one digest and has its required
  # API/worker/light/autoplace topology in a proven operational state; otherwise
  # a second mutation would make an ambiguous rollout worse.
  resolve_production_image false
  echo "Reclaiming expired, exact dormant deploy guard $machine_id owned by $owner..."
  flyctl machine destroy --force --app "$app_name" "$machine_id" || \
    fail "Expired deploy guard $machine_id could not be force-destroyed."
}

resolve_production_image() {
  local require_expected_revision="$1"
  local image_json machines_json managed_count unique_images deployed_sha image_match_count image_tag
  local attempt
  for ((attempt = 1; attempt <= production_settle_attempts; attempt++)); do
    machines_json="$(machine_list)" || fail "Could not read Fly Machine inventory."
    validate_reserved_inventory "$machines_json"
    managed_count="$(
      jq '[.[]
        | select(
            ((.config.metadata // .incomplete_config.metadata // {})
              | has("fly_process_group"))
          )]
        | length
      ' <<<"$machines_json"
    )" || fail "Fly managed Machine inventory is not JSON."
    unique_images="$(
      jq '[.[]
        | select(
            ((.config.metadata // .incomplete_config.metadata // {})
              | has("fly_process_group"))
          )
        | .image_ref.digest]
        | unique
        | length
      ' <<<"$machines_json"
    )" || fail "Fly managed Machine inventory has no digest data."
    [[ "$managed_count" -ge 1 && "$unique_images" == "1" ]] || \
      fail "Managed production Machines do not expose one immutable image digest; refusing mutation."
    jq -e '
      [.[]
        | select(
            ((.config.metadata // .incomplete_config.metadata // {})
              | has("fly_process_group"))
          )]
      | all(.image_ref.digest
        | type == "string" and test("^sha256:[0-9a-f]{64}$"))
    ' <<<"$machines_json" >/dev/null || \
      fail "Managed production Machines expose an invalid image digest; refusing mutation."
    managed_fleet_has_required_processes <<<"$machines_json" || \
      fail "Managed production Machines are missing a required api, worker, light, or autoplace process group; refusing mutation."
    if managed_fleet_is_operationally_stable <<<"$machines_json"; then
      break
    fi
    managed_fleet_has_safe_lifecycle_states <<<"$machines_json" || \
      fail "Managed production Machines expose an unsafe lifecycle state; refusing mutation."
    managed_fleet_has_transitional_states <<<"$machines_json" || \
      fail "Managed production Machines do not satisfy the required stable process topology; refusing mutation."
    if (( attempt == production_settle_attempts )); then
      fail "Managed production Machines did not reach the required stable process topology; refusing mutation."
    fi
    echo "Waiting for managed production Machines to settle (attempt $attempt/$production_settle_attempts)..."
    sleep "$poll_interval_s"
  done
  digest="$(
    jq -r '[.[]
      | select(
          ((.config.metadata // .incomplete_config.metadata // {})
            | has("fly_process_group"))
        )
      | .image_ref.digest]
      | unique
      | .[0]
    ' <<<"$machines_json"
  )"
  image_json="$(flyctl image show --app "$app_name" --json)" || \
    fail "Could not read Fly production image metadata."
  image_match_count="$(jq --arg digest "$digest" '[.[] | select(.Digest == $digest)] | length' <<<"$image_json")" || \
    fail "Fly image metadata is not JSON."
  registry="$(jq -r --arg digest "$digest" '[.[] | select(.Digest == $digest)][0].Registry // empty' <<<"$image_json")"
  repository="$(jq -r --arg digest "$digest" '[.[] | select(.Digest == $digest)][0].Repository // empty' <<<"$image_json")"
  [[ -n "$registry" && -n "$repository" && "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "Could not resolve a valid deployed registry, repository, and digest."
  image_tag="$(
    jq -er --arg digest "$digest" '
      [.[]
        | select(.Digest == $digest)
        | .Tag
        | select(type == "string" and length > 0)]
      | unique
      | if length == 1 then .[0] else empty end
    ' <<<"$image_json"
  )" || fail "Could not resolve one deployed image tag for the production digest."
  [[ -n "$image_tag" ]] || fail "Could not resolve one deployed image tag for the production digest."
  [[ "$image_match_count" -ge 1 ]] || \
    fail "Managed production digest has no matching immutable image metadata."
  # flyctl resolves and appends the digest internally for `machine create`.
  # Passing an already digest-qualified reference produces
  # `repository@sha256:...@sha256:...`, which Fly rejects. The deployment tag
  # is unique, and the created-only guard is accepted only after its resolved
  # image_ref.digest exactly matches `digest` below.
  image_ref="${registry}/${repository}:${image_tag}"
  if [[ "$require_expected_revision" == "true" ]]; then
    if ! deployed_sha="$(
      jq -er --arg label "$revision_label" --arg digest "$digest" '
        [.[]
          | select(.Digest == $digest)
          | .Labels
          | if type == "string" then fromjson else . end
          | .[$label]]
        | unique
        | if length == 1 then .[0] else empty end
        | select(type == "string" and test("^[0-9a-f]{40}$"))
      ' <<<"$image_json"
    )"; then
      fail "Could not resolve one deployed image revision label."
    fi
    [[ "$deployed_sha" == "$expected_sha" ]] || \
      fail "Fly image SHA $deployed_sha does not match expected deploy $expected_sha."
  fi
}

resolve_guard_after_create() {
  local attempt list_json machine_json
  RESOLVED_GUARD_JSON=""
  for ((attempt = 1; attempt <= guard_resolve_attempts; attempt++)); do
    list_json="$(machine_list)" || fail "Could not list Fly Machines after guard create."
    validate_reserved_inventory "$list_json"
    if machine_json="$(machine_by_guard_name <<<"$list_json")"; then
      RESOLVED_GUARD_JSON="$machine_json"
      return 0
    fi
    if (( attempt < guard_resolve_attempts )); then
      sleep "$poll_interval_s"
    fi
  done
  return 1
}

run_create_command() {
  local output_file="$1"
  shift
  set +e
  "$@" >"$output_file" 2>&1
  CREATE_STATUS=$?
  set -e
  CREATE_OUTPUT="$(<"$output_file")"
  printf '%s\n' "$CREATE_OUTPUT"
}

inspect_existing_reservations() {
  local purpose="$1"
  local list_json legacy_id stable_json operation machine_id
  list_json="$(machine_list)" || fail "Could not list existing Fly Machines."
  validate_reserved_inventory "$list_json"

  while IFS= read -r legacy_id; do
    [[ -z "$legacy_id" ]] || reconcile_backfill_machine "$legacy_id" true
  done < <(
    jq -r --arg operation_key "$operation_key" --arg operation "$backfill_operation" '
      .[]
      | select((.name // "") | startswith("poster-backfill-"))
      | select((.config.metadata // .incomplete_config.metadata // {})[$operation_key] == $operation)
      | .id
    ' <<<"$list_json"
  )

  list_json="$(machine_list)" || fail "Could not refresh Fly Machine inventory."
  validate_reserved_inventory "$list_json"
  if ! stable_json="$(machine_by_guard_name <<<"$list_json")"; then
    return 0
  fi
  operation="$(machine_operation <<<"$stable_json")"
  machine_id="$(jq -r '.id' <<<"$stable_json")"
  case "$operation" in
    "$backfill_operation")
      reconcile_backfill_machine "$machine_id" false
      ;;
    "$deploy_operation")
      if [[ "$purpose" == "acquire-deploy" ]]; then
        reclaim_or_block_deploy_guard "$stable_json" true
      else
        reclaim_or_block_deploy_guard "$stable_json" false
      fi
      ;;
    *)
      fail "Stable Fly mutation guard has unknown operation '$operation'; it was retained."
      ;;
  esac
}

acquire_deploy_guard() {
  local attempt now guard_deadline output_file operation machine_id owner revision actual_digest state
  local -a create_args
  ACQUIRED_DEPLOY_GUARD=false
  for ((attempt = 1; attempt <= guard_resolve_attempts; attempt++)); do
    inspect_existing_reservations "acquire-deploy"
    if [[ "$ACQUIRED_DEPLOY_GUARD" == "true" ]]; then
      return 0
    fi
    resolve_production_image false
    now="$(date -u +%s)" || fail "Could not read current UTC epoch for deploy guard."
    guard_deadline=$((now + deploy_guard_lease_s))
    output_file="$(mktemp)"
    create_args=(
      flyctl machine create "$image_ref"
      --app "$app_name"
      --region "$region"
      --name "$guard_name"
      --metadata "$operation_key=$deploy_operation"
      --metadata "nova_guard_owner=$guard_owner"
      --metadata "nova_guard_created_epoch=$now"
      --metadata "nova_guard_deadline_epoch=$guard_deadline"
      --metadata "nova_revision=$expected_sha"
      --metadata "nova_image_digest=$digest"
      --restart no
      --vm-cpu-kind shared
      --vm-cpus 1
      --vm-memory 256
      -- "$deploy_guard_command"
    )
    run_create_command "$output_file" "${create_args[@]}"
    rm -f "$output_file"
    if ! resolve_guard_after_create; then
      fail "Deploy guard create returned status $CREATE_STATUS but the stable name could not be resolved; no deploy was started."
    fi
    operation="$(machine_operation <<<"$RESOLVED_GUARD_JSON")"
    if [[ "$operation" == "$deploy_operation" ]]; then
      if deploy_guard_contract_is_valid "$RESOLVED_GUARD_JSON"; then
        machine_id="$(jq -r '.id' <<<"$RESOLVED_GUARD_JSON")"
        owner="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_guard_owner' <<<"$RESOLVED_GUARD_JSON")"
        revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$RESOLVED_GUARD_JSON")"
        actual_digest="$(jq -r '.image_ref.digest' <<<"$RESOLVED_GUARD_JSON")"
        state="$(jq -r '.state' <<<"$RESOLVED_GUARD_JSON")"
        if [[ "$owner" == "$guard_owner" && "$revision" == "$expected_sha" \
          && "$actual_digest" == "$digest" \
          && ( "$state" == "created" || "$state" == "stopped" ) ]]; then
          echo "Acquired dormant deploy guard $machine_id for $guard_owner at revision $expected_sha."
          return 0
        fi
      fi
      reclaim_or_block_deploy_guard "$RESOLVED_GUARD_JSON" false
      continue
    fi
    if [[ "$operation" == "$backfill_operation" ]]; then
      backfill_contract_is_valid "$RESOLVED_GUARD_JSON" || \
        fail "Create conflict resolved to an invalid poster backfill guard; it was retained."
      reconcile_backfill_machine "$(jq -r '.id' <<<"$RESOLVED_GUARD_JSON")" false
      continue
    fi
    fail "Create conflict resolved to an unknown stable guard operation; it was retained."
  done
  fail "Could not acquire the stable deploy guard after $guard_resolve_attempts attempts."
}

release_deploy_guard() {
  local list_json machine_json machine_id owner revision
  list_json="$(machine_list)" || fail "Could not list Fly Machines while releasing deploy guard."
  validate_reserved_inventory "$list_json"
  if ! machine_json="$(machine_by_guard_name <<<"$list_json")"; then
    echo "No stable deploy guard remains to release."
    return 0
  fi
  deploy_guard_contract_is_valid "$machine_json" || \
    fail "Stable Machine is not an exact dormant deploy guard; it was retained."
  machine_id="$(jq -r '.id' <<<"$machine_json")"
  owner="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_guard_owner' <<<"$machine_json")"
  revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$machine_json")"
  [[ "$owner" == "$guard_owner" && "$revision" == "$expected_sha" ]] || \
    fail "Deploy guard $machine_id is not owned by this workflow and revision; it was retained."
  [[ "$verified_deploy_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "VERIFIED_DEPLOY_DIGEST must be the exact digest from the completed deploy verification; the guard was retained."
  resolve_production_image true
  [[ "$digest" == "$verified_deploy_digest" ]] || \
    fail "Managed production digest no longer matches the verified deploy receipt; the guard was retained."
  flyctl machine destroy --force --app "$app_name" "$machine_id" || \
    fail "Owned deploy guard $machine_id could not be force-destroyed."
  echo "Released dormant deploy guard $machine_id for revision $expected_sha."
}

run_backfill() {
  local attempt now guard_deadline output_file operation machine_id owner revision actual_digest state
  local acquired_digest
  local -a create_args
  for ((attempt = 1; attempt <= guard_resolve_attempts; attempt++)); do
    inspect_existing_reservations "backfill"
    if [[ "$RECONCILED_REVISION" == "$expected_sha" ]]; then
      echo "A durable Machine receipt already completed and verified revision $expected_sha."
      return 0
    fi
    resolve_production_image true
    now="$(date -u +%s)" || fail "Could not read current UTC epoch for backfill guard."
    guard_deadline=$((now + backfill_guard_lease_s))
    output_file="$(mktemp)"
    create_args=(
      flyctl machine create "$image_ref"
      --app "$app_name"
      --region "$region"
      --name "$guard_name"
      --metadata "$operation_key=$backfill_operation"
      --metadata "nova_guard_owner=$guard_owner"
      --metadata "nova_guard_created_epoch=$now"
      --metadata "nova_guard_deadline_epoch=$guard_deadline"
      --metadata "nova_revision=$expected_sha"
      --metadata "nova_image_digest=$digest"
      --restart no
      --vm-cpu-kind shared
      --vm-cpus 4
      --vm-memory 8192
      -- /usr/bin/timeout --signal=TERM --kill-after=300s 18000s /bin/bash -lc "$backfill_command"
    )
    run_create_command "$output_file" "${create_args[@]}"
    rm -f "$output_file"
    if ! resolve_guard_after_create; then
      fail "Poster backfill guard create returned status $CREATE_STATUS but the stable name could not be resolved; it was not started."
    fi
    operation="$(machine_operation <<<"$RESOLVED_GUARD_JSON")"
    if [[ "$operation" == "$backfill_operation" ]]; then
      backfill_contract_is_valid "$RESOLVED_GUARD_JSON" || \
        fail "Stable poster backfill guard violates the bounded execution contract; it was retained."
      machine_id="$(jq -r '.id' <<<"$RESOLVED_GUARD_JSON")"
      owner="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_guard_owner' <<<"$RESOLVED_GUARD_JSON")"
      revision="$(jq -r '(.config.metadata // .incomplete_config.metadata).nova_revision' <<<"$RESOLVED_GUARD_JSON")"
      actual_digest="$(jq -r '.image_ref.digest' <<<"$RESOLVED_GUARD_JSON")"
      state="$(jq -r '.state' <<<"$RESOLVED_GUARD_JSON")"
      if [[ "$owner" == "$guard_owner" && "$revision" == "$expected_sha" \
        && "$actual_digest" == "$digest" && "$state" == "created" ]]; then
        # The name is the CAS point. Re-read production only after winning it,
        # then prove the immutable digest and OCI revision still match before
        # starting any data mutation.
        acquired_digest="$actual_digest"
        resolve_production_image true
        [[ "$digest" == "$acquired_digest" ]] || \
          fail "Production image changed after backfill guard acquisition; the created guard was retained and never started."
        # A retryable image-preparation precondition is not fatal here: the
        # reconciliation below re-reads the Machine, re-proves the contract,
        # and starts it under its own bounded deadline.
        start_backfill_machine "$machine_id" "acquired" || true
      fi
      reconcile_backfill_machine "$machine_id" false
      if [[ "$RECONCILED_REVISION" == "$expected_sha" ]]; then
        return 0
      fi
      continue
    fi
    if [[ "$operation" == "$deploy_operation" ]]; then
      reclaim_or_block_deploy_guard "$RESOLVED_GUARD_JSON" false
      continue
    fi
    fail "Create conflict resolved to an unknown stable guard operation; it was retained."
  done
  fail "Could not acquire the stable poster backfill guard after $guard_resolve_attempts attempts."
}

case "$mode" in
  --acquire-deploy-guard)
    acquire_deploy_guard
    ;;
  --release-deploy-guard)
    release_deploy_guard
    ;;
  --reconcile-only)
    inspect_existing_reservations "reconcile"
    echo "No active or failed poster backfill or deploy guard remains."
    ;;
  run)
    run_backfill
    ;;
esac
