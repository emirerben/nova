import AVFoundation
import UIKit
import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor final class SavedDraftPreviewRangeTests: XCTestCase {
    func testSavedPhotoWindowBuildsTheSamePreviewAfterHydration() async throws {
        try await verifyWindow(video: false)
    }

    func testSavedVideoWindowBuildsTheSamePreviewAfterHydration() async throws {
        try await verifyWindow(video: true)
    }

    func testARealOneMillisecondVideoOverrunIsStillRejected() async throws {
        let fixture = try await makeFixture(video: true, duration: 1.201)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        XCTAssertThrowsError(try compile(fixture)) { error in
            guard case RecipeError.invalidTimeline = error else {
                return XCTFail("Unexpected refusal: \(error)")
            }
        }
    }

    private func verifyWindow(video: Bool) async throws {
        let fixture = try await makeFixture(video: video)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let loadedDuration = try XCTUnwrap(fixture.session.document.clips.last?.durationS)
        XCTAssertGreaterThan(loadedDuration, 1.2) // Actual saved-draft subtraction, not an injected epsilon.
        let program = try compile(fixture)
        let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.last)
        XCTAssertEqual(clip.sourceStart, 0, accuracy: 1e-9)
        XCTAssertEqual(clip.duration, 1.2, accuracy: 1e-9)
        XCTAssertEqual(clip.rate, 1, accuracy: 1e-9)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 6.2, accuracy: 1e-9)
        let preview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let generator = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
        generator.videoComposition = preview.preview.playerItem.videoComposition
        let frame = try await generator.image(at: CMTime(seconds: 5.6, preferredTimescale: 600))
        var pixel = [UInt8](repeating: 0, count: 4)
        try pixel.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 1, height: 1,
                bitsPerComponent: 8, bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(frame.image, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        }
        XCTAssertGreaterThan(Int(pixel[2]), Int(pixel[0]) + 100)
        let image = XCTAttachment(image: UIImage(cgImage: frame.image))
        image.name = video ? "saved-video-preview" : "saved-photo-preview"
        image.lifetime = .keepAlways; add(image)
    }

    private struct Fixture {
        let directory: URL
        let session: NativeEditorSession
        let sources: [Int: ResolvedEditorSource]
    }

    private func compile(_ fixture: Fixture) throws -> NativeEditorRenderProgram {
        let compiler = try NativeEditorRenderCompiler(fontDirectory:
            XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: fixture.session.document,
            clips: fixture.session.timelineClips, items: fixture.session.timelineItems,
            sources: fixture.sources)
    }

    private func makeFixture(video: Bool, duration: Double = 1.2) async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let first = directory.appendingPathComponent("red.png")
        try image(.red).write(to: first)
        let second = directory.appendingPathComponent(video ? "blue.mp4" : "blue.png")
        if video { try await writeVideo(second) } else { try image(.blue).write(to: second) }
        let assetDuration = video ? try await AVURLAsset(url: second).load(.duration).seconds : nil
        let slots: [JSONValue] = [
            .object(["slot_id": .string("first"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(5)]),
            .object(["slot_id": .string("second"), "clip_index": .number(1), "in_s": .number(0),
                "duration_s": .number(duration), "source_duration_s": assetDuration.map(JSONValue.number) ?? .null])
        ]
        let saved = DraftSnapshot(draftID: "fixture", itemID: "fixture", variantKey: "guided_story",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": .array(slots)])])],
            canUndo: false, createdAt: .now)
        let session = NativeEditorSession(draft: saved.editorDraft(projectID: UUID()))
        var sources: [Int: ResolvedEditorSource] = [:]
        for (index, url) in [first, second].enumerated() {
            sources[index] = ResolvedEditorSource(clipIndex: index, mediaID: "source-\(index)",
                asset: MediaAsset(id: "source-\(index)", relativePath: url.lastPathComponent,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: url),
                    duration: index == 1 ? assetDuration : nil), url: url)
        }
        return Fixture(directory: directory, session: session, sources: sources)
    }

    private func image(_ color: UIColor) throws -> Data {
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64)).image { context in
            color.setFill(); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }.pngData())
    }

    private func writeVideo(_ url: URL) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
                kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<36 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 64,
                height: 64, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(UIColor.blue.cgColor); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        writer.endSession(atSourceTime: CMTime(value: 36, timescale: 30))
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed)
    }
}
