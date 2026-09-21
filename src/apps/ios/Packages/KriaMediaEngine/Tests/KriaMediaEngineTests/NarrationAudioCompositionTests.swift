#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// KRI-132: a recorded voiceover rides its own `TimelineTrack(kind: .audio)`,
/// exactly like the existing "sfx" pattern `LiveAudioMixTests` covers, plus
/// two invariants specific to narration: `audio.narrationAssetID` derives
/// `.narrationAudio` in `effectiveCapabilities`, and the compiler always caps
/// the narration clip's `sourceDuration` to the video timeline -- so a
/// same-length narration track must never extend the exported duration.
final class NarrationAudioCompositionTests: XCTestCase {
    @MainActor func testNarrationTrackMixesAudibleAndDoesNotExtendExportDuration() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let photo = directory.appendingPathComponent("photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)

        let voice = directory.appendingPathComponent("voice.caf")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let videoDuration = 2.0
        let frameCount = AVAudioFrameCount(48_000 * videoDuration)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount))
        buffer.frameLength = frameCount
        for index in 0..<Int(frameCount) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * 440 / 48_000)) }
        do {
            let file = try AVAudioFile(forWriting: voice, settings: format.settings)
            try file.write(from: buffer)
        }

        let urls = ["photo": photo, "voice": voice]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map {
            MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map { RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) }
        let recipe = EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: videoDuration)]),
                TimelineTrack(id: "narration", kind: .audio, clips: [
                    TimelineClip(id: "narration-voice", sourceAssetID: "voice", sourceDuration: videoDuration, timelineStart: 0, volume: 1),
                ]),
            ],
            audio: AudioMixRecipe(originalVolume: 1, narrationAssetID: "voice"),
            assetManifest: RenderAssetManifest(assets: references)
        )
        try recipe.validate()
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.narrationAudio))
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), videoDuration)

        let output = directory.appendingPathComponent("narration.mp4")
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)

        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        // Equal-length narration must not push the export past the video timeline.
        XCTAssertEqual(duration, videoDuration, accuracy: 0.05)

        let track = try await asset.loadTracks(withMediaType: .audio).first
        let reader = try AVAssetReader(asset: asset)
        let output_ = AVAssetReaderTrackOutput(track: try XCTUnwrap(track), outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsNonInterleaved: false,
        ])
        reader.add(output_)
        XCTAssertTrue(reader.startReading())
        var peak: Float = 0
        while let sample = output_.copyNextSampleBuffer() {
            guard let block = CMSampleBufferGetDataBuffer(sample) else { continue }
            var bytes = Data(count: CMBlockBufferGetDataLength(block))
            _ = bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }
            bytes.withUnsafeBytes { raw in for value in raw.bindMemory(to: Float32.self) { peak = max(peak, abs(value)) } }
        }
        XCTAssertGreaterThan(peak, 0.01, "the narration track must be audible in the export")
    }
}
#endif
