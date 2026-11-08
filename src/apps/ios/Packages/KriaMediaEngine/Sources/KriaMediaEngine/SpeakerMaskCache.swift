import Foundation
#if canImport(AVFoundation)
import CoreGraphics
import CoreImage
import CoreVideo
import CryptoKit
#if canImport(Vision)
import Vision
#endif

// A disk cache of Vision's person mask for the speaker matte, so an editor Save that changed only text
// or captions does not segment every frame again. Failure modes and their treatments:
// `docs/reviews/speaker-mask-cache-failure-modes.md`.
//
// The cache is keyed by the exact pixels handed to Vision, so a stale answer is not representable, and
// it sits below `SpeakerMatte`: a hit returns the same buffer bytes Vision would have, so the coverage
// gate, the 0.5 s hold and the clip reset behave identically.

/// Everything but the pixels that decides Vision's answer. Part of every key and the name of the
/// directory the entries live in, so an OS update neither serves old masks nor keeps them on disk.
struct SpeakerMaskEnvironment: Equatable, Sendable {
    var osBuild: String
    var hardware: String
    var requestRevision: Int
    /// Bump to drop every stored mask (a change in how the entry or the key is built).
    var formatVersion = 1

    var digest: String {
        let text = "v\(formatVersion)|rev\(requestRevision)|\(osBuild)|\(hardware)"
        return SHA256.hash(data: Data(text.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    static let current = SpeakerMaskEnvironment(
        osBuild: sysctlText("kern.osversion"),
        hardware: sysctlText("hw.machine") + "/" + sysctlText("hw.model"),
        requestRevision: currentRequestRevision)

    private static var currentRequestRevision: Int {
        #if canImport(Vision)
        return VisionPersonSegmenter.makeRequest(quality: .balanced).revision
        #else
        return 0
        #endif
    }

    private static func sysctlText(_ name: String) -> String {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return "unknown" }
        var bytes = [CChar](repeating: 0, count: size)
        guard sysctlbyname(name, &bytes, &size, nil, 0) == 0 else { return "unknown" }
        return String(cString: bytes)
    }
}

/// The two steps of a Vision segmenter the cache needs to tell apart: the pixels Vision is handed, and
/// the answer for them. A segmenter that is not split this way (a test double) is never cached.
protocol PixelSegmenting: NativePersonSegmenting {
    func segmentationInput(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) -> CVPixelBuffer?
    func segmentationMask(forInput input: CVPixelBuffer, config: SpeakerMatteConfig) throws -> CVPixelBuffer?
}

struct SpeakerMaskKey: Equatable, Sendable {
    let hex: String

    /// nil when the input is not the 32-bit buffer the Vision segmenter renders (nothing to hash safely).
    init?(input: CVPixelBuffer, config: SpeakerMatteConfig, environment: SpeakerMaskEnvironment) {
        guard CVPixelBufferGetPixelFormatType(input) == kCVPixelFormatType_32BGRA else { return nil }
        CVPixelBufferLockBaseAddress(input, .readOnly); defer { CVPixelBufferUnlockBaseAddress(input, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(input) else { return nil }
        let width = CVPixelBufferGetWidth(input), height = CVPixelBufferGetHeight(input), stride = CVPixelBufferGetBytesPerRow(input)
        guard width > 0, height > 0, stride >= width * 4 else { return nil }
        let quality: String
        switch config.quality {
        case .accurate: quality = "accurate"
        case .balanced: quality = "balanced"
        case .fast: quality = "fast"
        }
        var hasher = SHA256()
        hasher.update(data: Data("kria-speaker-mask|\(environment.digest)|\(quality)|\(config.inputMaxSide)|bgra|\(width)x\(height)|out8".utf8))
        // Row by row: the bytes past `width * 4` are padding the pool may leave dirty.
        for y in 0..<height { hasher.update(bufferPointer: UnsafeRawBufferPointer(start: base + y * stride, count: width * 4)) }
        hex = hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

/// A mask copied out of Vision's buffer, tightly packed. Sendable, so compressing and writing can leave the render thread.
struct SpeakerMaskPixels: Sendable {
    let width: Int
    let height: Int
    let bytes: Data

    static let maxSide = 8192

    /// nil for anything Vision does not answer in normal use: another pixel format or an absurd size. Never stored.
    init?(_ buffer: CVPixelBuffer) {
        guard CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_OneComponent8 else { return nil }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        guard (1...Self.maxSide).contains(width), (1...Self.maxSide).contains(height) else { return nil }
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        var packed = Data(count: width * height)
        packed.withUnsafeMutableBytes { destination in
            for y in 0..<height { memcpy(destination.baseAddress! + y * width, base + y * stride, width) }
        }
        self.width = width; self.height = height; self.bytes = packed
    }

    private init(width: Int, height: Int, bytes: Data) { self.width = width; self.height = height; self.bytes = bytes }

    /// Header (magic, width, height, 16 bytes of the raw mask's SHA-256), then the lzfse payload. The digest
    /// is what turns a file cut short by a power loss into a miss rather than a wrong silhouette.
    func encoded() -> Data? {
        guard let payload = try? (bytes as NSData).compressed(using: .lzfse) else { return nil }
        var data = Data("KMK1".utf8)
        for value in [UInt32(width), UInt32(height)] { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: SHA256.hash(data: bytes).prefix(16))
        data.append(payload as Data)
        return data
    }

    static func decode(_ data: Data) -> SpeakerMaskPixels? {
        let headerSize = 4 + 4 + 4 + 16
        guard data.count > headerSize, data.prefix(4) == Data("KMK1".utf8) else { return nil }
        func word(_ offset: Int) -> Int {
            Int(data.subdata(in: offset..<offset + 4).withUnsafeBytes { UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self)) })
        }
        let width = word(4), height = word(8)
        guard (1...maxSide).contains(width), (1...maxSide).contains(height) else { return nil }
        let digest = data.subdata(in: 12..<28)
        guard let raw = try? (data.subdata(in: headerSize..<data.count) as NSData).decompressed(using: .lzfse) as Data,
              raw.count == width * height,
              Data(SHA256.hash(data: raw).prefix(16)) == digest else { return nil }
        return SpeakerMaskPixels(width: width, height: height, bytes: raw)
    }

    func pixelBuffer() -> CVPixelBuffer? {
        var created: CVPixelBuffer?
        let attributes: [String: Any] = [kCVPixelBufferIOSurfacePropertiesKey as String: [String: String]()]
        guard CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_OneComponent8, attributes as CFDictionary, &created) == kCVReturnSuccess,
              let buffer = created else { return nil }
        CVPixelBufferLockBaseAddress(buffer, []); defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        bytes.withUnsafeBytes { source in
            for y in 0..<height { memcpy(base + y * stride, source.baseAddress! + y * width, width) }
        }
        return buffer
    }
}

/// Where cached masks live. Created once at launch with a directory the system may reclaim (like
/// `RenderLibraryCache(root:)`); with none installed the speaker matte takes the exact old path.
///
/// Layout: `<root>/v1/<environment>/<pack>/<key>.mask`. A pack is one speaker footage set (sorted SHA-256
/// of its sources); it groups entries for eviction and nothing else, since a key already names its pixels.
public final class SpeakerMaskStore: @unchecked Sendable {
    public struct Limits: Sendable {
        /// Hard cap on the bytes kept. Eviction is checked at launch and after every `trimEveryBytes` written.
        public var capBytes: Int64 = 512 * 1024 * 1024
        public var trimEveryBytes: Int64 = 16 * 1024 * 1024
        /// A pack opened (or written) this recently is not evicted while others remain.
        public var protectedSeconds: TimeInterval = 60
        /// More queued writes than this are dropped (the next render misses), so memory never grows with a slow disk.
        public var maxPendingWrites = 128
        public init() {}
    }

    public struct Statistics: Equatable, Sendable {
        public var hits = 0, misses = 0, stored = 0, droppedWrites = 0, corruptEntries = 0, evictedBytes: Int64 = 0
    }

    /// The store the engine uses; nil (the default) means no cache. Set once at launch.
    nonisolated(unsafe) public static var installed: SpeakerMaskStore?

    let environment: SpeakerMaskEnvironment
    let limits: Limits
    private let environmentRoot: URL
    private let versionRoot: URL
    private let queue = DispatchQueue(label: "kria.speaker-mask-store", qos: .utility)
    private let lock = NSLock()
    private var counters = Statistics()
    private var pendingWrites = 0
    private var bytesSinceTrim: Int64 = 0

    public convenience init?(root: URL, limits: Limits = Limits()) {
        self.init(root: root, limits: limits, environment: .current)
    }

    /// nil when the directory cannot be created, so a read-only volume or a full disk leaves the matte on Vision.
    init?(root: URL, limits: Limits, environment: SpeakerMaskEnvironment) {
        self.environment = environment
        self.limits = limits
        versionRoot = root.appendingPathComponent("v\(environment.formatVersion)", isDirectory: true)
        environmentRoot = versionRoot.appendingPathComponent(environment.digest, isDirectory: true)
        do { try FileManager.default.createDirectory(at: environmentRoot, withIntermediateDirectories: true) } catch { return nil }
        queue.async { [self] in trim() }
    }

    public var statistics: Statistics { lock.lock(); defer { lock.unlock() }; return counters }

    /// Waits for queued writes and evictions; tests and the launch sweep use it.
    public func flush() { queue.sync {} }

    /// Bytes of masks on disk across every environment.
    public func diskBytes() -> Int64 {
        flush()
        return Self.entries(under: versionRoot, suffix: ".mask").reduce(0) { $0 + $1.size }
    }

    func pack(sources: [String]) -> SpeakerMaskPack? {
        let name = SHA256.hash(data: Data(Set(sources).sorted().joined(separator: "\n").utf8)).map { String(format: "%02x", $0) }.joined()
        let directory = environmentRoot.appendingPathComponent(name, isDirectory: true)
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) } catch { return nil }
        try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: directory.path)
        return SpeakerMaskPack(store: self, directory: directory)
    }

    fileprivate func note(_ change: (inout Statistics) -> Void) { lock.lock(); change(&counters); lock.unlock() }

    fileprivate func enqueueWrite(_ pixels: SpeakerMaskPixels, to file: URL) {
        lock.lock()
        guard pendingWrites < limits.maxPendingWrites else { counters.droppedWrites += 1; lock.unlock(); return }
        pendingWrites += 1
        lock.unlock()
        queue.async { [self] in
            var written: Int64 = 0
            // Written whole under a name nothing reads, then renamed: a reader sees no file or a complete one.
            let partial = file.deletingLastPathComponent().appendingPathComponent(UUID().uuidString + ".partial")
            if let data = pixels.encoded(), (try? data.write(to: partial)) != nil, rename(partial.path, file.path) == 0 {
                written = Int64(data.count)
            } else {
                try? FileManager.default.removeItem(at: partial)
            }
            lock.lock()
            pendingWrites -= 1
            if written > 0 { counters.stored += 1; bytesSinceTrim += written }
            let due = bytesSinceTrim >= limits.trimEveryBytes
            if due { bytesSinceTrim = 0 }
            lock.unlock()
            if due { trim() }
        }
    }

    private struct Entry { let url: URL; let size: Int64; let modified: Date }

    private static func entries(under directory: URL, suffix: String) -> [Entry] {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]
        guard let walker = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: keys) else { return [] }
        var found: [Entry] = []
        for case let url as URL in walker where url.lastPathComponent.hasSuffix(suffix) {
            guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else { continue }
            found.append(Entry(url: url, size: Int64(values.fileSize ?? 0), modified: values.contentModificationDate ?? .distantPast))
        }
        return found
    }

    /// Runs on `queue`. Removes other environments, abandoned `.partial` files, then whole packs oldest first,
    /// then (only if one pack is still over the cap) its oldest entries.
    private func trim() {
        let fm = FileManager.default
        for url in (try? fm.contentsOfDirectory(at: versionRoot, includingPropertiesForKeys: nil)) ?? []
        where url.lastPathComponent != environment.digest {
            try? fm.removeItem(at: url)
        }
        for stray in Self.entries(under: environmentRoot, suffix: ".partial") where Date().timeIntervalSince(stray.modified) > 3600 {
            try? fm.removeItem(at: stray.url)
        }
        let packs = ((try? fm.contentsOfDirectory(at: environmentRoot, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []).map { url -> (url: URL, used: Date, size: Int64) in
            let used = (try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return (url, used, Self.entries(under: url, suffix: ".mask").reduce(0) { $0 + $1.size })
        }
        var total = packs.reduce(0) { $0 + $1.size }
        var removed: Int64 = 0
        for pack in packs.sorted(by: { $0.used < $1.used }) where total > limits.capBytes {
            if Date().timeIntervalSince(pack.used) < limits.protectedSeconds { continue }
            try? fm.removeItem(at: pack.url)
            total -= pack.size; removed += pack.size
        }
        if total > limits.capBytes {
            for entry in Self.entries(under: environmentRoot, suffix: ".mask").sorted(by: { $0.modified < $1.modified }) where total > limits.capBytes {
                try? fm.removeItem(at: entry.url)
                total -= entry.size; removed += entry.size
            }
        }
        if removed > 0 { note { $0.evictedBytes += removed } }
    }
}

/// One speaker footage set's entries.
final class SpeakerMaskPack: @unchecked Sendable {
    private let store: SpeakerMaskStore
    let directory: URL

    fileprivate init(store: SpeakerMaskStore, directory: URL) { self.store = store; self.directory = directory }

    private func file(_ key: SpeakerMaskKey) -> URL { directory.appendingPathComponent(key.hex + ".mask") }

    func lookup(_ key: SpeakerMaskKey) -> CVPixelBuffer? {
        let url = file(key)
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { store.note { $0.misses += 1 }; return nil }
        guard let pixels = SpeakerMaskPixels.decode(data), let buffer = pixels.pixelBuffer() else {
            // Cut short, bit-rotted or from an unknown format: gone, so the next render repairs it.
            try? FileManager.default.removeItem(at: url)
            store.note { $0.corruptEntries += 1; $0.misses += 1 }
            return nil
        }
        store.note { $0.hits += 1 }
        return buffer
    }

    func store(_ key: SpeakerMaskKey, mask: CVPixelBuffer) {
        guard let pixels = SpeakerMaskPixels(mask) else { return }
        store.enqueueWrite(pixels, to: file(key))
    }
}

/// The decorator `SpeakerMatte` is given when a store is installed: Vision's input is rendered and hashed
/// here, and Vision runs only on a miss. Everything else is the base segmenter's.
final class CachingPersonSegmenter: NativePersonSegmenting {
    private let base: PixelSegmenting
    private let pack: SpeakerMaskPack
    private let environment: SpeakerMaskEnvironment

    init(base: PixelSegmenting, pack: SpeakerMaskPack, environment: SpeakerMaskEnvironment) {
        self.base = base; self.pack = pack; self.environment = environment
    }

    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        try base.confidence(for: frame, canvas: canvas)
    }

    func personMask(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
        guard let input = base.segmentationInput(for: frame, canvas: canvas, config: config) else { return nil }
        guard let key = EngineSignpost.measure("talk.matte.cache.key", { SpeakerMaskKey(input: input, config: config, environment: environment) }) else {
            return try base.segmentationMask(forInput: input, config: config)
        }
        if let hit = pack.lookup(key) {
            EngineSignpost.count("talk.matte.cache.hit")
            return hit
        }
        // A throw or nil passes up untouched, so the matte's hold logic runs as it always did and nothing is stored.
        let mask = try base.segmentationMask(forInput: input, config: config)
        if let mask { pack.store(key, mask: mask) }
        return mask
    }
}

enum SpeakerMaskCaching {
    /// `base` itself unless a store is installed, `base` is split into input and answer, and the speaker footage
    /// is known: the off path is the same object as before, not a cache that happens to miss.
    static func wrap(_ base: NativePersonSegmenting?, store: SpeakerMaskStore?, sources: [String]?) -> NativePersonSegmenting? {
        guard let store, let sources, !sources.isEmpty, let pixelSegmenter = base as? PixelSegmenting,
              let pack = store.pack(sources: sources) else { return base }
        return CachingPersonSegmenter(base: pixelSegmenter, pack: pack, environment: store.environment)
    }

    /// The SHA-256 of every source file the speaker clips play, from the recipe's manifest. nil when any is unknown:
    /// a pack named by a guess would only mislead eviction, so there is no cache.
    static func speakerSources(in recipe: EditRecipe, speakerClipIDs: Set<String>) -> [String]? {
        guard let manifest = recipe.assetManifest else { return nil }
        var hashes: [String] = []
        for assetID in Set(recipe.tracks.flatMap(\.clips).filter { speakerClipIDs.contains($0.id) }.map(\.sourceAssetID)) {
            guard let reference = manifest.assets.first(where: { $0.id == assetID }) else { return nil }
            hashes.append(reference.fingerprint.sha256)
        }
        return hashes.isEmpty ? nil : hashes.sorted()
    }
}
#endif
