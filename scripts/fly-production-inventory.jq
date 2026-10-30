# The Machines that count as the production fleet and guard holders.
#
# `flyctl deploy` runs the release command (alembic) on a one-shot Machine whose
# `fly_process_group` is `fly_app_release_command`, on the NEW image, and destroys it
# afterwards. When the deploy aborts first (flyctl's wait times out on a slow image pull),
# that Machine is left behind `stopped`. It serves nothing and is not part of the fleet, but
# it carries `fly_process_group`, so every "managed Machine" digest/topology check counted it
# and a stopped orphan on a different digest blocked every later deploy.
#
# Only a `stopped` release-command Machine is dropped. One in any other state (created,
# starting, started, stopping, replacing, ...) may still be running a migration, so it stays
# in the inventory and keeps the digest and lifecycle checks failing closed.
map(select(
  ((.config.metadata // .incomplete_config.metadata // {}).fly_process_group) as $group
  | ($group == "fly_app_release_command" and .state == "stopped") | not
))
