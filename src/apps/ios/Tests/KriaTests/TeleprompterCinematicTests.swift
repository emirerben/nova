import AVFoundation
import CryptoKit
import XCTest
@testable import Kria

/// KRI-1160 failure scenarios, written before implementation:
/// 1. A device that cannot record Cinematic with the front camera (older iOS, older iPhone, the simulator) offers a
///    Cinematic switch that does nothing, so the creator believes a standard take is Cinematic.
/// 2. A standard take (switch off, or a take that never became Cinematic) is rewritten by the Cinematic step and
///    changes bytes, length or tracks. The standard path must be untouched.
/// 3. The choice is lost between camera sessions, or is "on" on an iPhone that cannot honour it.
/// 4. A Cinematic movie reaches playback, upload or the iPhone export with its depth tracks and no blur, because only
///    Apple's own apps render the Cinematic look from those tracks. The look must be baked into an ordinary movie.
/// 5. Baking fails (GPU unavailable in the background, odd metadata) and the take is lost or left half-written.
///    The original file stays until a verified replacement exists; otherwise an ordinary standard copy is kept.
/// 6. A take recorded with Cinematic on silently comes out standard (the camera dropped the mode). The creator is told.
/// 7. An old take manifest without the new field no longer decodes.
/// Scenarios 4–6 need a physical iPhone: `TeleprompterHardwareUITests.testPhysicalCinematicTake`.
@MainActor final class TeleprompterCinematicTests: XCTestCase {
    private var root: URL!
    private var defaults: UserDefaults!
    private var suiteName: String!

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("cinematic-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        suiteName = "cinematic-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
        defaults.removePersistentDomain(forName: suiteName)
    }

    func testSimulatorCannotRecordCinematicAndSaysWhy() {
        #if targetEnvironment(simulator)
        let availability = TeleprompterCinematic.availability()
        XCTAssertNotEqual(availability, .available, "The simulator has no camera that records Cinematic.")
        XCTAssertTrue(availability.creatorMessage?.localizedCaseInsensitiveContains("standard") == true,
                      "An unavailable switch says the take stays standard.")
        #else
        throw XCTSkip("Simulator-only check.")
        #endif
    }

    func testCinematicChoiceIsRefusedWhereItCannotWork() {
        let flow = makeFlow(availability: .unsupportedCamera)
        flow.setCinematic(true)
        XCTAssertFalse(flow.cinematicEnabled)
        XCTAssertFalse(flow.camera.cinematicRequested)
        XCTAssertFalse(defaults.bool(forKey: TeleprompterCinematic.preferenceKey))
    }

    func testCinematicChoicePersistsAcrossCameraSessions() {
        let first = makeFlow(availability: .available)
        XCTAssertFalse(first.cinematicEnabled, "Standard recording stays the default.")
        first.setCinematic(true)
        XCTAssertTrue(first.camera.cinematicRequested)

        let second = makeFlow(availability: .available)
        XCTAssertTrue(second.cinematicEnabled)
        XCTAssertTrue(second.camera.cinematicRequested)

        // A saved "on" from an iPhone that could do it never turns on where it cannot.
        let restoredOnOlderPhone = makeFlow(availability: .needsNewerIOS)
        XCTAssertFalse(restoredOnOlderPhone.cinematicEnabled)
        XCTAssertFalse(restoredOnOlderPhone.camera.cinematicRequested)
    }

    func testStandardTakeIsLeftByteIdentical() async throws {
        let url = root.appendingPathComponent("standard.mov")
        try await Self.writeStandardMovie(to: url)
        let before = try Data(contentsOf: url)

        let outcome = await CinematicTakeFinisher.finish(movieAt: url)

        XCTAssertNil(outcome, "A movie that is not Cinematic is not touched.")
        XCTAssertEqual(SHA256.hash(data: try Data(contentsOf: url)), SHA256.hash(data: before))
    }

    func testManifestWithoutCinematicFieldStillDecodes() throws {
        let take = TeleprompterTakeManifest(script: TeleprompterScriptDraft())
        var json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(take)) as! [String: Any]
        json.removeValue(forKey: "cinematic")
        let decoded = try JSONDecoder().decode(TeleprompterTakeManifest.self, from: JSONSerialization.data(withJSONObject: json))
        XCTAssertNil(decoded.cinematic)
    }

    private func makeFlow(availability: TeleprompterCinematic.Availability) -> TeleprompterRecordingFlow {
        TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, cinematicAvailability: availability,
                                  defaults: defaults)
    }

    /// A one-second H.264 movie with silent AAC audio: the shape of an ordinary standard take.
    private static func writeStandardMovie(to url: URL) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let video = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64,
        ])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: video, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64,
        ])
        let audio = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 44_100, AVNumberOfChannelsKey: 1,
        ])
        video.expectsMediaDataInRealTime = false
        audio.expectsMediaDataInRealTime = false
        writer.add(video)
        writer.add(audio)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for frame in 0..<30 {
            while !video.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(5)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
            adaptor.append(buffer!, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: 30))
        }
        video.markAsFinished()
        let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!
        let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 44_100)!
        pcm.frameLength = 44_100
        var description: CMAudioFormatDescription?
        CMAudioFormatDescriptionCreate(allocator: nil, asbd: format.streamDescription, layoutSize: 0, layout: nil,
                                       magicCookieSize: 0, magicCookie: nil, extensions: nil, formatDescriptionOut: &description)
        var sample: CMSampleBuffer?
        CMAudioSampleBufferCreateWithPacketDescriptions(allocator: nil, dataBuffer: nil, dataReady: false, makeDataReadyCallback: nil,
            refcon: nil, formatDescription: description!, sampleCount: 44_100, presentationTimeStamp: .zero,
            packetDescriptions: nil, sampleBufferOut: &sample)
        CMSampleBufferSetDataBufferFromAudioBufferList(sample!, blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
                                                       flags: 0, bufferList: pcm.audioBufferList)
        while !audio.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(5)) }
        audio.append(sample!)
        audio.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed, writer.error?.localizedDescription ?? "")
    }
}
