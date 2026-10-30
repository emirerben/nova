# Local notification runtime

This slice lets an authenticated native installation register an encrypted target and
lets committed current Activity records reach a recording transport in offline tests.
Production activation, credentials, notification permission, and device receipt are
outside this slice. The default policy disables registration and delivery.

## Types and ownership

`VerifiedMobilePrincipal` carries a verified user ID and database-derived session
family ID. Proxy authentication cannot construct it through HTTP.
`NotificationInstallation` owns one account-scoped installation UUID, encrypted
token, target fingerprint, preference, family binding, and monotonically increasing
registration version. The UUID cannot transfer owners, even after revocation.
An active target fingerprint cannot transfer owners until its old registration is
revoked. A different account then needs a fresh installation UUID.
The existing explicit family revoke and refresh replay branches revoke matching
registrations in the same database transaction. Refresh, logout, replay handling,
and registration acquire one PostgreSQL transaction advisory lock for the family
before locking any session member or installation. Refresh and logout first find
the family without a row lock, acquire the family lock, and reread the member with
`populate_existing` and `FOR UPDATE`. This prevents logout from missing a refresh
successor that was uncommitted when logout began. Registration checks for a live
member after acquiring the family lock. A missed offline logout with a still-live family
continues to reject transfer. Expiry does not silently transfer token ownership.
`NotificationDelivery` owns one Activity/installation pair and a fixed registration
version. Its persisted state is pending, leased, accepted, suppressed, rejected,
exhausted, or uncertain. A delivery never restarts after a terminal result.

The route validates input and delegates registration to the service. The service
discovers committed Activities, applies current eligibility, claims one delivery,
calls an injected transport, and conditionally stores its result. Celery entrypoints remain disabled under the default configuration. The later wiring checkpoint below adds resource-free sender construction and conditional dispatch scheduling. Existing Activity writers
and the isolated APNs transport remain unchanged.

## Failure model

Repeated registration preserves its version unless target, family, revocation, or
preference changes. Duplicate discovery cannot create another delivery for the same
pair. Concurrent claims have a single persisted lease. An expired lease becomes
uncertain instead of being resent because a crashed worker may already have sent.
Only explicit retryable provider rejection can return to pending, within finite
attempt and freshness bounds. Timeout, connection failure, protocol uncertainty,
and cancellation never authorize an automatic resend.

Dispatch checks current Activity ownership, generation, export proof, unread state,
installation version, preference, freshness, and live session family immediately
before admission. No database lock remains held over network I/O. Revocation or
ownership changes after admission cannot retract an in-flight generic alert.
The worker records this bounded race rather than claiming atomic revocation and send.
A stale completion cannot update a different lease or revoke a rotated target.
Admission captures the registration's last-seen timestamp. Token invalidation
requires that exact renewal fence as well as the registration version, including
responses without a usable provider timestamp. Identical out-of-order receipts
cannot decrease last-seen time. A changed registration with a receipt timestamp
at or before the stored last-seen time is rejected instead of overwriting newer
token, preference, or family state.
Timestamped provider invalidation must be at least as recent as `last_seen_at`.
That field records receipt of authenticated token registration, including an
identical PUT. It is not proof that APNs issued a fresh token callback.
APNs acceptance does not prove device receipt.

The assumed utility limits are one alert per installation per 60 seconds and at
most three per rolling hour. Accepted and uncertain completions count at completion
time. An active lease reserves a slot immediately and remains conservative until
its result is stored. The installation lock serializes admission for distinct
deliveries. Deferred rows stay pending without consuming an attempt. Every later
admission rechecks the ten-minute Activity lifetime, so a deferred stale alert is
suppressed. These limits use existing timestamps and do not imply device receipt.

## Acceptance and evidence plan

Use real PostgreSQL, actual verified bearer authentication, HTTP registration,
existing Activity generation/proof code, durable discovery and claims, and only a
fake APNs transport. Assert one accepted delivery across repeated discovery and
dispatch. Negative controls cover invalid auth, cross-account target theft, missing
proof, changed generation/ownership, read state, disabled preference/policy,
revoked family, expired freshness, old token rejection after rotation, concurrent
claims, stale lease, retry exhaustion, and uncertain transport outcomes.

This is offline runtime evidence. Native notification receipt and production
exposure remain unverified. This local slice is separate from the Activity work
in PR 1939 and does not modify its source writers.

## Offline verification

On 2026-10-30, migration 0129 applied successfully to the isolated local database
`nova_notification_runtime_local_test`. No existing database was reset or dropped.
The uncommitted implementation was based on `2457ba6a6336fd42b9bb6fac61bb3c9894f4d117`.

The initial focused regression command passed 147 tests with no failures:

```sh
python -m pytest tests/routes/test_notification_delivery_postgres.py \
  tests/routes/test_mobile_auth.py tests/routes/test_native_activity_postgres.py \
  tests/services/test_apns.py -q --tb=short
```

The database URL came only from this worktree's `.dev/test-db-url`. The new suite
uses ephemeral encryption and signing fixtures. Its 36 cases cover real HTTP and
PostgreSQL, existing Activity proof checks, fake Celery dispatch, and fake APNs.
The remaining tests exercise existing authentication, Activity, and transport
behavior. Reported warnings concern existing framework deprecations.

Both volume negative controls failed before admission limiting was added. A
delayed timestamped invalidation control also failed before the last-seen fence
was added. They pass with the fixes. Current token rejection still revokes the
matching registration. Scoped Ruff and `git diff --check` passed.

At this earlier checkpoint, `notification_sender()` returned `None`. The wiring checkpoint below supersedes that code limitation. These results do not prove live
dispatch, APNs device receipt, production activation, or delivery after offline
logout. Production sender construction and activation remain separate work.

Independent review then reproduced three lifecycle failures in real PostgreSQL.
An untimestamped provider invalidation revoked a renewed token. An older identical
registration decreased its last-seen timestamp. Logout blocked behind refresh's
member lock but missed the uncommitted successor in its UPDATE snapshot.
The controlled red run and source hashes are retained locally in
`.dev/notification-race-red.log` and `.dev/notification-race-red-sources.json`.
The latter records the actual queried Alembic revision `0129`.

The correction adds the renewal fence and the shared family transaction lock
described above. Additional controls cover malformed provider timestamps, stale
changed receipts, concurrent refresh followed by logout or replay, logout followed
by refresh, and both registration/logout orders. Tests observe actual PostgreSQL
lock waits and use bounded task and database timeouts. The intermediate focused
notification and mobile-auth suite passed all 80 cases after these changes.

The final affected regression run passed 157 tests, including all 46 notification
cases. Its raw output is retained in `.dev/notification-race-green.log`, with source
hashes and another actual `0129` version query in
`.dev/notification-race-green-sources.json`. The verification runner checked those
source hashes again after the tests to detect edits during the run. Scoped Ruff
and `git diff --check` also passed. These artifacts remain local and contain no
production credentials or device tokens.


## Native preparation and remaining activation gates

The local client preparation passed 105 tests with one existing unsigned-simulator Keychain entitlement skip. It includes explicit opt-in, owner-bound registration and Activity navigation, token rotation and revocation handling, and atomic session replacement. Independent review returned PASS after account-switch and delayed-expiry controls were fixed. The skip does not establish physical-device Keychain behavior.

Native temporary source-preview Save/Share export now has a tested continued-processing wrapper with one-shot execution, progress, cancellation and expiration cleanup. Its final focused suite passed 22 tests without skips and received independent PASS with notes. This does not turn temporary exports into durable DeviceRender jobs, add checkpoint/resume, or emit Activity from a temporary file. GPU admission remains disabled. No entitlement, provisioning profile, permission, or background capability was changed.

The published #1941 branch remains at `28982e471ae687d56c419fa0bc19bfd37e362c3d`. The fuller preparation above was developed on local base `2457ba6a6336fd42b9bb6fac61bb3c9894f4d117`. It must integrate the actual #1939 merge, including byte publication proof and the founder's “Export available” wording without an inbox warning, before fresh validation and publication. Adapter-only evidence from the published branch does not establish these runtime callers.

The later wiring checkpoint adds live sender construction and conditional discovery code. Neither is activated. The default configuration still returns no sender and adds no notification Beat entry. Actual APNs credentials, bundle topic/environment, signing/provisioning capability, device permission, and a reachable phone need separate confirmation. APNs acceptance still cannot prove delivery.

## First physical-phone acceptance test

The paired iPhone13Pro was unavailable in the latest device inventory. Once connected and unlocked, first verify a signed candidate containing #1939 and the matching deployed backend/migration. Use one disposable project with an existing approved recipe and local source files, without requesting new paid analysis. Complete its native export. Open Activity and check that one “Export available” entry opens that exact project and its current export plays. Background the app, reopen it, and check that the entry remains durable. An older-generation entry must not open a cached project. A developer-controlled synthetic fixture is required for duplicate, action-required, and stale-event injections; do not damage creator media to manufacture them.

This first test needs no push permission. Push receipt on foreground, background and locked phone is a later, explicitly scoped device trial after sender, credential and signing gates. Continued native rendering is another separate device test with expiration/cancellation and supported-device controls. Force-quit is not a promise of continued rendering, and no server-render fallback is proposed.


## Sender and dispatch wiring checkpoint

The local implementation now connects validated configuration, registration capability, sender construction and conditional one-minute discovery. A complete configuration and a nonempty approved-owner allowlist are required. Capabilities and registration are unavailable to other owners. Discovery excludes them, and pending admission is suppressed if an owner is removed. Authenticated revocation remains available after removal. This is code preparation; no server setting was changed.

The immutable configuration validates the exact topic and APNs environment, ten-character Team and Key IDs, an ES256/P-256 private key and the existing Fernet encryption configuration. Secret material remains wrapped and parser failures return disabled state without logging raw inputs. A bounded process/configuration cache reuses signed bearers for fifty minutes. Clock rollback fails closed. Signing and HTTP construction do not occur during discovery or schedule creation.

Each admitted send owns a fresh HTTP/2 transport, so separate Celery task event loops cannot reuse one another's pools. A seventeen-second total budget bounds delivery and cleanup. A known accepted or rejected APNs response survives cleanup errors or hanging cleanup. Unknown results retain terminal uncertainty without another automatic provider send. The existing rate, freshness, revocation, owner and generation checks remain authoritative. Both discovery and delivery are explicitly routed to the maintenance queue.

The final affected backend suite passed 186 tests with zero skips. It includes real mobile authentication, registration, durable Activity discovery, actual ephemeral-key signing, fake HTTP/2 acceptance, durable completion and duplicate suppression. Worker/configuration contracts passed 27 tests. CI selectors passed 41 tests and 183 subtests. Five controlled regressions failed when sender construction was removed, account scope was ignored, or cleanup erased known results. Source hashes and commands are retained in `.dev/notification-runtime-wiring-sources.json`. Independent read-only review matched all eight hashes and returned PASS. These results do not establish a live Celery broker or APNs receipt.

The native configuration adapter also now reads a missing-default-false build declaration and an exact permitted identifier wildcard. The focused simulator result `.dev/runtime-native-config.xcresult` reports 24 passed, zero failed and zero skipped. No Info.plist, entitlement or signing configuration was changed. OS GPU support and successful submission remain required; a build declaration alone is not entitlement proof. This checkpoint covers the existing temporary Save/Share path. Actual durable-job continued-processing integration is being implemented separately and must pass its own run, initiation, expiration and receipt tests.

## Exact activation inputs, pending separate approval

| Area | Required input or action | Current state |
| --- | --- | --- |
| Server switch | `NATIVE_NOTIFICATIONS_ENABLED=true` only for the approved trial. Default remains false. | Not configured or activated. |
| Account scope | `NATIVE_NOTIFICATIONS_ALLOWED_OWNER_IDS` as a nonempty JSON array of exact authenticated trial account UUIDs. | Empty by default. No owner was guessed or enrolled. |
| APNs binding | `NATIVE_NOTIFICATIONS_APNS_ENVIRONMENT`, `NATIVE_NOTIFICATIONS_APNS_TOPIC`, `NATIVE_NOTIFICATIONS_APNS_TEAM_ID`, `NATIVE_NOTIFICATIONS_APNS_KEY_ID`. | No values configured. Topic must match the actual signed app. Production source declares `com.emirerben.kria`; development and staging identifiers differ. |
| Signing secret | `NATIVE_NOTIFICATIONS_APNS_PRIVATE_KEY`, the approved existing APNs P-256 `.p8` material. | No key inspected, created or stored. Creating a key would be a separate action. |
| Token encryption | Existing valid `TOKEN_ENCRYPTION_KEY`, preserved rather than replaced. | Test-only ephemeral keys validated. No production inspection or rotation. |
| Worker activation | Deploy reviewed code/migration, restart the API and maintenance/Beat fleet with the same approved configuration. | Not performed. Configuration changes require process restart. Database revocation remains the immediate opt-out boundary. |
| iPhone push | Signed provisioning profile with the matching `aps-environment` entitlement and app's Push Notifications capability; explicit creator opt-in and OS notification consent. | No capability, signing asset or permission changed. No real push sent. |
| Native continued processing | Compatible installed iOS/device resources, approved GPU entitlement `com.apple.developer.background-tasks.continued-processing.gpu`, missing-default-false `KriaContinuedProcessingExportEnabled`, exact `<signed bundle>.continued-export.*` permitted identifier. | No build declaration or capability activated. Durable-job integration and real-device acceptance remain separate gates. |
| Physical acceptance | Available connected/unlocked phone and a verified signed build/backend. | Latest paired iPhone13Pro inventory is unavailable. |

APNs environment must come from the chosen signed build. Do not treat a Debug token as a TestFlight production token or silently try both environments. Apple documents the [token authentication requirements](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns). The installed iOS26.5 SDK states that GPU submission requires the entitlement and does not guarantee runtime under device contention. No silent push or force-quit workaround is proposed.


## Provisional integration with Activity head

An isolated integration combines the reviewed local runtime commit `40357e33b42451e2cb147a5bae114cce4ad10af0` with published Activity head `258eec1f89b20089d31ef861799805a44ff2f4ac`. This is local preparation while #1939 remains unmerged, not dependency completion or rollout. The three merge conflicts were limited to the journey registry, selector expectations and one atomic-session test. The registry preserves all 116 Activity/main entries unchanged and appends the three existing notification entries. The atomic-session test keeps the Activity endpoint control; separate notification account tests retain registration coverage.

The merged production publication route, Activity proof service and both proof/publication test files are byte-identical to #1939. The earlier notification fixture correctly failed because a bare published status lacked the required server proof. It now uses the existing authoritative synthetic publication fixture. Additional controls remove the proof, alter its checksum and replace the artifact pointer after discovery; all suppress dispatch without transport calls. No production proof requirement was weakened.

The combined affected backend run passed 415 tests without skips. Selector checks passed 41 tests and 183 subtests. These logs are retained in the provisional integration worktree under `.dev/provisional-integration-api.log` and `.dev/provisional-selectors.log`; the initial expected fixture failure remains in `.dev/integration-proof-fixture-red.log`. Native and final background integration checks remain a separate subsequent gate.
