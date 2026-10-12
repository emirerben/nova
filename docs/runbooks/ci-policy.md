# CI policy

Nova CI covers the API and native phone contracts. Web application tests and
web lint are retired because the web app is no longer developed. Web assets
bundled into the iOS app still select native coverage.

Pull requests run API checks on Linux. Journey selections use the manifest's
`needs_native` flag: Python only journeys run on Linux, while any mixed
selection containing a native journey runs on macOS and keeps its Xcode/iPhone
validation. Unknown Kria journey paths fail closed to the shared API plus
native contract selection.

Pushes to `main` retain full regression coverage, including native checks.

The retired web tree still supplies the shared motion runtime contract. Changes
to that runtime, its generated catalogs, or the targeted Jest/Deno harness
inputs (`package*.json`, Jest config/setup, or `tsconfig.json`) run one Linux
shared-runtime job; unrelated web UI changes do not.

Known limits: mixed journeys still require the native runner, and unmapped
Kria paths intentionally keep the conservative native fallback until their
contract boundary is documented in the selector or journey manifest.
