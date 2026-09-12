import Foundation
#if DEBUG
import UIKit
#endif
import AVFoundation
import ImageIO
import CryptoKit
import KriaMediaEngine

struct NativeTimelineSource: Decodable, Sendable {
    let mediaID: String
    let sourceURL: URL?
    let original: OriginalMediaDescriptor?
    let localRequired: Bool
    enum CodingKeys: String, CodingKey {
        case mediaID = "media_id", sourceURL = "source_url", original, localRequired = "local_required"
    }
}

struct NativeEditorAsset: Decodable, Sendable {
    let id: String
    let kind: String
    let mediaID: String
    let sourceURL: URL
    let preserveAlpha: Bool?
    enum CodingKeys: String, CodingKey { case id, kind, mediaID = "media_id", sourceURL = "source_url", preserveAlpha = "preserve_alpha" }
}

struct NativeEditorSourcePool: Decodable, Sendable {
    struct Clip: Decodable, Sendable {
        let clipIndex: Int
        let nativeSource: NativeTimelineSource?
        init(clipIndex: Int, nativeSource: NativeTimelineSource?) {
            self.clipIndex = clipIndex; self.nativeSource = nativeSource
        }
        enum CodingKeys: String, CodingKey {
            case clipIndex = "clip_index", nativeSource = "native_source"
            case signedURL = "signed_url", mediaID = "media_id", kind
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            clipIndex = try c.decode(Int.self, forKey: .clipIndex)
            if c.contains(.nativeSource) {
                // Explicit null means the server could not resolve an original.
                nativeSource = try c.decodeIfPresent(NativeTimelineSource.self, forKey: .nativeSource)
            } else {
                let url = try c.decodeIfPresent(URL.self, forKey: .signedURL)
                let kind = try c.decodeIfPresent(String.self, forKey: .kind)
                // Legacy video URLs point to sources. Image URLs can be browser
                // derivatives; analysis proxies must never stand in for originals.
                if let url, url.scheme == "https", kind == nil || kind == "video",
                   !url.lastPathComponent.hasPrefix("analysis-proxy-") {
                    nativeSource = NativeTimelineSource(
                        mediaID: try c.decodeIfPresent(String.self, forKey: .mediaID) ?? "clip-\(clipIndex)",
                        sourceURL: url, original: nil, localRequired: false)
                } else {
                    nativeSource = nil
                }
            }
        }
    }
    let clips: [Clip]
    let baseGeneration: String?
    let nativeAssets: [NativeEditorAsset]
    init(clips: [Clip], baseGeneration: String?, nativeAssets: [NativeEditorAsset] = []) {
        self.clips = clips; self.baseGeneration = baseGeneration; self.nativeAssets = nativeAssets
    }
    enum CodingKeys: String, CodingKey { case clips, baseGeneration = "base_generation", nativeAssets = "native_assets" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        clips = try c.decode([Clip].self, forKey: .clips)
        baseGeneration = try c.decodeIfPresent(String.self, forKey: .baseGeneration)
        nativeAssets = try c.decodeIfPresent([NativeEditorAsset].self, forKey: .nativeAssets) ?? []
    }
}

extension KriaAPI {
    func editorSourcePool(jobID: UUID, variantID: String) async throws -> NativeEditorSourcePool {
        try await request(path: "generative-jobs/\(jobID.uuidString)/variants/\(variantID)/timeline",
                          method: "GET", bodyData: nil, decode: NativeEditorSourcePool.self)
    }
}

/// Speech composites have an immutable cut/audio bed and a separately stored,
/// text-free base. Their public clip timeline is deliberately empty.
struct NativeEditorBaseSource: Sendable {
    let url: URL
    let generation: String
    let mediaID: String

    init?(variant: [String: JSONValue], document: EditorDocument) {
        guard variant["resolved_archetype"] == .string("talking_head"),
              variant["render_destination"] != .string("device"),
              document.capabilities["timeline"]?.editable != true,
              document.tombstones.isEmpty,
              document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }),
              let path = variant["base_video_path"]?.stringValue, !path.isEmpty,
              let value = variant["base_video_url"]?.stringValue, let url = URL(string: value), url.scheme == "https",
              url.path != variant["output_url"]?.stringValue.flatMap({ URL(string: $0)?.path }),
              !document.revision.baseGeneration.isEmpty else { return nil }
        self.url = url
        generation = document.revision.baseGeneration
        mediaID = "variant-base-" + generation
    }

    func hydrate(_ document: EditorDocument, duration: Double) throws -> EditorDocument {
        guard duration.isFinite, duration > 0, document.revision.baseGeneration == generation else { throw APIError.conflict }
        guard document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }) else { return document }
        var result = document
        result.clips = [.init(id: "native-composite-base", clipIndex: 0, inS: 0, durationS: duration,
            raw: ["native_composite_source": .bool(true), "source_duration_s": .number(duration)])]
        return result
    }
}

enum NativeNarratedSourceTiming {
    /// Mirrors narrated_assembler._fit_clip_segment: short footage slows to
    /// fill its voiceover step, with a 50 ms guard before source EOF.
    static func hydrate(_ document: EditorDocument, sources: [Int: ResolvedEditorSource]) throws -> EditorDocument {
        var result = document
        for index in result.clips.indices {
            let slot = result.clips[index]
            guard let total = sources[slot.clipIndex]?.asset.duration,
                  let target = slot.durationS, target > 0, total > 0.05 else {
                #if DEBUG
                NativePreviewDiagnostics.record("narrated-invalid-source", fields: [
                    "index": String(slot.clipIndex), "sourcePresent": String(sources[slot.clipIndex] != nil),
                    "duration": String(sources[slot.clipIndex]?.asset.duration ?? -1), "target": String(slot.durationS ?? -1)])
                #endif
                throw RecipeError.invalidTimeline
            }
            let start = total - slot.inS <= 0.05 ? 0 : max(0, slot.inS)
            let available = total - start
            let span = available >= target ? target : max(0.05, available - 0.05)
            result.clips[index].inS = start
            result.clips[index].raw["source_duration_s"] = .number(total)
            result.clips[index].raw["native_source_span_s"] = .number(span)
        }
        return result
    }
}

struct ResolvedEditorSource: Sendable {
    var preserveAlpha = false
    let clipIndex: Int
    let mediaID: String
    let asset: MediaAsset
    let url: URL
}

/// URLSession download locations end in .tmp. AVFoundation rejects otherwise
/// valid MP4/AAC bytes at that extension, so retain their media type on import.
enum NativeDownloadedMedia {
    static func isStillImage(_ url: URL) -> Bool {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return false }
        return CGImageSourceGetCount(source) > 0
    }

    static func fileExtension(sourceURL: URL, response: URLResponse) -> String? {
        let byMIME = [
            "video/mp4": "mp4", "video/quicktime": "mov", "video/x-m4v": "m4v",
            "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/mpeg": "mp3",
            "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav",
            "audio/flac": "flac", "audio/x-caf": "caf",
            "image/png": "png", "image/jpeg": "jpg", "image/heic": "heic",
            "image/heif": "heif", "image/webp": "webp", "image/gif": "gif",
        ]
        if let mime = response.mimeType?.lowercased(), let ext = byMIME[mime] { return ext }
        let allowed = Set(byMIME.values).union(["jpeg", "aif", "aiff", "tif", "tiff", "avif"])
        for ext in [sourceURL.pathExtension.lowercased(), (response.suggestedFilename as NSString?)?.pathExtension.lowercased() ?? ""] {
            if allowed.contains(ext) { return ext }
        }
        return nil
    }

    static func importAsset(from file: URL, sourceURL: URL, response: URLResponse, project: ProjectDirectory) async throws -> MediaAsset {
        guard let ext = fileExtension(sourceURL: sourceURL, response: response) else { throw APIError.invalidResponse }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let named = directory.appendingPathComponent("download." + ext)
        try FileManager.default.moveItem(at: file, to: named)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: named)
        let id = "native-preview-" + fingerprint.hex
        let destination = project.originals.appendingPathComponent(id + "." + ext)
        try project.createIfNeeded()
        if FileManager.default.fileExists(atPath: destination.path) {
            if try SHA256Fingerprinter().fingerprint(file: destination) == fingerprint {
                return MediaAsset(id: id, relativePath: project.root.relativePath(to: destination), fingerprint: fingerprint)
            }
            // Only a corrupt, app-owned cache copy reaches this branch.
            try FileManager.default.removeItem(at: destination)
        }
        do { try FileManager.default.moveItem(at: named, to: destination) }
        catch {
            // Another editor can finish the same immutable download first.
            guard (try? SHA256Fingerprinter().fingerprint(file: destination)) == fingerprint else { throw error }
        }
        return MediaAsset(id: id, relativePath: project.root.relativePath(to: destination), fingerprint: fingerprint)
    }
}

/// Discardable, content-addressed downloads. The index stores no signed URLs;
/// generation and project identity scope every lookup. Device originals remain
/// in their original project and never enter this cache.
struct NativePreviewAssetCache {
    let project: ProjectDirectory
    private let namespace: String
    init(project: ProjectDirectory, directory: URL? = nil, jobID: UUID? = nil) {
        // Library opens can have no creation thread and a fresh local project ID.
        // The authenticated job is the stable identity across those sessions.
        namespace = jobID.map { "job:" + $0.uuidString } ?? project.root.standardizedFileURL.path
        let root = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("native-editor-preview", isDirectory: true)
        self.project = ProjectDirectory(root: root)
    }
    private func indexURL(_ key: String) -> URL {
        let digest = SHA256.hash(data: Data((namespace + "\n" + key).utf8)).map { String(format: "%02x", $0) }.joined()
        return project.root.appendingPathComponent("index", isDirectory: true).appendingPathComponent(digest + ".json")
    }
    func load(_ key: String) -> MediaAsset? {
        guard let data = try? Data(contentsOf: indexURL(key)),
              let asset = try? JSONDecoder().decode(MediaAsset.self, from: data),
              let fingerprint = asset.fingerprint,
              asset.id == "native-preview-" + fingerprint.hex else { return nil }
        let url = project.root.appendingPathComponent(asset.relativePath).standardizedFileURL
        guard url.deletingLastPathComponent() == project.originals.standardizedFileURL,
              url.deletingPathExtension().lastPathComponent == asset.id,
              (try? SHA256Fingerprinter().fingerprint(file: url)) == fingerprint else { return nil }
        return asset
    }
    func store(_ asset: MediaAsset, for key: String) throws {
        let url = indexURL(key)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(asset).write(to: url, options: .atomic)
    }
}

/// One resolver per editor session, backed by a shared discardable disk cache.
/// Gesture samples only use the resolved local files.
actor NativeEditorSourceResolver {
    private let project: ProjectDirectory
    private let downloads: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 600
        return URLSession(configuration: configuration)
    }()
    private let diskCache: NativePreviewAssetCache
    private var cached: [String: ResolvedEditorSource] = [:]

    init(project: ProjectDirectory, cacheDirectory: URL? = nil, jobID: UUID? = nil) {
        self.project = project
        diskCache = NativePreviewAssetCache(project: project, directory: cacheDirectory, jobID: jobID)
    }

    private func cachedDownload(key: String, id: String, clipIndex: Int = -1) -> ResolvedEditorSource? {
        if let value = cached[key], FileManager.default.fileExists(atPath: value.url.path) { return value }
        guard let asset = diskCache.load(key) else { return nil }
        let value = ResolvedEditorSource(clipIndex: clipIndex, mediaID: id, asset: asset,
            url: diskCache.project.root.appendingPathComponent(asset.relativePath))
        cached[key] = value
        return value
    }

    private func rememberDownload(_ value: ResolvedEditorSource, key: String) {
        cached[key] = value
        // A cache write failure must not make an already resolved asset unusable.
        try? diskCache.store(value.asset, for: key)
    }

    func resolveAudio(id: String, url sourceURL: URL, generation: String) async throws -> ResolvedEditorSource {
        guard sourceURL.scheme == "https", !id.isEmpty, !generation.isEmpty else { throw APIError.invalidResponse }
        let key = "audio:\(generation):\(id)"
        if let value = cachedDownload(key: key, id: id) { return value }
        let (file, response) = try await downloads.download(from: sourceURL)
        defer { try? FileManager.default.removeItem(at: file) }
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw APIError.requestFailed }
        try Task.checkCancellation()
        var asset = try await NativeDownloadedMedia.importAsset(from: file, sourceURL: sourceURL, response: response, project: diskCache.project)
        let url = diskCache.project.root.appendingPathComponent(asset.relativePath)
        let duration = try await AVURLAsset(url: url).load(.duration).seconds
        guard duration.isFinite, duration > 0 else { throw APIError.invalidResponse }
        asset.duration = duration
        let resolved = ResolvedEditorSource(clipIndex: -1, mediaID: id, asset: asset, url: url)
        rememberDownload(resolved, key: key)
        return resolved
    }

    func resolveMedia(id: String, url sourceURL: URL, generation: String) async throws -> ResolvedEditorSource {
        guard sourceURL.scheme == "https", !id.isEmpty, !generation.isEmpty else { throw APIError.invalidResponse }
        let key = "media:\(generation):\(id)"
        if let value = cachedDownload(key: key, id: id) { return value }
        let (file, response) = try await downloads.download(from: sourceURL)
        defer { try? FileManager.default.removeItem(at: file) }
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw APIError.requestFailed }
        try Task.checkCancellation()
        var asset = try await NativeDownloadedMedia.importAsset(from: file, sourceURL: sourceURL, response: response, project: diskCache.project)
        let url = diskCache.project.root.appendingPathComponent(asset.relativePath)
        if let image = CGImageSourceCreateWithURL(url as CFURL, nil),
           let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [CFString: Any],
           let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
           let height = properties[kCGImagePropertyPixelHeight] as? NSNumber {
            let orientation = (properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
            asset.naturalSize = MediaSize(width: orientation >= 5 ? height.doubleValue : width.doubleValue,
                                          height: orientation >= 5 ? width.doubleValue : height.doubleValue)
        } else {
            let media = AVURLAsset(url: url)
            guard let track = try await media.loadTracks(withMediaType: .video).first else { throw APIError.invalidResponse }
            let size = try await track.load(.naturalSize)
            let transform = try await track.load(.preferredTransform)
            let rectangle = CGRect(origin: .zero, size: size).applying(transform)
            asset.naturalSize = MediaSize(width: abs(rectangle.width), height: abs(rectangle.height))
            asset.duration = try await media.load(.duration).seconds
        }
        guard let size = asset.naturalSize, size.width > 0, size.height > 0,
              size.width.isFinite, size.height.isFinite else { throw APIError.invalidResponse }
        let resolved = ResolvedEditorSource(clipIndex: -1, mediaID: id, asset: asset, url: url)
        rememberDownload(resolved, key: key)
        return resolved
    }

    private func decoderCompatible(_ source: ResolvedEditorSource) async throws -> ResolvedEditorSource {
        let url = try await NativeVP9Source.shared.prepare(source.url,
            cacheDirectory: diskCache.project.root.appendingPathComponent("decoded-sources", isDirectory: true), trace: { stage in
                #if DEBUG
                NativePreviewDiagnostics.record("source-prepare", fields: ["index": String(source.clipIndex), "step": stage])
                #endif
            })
        guard url != source.url else { return source }
        var asset = source.asset
        asset.fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
        asset.relativePath = "decoded-sources/" + url.lastPathComponent
        return ResolvedEditorSource(clipIndex: source.clipIndex, mediaID: source.mediaID, asset: asset, url: url)
    }

    func resolve(_ pool: NativeEditorSourcePool, generation: String, requiredIndices: Set<Int>? = nil) async throws -> [Int: ResolvedEditorSource] {
        guard !generation.isEmpty, pool.baseGeneration == generation else { throw APIError.conflict }
        guard Set(pool.clips.map(\.clipIndex)).count == pool.clips.count else { throw APIError.invalidResponse }
        var result: [Int: ResolvedEditorSource] = [:]
        let required = requiredIndices ?? Set(pool.clips.map(\.clipIndex))
        guard required.isSubset(of: Set(pool.clips.map(\.clipIndex))) else {
            throw MediaEngineError.missingAsset("source-pool")
        }
        for clip in pool.clips where required.contains(clip.clipIndex) {
            try Task.checkCancellation()
            guard let source = clip.nativeSource, !source.mediaID.isEmpty else {
                throw MediaEngineError.missingAsset("clip-\(clip.clipIndex)")
            }
            #if DEBUG
            NativePreviewDiagnostics.record("source-resolve", fields: ["index": String(clip.clipIndex)])
            #endif
            let key = "\(generation):\(clip.clipIndex):\(source.mediaID)"
            if !source.localRequired, let previous = cachedDownload(key: key, id: source.mediaID, clipIndex: clip.clipIndex) {
                result[clip.clipIndex] = try await decoderCompatible(previous)
                continue
            }
            var asset: MediaAsset
            let url: URL
            if source.localRequired {
                guard source.sourceURL == nil, let descriptor = source.original else { throw APIError.invalidResponse }
                let store = SourceAssetStore(project: project)
                let files = try store.resolve(mediaIDs: [source.mediaID])
                guard let originalURL = files[source.mediaID],
                      let binding = try store.bindings().first(where: { $0.mediaID == source.mediaID }) else {
                    throw SourceAssetError.missingOriginal(source.mediaID)
                }
                let fingerprint = try SHA256Fingerprinter().fingerprint(file: originalURL)
                guard fingerprint.hex == descriptor.sha256, fingerprint.byteCount == descriptor.byteCount else {
                    throw SourceAssetError.changedOriginal(source.mediaID)
                }
                asset = binding.original
                url = originalURL
            } else {
                guard let sourceURL = source.sourceURL, sourceURL.scheme == "https", source.original == nil else {
                    throw APIError.invalidResponse
                }
                #if DEBUG
                NativePreviewDiagnostics.record("source-download", fields: ["index": String(clip.clipIndex)])
                #endif
                #if DEBUG
                let monitor = Task { [downloads] in
                    while !Task.isCancelled {
                        downloads.getAllTasks { tasks in
                            for task in tasks {
                                NativePreviewDiagnostics.record("source-transfer", fields: ["index": String(clip.clipIndex),
                                    "received": String(task.countOfBytesReceived), "expected": String(task.countOfBytesExpectedToReceive),
                                    "state": String(task.state.rawValue)])
                            }
                        }
                        try? await Task.sleep(for: .seconds(2))
                    }
                }
                defer { monitor.cancel() }
                #endif
                let (file, response) = try await downloads.download(from: sourceURL)
                #if DEBUG
                NativePreviewDiagnostics.record("source-downloaded", fields: ["index": String(clip.clipIndex)])
                #endif
                defer { try? FileManager.default.removeItem(at: file) }
                guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw APIError.requestFailed }
                try Task.checkCancellation()
                asset = try await NativeDownloadedMedia.importAsset(from: file, sourceURL: sourceURL, response: response, project: diskCache.project)
                url = diskCache.project.root.appendingPathComponent(asset.relativePath)
            }
            if asset.duration == nil, !NativeDownloadedMedia.isStillImage(url) {
                let duration = try await AVURLAsset(url: url).load(.duration).seconds
                guard duration.isFinite, duration > 0 else { throw APIError.invalidResponse }
                asset.duration = duration
            }
            let resolved = ResolvedEditorSource(clipIndex: clip.clipIndex, mediaID: source.mediaID, asset: asset, url: url)
            if source.localRequired { cached[key] = resolved }
            else { rememberDownload(resolved, key: key) }
            result[clip.clipIndex] = try await decoderCompatible(resolved)
        }
        return result
    }
}

struct NativeEditorMusicTrack: Decodable, Sendable {
    let id: String
    let title: String
    let previewAudioURL: URL?
    enum CodingKeys: String, CodingKey { case id, title, previewAudioURL = "preview_audio_url" }
}

extension KriaAPI {
    func editorMusicTracks() async throws -> [NativeEditorMusicTrack] {
        struct Envelope: Decodable { let tracks: [NativeEditorMusicTrack] }
        return try await request(path: "music-tracks", method: "GET", bodyData: nil, decode: Envelope.self).tracks
    }
}

#if DEBUG
/// Bounded device diagnostics; no URLs, tokens, content, or raw errors.
enum NativePreviewDiagnostics {
    private static let lock = NSLock()
    static func record(_ stage: String, fields: [String: String] = [:]) {
        lock.lock()
        defer { lock.unlock() }
        guard let root = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else { return }
        let url = root.appendingPathComponent("native-preview-diagnostics.json")
        var events = (try? Data(contentsOf: url)).flatMap { try? JSONDecoder().decode([[String: String]].self, from: $0) } ?? []
        events.append(fields.merging(["stage": stage, "time": ISO8601DateFormatter().string(from: Date())]) { _, value in value })
        if let data = try? JSONEncoder().encode(Array(events.suffix(40))) { try? data.write(to: url, options: .atomic) }
    }
    static func failure(_ stage: String, error: Error) {
        var fields = ["type": String(describing: type(of: error)), "domain": (error as NSError).domain, "code": String((error as NSError).code)]
        if let feature = error as? NativePreviewFeatureError { fields["feature"] = feature.feature }
        if let engine = error as? MediaEngineError {
            switch engine {
            case .unsupportedCapability: fields["engineCase"] = "unsupportedCapability"
            case .missingAsset: fields["engineCase"] = "missingAsset"
            default: fields["engineCase"] = "other"
            }
        }
        if case MediaEngineError.missingAsset(let id) = error {
            fields["assetCategory"] = id == "narration" ? "narration" : id.hasPrefix("sfx:") ? "sfx" : id.hasPrefix("overlay:") ? "overlay" : id.hasPrefix("visual:") ? "visual" : "source-or-music"
        }
        if let error = error as? APIError { fields["api"] = String(describing: error) }
        if case NativeEditorRenderError.unsupportedLane(let lane) = error { fields["unsupportedLane"] = lane }
        if case DecodingError.keyNotFound(let key, let context) = error {
            fields["key"] = key.stringValue
            fields["codingPath"] = context.codingPath.map(\.stringValue).joined(separator: ".")
        }
        record(stage, fields: fields)
    }
}
#endif

#if DEBUG
@MainActor enum NativeLibraryAudit {
    static func run(api: KriaAPI) async {
        let previousIdleTimerState = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = previousIdleTimerState }
        var rows: [[String: String]] = []
        func count(_ value: JSONValue?) -> Int {
            if case .array(let array) = value { return array.count }
            return 0
        }
        func object(_ value: JSONValue?) -> [String: JSONValue] {
            if case .object(let value) = value { return value }
            return [:]
        }
        func save() {
            guard let root = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first,
                  let data = try? JSONEncoder().encode(rows) else { return }
            try? data.write(to: root.appendingPathComponent("native-library-audit.json"), options: .atomic)
        }
        do {
            if ProcessInfo.processInfo.arguments.contains("-native-clock-audit") {
                let url = FileManager.default.temporaryDirectory.appendingPathComponent("clock-test-\(UUID().uuidString).png")
                defer { try? FileManager.default.removeItem(at: url) }
                let image = UIGraphicsImageRenderer(size: CGSize(width: 96, height: 160)).image { context in
                    UIColor.red.setFill(); context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
                }
                try image.pngData()!.write(to: url)
                do {
                    let recipe = KriaMediaEngine.EditRecipe(canvas: .init(width: 96, height: 160),
                        assets: [MediaAsset(id: "photo", relativePath: "photo.png")],
                        tracks: [TimelineTrack(id: "photos", kind: .video, clips: [TimelineClip(id: "photo", sourceAssetID: "photo", sourceDuration: 2)])])
                    let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["photo": url])
                    let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
                    generator.videoComposition = preview.playerItem.videoComposition
                    let frame = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600))
                    rows.append(["stage": "clock-frame", "frame": "\(frame.image.width)x\(frame.image.height)"])
                } catch {
                    let detail = error as NSError
                    rows.append(["stage": "clock-frame-failure", "code": String(detail.code),
                        "underlyingCode": String((detail.userInfo[NSUnderlyingErrorKey] as? NSError)?.code ?? 0)])
                }
                rows.append(["stage": "complete"]); save(); return
            }
            if ProcessInfo.processInfo.arguments.contains("-native-library-link-audit") {
                struct Links: Decodable {
                    struct Row: Decodable {
                        let id: String
                        let mode: String
                        let content_plan_item_id: String?
                    }
                    let jobs: [Row]
                }
                let links = try await api.request(path: "me/jobs", method: "GET", bodyData: nil, decode: Links.self)
                for job in links.jobs {
                    rows.append(["stage": "editor-link", "job": job.id,
                        "mode": job.mode, "hasPlanItem": String(job.content_plan_item_id != nil)])
                }
                rows.append(["stage": "complete"]); save(); return
            }
            let library = try await api.library()
            let projects = try await api.projects()
            let allJobs = Set(library.map(\.id) + projects.compactMap(\.activeJobID))
            let args = ProcessInfo.processInfo.arguments
            let requested = args.firstIndex(of: "-native-audit-job").flatMap { args.indices.contains($0 + 1) ? UUID(uuidString: args[$0 + 1]) : nil }
            let requestedTitle = args.firstIndex(of: "-native-audit-project-title").flatMap { args.indices.contains($0 + 1) ? args[$0 + 1] : nil }
            let matchedProjects = requestedTitle.map { title in projects.filter { $0.title == title || (title.count >= 20 && $0.title.hasPrefix(title)) } } ?? []
            let requestedIndex = args.firstIndex(of: "-native-audit-project-index").flatMap { args.indices.contains($0 + 1) ? Int(args[$0 + 1]) : nil }
            let indexedJobs = requestedIndex.flatMap { index in projects.indices.contains(index - 1) ? projects[index - 1].activeJobID : nil }
            let jobs = requestedIndex != nil ? Set(indexedJobs.map { [$0] } ?? []) : (requestedTitle != nil ? Set(matchedProjects.count == 1 ? matchedProjects.compactMap(\.activeJobID) : []) : (requested.map { allJobs.intersection([$0]) } ?? allJobs))
            rows.append(["stage": "inventory", "jobs": String(jobs.count), "requestedJobs": jobs.map(\.uuidString).sorted().joined(separator: ","),
                "readyLibraryJobs": String(library.filter { $0.status == .ready }.count)])
            save()
            for job in jobs.sorted(by: { $0.uuidString < $1.uuidString }) {
                rows.append(["stage": "library-status", "job": job.uuidString,
                    "status": library.first(where: { $0.id == job })?.status.rawValue ?? "project-only"])
                save()
                do {
                    let variants: [[String: JSONValue]] = args.contains("-native-window-audit") ? [] : ((try? await api.editorVariants(jobID: job)) ?? [])
                    for variant in variants where variant["render_status"] == .string("ready") {
                        guard let id = variant["variant_id"]?.stringValue else { continue }
                        var row = ["job": job.uuidString, "variant": id,
                            "archetype": variant["resolved_archetype"]?.stringValue ?? "missing",
                            "aiSlots": String(count(object(variant["ai_timeline"])["slots"])),
                            "userSlots": String(count(object(variant["user_timeline"])["slots"])),
                            "text": String(count(variant["text_elements"])),
                            "captions": String(count(variant["caption_cues"])),
                            "base": String(variant["base_video_url"]?.stringValue != nil),
                            "music": String(variant["music_track_id"]?.stringValue != nil),
                            "timelineEditable": String(object(variant["editor_capabilities"])["timeline"] == .bool(true))]
                        row["musicURL"] = String(variant["music_preview_url"]?.stringValue != nil)
                        if case .array(let cues) = variant["caption_cues"], let first = cues.first {
                            row["captionKeys"] = object(first).keys.sorted().joined(separator: ",")
                        }
                        for lane in ["media_overlays", "sound_effects", "visual_blocks", "motion_scenes", "camera_effects", "narrated_timings", "story_timeline"] { row[lane] = String(count(variant[lane])) }
                        if case .number(let duration) = variant["duration_s"] { row["duration"] = String(duration) }
                        do {
                        let timeline: [String: JSONValue] = try await api.request(path: "generative-jobs/\(job.uuidString)/variants/\(id)/timeline", method: "GET", bodyData: nil, decode: [String: JSONValue].self)
                        func counts(_ key: String, _ field: String) -> String {
                            guard case .array(let values) = timeline[key] else { return "none" }
                            let groups = Dictionary(grouping: values.map { object($0)[field]?.stringValue ?? "missing" }, by: { $0 })
                            return groups.keys.sorted().map { "\($0):\(groups[$0]!.count)" }.joined(separator: ",")
                        }
                        row["sourceKinds"] = counts("clips", "kind")
                        row["layouts"] = counts("slots", "layout")
                        row["looks"] = counts("slots", "look_preset")
                        row["transitions"] = counts("slots", "transition_after")
                        row["nativeAssets"] = String(count(timeline["native_assets"]))
                        row["apiSlots"] = String(count(timeline["slots"]))
                        row["sources"] = String(count(timeline["clips"]))
                        row["timelineReason"] = timeline["reason"]?.stringValue ?? "none"
                        } catch {
                            row["timelineErrorType"] = String(describing: type(of: error))
                            row["timelineErrorCode"] = String((error as NSError).code)
                        }
                        rows.append(row)
                        save()
                    }
                    if ProcessInfo.processInfo.arguments.contains("-native-library-preview-audit") {
                        let editor = NativeEditorSession()
                        if let project = projects.first(where: { $0.activeJobID == job }) {
                            await editor.load(project: project, api: api)
                        } else {
                            await editor.load(libraryJobID: job, api: api)
                        }
                        rows.append(["job": job.uuidString, "stage": "preview",
                            "state": String(describing: editor.sourcePreviewState),
                            "load": String(describing: editor.loadState),
                            "cuts": String(editor.document.clips.count),
                            "looks": Array(Set(editor.document.clips.compactMap(\.lookPreset))).sorted().joined(separator: ",")])
                        save()
                        if case .failed = editor.sourcePreviewState,
                           let cache = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first,
                           let data = try? Data(contentsOf: cache.appendingPathComponent("native-preview-diagnostics.json")),
                           let events = try? JSONDecoder().decode([[String: String]].self, from: data),
                           let failure = events.last(where: { $0["stage"] == "preview-failure" }) {
                            var row = ["job": job.uuidString, "stage": "preview-error"]
                            for key in ["feature", "engineCase", "code", "domain", "unsupportedLane"] { row[key] = failure[key] }
                            rows.append(row); save()
                        }
                        if args.contains("-native-window-audit"), editor.hasSourcePreview {
                            rows += await editor.auditPreviewWindow()
                            save()
                        }
                        if args.contains("-native-frame-audit"), editor.hasSourcePreview,
                           let item = editor.player?.currentItem {
                            do {
                                let generator = AVAssetImageGenerator(asset: item.asset)
                                generator.videoComposition = item.videoComposition
                                generator.appliesPreferredTrackTransform = true
                                var frames: [String] = []
                                for second in [min(0.25, editor.duration / 2), editor.duration / 2, max(0, editor.duration - 0.1)] {
                                    let frame = try await generator.image(at: CMTime(seconds: second, preferredTimescale: 600))
                                    frames.append("\(frame.image.width)x\(frame.image.height)")
                                }
                                rows.append(["job": job.uuidString, "stage": "decoded-frames", "frames": frames.joined(separator: ",")])
                            } catch {
                                let detail = error as NSError
                                let underlying = detail.userInfo[NSUnderlyingErrorKey] as? NSError
                                let tracks = (try? await item.asset.loadTracks(withMediaType: .video)) ?? []
                                rows.append(["job": job.uuidString, "stage": "decoded-frame-failure",
                                    "errorType": String(describing: type(of: error)), "errorCode": String(detail.code),
                                    "underlyingCode": String(underlying?.code ?? 0),
                                    "underlyingDomain": underlying?.domain ?? "none",
                                    "mediaType": detail.userInfo[AVErrorMediaTypeKey] as? String ?? "none",
                                    "mediaSubtype": (detail.userInfo[AVErrorMediaSubTypeKey] as? NSNumber)?.stringValue ?? "none",
                                    "videoTracks": String(tracks.count)])
                            }
                            save()
                            if args.contains("-native-source-frame-audit") {
                                var seen = Set<URL>()
                                let tracks = (try? await item.asset.loadTracks(withMediaType: .video)) ?? []
                                for track in tracks {
                                    for segment in (track as? AVCompositionTrack)?.segments ?? [] {
                                        guard let url = segment.sourceURL, seen.insert(url).inserted else { continue }
                                        var row = ["stage": "source-decoder", "index": String(seen.count - 1)]
                                        do {
                                            let asset = AVURLAsset(url: url)
                                            if let source = try await asset.loadTracks(withMediaType: .video).first {
                                                let formats = try await source.load(.formatDescriptions)
                                                row["codecs"] = formats.map { String(CMFormatDescriptionGetMediaSubType($0)) }.joined(separator: ",")
                                                let size = try await source.load(.naturalSize)
                                                row["size"] = "\(Int(size.width))x\(Int(size.height))"
                                            }
                                            let frame = try await AVAssetImageGenerator(asset: asset).image(at: .zero)
                                            row["frame"] = "\(frame.image.width)x\(frame.image.height)"
                                        } catch {
                                            row["errorCode"] = String((error as NSError).code)
                                            row["underlyingCode"] = String(((error as NSError).userInfo[NSUnderlyingErrorKey] as? NSError)?.code ?? 0)
                                        }
                                        rows.append(row); save()
                                    }
                                }
                            }
                        }
                        editor.player?.pause()
                        editor.player?.replaceCurrentItem(with: nil)
                    }
                } catch {
                    rows.append(["job": job.uuidString, "errorType": String(describing: type(of: error)), "errorCode": String((error as NSError).code)])
                    save()
                }
            }
            rows.append(["stage": "complete"])
            save()
        } catch { NativePreviewDiagnostics.failure("library-audit-failure", error: error) }
    }
}
#endif
