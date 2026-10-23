import XCTest
@testable import KriaMediaEngine

#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreGraphics
import CoreImage
import ImageIO
import UniformTypeIdentifiers

/// Opt-in macOS-native renderer proof for the T1 duration-limit evidence.
/// It never reaches the network: publication is an in-memory acknowledgement.
/// Run only after the evidence generator has written `cases.json` and request
/// payloads beneath `KRIA_T1_DURATION_DIR`; raw videos remain outside git.
@MainActor
final class StrictDurationT1Tests: XCTestCase {
    private struct Evidence: Decodable {
        let cases: [Case]
    }

    private struct Case: Decodable {
        let name: String
        let sourceFile: String
        let requestFile: String
        let expectedEditS: Double
        let maximumS: Double
        let badgeSampleS: Double
    }

    func testNativeMacOSStrictDurationEvidence() async throws {
        guard let rootPath = ProcessInfo.processInfo.environment["KRIA_T1_DURATION_DIR"], !rootPath.isEmpty else {
            throw XCTSkip("Set KRIA_T1_DURATION_DIR to the generated T1 evidence directory")
        }
        let root = URL(fileURLWithPath: rootPath, isDirectory: true)
        let evidenceDecoder = JSONDecoder()
        evidenceDecoder.keyDecodingStrategy = .convertFromSnakeCase
        let evidence = try evidenceDecoder.decode(Evidence.self, from: Data(contentsOf: root.appendingPathComponent("cases.json")))
        XCTAssertFalse(evidence.cases.isEmpty)

        for evidenceCase in evidence.cases {
            try await run(evidenceCase, root: root)
        }
    }

    private func run(_ evidence: Case, root: URL) async throws {
        let source = URL(fileURLWithPath: evidence.sourceFile)
        let requestURL = URL(fileURLWithPath: evidence.requestFile)
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path), "missing T1 source \(source.path)")
        XCTAssertTrue(FileManager.default.fileExists(atPath: requestURL.path), "missing T1 request \(requestURL.path)")

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let decoded = try decoder.decode(DeviceRenderRequest.self, from: Data(contentsOf: requestURL))
        let request = decoded
        let serverMaximum = try XCTUnwrap(request.maxExportDurationS, "T1 request must carry the server strict export limit")
        XCTAssertEqual(serverMaximum, evidence.maximumS, accuracy: 0.000_001,
                       "the server request, not test code, owns the strict export limit")
        let assetID = try XCTUnwrap(request.recipe.assets.first?.id, "T1 recipe needs an original media asset")
        var sourceURLs = [assetID: source]
        for asset in request.recipe.assets where asset.id.hasPrefix("font-") {
            let fontName = String(asset.id.dropFirst("font-".count))
            let font = repositoryRoot.appendingPathComponent("src/apps/web/public/fonts/\(fontName)")
            XCTAssertTrue(FileManager.default.fileExists(atPath: font.path), "missing local font \(fontName)")
            sourceURLs[asset.id] = font
        }
        let outputDirectory = root.appendingPathComponent(evidence.name, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

        let publisher = LocalPublisher()
        let coordinator = try DeviceRenderCoordinator(
            directory: outputDirectory,
            exporter: AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: outputDirectory), branding: .standard),
            sources: SourceMap(urls: sourceURLs), publisher: publisher
        )
        try await coordinator.start(request, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()

        let savedReceipt = await coordinator.snapshot()
        let receipt = try XCTUnwrap(savedReceipt)
        let output = try XCTUnwrap(receipt.outputURL, "native output should be retained in the evidence directory")
        let duration = try await AVURLAsset(url: output).load(.duration).seconds
        let frameTolerance = 2 / max(1, request.recipe.frameRate)
        let expectedOutput = evidence.expectedEditS + 1.6
        let textEnd = try XCTUnwrap(request.recipe.textLayers.first(where: { $0.runs.contains(where: { $0.text.localizedCaseInsensitiveContains("Loy Tree") }) })?.end,
                                    "T1 request needs the Loy Tree badge text layer")
        let badgeFrame = outputDirectory.appendingPathComponent("badge-\(evidence.name).png")
        let outroFrame = outputDirectory.appendingPathComponent("outro-\(evidence.name).png")
        let badgeLimePixels = try saveFrameAndCountLime(asset: AVURLAsset(url: output), at: evidence.badgeSampleS, to: badgeFrame)
        let outroLimePixels = try saveFrameAndCountLime(asset: AVURLAsset(url: output), at: evidence.expectedEditS + 0.8, to: outroFrame)

        var report: [String: Any] = [
            "renderer": "macOS AVFoundation native renderer proof only; not an iPhone or real-chat proof",
            "case": evidence.name,
            "source": source.path,
            "request": requestURL.path,
            "output": output.path,
            "phase": receipt.phase.rawValue,
            "duration_s": duration,
            "maximum_s": evidence.maximumS,
            "expected_output_s": expectedOutput,
            "frame_tolerance_s": frameTolerance,
            "loy_tree_end_s": textEnd,
            "badge_sample_s": evidence.badgeSampleS,
            "badge_frame": badgeFrame.path,
            "outro_frame": outroFrame.path,
            "badge_upper_band_lime_pixels": badgeLimePixels,
            "outro_upper_band_lime_pixels": outroLimePixels,
        ]

        XCTAssertEqual(receipt.phase, .synced)
        XCTAssertLessThan(duration, evidence.maximumS)
        XCTAssertEqual(duration, expectedOutput, accuracy: frameTolerance)
        XCTAssertGreaterThanOrEqual(textEnd, evidence.expectedEditS - 3)
        XCTAssertLessThanOrEqual(textEnd, evidence.expectedEditS + frameTolerance)
        XCTAssertGreaterThan(badgeLimePixels, 100, "Loy Tree lime badge must be visible in the edit's final three seconds")

        if evidence.name == "h264" {
            let controlOutput = outputDirectory.appendingPathComponent("wrong-content-control.mp4")
            var withoutBadge = request.recipe
            withoutBadge.textLayers = []
            let controlExporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: outputDirectory), branding: .standard)
            _ = try await controlExporter.export(recipe: withoutBadge, assetURLs: sourceURLs, outputURL: controlOutput, exportID: "wrong-content-control")
            let controlFrame = outputDirectory.appendingPathComponent("wrong-content-control.png")
            let controlLimePixels = try saveFrameAndCountLime(asset: AVURLAsset(url: controlOutput), at: evidence.badgeSampleS, to: controlFrame)
            report["wrong_content_control_output"] = controlOutput.path
            report["wrong_content_control_frame"] = controlFrame.path
            report["wrong_content_control_upper_band_lime_pixels"] = controlLimePixels
            XCTAssertLessThan(controlLimePixels, max(100, badgeLimePixels / 2), "removing the badge text must substantially reduce lime pixels")
        }

        let reportURL = outputDirectory.appendingPathComponent("report.json")
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: reportURL, options: .atomic)
        print("T1 macOS native proof output: \(output.path)")
        print("T1 macOS native proof report: \(reportURL.path)")
    }

    private func saveFrameAndCountLime(asset: AVAsset, at seconds: Double, to destination: URL) throws -> Int {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let image = try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 60_000), actualTime: nil)
        let destinationWriter = try XCTUnwrap(CGImageDestinationCreateWithURL(destination as CFURL, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destinationWriter, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destinationWriter))

        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                               space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        var lime = 0
        for y in 0..<(height / 2) {
            for x in 0..<width {
                let offset = (y * width + x) * 4
                let red = Int(pixels[offset]), green = Int(pixels[offset + 1]), blue = Int(pixels[offset + 2]), alpha = Int(pixels[offset + 3])
                if alpha > 200, green > 150, green > red + 35, green > blue + 60 { lime += 1 }
            }
        }
        return lime
    }

    private var repositoryRoot: URL {
        var directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        for _ in 0..<7 { directory.deleteLastPathComponent() }
        return directory
    }
}

private struct SourceMap: DeviceSourceResolving {
    let urls: [String: URL]
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { urls }
}

private actor LocalPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}
#endif
