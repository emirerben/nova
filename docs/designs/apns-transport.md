# APNs transport boundary

This isolated adapter sends a generic activity alert. Nothing imports it from a
route, worker, or application startup. NativeActivity remains the durable event
written in the terminal-result transaction. This adapter has no delivery ledger.

## Data shape and ownership

`APNsTarget` holds a sandbox or production environment, a validated bundle topic,
and a `SecretStr` device token. `ActivityNotification` holds activity and
notification UUIDs. The only custom payload field is `activity_id`; the alert is
always `Open Kria to see an update.` No caller can provide a title, URL, media,
user identifier, or arbitrary payload. Results are `Accepted`, `Rejected`, or
`TransportUncertain`. Accepted means APNs accepted a request, never receipt.

The parent selected this isolated typed transport over a database delivery ledger.
Model the Domain shaped the choice of a result union and immutable input records.
The existing activity transaction already owns event durability. Registration,
recipient eligibility, delivery claims, and retry policy belong to future work.

## Failure modes and acceptance criteria

Written before implementation and behavioral tests.

| Failure | Required behavior |
| --- | --- |
| Token path injection, odd hex, invalid environment or topic | Static input error before any HTTP call; secret absent from repr and error text. |
| Invalid bearer or provider failure | No HTTP call; never include provider exception or bearer in output. |
| Private activity content leaks | Exact fixed payload plus UUID only; reject additional constructor fields. |
| Redirect or environment confusion | Fixed Apple hosts, no redirect following, no proxy environment, HTTP/2 only. |
| Connection, timeout, protocol, interrupted response | Uncertain result; no implicit retry or receipt claim. |
| Read deadline followed by stalled response close | Independent one-second cleanup deadline; timeout outcome without a hanging send. |
| External cancellation followed by cleanup failure or stall | Preserve cancellation; never replace it with a transport result or raw exception. |
| APNs 200 | Accepted only with an empty HTTP/2 response. |
| APNs 410 | Allowlisted unregistered result; optional nonnegative integer invalidation timestamp in milliseconds, never a guessed time. |
| APNs 400 or 403 | Separate token/request and provider categories; only allowlisted reason codes survive. |
| APNs 429 or 5xx | Retryable rejection category, with no retry performed. |
| Unknown, malformed, oversized response | Bounded parsing and safe fallback; arbitrary body and headers never escape. |
| Duplicate execution | Stable notification and collapse IDs, no exactly-once claim. |
| Offline phone or logout | Expiration zero avoids APNs storage; no offline logout guarantee. |

The adapter uses `httpx.AsyncHTTPTransport` directly, avoiding AsyncClient's URL
logging. It does not emit logs, retain raw responses, or propagate transport
exceptions. HTTPcore debug and custom transport tracing can expose wire data and
must remain disabled for APNs. This is not a general logging redaction layer.
Caller-supplied test transports must obey the same secrecy contract. No request,
response, or exception object should enter telemetry. Task cancellation propagates.

The asynchronous request/read budget is 15 seconds. Response cleanup has an
independent one-second budget, including when the request deadline has already
cancelled the read. Request plus response cleanup therefore has a maximum budget
of 16 seconds with cooperative asynchronous transports and event-loop scheduling.
A fully parsed outcome survives cleanup failure or timeout, so a known acceptance
is never downgraded to uncertainty by cleanup alone. The request deadline ends
before cleanup begins. Existing request errors retain their classification despite
cleanup errors. External cancellation propagates, including during cleanup after
a known acceptance. When no complete outcome or request error exists, cleanup
timeout returns `TransportUncertain("timeout")` and other cleanup errors are
sanitized. Synchronous bearer generation
is outside this budget. A custom transport that blocks the event loop or suppresses
cancellation cannot be bounded by asyncio timeouts. Context-manager exit closes the
reusable transport separately; that transport-wide close is not time bounded by
this adapter. Its errors remain sanitized and cancellation propagates.

## Verification and scope

The observable contract is the outgoing request and returned outcome. Offline
MockTransport tests exercise the real request builder and response classifier.
Wrong-content controls reject arbitrary payload fields, inspect exact JSON and
headers, and check zero calls on invalid inputs. Fixtures contain fake credentials.
No device, live APNs, production, or notification receipt is verified.

Throughput checkpoint:

- Blocking first steps were repository guidance, Apple documentation, and this failure matrix.
- Independent workstreams are the parent's native changes and these adapter-only files.
- Shared mutable state is absent from this adapter. It owns one reusable HTTP transport.
- The smallest safe decomposition is one worker for the typed transport and its tests.

## Activation blockers

A separate change must add APNs entitlement and permission UX, authenticated device
registration with environment/topic validation, encrypted token storage, and a
revocation lifecycle bound to the mobile session family. Session IDs rotate on
refresh and are not a stable binding. Recheck eligibility before dispatch and
before navigation. An offline logout cannot guarantee immediate server revocation.

Activation also requires an injected signing provider with key rotation and JWT
refresh, bounded worker dispatch from committed activities, delivery-attempt
ownership and retry policy, and token invalidation handling that respects newer
registrations. Stable IDs and collapse behavior do not guarantee exactly once.
Use expiration zero to avoid stale stored alerts. Add device receipt/navigation
verification and deployment controls before enabling any runtime integration.

## Primary references

- [Apple request contract](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns).
- [Apple response contract](https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns).
- [Apple archived provider protocol](https://developer.apple.com/library/archive/documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/CommunicatingwithAPNs.html).

## Recorded offline evidence

The [behavioral fixtures](../../src/apps/api/tests/services/test_apns.py) were
written before the adapter. Initial collection failed because the module did not
exist. The additional close-error control failed with a fabricated secret before
the close boundary was sanitized. The final focused run passed 64 tests. The stalled streaming-read/close regression
failed before the cleanup fix with `request deadline left send stuck in response
cleanup`. The cancellation/close-error regression failed with `DID NOT RAISE
CancelledError`. Both now pass, alongside completed-read/stalled-close and
external-cancellation/stalled-close controls. These fixtures accelerate the timeout
clock and use an independent watchdog to detect a hanging send without live APNs.
The additional accepted-response/cleanup-failure controls first failed with timeout
and protocol uncertainty, then passed after complete outcomes were preserved.
Cancellation during cleanup after acceptance also propagates in the final suite.

Run from `src/apps/api`:

```sh
python -m pytest tests/services/test_apns.py -q
python -m ruff check app/services/apns.py tests/services/test_apns.py
python -m ruff format --check app/services/apns.py tests/services/test_apns.py
```

Response bytes are bounded before parsing and are never decompressed. A compressed
100 KB negative control verifies that content-encoding cannot bypass the bound.

The selected environment used the existing API virtual environment at
`/Users/emirerben/Projects/nova/src/apps/api/.venv/bin/python`. Lint and diff
whitespace checks passed. Constructing and closing the real HTTP/2 transport
also passed without opening a connection. The dependency now declares
`httpx[http2]` so clean installations include HTTP/2 support.

These results establish the local transport contract only. Tests do not prove
credential validity, Apple acceptance, device notification presentation, tap
routing, logout revocation, or production delivery. The parent owns revision
binding and broader journey evidence before a PR.
