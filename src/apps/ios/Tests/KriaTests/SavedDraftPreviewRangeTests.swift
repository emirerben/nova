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

    func testSerializedNullEndpointAndStaleHintsUseMeasuredOriginal() async throws {
        for hint: JSONValue? in [.number(1.92), .number(1.90), .number(3), .null, nil] {
            let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true,
                                                endpoint: .null, hint: hint)
            defer { try? FileManager.default.removeItem(at: fixture.directory) }
            let before = fixture.session.document
            XCTAssertEqual(before.clips.last?.raw["source_end_s"], .null)
            XCTAssertEqual(fixture.session.timelineClips.last!.end, 5 + 58.0 / 30, accuracy: 0.000_001)
            let program = try compile(fixture)
            let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.last)
            XCTAssertEqual(clip.rate, 1, accuracy: 0.000_001, "hint: \(String(describing: hint))")
            XCTAssertEqual(clip.sourceDuration, 1.92, accuracy: 0.000_001)
            XCTAssertEqual(clip.holdDuration ?? 0, 58.0 / 30 - 1.92, accuracy: 0.000_001)
            XCTAssertEqual(fixture.session.document, before)
        }
    }

    func testRecoveredTailPreservesOffGridNeighborTransitionAndTimedLanes() async throws {
        let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true,
                                            firstDuration: 1.91, overlap: 0.157)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let before = fixture.session.document
        let program = try compile(fixture)
        let clips = try XCTUnwrap(program.recipe.tracks.first?.clips)
        XCTAssertEqual(clips[0].duration, 1.91, accuracy: 0.000_001)
        XCTAssertEqual(clips[1].timelineStart, 1.91 - 0.157, accuracy: 0.000_001)
        XCTAssertEqual(clips[1].transition?.duration ?? 0, 0.157, accuracy: 0.000_001)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), 1.91 - 0.157 + 58.0 / 30, accuracy: 0.000_001)
        XCTAssertEqual(fixture.session.document, before)
    }

    func testBackendSaveWireReopensWithExactNeighborTextAndAudioClocks() async throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "saved_editor_legacy_wire", withExtension: "json"))
        var wire = try JSONDecoder().decode([String: JSONValue].self, from: Data(contentsOf: url))
        var payload = try XCTUnwrap(wire["editor_payload"]?.objectValue)
        var sections = try XCTUnwrap(payload["sections"]?.objectValue)
        // An independent audio lane must retain its own off-grid clock as well.
        sections["clip_audio"] = .array([.object([
            "id": .string("speech"), "source_clip_index": .number(1),
            "source_start_s": .number(0.217), "source_end_s": .number(0.917),
            "start_s": .number(1.817), "gain": .number(0.8)
        ])])
        payload["sections"] = .object(sections); wire["editor_payload"] = .object(payload)
        let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true, wire: wire)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let before = fixture.session.document
        let program = try compile(fixture)
        let video = try XCTUnwrap(program.recipe.tracks.first { $0.kind == .video })
        XCTAssertEqual(video.clips[0].duration, 1.91, accuracy: 0.000_001)
        XCTAssertEqual(video.clips[1].timelineStart, 1.753, accuracy: 0.000_001)
        XCTAssertEqual(video.clips[1].rate, 1)
        XCTAssertEqual(video.clips[1].holdDuration ?? 0, 58.0 / 30 - 1.92, accuracy: 0.000_001)
        let text = try XCTUnwrap(program.recipe.textLayers.first)
        XCTAssertEqual(text.start, 1.753, accuracy: 0.000_001)
        XCTAssertEqual(text.end, 2.417, accuracy: 0.000_001)
        let audio = try XCTUnwrap(program.recipe.tracks.first { $0.kind == .audio }?.clips.first)
        XCTAssertEqual(audio.timelineStart, 1.817, accuracy: 0.000_001)
        XCTAssertEqual(audio.sourceStart, 0.217, accuracy: 0.000_001)
        XCTAssertEqual(audio.sourceDuration, 0.7, accuracy: 0.000_001)
        XCTAssertEqual(fixture.session.document, before)
    }

    func testBoundOriginalWinsOverHintsForFitsAndRealShortages() async throws {
        for original in [1.90, 2.0] {
            let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true,
                                                endpoint: .null, hint: .number(1.92), originalDuration: original)
            defer { try? FileManager.default.removeItem(at: fixture.directory) }
            if original < 1.92 {
                XCTAssertThrowsError(try compile(fixture))
            } else {
                let clip = try XCTUnwrap(compile(fixture).recipe.tracks.first?.clips.last)
                XCTAssertEqual(clip.rate, 1)
                XCTAssertEqual(clip.sourceDuration, 58.0 / 30, accuracy: 0.000_001)
                XCTAssertNil(clip.holdDuration)
            }
        }
    }

    func testMillisecondAlignedStrictFitUsesOriginalInsteadOfSavedHint() async throws {
        let fixture = try await makeFixture(video: true, duration: 1.9, legacyTail: true,
                                            endpoint: .null, hint: .number(1.888), originalDuration: 2)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let before = fixture.session.document
        let clip = try XCTUnwrap(compile(fixture).recipe.tracks.first?.clips.last)
        XCTAssertEqual(clip.rate, 1, accuracy: 0.000_001)
        XCTAssertEqual(clip.sourceDuration, 1.9, accuracy: 0.000_001)
        XCTAssertEqual(clip.sourceStart, 0)
        XCTAssertEqual(clip.timelineStart, 5)
        XCTAssertNil(clip.holdDuration)
        XCTAssertEqual(fixture.session.document, before)
    }

    func testMissingOriginalMeasurementCannotAuthorizeTailPadding() async throws {
        let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true, endpoint: .null)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let bound = try XCTUnwrap(fixture.sources[1])
        var sources = fixture.sources
        sources[1] = ResolvedEditorSource(clipIndex: 1, mediaID: bound.mediaID,
            asset: MediaAsset(id: bound.asset.id, relativePath: bound.asset.relativePath, fingerprint: bound.asset.fingerprint), url: bound.url)
        XCTAssertThrowsError(try compile(Fixture(directory: fixture.directory, session: fixture.session, sources: sources)))
    }

    func testInvalidNonNullEndpointCannotBecomeLegacyRecovery() async throws {
        for endpoint: JSONValue in [.string("invalid"), .number(0), .number(-1), .bool(false)] {
            let fixture = try await makeFixture(video: true, duration: 58.0 / 30, legacyTail: true, endpoint: endpoint)
            defer { try? FileManager.default.removeItem(at: fixture.directory) }
            XCTAssertThrowsError(try compile(fixture), "endpoint: \(endpoint)")
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

    private func makeFixture(video: Bool, duration: Double = 1.2, legacyTail: Bool = false, mixed: Bool = false, endpoint: JSONValue? = nil, hint: JSONValue? = .number(1.92), firstDuration: Double = 5, overlap: Double? = nil, wire: [String: JSONValue]? = nil, originalDuration: Double? = nil) async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let first = directory.appendingPathComponent("red.png")
        try image(.red).write(to: first)
        let second = directory.appendingPathComponent(video ? "blue.mp4" : "blue.png")
        if video { try await writeVideo(second, frames: originalDuration.map { Int(($0 * 100).rounded()) } ?? (legacyTail ? 48 : 36), fps: originalDuration != nil ? 100 : (legacyTail ? 25 : 30)) } else { try image(.blue).write(to: second) }
        let assetDuration = video ? try await AVURLAsset(url: second).load(.duration).seconds : nil
        var slots: [JSONValue] = [
            .object(["slot_id": .string("first"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(firstDuration)]),
            .object(["slot_id": .string("second"), "clip_index": .number(1), "in_s": .number(0),
                "duration_s": .number(duration), "source_duration_s": assetDuration.map(JSONValue.number) ?? .null])
        ]
        if legacyTail, var tail = slots[1].objectValue {
            tail["source_end_s"] = endpoint
            tail["source_duration_s"] = hint
            slots[1] = .object(tail)
        }
        if let overlap, var first = slots[0].objectValue {
            first["transition_after"] = .string("crossfade")
            first["transition_duration_s"] = .number(overlap)
            slots[0] = .object(first)
        }
        if mixed, var first = slots[0].objectValue {
            first["source_end_s"] = .number(5)
            slots[0] = .object(first)
        }
        let saved = DraftSnapshot(draftID: "fixture", itemID: "fixture", variantKey: "guided_story",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: wire ?? ["editor_payload": .object(["sections": .object(["timeline_slots": .array(slots)])])],
            canUndo: false, createdAt: .now)
        let reopened = try JSONDecoder().decode(DraftSnapshot.self, from: JSONEncoder().encode(saved))
        let session = NativeEditorSession(draft: reopened.editorDraft(projectID: UUID()))
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
