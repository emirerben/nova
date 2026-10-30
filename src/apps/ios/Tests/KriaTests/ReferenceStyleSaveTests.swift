import AVFoundation
import UIKit
import Vision
import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor final class ReferenceStyleSaveTests: XCTestCase {
    private struct Fixture: Decodable {
        let name: String
        let recipe: KriaMediaEngine.EditRecipe
    }
    private var evidence: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent(".audit/native-reference-style")
    }
    func testSavedReferenceStyleAndLocalUndoRecipesPreviewAndExport() async throws {
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/ReferenceStyleSave.json")
        let fixtures = try RecipeJSON.decoder().decode([Fixture].self, from: Data(contentsOf: fixtureURL))
        XCTAssertEqual(fixtures.count, 2)
        XCTAssertEqual(fixtures.filter { $0.name.lowercased().contains("undo") }.count, 1)
        try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
        let source = evidence.appendingPathComponent("synthetic-black.mp4")
        try? FileManager.default.removeItem(at: source)
        try await writeVideo(source)
        let sourceDuration = try await AVURLAsset(url: source).load(.duration).seconds
        XCTAssertEqual(sourceDuration, 10, accuracy: 1e-9)
        let sourceFingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        let fontDirectory = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        for fixture in fixtures {
            var recipe = fixture.recipe
            try recipe.validate()
            let undo = fixture.name.lowercased().contains("undo")
            let words = ["Our", "Lisbon", "trip"]
            XCTAssertEqual(recipe.textLayers.count, 3, "Caption metadata must not produce extra layers")
            XCTAssertTrue(recipe.tracks.flatMap(\.clips).allSatisfy { $0.text == nil })
            XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 3, accuracy: 1e-9)
            var urls: [String: URL] = [:]
            var manifest: [RenderAssetReference] = []
            var fontNames: [String: String] = [:]
            for asset in try XCTUnwrap(recipe.assetManifest).assets {
                switch asset.source {
                case .original:
                    urls[asset.id] = source
                    manifest.append(RenderAssetReference(id: asset.id,
                        fingerprint: try RenderFingerprint(sourceFingerprint), source: asset.source))
                    let index = try XCTUnwrap(recipe.assets.firstIndex { $0.id == asset.id })
                    recipe.assets[index].fingerprint = sourceFingerprint
                case .library(let catalog, let catalogID, _):
                    XCTAssertEqual(catalog, .font)
                    let url = fontDirectory.appendingPathComponent(catalogID)
                    XCTAssertEqual(try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: url)), asset.fingerprint)
                    urls[asset.id] = url; fontNames[asset.id] = catalogID; manifest.append(asset)
                default:
                    XCTFail("Unexpected non-synthetic media dependency")
                    throw RecipeError.invalidTimeline
                }
            }
            recipe.assetManifest = RenderAssetManifest(assets: manifest)
            XCTAssertEqual(recipe.textLayers, fixture.recipe.textLayers)
            XCTAssertEqual(recipe.tracks, fixture.recipe.tracks)
            for (index, layer) in recipe.textLayers.enumerated() {
                XCTAssertEqual(layer.runs.map(\.text).joined(), words[index])
                XCTAssertEqual(layer.start, Double(index), accuracy: 1e-9)
                XCTAssertEqual(layer.end, Double(index + 1), accuracy: 1e-9)
                let referenceStyle = !undo || index == 1
                for run in layer.runs {
                    XCTAssertEqual(run.fontSize, referenceStyle ? 84 : 64)
                    XCTAssertTrue(try XCTUnwrap(fontNames[run.fontAssetID]).contains(referenceStyle ? "HankenGrotesk" : "Montserrat"))
                }
            }
            let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
            let output = evidence.appendingPathComponent("\(fixture.name).mp4")
            try? FileManager.default.removeItem(at: output)
            let result = try await AVFoundationLocalExporter(
                stateStore: FileExportStateStore(directory: evidence.appendingPathComponent("state-\(fixture.name)")), branding: .none)
                .export(recipe: live.exportSnapshot().recipe, assetURLs: urls, outputURL: output)
            XCTAssertEqual(result.status, .completed)
            let exported = AVURLAsset(url: output)
            let exportedDuration = try await exported.load(.duration).seconds
            XCTAssertEqual(exportedDuration, 3, accuracy: 1.0 / 30)
            var samples: [[String: Any]] = []
            for (index, word) in words.enumerated() {
                let time = Double(index) + 0.5
                let preview = try await frame(live.preview.playerItem.asset, composition: live.preview.playerItem.videoComposition, at: time)
                let rendered = try await frame(exported, at: time)
                let previewText = try recognized(preview); let exportText = try recognized(rendered)
                XCTAssertEqual(previewText, word.lowercased()); XCTAssertEqual(exportText, word.lowercased())
                try save(preview, name: "\(fixture.name)-preview-\(index)")
                try save(rendered, name: "\(fixture.name)-export-\(index)")
                samples.append(["time": time, "expected": word, "preview": previewText, "export": exportText])
            }
            let report: [String: Any] = ["case": fixture.name, "samples": samples,
                "extra_caption_layers": 0, "export_duration": exportedDuration,
                "source_binding": "Synthetic black 10-second video replaces backend fixture source fingerprint only; fonts retain API fingerprints"]
            try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                .write(to: evidence.appendingPathComponent("\(fixture.name)-report.json"))
        }
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
    private func save(_ image: CGImage, name: String) throws {
        try XCTUnwrap(UIImage(cgImage: image).pngData()).write(to: evidence.appendingPathComponent("\(name).png"))
    }
    private func writeVideo(_ url: URL) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
                kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<300 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 64,
                height: 64, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(UIColor.black.cgColor); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        writer.endSession(atSourceTime: CMTime(value: 300, timescale: 30))
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed)
    }
}
