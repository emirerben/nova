import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage

/// Pins `NativeGiantHandwritingPainter`'s settled-path/settled-tile caches
/// byte-for-byte against an uncached reference. Unlike the cloud-reference
/// comparisons in `GiantTitleTests` (which can only assert a tolerance, since
/// they compare two different renderers), both sides here run the exact same
/// drawing code — the only difference is whether state is retained across
/// calls — so this test can and does require EXACT equality, zero tolerance.
///
/// The scenario that actually caught a real bug during development: a single
/// retained painter is warmed once at `progress: 1` (mirroring
/// `NativeGiantTitlePainter.settledImage()`, called once per layer before any
/// real animated frame) and then driven through a forward sequence of lower
/// progress values, plus one explicit backward jump. The "reference" side
/// constructs a brand-new, never-reused painter for every single call, so it
/// has no cross-call state to get wrong by construction.
final class GiantTitleShadowParityTests: XCTestCase {
    private func fixture() throws -> GiantTitleTests.Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_giant_title.json").standardizedFileURL
        return try RecipeJSON.decoder().decode(GiantTitleTests.Fixture.self, from: Data(contentsOf: url))
    }

    private func pixels(_ image: CIImage, canvas: CGSize, context: CIContext) -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: Int(canvas.width) * Int(canvas.height) * 4)
        context.render(image, toBitmap: &buffer, rowBytes: Int(canvas.width) * 4,
            bounds: CGRect(origin: .zero, size: canvas), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return buffer
    }

    func testCachedPainterMatchesUncachedReferenceAcrossWarmupForwardAndBackwardProgress() throws {
        let fixture = try fixture()
        let root = String(#filePath.prefix(upTo: #filePath.range(of: "/src/apps/ios/")!.lowerBound))
        let font = URL(fileURLWithPath: root).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])

        for rowID in ["handwriting-legacy", "handwriting-glow", "handwriting-fading-glow", "handwriting-gradient"] {
            guard let row = fixture.cases.first(where: { $0.id == rowID }) else {
                XCTFail("fixture missing \(rowID)"); continue
            }
            let times = row.frames.map(\.time)
            // Real playback order: the warm-up settled image (implicit,
            // exercised via `settledImage()` below), then every fixture time
            // forward, then one explicit backward jump to an earlier time
            // that was already visited — the exact shape of the bug this
            // pins (a stroke settled during warm-up, then asked about again
            // at a genuinely lower, not-yet-settled progress).
            var sequence = times
            if times.count >= 2 { sequence.append(times[times.count / 2]) }

            let cachedLayer = try RecipeTextLayer.make(row.layer, assetURLs: ["font-Inter-Bold.ttf": font], canvas: canvas)
            let cachedPainter = try XCTUnwrap(cachedLayer.giantTitle)
            _ = try cachedPainter.settledImage() // warm-up, mirrors makeGiantTitle's real call

            for time in sequence {
                let cachedImage = try cachedPainter.image(localTime: time, settled: cachedLayer.image)
                let cachedPixels = pixels(cachedImage, canvas: canvas, context: context)

                // Fresh, never-reused painter: no cache can have gone stale
                // because there is no cache to begin with.
                let referenceLayer = try RecipeTextLayer.make(row.layer, assetURLs: ["font-Inter-Bold.ttf": font], canvas: canvas)
                let referencePainter = try XCTUnwrap(referenceLayer.giantTitle)
                let referenceImage = try referencePainter.image(localTime: time, settled: referenceLayer.image)
                let referencePixels = pixels(referenceImage, canvas: canvas, context: context)

                XCTAssertEqual(cachedPixels, referencePixels, "\(rowID) t=\(time): cached painter diverged from an uncached reference")
            }
        }
    }

    /// A starved cache budget must degrade to slower-but-correct, never to
    /// wrong bytes. Drives `NativeGiantHandwritingPainter` directly (bypassing
    /// `NativeGiantTitlePainter`, which doesn't expose a budget knob) with a
    /// `maxCacheBytes` far too small to hold even one tile, guaranteeing every
    /// call falls through the "don't cache, just render" branch, then checks
    /// that path against an uncached reference exactly as above.
    func testStarvedTileCacheStillMatchesReference() throws {
        let fixture = try fixture()
        guard let row = fixture.cases.first(where: { $0.id == "handwriting-glow" }),
              let content = row.layer.handwriting else {
            XCTFail("fixture missing handwriting-glow"); return
        }
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
        let imageContext = CIContext(options: [.cacheIntermediates: false,
            .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
        let bounds = CGRect(origin: .zero, size: canvas)
        let maxBitmapBytes = 64 * 1024 * 1024

        let starved = NativeGiantHandwritingPainter(content: content, canvas: canvas, maxCacheBytes: 1)
        _ = try starved.image(bounds: bounds, rotation: .identity, transform: .identity, progress: 1,
            opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)

        for progress in [0.1, 0.4, 0.68, 0.9, 1.0, 0.5] {
            let starvedImage = try starved.image(bounds: bounds, rotation: .identity, transform: .identity,
                progress: progress, opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
            let starvedPixels = pixels(starvedImage, canvas: canvas, context: context)

            let reference = NativeGiantHandwritingPainter(content: content, canvas: canvas)
            let referenceImage = try reference.image(bounds: bounds, rotation: .identity, transform: .identity,
                progress: progress, opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
            let referencePixels = pixels(referenceImage, canvas: canvas, context: context)

            XCTAssertEqual(starvedPixels, referencePixels, "handwriting-glow progress=\(progress): starved-cache path diverged")
        }
    }
}
#endif
