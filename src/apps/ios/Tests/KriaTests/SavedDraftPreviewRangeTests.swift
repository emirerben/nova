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

    func testLegacyFrameRoundedSourceTailPreservesWholeTimelineRateAndPreview() async throws {
        for mixed in [false, true] {
            let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true, mixed: mixed)
            defer { try? FileManager.default.removeItem(at: fixture.directory) }
            let before = fixture.session.document
            let program = try compile(fixture)
            let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.last)
            XCTAssertEqual(clip.timelineStart, 5, accuracy: 0.000_001)
            XCTAssertEqual(clip.sourceStart, 0, accuracy: 0.000_001)
            XCTAssertEqual(clip.sourceDuration, 1.92, accuracy: 0.000_001)
            XCTAssertEqual(clip.rate, 1, accuracy: 0.000_001)
            XCTAssertEqual(clip.holdDuration ?? 0, 58.0 / 30 - 1.92, accuracy: 0.000_001)
            XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 5 + 58.0 / 30, accuracy: 0.000_001)
            XCTAssertEqual(fixture.session.document, before, "render projection must not rewrite the saved lanes")
            let reopened = EditorDocument(snapshot: before.encodeSnapshot())
            XCTAssertEqual(reopened.clips.last?.durationS ?? 0, 58.0 / 30, accuracy: 0.000_001)
            XCTAssertNil(reopened.clips.last?.raw["source_end_s"])
            let preview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
            let generator = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
            generator.videoComposition = preview.preview.playerItem.videoComposition
            let frame = try await generator.image(at: CMTime(seconds: 5 + 58.0 / 30 - 0.005, preferredTimescale: 600))
            XCTAssertGreaterThan(frame.image.width, 0, "the rounded tail must have a visible final frame")
            let attachment = XCTAttachment(image: UIImage(cgImage: frame.image))
            attachment.name = mixed ? "mixed-saved-source-tail" : "legacy-saved-source-tail"
            attachment.lifetime = .keepAlways; add(attachment)
        }
    }

    func testLegacyTailDoesNotAdmitNonGridExcessOrLongHolds() throws {
        XCTAssertNil(NativeLegacySourceWindow.sourceSpan(start: 0, duration: 1.921, rate: 1, sourceDuration: 1.92))
        XCTAssertNil(NativeLegacySourceWindow.sourceSpan(start: 0, duration: 2, rate: 1, sourceDuration: 1.92))
        XCTAssertNil(NativeLegacySourceWindow.sourceSpan(start: 1.92, duration: 1.0 / 30, rate: 1, sourceDuration: 1.92))
        XCTAssertNil(NativeLegacySourceWindow.sourceSpan(start: 0, duration: 58.0 / 30, rate: 1, sourceDuration: .nan))
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

    private func makeFixture(video: Bool, duration: Double = 1.2, legacyTail: Bool = false, mixed: Bool = false) async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let first = directory.appendingPathComponent("red.png")
        try image(.red).write(to: first)
        let second = directory.appendingPathComponent(video ? "blue.mp4" : "blue.png")
        if video { try await writeVideo(second, frames: legacyTail ? 48 : 36, fps: legacyTail ? 25 : 30) } else { try image(.blue).write(to: second) }
        let assetDuration = video ? try await AVURLAsset(url: second).load(.duration).seconds : nil
        var slots: [JSONValue] = [
            .object(["slot_id": .string("first"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(5)]),
            .object(["slot_id": .string("second"), "clip_index": .number(1), "in_s": .number(0),
                "duration_s": .number(duration), "source_duration_s": assetDuration.map(JSONValue.number) ?? .null])
        ]
        if mixed, var first = slots[0].objectValue {
            first["source_end_s"] = .number(5)
            slots[0] = .object(first)
        }
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

    private func writeVideo(_ url: URL, frames: Int = 36, fps: Int32 = 30) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
                kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 64,
                height: 64, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(UIColor.blue.cgColor); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        writer.endSession(atSourceTime: CMTime(value: Int64(frames), timescale: fps))
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed)
    }
}
