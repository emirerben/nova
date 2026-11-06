import XCTest
@testable import Kria

final class TeleprompterStopReasonTests: XCTestCase {
    func testOnlyARealLossOfCameraOrMicrophoneMapsToAStopReason() {
        let table: [(TakeStopSignal, TakeStopReason?)] = [
            (.audioInterruptionBegan, .audioInterruptionBegan),
            (.audioInterruptionEnded, nil),
            (.routeLost, .routeLost),
            (.mediaServicesReset, .mediaServicesReset),
            (.captureSessionInterrupted, .captureSessionInterrupted),
            (.captureRuntimeError, .runtimeError),
            (.sceneBackground, .backgrounded),
            (.sceneInactive, nil),
            (.viewDisappeared(reappeared: true), nil),
            (.viewDisappeared(reappeared: false), .viewRemoved),
        ]
        for (signal, expected) in table {
            XCTAssertEqual(TakeStopPolicy.reason(for: signal), expected, "\(signal)")
        }
    }

    func testReasonRoundTripsThroughTheManifestEncoding() throws {
        for reason in TakeStopReason.allCases {
            let data = try JSONEncoder().encode([reason])
            XCTAssertEqual(try JSONDecoder().decode([TakeStopReason].self, from: data), [reason])
        }
        XCTAssertEqual(TakeStopReason.audioInterruptionBegan.rawValue, "audio_interruption_began")
    }

    func testEveryReasonFitsTheServerLabelLimit() {
        for reason in TakeStopReason.allCases { XCTAssertLessThanOrEqual(reason.rawValue.count, 40) }
    }
}
