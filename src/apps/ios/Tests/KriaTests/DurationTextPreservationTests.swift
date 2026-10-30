import AVFoundation
import UIKit
import Vision
import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor final class DurationTextPreservationTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let duration: Double
        let before: [[String: JSONValue]]
        let payload: [String: JSONValue]
    }
    private var evidence: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent(".audit/native-duration")
    }
    func testTwentySecondDurationPreservesEveryRenderedWord() async throws { try await verify("duration20") }
    func testTwelveSecondDurationPreservesEveryRenderedWord() async throws { try await verify("duration12") }
    func testExplicitRetimePreservesEveryRenderedWord() async throws { try await verify("retime12") }
    func testUnevenPacingAndGapsSurviveNaturalLanguageRetime() async throws { try await verify("retimeUneven20") }
    func testCanonicalPacingReturnsAfterCompressionAndExpansion() async throws { try await verify("retimeAfterResize20") }

    func testOverlappingSequenceFailsTheSameConcurrencyAndContentOracle() async throws {
        let fixture = try fixture("duration12")
        var payload = fixture.payload
        payload["text_elements"] = .array(fixture.before.map { row in
            var row = row
            row["start_s"] = .number(0); row["end_s"] = .number(10.8)
            return .object(row)
        })
        let program = try compile(payload)
        XCTAssertEqual(maxConcurrency(program.recipe), 27)
        XCTAssertNotEqual(maxConcurrency(program.recipe), 1)
        let live = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let image = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: 0.2)
        try save(image, name: "overlap-negative-control")
        XCTAssertGreaterThan(try inkPixels(image), 40, "The negative control must render visible overlapping glyphs")
        XCTAssertNotEqual(try recognized(image), "apple", "Stacked text must fail the one-word content oracle")
    }
    private func fixture(_ name: String) throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/DurationTextPreservation.json")
        return try XCTUnwrap(JSONDecoder().decode([Fixture].self, from: Data(contentsOf: url)).first { $0.name == name })
    }
    private func compile(_ payload: [String: JSONValue]) throws -> NativeEditorRenderProgram {
        try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
        let sourceURL = evidence.appendingPathComponent("synthetic-black.png")
        let image = UIGraphicsImageRenderer(size: CGSize(width: 108, height: 192)).image { context in
            UIColor.black.setFill(); context.fill(CGRect(x: 0, y: 0, width: 108, height: 192))
        }
        try XCTUnwrap(image.pngData()).write(to: sourceURL)
        let snapshot = DraftSnapshot(draftID: "synthetic-duration", itemID: "synthetic", variantKey: "guided_story",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: ["editor_payload": .object(["sections": .object(payload)])], canUndo: false, createdAt: .now)
        let loaded = NativeEditorSession(draft: snapshot.editorDraft(projectID: UUID()))
        let encoded = try JSONEncoder().encode(loaded.document.encodeSnapshot())
        let decoded = try JSONDecoder().decode([String: JSONValue].self, from: encoded)
        let reopened = DraftSnapshot(draftID: "synthetic-reopen", itemID: "synthetic", variantKey: "guided_story",
            draftRevision: 1, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: decoded, canUndo: false, createdAt: .now)
        let session = NativeEditorSession(draft: reopened.editorDraft(projectID: UUID()))
        XCTAssertEqual(session.document.textElements, loaded.document.textElements,
            "Local JSON serialization and reopening must retain IDs, wording, style, timing, and source metadata")
        for (original, restored) in zip(loaded.document.textElements, session.document.textElements) {
            XCTAssertEqual(restored.raw["source_params"]?.objectValue?["sequence_timing"],
                original.raw["source_params"]?.objectValue?["sequence_timing"])
        }
        XCTAssertEqual(session.document.textElements.count, 27)
        var sources: [Int: ResolvedEditorSource] = [:]
        for slot in session.document.clips {
            sources[slot.clipIndex] = ResolvedEditorSource(clipIndex: slot.clipIndex, mediaID: "synthetic-\(slot.clipIndex)",
                asset: MediaAsset(id: "synthetic-\(slot.clipIndex)", relativePath: sourceURL.lastPathComponent,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: sourceURL)), url: sourceURL)
        }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems, sources: sources)
    }
    private func verify(_ name: String) async throws {
        let fixture = try fixture(name)
        let document = EditorDocument(snapshot: fixture.payload)
        XCTAssertEqual(document.textElements.map(\.id), (1...27).map { "opening::sequence-\($0)" })
        XCTAssertEqual(document.textElements.map(\.text), "apple beach candle dawn eagle forest garden harbor island jungle kettle lemon meadow nectar ocean paper quartz river sunset tulip velvet willow yellow zebra amber cloud silver".split(separator: " ").map(String.init))
        for (before, after) in zip(fixture.before, document.textElements) {
            var expected = before; var actual = after.raw
            if name.hasPrefix("retime") {
                expected.removeValue(forKey: "start_s"); expected.removeValue(forKey: "end_s")
                actual.removeValue(forKey: "start_s"); actual.removeValue(forKey: "end_s")
                var params = try XCTUnwrap(actual["source_params"]?.objectValue)
                XCTAssertNotNil(params.removeValue(forKey: "sequence_timing"))
                actual["source_params"] = .object(params)
            }
            XCTAssertEqual(actual, expected)
        }
        if name.hasPrefix("retime") {
            for (index, row) in document.textElements.enumerated() {
                let timing = try XCTUnwrap(row.raw["source_params"]?.objectValue?["sequence_timing"]?.objectValue)
                let before = fixture.before[index]
                let originalStart = try XCTUnwrap(before["start_s"]?.numberValue)
                let originalEnd = try XCTUnwrap(before["end_s"]?.numberValue)
                XCTAssertEqual(try XCTUnwrap(timing["duration_s"]?.numberValue), originalEnd - originalStart, accuracy: 0.000001)
                let nextStart = index + 1 < fixture.before.count
                    ? try XCTUnwrap(fixture.before[index + 1]["start_s"]?.numberValue) : originalEnd
                XCTAssertEqual(try XCTUnwrap(timing["gap_after_s"]?.numberValue), nextStart - originalEnd, accuracy: 0.000001)
            }
        }
        if name == "retimeUneven20" || name == "retimeAfterResize20" {
            let direct = try self.fixture("retimeUneven20")
            let resized = try self.fixture("retimeAfterResize20")
            XCTAssertEqual(direct.payload["text_elements"], resized.payload["text_elements"],
                "Compression followed by expansion must recover the canonical pacing and styles")
            for (left, right) in zip(document.textElements, document.textElements.dropFirst()) {
                XCTAssertGreaterThan(right.startS - left.endS, 0)
            }
        }
        let program = try compile(fixture.payload)
        XCTAssertEqual(program.recipe.textLayers.count, 27)
        XCTAssertEqual(maxConcurrency(program.recipe), 1)
        XCTAssertEqual(TimelineMath.totalDuration(of: program.recipe), fixture.duration, accuracy: 0.05)
        for (row, layer) in zip(document.textElements, program.recipe.textLayers) {
            XCTAssertEqual(layer.id, row.id)
            XCTAssertEqual(layer.start, row.startS, accuracy: 0.001)
            XCTAssertEqual(layer.end, row.endS, accuracy: 0.001)
            XCTAssertEqual(layer.runs.map(\.text).joined(), row.text)
            XCTAssertEqual(layer.runs.first?.fontSize, row.raw["size_px"]?.numberValue)
        }
        let live = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let output = evidence.appendingPathComponent("\(name).mp4")
        try? FileManager.default.removeItem(at: output)
        let snapshot = live.exportSnapshot()
        let result = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: evidence.appendingPathComponent("state-\(name)")), branding: .none)
            .export(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs, outputURL: output)
        XCTAssertEqual(result.status, .completed)
        let exported = AVURLAsset(url: output)
        let duration = try await exported.load(.duration).seconds
        XCTAssertEqual(duration, fixture.duration, accuracy: 0.05)
        var observations: [[String: Any]] = []
        for row in document.textElements {
            let time = (row.startS + row.endS) / 2
            let previewImage = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: time)
            let exportImage = try await frame(exported, at: time)
            let previewText = try recognized(previewImage); let exportText = try recognized(exportImage)
            try save(previewImage, name: "\(name)-preview-\(row.text)")
            try save(exportImage, name: "\(name)-export-\(row.text)")
            XCTAssertEqual(previewText, row.text, "preview at \(time)")
            XCTAssertEqual(exportText, row.text, "export at \(time)")
            observations.append(["id": row.id, "time": time, "expected": row.text, "preview": previewText, "export": exportText])
        }
        var gapObservations: [[String: Any]] = []
        for (index, pair) in zip(document.textElements, document.textElements.dropFirst()).enumerated() {
            let (left, right) = pair
            guard right.startS - left.endS >= 2.0 / 30 else { continue }
            let time = (left.endS + right.startS) / 2
            let previewImage = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: time)
            let exportImage = try await frame(exported, at: time)
            let previewInk = try inkPixels(previewImage); let exportInk = try inkPixels(exportImage)
            XCTAssertEqual(previewInk, 0, "Preview gap after \(left.text) must be blank")
            XCTAssertEqual(exportInk, 0, "Export gap after \(left.text) must be blank")
            try save(previewImage, name: "\(name)-preview-gap-\(index + 1)")
            try save(exportImage, name: "\(name)-export-gap-\(index + 1)")
            gapObservations.append(["after": left.id, "time": time, "preview_ink_pixels": previewInk, "export_ink_pixels": exportInk])
        }
        if name == "retimeUneven20" || name == "retimeAfterResize20" { XCTAssertEqual(gapObservations.count, 26) }
        let report: [String: Any] = ["case": name, "duration": duration, "maximum_concurrency": maxConcurrency(program.recipe),
            "samples": observations, "gaps": gapObservations, "local_serialization_reopen": true]
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            .write(to: evidence.appendingPathComponent("\(name)-report.json"))
    }
    private func maxConcurrency(_ recipe: KriaMediaEngine.EditRecipe) -> Int {
        recipe.textLayers.map { sample in
            recipe.textLayers.filter { $0.start <= sample.start && $0.end > sample.start }.count
        }.max() ?? 0
    }
    private func frame(_ asset: AVAsset, composition: AVVideoComposition? = nil, at seconds: Double) async throws -> CGImage {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 60_000), actualTime: nil)
    }
    private func recognized(_ image: CGImage) throws -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate; request.usesLanguageCorrection = false
        request.recognitionLanguages = ["en-US"]
        try VNImageRequestHandler(cgImage: image).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            .joined().lowercased().filter { !$0.isWhitespace }
    }
    private func inkPixels(_ image: CGImage) throws -> Int {
        var pixels = [UInt8](repeating: 0, count: 270 * 480 * 4)
        try pixels.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 270, height: 480,
                bitsPerComponent: 8, bytesPerRow: 270 * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(image, in: CGRect(x: 0, y: 0, width: 270, height: 480))
        }
        return stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count
    }
    private func save(_ image: CGImage, name: String) throws {
        try XCTUnwrap(UIImage(cgImage: image).pngData()).write(to: evidence.appendingPathComponent("\(name).png"))
    }
}
