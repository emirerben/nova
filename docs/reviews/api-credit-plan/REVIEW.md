# API credit implementation review

Independent architecture review selected separate binary-asset ownership and reuse of the existing comparison harness and online judge. Independent same-family review covered the integrated implementation at `8d13cc470`; merge reconciliation at `9c128d23b` found no integration blocker. Cross-family review was unavailable.

Review and real-boundary checks corrected corrupt PNG acceptance, mismatched audio/transcript identity, immutable-storage recovery, stale submissions, library-reference identity, alpha/orientation preservation and native narration acceptance without an image asset ID. Native simulator verification passed all 14 selected tests.

The broad regression run then exposed canonical-lock violations and an outdated expected migration head. The service now uses the existing canonical synchronous lock helper and revalidates ownership and graph bindings after refresh. Existing lock guards remain unchanged. The migration guard adds `0127 → 0126` and expects the sole head `0127`.

A further independent review found narration acceptance locking CreationThread before the owner graph, hidden by the static guard's treatment of conditional helper calls. The existing narration acceptance test was extended to record actual PostgreSQL lock statements against an active thread. It failed before correction and passed after owner/item locks moved ahead of thread locking and script revalidation. Independent final review confirmed the correction, inspected the red/green and consolidated XML receipts, and found no remaining blocking finding in the final code tree.

The existing 21 judge tests were relocated intact into the structural-evaluation suite. WebSockets is now a direct dependency, qualified locally against 16.0 and compatible with the installed Gemini SDK range. No live Realtime behavior is inferred from signatures or mocked contracts.

[VERIFY.md](VERIFY.md) records current checks and gaps. No live quality winner, verified billing balance, complete final-head broad-suite pass or inspected physical-phone export is claimed. Eligible real holdouts remain unavailable. Draft PR publication is authorized; deployment-sensitive merge and denied metadata writes remain held.

The final deadline-runtime merge at `1e82c2c8` was independently reviewed: upstream model client is preserved, runtime retains judge attribution, and budget/generated-media/native implementation is unchanged. Obsolete PR-only deadline test skips were removed while retaining all assertions. The consolidated run passed 1,660 tests and 24 subtests with zero failures/errors/skips.
