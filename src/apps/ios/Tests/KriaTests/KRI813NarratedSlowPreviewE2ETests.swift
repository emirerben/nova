import XCTest
import AVFoundation
import CoreGraphics
import KriaMediaEngine
@testable import Kria

/// KRI-813 journey: the Troya museum narrated edit (stress run C7, job b9e4c530) previews its own
/// editable sources in the editor.
///
/// The phone exported the edit, but the editor's source preview failed on open, Retry, crop Save and
/// relaunch with "This edit's timeline couldn't be read", then fell back to the finished video. The
/// server's narrated planner slows short footage to fill each voiceover step and has no rate floor
/// (the sculpture plays at ~0.165x); the preview compiler refused anything under 0.25x.
///
/// `Fixtures/KRI813MuseumNarrated.json` is pinned against the server compiler by
/// `src/apps/api/tests/pipeline/test_kri813_narrated_preview_fixture.py`: the C7 step timings, each
/// step's source duration, and the exact per-clip `(source_start, source_duration, rate)` the phone
/// export plays. This test generates sources of those exact durations, binds them as the creator's
/// local originals, and runs the editor's real `prepareSourcePreview` (pool -> resolver ->
/// narrated hydration -> compiler -> live composition). The preview must be the editable composition
/// (not the finished-video fallback) and must play every step at the export's rate.
///
/// Evidence: `.dev/kri-813/report.json` (per-slot preview vs export rate, guard, composition duration).
@MainActor final class KRI813NarratedSlowPreviewE2ETests: XCTestCase {
    private struct Step: Decodable {
        struct Expected: Decodable { let sourceStart: Double; let sourceDuration: Double; let rate: Double }
        let stepID: String
        let clipIndex: Int
        let startS: Double
        let endS: Double
        let sourceDurationS: Double
        let expected: Expected
        // `convertFromSnakeCase` maps step_id to "stepId".
        enum CodingKeys: String, CodingKey { case stepID = "stepId", clipIndex, startS, endS, sourceDurationS, expected }
    }

    private struct Fixture: Decodable {
        struct Crop: Decodable {
            let stepID: String; let sourceCrop: [String: Double]
            enum CodingKeys: String, CodingKey { case stepID = "stepId", sourceCrop }
        }
        let voiceoverS: Double
        let crop: Crop
        let steps: [Step]
    }

    private static let repoRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

    private static func fixture() throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI813MuseumNarrated.json")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Fixture.self, from: Data(contentsOf: url))
    }

    // MARK: - Journey

    /// Generation aafe53ea: fresh open, then Retry (a second build of the same saved revision).
    func testMuseumEditPreviewsItsSourcesAtTheExportRates() async throws {
        let fixture = try Self.fixture()
        let session = try await museumSession(fixture, savedCrop: false)
        await session.prepareSourcePreview()
        _ = try await assertEditablePreview(session, fixture: fixture, label: "fresh-open")
        try await assertCompositionKeepsTheNarrationClock(session, seconds: fixture.voiceoverS)
        await session.prepareSourcePreview()
        _ = try await assertEditablePreview(session, fixture: fixture, label: "retry")
    }

    /// Generation ee703e65: the creator's saved crop on the slow sculpture slot, plus unsaved text and an
    /// unsaved trim of that slot. Each must show before Save and keep the export's slow-to-fill math.
    func testCropCorrectedEditPreviewsAndUnsavedEditsShowBeforeSave() async throws {
        let fixture = try Self.fixture()
        let session = try await museumSession(fixture, savedCrop: true)
        await session.prepareSourcePreview()
        let recipe = try await assertEditablePreview(session, fixture: fixture, label: "crop-saved")
        let cropped = try XCTUnwrap(videoClips(recipe).first { $0.id == fixture.crop.stepID })
        XCTAssertEqual(cropped.sourceCrop, .init(x: fixture.crop.sourceCrop["x"]!, y: fixture.crop.sourceCrop["y"]!,
                                                  width: fixture.crop.sourceCrop["width"]!, height: fixture.crop.sourceCrop["height"]!))

        // Unsaved text: the preview rebuilds with it (wrapped into one run per line), rates unchanged.
        session.addText(content: "Troy Museum")
        let withText = await waitUntil { session.displayedSourcePreviewRecipe?.textLayers.contains { $0.runs.map(\.text).joined(separator: " ") == "Troy Museum" } == true }
        XCTAssertTrue(withText, "Unsaved text must reach the editable preview; state \(session.sourcePreviewState)")
        _ = try await assertEditablePreview(session, fixture: fixture, label: "unsaved-text")

        // Unsaved trim (KRI-290 path): shorten the 0.165x sculpture slot from 14.22 s to 12 s. The span is
        // re-derived exactly like the server's `_fit_step_window`, still under 0.25x.
        let step = try XCTUnwrap(fixture.steps.first { $0.stepID == fixture.crop.stepID })
        let clip = try XCTUnwrap(session.timelineClips.first { $0.slotID == step.stepID })
        session.beginTrim(clipID: clip.id, edge: .trailing)
        session.updateTrim(by: 12 - (step.endS - step.startS))
        session.endTrim()
        let span = NativeNarratedSourceTiming.span(target: 12, available: step.sourceDurationS)
        let expectedRate = span / 12
        XCTAssertLessThan(expectedRate, 0.25)
        let trimmed = await waitUntil {
            guard let rate = session.displayedSourcePreviewRecipe.flatMap({ self.videoClips($0).first { $0.id == step.stepID }?.rate }) else { return false }
            return abs(rate - expectedRate) < 1e-6
        }
        XCTAssertTrue(trimmed, "The unsaved trim must preview at span/target = \(expectedRate); state \(session.sourcePreviewState)")
        let after = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        try after.validate()
        let trimmedClip = try XCTUnwrap(videoClips(after).first { $0.id == step.stepID })
        XCTAssertEqual(trimmedClip.sourceDuration, span, accuracy: 1e-6)
        XCTAssertEqual(trimmedClip.sourceDuration / trimmedClip.rate, 12, accuracy: 1e-6, "No freeze-hold: the slowed footage fills the slot")
    }

    // MARK: - Guards around the narrated exception

    /// Only narrated-derived rates may go under 0.25x. A creator-authored rate or a plain slot keeps
    /// the editor's 0.25...4 range; a source with no usable footage stays an honest error.
    func testSlowRatesStayLimitedToNarratedDerivedSlots() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let source = Self.source(index: 0, duration: 2.4)
        func compile(_ raw: [String: JSONValue], trimOut: Double = 2.35, duration: Double = 14.22) throws -> TimelineClip {
            let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: duration, raw: raw)])
            let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
                                  trimIn: 0, trimOut: trimOut, sourceDuration: 2.4, slotID: "slot")
            // Not XCTUnwrap: it records a failure for the throws this test expects.
            guard let compiled = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source]).recipe.tracks.first?.clips.first else {
                throw CocoaError(.coderValueNotFound)
            }
            return compiled
        }
        // Narrated hydration (span only) and the device-recipe seed (explicit seed rate) compile.
        XCTAssertEqual(try compile(["native_source_span_s": .number(2.35)]).rate, 2.35 / 14.22, accuracy: 1e-9)
        XCTAssertEqual(try compile(["native_composite_source": .bool(true), "native_source_span_s": .number(2.35),
                                    "playback_rate": .number(2.35 / 14.22)]).rate, 2.35 / 14.22, accuracy: 1e-9)
        // A plain slot whose window would need 0.165x, and a creator rate under 0.25x, still refuse.
        XCTAssertThrowsError(try compile([:])) { XCTAssertEqual($0 as? RecipeError, .invalidTimeline) }
        for bad in [0.2, 0, -1, 4.5, .nan] {
            XCTAssertThrowsError(try compile(["playback_rate": .number(bad)]), "creator rate \(bad)")
            XCTAssertThrowsError(try compile(["native_source_span_s": .number(2.35), "playback_rate": .number(bad)]), "explicit rate \(bad) on a narrated slot")
        }
        // Under the server's own 0.05 s minimum there is no footage to slow.
        XCTAssertThrowsError(try compile(["native_source_span_s": .number(0.04)], trimOut: 0.04))
    }

    /// A slot whose source is missing from the resolved pool is a missing original (actionable recovery),
    /// not "the timeline couldn't be read". A removed slot needs no source at all.
    func testMissingNarratedSourceRoutesToOriginalsRecovery() throws {
        let sources = [0: Self.source(index: 0, duration: 2.4)]
        var removed = EditorTimelineSlot(id: "gone", clipIndex: 7, inS: 0, durationS: 3)
        removed.removed = true
        let ok = try NativeNarratedSourceTiming.hydrate(EditorDocument(clips: [.init(id: "a", clipIndex: 0, inS: 0, durationS: 14.22), removed]), sources: sources)
        XCTAssertEqual(ok.clips[0].raw["native_source_span_s"], .number(2.35))
        XCTAssertNil(ok.clips[1].raw["native_source_span_s"])
        XCTAssertThrowsError(try NativeNarratedSourceTiming.hydrate(
            EditorDocument(clips: [.init(id: "a", clipIndex: 0, inS: 0, durationS: 3), .init(id: "b", clipIndex: 4, inS: 0, durationS: 3)]),
            sources: sources)) {
            XCTAssertTrue(NativeEditorSession.isMissingOriginals($0), "\($0)")
        }
        // A present source with no usable footage stays an honest timeline error, as on the server.
        XCTAssertThrowsError(try NativeNarratedSourceTiming.hydrate(EditorDocument(clips: [.init(id: "a", clipIndex: 0, inS: 0, durationS: 3)]),
                                                                    sources: [0: Self.source(index: 0, duration: 0.04)])) {
            XCTAssertEqual($0 as? RecipeError, .invalidTimeline)
        }
    }

    // MARK: - Harness

    private func assertEditablePreview(_ session: NativeEditorSession, fixture: Fixture, label: String) async throws -> KriaMediaEngine.EditRecipe {
        let ready = await waitUntil { session.displayedSourcePreviewRecipe != nil }
        XCTAssertTrue(ready, "\(label): the editable preview must be on screen, not the finished-video fallback; state \(session.sourcePreviewState)")
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(session.hasSourcePreview)
        try recipe.validate()
        let clips = videoClips(recipe)
        XCTAssertEqual(clips.map(\.id), fixture.steps.map(\.stepID))
        var rows: [[String: Any]] = []
        for step in fixture.steps {
            guard let clip = clips.first(where: { $0.id == step.stepID }) else { XCTFail("\(label): \(step.stepID) missing"); continue }
            // Exact parity with the export, never a clamp (a clamp would freeze-hold and drift from the voice).
            XCTAssertEqual(clip.rate, step.expected.rate, accuracy: 1e-6, "\(label) \(step.stepID) rate")
            XCTAssertEqual(clip.sourceStart, step.expected.sourceStart, accuracy: 1e-6, "\(label) \(step.stepID) source start")
            XCTAssertEqual(clip.sourceDuration, step.expected.sourceDuration, accuracy: 1e-6, "\(label) \(step.stepID) source span")
            XCTAssertEqual(clip.timelineStart, step.startS, accuracy: 1e-6, "\(label) \(step.stepID) timeline start")
            XCTAssertNil(clip.holdDuration, "\(label) \(step.stepID) must fill its step without a freeze-hold")
            rows.append(["step": step.stepID, "preview_rate": clip.rate, "export_rate": step.expected.rate,
                         "delta": abs(clip.rate - step.expected.rate), "source_span": clip.sourceDuration])
        }
        writeReport(label: label, fields: ["state": "ready", "clips": rows, "source_crop_slots":
            clips.filter { $0.sourceCrop != nil }.map(\.id)])
        return recipe
    }

    /// The composition's picture must end where the narration does: slow `scaleTimeRange` rounding must
    /// not drift the cut points away from the voice.
    private func assertCompositionKeepsTheNarrationClock(_ session: NativeEditorSession, seconds: Double) async throws {
        let asset = try XCTUnwrap(session.player?.currentItem?.asset)
        let duration = try await asset.load(.duration).seconds
        let video = try await asset.loadTracks(withMediaType: .video)
        var trackEnds: [Double] = []
        var segments: [[String: Double]] = []
        for (number, track) in video.enumerated() {
            trackEnds.append(try await track.load(.timeRange).end.seconds)
            for segment in try await track.load(.segments) where !segment.isEmpty {
                let mapping = segment.timeMapping
                segments.append(["track": Double(number), "target_start": mapping.target.start.seconds, "target_end": mapping.target.end.seconds,
                                 "source_start": mapping.source.start.seconds, "source_duration": mapping.source.duration.seconds])
            }
        }
        // The edit's own picture must end where the narration does; the only thing after it is the Kria outro,
        // which the engine appends at exactly the edit's end.
        let frame = 1.0 / 25
        let edit = segments.filter { $0["target_start"]! < seconds - frame }
        let tail = segments.filter { $0["target_start"]! >= seconds - frame }
        XCTAssertEqual(edit.map { $0["target_end"]! }.max() ?? 0, seconds, accuracy: frame, "Slow scaleTimeRange must not drift the cuts from the 54 s voice")
        XCTAssertTrue(tail.allSatisfy { abs($0["target_start"]! - seconds) < frame }, "Only the outro may follow the edit: \(tail)")
        XCTAssertEqual(duration, (tail.map { $0["target_end"]! }.max() ?? seconds), accuracy: frame)
        writeReport(label: "composition", fields: ["composition_duration_s": duration, "video_track_end_s": trackEnds, "narration_s": seconds,
                                                         "segments": segments])
    }

    private func museumSession(_ fixture: Fixture, savedCrop: Bool) async throws -> NativeEditorSession {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri813-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var poolClips: [NativeEditorSourcePool.Clip] = []
        var slots: [JSONValue] = []
        for step in fixture.steps {
            let url = try await Self.makeVideo(in: directory, index: step.clipIndex, seconds: step.sourceDurationS)
            let asset = try await AssetImportCoordinator(project: project).importAsset(from: url)
            let mediaID = "kri813-clip-\(step.clipIndex)"
            try SourceAssetStore(project: project).bind(mediaID: mediaID, original: asset)
            let fingerprint = try XCTUnwrap(asset.fingerprint)
            poolClips.append(.init(clipIndex: step.clipIndex, nativeSource: .init(mediaID: mediaID, sourceURL: nil,
                original: OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount, durationS: step.sourceDurationS,
                    width: 64, height: 112, orientationDegrees: 0, hasAudio: false), localRequired: true)))
            var slot: [String: JSONValue] = ["slot_id": .string(step.stepID), "clip_index": .number(Double(step.clipIndex)),
                "in_s": .number(0), "duration_s": .number(step.endS - step.startS),
                "source_duration_s": .number(step.sourceDurationS), "removed": .bool(false)]
            if savedCrop, step.stepID == fixture.crop.stepID {
                slot["source_crop"] = .object(fixture.crop.sourceCrop.mapValues(JSONValue.number))
            }
            slots.append(.object(slot))
        }
        let variant: [String: JSONValue] = [
            "variant_id": .string("narrated"),
            "render_generation_id": .string("generation-1"),
            "render_status": .string("ready"),
            "render_destination": .string("device"),
            "resolved_archetype": .string("narrated"),
            "duration_s": .number(fixture.voiceoverS),
            "output_url": .string("file:///tmp/kri813-finished.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
                "clips": .object(["source_crop": .object(["editable": .bool(true)])])]),
            "user_timeline": .object(["slots": .array(slots)]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "narrated", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: poolClips, baseGeneration: "generation-1")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: DeviceRenderRequest(
            identity: DeviceRenderIdentity(jobID: jobID, variantID: "narrated", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
                tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])])
        ), publishedGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        XCTAssertEqual(session.timelineClips.count, fixture.steps.count)
        return session
    }

    private func videoClips(_ recipe: KriaMediaEngine.EditRecipe) -> [TimelineClip] {
        recipe.tracks.filter { $0.kind == .video }.flatMap(\.clips).sorted { $0.timelineStart < $1.timelineStart }
    }

    private static func source(index: Int, duration: Double) -> ResolvedEditorSource {
        ResolvedEditorSource(clipIndex: index, mediaID: "kri813-clip-\(index)",
            asset: MediaAsset(id: "kri813-\(index)", relativePath: "kri813-\(index).mp4",
                fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: duration),
            url: URL(fileURLWithPath: "/fixture/kri813-\(index).mp4"))
    }

    /// 25 fps so every C7 duration (1.92 ... 26.4 s) is a whole number of frames.
    private static func makeVideo(in directory: URL, index: Int, seconds: Double) async throws -> URL {
        let url = directory.appendingPathComponent("clip-\(index).mp4")
        let width = 64, height = 112, fps: Int32 = 25
        let frames = Int((seconds * Double(fps)).rounded())
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        input.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(input)
        guard writer.startWriting() else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
        writer.startSession(atSourceTime: .zero)
        let shade = CGFloat(index + 1) / 13
        for frame in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(CGColor(red: shade, green: CGFloat(frame % 25) / 25, blue: 1 - shade, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            guard adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: fps)) else {
                throw writer.error ?? CocoaError(.fileWriteUnknown)
            }
        }
        input.markAsFinished()
        writer.endSession(atSourceTime: CMTime(value: Int64(frames), timescale: fps))
        await writer.finishWriting()
        guard writer.status == .completed else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
        return url
    }

    private func writeReport(label: String, fields: [String: Any]) {
        let directory = Self.repoRoot.appendingPathComponent(".dev/kri-813", isDirectory: true)
        let url = directory.appendingPathComponent("report.json")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var report = (try? Data(contentsOf: url)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        report["\(name) \(label)"] = fields.merging(["recorded_at": ISO8601DateFormatter().string(from: Date())]) { value, _ in value }
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    private func waitUntil(timeout: Duration = .seconds(60), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(25))
        }
        return condition()
    }
}
