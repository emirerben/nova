import XCTest
@preconcurrency import AVFoundation
import KriaMediaEngine
@testable import Kria

/// End-to-end native media journey using the production original resolver and exporter.
@MainActor final class MediaLibraryExportJourneyTests: XCTestCase {
    func testReusedVideoExportsAfterDeletingItsFirstPost() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let video = try await makeVideo(in: root)
        let first = ProjectDirectory(root: root.appendingPathComponent("first"))
        let second = ProjectDirectory(root: root.appendingPathComponent("second"))
        let original = try await AssetImportCoordinator(project: first).importAsset(from: video)
        let hash = try XCTUnwrap(original.fingerprint)
        try SourceAssetStore(project: first).bind(mediaID: "first-clip", original: original)
        let assetID = UUID()
        let library = MediaLibraryOriginals(owner: "export-journey", base: root)
        try await library.retain(assetID: assetID, from: first, mediaID: "first-clip", original: original)
        try await library.bind(assetID: assetID, fingerprint: hash, mediaID: "second-clip", to: second)
        let exporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: root.appendingPathComponent("exports")), branding: .none)
        let firstOutput = root.appendingPathComponent("first.mp4")
        let secondOutput = root.appendingPathComponent("second.mp4")
        let firstRecipe = recipe(mediaID: "first-clip", start: 0, seconds: 1)
        let secondRecipe = recipe(mediaID: "second-clip", start: 0.25, seconds: 0.5)
        _ = try await exporter.export(recipe: firstRecipe, assetURLs: SourceAssetStore(project: first).resolve(mediaIDs: ["first-clip"]), outputURL: firstOutput)
        try FileManager.default.removeItem(at: first.root)
        _ = try await exporter.export(recipe: secondRecipe, assetURLs: SourceAssetStore(project: second).resolve(mediaIDs: ["second-clip"]), outputURL: secondOutput)
        let firstDuration = try await AVURLAsset(url: firstOutput).load(.duration).seconds
        let secondDuration = try await AVURLAsset(url: secondOutput).load(.duration).seconds
        XCTAssertEqual(firstDuration, 1, accuracy: 0.1)
        XCTAssertEqual(secondDuration, 0.5, accuracy: 0.1)
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: secondOutput))
        let frame = try await generator.image(at: .zero).image
        XCTAssertGreaterThan(frame.width, 0)
        // Real encoded outputs remain in the xcresult, including on a physical device.
        for url in [firstOutput, secondOutput] {
            let attachment = XCTAttachment(contentsOfFile: url)
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }
    private func recipe(mediaID: String, start: Double, seconds: Double) -> KriaMediaEngine.EditRecipe {
        KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: mediaID, relativePath: "originals/\(mediaID).mp4", duration: 1)], tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: mediaID, sourceStart: start, sourceDuration: seconds, timelineStart: 0)])], requiredCapabilities: [.basicComposition, .local1080Export])
    }
    private func makeVideo(in root: URL) async throws -> URL {
        let url = root.appendingPathComponent("original.mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input)
        XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<30 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(CGColor(red: 0.8, green: 0.1, blue: 0.1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished(); await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }
}
