import XCTest
@testable import Kria

@MainActor
final class NativeEditorPanelLifecycleTests: XCTestCase {
    func testOutgoingDraftFlushesOnceBeforeNextPanelRegisters() {
        let lifecycle = NativeEditorPanelLifecycle()
        var draft = "72"
        var committed = ""
        var calls = 0
        lifecycle.register(owner: UUID()) { committed = draft; calls += 1 }
        draft = "108"
        lifecycle.prepareToClose()
        XCTAssertEqual(committed, "108")
        lifecycle.prepareToClose()
        XCTAssertEqual(calls, 1)
    }

    func testOutgoingDisappearCannotClearIncomingCleanup() {
        let lifecycle = NativeEditorPanelLifecycle()
        let outgoing = UUID(), incoming = UUID()
        var events: [String] = []
        lifecycle.register(owner: outgoing) { events.append("outgoing") }
        lifecycle.prepareToClose()
        lifecycle.register(owner: incoming) { events.append("incoming") }
        lifecycle.unregister(owner: outgoing)
        lifecycle.prepareToClose()
        XCTAssertEqual(events, ["outgoing", "incoming"])
    }

    func testCleanupCanRegisterNextOwnerWithoutLosingItsCallback() {
        let lifecycle = NativeEditorPanelLifecycle()
        var calls = 0
        lifecycle.register(owner: UUID()) {
            lifecycle.register(owner: UUID()) { calls += 1 }
        }
        lifecycle.prepareToClose()
        lifecycle.prepareToClose()
        XCTAssertEqual(calls, 1)
    }
}
