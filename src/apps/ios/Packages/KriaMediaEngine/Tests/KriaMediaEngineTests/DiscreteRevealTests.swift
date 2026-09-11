import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
#endif

final class DiscreteRevealTests: XCTestCase {
    private struct Case: Decodable { let layer: PortableTextLayer; let samples: [Sample] }
    private struct Sample: Decodable { let time: Double; let runs: [Run] }
    private struct Run: Decodable { let text: String; let x: Double; let baselineY: Double }
    private func cases() throws -> [Case] {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_reveal_layout.json").standardizedFileURL
        return try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
    }
    func testVisibleGlyphRunsAndCursorMatchCloudDrawingPositions() throws {
        let cases = try cases()
        XCTAssertEqual(cases.count, 12)
        for test in cases {
            let layer = test.layer
            let content = try XCTUnwrap(layer.discreteReveal)
            try content.validate(runs: layer.runs)
            for expected in test.samples {
                let state = try TextRevealTiming.sample(effect: PortableTextEffect(rawValue: layer.effect.rawValue)!, text: content.text,
                                                       localTime: expected.time, start: layer.start, schedule: content.schedule, motion: layer.motion)
                let runs = content.visibleRuns(layer.runs, sample: state)
                XCTAssertEqual(runs.count, expected.runs.count)
                for (actual, reference) in zip(runs, expected.runs) {
                    XCTAssertEqual(actual.text, reference.text)
                    XCTAssertEqual(actual.x, reference.x, accuracy: 1e-5)
                    XCTAssertEqual(actual.baselineY, reference.baselineY, accuracy: 1e-5)
                    if !actual.shaped { XCTAssertEqual(actual.glyphs?.count, actual.text.unicodeScalars.count) }
                }
            }
        }
    }

    #if canImport(AVFoundation)
    @MainActor func testRevealUsesCompositionTimeInPreviewAndExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let photo = directory.appendingPathComponent("black.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 300, height: 200)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["photo": photo, "font-Inter-Bold.ttf": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "photo" ? .original(mediaID: "photo") : .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: asset.fingerprint!.hex))
        }
        let exportCases = try cases().filter({ $0.layer.runs[0].x == 150 && !$0.layer.runs[0].shaped })
        XCTAssertEqual(exportCases.count, 2)
        for test in exportCases {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 300, height: 200), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 7)])],
                assetManifest: RenderAssetManifest(assets: references), textLayers: [test.layer])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("\(test.layer.effect.rawValue).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
            reference.videoComposition = preview.playerItem.videoComposition
            let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            for generator in [reference, exported] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                var counts: [Int] = []
                for time in [1.0, 2.0, 5.5, 6.5] {
                    let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    var pixels = [UInt8](repeating: 0, count: 300 * 200 * 4)
                    CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 1200, bounds: CGRect(x: 0, y: 0, width: 300, height: 200),
                                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    counts.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count)
                }
                XCTAssertEqual(counts[0], 0); XCTAssertEqual(counts[3], 0)
                XCTAssertGreaterThan(counts[1], 0); XCTAssertLessThan(counts[1], counts[2])
            }
        }
    }

    func testPartialRotatedTextPaintsAndSettlesWithoutMovingItsBitmap() throws {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let fonts = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts")
        for test in try cases() {
            let layer = test.layer
            let urls = Dictionary(uniqueKeysWithValues: Set(layer.runs.map(\.fontAssetID)).map {
                ($0, fonts.appendingPathComponent(String($0.dropFirst(5))))
            })
            let painted = try RecipeTextLayer.make(layer, assetURLs: urls, canvas: CGSize(width: 300, height: 200))
            let painter = try XCTUnwrap(painted.discreteReveal)
            let partial = try painter.image(localTime: 0, settled: painted.image)
            let settled = try painter.image(localTime: 4, settled: painted.image)
            XCTAssertEqual(partial.extent, painted.image.extent)
            XCTAssertEqual(settled.extent, painted.image.extent)
            func pixels(_ image: CIImage) -> Int {
                var data = [UInt8](repeating: 0, count: Int(image.extent.width * image.extent.height) * 4)
                CIContext().render(image, toBitmap: &data, rowBytes: Int(image.extent.width) * 4,
                                   bounds: image.extent, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                return stride(from: 3, to: data.count, by: 4).filter { data[$0] > 200 }.count
            }
            XCTAssertGreaterThan(pixels(partial), 0)
            XCTAssertLessThan(pixels(partial), pixels(settled))
            XCTAssertThrowsError(try RecipeTextLayer.make(layer, assetURLs: urls, canvas: CGSize(width: 300, height: 200), maxBitmapBytes: 100))
        }
    }
    #endif
}
