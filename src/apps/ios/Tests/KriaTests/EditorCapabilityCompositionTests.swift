import AVFoundation
import UIKit
import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor final class EditorCapabilityCompositionTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let beforeTextElements: [[String: JSONValue]]
        let payload: [String: JSONValue]
        let expectedSourceOrder: [Int]
        let targetS: Double
        let catalogDigest: String
    }
    private struct PixelEvidence {
        let source: Int
        let redBounds: CGRect?
        let redPixels: Int
    }
    private var root: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
    }
    private var evidence: URL { root.appendingPathComponent(".dev/native-capability-composition") }

    func testTwelveSecondCompositionSaveReopenPreviewAndExport() async throws { try await verify("saved-fit12") }
    func testTwentySecondCompositionSaveReopenPreviewAndExport() async throws { try await verify("saved-fit20") }
    func testTwelveSecondSplitCompositionSaveReopenPreviewAndExport() async throws { try await verify("saved-split-fit12") }
    func testTwentySecondSplitCompositionSaveReopenPreviewAndExport() async throws { try await verify("saved-split-fit20") }

    func testWrongSourceAndSimultaneousTextControlsAreRejected() async throws {
        try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
        let fixture = try fixture("saved-fit12")
        let draft = stored(fixture.payload, job: UUID(), generation: "g1")
        let session = NativeEditorSession(draft: draft.editorDraft(projectID: UUID()))
        let program = try compile(session)
        let firstIndex = fixture.expectedSourceOrder[0], secondIndex = fixture.expectedSourceOrder[1]
        var wrongURLs = program.assetURLs
        let firstKey = "source-\(firstIndex)", secondKey = "source-\(secondIndex)"
        wrongURLs[firstKey] = program.assetURLs[secondKey]
        wrongURLs[secondKey] = program.assetURLs[firstKey]
        let wrongPreview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: wrongURLs)
        let firstClip = try XCTUnwrap(session.timelineClips.first)
        let wrongFrame = try await frame(wrongPreview.preview.playerItem.asset,
            composition: wrongPreview.preview.playerItem.videoComposition, at: (firstClip.start + firstClip.end) / 2)
        let wrongSource = try inspect(wrongFrame).source
        XCTAssertEqual(wrongSource, secondIndex)
        XCTAssertNotEqual(wrongSource, firstIndex, "The pixel oracle must reject wrong footage despite identical recipe count and duration")
        try save(wrongFrame, "wrong-source-negative-control")
        var colliding = fixture.payload
        colliding["text_elements"] = .array(try XCTUnwrap(fixture.payload["text_elements"]?.arrayValue).map { value in
            var row = value.objectValue!
            row["start_s"] = .number(0)
            row["end_s"] = .number(1)
            return .object(row)
        })
        let collidingDraft = stored(colliding, job: UUID(), generation: "g1")
        let collisionProgram = try compile(NativeEditorSession(draft: collidingDraft.editorDraft(projectID: UUID())))
        XCTAssertEqual(maxConcurrency(collisionProgram.recipe), 27)
        XCTAssertNotEqual(maxConcurrency(collisionProgram.recipe), 1,
            "Intended repeated spatial placement must not allow simultaneous word collisions")
        let collisionPreview = try await LivePreviewComposition(recipe: collisionProgram.recipe, assetURLs: collisionProgram.assetURLs)
        let collisionFrame = try await frame(collisionPreview.preview.playerItem.asset,
            composition: collisionPreview.preview.playerItem.videoComposition, at: 0.2)
        XCTAssertGreaterThan(try inspect(collisionFrame).redPixels, 30)
        try save(collisionFrame, "temporal-overlap-negative-control")
    }

    private func fixture(_ name: String) throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/EditorCapabilityComposition.json")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try XCTUnwrap(decoder.decode([Fixture].self, from: Data(contentsOf: url)).first { $0.name == name })
    }

    private func stored(_ sections: [String: JSONValue], job: UUID, generation: String) -> DraftSnapshot {
        DraftSnapshot(draftID: "composition", itemID: "item", variantKey: "guided_story", draftRevision: 1,
            snapshotHash: "composition", etag: "composition", baseJobID: job.uuidString,
            baseGenerationID: generation, snapshot: ["editor_payload": .object([
                "base_generation": .string(generation), "sections": .object(sections)])], canUndo: false, createdAt: .now)
    }

    private func savedSession(_ fixture: Fixture) async throws -> NativeEditorSession {
        let job = UUID(), thread = UUID()
        var variant: [String: JSONValue] = ["variant_id": .string("guided_story"),
            "resolved_archetype": .string("guided_story"), "render_generation_id": .string("g1"),
            "render_status": .string("ready"), "render_destination": .string("device"),
            "duration_s": .number(fixture.targetS), "editor_capabilities": .object([
                "text_elements": .bool(true), "timeline": .bool(true)]),
            "text_elements": fixture.payload["text_elements"]!,
            "user_timeline": .object(["slots": fixture.payload["timeline_slots"]!])]
        let transport = EditorCommitSpy(draftSnapshot: stored(fixture.payload, job: job, generation: "g1"), authoritativeVariant: variant)
        let session = NativeEditorSession()
        await session.load(api: transport, threadID: thread)
        let staged = EditorDocument(snapshot: fixture.payload)
        XCTAssertEqual(session.document.textElements, staged.textElements)
        XCTAssertEqual(session.document.clips.map(\.clipIndex), fixture.expectedSourceOrder)
        let first = try XCTUnwrap(session.document.textElements.first)
        let editedSize = try XCTUnwrap(first.raw["size_px"]?.numberValue) + 1
        session.setTextSize(id: first.id, sizePX: editedSize)
        XCTAssertTrue(session.hasUnsavedChanges)
        let edited = session.document.textElements
        transport.commitResponse = EditorCommitResponse(ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "composition-saved", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(transport.commitCount, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
        let sent = try XCTUnwrap(transport.lastRequest?.textElements)
        XCTAssertEqual(sent.count, 27)
        XCTAssertNil(transport.lastRequest?.timelineSlots)
        XCTAssertNil(transport.lastRequest?.mix)
        XCTAssertEqual(sent.map { $0.objectValue }, edited.map(\.raw))
        variant["text_elements"] = .array(sent)
        variant["render_generation_id"] = .string("g2")
        transport.authoritativeVariant = variant
        var saved = fixture.payload
        saved["text_elements"] = .array(sent)
        transport.draftSnapshot = stored(saved, job: job, generation: "g2")
        let reopened = NativeEditorSession()
        await reopened.load(api: transport, threadID: thread)
        XCTAssertEqual(reopened.document.textElements, edited)
        XCTAssertEqual(reopened.document.clips.map { slot in
            slot.raw.filter { $0.key != "asset_id" }
        }, session.document.clips.map { slot in slot.raw.filter { $0.key != "asset_id" } },
            "Reopen must preserve every timeline field except the local asset cache UUID")
        XCTAssertFalse(reopened.hasUnsavedChanges)
        try JSONEncoder().encode(transport.lastRequest).write(to: evidence.appendingPathComponent("\(fixture.name)-captured-save.json"))
        return reopened
    }

    private func sourceColor(_ index: Int) -> UIColor {
        UIColor(red: CGFloat(20 + index * 12) / 255, green: CGFloat(50 + (index % 3) * 60) / 255,
                blue: CGFloat(210 - index * 16) / 255, alpha: 1)
    }

    private func compile(_ session: NativeEditorSession) throws -> NativeEditorRenderProgram {
        var sources: [Int: ResolvedEditorSource] = [:]
        for slot in session.document.clips {
            let index = slot.clipIndex
            let url = evidence.appendingPathComponent("source-\(index).png")
            let image = UIGraphicsImageRenderer(size: CGSize(width: 270, height: 480)).image { context in
                sourceColor(index).setFill()
                context.fill(CGRect(x: 0, y: 0, width: 270, height: 480))
                ("SOURCE \(index)" as NSString).draw(at: CGPoint(x: 12, y: 420), withAttributes: [
                    .font: UIFont.monospacedSystemFont(ofSize: 22, weight: .bold), .foregroundColor: UIColor.white])
            }
            try XCTUnwrap(image.pngData()).write(to: url)
            sources[index] = ResolvedEditorSource(clipIndex: index, mediaID: "synthetic-\(index)",
                asset: MediaAsset(id: "synthetic-\(index)", relativePath: url.lastPathComponent,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: url)), url: url)
        }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems, sources: sources)
    }

    private func verify(_ name: String) async throws {
        try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
        let fixture = try fixture(name)
        let staged = EditorDocument(snapshot: fixture.payload)
        XCTAssertEqual(staged.textElements.count, 27)
        if fixture.beforeTextElements.count == 1 {
            let original = try XCTUnwrap(fixture.beforeTextElements.first)
            let originalID = try XCTUnwrap(original["id"]?.stringValue)
            let originalText = try XCTUnwrap(original["text"]?.stringValue)
            let words = originalText.split(whereSeparator: \.isWhitespace).map(String.init)
            XCTAssertEqual(words.count, 27)
            XCTAssertEqual(staged.textElements.map(\.text), words)
            XCTAssertEqual(staged.textElements.map(\.id), words.indices.map { "\(originalID)::sequence-\($0 + 1)" })
            for row in staged.textElements {
                XCTAssertEqual(row.raw["source_params"]?.objectValue?["sequence_source_id"], .string(originalID))
            }
        } else {
            XCTAssertEqual(staged.textElements.map(\.id), fixture.beforeTextElements.compactMap { $0["id"]?.stringValue })
            XCTAssertEqual(staged.textElements.map(\.text), fixture.beforeTextElements.compactMap { $0["text"]?.stringValue })
        }
        XCTAssertEqual(Set(fixture.expectedSourceOrder), Set(0..<fixture.expectedSourceOrder.count))
        for row in staged.textElements {
            XCTAssertEqual(row.raw["font_family"], .string("DM Sans"))
            XCTAssertEqual(row.raw["color"], .string("#FF0000"))
            XCTAssertEqual(row.raw["alignment"], .string("left"))
            XCTAssertEqual(row.raw["x_frac"], .number(0.3))
            XCTAssertEqual(row.raw["y_frac"], .number(0.35))
            XCTAssertGreaterThanOrEqual(row.endS - row.startS, 0.25)
            XCTAssertGreaterThanOrEqual(row.startS, 0)
            XCTAssertLessThanOrEqual(row.endS, fixture.targetS + 0.000001)
        }
        let session = try await savedSession(fixture)
        let program = try compile(session)
        XCTAssertEqual(program.recipe.textLayers.count, 27)
        XCTAssertEqual(maxConcurrency(program.recipe), 1)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), fixture.targetS, accuracy: 0.05)
        for (row, layer) in zip(session.document.textElements, program.recipe.textLayers) {
            XCTAssertEqual(layer.id, row.id)
            XCTAssertEqual(layer.runs.map(\.text).joined(), row.text)
            XCTAssertEqual(layer.start, row.startS, accuracy: 0.0005)
            XCTAssertEqual(layer.end, row.endS, accuracy: 0.0005)
            XCTAssertEqual(layer.anchorX, 324, accuracy: 0.01)
            XCTAssertEqual(layer.anchorY, 672, accuracy: 0.01)
            XCTAssertEqual(layer.runs.first?.fontSize, row.raw["size_px"]?.numberValue)
            for run in layer.runs {
                XCTAssertEqual(program.assetURLs[run.fontAssetID]?.lastPathComponent, "DMSans-Bold.ttf")
            }
        }
        let live = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let output = evidence.appendingPathComponent("\(name).mp4")
        try? FileManager.default.removeItem(at: output)
        let snapshot = live.exportSnapshot()
        let result = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: evidence.appendingPathComponent("state-\(name)")), branding: .none)
            .export(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs, outputURL: output)
        XCTAssertEqual(result.status, .completed)
        let exported = AVURLAsset(url: output)
        let duration = try await exported.load(.duration).seconds
        XCTAssertEqual(duration, fixture.targetS, accuracy: 0.05)
        var samples: [[String: Any]] = []
        for row in session.document.textElements {
            let time = (row.startS + row.endS) / 2
            let preview = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: time)
            let render = try await frame(exported, at: time)
            let a = try inspect(preview), b = try inspect(render)
            XCTAssertGreaterThan(a.redPixels, 30, "Preview glyphs missing for \(row.id)")
            XCTAssertGreaterThan(b.redPixels, 30, "Export glyphs missing for \(row.id)")
            let pa = try XCTUnwrap(a.redBounds), pb = try XCTUnwrap(b.redBounds)
            for bounds in [pa, pb] {
                XCTAssertEqual(bounds.minX / 270, 0.3, accuracy: 0.025, "Left glyph edge must follow custom anchor")
                XCTAssertEqual(bounds.midY / 480, 0.35, accuracy: 0.04, "Glyphs must follow vertical anchor")
                XCTAssertLessThan(bounds.maxX, 267, "Glyphs must be readable inside canvas")
                XCTAssertGreaterThan(bounds.width, 20)
                XCTAssertGreaterThan(bounds.height, 8)
            }
            XCTAssertEqual(pa.minX, pb.minX, accuracy: 2)
            XCTAssertEqual(pa.midY, pb.midY, accuracy: 2)
            try save(preview, "\(name)-preview-\(row.text)")
            try save(render, "\(name)-export-\(row.text)")
            samples.append(["id": row.id, "text": row.text, "time": time, "preview_red_pixels": a.redPixels,
                "export_red_pixels": b.redPixels, "preview_bounds": NSCoder.string(for: pa), "export_bounds": NSCoder.string(for: pb)])
        }
        var clipSamples: [[String: Any]] = []
        var observed: [Int] = []
        for (index, clip) in session.timelineClips.enumerated() {
            let time = (clip.start + clip.end) / 2
            let preview = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: time)
            let render = try await frame(exported, at: time)
            let a = try inspect(preview), b = try inspect(render)
            XCTAssertEqual(a.source, fixture.expectedSourceOrder[index])
            XCTAssertEqual(b.source, fixture.expectedSourceOrder[index])
            observed.append(b.source)
            try save(render, "\(name)-clip-\(index)-source-\(b.source)")
            clipSamples.append(["slot": index, "time": time, "expected_source": fixture.expectedSourceOrder[index], "preview_source": a.source, "export_source": b.source])
        }
        XCTAssertEqual(observed, fixture.expectedSourceOrder)
        let report: [String: Any] = ["case": name, "duration": duration, "catalog_digest": fixture.catalogDigest,
            "text_layers": samples, "clip_samples": clipSamples, "expected_source_order": fixture.expectedSourceOrder,
            "original_text_row_count": fixture.beforeTextElements.count,
            "original_text": fixture.beforeTextElements.compactMap { $0["text"]?.stringValue }.joined(separator: " "),
            "maximum_temporal_concurrency": maxConcurrency(program.recipe), "manual_edit": "first word size_px + 1",
            "save_transport": "captured HTTP transport, real native Save construction and acknowledged reopen",
            "source_provenance": "unique runtime synthetic colored numbered PNGs", "font_evidence": "DM Sans metadata and bundled compiler resolution; no visual reference recognition"]
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: evidence.appendingPathComponent("\(name)-report.json"))
    }

    private func maxConcurrency(_ recipe: KriaMediaEngine.EditRecipe) -> Int {
        recipe.textLayers.map { sample in recipe.textLayers.filter { $0.start <= sample.start && $0.end > sample.start }.count }.max() ?? 0
    }
    private func frame(_ asset: AVAsset, composition: AVVideoComposition? = nil, at seconds: Double) async throws -> CGImage {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        return try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 60_000), actualTime: nil)
    }
    private func inspect(_ image: CGImage) throws -> PixelEvidence {
        var pixels = [UInt8](repeating: 0, count: 270 * 480 * 4)
        try pixels.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 270, height: 480, bitsPerComponent: 8,
                bytesPerRow: 1080, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(image, in: CGRect(x: 0, y: 0, width: 270, height: 480))
        }
        let corner = (20 * 270 + 20) * 4
        let source = (0..<10).min { left, right in
            func distance(_ index: Int) -> Int {
                abs(Int(pixels[corner]) - (20 + index * 12)) + abs(Int(pixels[corner + 1]) - (50 + (index % 3) * 60)) + abs(Int(pixels[corner + 2]) - (210 - index * 16))
            }
            return distance(left) < distance(right)
        }!
        var minX = 270, minY = 480, maxX = -1, maxY = -1, count = 0
        for y in 0..<480 { for x in 0..<270 {
            let p = (y * 270 + x) * 4
            if pixels[p] > 185 && pixels[p + 1] < 65 && pixels[p + 2] < 65 {
                minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y); count += 1
            }
        } }
        return PixelEvidence(source: source, redBounds: count == 0 ? nil : CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1), redPixels: count)
    }
    private func save(_ image: CGImage, _ name: String) throws {
        try XCTUnwrap(UIImage(cgImage: image).pngData()).write(to: evidence.appendingPathComponent("\(name).png"))
    }
}
