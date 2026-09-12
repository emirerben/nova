#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import XCTest
import CoreImage
@testable import KriaMediaEngine

final class NativeVP9SourceTests: XCTestCase {
    @MainActor func testImageSourcesBypassConversion() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let image = directory.appendingPathComponent("photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 32, height: 32)),
            to: image, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let output = try await NativeVP9Source.shared.prepare(image, cacheDirectory: directory.appendingPathComponent("cache"))
        XCTAssertEqual(output, image)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), ["photo.png"])
    }

    @MainActor func testCancelledPreparationDoesNotPublishCache() async throws {
        let source = try XCTUnwrap(Bundle.module.url(forResource: "vp9-synthetic", withExtension: "mp4", subdirectory: "Fixtures"))
        let cache = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: cache) }
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await NativeVP9Source.shared.prepare(source, cacheDirectory: cache)
        }
        do { _ = try await task.value; XCTFail("Cancelled conversion succeeded") }
        catch is CancellationError { }
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.path))
    }

    @MainActor func testVP9SourceConvertsLocallyWithAudioAndReusableCache() async throws {
        let source = try XCTUnwrap(Bundle.module.url(forResource: "vp9-synthetic", withExtension: "mp4", subdirectory: "Fixtures"))
        let cache = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: cache) }
        let original = try SHA256Fingerprinter().fingerprint(file: source)
        let output = try await NativeVP9Source.shared.prepare(source, cacheDirectory: cache)
        XCTAssertNotEqual(source, output)
        XCTAssertEqual(try SHA256Fingerprinter().fingerprint(file: source), original)
        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 0.05)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let video = try XCTUnwrap(tracks.first)
        let formats = try await video.load(.formatDescriptions)
        XCTAssertEqual(formats.map(CMFormatDescriptionGetMediaSubType), [kCMVideoCodecType_H264])
        let audio = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertEqual(audio.count, 1)
        let audioReader = try AVAssetReader(asset: asset)
        let audioOutput = AVAssetReaderTrackOutput(track: try XCTUnwrap(audio.first), outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM])
        audioReader.add(audioOutput)
        XCTAssertTrue(audioReader.startReading())
        XCTAssertNotNil(audioOutput.copyNextSampleBuffer())
        audioReader.cancelReading()
        let generator = AVAssetImageGenerator(asset: asset)
        for seconds in [0.0, 0.5, 0.9] {
            let frame = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600))
            XCTAssertEqual(frame.image.width, 160)
            XCTAssertEqual(frame.image.height, 96)
        }
        let again = try await NativeVP9Source.shared.prepare(source, cacheDirectory: cache)
        XCTAssertEqual(output, again)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: cache.path).count, 1)
        let passthrough = try await NativeVP9Source.shared.prepare(output, cacheDirectory: cache)
        XCTAssertEqual(passthrough, output)
    }
}
#endif
