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
        let session = NativeEditorSession(draft: snapshot.editorDraft(projectID: UUID()))
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
            if name == "retime12" {
                expected.removeValue(forKey: "start_s"); expected.removeValue(forKey: "end_s")
                actual.removeValue(forKey: "start_s"); actual.removeValue(forKey: "end_s")
            }
            XCTAssertEqual(actual, expected)
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
        let report: [String: Any] = ["case": name, "duration": duration, "maximum_concurrency": maxConcurrency(program.recipe), "samples": observations]
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
