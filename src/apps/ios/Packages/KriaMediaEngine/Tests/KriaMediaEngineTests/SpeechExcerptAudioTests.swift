import XCTest
@testable import KriaMediaEngine

/// KRI-282: speech excerpts played from one clip over other clips' visuals.
/// Pure geometry first, then the real preview mix.
final class SpeechExcerptAudioTests: XCTestCase {
    private func excerpt(_ id: String, _ asset: String = "take", source: Double, duration: Double, at start: Double) -> TimelineClip {
        TimelineClip(id: id, sourceAssetID: asset, sourceStart: source, sourceDuration: duration, timelineStart: start)
    }

    func testBackToBackExcerptsOfOneTakeCrossfadeOnAnAudioTrack() {
        let track = TimelineTrack(id: "clip-audio", kind: .audio, clips: [
            excerpt("a", source: 1, duration: 1, at: 0), excerpt("b", source: 4, duration: 1, at: 1)])
        let handles = AudioCutHandles.plan([track])
        XCTAssertEqual(handles["a"], AudioCutHandles(lead: 0, tail: audioCutCrossfadeHandle))
        XCTAssertEqual(handles["b"], AudioCutHandles(lead: audioCutCrossfadeHandle, tail: 0))
    }

    func testExcerptsFromDifferentTakesOrWithGapsKeepTheirDeclick() {
        func plan(_ clips: [TimelineClip]) -> [String: AudioCutHandles] { AudioCutHandles.plan([TimelineTrack(id: "x", kind: .audio, clips: clips)]) }
        XCTAssertTrue(plan([excerpt("a", source: 1, duration: 1, at: 0), excerpt("b", "other", source: 4, duration: 1, at: 1)]).isEmpty)
        XCTAssertTrue(plan([excerpt("a", source: 1, duration: 1, at: 0), excerpt("b", source: 4, duration: 1, at: 2)]).isEmpty)
        XCTAssertTrue(plan([excerpt("a", source: 4, duration: 1, at: 0), excerpt("b", source: 1, duration: 1, at: 1)]).isEmpty)
    }

    func testSpeechDuckFallsBeforeTheFirstSyllableAndRecoversAfter() throws {
        let duck = try XCTUnwrap(AudioDuckEnvelope.speech(windows: [(2, 3)], level: 0.3, attack: 0.15, release: 0.35))
        XCTAssertEqual(duck.gain(at: 1), 1, accuracy: 1e-9)
        XCTAssertEqual(duck.gain(at: 2), 0.3, accuracy: 1e-9, "fully ducked when speech starts")
        XCTAssertEqual(duck.gain(at: 2.5), 0.3, accuracy: 1e-9)
        XCTAssertEqual(duck.gain(at: 3), 0.3, accuracy: 1e-9)
        XCTAssertEqual(duck.gain(at: 3.35), 1, accuracy: 1e-9)
        XCTAssertGreaterThan(duck.gain(at: 1.9), 0.3, "still ramping down")
        XCTAssertNil(AudioDuckEnvelope.speech(windows: []))
    }

    func testCloseExcerptsShareOneDuckInsteadOfPumping() throws {
        let duck = try XCTUnwrap(AudioDuckEnvelope.speech(windows: [(2, 3), (3.2, 4)], level: 0.3, attack: 0.15, release: 0.35))
        XCTAssertEqual(duck.gain(at: 3.1), 0.3, accuracy: 1e-9)
        XCTAssertEqual(duck.gain(at: 4.35), 1, accuracy: 1e-9)
    }

    func testMusicDuckFlagIsOptInAndAbsentFromLegacyPayloads() throws {
        let legacy = AudioMixRecipe(musicAssetID: "m", musicVolume: 0.5)
        XCTAssertFalse(legacy.duckMusicDuringSpeech)
        let encoded = try JSONEncoder().encode(legacy)
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("duckMusicDuringSpeech"))
        XCTAssertEqual(try JSONDecoder().decode(AudioMixRecipe.self, from: encoded), legacy)
        var ducked = legacy; ducked.duckMusicDuringSpeech = true
        XCTAssertEqual(try JSONDecoder().decode(AudioMixRecipe.self, from: JSONEncoder().encode(ducked)), ducked)
    }
}

#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class SpeechExcerptMusicDuckTests: XCTestCase {
    @MainActor func testMusicBedDucksUnderTheExcerptOnlyWhenOptedIn() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let music = try tone(directory.appendingPathComponent("music.caf"), seconds: 5)
        let voice = try tone(directory.appendingPathComponent("voice.caf"), seconds: 5)
        let urls = ["photo": photo, "music": music, "voice": voice]
        let assets = try urls.sorted { $0.key < $1.key }.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) }
        func recipe(duck: Bool) -> EditRecipe {
            EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 4)]),
                         TimelineTrack(id: "clip-audio", kind: .audio, clips: [TimelineClip(id: "x", sourceAssetID: "voice", sourceStart: 1, sourceDuration: 1, timelineStart: 2)])],
                audio: AudioMixRecipe(musicAssetID: "music", musicVolume: 0.8, duckMusicDuringSpeech: duck),
                assetManifest: RenderAssetManifest(assets: references))
        }
        for (duck, expectedUnderSpeech) in [(true, 0.8 * 0.3), (false, 0.8)] {
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe(duck: duck), assetURLs: urls)
            let parameters = try XCTUnwrap(preview.playerItem.audioMix?.inputParameters)
            func level(_ p: AVAudioMixInputParameters, _ t: Double) -> Double {
                var start: Float = 1, end: Float = 1, range = CMTimeRange.zero
                let time = CMTime(seconds: t, preferredTimescale: 60_000)
                guard p.getVolumeRamp(for: time, startVolume: &start, endVolume: &end, timeRange: &range), range.duration > .zero,
                      range.containsTime(time) else { return Double(start) }
                return Double(start) + Double(end - start) * (t - range.start.seconds) / range.duration.seconds
            }
            let bed = try XCTUnwrap(parameters.first { abs(level($0, 0.8) - 0.8) < 0.01 && abs(level($0, 3.5) - 0.8) < 0.01 }, "music bed parameter")
            XCTAssertEqual(level(bed, 2.5), expectedUnderSpeech, accuracy: 0.02, "duck=\(duck)")
            XCTAssertEqual(level(bed, 3.7), 0.8, accuracy: 0.02, "recovered, duck=\(duck)")
        }
    }

    private func tone(_ url: URL, seconds: Double) throws -> URL {
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let frames = AVAudioFrameCount(48_000 * seconds)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)); buffer.frameLength = frames
        for index in 0..<Int(frames) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * 440 / 48_000)) }
        try AVAudioFile(forWriting: url, settings: format.settings).write(from: buffer)
        return url
    }
}
#endif
