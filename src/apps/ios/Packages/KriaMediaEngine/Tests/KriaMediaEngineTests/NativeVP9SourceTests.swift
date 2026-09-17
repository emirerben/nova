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

    @MainActor func testHEVCSourceConvertsToSDRH264Proxy() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = try await makeHEVCVideo(in: directory)
        let cache = directory.appendingPathComponent("cache")

        let output = try await NativeVP9Source.shared.prepare(source, cacheDirectory: cache)
        XCTAssertNotEqual(output, source)
        let asset = AVURLAsset(url: output)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let video = try XCTUnwrap(tracks.first)
        let formats = try await video.load(.formatDescriptions)
        XCTAssertEqual(formats.map(CMFormatDescriptionGetMediaSubType), [kCMVideoCodecType_H264])
        let color = (CMFormatDescriptionGetExtensions(try XCTUnwrap(formats.first)) as NSDictionary?) ?? [:]
        XCTAssertEqual(color[kCMFormatDescriptionExtension_TransferFunction] as? String,
                       AVVideoTransferFunction_ITU_R_709_2 as String)
        let reader = try AVAssetReader(asset: asset)
        let decoded = AVAssetReaderTrackOutput(track: video, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ])
        reader.add(decoded)
        XCTAssertTrue(reader.startReading())
        XCTAssertNotNil(decoded.copyNextSampleBuffer())
        reader.cancelReading()
        let again = try await NativeVP9Source.shared.prepare(source, cacheDirectory: cache)
        XCTAssertEqual(again, output)
    }

    @MainActor private func makeHEVCVideo(in directory: URL) async throws -> URL {
        let source = directory.appendingPathComponent("phone-hevc.mp4")
        let writer = try AVAssetWriter(outputURL: source, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.hevc, AVVideoWidthKey: 64, AVVideoHeightKey: 64
        ])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64
        ])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for frame in 0..<3 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var raw: CVPixelBuffer?
            XCTAssertEqual(CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw), kCVReturnSuccess)
            let pixel = try XCTUnwrap(raw)
            CVPixelBufferLockBaseAddress(pixel, [])
            memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), Int32(frame * 40), CVPixelBufferGetDataSize(pixel))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: 30)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed, "\(String(describing: writer.error))")
        return source
    }
}
#endif
