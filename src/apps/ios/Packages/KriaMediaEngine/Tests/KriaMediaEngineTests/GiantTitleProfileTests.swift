import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage

/// Counter-based (never wall-clock — wall-clock assertions are flaky in CI)
/// proof that `NativeGiantHandwritingPainter`'s settled-path/settled-tile
/// caches are actually engaging, not just present in the code. See
/// `docs/reviews/kri-29/giant-title.md` for the physical-device wall-clock
/// numbers this predicts.
final class GiantTitleProfileTests: XCTestCase {
    private func fixture() throws -> GiantTitleTests.Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_giant_title.json").standardizedFileURL
        return try RecipeJSON.decoder().decode(GiantTitleTests.Fixture.self, from: Data(contentsOf: url))
    }

    override func tearDown() {
        RenderProfiler.enabled = false
        RenderProfiler.reset()
        super.tearDown()
    }

    func testRepeatedIdentityFrameCollapsesToCacheHitsNotFreshTiles() throws {
        let fixture = try fixture()
        guard let row = fixture.cases.first(where: { $0.id == "handwriting-glow" }),
              let content = row.layer.handwriting else {
            XCTFail("fixture missing handwriting-glow"); return
        }
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let imageContext = CIContext(options: [.cacheIntermediates: false,
            .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
        let bounds = CGRect(origin: .zero, size: canvas)
        let maxBitmapBytes = 64 * 1024 * 1024
        let painter = NativeGiantHandwritingPainter(content: content, canvas: canvas)

        RenderProfiler.enabled = true
        RenderProfiler.reset()
        // First call at this (transform, opacity, progress): every settled
        // stroke's blur layer must render a fresh tile.
        _ = try painter.image(bounds: bounds, rotation: .identity, transform: .identity, progress: 1,
            opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
        let firstCallTiles = RenderProfiler.snapshot()["shadow.tile"]?["count"] ?? 0
        let firstCallHits = RenderProfiler.snapshot()["shadow.cache.hit"]?["count"] ?? 0
        XCTAssertGreaterThan(firstCallTiles, 0, "first call at a new signature should build fresh tiles")
        XCTAssertEqual(firstCallHits, 0, "nothing was cached yet on the very first call")

        RenderProfiler.reset()
        // Second call, IDENTICAL (transform, opacity, progress: all strokes
        // already settled from the first call): every stroke/blur-layer pair
        // should now be a cache hit, and `shadow.tile` should build nothing.
        _ = try painter.image(bounds: bounds, rotation: .identity, transform: .identity, progress: 1,
            opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
        let secondCallTiles = RenderProfiler.snapshot()["shadow.tile"]?["count"] ?? 0
        let secondCallHits = RenderProfiler.snapshot()["shadow.cache.hit"]?["count"] ?? 0
        XCTAssertEqual(secondCallTiles, 0, "a fully-settled repeat call should build zero fresh tiles")
        XCTAssertEqual(secondCallHits, firstCallTiles, "every tile built on the first call should be reused, not rebuilt")
    }

    func testSignatureChangeInvalidatesTheWholeShadowCache() throws {
        let fixture = try fixture()
        guard let row = fixture.cases.first(where: { $0.id == "handwriting-glow" }),
              let content = row.layer.handwriting else {
            XCTFail("fixture missing handwriting-glow"); return
        }
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let imageContext = CIContext(options: [.cacheIntermediates: false,
            .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
        let bounds = CGRect(origin: .zero, size: canvas)
        let maxBitmapBytes = 64 * 1024 * 1024
        let painter = NativeGiantHandwritingPainter(content: content, canvas: canvas)

        _ = try painter.image(bounds: bounds, rotation: .identity, transform: .identity, progress: 1,
            opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)

        RenderProfiler.enabled = true
        RenderProfiler.reset()
        // A different transform (the camera-zoom phase) must not reuse any
        // tile cached under `.identity` — every stroke/blur-layer pair is a
        // fresh build again, matching today's unconditional per-frame cost
        // for that phase (L1 targets the writing phase specifically).
        let zoomed = CGAffineTransform(scaleX: 4, y: 4)
        _ = try painter.image(bounds: bounds, rotation: .identity, transform: zoomed, progress: 1,
            opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
        let hits = RenderProfiler.snapshot()["shadow.cache.hit"]?["count"] ?? 0
        XCTAssertEqual(hits, 0, "a transform change must invalidate every cached tile, never serve a stale one")
    }
}
#endif
