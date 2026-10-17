#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// `KaraokeContent.singleWord`: one word on screen at a time from ONE layer per cue. It must look exactly like the
/// per-word layers the editor used to emit (one `AuthoredTextLayout.compile` layer per word), and cost a fraction.
final class SingleWordCaptionTests: XCTestCase {
    private let width = 192, height = 320
    private let words = ["Hello", "brave", "world"]
    private let starts = [0.0, 0.5, 1.0]            // layer-local
    private let layerStart = 0.25, layerEnd = 1.75   // absolute

    private struct Fixture { var directory: URL; var urls: [String: URL]; var assets: [MediaAsset]; var manifest: RenderAssetManifest; var font: URL }

    @MainActor private func fixture() async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let speaker = try await SolidVideoFixture.make(directory: directory, name: "speaker", color: CGColor(red: 0, green: 0, blue: 0.4, alpha: 1), seconds: 3, size: (width, height))
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["speaker": speaker, "font": font]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        return Fixture(directory: directory, urls: urls, assets: assets, manifest: RenderAssetManifest(assets: references), font: font)
    }

    private let yellow = TextInk(red: 1, green: 0.9, blue: 0, alpha: 1)
    private var canvas: Canvas { Canvas(width: width, height: height) }
    private func style(_ f: Fixture, color: TextInk) -> AuthoredTextLayout.Style {
        .init(fontAssetID: "font", size: 30, widthFraction: 0.9, xFraction: 0.5, yFraction: 0.8, color: color,
              stroke: .init(red: 0, green: 0, blue: 0, alpha: 1), strokeWidth: 3,
              shadows: [TextBlurLayer(color: .init(red: 0, green: 0, blue: 0, alpha: 0.5), sigma: 0, dx: 1, dy: 1)], bottomAligned: true)
    }

    /// What the editor emitted before: one plain layer per word, each on [its start, the next word's start).
    private func perWordLayers(_ f: Fixture) throws -> [PortableTextLayer] {
        try words.indices.map { i in
            try AuthoredTextLayout.compile(id: "w\(i)", text: words[i], start: layerStart + starts[i],
                end: i + 1 < words.count ? layerStart + starts[i + 1] : layerEnd, style: style(f, color: yellow), fontURL: f.font, canvas: canvas)
        }
    }
    private func singleWordLayers(_ f: Fixture) throws -> [PortableTextLayer] {
        try XCTUnwrap(try AuthoredTextLayout.compileSingleWords(id: "cue", text: words.joined(separator: " "), start: layerStart, end: layerEnd,
            starts: starts, highlight: yellow, style: style(f, color: TextInk(red: 1, green: 1, blue: 1, alpha: 1)), fontURL: f.font, canvas: canvas))
    }
    private func recipe(_ f: Fixture, text: [PortableTextLayer]) throws -> EditRecipe {
        let r = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: f.assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "speaker", sourceDuration: 3)])],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: f.manifest, textLayers: text)
        try r.validate()
        return r
    }

    private func pixels(_ image: CGImage) throws -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return bytes
    }
    /// Pixels clearly brighter than the dark-blue picture: the caption ink (fill or stroke/shadow edges excluded).
    private func inkCount(_ bytes: [UInt8]) -> Int { stride(from: 0, to: bytes.count, by: 4).filter { bytes[$0] > 150 && bytes[$0 + 1] > 120 }.count }
    private func maxDifference(_ a: [UInt8], _ b: [UInt8]) -> Int { zip(a, b).map { abs(Int($0) - Int($1)) }.max() ?? 0 }

    @MainActor private func frames(_ r: EditRecipe, _ f: Fixture, at times: [Double]) async throws -> [[UInt8]] {
        let output = f.directory.appendingPathComponent("\(UUID().uuidString).mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: f.directory.appendingPathComponent("state-\(UUID().uuidString)")), branding: .none)
            .export(recipe: r, assetURLs: f.urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var result: [[UInt8]] = []
        for time in times { result.append(try pixels(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image)) }
        return result
    }

    /// Same pixels as the old per-word layers at every moment, and exactly one word at a time.
    @MainActor func testSingleWordLayerMatchesPerWordLayersPixelForPixelAndShowsOneWordAtATime() async throws {
        let f = try await fixture(); defer { try? FileManager.default.removeItem(at: f.directory) }
        let layers = try singleWordLayers(f)
        XCTAssertEqual(layers.count, 1, "one layer per cue")
        let times = [0.1, 0.5, 1.0, 1.5, 2.5]   // before, word 0, word 1, word 2, after
        let old = try await frames(try recipe(f, text: try perWordLayers(f)), f, at: times)
        let new = try await frames(try recipe(f, text: layers), f, at: times)
        for (index, time) in times.enumerated() {
            let difference = maxDifference(old[index], new[index])
            // Two H.264 encodes of identical pictures differ by a few levels of noise; a different word differs by 100+.
            XCTAssertLessThanOrEqual(difference, 8, "frame at \(time)s differs from the per-word layers by \(difference)")
        }
        XCTAssertEqual(inkCount(new[0]), 0, "nothing before the first word")
        XCTAssertEqual(inkCount(new[4]), 0, "nothing after the layer")
        let shown = [1, 2, 3].map { inkCount(new[$0]) }
        XCTAssertTrue(shown.allSatisfy { $0 > 40 }, "a word is on screen during each word window: \(shown)")
        for pair in [(1, 2), (2, 3), (1, 3)] { XCTAssertGreaterThan(maxDifference(new[pair.0], new[pair.1]), 100, "different words at \(pair)") }
        // One word, not the line: a word's ink is a fraction of what the whole line would take.
        let line = try XCTUnwrap(try AuthoredTextLayout.compile(id: "line", text: words.joined(separator: " "), start: layerStart, end: layerEnd,
            style: style(f, color: yellow), fontURL: f.font, canvas: canvas))
        let lineFrame = try await frames(try recipe(f, text: [line]), f, at: [1.0])[0]
        XCTAssertGreaterThan(inkCount(lineFrame), shown[1] * 2 - 1)
    }

    /// The live preview (same compositor) shows the same single word and survives an in-place text update.
    @MainActor func testLivePreviewShowsTheActiveWordAndUpdatesInPlace() async throws {
        let f = try await fixture(); defer { try? FileManager.default.removeItem(at: f.directory) }
        let live = try await LivePreviewComposition(recipe: try recipe(f, text: try singleWordLayers(f)), assetURLs: f.urls)
        func ink(at time: Double) async throws -> Int {
            let item = live.preview.playerItem
            let generator = AVAssetImageGenerator(asset: item.asset)
            generator.videoComposition = item.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            return inkCount(try pixels(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image))
        }
        let word0 = try await ink(at: 0.5)
        XCTAssertGreaterThan(word0, 40)
        var edited = try recipe(f, text: try singleWordLayers(f))
        edited.textLayers = [try XCTUnwrap(try AuthoredTextLayout.compileSingleWords(id: "cue", text: "Hello brave planet", start: layerStart, end: layerEnd,
            starts: starts, highlight: yellow, style: style(f, color: yellow), fontURL: f.font, canvas: canvas)).first!]
        try live.updateText(recipe: edited)
        let after = try await ink(at: 1.5)
        XCTAssertGreaterThan(after, 20, "the edited third word shows after the in-place update")
    }

    /// Backward compatible wire: absent key = today's behaviour; the mode needs `activeOnly`, one whole word per run.
    func testWireContractDecodesValidatesAndDerivesTheCapability() throws {
        let ink = #"{"red":1,"green":1,"blue":0,"alpha":1}"#
        let legacy = try JSONDecoder().decode(KaraokeContent.self, from: Data(#"{"starts":[0,1],"highlight":\#(ink)}"#.utf8))
        XCTAssertNil(legacy.singleWord)
        let single = try JSONDecoder().decode(KaraokeContent.self, from: Data(#"{"starts":[0,1],"highlight":\#(ink),"activeOnly":true,"singleWord":true}"#.utf8))
        XCTAssertEqual(single.singleWord, true)
        XCTAssertEqual(try JSONDecoder().decode(KaraokeContent.self, from: JSONEncoder().encode(single)), single)
        let run = { (text: String) in PositionedTextRun(text: text, fontAssetID: "font", fontSize: 20, x: 10, baselineY: 20, letterSpacing: 0, shaped: false,
            fill: TextInk(red: 1, green: 1, blue: 1, alpha: 1), stroke: TextInk(red: 0, green: 0, blue: 0, alpha: 1), strokeWidth: 0, blurLayers: [], glyphs: nil, fontVariations: [:]) }
        try single.validate(runs: [run("a"), run("b")])
        XCTAssertThrowsError(try KaraokeContent(starts: [0, 1], highlight: single.highlight, singleWord: true).validate(runs: [run("a"), run("b")]), "needs activeOnly")
        XCTAssertThrowsError(try single.validate(runs: [run("a b"), run("c")]), "a run is one whole word")
        XCTAssertThrowsError(try single.validate(runs: [run("a")]), "one run per start")
        let layer = PortableTextLayer(id: "c", start: 0, end: 2, anchorX: 10, anchorY: 10, rotationDegrees: 0, runs: [run("a"), run("b")], effect: .karaokeLine, karaoke: single)
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 192, height: 320), assets: [],
            tracks: [], audio: AudioMixRecipe(originalVolume: 1), textLayers: [layer])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.singleWordText))
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.authoredText))
        recipe.textLayers = [PortableTextLayer(id: "c", start: 0, end: 2, anchorX: 10, anchorY: 10, rotationDegrees: 0, runs: [run("a"), run("b")], effect: .karaokeLine,
            karaoke: legacy)]
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.singleWordText))
    }

    /// 320 words as 40 cues (a long talk is hundreds of words): one layer per cue, built quickly, and the store (which lays out every layer up front,
    /// on the main actor for an in-place update) costs a fraction of one layer per word.
    func testLongTalkIsOneLayerPerCueAndTheStoreInitIsMuchCheaper() throws {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1)
        let style = AuthoredTextLayout.Style(fontAssetID: "font", size: 30, color: white)
        let cueCount = 40, perCue = 8
        var single: [PortableTextLayer] = [], perWord: [PortableTextLayer] = []
        let built = Date()
        for cue in 0..<cueCount {
            let text = (0..<perCue).map { "w\(cue)x\($0)" }
            let starts = (0..<perCue).map { Double($0) * 0.25 }
            single += try XCTUnwrap(try AuthoredTextLayout.compileSingleWords(id: "c\(cue)", text: text.joined(separator: " "), start: Double(cue) * 2,
                end: Double(cue) * 2 + 2, starts: starts, highlight: white, style: style, fontURL: font, canvas: Canvas(width: 1080, height: 1920)))
            for (i, word) in text.enumerated() {
                perWord.append(try AuthoredTextLayout.compile(id: "c\(cue)w\(i)", text: word, start: Double(cue) * 2 + starts[i], end: Double(cue) * 2 + starts[i] + 0.25,
                    style: style, fontURL: font, canvas: Canvas(width: 1080, height: 1920)))
            }
        }
        XCTAssertLessThan(Date().timeIntervalSince(built), 30, "building 320 words of captions is bounded")
        XCTAssertEqual(single.count, cueCount)
        XCTAssertEqual(perWord.count, cueCount * perCue)
        let size = CGSize(width: 1080, height: 1920)
        let t1 = Date(); _ = try NativeTextLayerStore(layers: single, assetURLs: ["font": font], canvas: size); let singleCost = Date().timeIntervalSince(t1)
        let t2 = Date(); _ = try NativeTextLayerStore(layers: perWord, assetURLs: ["font": font], canvas: size); let perWordCost = Date().timeIntervalSince(t2)
        XCTAssertLessThan(singleCost, perWordCost * 0.6, "store init: one layer per cue \(singleCost)s vs one per word \(perWordCost)s")
    }
}
#endif
