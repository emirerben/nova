#if canImport(AVFoundation)
import XCTest
import CoreImage
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

final class DesignedTextAnimationTests: XCTestCase {
    private let names = ["silkReveal", "elasticCondensed", "focusPull", "wordRelay", "ribbonWipe", "echoStack"]
    private let canvas = Canvas(width: 360, height: 640)
    private var font: URL {
        let root = String(#filePath.prefix(upTo: #filePath.range(of: "/src/apps/ios/")!.lowerBound))
        return URL(fileURLWithPath: root).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
    }
    private func pixels(_ image: CIImage) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: 360 * 640 * 4)
        CIContext().render(image, toBitmap: &bytes, rowBytes: 360 * 4, bounds: CGRect(x: 0, y: 0, width: 360, height: 640),
            format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return bytes
    }
    func testEveryDesignedAnimationPaintsCreatorTextAndReplaysDeterministically() throws {
        for name in names {
            let phases = try JSONDecoder().decode(TextAnimationPhases.self, from: Data("{\"entrance\":\"\(name)\"}".utf8))
            for content in ["Somewhere\nworth getting lost.", "İstanbul güzel", "ONE", "I 🤍 NYC 👨‍👩‍👧‍👦", "A\n\nB"] {
                var style = AuthoredTextLayout.Style(fontAssetID: "font", size: 36, color: TextInk(red: 1, green: 1, blue: 1, alpha: 1))
                if content == "İstanbul güzel" {
                    style.bend = 65; style.rotation = 15; style.strokeWidth = 2
                    style.shadows = [TextBlurLayer(color: .init(red: 0.2, green: 0.4, blue: 0.6, alpha: 0.5), sigma: 3, dx: 2, dy: 2)]
                }
                if content == "ONE" { style.size = 96; style.background = .init(red: 0.2, green: 0.3, blue: 0.1, alpha: 1) }
                let layer = try AuthoredTextLayout.compile(id: "title", text: content, start: 0, end: 3,
                    style: style, fontURL: font, canvas: canvas, phases: phases)
                let text = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: canvas.width, height: canvas.height))
                let background = CIImage(color: .black).cropped(to: CGRect(origin: .zero, size: CGSize(width: canvas.width, height: canvas.height)))
                func frame(_ time: Double) throws -> [UInt8] {
                    pixels(try RecipeVideoCompositor.paintText([text], over: background,
                        canvas: background.extent, time: time))
                }
                let entry = try frame(0.12), settled = try frame(2)
                XCTAssertNotEqual(entry, settled, "\(name) must move rather than silently render static text")
                XCTAssertEqual(try frame(0.12), entry, "\(name) must support backward seeks")
                XCTAssertTrue(stride(from: 0, to: settled.count, by: 4).contains { settled[$0] > 100 || settled[$0 + 1] > 100 || settled[$0 + 2] > 100 }, "\(name) must paint visible ink, not just a black frame with opaque alpha")
                if let output = ProcessInfo.processInfo.environment["KRIA_TEXT_MOTION_EVIDENCE"], content.contains("Somewhere") {
                    let directory = URL(fileURLWithPath: output)
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                    for time in [0.12, 0.4, 0.8, 2.0] {
                        let image = try RecipeVideoCompositor.paintText([text], over: background, canvas: background.extent, time: time)
                        try CIContext().writePNGRepresentation(of: image, to: directory.appendingPathComponent("\(name)-\(time).png"),
                            format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    }
                }
            }
        }
    }
    func testCurvedMultilineSilkSettlesWithoutChangingItsGeometry() throws {
        let phases = try JSONDecoder().decode(TextAnimationPhases.self, from: Data("{\"entrance\":\"silkReveal\"}".utf8))
        let layer = try AuthoredTextLayout.compile(id: "curved", text: "Somewhere\njoy", start: 0, end: 3,
            style: .init(fontAssetID: "font", size: 48, rotation: 15, color: .init(red: 1, green: 1, blue: 1, alpha: 1), bend: 100),
            fontURL: font, canvas: canvas, phases: phases)
        let made = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 360, height: 640))
        let background = CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 360, height: 640))
        let a = pixels(try RecipeVideoCompositor.paintText([made], over: background, canvas: background.extent, time: 1.1999))
        let b = pixels(try RecipeVideoCompositor.paintText([made], over: background, canvas: background.extent, time: 1.2001))
        let difference = zip(a, b).reduce(0) { $0 + abs(Int($1.0) - Int($1.1)) }
        XCTAssertLessThan(Double(difference) / Double(a.count), 0.01, "A line must not change its curve when the entrance ends")
    }

    func testDesignedHoldsComposeWithTypewriterEdges() throws {
        for (entrance, exit) in [("wordRelay", "typewriter"), ("ribbonWipe", "typewriter"), ("typewriter", "ribbonWipe")] {
            let phases = try JSONDecoder().decode(TextAnimationPhases.self, from: Data("{\"entrance\":\"\(entrance)\",\"exit\":\"\(exit)\"}".utf8))
            let layer = try AuthoredTextLayout.compile(id: "mixed", text: "HELLO", start: 0, end: 3,
                style: .init(fontAssetID: "font", size: 48, color: .init(red: 1, green: 1, blue: 1, alpha: 1)), fontURL: font, canvas: canvas, phases: phases)
            let made = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 360, height: 640))
            let background = CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 360, height: 640))
            func ink(_ time: Double) throws -> Int {
                let bytes = pixels(try RecipeVideoCompositor.paintText([made], over: background, canvas: background.extent, time: time))
                var sum = 0
                for index in stride(from: 0, to: bytes.count, by: 4) {
                    sum += Int(bytes[index]); sum += Int(bytes[index + 1]); sum += Int(bytes[index + 2])
                }
                return sum
            }
            if entrance == "typewriter" { XCTAssertLessThan(try ink(0.2), try ink(1.4)) }
            else { XCTAssertGreaterThan(try ink(2.2), try ink(2.8)) }
        }
    }

    @MainActor func testPreviewAndExportKeepAllSixDesignedTreatments() async throws {
        let evidenceRoot = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KRIA_TEXT_MOTION_EVIDENCE"] ?? FileManager.default.temporaryDirectory.path)
        let directory = evidenceRoot.appendingPathComponent("export-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let photo = directory.appendingPathComponent("black.png")
        let rectangle = CGRect(x: 0, y: 0, width: 360, height: 640)
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: rectangle),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["photo": photo, "font": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        let layers = try names.enumerated().map { index, name in
            let edge = try XCTUnwrap(TextAnimationPhases.Edge(rawValue: name))
            return try AuthoredTextLayout.compile(id: name, text: "Somewhere\nworth getting lost.", start: Double(index) * 3, end: Double(index + 1) * 3,
                style: .init(fontAssetID: "font", size: 36, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
                fontURL: font, canvas: canvas, phases: .init(entrance: edge))
        }
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "background", sourceAssetID: "photo", sourceDuration: 18)])],
            assetManifest: RenderAssetManifest(assets: references), textLayers: layers)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("six-text-animations.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        if ProcessInfo.processInfo.environment["KRIA_TEXT_MOTION_EVIDENCE"] != nil {
            try output.path.write(to: evidenceRoot.appendingPathComponent("latest-export.txt"), atomically: true, encoding: .utf8)
        }
        let a = AVAssetImageGenerator(asset: preview.playerItem.asset)
        a.videoComposition = preview.playerItem.videoComposition
        let b = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        for generator in [a, b] {
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
        }
        for index in names.indices {
            var exported: [[UInt8]] = []
            for local in [0.1, 0.4, 2.0] {
                let time = CMTime(seconds: Double(index) * 3 + local, preferredTimescale: 600)
                let expected = pixels(CIImage(cgImage: try await a.image(at: time).image))
                let actual = pixels(CIImage(cgImage: try await b.image(at: time).image))
                let difference = zip(expected, actual).reduce(0) { $0 + abs(Int($1.0) - Int($1.1)) }
                XCTAssertLessThan(Double(difference) / Double(actual.count), 2, names[index])
                exported.append(actual)
            }
            XCTAssertNotEqual(exported[0], exported[2], "Static output must fail the motion check for \(names[index])")
        }
    }

}
#endif
