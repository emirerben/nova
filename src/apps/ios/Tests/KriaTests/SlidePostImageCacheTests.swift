import XCTest
@testable import Kria

/// Switching slides must not re-download or re-decode: the cache is keyed by asset id (signed URLs
/// change on every poll), shares in-flight loads, survives relaunch on disk, and reports expiry.
final class SlidePostImageCacheTests: XCTestCase {
    private var directory: URL!
    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory.appending(path: "SlidePostImageCacheTests-\(UUID().uuidString)")
    }
    override func tearDown() { try? FileManager.default.removeItem(at: directory); super.tearDown() }

    private func jpeg(width: Int = 2400, height: Int = 1800) -> Data {
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: { let f = UIGraphicsImageRendererFormat(); f.scale = 1; return f }())
        return renderer.jpegData(withCompressionQuality: 0.8) { ctx in UIColor.systemTeal.setFill(); ctx.fill(CGRect(x: 0, y: 0, width: width, height: height)) }
    }
    private final class Counter: @unchecked Sendable {
        private let lock = NSLock(); private var value = 0
        func bump() { lock.withLock { value += 1 } }
        var count: Int { lock.withLock { value } }
    }
    private func cache(fetches: Counter, delayMs: UInt64 = 0, failWith: Error? = nil) -> SlidePostImageCache {
        let data = jpeg()
        return SlidePostImageCache(directory: directory) { _ in
            fetches.bump()
            if delayMs > 0 { try await Task.sleep(nanoseconds: delayMs * 1_000_000) }
            if let failWith { throw failWith }
            return data
        }
    }
    private func url(_ sig: String) -> URL { URL(string: "https://cdn.test/a.jpg?sig=\(sig)")! }

    func testSameAssetWithAFreshSignedURLIsServedFromMemory() async throws {
        let fetches = Counter(); let cache = cache(fetches: fetches)
        _ = try await cache.image(assetID: "a", variant: .preview, url: url("one"))
        XCTAssertNotNil(cache.cachedImage(assetID: "a", variant: .preview), "instant, synchronous lookup once loaded")
        _ = try await cache.image(assetID: "a", variant: .preview, url: url("two"))
        XCTAssertEqual(fetches.count, 1, "a re-signed URL for the same asset must not download again")
        XCTAssertEqual(cache.stats.memoryHits, 1)
    }

    func testDownsamplesToTheVariantsLongestEdge() async throws {
        let fetches = Counter(); let cache = cache(fetches: fetches)
        let preview = try await cache.image(assetID: "a", variant: .preview, url: url("x"))
        XCTAssertLessThanOrEqual(max(preview.size.width, preview.size.height) * preview.scale, 1600)
        let thumb = try XCTUnwrap(cache.cachedImage(assetID: "a", variant: .thumb), "a full-size download also yields the thumbnail")
        XCTAssertLessThanOrEqual(max(thumb.size.width, thumb.size.height) * thumb.scale, 360)
        XCTAssertEqual(fetches.count, 1)
    }

    func testDiskSurvivesAFreshCacheInstance() async throws {
        let first = Counter()
        _ = try await cache(fetches: first).image(assetID: "a", variant: .preview, url: url("x"))
        let second = Counter(); let reopened = cache(fetches: second)
        XCTAssertNil(reopened.cachedImage(assetID: "a", variant: .preview), "memory is empty after relaunch")
        _ = try await reopened.image(assetID: "a", variant: .preview, url: url("different"))
        XCTAssertEqual(second.count, 0, "served from disk")
        XCTAssertEqual(reopened.stats.diskHits, 1)
        XCTAssertNotNil(reopened.cachedImage(assetID: "a", variant: .preview))
    }

    func testConcurrentRequestsShareOneDownload() async throws {
        let fetches = Counter(); let cache = cache(fetches: fetches, delayMs: 150)
        let urls = (0..<6).map { url("s\($0)") }
        await withThrowingTaskGroup(of: Void.self) { group in
            for u in urls { group.addTask { _ = try await cache.image(assetID: "a", variant: .preview, url: u) } }
        }
        XCTAssertEqual(fetches.count, 1)
    }

    func testExpiredURLIsReportedSoTheCallerCanRefresh() async {
        let fetches = Counter(); let cache = cache(fetches: fetches, failWith: SlidePostImageCache.LoadError.expired)
        do { _ = try await cache.image(assetID: "a", variant: .preview, url: url("old")); XCTFail("should throw") }
        catch { XCTAssertEqual(error as? SlidePostImageCache.LoadError, .expired) }
        XCTAssertNil(cache.cachedImage(assetID: "a", variant: .preview), "a failure caches nothing")
    }

    func testPrefetchWarmsTheCache() async throws {
        let fetches = Counter(); let cache = cache(fetches: fetches)
        cache.prefetch([.init(assetID: "a", variant: .preview, url: url("x")), .init(assetID: "b", variant: .preview, url: url("y"))])
        for _ in 0..<50 where cache.cachedImage(assetID: "b", variant: .preview) == nil || cache.cachedImage(assetID: "a", variant: .preview) == nil {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertNotNil(cache.cachedImage(assetID: "a", variant: .preview))
        XCTAssertNotNil(cache.cachedImage(assetID: "b", variant: .preview))
    }

    // MARK: Prefetch plan

    private func asset(_ id: String, kind: String = "image", withURLs: Bool = true) -> SlidePostAsset {
        let u = URL(string: "https://cdn.test/\(id).jpg")
        return SlidePostAsset(id: id, kind: kind, status: "ready", displayURL: withURLs ? u : nil, previewURL: withURLs ? u : nil, sourceURL: withURLs ? u : nil)
    }

    func testPrefetchPlanIsSelectedPlusMinusTwoNearestFirstThenEveryThumbnail() {
        let ids = (0..<8).map { "a\($0)" }
        let plan = SlidePostPrefetch.requests(slideAssetIDs: ids, selectedIndex: 3, assets: ids.map { asset($0) })
        let previews = plan.filter { $0.variant == .preview }.map(\.assetID)
        XCTAssertEqual(Set(previews), ["a1", "a2", "a3", "a4", "a5"])
        XCTAssertEqual(previews.first, "a3", "the selected slide loads first")
        XCTAssertEqual(plan.filter { $0.variant == .thumb }.count, 8)
        XCTAssertEqual(plan.prefix(5).allSatisfy { $0.variant == .preview }, true, "previews are queued before thumbnails")
    }

    func testPrefetchPlanSkipsAssetsWithoutURLsAndClampsAtTheEdges() {
        let ids = ["a", "b", "c"]
        let assets = [asset("a"), asset("b", withURLs: false), asset("c")]
        let plan = SlidePostPrefetch.requests(slideAssetIDs: ids, selectedIndex: 0, assets: assets)
        XCTAssertEqual(Set(plan.map { $0.assetID }), ["a", "c"])
        XCTAssertTrue(SlidePostPrefetch.requests(slideAssetIDs: [], selectedIndex: nil, assets: assets).isEmpty)
    }

    func testVideosPreviewFromTheirPosterNotTheMovie() {
        let video = SlidePostAsset(id: "v", kind: "video", status: "ready", displayURL: nil,
                                   previewURL: URL(string: "https://cdn.test/poster.jpg"), sourceURL: URL(string: "https://cdn.test/movie.mp4"))
        XCTAssertEqual(video.previewMediaURL?.lastPathComponent, "poster.jpg")
    }
}
