import AVFoundation
import UIKit
import XCTest
@testable import Kria

/// A take ends only when the creator ends it or iOS takes the camera or microphone away.
/// Failure modes written before the code (KRI-1149):
/// 1. audio interruption `.ended`, a route change that is not a lost device, and scene `.inactive` leave the take running;
/// 2. each real loss stops it with its own reason and keeps the partial take;
/// 3. the first reason wins, so a later teardown cannot relabel a take;
/// 4. a movie output that finishes without a stop request is `systemFinished`, never a silent success;
/// 5. every finished take reports its reason and phase history, not only silent ones.
@MainActor final class TeleprompterCameraStopTests: XCTestCase {
    private func recordingCamera() -> TeleprompterCamera {
        let camera = TeleprompterCamera()
        camera.seedRecordingForTesting()
        return camera
    }

    private func interruption(_ type: AVAudioSession.InterruptionType) {
        NotificationCenter.default.post(name: AVAudioSession.interruptionNotification, object: nil,
                                        userInfo: [AVAudioSessionInterruptionTypeKey: type.rawValue])
    }

    private func routeChange(_ reason: AVAudioSession.RouteChangeReason) {
        NotificationCenter.default.post(name: AVAudioSession.routeChangeNotification, object: nil,
                                        userInfo: [AVAudioSessionRouteChangeReasonKey: reason.rawValue])
    }

    private func settle(_ camera: TeleprompterCamera, until condition: () -> Bool) async {
        for _ in 0..<40 {
            if camera.phase != .recording || condition() { return }
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    func testAudioInterruptionEndedLeavesTheTakeRunning() async {
        let camera = recordingCamera()
        interruption(.ended)
        await settle(camera) { camera.phaseLabelsForTesting.contains("audio_int_ended_ignored") }
        XCTAssertEqual(camera.phase, .recording)
        XCTAssertNil(camera.stopReason)
        XCTAssertFalse(camera.interrupted)
    }

    func testRouteChangeThatIsNotALostDeviceLeavesTheTakeRunning() async {
        let camera = recordingCamera()
        routeChange(.newDeviceAvailable)
        await settle(camera) { camera.phaseLabelsForTesting.contains("route_change") }
        XCTAssertEqual(camera.phase, .recording)
        XCTAssertNil(camera.stopReason)
    }

    func testSceneInactiveLeavesTheTakeRunning() async {
        let camera = recordingCamera()
        camera.sceneBecameInactive()
        await settle(camera) { false }
        XCTAssertEqual(camera.phase, .recording)
        XCTAssertNil(camera.stopReason)
    }

    func testAudioInterruptionBeganStopsAndKeepsThePartialTake() async {
        let camera = recordingCamera()
        interruption(.began)
        await settle(camera) { false }
        XCTAssertEqual(camera.phase, .finalizing)
        XCTAssertEqual(camera.stopReason, .audioInterruptionBegan)
        XCTAssertTrue(camera.interrupted)
        XCTAssertTrue(camera.error?.contains("partial take will be kept") == true)
    }

    func testBackgroundingStopsWithItsOwnReason() async {
        let camera = recordingCamera()
        NotificationCenter.default.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .backgrounded)
    }

    func testCaptureSessionInterruptionStopsWithItsOwnReason() async {
        let camera = recordingCamera()
        NotificationCenter.default.post(name: AVCaptureSession.wasInterruptedNotification, object: camera.session)
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .captureSessionInterrupted)
    }

    func testRuntimeErrorStopsWithItsOwnReason() async {
        let camera = recordingCamera()
        NotificationCenter.default.post(name: AVCaptureSession.runtimeErrorNotification, object: camera.session,
                                        userInfo: [AVCaptureSessionErrorKey: NSError(domain: AVFoundationErrorDomain, code: -11800)])
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .runtimeError)
    }

    func testLostMicrophoneRouteStopsWithItsOwnReason() async {
        let camera = recordingCamera()
        routeChange(.oldDeviceUnavailable)
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .routeLost)
    }

    func testMediaServicesResetStopsWithItsOwnReason() async {
        let camera = recordingCamera()
        NotificationCenter.default.post(name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .mediaServicesReset)
    }

    func testInterruptionBeforeAnyTakeDoesNotClaimARecordingStopped() async {
        let camera = TeleprompterCamera()
        camera.seedPreviewReadyForTesting()
        interruption(.began)
        for _ in 0..<40 where camera.phase == .ready { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(camera.phase, .failed)
        let message = camera.error ?? ""
        XCTAssertFalse(message.isEmpty)
        XCTAssertFalse(message.contains("Recording stopped"), "nothing was recording: \(message)")
    }

    func testCreatorStopIsNotAnInterruption() {
        let camera = recordingCamera()
        camera.stopRecording(reason: .user)
        XCTAssertEqual(camera.phase, .finalizing)
        XCTAssertEqual(camera.stopReason, .user)
        XCTAssertFalse(camera.interrupted)
    }

    func testFirstStopReasonWins() async {
        let camera = recordingCamera()
        interruption(.began)
        await settle(camera) { false }
        camera.stopSession()
        NotificationCenter.default.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
        await settle(camera) { false }
        XCTAssertEqual(camera.stopReason, .audioInterruptionBegan)
    }

    func testMovieOutputFinishingOnItsOwnIsSystemFinished() async {
        let camera = recordingCamera()
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        camera.fileOutput(AVCaptureMovieFileOutput(), didFinishRecordingTo: url, from: [],
                          error: NSError(domain: AVFoundationErrorDomain, code: -11810))
        for _ in 0..<100 where camera.stopReason == nil { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(camera.stopReason, .systemFinished)
        XCTAssertTrue(camera.interrupted)
    }

    func testEveryFinishedTakeReportsItsReasonAndPhaseHistory() async {
        let camera = recordingCamera()
        var reports: [RecorderDiagnosticReport] = []
        camera.reportDiagnostic = { reports.append($0) }
        camera.stopRecording(reason: .user)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        camera.fileOutput(AVCaptureMovieFileOutput(), didFinishRecordingTo: url, from: [], error: nil)
        for _ in 0..<200 where !reports.contains(where: { $0.kind == .takeFinished }) { try? await Task.sleep(for: .milliseconds(10)) }

        let finished = reports.filter { $0.kind == .takeFinished }
        XCTAssertEqual(finished.count, 1)
        XCTAssertEqual(finished.first?.stopReason, "user")
        XCTAssertNotNil(finished.first?.takeDurationS)
        XCTAssertTrue(finished.first?.phaseHistory.contains { $0.hasPrefix("stop_user+") } == true, "\(finished.first?.phaseHistory ?? [])")
    }
}
