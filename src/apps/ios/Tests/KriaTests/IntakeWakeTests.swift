import XCTest
@testable import Kria

@MainActor final class IntakeWakeTests: XCTestCase {
    private func elapsed(_ block: () async -> Void) async -> Duration {
        let start = ContinuousClock.now
        await block()
        return start.duration(to: .now)
    }

    func testASignalSentBeforeWaitingMakesTheNextWaitReturnAtOnce() async {
        let wake = IntakeWake()
        wake.signal()
        let took = await elapsed { await wake.wait(timeout: .seconds(5)) }
        XCTAssertLessThan(took, .milliseconds(200))
        let second = await elapsed { await wake.wait(timeout: .milliseconds(80)) }
        XCTAssertGreaterThanOrEqual(second, .milliseconds(70), "one signal wakes one wait, it is not remembered twice")
    }

    func testASignalWhileWaitingWakesBeforeTheTimeout() async {
        let wake = IntakeWake()
        Task { try? await Task.sleep(for: .milliseconds(30)); wake.signal() }
        let took = await elapsed { await wake.wait(timeout: .seconds(5)) }
        XCTAssertLessThan(took, .seconds(1))
    }

    func testWithNoSignalTheTimeoutIsTheSafetyNet() async {
        let wake = IntakeWake()
        let took = await elapsed { await wake.wait(timeout: .milliseconds(60)) }
        XCTAssertGreaterThanOrEqual(took, .milliseconds(50))
        XCTAssertLessThan(took, .seconds(2))
    }

    func testCancellingTheWaitingTaskEndsTheWait() async {
        let wake = IntakeWake()
        let waiting = Task { @MainActor in await wake.wait(timeout: .seconds(30)) }
        try? await Task.sleep(for: .milliseconds(30))
        waiting.cancel()
        let took = await elapsed { await waiting.value }
        XCTAssertLessThan(took, .seconds(2))
    }
}
