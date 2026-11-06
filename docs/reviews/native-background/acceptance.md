# Native background and push foundations acceptance

The founder wants videos prepared while away and a truthful notification when ready. Final rendering must stay on iPhone. This branch prepares the next boundaries after PR1939 without activating either external delivery or extended execution.

## Observable contract

- Existing native rendering, publication proofs, inbox behavior, permissions and credentials remain unchanged.
- The APNs adapter emits only fixed generic copy and an opaque activity ID to a fixed Apple endpoint. An accepted HTTP response means provider acceptance, never device receipt.
- Malformed targets never cause HTTP. Unknown, retryable, invalid-token and uncertain transport outcomes remain distinct. No automatic retry or credential configuration occurs.
- Continued-processing admission requires a direct foreground creator action and required GPU support. Automatic recover/reconcile never becomes a creator action.
- Admission rejection, late callback, expiration and repeated completion cannot start duplicate work or complete a task twice. Expiration is interruption, not a creator Stop or published success.
- No app startup, Save/Share or worker wiring, plist capability, entitlement, APNs registration, real notification or production rollout is introduced.

## Failure matrix and negative controls

APNs cases include sandbox/production host separation, topic/header injection, malformed token, redirect, malformed/unknown response, provider authentication rejection, throttling, server failure, timeout after possible transmission and stale invalidation timestamp. Private titles/media URLs and raw credential-bearing exceptions must not enter results.

Native cases include non-user work, inactive scene, missing GPU, denied admission, delayed launch after rejection, expiry before/after completion, repeated expiration, late progress, and simultaneous exports. Fake scheduler tests establish contract behavior only. Physical-device GPU execution, locked-device files, thermal limits, indefinite runtime, Photos permission and final-output parity remain unverified.

## Required activation work

Push needs an authenticated mobile-session-family installation registry, token rotation and account-switch/revocation policy, current activity checks before dispatch, revision-fenced invalidation, durable bounded delivery attempts, tap authentication, explicit user preference/permission UI and approved credentials. Native execution needs approved GPU capability and scheduler plist configuration, a fresh foreground gesture at submission, cancellation-aware export integration, durable recovery for the chosen path and physical-device evidence. Temporary editor Save/Share output is not the durable DeviceRenderReceipt path.

No customer journey is claimed complete by these foundations. KRI-1039 remains the coordination context. No external tickets are created.
