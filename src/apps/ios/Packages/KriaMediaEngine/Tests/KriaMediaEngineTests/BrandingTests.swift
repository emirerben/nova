import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
#endif

/// The Kria watermark and outro on previews and exported edits.
///
/// The numbers here are the ones `brand/social/build.py` measures and gates at
/// the repo root; `testBundledAssetsMatchTheBrandKit` is what stops the two
/// copies drifting apart.
final class BrandingTests: XCTestCase {
    private var kitRoot: URL {
        URL(fileURLWithPath: #filePath)            // .../Tests/KriaMediaEngineTests/BrandingTests.swift
            .deletingLastPathComponent()           // KriaMediaEngineTests
            .deletingLastPathComponent()           // Tests
            .deletingLastPathComponent()           // KriaMediaEngine
            .deletingLastPathComponent()           // Packages
            .deletingLastPathComponent()           // ios
            .deletingLastPathComponent()           // apps
            .deletingLastPathComponent()           // src
            .deletingLastPathComponent()           // repo root
            .appendingPathComponent("brand/social/dist")
    }

    func testBundledAssetsMatchTheBrandKit() throws {
        // Resolved through KriaBranding so we read the LIBRARY's bundle:
        // `Bundle.module` in a test file is the test target's own bundle.
        var pairs: [(String, URL?)] = [("outro/kria-outro-paper.mp4", KriaBranding.outroURL())]
        for variant in KriaBranding.Variant.allCases {
            pairs.append(("watermark/\(variant.resourceName).png", KriaBranding.watermarkURL(variant)))
        }
        for (relativePath, url) in pairs {
            guard let bundled = url else {
                return XCTFail("\(relativePath) is not in the package bundle")
            }
            let source = kitRoot.appendingPathComponent(relativePath)
            try XCTSkipUnless(FileManager.default.fileExists(atPath: source.path),
                              "brand kit not present in this checkout")
            XCTAssertEqual(try Data(contentsOf: bundled), try Data(contentsOf: source),
                           "\(relativePath) drifted from brand/social/dist — re-run build.py")
        }
    }

    /// The mark's box on a 1080x1920 canvas, derived the way the compositor
    /// derives it. Pure geometry, so it pins the anchor without a render.
    func testWatermarkSitsClearOfTheCaptionBlock() {
        let canvas = CGSize(width: 1080, height: 1920)
        let transform = KriaBranding.tileTransform(canvas: canvas)
        // The mark's own origin inside the padded tile, in Core Image's
        // bottom-left space.
        let markOrigin = CGPoint(x: KriaBranding.tilePad, y: KriaBranding.tilePad)
            .applying(transform)
        XCTAssertEqual(markOrigin.x, 60, accuracy: 0.5)
        // 445px of clearance under the mark, and the caption block starts at
        // 1530 from the top => 390 from the bottom. The mark must stay above it.
        XCTAssertEqual(markOrigin.y, 445, accuracy: 0.5)
        // Bottom edge on 1475, which is what the position was signed off as.
        XCTAssertEqual(1920 - markOrigin.y, 1475, accuracy: 0.5)
        XCTAssertGreaterThan(markOrigin.y, 1920 - 1530, "mark must clear the caption block")
    }

    /// A canvas that is not 9:16 must still keep the whole mark on screen.
    /// The canvas comes from a server snapshot, so this is reachable.
    func testMarkStaysInsideNonVerticalCanvases() {
        for canvas in [CGSize(width: 1920, height: 1080),   // landscape
                       CGSize(width: 1080, height: 1080),   // square
                       CGSize(width: 300, height: 200)] {   // tiny landscape
            let t = KriaBranding.tileTransform(canvas: canvas)
            let origin = CGPoint(x: KriaBranding.tilePad, y: KriaBranding.tilePad).applying(t)
            let scale = min(canvas.width / KriaBranding.referenceWidth,
                            canvas.height / KriaBranding.referenceHeight)
            let size = CGSize(width: KriaBranding.markWidth * scale,
                              height: KriaBranding.markHeight * scale)
            XCTAssertGreaterThanOrEqual(origin.x, 0, "\(canvas)")
            XCTAssertGreaterThanOrEqual(origin.y, 0, "\(canvas)")
            XCTAssertLessThanOrEqual(origin.x + size.width, canvas.width, "\(canvas)")
            XCTAssertLessThanOrEqual(origin.y + size.height, canvas.height, "\(canvas)")
            // and never dominates the frame
            XCTAssertLessThan(size.width / canvas.width, 0.30, "\(canvas)")
            XCTAssertLessThan(size.height / canvas.height, 0.30, "\(canvas)")
        }
    }

    func testTileTransformScalesWithTheCanvas() {
        let half = KriaBranding.tileTransform(canvas: CGSize(width: 540, height: 960))
        let markOrigin = CGPoint(x: KriaBranding.tilePad, y: KriaBranding.tilePad).applying(half)
        XCTAssertEqual(markOrigin.x, 30, accuracy: 0.5)
        XCTAssertEqual(markOrigin.y, 222.5, accuracy: 1.0)
    }

    /// Every branding file the engine ships resolves out of the LIBRARY's
    /// bundle. Unconditional on purpose: `testBundledAssetsMatchTheBrandKit`
    /// skips when the brand kit is not in the checkout, but "is the file in
    /// the bundle at all" is the part that has actually broken in the field —
    /// an incremental `xcodebuild` leaving `Kria.app` with a stale
    /// `KriaMediaEngine_KriaMediaEngine.bundle` (2026-09-22). A miswired
    /// `resources:` in Package.swift lands here the same way.
    func testBrandingResourcesResolveFromTheLibraryBundle() throws {
        var bundled: [(String, URL)] = [
            (KriaBranding.outroFileName, try XCTUnwrap(KriaBranding.outroURL(),
                                                       "\(KriaBranding.outroFileName) is not in Bundle.module"))
        ]
        for variant in KriaBranding.Variant.allCases {
            let name = KriaBranding.watermarkFileName(variant)
            bundled.append((name, try XCTUnwrap(KriaBranding.watermarkURL(variant),
                                                "\(name) is not in Bundle.module")))
        }
        for (name, url) in bundled {
            XCTAssertGreaterThan(try Data(contentsOf: url).count, 0, "\(name) is bundled but empty")
        }
    }

    /// A requested tail that cannot be composited must fail the export rather
    /// than drop out of it. `Options.contractTail` is declared to the server
    /// from the REQUEST, so a silently missing outro reaches the creator as an
    /// "export duration mismatch" rejection on upload instead of a local error
    /// naming the file.
    func testPreflightFailsOnAMissingOutro() {
        XCTAssertThrowsError(try KriaBranding.preflight(.standard, resolveOutro: { nil })) { error in
            XCTAssertEqual(error as? MediaEngineError,
                           .missingBrandingResource(KriaBranding.outroFileName))
        }
    }

    /// The watermark does not move the duration, so a miss here is invisible
    /// to the server's check — it just ships an unbranded export, which is
    /// the bug branding was added to prevent.
    func testPreflightFailsOnAMissingWatermark() {
        let options = KriaBranding.Options(variant: .graphite)
        XCTAssertThrowsError(try KriaBranding.preflight(options, resolveWatermark: { _ in nil })) { error in
            XCTAssertEqual(error as? MediaEngineError,
                           .missingBrandingResource(KriaBranding.watermarkFileName(.graphite)))
        }
    }

    /// An unbranded export must not be gated on assets it will never read.
    func testPreflightIgnoresResourcesUnbrandedOptionsNeverRead() {
        XCTAssertNoThrow(try KriaBranding.preflight(.none, resolveWatermark: { _ in nil },
                                                    resolveOutro: { nil }))
    }

    /// ...and the options the app actually ships pass against the real bundle.
    func testPreflightPassesForTheShippedOptions() {
        XCTAssertNoThrow(try KriaBranding.preflight(.standard))
    }

    /// The creator-facing consequence of the classification. `export_failed`
    /// says the render did not finish and the project is saved; the wrong
    /// neighbour, `unsupported_recipe`, tells them to start a different edit
    /// and that retrying cannot help — the opposite of true for a build that
    /// only needs reinstalling.
    func testMissingBrandingResourceReportsAsAnExportFailure() {
        XCTAssertEqual(
            DeviceRenderFailureReasonCode.forRenderFailure(
                MediaEngineError.missingBrandingResource(KriaBranding.outroFileName)),
            .exportFailed)
    }

    #if canImport(AVFoundation)
    /// Bright pixels inside `rect`, measured from the visual top-left.
    @MainActor private func brightPixels(in image: CGImage, rect: CGRect,
                                         canvas: CGRect, context: CIContext) -> Int {
        let width = Int(canvas.width), height = Int(canvas.height)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        context.render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: width * 4,
                       bounds: canvas, format: .RGBA8,
                       colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        var count = 0
        for y in Int(rect.minY)..<Int(rect.maxY) {
            // The mark ships near-subliminal: #CAD2DB at 42% over black peaks
            // around 84, so the threshold sits below that rather than at a
            // "clearly visible" level.
            for x in Int(rect.minX)..<Int(rect.maxX) where pixels[(y * width + x) * 4] > 55 {
                count += 1
            }
        }
        return count
    }

    @MainActor private func blackPhotoRecipe(in directory: URL) throws -> (EditRecipe, [String: URL]) {
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let picture = CIImage(color: .black).cropped(to: canvas)
        let photo = directory.appendingPathComponent("black.png")
        try CIContext().writePNGRepresentation(of: picture, to: photo, format: .RGBA8,
                                               colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        let recipe = EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 1080, height: 1920),
            assets: [MediaAsset(id: "photo", relativePath: "photo", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "v", kind: .video,
                                   clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 2)])],
            assetManifest: RenderAssetManifest(
                assets: [RenderAssetReference(id: "photo", fingerprint: try RenderFingerprint(fingerprint),
                                              source: .original(mediaID: "photo"))]))
        return (recipe, ["photo": photo])
    }

    @MainActor func testBrandedExportCarriesTheMarkThroughoutAndEndsOnTheOutro() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let (recipe, urls) = try blackPhotoRecipe(in: directory)
        let output = directory.appendingPathComponent("branded.mp4")
        _ = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)

        let asset = AVURLAsset(url: output)
        let outro = AVURLAsset(url: try XCTUnwrap(KriaBranding.outroURL()))
        let duration = try await asset.load(.duration).seconds
        let outroDuration = try await outro.load(.duration).seconds
        XCTAssertEqual(duration, 2 + outroDuration, accuracy: 0.1,
                       "the export should be the edit plus the outro")

        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let context = CIContext()
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero

        // The mark's box, measured from the visual top-left.
        let markBox = CGRect(x: 60, y: 1475 - 59, width: 133, height: 59)
        let mirrored = CGRect(x: 1080 - 60 - 133, y: 1475 - 59, width: 133, height: 59)
        for time in [0.1, 1.0, 1.9] {
            let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            XCTAssertGreaterThan(brightPixels(in: frame, rect: markBox, canvas: canvas, context: context), 500,
                                 "no watermark at \(time)s")
            XCTAssertEqual(brightPixels(in: frame, rect: mirrored, canvas: canvas, context: context), 0,
                           "the mark must be bottom-LEFT only, at \(time)s")
        }

        // The outro is a near-white card, so the tail frame is overwhelmingly bright.
        let tail = try await generator.image(at: CMTime(seconds: duration - 0.2, preferredTimescale: 600)).image
        let bright = brightPixels(in: tail, rect: canvas, canvas: canvas, context: context)
        XCTAssertGreaterThan(bright, Int(canvas.width * canvas.height) / 2, "the export should end on the outro")
    }

    /// Pins what the PRODUCTION exporter declares. The server trusts this string
    /// to decide how long the uploaded file is allowed to be.
    @MainActor func testProductionExporterDeclaresTheStandardTail() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = FileExportStateStore(directory: directory)
        XCTAssertEqual(AVFoundationLocalExporter(stateStore: store).brandTail, "standard")
        XCTAssertEqual(
            AVFoundationLocalExporter(stateStore: store, branding: .none).brandTail, "none")
        // Only the outro changes duration, so the watermark alone declares "none".
        XCTAssertEqual(KriaBranding.Options(watermark: true, outro: false).contractTail, "none")
        XCTAssertEqual(KriaBranding.Options.standard.contractTail, "standard")
    }

    @MainActor func testUnbrandedExportHasNeitherMarkNorOutro() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let (recipe, urls) = try blackPhotoRecipe(in: directory)
        let output = directory.appendingPathComponent("plain.mp4")
        _ = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")),
            branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)

        let asset = AVURLAsset(url: output)
        let plainDuration = try await asset.load(.duration).seconds
        XCTAssertEqual(plainDuration, 2, accuracy: 0.1)
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let frame = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        XCTAssertEqual(brightPixels(in: frame, rect: CGRect(x: 60, y: 1475 - 59, width: 133, height: 59),
                                    canvas: canvas, context: CIContext()), 0)
    }

    @MainActor func testBrandedLivePreviewShowsMarkAndOutroBeforeAndAfterTextEdits() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var (recipe, urls) = try blackPhotoRecipe(in: directory)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound]))
            .appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: font)
        recipe.assets.append(MediaAsset(id: "font", relativePath: "font", fingerprint: fingerprint))
        recipe.assetManifest = RenderAssetManifest(assets: (recipe.assetManifest?.assets ?? []) + [
            RenderAssetReference(id: "font", fingerprint: try RenderFingerprint(fingerprint),
                source: .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: fingerprint.hex))
        ])
        urls["font"] = font
        func caption(_ text: String) throws -> PortableTextLayer {
            try AuthoredTextLayout.compile(id: "caption", text: text, start: 0, end: 2,
                style: .init(fontAssetID: "font", size: 72, color: TextInk(red: 1, green: 1, blue: 1, alpha: 1)),
                fontURL: font, canvas: recipe.canvas)
        }
        recipe.textLayers = [try caption("First version")]
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard)
        let item = live.preview.playerItem
        let outro = try await AVURLAsset(url: XCTUnwrap(KriaBranding.outroURL())).load(.duration).seconds
        XCTAssertEqual(live.preview.description.duration, 2 + outro, accuracy: 0.01)

        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let markBox = CGRect(x: 60, y: 1475 - 59, width: 133, height: 59)
        let context = CIContext()
        for edited in [false, true] {
            if edited {
                recipe.textLayers = [try caption("Edited version")]
                try live.updateText(recipe: recipe)
                XCTAssertTrue(item === live.preview.playerItem, "Text edits retain the live composition")
            }
            let generator = AVAssetImageGenerator(asset: item.asset)
            generator.videoComposition = item.videoComposition
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
            for time in [0.0, 1.0, 1.9] {
                let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                XCTAssertGreaterThan(brightPixels(in: frame, rect: markBox, canvas: canvas, context: context), 500,
                                     "Missing preview watermark at \(time)s, edited=\(edited)")
            }
            let tail = try await generator.image(at: CMTime(seconds: 2 + outro - 0.2, preferredTimescale: 600)).image
            XCTAssertGreaterThan(brightPixels(in: tail, rect: canvas, canvas: canvas, context: context),
                                 Int(canvas.width * canvas.height) / 2, "Missing preview outro, edited=\(edited)")
            let snapshot = live.exportSnapshot()
            XCTAssertEqual(snapshot.recipe, recipe, "Export inputs must not contain baked branding")
            XCTAssertEqual(TimelineMath.totalDuration(of: snapshot.recipe), 2, accuracy: 0.001)
        }
    }

    /// Internal sampling for blur fills and thumbnails stays unbranded.
    @MainActor func testPreviewCompositionIsUnbrandedByDefault() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let (recipe, urls) = try blackPhotoRecipe(in: directory)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        XCTAssertEqual(preview.description.duration, 2, accuracy: 0.001,
                       "the creator's timeline must not grow by the outro")
    }
    #endif
}
