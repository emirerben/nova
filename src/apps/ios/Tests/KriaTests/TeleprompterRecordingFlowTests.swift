import XCTest
@testable import Kria

@MainActor final class TeleprompterRecordingFlowTests: XCTestCase {
    // @Published emits the incoming phase before camera.phase has changed.
    // Capture acknowledgment must start scrolling from that incoming value.
    func testIncomingRecordingAcknowledgmentStartsScriptBeforeStoredPhaseChanges() {
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID())
        flow.script.updateText("Read this after recording starts.")
        flow.stage = .starting
        XCTAssertEqual(flow.camera.phase, .idle)
        flow.cameraChanged(.recording)
        XCTAssertEqual(flow.stage, .recording)
        XCTAssertTrue(flow.script.isPlaying)
    }

    func testIncomingFinalizationPausesTextAndFailureCancelsCountdown() {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root)
        flow.script.updateText("A recording can finish independently of this script.")
        flow.script.resume()
        flow.stage = .recording
        flow.cameraChanged(.finalizing)
        XCTAssertEqual(flow.stage, .finalizing)
        XCTAssertFalse(flow.script.isPlaying)

        flow.stage = .countdown
        flow.cameraChanged(.failed)
        XCTAssertEqual(flow.stage, .camera)
    }
}
