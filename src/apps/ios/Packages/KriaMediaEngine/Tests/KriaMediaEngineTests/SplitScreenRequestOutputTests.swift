import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation) && os(macOS)
@preconcurrency import AVFoundation
import CoreImage
import CryptoKit

/// Authored offline replay only. Run preparation, the Python compiler, then consumption
/// as separate opt-in steps; retained artifacts are not device or provider evidence.
@MainActor
final class SplitScreenRequestOutputTests: XCTestCase {
    private struct Scenario: Decodable {
        let schemaVersion: Int
        let rgbTolerance: Int
        let sources: [Source]
        let cases: [ScenarioCase]
    }
    private struct Source: Decodable {
        let id: String
        let kind: String
        let durationS: Double?
        let width: Int
        let height: Int
        let fps: Int32
        let colors: [Color]
    }
    private struct Color: Decodable {
        let startFrame: Int
        let rgb: [Int]
        var cgColor: CGColor {
            CGColor(red: CGFloat(rgb[0]) / 255, green: CGFloat(rgb[1]) / 255,
                    blue: CGFloat(rgb[2]) / 255, alpha: 1)
        }
    }
    private struct ScenarioCase: Decodable {
        let id: String
        let expectedDurationS: Double
        let samples: [Sample]
    }
    private struct Sample: Decodable {
        let timeS: Double
        let xFraction: Double
        let yFromTop: Double
        let rgb: [Int]
        let controlMustFail: Bool
    }
    private struct Receipts: Decodable {
        let schemaVersion: Int
        let scenarioSha256: String
        let sources: [Receipt]
    }
    private struct Receipt: Decodable {
        let mediaId: String
        let path: String
        let kind: String
        let sha256: String
        let byteCount: Int64
        let durationS: Double?
        let width: Int
        let height: Int
        let hasAudio: Bool
    }
    private struct CompiledIndex: Decodable {
        let schemaVersion: Int
        let headSha: String
        let workingTreeDirty: Bool
        let sourceReceiptsSha256: String
        let scenarioSha256: String
        let cases: [CompiledCase]
    }
    private struct CompiledCase: Decodable {
        let id: String
        let control: Bool
        let recipePath: String
        let recipeSha256: String
        let requestPath: String
    }

    func testPrepareSourceReceipts() async throws {
        let root = try replayDirectory()
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try require(FileManager.default.contentsOfDirectory(atPath: root.path).isEmpty,
                    "Preparation requires an empty directory; never overwrite sources, receipts or compiled evidence")
        let (scenario, bytes) = try scenarioFixture()
        var receipts: [[String: Any]] = []
        for source in scenario.sources {
            let first = try XCTUnwrap(source.colors.first)
            let url: URL
            switch source.kind {
            case "video":
                url = try await SolidVideoFixture.make(directory: root, name: source.id, color: first.cgColor,
                    seconds: try XCTUnwrap(source.durationS), size: (source.width, source.height), fps: source.fps,
                    frameColor: { frame in
                        source.colors.last(where: { $0.startFrame <= frame })!.cgColor
                    })
            case "image":
                url = root.appendingPathComponent("\(source.id).png")
                try savePNG(CIImage(color: CIColor(cgColor: first.cgColor)).cropped(to:
                    CGRect(x: 0, y: 0, width: source.width, height: source.height)), to: url)
            default:
                throw failure("Unsupported authored source kind \(source.kind)")
            }
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
            let metadata = try await probe(url, kind: source.kind)
            try require(metadata.width == source.width && metadata.height == source.height, "Source dimensions differ from fixture")
            if let expected = source.durationS {
                try require(abs(try XCTUnwrap(metadata.duration) - expected) < 0.1, "Source duration differs from fixture")
            }
            var receipt: [String: Any] = ["media_id": source.id, "path": url.lastPathComponent,
                "kind": source.kind, "sha256": fingerprint.hex, "byte_count": fingerprint.byteCount,
                "width": metadata.width, "height": metadata.height, "has_audio": metadata.hasAudio]
            if let duration = metadata.duration { receipt["duration_s"] = duration }
            receipts.append(receipt)
        }
        try writeJSON(["schema_version": 1, "scenario_sha256": hash(bytes), "sources": receipts],
                      to: root.appendingPathComponent("source-receipts.json"))
    }

    func testRenderCompiledRequests() async throws {
        let root = try replayDirectory()
        let nativeHead = try cleanCheckoutRevision()
        let (scenario, scenarioBytes) = try scenarioFixture()
        let receiptBytes = try Data(contentsOf: root.appendingPathComponent("source-receipts.json"))
        let receipts = try RecipeJSON.decoder().decode(Receipts.self, from: receiptBytes)
        let index = try RecipeJSON.decoder().decode(CompiledIndex.self,
            from: Data(contentsOf: root.appendingPathComponent("compiled-index.json")))
        try require(index.schemaVersion == 1 && receipts.schemaVersion == 1, "Unsupported evidence schema")
        try require(index.scenarioSha256 == hash(scenarioBytes) && receipts.scenarioSha256 == hash(scenarioBytes), "Scenario fingerprint mismatch")
        try require(index.sourceReceiptsSha256 == hash(receiptBytes), "Receipt fingerprint mismatch")
        try require(index.headSha == nativeHead && !index.workingTreeDirty,
                    "Native consumption requires the same clean revision as compilation")
        try require(Set(receipts.sources.map(\.mediaId)) == Set(scenario.sources.map(\.id)) && receipts.sources.count == scenario.sources.count,
                    "Receipts must cover exactly the authored sources")
        try require(index.cases.count == scenario.cases.count * 2, "Expected one positive and one control per case")
        try require(Set(index.cases.map(\.id)) == Set(scenario.cases.map(\.id)), "Unexpected compiled case IDs")
        let outputRoot = root.appendingPathComponent("native-output", isDirectory: true)
        try require(!FileManager.default.fileExists(atPath: outputRoot.path), "Retained native evidence already exists; use a fresh replay directory")
        try FileManager.default.createDirectory(at: outputRoot, withIntermediateDirectories: true)
        var reports: [[String: Any]] = []
        do {
            for expected in scenario.cases {
                let entries = index.cases.filter { $0.id == expected.id }
                try require(entries.filter { !$0.control }.count == 1 && entries.filter(\.control).count == 1,
                            "Each case needs exactly one positive and one swapped control")
                var positive: EditRecipe?
                for entry in entries.sorted(by: { !$0.control && $1.control }) {
                    let bytes = try Data(contentsOf: relativeURL(entry.recipePath, root: root))
                    try require(hash(bytes) == entry.recipeSha256, "Recipe byte fingerprint mismatch")
                    let request = try relativeURL(entry.requestPath, root: root)
                    try require(FileManager.default.fileExists(atPath: request.path), "Missing authored request artifact")
                    let recipe = try RecipeJSON.decode(bytes)
                    try recipe.validate()
                    let urls = try await verifiedSources(recipe: recipe, receipts: receipts, root: root)
                    try require(abs(TimelineMath.totalDuration(of: recipe) - expected.expectedDurationS) < 0.000_001, "Recipe duration differs from authored intent")
                    if entry.control {
                        let original = try XCTUnwrap(positive)
                        let sections = recipe.splitScreenSections.sorted { $0.start < $1.start }
                        let originalSections = original.splitScreenSections.sorted { $0.start < $1.start }
                        try require(sections.count == originalSections.count, "Control changed section count")
                        for (section, originalSection) in zip(sections, originalSections) {
                            try require(section.start == originalSection.start && section.end == originalSection.end
                                && section.panelWeights == originalSection.panelWeights
                                && section.gutter == originalSection.gutter
                                && section.backgroundColor == originalSection.backgroundColor,
                                "Control changed section timing, weights or layout")
                        }
                        try require(TimelineMath.totalDuration(of: recipe) == TimelineMath.totalDuration(of: original), "Control changed total duration")
                        try require(recipe.tracks.map { $0.clips.count } == original.tracks.map { $0.clips.count }, "Control changed clip counts")
                    } else { positive = recipe }
                    let name = "\(entry.id)-\(entry.control ? "control" : "positive")"
                    let directory = outputRoot.appendingPathComponent(name, isDirectory: true)
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                    let preview = try await AVPlayerPreviewComposer(branding: .none).makePreview(recipe: recipe, assetURLs: urls)
                    let output = directory.appendingPathComponent("export.mp4")
                    _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
                        .export(recipe: recipe, assetURLs: urls, outputURL: output)
                    let live = AVAssetImageGenerator(asset: preview.playerItem.asset)
                    live.videoComposition = preview.playerItem.videoComposition
                    let exported = AVURLAsset(url: output)
                    let previewDuration = try await preview.playerItem.asset.load(.duration).seconds
                    let exportDuration = try await exported.load(.duration).seconds
                    let durationMatches = abs(previewDuration - expected.expectedDurationS) <= 1 / recipe.frameRate
                        && abs(exportDuration - expected.expectedDurationS) <= 1 / recipe.frameRate
                    XCTAssertEqual(previewDuration, expected.expectedDurationS, accuracy: 1 / recipe.frameRate)
                    XCTAssertEqual(exportDuration, expected.expectedDurationS, accuracy: 1 / recipe.frameRate)
                    var measurements: [[String: Any]] = []
                    for (mode, generator) in [("preview", live), ("export", AVAssetImageGenerator(asset: exported))] {
                        generator.appliesPreferredTrackTransform = true
                        generator.requestedTimeToleranceBefore = .zero
                        generator.requestedTimeToleranceAfter = .zero
                        for (sampleIndex, sample) in expected.samples.enumerated() {
                            let frame = try await generator.image(at: CMTime(seconds: sample.timeS, preferredTimescale: 60_000))
                            let image = CIImage(cgImage: frame.image)
                            let png = "\(mode)-\(sampleIndex).png"
                            try savePNG(image, to: directory.appendingPathComponent(png))
                            let actual = rgb(image, sample: sample)
                            let matches = zip(actual, sample.rgb).allSatisfy { abs($0 - $1) <= scenario.rgbTolerance }
                            let shouldMatch = !(entry.control && sample.controlMustFail)
                            XCTAssertEqual(matches, shouldMatch, "\(name) \(mode) sample \(sampleIndex): \(actual), expected \(sample.rgb), must match: \(shouldMatch)")
                            let timeMatches = abs(frame.actualTime.seconds - sample.timeS) <= 0.000_001
                            XCTAssertEqual(frame.actualTime.seconds, sample.timeS, accuracy: 0.000_001)
                            measurements.append(["mode": mode, "sample_index": sampleIndex, "time_s": sample.timeS,
                                "actual_time_s": frame.actualTime.seconds, "x_fraction": sample.xFraction, "y_from_top": sample.yFromTop,
                                "expected_rgb": sample.rgb, "actual_rgb": actual, "matches_oracle": matches,
                                "expected_match": shouldMatch, "passed": matches == shouldMatch && timeMatches, "frame": "\(name)/\(png)"])
                        }
                    }
                    reports.append(["id": entry.id, "control": entry.control, "recipe_sha256": entry.recipeSha256,
                        "preview_duration_s": previewDuration, "export_duration_s": exportDuration,
                        "output": "\(name)/export.mp4", "measurements": measurements,
                        "passed": durationMatches && measurements.allSatisfy { $0["passed"] as? Bool == true }])
                }
            }
        } catch {
            try writeJSON(["schema_version": 1, "head_sha": nativeHead, "compiler_head_sha": index.headSha, "native_head_sha": nativeHead, "status": "error",
                "error": String(describing: error), "cases": reports], to: outputRoot.appendingPathComponent("report.json"))
            throw error
        }
        try writeJSON(["schema_version": 1, "head_sha": nativeHead, "compiler_head_sha": index.headSha, "native_head_sha": nativeHead,
            "evidence_level": "authored offline native renderer replay; no provider or device claim",
            "scenario_sha256": hash(scenarioBytes), "source_receipts_sha256": hash(receiptBytes),
            "rgb_tolerance": scenario.rgbTolerance,
            "status": reports.allSatisfy { $0["passed"] as? Bool == true } ? "passed" : "failed",
            "cases": reports], to: outputRoot.appendingPathComponent("report.json"))
    }

    private func verifiedSources(recipe: EditRecipe, receipts: Receipts, root: URL) async throws -> [String: URL] {
        let manifest = try XCTUnwrap(recipe.assetManifest)
        try require(Set(manifest.assets.map(\.id)) == Set(recipe.assets.map(\.id)), "Manifest and recipe asset sets differ")
        var urls: [String: URL] = [:]
        for asset in recipe.assets {
            let reference = try XCTUnwrap(manifest.assets.first { $0.id == asset.id })
            let mediaID: String
            switch reference.source {
            case .original(let id): mediaID = id
            case .visual(let id, _, _): mediaID = id
            default: throw failure("Unexpected source identity in bounded replay: \(asset.id)")
            }
            let receipt = try XCTUnwrap(receipts.sources.first { $0.mediaId == mediaID })
            let url = try relativeURL(receipt.path, root: root)
            let actual = try SHA256Fingerprinter().fingerprint(file: url)
            try require(actual.hex == receipt.sha256 && actual.byteCount == receipt.byteCount, "Source receipt fingerprint mismatch")
            try require(try XCTUnwrap(asset.fingerprint) == actual, "Recipe fingerprint mismatch; never repin")
            try require(reference.fingerprint == RenderFingerprint(actual), "Manifest fingerprint mismatch; never repin")
            let metadata = try await probe(url, kind: receipt.kind)
            try require(metadata.width == receipt.width && metadata.height == receipt.height && metadata.hasAudio == receipt.hasAudio,
                        "Probed source metadata differs from receipt")
            try require(metadata.duration == receipt.durationS, "Probed source duration differs from receipt")
            urls[asset.id] = url
        }
        return urls
    }

    private func probe(_ url: URL, kind: String) async throws -> (width: Int, height: Int, duration: Double?, hasAudio: Bool) {
        if kind == "image" {
            let image = try XCTUnwrap(CIImage(contentsOf: url))
            return (Int(image.extent.width), Int(image.extent.height), nil, false)
        }
        try require(kind == "video", "Unsupported receipt source kind")
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let size = try await track.load(.naturalSize)
        let duration = try await asset.load(.duration).seconds
        let audio = try await asset.loadTracks(withMediaType: .audio)
        return (Int(size.width), Int(size.height), duration, !audio.isEmpty)
    }

    private func scenarioFixture() throws -> (Scenario, Data) {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "split-screen-request-output", withExtension: "json", subdirectory: "Fixtures"))
        let bytes = try Data(contentsOf: url)
        let scenario = try RecipeJSON.decoder().decode(Scenario.self, from: bytes)
        try require(scenario.schemaVersion == 1 && scenario.rgbTolerance == 45, "Unexpected authored oracle version or tolerance")
        return (scenario, bytes)
    }

    private func cleanCheckoutRevision() throws -> String {
        func git(_ arguments: [String]) throws -> String {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
            process.currentDirectoryURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            process.arguments = arguments
            let output = Pipe()
            process.standardOutput = output
            try process.run()
            let bytes = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            try require(process.terminationStatus == 0, "Cannot establish native checkout revision")
            return String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let head = try git(["rev-parse", "HEAD"])
        try require(head.range(of: "^[0-9a-f]{40}$", options: .regularExpression) != nil, "Invalid native checkout revision")
        try require(try git(["status", "--porcelain"]).isEmpty, "Native output acceptance requires a clean checkout")
        return head
    }

    private func replayDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_SPLIT_REPLAY_DIR"], !path.isEmpty else {
            throw XCTSkip("Set KRIA_SPLIT_REPLAY_DIR; run preparation, Python compilation, and rendering separately")
        }
        return URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
    }

    private func relativeURL(_ path: String, root: URL) throws -> URL {
        try require(!path.isEmpty && !path.hasPrefix("/"), "Evidence path must be relative")
        let url = root.appendingPathComponent(path).standardizedFileURL.resolvingSymlinksInPath()
        try require(url.path.hasPrefix(root.path + "/"), "Evidence path escapes replay directory")
        return url
    }

    private func rgb(_ image: CIImage, sample: Sample) -> [Int] {
        let x = min(Int(image.extent.width) - 1, Int(sample.xFraction * image.extent.width))
        let rowFromTop = min(Int(image.extent.height) - 1, Int(sample.yFromTop * image.extent.height))
        let y = Int(image.extent.height) - 1 - rowFromTop
        var pixel = [UInt8](repeating: 0, count: 4)
        CIContext().render(image, toBitmap: &pixel, rowBytes: 4, bounds: CGRect(x: x, y: y, width: 1, height: 1),
            format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return pixel.prefix(3).map(Int.init)
    }

    private func savePNG(_ image: CIImage, to url: URL) throws {
        try CIContext().writePNGRepresentation(of: image, to: url, format: .RGBA8,
                                              colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
    }
    private func hash(_ bytes: Data) -> String { SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined() }
    private func writeJSON(_ object: [String: Any], to url: URL) throws {
        try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]).write(to: url, options: .withoutOverwriting)
    }
    private func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }
    private func failure(_ message: String) -> NSError {
        NSError(domain: "SplitScreenRequestOutput", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
#endif
