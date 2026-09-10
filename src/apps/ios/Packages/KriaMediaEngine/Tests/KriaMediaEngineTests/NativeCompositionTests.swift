import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class NativeCompositionTests: XCTestCase {
    @MainActor func testExportCannotOverwriteOriginalThroughSymlink() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("original.mp4")
        let bytes = Data("irreplaceable original".utf8)
        try bytes.write(to: source)
        let alias = directory.appendingPathComponent("export.mp4")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: source)
        let recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 1)])])
        let exporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
        for output in [source, alias] {
            do {
                _ = try await exporter.export(recipe: recipe, assetURLs: ["a": source], outputURL: output)
                XCTFail("Original overwrite must be rejected")
            } catch { XCTAssertEqual(try Data(contentsOf: source), bytes) }
        }
    }

    @MainActor func testRealExportMatchesPreviewTimingCrossfadeAndCodec() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let red = try await makeVideo(directory: directory, name: "red", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1))
        let green = try await makeVideo(directory: directory, name: "green", color: CGColor(red: 0, green: 1, blue: 0, alpha: 1))
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "r", relativePath: "r"), MediaAsset(id: "g", relativePath: "g")], tracks: [
            TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "r", sourceAssetID: "r", sourceDuration: 0.6, text: TextTreatment(text: "TOP", fontSize: 15, anchor: .top, animation: .none)),
                TimelineClip(id: "g", sourceAssetID: "g", sourceDuration: 0.6, timelineStart: 0.4, transition: Transition(duration: 0.2), text: TextTreatment(text: "BOTTOM", fontSize: 15, anchor: .bottom, animation: .none))
            ])
        ])
        let urls = ["r": red, "g": green]
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        XCTAssertEqual(preview.description.duration, 1, accuracy: 0.0001)
        let validator = CompositionValidator()
        let valid = try await preview.playerItem.videoComposition!.isValid(for: preview.playerItem.asset, timeRange: CMTimeRange(start: .zero, duration: CMTime(seconds: 1, preferredTimescale: 600)), validationDelegate: validator)
        XCTAssertTrue(valid)
        let output = directory.appendingPathComponent("output.mp4")
        let exporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("checkpoints")))
        let checkpoint = try await exporter.export(recipe: recipe, assetURLs: urls, outputURL: output, exportID: "test")
        XCTAssertEqual(checkpoint.status, .completed)
        let exported = AVURLAsset(url: output)
        let duration = try await exported.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 1 / 30)
        let tracks = try await exported.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let descriptions = try await track.load(.formatDescriptions)
        XCTAssertEqual(CMFormatDescriptionGetMediaSubType(try XCTUnwrap(descriptions.first)), kCMVideoCodecType_H264)
        let previewGenerator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        previewGenerator.videoComposition = preview.playerItem.videoComposition
        let exportGenerator = AVAssetImageGenerator(asset: exported)
        previewGenerator.requestedTimeToleranceBefore = .zero
        previewGenerator.requestedTimeToleranceAfter = .zero
        exportGenerator.requestedTimeToleranceBefore = .zero
        exportGenerator.requestedTimeToleranceAfter = .zero
        for seconds in [0.1, 0.5, 0.9] {
            let time = CMTime(seconds: seconds, preferredTimescale: 600)
            let expected = try await previewGenerator.image(at: time).image
            let actual = try await exportGenerator.image(at: time).image
            let a = rgba(expected), b = rgba(actual)
            XCTAssertEqual(a.count, b.count)
            let error = zip(a, b).map { abs(Int($0) - Int($1)) }.reduce(0, +)
            XCTAssertLessThan(Double(error) / Double(a.count), 8, "Preview/export parity at \(seconds)")
            let center = (80 * 96 + 48) * 4
            if seconds < 0.2 { XCTAssertGreaterThan(b[center], 220); XCTAssertLessThan(b[center + 1], 64) }
            if seconds > 0.8 { XCTAssertGreaterThan(b[center + 1], 220); XCTAssertLessThan(b[center], 30) }
            if seconds == 0.5 { XCTAssertGreaterThan(b[center], 70); XCTAssertGreaterThan(b[center + 1], 70) }
        }
        // Both treatments are present, have different intervals and are retained by every segment.
        let instruction = try XCTUnwrap(preview.playerItem.videoComposition?.instructions.first as? RecipeVideoInstruction)
        XCTAssertEqual(instruction.text.count, 2)
        XCTAssertEqual(instruction.text[1].start, 0.4)
        XCTAssertGreaterThan(instruction.text[0].frame.minY, instruction.text[1].frame.minY)
    }

    @MainActor func testProxyHasBoundedDimensionsAndPreservesSourceDuration() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let original = try await makeVideo(directory: directory, name: "source", color: CGColor(gray: 0.5, alpha: 1))
        let destination = directory.appendingPathComponent("proxy.mp4")
        _ = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 64, height: 36)).makeProxy(for: original, destination: destination)
        let proxy = AVURLAsset(url: destination)
        let tracks = try await proxy.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let size = try await track.load(.naturalSize)
        XCTAssertLessThanOrEqual(max(size.width, size.height), 64)
        let duration = try await proxy.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 1 / 30)
        XCTAssertNotEqual(try SHA256Fingerprinter().fingerprint(file: original), try SHA256Fingerprinter().fingerprint(file: destination))
    }

    @MainActor private func makeVideo(directory: URL, name: String, color: CGColor) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for index in 0..<30 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color)
            context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }

    private func rgba(_ image: CGImage) -> [UInt8] {
        var data = [UInt8](repeating: 0, count: image.width * image.height * 4)
        data.withUnsafeMutableBytes { bytes in
            let context = CGContext(data: bytes.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
        return data
    }
}
private final class CompositionValidator: NSObject, AVVideoCompositionValidationHandling {
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidValueForKey key: String) -> Bool { XCTFail("Invalid composition key: \(key)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingEmptyTimeRange timeRange: CMTimeRange) -> Bool { XCTFail("Uncovered composition time: \(timeRange)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidTimeRangeIn instruction: any AVVideoCompositionInstructionProtocol) -> Bool { XCTFail("Invalid instruction range: \(instruction.timeRange)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidTrackIDIn instruction: any AVVideoCompositionInstructionProtocol, layerInstruction: AVVideoCompositionLayerInstruction, asset: AVAsset) -> Bool { XCTFail("Invalid track: \(layerInstruction.trackID)"); return false }
}
#endif
