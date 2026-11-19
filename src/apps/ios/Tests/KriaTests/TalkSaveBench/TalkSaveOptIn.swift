import XCTest

/// The Save benchmark exports 30 s of video on the device; it runs only when TALKSAVE_RUN=1 is in the test runner environment
/// (`TEST_RUNNER_TALKSAVE_RUN=1 xcodebuild test ...`), so CI and simulators skip it.
enum TalkSaveOptIn {
    static func require() throws {
        guard ProcessInfo.processInfo.environment["TALKSAVE_RUN"] == "1" else { throw XCTSkip("opt-in device benchmark: set TALKSAVE_RUN=1") }
    }
}
