import AVFoundation
import UIKit
import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor final class RenderedOriginalClockTests: XCTestCase {
    func testRenderedPhotoWindowBuildsAndExportsAfterHydration() async throws {
        try await verifyWindow(video: false)
    }

    func testRenderedVideoWindowBuildsAndExportsAfterHydration() async throws {
        try await verifyWindow(video: true)
    }

    func testMixedMontagePhotoQuickCutReordersReopensAndExportsSixFrames() async throws {
        let fixture = try await makeFixture(video: true, duration: 1.9)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        fixture.session.setClipTiming(clipID: "first", durationS: 0.2)
        let opening = fixture.session.clipOrderItems.map(\.id)
        XCTAssertEqual(opening.count, 2)
        guard opening.count == 2 else { return }
        XCTAssertTrue(fixture.session.applyClipOrder(Array(opening.reversed()), opening: opening))
        let snapshot = DraftSnapshot(draftID: "quick-cut", itemID: "fixture", variantKey: "guided_story",
            draftRevision: 1, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: fixture.session.document.encodeSnapshot(), canUndo: false, createdAt: .now)
        let reopened = NativeEditorSession(draft: snapshot.editorDraft(projectID: UUID()))
        let saved = Fixture(directory: fixture.directory, session: reopened, sources: fixture.sources)
        XCTAssertEqual(reopened.document.clips.map(\.id), ["second", "first"])
        XCTAssertEqual(try XCTUnwrap(reopened.document.clips[0].durationS), 1.9, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(reopened.document.clips[1].durationS), 0.2, accuracy: 1e-9)
        let program = try compile(saved)
        let cuts = try XCTUnwrap(program.recipe.tracks.first?.clips)
        XCTAssertEqual(cuts.map(\.sourceAssetID), ["source-1", "source-0"])
        XCTAssertEqual(cuts.last?.duration ?? 0, 0.2, accuracy: 1e-9)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 2.1, accuracy: 1e-9)
        let output = fixture.directory.appendingPathComponent("quick-cut.mp4")
        _ = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: fixture.directory.appendingPathComponent("export-state")), branding: .none
        ).export(recipe: program.recipe, assetURLs: program.assetURLs, outputURL: output)
        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, 2.1, accuracy: 1e-3)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        generator.maximumSize = CGSize(width: 32, height: 32)
        for frame in 56...62 {
            let result = try await generator.image(at: CMTime(value: Int64(frame), timescale: 30))
            let color = try pixel(result.image)
            if frame == 56 { XCTAssertGreaterThan(Int(color[2]), Int(color[0]) + 100, "video immediately before photo") }
            else { XCTAssertGreaterThan(Int(color[0]), Int(color[2]) + 100, "photo frame \(frame - 56) of six") }
        }
        let preview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let previewGenerator = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
        previewGenerator.videoComposition = preview.preview.playerItem.videoComposition
        previewGenerator.requestedTimeToleranceBefore = .zero
        previewGenerator.requestedTimeToleranceAfter = .zero
        let photo = try await previewGenerator.image(at: CMTime(value: 60, timescale: 30))
        let color = try pixel(photo.image)
        XCTAssertGreaterThan(Int(color[0]), Int(color[2]) + 100)
        let attachment = XCTAttachment(contentsOfFile: output)
        attachment.name = "mixed-montage-six-frame-photo.mp4"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func pixel(_ image: CGImage) throws -> [UInt8] {
        var rgba = [UInt8](repeating: 0, count: 4)
        try rgba.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 1, height: 1,
                bitsPerComponent: 8, bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(image, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        }
        return rgba
    }

    func testProxyRoundedVideoUsesOnlyBoundedFinalFrameHold() async throws {
        let duration = 1.933333
        let fixture = try await makeFixture(video: true, duration: duration)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let before = fixture.session.document
        let program = try compile(fixture)
        let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.last)
        XCTAssertEqual(clip.sourceStart, 0, accuracy: 1e-9)
        XCTAssertEqual(clip.sourceDuration, 1.92, accuracy: 1e-9)
        XCTAssertEqual(clip.rate, 1, accuracy: 1e-9)
        XCTAssertEqual(clip.duration, duration, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(clip.holdDuration), duration - 1.92, accuracy: 1e-9)
        XCTAssertLessThanOrEqual(try XCTUnwrap(clip.holdDuration), 1.0 / 60.0 + 1e-6)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 5 + duration, accuracy: 1e-9)
        XCTAssertEqual(fixture.session.document, before)
    }

    func testGenuineVideoOverrunIsStillRejected() async throws {
        let fixture = try await makeFixture(video: true, duration: 59.0 / 30.0)
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
        XCTAssertEqual(loadedDuration, 1.92, accuracy: 1e-9)
        let program = try compile(fixture)
        let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.last)
        XCTAssertEqual(clip.sourceStart, 0, accuracy: 1e-9)
        XCTAssertEqual(clip.duration, 1.92, accuracy: 1e-9)
        XCTAssertEqual(clip.rate, 1, accuracy: 1e-9)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 6.92, accuracy: 1e-9)
        let output = fixture.directory.appendingPathComponent("reopened.mp4")
        _ = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: fixture.directory.appendingPathComponent("export-state")),
            branding: .none
        ).export(recipe: program.recipe, assetURLs: program.assetURLs, outputURL: output)
        let exportedDuration = try await AVURLAsset(url: output).load(.duration).seconds
        XCTAssertEqual(exportedDuration, 6.92, accuracy: 1.0 / 30.0)
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

    private func makeFixture(video: Bool, duration: Double = 1.92) async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let first = directory.appendingPathComponent("red.png")
        try image(.red).write(to: first)
        let second = directory.appendingPathComponent(video ? "blue.mp4" : "blue.png")
        if video { try await writeVideo(second) } else { try image(.blue).write(to: second) }
        let assetDuration = video ? try await AVURLAsset(url: second).load(.duration).seconds : nil
        if let assetDuration { XCTAssertEqual(assetDuration, 1.92, accuracy: 1e-9) }
        let slots: [JSONValue] = [
            .object(["slot_id": .string("first"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(5)]),
            .object(["slot_id": .string("second"), "clip_index": .number(1), "in_s": .number(0),
                "duration_s": .number(duration), "timing_mode": .string("rendered"), "source_duration_s": assetDuration.map(JSONValue.number) ?? .null])
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
        for index in 0..<48 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 64,
                height: 64, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(UIColor.blue.cgColor); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 25)))
        }
        writer.endSession(atSourceTime: CMTime(value: 48, timescale: 25))
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed)
    }
}
