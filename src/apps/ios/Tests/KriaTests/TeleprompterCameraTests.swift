import XCTest
@testable import Kria

/// Failure scenarios specified before implementation:
/// 1. Simulator/no front camera remains an actionable failure, never a fake ready preview.
/// 2. A permission change is checked again by every `prepare()` call.
/// 3. Background, audio interruption, route loss, and capture errors stop/finalize a partial take.
/// 4. Finalization requires both playable video and non-empty audio; delegate wording cannot create success.
/// These hardware and privacy paths need physical-iPhone proof; the pure state surface is covered here.
@MainActor final class TeleprompterCameraTests: XCTestCase {
    func testInitialStateDoesNotClaimCaptureReadiness() {
        let camera = TeleprompterCamera()
        XCTAssertEqual(camera.phase, .idle)
        XCTAssertNil(camera.error)
        XCTAssertNil(camera.recordingStartedAt)
        XCTAssertNil(camera.finalizedURL)
        XCTAssertFalse(camera.interrupted)
    }

    func testStopRecordingBeforeAStartedTakeIsAStateNoOp() {
        let camera = TeleprompterCamera()
        camera.stopRecording(reason: .backgrounded)
        XCTAssertEqual(camera.phase, .idle)
        XCTAssertFalse(camera.interrupted, "a stop request cannot invent an interrupted take")
    }
}
