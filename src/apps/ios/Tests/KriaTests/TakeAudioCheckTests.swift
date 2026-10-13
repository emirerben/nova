import AVFoundation
import XCTest
@testable import Kria

/// KRI-653: a 192 s in-app talk-to-camera take had a digitally silent audio track and the recorder
/// called it valid because the track existed. Failure modes pinned here, written before the code:
/// 1. digital silence is `.silent` (blocks Use), including a take whose only sound is a faint click;
/// 2. a soft but real voice is NOT `.silent` (a real creator must never be blocked);
/// 3. barely-audible audio on a real-length take is `.quiet` (a warning, never a block);
/// 4. a clip too short to judge, or a file that cannot be read, never blocks;
/// 5. the live meter never claims silence from "no reading", and clears only after sound returns;
/// 6. nothing deactivates the shared audio session while the capture graph owns it.
final class TakeAudioCheckTests: XCTestCase {
    private var created: [URL] = []
    override func tearDown() { created.forEach { try? FileManager.default.removeItem(at: $0) }; created = [] }

    private func writeWAV(seconds: Double, amplitude: Float) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).wav")
        created.append(url)
        let format = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false))
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        let frames = AVAudioFrameCount(seconds * 16_000)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames))
        buffer.frameLength = frames
        let samples = try XCTUnwrap(buffer.floatChannelData?[0])
        for index in 0..<Int(frames) { samples[index] = amplitude * sin(2 * .pi * 220 * Float(index) / 16_000) }
        try file.write(from: buffer)
        return url
    }

    // MARK: File measurement

    func testDigitalSilenceIsSilent() async throws {
        let check = await TakeAudioMeasurement.measure(url: try writeWAV(seconds: 8, amplitude: 0))
        XCTAssertEqual(check.verdict, .silent)
        XCTAssertEqual(check.activeFraction, 0)
    }

    func testNormalVoiceLevelIsOK() async throws {
        let check = await TakeAudioMeasurement.measure(url: try writeWAV(seconds: 6, amplitude: 0.1))
        XCTAssertEqual(check.verdict, .ok)
        XCTAssertGreaterThan(check.activeFraction, 0.9)
    }

    func testSoftVoiceIsNotSilent() async throws {
        // ~-40 dBFS peak: a hushed talker in a quiet room. Blocking this would block a real creator.
        let check = await TakeAudioMeasurement.measure(url: try writeWAV(seconds: 6, amplitude: 0.01))
        XCTAssertNotEqual(check.verdict, .silent)
    }

    func testBarelyAudibleRealLengthTakeIsQuietNotSilent() async throws {
        // ~-54 dBFS peak: present, but no window reaches speech level.
        let check = await TakeAudioMeasurement.measure(url: try writeWAV(seconds: 8, amplitude: 0.002))
        XCTAssertEqual(check.verdict, .quiet)
    }

    func testShortBarelyAudibleClipIsNotJudgedQuiet() async throws {
        let check = await TakeAudioMeasurement.measure(url: try writeWAV(seconds: 1.5, amplitude: 0.002))
        XCTAssertEqual(check.verdict, .ok, "a clip under minimumJudgedDuration never earns a warning")
    }

    func testUnreadableFileFailsOpen() async {
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent("nope-\(UUID().uuidString).mov")
        let check = await TakeAudioMeasurement.measure(url: missing)
        XCTAssertEqual(check.verdict, .unknown)
    }

    // MARK: Accumulator windows

    func testAccumulatorSplitsChunksAcrossWindowBoundaries() {
        // One 1,600-sample window delivered as 700 + 900 samples must be a single loud window.
        var accumulator = AudioLevelAccumulator()
        let loud = [Float](repeating: 0.2, count: 700), rest = [Float](repeating: 0.2, count: 900)
        loud.withUnsafeBufferPointer { accumulator.ingest($0) }
        rest.withUnsafeBufferPointer { accumulator.ingest($0) }
        XCTAssertEqual(accumulator.windowCount, 1)
        XCTAssertEqual(accumulator.activeWindowCount, 1)
    }

    func testAccumulatorWithLessThanOneWindowHasNoVerdict() {
        var accumulator = AudioLevelAccumulator()
        [Float](repeating: 0.2, count: 500).withUnsafeBufferPointer { accumulator.ingest($0) }
        XCTAssertEqual(accumulator.result(duration: 10).verdict, .unknown)
    }

    // MARK: Live meter

    func testNoReadingNeverClaimsSilence() {
        var monitor = MicSilenceMonitor()
        for tick in 0..<40 { XCTAssertFalse(monitor.ingest(level: nil, at: Double(tick) * 0.25)) }
    }

    func testDeadMicWarnsOnlyAfterThreeSeconds() {
        var monitor = MicSilenceMonitor()
        XCTAssertFalse(monitor.ingest(level: -160, at: 0))
        XCTAssertFalse(monitor.ingest(level: -160, at: 2.9))
        XCTAssertTrue(monitor.ingest(level: -160, at: 3.0))
    }

    func testQuietRoomIsNotSilence() {
        var monitor = MicSilenceMonitor()
        for tick in 0..<40 { XCTAssertFalse(monitor.ingest(level: -55, at: Double(tick) * 0.25)) }
    }

    func testWarningClearsOnlyAfterSoundReturnsForHalfASecond() {
        var monitor = MicSilenceMonitor()
        _ = monitor.ingest(level: -160, at: 0)
        XCTAssertTrue(monitor.ingest(level: -160, at: 3.5))
        XCTAssertTrue(monitor.ingest(level: -30, at: 4.0), "a single loud reading is not recovery")
        XCTAssertFalse(monitor.ingest(level: -30, at: 4.6))
    }

    // MARK: Audio session ownership

    func testArbiterNeverDeactivatesWhileCaptureOwnsTheSession() {
        let counter = Counter()
        let arbiter = AudioSessionArbiter(deactivateSession: { counter.increment() })
        arbiter.claimCapture("camera")
        XCTAssertFalse(arbiter.deactivate(requester: "voice-recorder.pause"))
        XCTAssertFalse(arbiter.deactivate(requester: "song-order-preview"))
        XCTAssertEqual(counter.value, 0)
        arbiter.releaseCapture("camera")
        XCTAssertTrue(arbiter.deactivate(requester: "voice-recorder.pause"))
        XCTAssertEqual(counter.value, 1)
    }

    func testArbiterStaysClaimedUntilEveryOwnerReleases() {
        let counter = Counter()
        let arbiter = AudioSessionArbiter(deactivateSession: { counter.increment() })
        arbiter.claimCapture("a"); arbiter.claimCapture("b")
        arbiter.releaseCapture("a")
        XCTAssertFalse(arbiter.deactivate(requester: "x"))
        arbiter.releaseCapture("b")
        XCTAssertTrue(arbiter.deactivate(requester: "x"))
    }
}

private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func increment() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}
