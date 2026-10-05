import UIKit
import XCTest
@testable import Kria

/// KRI-432: the start-point audition. A fake engine records what would play, so these pin when it starts, from
/// where, how often it restarts, and when it stops and hands the video back.
@MainActor final class NativeSongAuditionTests: XCTestCase {
    private final class FakeEngine: SongAuditionEngine {
        var onFinished: (() -> Void)?
        var played: [SongAuditionRequest] = []
        var stops = 0
        func play(_ request: SongAuditionRequest) { played.append(request) }
        func stop() { stops += 1 }
        func finishWindow() { onFinished?() }
    }

    private final class FakeVideo {
        var playing: Bool
        var pauses = 0, resumes = 0, sessionActivations = 0
        init(playing: Bool) { self.playing = playing }
        var host: NativeSongAuditionController.Host {
            .init(pauseVideo: { [self] in let was = playing; if was { playing = false; pauses += 1 }; return was },
                  resumeVideo: { [self] in playing = true; resumes += 1 },
                  activateAudioSession: { [self] in sessionActivations += 1 })
        }
    }

    private let url = URL(fileURLWithPath: "/song.m4a")
    private func request(_ start: Double, length: Double = 14.5, volume: Float = 0.8) -> SongAuditionRequest {
        SongAuditionRequest(url: url, start: start, length: length, volume: volume)
    }
    private func make(videoPlaying: Bool = true) -> (NativeSongAuditionController, FakeEngine, FakeVideo) {
        let engine = FakeEngine(), video = FakeVideo(playing: videoPlaying)
        return (NativeSongAuditionController(engine: engine, host: video.host, debounce: 0.03), engine, video)
    }
    private func settle() async throws { try await Task.sleep(for: .milliseconds(120)) }

    func testPlaysFromTheNewStartForTheWindowLengthAtTheSongVolume() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        XCTAssertTrue(controller.isActive)
        XCTAssertEqual(video.pauses, 1, "the video pauses while the song plays")
        XCTAssertEqual(video.sessionActivations, 1)
        controller.update(request(42.5))
        try await settle()
        XCTAssertEqual(engine.played, [request(42.5)])
        XCTAssertEqual(engine.played[0].start + engine.played[0].length, 57, accuracy: 0.0001, "ends at start + video length")
        XCTAssertEqual(engine.played[0].volume, 0.8)
    }

    func testSeeksAreThrottledWhileDragging() async throws {
        let (controller, engine, _) = make()
        controller.begin()
        for start in stride(from: 10.0, through: 14.0, by: 1) { controller.update(request(start)) }
        try await settle()
        XCTAssertEqual(engine.played, [request(14)], "a burst of drag samples restarts once, from the last")
        controller.update(request(20))
        controller.update(request(21))
        try await settle()
        XCTAssertEqual(engine.played.map(\.start), [14, 21])
    }

    func testReleasePlaysFromTheFinalStartThenStopsAndRestoresTheVideo() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        controller.update(request(30))
        controller.end()
        XCTAssertEqual(engine.played, [request(30)], "release restarts at once, without waiting for the debounce")
        XCTAssertTrue(controller.isActive, "the window plays out after the finger lifts")
        XCTAssertFalse(video.playing)
        engine.finishWindow()
        XCTAssertFalse(controller.isActive)
        XCTAssertTrue(video.playing, "the video that was playing resumes")
        XCTAssertEqual(video.resumes, 1)
    }

    func testReleaseDoesNotRestartAnAuditionAlreadyPlayingFromThatStart() async throws {
        let (controller, engine, _) = make()
        controller.begin()
        controller.update(request(30))
        try await settle()
        controller.end()
        XCTAssertEqual(engine.played.count, 1)
    }

    func testWindowEndingMidDragWaitsForTheDragToFinish() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        controller.update(request(30))
        try await settle()
        engine.finishWindow()
        XCTAssertTrue(controller.isActive, "still dragging: the session stays open")
        XCTAssertEqual(video.resumes, 0)
        controller.end()
        XCTAssertEqual(engine.played.count, 2, "released: the chosen start plays once more")
        engine.finishWindow()
        XCTAssertFalse(controller.isActive)
        XCTAssertEqual(video.resumes, 1)
    }

    func testCancelStopsAndRestoresOnlyAVideoThatWasPlaying() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        controller.update(request(30))
        try await settle()
        controller.cancel()
        XCTAssertEqual(engine.stops, 1)
        XCTAssertFalse(controller.isActive)
        XCTAssertEqual(video.resumes, 1)

        let (idle, idleEngine, pausedVideo) = make(videoPlaying: false)
        idle.begin()
        idle.update(request(5))
        idle.end()
        idleEngine.finishWindow()
        XCTAssertEqual(pausedVideo.pauses, 0)
        XCTAssertEqual(pausedVideo.resumes, 0, "a video that was paused stays paused")
    }

    func testCancelWithoutRestoreLeavesTheVideoToItsNewOwner() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        controller.update(request(30))
        controller.cancel(restoreVideo: false)
        XCTAssertEqual(engine.stops, 1)
        XCTAssertEqual(video.resumes, 0)
        try await settle()
        XCTAssertTrue(engine.played.isEmpty, "a cancelled drag's pending seek never plays")
    }

    func testBackgroundingTheAppStopsTheAuditionWithoutResumingTheVideo() async throws {
        let (controller, engine, video) = make()
        controller.begin()
        controller.update(request(30))
        try await settle()
        NotificationCenter.default.post(name: UIApplication.willResignActiveNotification, object: nil)
        try await settle()
        XCTAssertFalse(controller.isActive)
        XCTAssertEqual(engine.stops, 1)
        XCTAssertEqual(video.resumes, 0)
    }
}
