import CryptoKit
import Foundation
import ImageIO
import OSLog
import UIKit

/// Memory + disk image cache for the slide editor, keyed by STABLE asset identity (asset id + size
/// variant), never by the signed URL: those URLs change on every poll/refresh, so a URL-keyed cache
/// (or `AsyncImage`, which restarts whenever its URL changes) re-downloads the same photo each time.
///
/// Loads decode and downsample off the main thread (ImageIO thumbnail), so a 12 MP original is never
/// held at full size. Concurrent requests for one key share a single task. An expired signed URL
/// (401/403/410) throws `LoadError.expired` so the caller can fetch fresh URLs and retry.
final class SlidePostImageCache: @unchecked Sendable {
    enum Variant: String, Sendable {
        case thumb, preview
        /// Longest edge, in pixels, the cached copy is decoded at.
        var maxPixel: Int { self == .thumb ? 360 : 1600 }
    }
    enum LoadError: Error, Equatable { case expired, failed }
    typealias Fetcher = @Sendable (URL) async throws -> Data

    struct Request: Hashable, Sendable {
        let assetID: String
        let variant: Variant
        let url: URL
    }
    struct Stats: Equatable, Sendable { var memoryHits = 0, diskHits = 0, network = 0 }

    static let shared = SlidePostImageCache()

    private let memory = NSCache<NSString, UIImage>()
    private let directory: URL
    private let fetch: Fetcher
    private let lock = NSLock()
    private var inflight: [String: Task<UIImage, Error>] = [:]
    private var counts = Stats()
    private let log = Logger(subsystem: "com.kria.app", category: "slidepost-images")

    init(directory: URL? = nil, fetch: @escaping Fetcher = SlidePostImageCache.defaultFetch) {
        self.directory = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appending(path: "SlidePostImages", directoryHint: .isDirectory)
        self.fetch = fetch
        memory.countLimit = 120
        memory.totalCostLimit = 96 * 1024 * 1024
        try? FileManager.default.createDirectory(at: self.directory, withIntermediateDirectories: true)
    }

    /// UI-test switch that makes every lookup miss (the pre-fix behaviour); always false in release.
    private var bypass: Bool {
        #if DEBUG
        SlidePostImageFixtures.cacheDisabled
        #else
        false
        #endif
    }
    var stats: Stats { lock.withLock { counts } }
    static func key(_ assetID: String, _ variant: Variant) -> String { "\(assetID)|\(variant.rawValue)" }

    /// Instant, main-thread-safe lookup. Nil when the image is not decoded in memory yet.
    func cachedImage(assetID: String, variant: Variant) -> UIImage? {
        if bypass { return nil }
        return memory.object(forKey: Self.key(assetID, variant) as NSString)
    }

    /// Memory, then disk, then network. Never touches the main thread for decoding.
    func image(assetID: String, variant: Variant, url: URL) async throws -> UIImage {
        let key = Self.key(assetID, variant)
        if bypass {
            guard let image = Self.decode(try await fetch(url), maxPixel: variant.maxPixel) else { throw LoadError.failed }
            return image
        }
        if let hit = memory.object(forKey: key as NSString) {
            lock.withLock { counts.memoryHits += 1 }
            return hit
        }
        let task: Task<UIImage, Error> = lock.withLock {
            if let running = inflight[key] { return running }
            let created = Task.detached(priority: .userInitiated) { [self] in
                try await load(key: key, variant: variant, url: url)
            }
            inflight[key] = created
            return created
        }
        defer { lock.withLock { inflight[key] = nil } }
        return try await task.value
    }

    /// Warms the cache in the background; failures (including an expired URL) are ignored.
    func prefetch(_ requests: [Request]) {
        for request in requests where cachedImage(assetID: request.assetID, variant: request.variant) == nil {
            Task.detached(priority: .utility) { [self] in
                _ = try? await image(assetID: request.assetID, variant: request.variant, url: request.url)
            }
        }
    }

    func removeAll() {
        memory.removeAllObjects()
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    // MARK: Loading

    private func load(key: String, variant: Variant, url: URL) async throws -> UIImage {
        let file = directory.appending(path: Self.fileName(key))
        if let data = try? Data(contentsOf: file), let image = Self.decode(data, maxPixel: variant.maxPixel) {
            memory.setObject(image, forKey: key as NSString, cost: Self.cost(image))
            lock.withLock { counts.diskHits += 1 }
            return image
        }
        let started = ContinuousClock.now
        let data = try await fetch(url)
        guard let image = Self.decode(data, maxPixel: variant.maxPixel) else { throw LoadError.failed }
        lock.withLock { counts.network += 1 }
        memory.setObject(image, forKey: key as NSString, cost: Self.cost(image))
        if let jpeg = image.jpegData(compressionQuality: 0.88) { try? jpeg.write(to: file, options: .atomic) }
        // Every rendition of one asset is the same picture: a full-size download also yields the
        // thumbnail, so the strip tile (and the blur-up placeholder) never costs a second request.
        if variant == .preview {
            let thumbKey = Self.key(String(key.dropLast(Variant.preview.rawValue.count + 1)), .thumb)
            if memory.object(forKey: thumbKey as NSString) == nil, let thumb = Self.decode(data, maxPixel: Variant.thumb.maxPixel) {
                memory.setObject(thumb, forKey: thumbKey as NSString, cost: Self.cost(thumb))
                if let jpeg = thumb.jpegData(compressionQuality: 0.85) { try? jpeg.write(to: directory.appending(path: Self.fileName(thumbKey)), options: .atomic) }
            }
        }
        log.debug("loaded \(key, privacy: .public) in \(started.duration(to: .now).description, privacy: .public)")
        return image
    }

    private static func fileName(_ key: String) -> String {
        SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined() + ".jpg"
    }
    private static func cost(_ image: UIImage) -> Int { Int(image.size.width * image.scale * image.size.height * image.scale * 4) }

    /// Orientation-correct, downsampled and fully decoded (so the main thread never decodes on draw).
    static func decode(_ data: Data, maxPixel: Int) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: cg)
    }

    static let defaultFetch: Fetcher = { url in
        if url.isFileURL { return try Data(contentsOf: url) }
        #if DEBUG
        if let data = try await SlidePostImageFixtures.fetch(url) { return data }
        #endif
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData   // this cache owns persistence, keyed by asset id
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse {
            if [401, 403, 410].contains(http.statusCode) { throw LoadError.expired }
            guard (200..<300).contains(http.statusCode) else { throw LoadError.failed }
        }
        return data
    }
}

/// What to warm when the strip appears, the selection moves, or slides are reordered/added: the
/// selected slide and its neighbours (selected +/- `radius`) at preview size, and every slide's
/// thumbnail (the instant blur-up placeholder and the strip tile).
enum SlidePostPrefetch {
    static let radius = 2

    static func requests(slideAssetIDs: [String], selectedIndex: Int?, assets: [SlidePostAsset], radius: Int = SlidePostPrefetch.radius) -> [SlidePostImageCache.Request] {
        let byID = Dictionary(assets.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var seen = Set<String>(), result: [SlidePostImageCache.Request] = []
        func add(_ request: SlidePostImageCache.Request) {
            if seen.insert(SlidePostImageCache.key(request.assetID, request.variant)).inserted { result.append(request) }
        }
        let center = selectedIndex.flatMap { slideAssetIDs.indices.contains($0) ? $0 : nil } ?? 0
        let order = slideAssetIDs.indices.sorted { abs($0 - center) < abs($1 - center) }
        for index in order where abs(index - center) <= radius {
            if let asset = byID[slideAssetIDs[index]], let url = asset.previewMediaURL { add(.init(assetID: asset.id, variant: .preview, url: url)) }
        }
        for index in order {
            if let asset = byID[slideAssetIDs[index]], let url = asset.thumbnailURL { add(.init(assetID: asset.id, variant: .thumb, url: url)) }
        }
        return result
    }
}

extension SlidePostAsset {
    /// Photos preview from the original; a video previews from its poster frame (the player is separate).
    var previewMediaURL: URL? { kind == "video" ? (previewURL ?? displayURL) : (sourceURL ?? displayURL ?? previewURL) }
    /// Smallest rendition the server offers.
    var thumbnailURL: URL? { previewURL ?? displayURL ?? (kind == "image" ? sourceURL : nil) }
}

#if DEBUG
/// UI tests only (`-ui-testing-chat` + `KRIA_SLIDE_POST_REMOTE_MEDIA=1`): fixture "remote" URLs
/// (`https://fixture.invalid/<bundle file>?sig=...`) are served from the app bundle after a delay, so the
/// loading behaviour is observable without a network. `KRIA_SLIDE_POST_NO_IMAGE_CACHE=1` makes the cache
/// forget every image, which reproduces the pre-fix "reload on every selection" behaviour.
enum SlidePostImageFixtures {
    static var isActive: Bool {
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") && ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_REMOTE_MEDIA"] == "1"
    }
    static var cacheDisabled: Bool { isActive && ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_NO_IMAGE_CACHE"] == "1" }
    static func fetch(_ url: URL) async throws -> Data? {
        guard isActive, url.host == "fixture.invalid" else { return nil }
        let delay = UInt64(ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_MEDIA_DELAY_MS"].flatMap(UInt64.init) ?? 700)
        try await Task.sleep(nanoseconds: delay * 1_000_000)
        let name = url.deletingPathExtension().lastPathComponent
        guard let file = Bundle.main.url(forResource: name, withExtension: url.pathExtension) else { throw SlidePostImageCache.LoadError.failed }
        return try Data(contentsOf: file)
    }
}
#endif
