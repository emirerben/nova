import XCTest
import AVFoundation
import CoreGraphics
import KriaMediaEngine
@testable import Kria

/// KRI-1032 journey: Plaza de Mayo M1 ("maximo 3", job 97d55f9a). The creator's own copy, saved
/// from the editor, was not the render: every clip audible (the render muted all but Recoleta),
/// the three landscape clips centre-cropped (the render had bars), labels at y 0.78 (render 0.68).
///
/// Ways this fails, written before the fix:
/// 1. The server serves the editor the approved plan's text and timeline instead of the rendered
///    ones (no `muted` slots, labels at 0.78) — pinned server-side by
///    `src/apps/api/tests/routes/test_kri1032_editor_matches_render.py`, which also writes
///    `Fixtures/KRI1032PlazaM1Editor.json`: this video's status variant and timeline as served.
/// 2. The editor never knows a clip's display size (a device original or a downloaded copy carries
///    none), so `fitTransform` returns identity and every landscape clip cover-fills.
/// 3. The preview drops a slot's `muted` (camera sound plays) or moves the text.
/// 4. Save to Photos / Share re-exports the editor composition instead of handing over the
///    finished render when the creator changed nothing.
///
/// The test binds generated originals of the run's shapes and lengths (with a tone on every clip)
/// as the creator's local files, runs the editor's real `prepareSourcePreview` (pool -> resolver ->
/// compiler -> live composition), and checks the preview recipe against the render; it exports
/// that composition to `.dev/kri-1032/editor-copy.mp4` for volumedetect / cropdetect.
@MainActor final class KRI1032MontageCopyParityE2ETests: XCTestCase {
    private struct Fixture: Decodable {
        struct Source: Decodable { let clipIndex: Int; let durationS: Double; let aspect: Double }
        struct Render: Decodable {
            struct Text: Decodable { let id: String; let xFrac: Double; let yFrac: Double }
            let landscapeFit: String
            let mutedSlots: [String]
            let text: [Text]
        }
        let variant: [String: JSONValue]
        let slots: [JSONValue]
        let sources: [Source]
        let render: Render
    }

    private static let repoRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    private static let generation = "53d6a9f3-a2a5-4781-b26e-22d4bec99bcc"

    private static func fixture() throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI1032PlazaM1Editor.json")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Fixture.self, from: Data(contentsOf: url))
    }

    func testEditorPreviewAndSavedCopyAreTheRender() async throws {
        let fixture = try Self.fixture()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri1032-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let finished = directory.appendingPathComponent("finished-render.mp4")
        let (session, fake) = try await plazaSession(fixture, in: directory, finishedRender: finished)

        await session.prepareSourcePreview()
        let ready = await waitUntil { session.displayedSourcePreviewRecipe != nil && session.canDownloadCurrentVideo }
        XCTAssertTrue(ready, "the editable preview must be on screen; state \(session.sourcePreviewState)")
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        try recipe.validate()

        // Sound: exactly the clips the render muted are silent.
        let clips = recipe.tracks.filter { $0.kind == .video }.flatMap(\.clips)
        XCTAssertEqual(clips.count, fixture.slots.count)
        let muted = Set(fixture.render.mutedSlots)
        var rows: [[String: Any]] = []
        let aspects = Dictionary(uniqueKeysWithValues: fixture.sources.map { ($0.clipIndex, $0.aspect) })
        for (slotValue, clip) in zip(fixture.slots, clips) {
            let slot = try XCTUnwrap(slotValue.objectValue)
            let slotID = try XCTUnwrap(slot["slot_id"]?.stringValue)
            let index = Int(try XCTUnwrap(slot["clip_index"]?.numberValue))
            XCTAssertEqual(clip.id, slotID)
            XCTAssertEqual(clip.volume, muted.contains(slotID) ? 0 : 1, "\(slotID) camera sound")
            // Bars: a landscape original is letterboxed exactly like the render's `fit_transform`.
            let aspect = try XCTUnwrap(aspects[index])
            if fixture.render.landscapeFit == "fit", aspect > 1 {
                let display = MediaSize(width: 1280, height: 1280 / aspect)
                let expected = NativeEditorRenderCompiler.fitTransform(display: display, canvas: recipe.canvas, landscapeFit: "fit")
                XCTAssertNotEqual(expected, .identity)
                XCTAssertEqual(clip.transform.scale, expected.scale, accuracy: 0.002, "\(slotID) must keep the render's bars")
            } else {
                XCTAssertEqual(clip.transform, .identity, "\(slotID) fills the portrait frame")
            }
            rows.append(["slot": slotID, "volume": clip.volume, "scale": clip.transform.scale, "aspect": aspect,
                         "timeline_start": clip.timelineStart])
        }

        // Text: the rendered rows, where the render drew them.
        for text in fixture.render.text {
            let layer = try XCTUnwrap(recipe.textLayers.first { $0.id == text.id }, "\(text.id) drawn")
            XCTAssertEqual(layer.anchorY / Double(recipe.canvas.height), text.yFrac, accuracy: 0.0005, "\(text.id) y")
        }

        // The creator's copy of an unedited video is the render file itself.
        XCTAssertEqual(try session.videoDownloadRoute(deviceLocalFile: nil), .localFile(finished))

        // The composition the editor exports once the creator edits, measured as a file: silence on
        // every muted clip, the tone on Recoleta, black bars around a landscape clip, none on a portrait one.
        let exported = try await session.exportDisplayedSourcePreview()
        defer { try? FileManager.default.removeItem(at: exported.cleanupURL) }
        let windows: [(id: String, start: Double, end: Double)] = try fixture.slots.map { value in
            let slot = try XCTUnwrap(value.objectValue)
            return (try XCTUnwrap(slot["slot_id"]?.stringValue), try XCTUnwrap(slot["output_start_s"]?.numberValue),
                    try XCTUnwrap(slot["output_end_s"]?.numberValue))
        }
        let levels = try await Self.rmsLevels(of: exported.fileURL, windows: windows.filter { $0.end - $0.start > 0.4 })
        for (id, rms) in levels {
            if muted.contains(id) { XCTAssertLessThan(rms, 0.002, "\(id) must be silent in the exported file") }
            else { XCTAssertGreaterThan(rms, 0.05, "\(id) keeps its sound in the exported file") }
        }
        var bars: [[String: Any]] = []
        for (value, window) in zip(fixture.slots, windows) where window.end - window.start > 0.4 {
            let index = Int(try XCTUnwrap(value.objectValue?["clip_index"]?.numberValue))
            let (edge, centre) = try await Self.lumaEdgeAndCentre(of: exported.fileURL, at: (window.start + window.end) / 2)
            if try XCTUnwrap(aspects[index]) > 1 {
                XCTAssertLessThan(edge, 24, "\(window.id): black bar above the landscape picture")
            } else {
                XCTAssertGreaterThan(edge, 100, "\(window.id): a portrait clip fills the frame")
            }
            XCTAssertGreaterThan(centre, 100, "\(window.id): picture in the middle")
            bars.append(["slot": window.id, "top_band_luma": edge, "centre_luma": centre])
        }
        let evidence = Self.repoRoot.appendingPathComponent(".dev/kri-1032", isDirectory: true)
        try? FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
        let copy = evidence.appendingPathComponent("editor-copy.mp4")
        try? FileManager.default.removeItem(at: copy)
        try? FileManager.default.copyItem(at: exported.fileURL, to: copy)
        writeReport(["clips": rows, "muted_slots": fixture.render.mutedSlots, "export": copy.path, "export_rms": levels,
                     "export_bars": bars,
                     "text": recipe.textLayers.map { ["id": $0.id, "y_frac": $0.anchorY / Double(recipe.canvas.height)] }])

        // A saved edit makes the render stale: the copy is the editor's composition until it re-renders.
        let first = try XCTUnwrap(session.timelineClips.first)
        session.selectClip(first.id)
        session.trimSelected(edge: .trailing, to: first.end - 0.5)
        XCTAssertNil(try? session.videoDownloadRoute(deviceLocalFile: nil).isFinishedRender(finished),
                     "an unsaved edit never exports the old render")
        // As the server answers after a Save: a new generation, its phone render not made yet.
        fake.authoritativeVariant?["render_generation_id"] = .string("next")
        fake.authoritativeVariant?["render_status"] = .string("awaiting_device")
        let priorAudioStatus = try XCTUnwrap(fake.deviceRenderResponse)
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "awaiting_device", request: priorAudioStatus.request,
                                                               baseGeneration: "next")
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        let rebuilt = await waitUntil { session.canDownloadCurrentVideo }
        XCTAssertTrue(rebuilt, "the saved document's preview must settle; state \(session.sourcePreviewState)")
        XCTAssertEqual(try session.videoDownloadRoute(deviceLocalFile: nil), .sourcePreview)
    }

    // MARK: - Harness

    private func plazaSession(_ fixture: Fixture, in directory: URL, finishedRender: URL) async throws -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        var pool: [NativeEditorSourcePool.Clip] = []
        for (number, source) in fixture.sources.enumerated() {
            // The run's originals were 1080x1920 / 1920x1080; small copies keep the same shape.
            let landscape = source.aspect > 1
            let url = try await Self.makeClip(in: directory, index: source.clipIndex, width: landscape ? 128 : 72,
                                              height: landscape ? 72 : 128, seconds: source.durationS, shade: CGFloat(number + 1) / 9)
            let asset = try await AssetImportCoordinator(project: project).importAsset(from: url)
            let mediaID = "kri1032-clip-\(source.clipIndex)"
            try SourceAssetStore(project: project).bind(mediaID: mediaID, original: asset)
            let fingerprint = try XCTUnwrap(asset.fingerprint)
            pool.append(.init(clipIndex: source.clipIndex, nativeSource: .init(mediaID: mediaID, sourceURL: nil,
                original: OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount, durationS: source.durationS,
                    width: landscape ? 1920 : 1080, height: landscape ? 1080 : 1920, orientationDegrees: 0, hasAudio: true),
                localRequired: true)))
        }
        // The phone's own render of this video (any playable file: only its identity matters here).
        try FileManager.default.copyItem(at: directory.appendingPathComponent("clip-4.mp4"), to: finishedRender)
        var variant = fixture.variant
        variant["output_url"] = .string(finishedRender.absoluteString)
        variant["user_timeline"] = .object(["slots": .array(fixture.slots)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "guided_story", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: Self.generation,
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: pool, baseGeneration: Self.generation)
        // A cold device preview reads the pinned recipe's audio contract even with no bed.
        // Supply that transport response; a missing mock is an unsupported API, not an export failure.
        let audioRequest = DeviceRenderRequest(
            identity: DeviceRenderIdentity(jobID: jobID, variantID: "guided_story", recipeRevision: 1,
                                           recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(
                assets: [MediaAsset(id: "source", relativePath: "source")],
                tracks: [TimelineTrack(id: "video", kind: .video,
                                       clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])]))
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: audioRequest,
                                                               publishedGeneration: Self.generation)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        XCTAssertEqual(session.timelineClips.count, fixture.slots.count)
        XCTAssertFalse(session.hasUnsavedChanges)
        return (session, fake)
    }

    /// A bright frame (so the export's bars measure as bars) and a 440 Hz tone (so a muted clip
    /// measures as silence), 30 fps like the run's footage: the 0.167 s clip keeps its 5 frames.
    private static func makeClip(in directory: URL, index: Int, width: Int, height: Int, seconds: Double, shade: CGFloat) async throws -> URL {
        let url = directory.appendingPathComponent("clip-\(index).mp4")
        let fps: Int32 = 30, rate = 44_100
        let frames = Int((seconds * Double(fps)).rounded(.up))
        let samples = Int((Double(frames) / Double(fps) * Double(rate)).rounded(.up))
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let video = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        let audio = AVAssetWriterInput(mediaType: .audio, outputSettings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: rate,
                                                                          AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 64_000])
        video.expectsMediaDataInRealTime = false; audio.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: video, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(video); writer.add(audio)
        guard writer.startWriting() else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
        writer.startSession(atSourceTime: .zero)
        var stream = AudioStreamBasicDescription(mSampleRate: Double(rate), mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked, mBytesPerPacket: 2,
            mFramesPerPacket: 1, mBytesPerFrame: 2, mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)
        var format: CMAudioFormatDescription?
        CMAudioFormatDescriptionCreate(allocator: nil, asbd: &stream, layoutSize: 0, layout: nil, magicCookieSize: 0,
                                       magicCookie: nil, extensions: nil, formatDescriptionOut: &format)
        let audioFormat = try XCTUnwrap(format)
        var frame = 0, sample = 0
        var lastProgress = ContinuousClock.now
        while frame < frames || sample < samples {
            if ContinuousClock.now - lastProgress > .seconds(20) {
                writer.cancelWriting()
                throw NSError(domain: "KRI1032Fixture", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    "Writer stalled: video \(frame)/\(frames), audio \(sample)/\(samples)"])
            }
            let videoTime = Double(frame) / Double(fps), audioTime = Double(sample) / Double(rate)
            if frame < frames, video.isReadyForMoreMediaData, sample >= samples || videoTime <= audioTime {
                var buffer: CVPixelBuffer?
                CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
                let pixel = try XCTUnwrap(buffer)
                CVPixelBufferLockBaseAddress(pixel, [])
                let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8,
                    bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
                context.setFillColor(CGColor(red: 0.55 + shade * 0.4, green: 0.85, blue: 0.95 - shade * 0.4, alpha: 1))
                context.fill(CGRect(x: 0, y: 0, width: width, height: height))
                CVPixelBufferUnlockBaseAddress(pixel, [])
                guard adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: fps)) else {
                    throw writer.error ?? CocoaError(.fileWriteUnknown)
                }
                frame += 1
                lastProgress = .now
                if frame == frames { video.markAsFinished() }
            } else if sample < samples, audio.isReadyForMoreMediaData {
                let count = min(4_410, samples - sample)
                var pcm = [Int16](repeating: 0, count: count)
                for offset in 0..<count { pcm[offset] = Int16(12_000 * sin(2 * .pi * 440 * Double(sample + offset) / Double(rate))) }
                var block: CMBlockBuffer?
                CMBlockBufferCreateWithMemoryBlock(allocator: nil, memoryBlock: nil, blockLength: count * 2, blockAllocator: nil,
                    customBlockSource: nil, offsetToData: 0, dataLength: count * 2, flags: kCMBlockBufferAssureMemoryNowFlag, blockBufferOut: &block)
                let data = try XCTUnwrap(block)
                _ = pcm.withUnsafeBytes { CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: data, offsetIntoDestination: 0, dataLength: count * 2) }
                var buffer: CMSampleBuffer?
                CMAudioSampleBufferCreateReadyWithPacketDescriptions(allocator: nil, dataBuffer: data, formatDescription: audioFormat,
                    sampleCount: count, presentationTimeStamp: CMTime(value: CMTimeValue(sample), timescale: CMTimeScale(rate)),
                    packetDescriptions: nil, sampleBufferOut: &buffer)
                guard audio.append(try XCTUnwrap(buffer)) else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
                sample += count
                lastProgress = .now
                if sample == samples { audio.markAsFinished() }
            } else {
                try await Task.sleep(for: .milliseconds(2))
            }
        }
        await writer.finishWriting()
        guard writer.status == .completed else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
        return url
    }

    /// RMS of the file's mixed-down audio inside each window (0.1 s trimmed off both ends, past the cut).
    private static func rmsLevels(of url: URL, windows: [(id: String, start: Double, end: Double)]) async throws -> [String: Double] {
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        let track = try XCTUnwrap(tracks.first, "the export has an audio track")
        let reader = try AVAssetReader(asset: asset)
        let rate = 44_100.0
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: rate,
            AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
        reader.add(output)
        guard reader.startReading() else { throw reader.error ?? CocoaError(.fileReadUnknown) }
        var sums = [String: (Double, Int)]()
        while let buffer = output.copyNextSampleBuffer() {
            let first = CMSampleBufferGetPresentationTimeStamp(buffer).seconds
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            var length = 0
            var pointer: UnsafeMutablePointer<CChar>?
            CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &pointer)
            guard let pointer else { continue }
            let count = length / MemoryLayout<Float>.size
            pointer.withMemoryRebound(to: Float.self, capacity: count) { samples in
                for index in 0..<count {
                    let time = first + Double(index) / rate
                    guard let window = windows.first(where: { time >= $0.start + 0.1 && time <= $0.end - 0.1 }) else { continue }
                    let value = Double(samples[index])
                    let (sum, n) = sums[window.id] ?? (0, 0)
                    sums[window.id] = (sum + value * value, n + 1)
                }
            }
        }
        return sums.mapValues { $0.1 > 0 ? ($0.0 / Double($0.1)).squareRoot() : 0 }
    }

    /// Mean luma (0-255) of the top 15 % of the frame and of its middle band at `seconds`.
    private static func lumaEdgeAndCentre(of url: URL, at seconds: Double) async throws -> (Double, Double) {
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let (image, _) = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600))
        let width = 90, height = 160
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        func luma(rows: Range<Int>) -> Double {
            var total = 0.0
            for row in rows { for column in 0..<width {
                let offset = (row * width + column) * 4
                total += 0.299 * Double(pixels[offset]) + 0.587 * Double(pixels[offset + 1]) + 0.114 * Double(pixels[offset + 2])
            } }
            return total / Double(rows.count * width)
        }
        // Row 0 of the bitmap is the top of the frame.
        return (luma(rows: 0..<height * 15 / 100), luma(rows: height * 45 / 100..<height * 55 / 100))
    }

    private func waitUntil(timeout: Duration = .seconds(60), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(25))
        }
        return condition()
    }

    private func writeReport(_ fields: [String: Any]) {
        let directory = Self.repoRoot.appendingPathComponent(".dev/kri-1032", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var report = fields
        report["recorded_at"] = ISO8601DateFormatter().string(from: Date())
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: directory.appendingPathComponent("report.json"), options: .atomic)
        }
    }
}

private extension NativeEditorVideoDownloadRoute {
    /// Non-nil only when this route hands over `file` (the finished render).
    func isFinishedRender(_ file: URL) -> Bool? {
        if case let .localFile(url) = self, url == file { return true }
        return nil
    }
}
