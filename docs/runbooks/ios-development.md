# Kria iOS development

The native app lives in `src/apps/ios`. `project.yml` is the source of truth;
`Kria.xcodeproj` is generated and must not be edited by hand.

## Prerequisites

- Xcode with an iOS 18 simulator runtime
- XcodeGen 2.38 or newer (`brew install xcodegen`)

No Apple team or production bundle identifier is required for simulator builds.
Development defaults come from `Config/Development.xcconfig`; production signing
values belong in an untracked local override or CI secrets.

## Commands

```bash
make ios-generate  # regenerate Kria.xcodeproj
make ios-build     # unsigned simulator build
make ios-test      # generate, build, and run unit/UI tests
make ios-verify    # same gate used by CI
```

Set `KRIA_SKIP_SIMULATOR_TESTS=1` only when validating compilation on a host
without an installed iPhone simulator. CI must run the complete gate.

## Architecture boundaries

- Creation threads, runtime-v2 drafts, approvals, editor commits, and jobs remain
  server-authoritative. SwiftData is a recoverable device cache, not a second
  product authority.
- The checked mobile API contract is generated from the API's Pydantic models.
  Do not hand-add a field to Swift without updating the server contract and its
  drift fixture.
- Native preview and export consume the versioned edit recipe. They must never
  decode a job's renderer-internal `assembly_plan`.
- Full-resolution media remains on device during analysis. Uploading originals
  for cloud rendering requires explicit user consent and a temporary-media
  retention receipt.
- Access and refresh tokens belong in Keychain. The native app never contains
  `INTERNAL_API_KEY` and never sends `X-User-Id`.

## Performance gate

Simulator results prove correctness only. Before enabling a local-render
capability, run the instrumented fixture on an iPhone 13 and a current iPhone,
then record preview FPS, seek-to-visible-frame p95, export duration, peak memory,
thermal changes, and preview/export parity. Local export stays behind the server
capability flag until those measurements pass the rollout thresholds.
