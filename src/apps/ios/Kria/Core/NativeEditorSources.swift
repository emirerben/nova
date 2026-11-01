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

/// The additive, device-editor source admission contract.  This deliberately
/// lives beside the source-pool reader rather than the creation attachment
/// APIs: an admitted source is private to one approved variant and must never
/// mutate the creation thread or its approval.
struct EditorSourceRegistrationTarget: Codable, Sendable, Equatable {
    enum SourceKind: String, Codable, Sendable { case footage, visual }
    let itemID: String
    let variantID: String
    let clientImportID: UUID
    let baseGeneration: String
    let guidedRevisionNumber: Int
    let sourceKind: SourceKind
}

struct EditorSourceRegistrationResponse: Codable, Sendable, Equatable {
    let importID: UUID
    let status: String
    let sourceID: String
    let sourceIndex: Int?
    let source: [String: JSONValue]?
    let error: String?
    let reasonCode: String?
    let retryable: Bool

    enum CodingKeys: String, CodingKey {
        case importID = "import_id", status, sourceID = "source_id", sourceIndex = "source_index"
        case source, error, reasonCode = "reason_code", retryable
    }

    var isTerminal: Bool { status == "ready" || status == "failed" }
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
        guard document.editorState != "empty" else { return document }
        guard document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }) else { return document }
        var result = document
        result.clips = [.init(id: "native-composite-base", clipIndex: 0, inS: 0, durationS: duration,
            raw: ["native_composite_source": .bool(true), "source_duration_s": .number(duration)])]
        return result
    }
}

/// A phone-rendered Talking (subtitled) edit plays its one source clip, with
/// its own audio, and the server keeps no timeline slots for it. Without
/// that clip the preview has no video track: photo cards once made that recipe
/// validate as a black canvas (the compiler now rejects it, `missingVideoTrack`,
/// so it falls back to the finished MP4). The source becomes the same locked
/// composite clip a talking-head base uses, so the preview stays live.
///
/// Speech cleanup (KRI-232): the server's cues, words, cards and sounds are all
/// on the CUT timeline, and its recipe plays one hard-cut clip per kept
/// segment (`compile_phone_subtitled_plan`). The source is split the same way,
/// from `silence_cut.removed` (the exact complement of the kept segments), so
/// captions stay on the words instead of drifting early by every removed span.
///
/// Only when the edit's cards and sounds are editable (`lanesEditable`): with
/// them closed the server doesn't send those lanes, so a live preview would
/// drop what the finished MP4 shows.
///
/// Several videos (KRI-719): the server plays every speaker back to back on
/// the main track, in the order of the variant's `phone_talking_multi_clip`
/// receipt (`speaker_media_ids`). The first speaker carries any cut; the rest
/// play in full. The receipt's `cutaways` windows (footage clips shown full
/// frame, muted, over the speakers) are drawn on top as locked preview
/// clips: they never enter the document, so nothing can select, move or
/// Save them (Save keeps them from the pinned recipe, `cutaways_from_recipe`).
enum NativePhoneTalkingSource {
    static let slotID = "native-phone-talking-source"
    static let multiClipReceiptField = "phone_talking_multi_clip"

    struct Speaker: Equatable, Sendable {
        let clipIndex: Int
        let duration: Double
    }

    /// One muted full-frame footage window (KRI-719), on the edit's timeline.
    /// Mirrors a `talking-head-cutaways` recipe clip: the footage plays from
    /// `sourceStart` for the whole window.
    struct Cutaway: Equatable, Sendable {
        let clipIndex: Int
        let start: Double
        let end: Double
        var sourceStart: Double = 0
    }

    /// Source-pool cutaways from the receipt's `cutaways` rows
    /// (`{media_id, start_s, end_s}`), sorted by start. Empty when there is no
    /// receipt or it placed no footage. Nil when any row is malformed, can't be
    /// found in the pool, reuses a speaker, or overlaps another: a preview that
    /// silently dropped one would show the speaker where the MP4 cuts away, so
    /// the caller falls back to the finished MP4 instead.
    static func cutaways(variant: [String: JSONValue], pool: NativeEditorSourcePool,
                         speakerIndices: [Int]) -> [Cutaway]? {
        guard let receipt = variant[multiClipReceiptField]?.objectValue else { return [] }
        guard let rowsValue = receipt["cutaways"] else { return [] }
        guard case .array(let rows) = rowsValue else { return nil }
        var result: [Cutaway] = []
        for row in rows {
            guard let fields = row.objectValue, let mediaID = fields["media_id"]?.stringValue,
                  let start = fields["start_s"]?.numberValue, let end = fields["end_s"]?.numberValue,
                  start.isFinite, end.isFinite, start >= 0, end > start,
                  let clip = pool.clips.first(where: { $0.nativeSource?.mediaID == mediaID }),
                  !speakerIndices.contains(clip.clipIndex) else { return nil }
            result.append(Cutaway(clipIndex: clip.clipIndex, start: start, end: end))
        }
        result.sort { ($0.start, $0.end) < ($1.start, $1.end) }
        for (previous, next) in zip(result, result.dropFirst()) where next.start < previous.end - 1e-6 { return nil }
        return result
    }

    /// Source spans speech cleanup removed from this variant, sorted. Empty
    /// when no cleanup was applied.
    static func removedSpans(variant: [String: JSONValue]) -> [(start: Double, end: Double)] {
        guard case .array(let rows)? = variant["silence_cut"]?.objectValue?["removed"] else { return [] }
        return rows.compactMap { row -> (start: Double, end: Double)? in
            guard let span = row.objectValue, let start = span["start_s"]?.numberValue,
                  let end = span["end_s"]?.numberValue, start.isFinite, end.isFinite, end > start else { return nil }
            return (start, end)
        }.sorted { $0.start < $1.start }
    }

    /// Kept source segments: `[0, duration]` minus `removed`.
    static func keptSegments(duration: Double, removed: [(start: Double, end: Double)]) -> [(start: Double, end: Double)] {
        var kept: [(start: Double, end: Double)] = []
        var cursor = 0.0
        for span in removed {
            let start = min(max(span.start, 0), duration), end = min(max(span.end, 0), duration)
            if start > cursor { kept.append((cursor, start)) }
            cursor = max(cursor, end)
        }
        if duration > cursor { kept.append((cursor, duration)) }
        // Sub-millisecond slivers are rounding residue from the persisted
        // 3-decimal summary, not footage.
        return kept.filter { $0.end - $0.start > 0.001 }
    }

    /// Source-pool indices of the speaker clips, in play order: the one pool
    /// clip, or each `speaker_media_ids` entry of a multi-clip receipt. Nil
    /// (finished-MP4 fallback) when the edit isn't a live phone Talking edit
    /// or any speaker can't be found in the pool.
    static func sourceIndices(variant: [String: JSONValue], document: EditorDocument,
                              pool: NativeEditorSourcePool, lanesEditable: Bool) -> [Int]? {
        // The source-preview compiler does not hydrate the pinned recipe's audio
        // fades or processed track. Use the finished export to preserve exact cuts
        // and noise treatment until that compiler supports the complete recipe.
        if case .array(let cuts)? = variant["explicit_speech_cuts"],
           cuts.contains(where: { $0.objectValue?["status"] == .string("applied") }) {
            return nil
        }
        if variant["engine_noise_reduction"]?.objectValue?["status"] == .string("applied") {
            return nil
        }
        guard lanesEditable,
              variant["resolved_archetype"] == .string("subtitled"),
              variant["render_destination"] == .string("device"),
              document.capabilities["timeline"]?.editable != true,
              document.tombstones.isEmpty,
              document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }) else { return nil }
        guard let receipt = variant[multiClipReceiptField] else {
            return pool.clips.count == 1 ? [pool.clips[0].clipIndex] : nil
        }
        guard case .array(let rows)? = receipt.objectValue?["speaker_media_ids"], !rows.isEmpty else { return nil }
        var indices: [Int] = []
        for row in rows {
            guard let mediaID = row.stringValue,
                  let clip = pool.clips.first(where: { $0.nativeSource?.mediaID == mediaID }),
                  !indices.contains(clip.clipIndex) else { return nil }
            indices.append(clip.clipIndex)
        }
        return indices
    }

    static func hydrate(_ document: EditorDocument, clipIndex: Int, duration: Double,
                        removed: [(start: Double, end: Double)] = []) throws -> EditorDocument {
        try hydrate(document, speakers: [.init(clipIndex: clipIndex, duration: duration)], removed: removed)
    }

    /// `removed` applies to the first speaker only (the server cuts only it);
    /// every later speaker plays in full right after it.
    static func hydrate(_ document: EditorDocument, speakers: [Speaker],
                        removed: [(start: Double, end: Double)] = []) throws -> EditorDocument {
        guard let first = speakers.first,
              speakers.allSatisfy({ $0.duration.isFinite && $0.duration > 0 }) else { throw APIError.invalidResponse }
        guard document.editorState != "empty" else { return document }
        guard document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }) else { return document }
        let kept = keptSegments(duration: first.duration, removed: removed)
        guard !kept.isEmpty else { throw RecipeError.invalidTimeline }
        let segments = kept.map { (speaker: first, start: $0.start, end: $0.end) }
            + speakers.dropFirst().map { (speaker: $0, start: 0.0, end: $0.duration) }
        var result = document
        result.clips = segments.enumerated().map { index, segment in
            .init(id: index == 0 ? slotID : slotID + "-\(index)", clipIndex: segment.speaker.clipIndex, inS: segment.start,
                  durationS: segment.end - segment.start,
                  raw: ["native_composite_source": .bool(true), "source_duration_s": .number(segment.speaker.duration)])
        }
        return result
    }
}

/// Defence in depth for phone-rendered Narrated / Voiceover edits. The server
/// should project their clip slots, but older videos (and any projection gap)
/// arrive with an empty timeline, which stretches the "Kria outro" placeholder
/// over the whole strip and makes the compiler reject the preview
/// (`missingVideoTrack`). The pinned device recipe's video track is the
/// authoritative cut list, so rebuild locked source clips from it. Like the
/// Talking source, hydration is server state, not an edit.
enum NativePhoneNarratedSource {
    static let slotID = "native-phone-narrated-source"

    struct Seed: Equatable, Sendable {
        let clipID: String
        let clipIndex: Int
        let inS: Double
        let durationS: Double
        let rate: Double
        let sourceSpanS: Double
    }

    static func applies(variant: [String: JSONValue], document: EditorDocument) -> Bool {
        guard ["narrated", "voiceover"].contains(variant["resolved_archetype"]?.stringValue ?? ""),
              variant["render_destination"] == .string("device"),
              document.editorState != "empty", document.tombstones.isEmpty,
              document.capabilities["timeline"]?.editable != true else { return false }
        return document.clips.isEmpty || document.clips.allSatisfy { $0.raw["native_composite_source"] == .bool(true) }
    }

    /// Video-track clips of `recipe`, mapped onto source-pool indices through
    /// the recipe's original media ids. Empty when any clip cannot be mapped:
    /// a partial timeline would silently drop footage from the preview.
    static func seeds(recipe: KriaMediaEngine.EditRecipe, pool: NativeEditorSourcePool) -> [Seed] {
        var originals: [String: String] = [:]
        for reference in recipe.assetManifest?.assets ?? [] {
            if case .original(let mediaID) = reference.source { originals[reference.id] = mediaID }
        }
        func index(for assetID: String) -> Int? {
            let mediaID = originals[assetID] ?? assetID
            if let match = pool.clips.first(where: { $0.nativeSource?.mediaID == mediaID }) { return match.clipIndex }
            for prefix in ["clip-", "clip_", "source-"] where mediaID.hasPrefix(prefix) {
                if let number = Int(mediaID.dropFirst(prefix.count)), pool.clips.contains(where: { $0.clipIndex == number }) { return number }
            }
            return nil
        }
        let clips = recipe.tracks.filter { $0.kind == .video }.flatMap(\.clips).sorted { $0.timelineStart < $1.timelineStart }
        var result: [Seed] = []
        for (position, clip) in clips.enumerated() {
            guard let clipIndex = index(for: clip.sourceAssetID) else { return [] }
            // Abut clips: a recipe overlap must not lengthen the editable strip.
            let next = clips.indices.contains(position + 1) ? clips[position + 1].timelineStart : nil
            let duration = next.map { $0 - clip.timelineStart } ?? clip.duration
            guard duration.isFinite, duration > 0.001 else { continue }
            result.append(Seed(clipID: clip.id, clipIndex: clipIndex, inS: clip.sourceStart, durationS: duration,
                               rate: clip.rate, sourceSpanS: clip.sourceDuration))
        }
        return result
    }

    static func hydrate(_ document: EditorDocument, seeds: [Seed], sources: [Int: ResolvedEditorSource]) -> EditorDocument {
        guard !seeds.isEmpty, document.editorState != "empty",
              document.clips.isEmpty || document.clips.allSatisfy({ $0.raw["native_composite_source"] == .bool(true) }) else { return document }
        var result = document
        result.clips = seeds.enumerated().map { index, seed in
            var raw: [String: JSONValue] = ["native_composite_source": .bool(true), "playback_rate": .number(seed.rate)]
            var span = seed.sourceSpanS
            if let total = sources[seed.clipIndex]?.asset.duration, total.isFinite, total > 0 {
                raw["source_duration_s"] = .number(total)
                span = min(span, max(0.05, total - seed.inS))
            }
            raw["native_source_span_s"] = .number(span)
            return .init(id: index == 0 ? slotID : slotID + "-\(index)", clipIndex: seed.clipIndex, inS: seed.inS,
                         durationS: seed.durationS, raw: raw)
        }
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
            // A removed slot plays nothing, so it needs no source and no span.
            if slot.removed { continue }
            guard let source = sources[slot.clipIndex] else {
                // KRI-813: only history documents (clean/undo/redo/chat-staged) reach here -- the resolver already
                // throws for a current-document source it cannot load. Leave that slot unhydrated, never a dead-end recovery.
                NativePreviewDiagnostics.record("rate-guard", fields: ["guard": "narrated-unresolved-history-source", "index": String(slot.clipIndex)])
                continue
            }
            guard let total = source.asset.duration, let target = slot.durationS, target > 0,
                  total > NarratedRateGate.minimumSourceSpanS else {
                // As on the server: a source with no usable footage is an honest timeline error.
                NativePreviewDiagnostics.record("rate-guard", fields: [
                    "guard": "narrated-short-source", "index": String(slot.clipIndex),
                    "duration": String(source.asset.duration ?? -1), "target": String(slot.durationS ?? -1)])
                throw RecipeError.invalidTimeline
            }
            let start = total - slot.inS <= 0.05 ? 0 : max(0, slot.inS)
            result.clips[index].inS = start
            result.clips[index].raw["source_duration_s"] = .number(total)
            result.clips[index].raw["native_source_span_s"] = .number(span(target: target, available: total - start))
        }
        return result
    }

    /// Source seconds a narrated slot of `target` output seconds plays: all of
    /// it at 1x when the footage covers it, else the remaining footage (minus
    /// the EOF guard) slowed to fill it -- the server's `_fit_step_window`.
    static func span(target: Double, available: Double) -> Double {
        available >= target ? target : max(0.05, available - 0.05)
    }
}

struct ResolvedEditorSource: Sendable {
    var preserveAlpha = false
    let clipIndex: Int
    let mediaID: String
    let asset: MediaAsset
    let url: URL
    /// The Visuals-pool storage generation the bytes belong to, for a source that is a pool visual
    /// (the Talk-to-camera background, KRI-1118). Nil for every other source.
    var visualGeneration: String? = nil
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
              url.deletingPathExtension().lastPathComponent == asset.id else { return nil }
        // The file was verified when it was cached, so a file whose size and modification time are still what they were
        // then is trusted. Hashing the whole file on every open grows with the size of the footage. Anything that
        // changed (or an entry from before this record existed) is hashed as before, and the record is refreshed.
        if let record = fileRecord(for: key), record.matches(url, byteCount: fingerprint.byteCount) { return asset }
        guard (try? SHA256Fingerprinter().fingerprint(file: url)) == fingerprint else { return nil }
        try? writeFileRecord(for: key, file: url)
        return asset
    }
    func store(_ asset: MediaAsset, for key: String) throws {
        let url = indexURL(key)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(asset).write(to: url, options: .atomic)
        try? writeFileRecord(for: key, file: project.root.appendingPathComponent(asset.relativePath))
    }

    /// Size and modification time of the cached file at the moment it was verified.
    private struct FileRecord: Codable {
        let byteCount: Int64
        let modified: TimeInterval
        func matches(_ file: URL, byteCount expected: Int64) -> Bool {
            guard let attributes = try? FileManager.default.attributesOfItem(atPath: file.path),
                  let size = (attributes[.size] as? NSNumber)?.int64Value,
                  let date = attributes[.modificationDate] as? Date else { return false }
            return size == expected && byteCount == expected && date.timeIntervalSince1970 == modified
        }
    }
    private func recordURL(_ key: String) -> URL { indexURL(key).deletingPathExtension().appendingPathExtension("file.json") }
    private func fileRecord(for key: String) -> FileRecord? {
        (try? Data(contentsOf: recordURL(key))).flatMap { try? JSONDecoder().decode(FileRecord.self, from: $0) }
    }
    private func writeFileRecord(for key: String, file: URL) throws {
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        guard let size = (attributes[.size] as? NSNumber)?.int64Value,
              let date = attributes[.modificationDate] as? Date else { return }
        try JSONEncoder().encode(FileRecord(byteCount: size, modified: date.timeIntervalSince1970)).write(to: recordURL(key), options: .atomic)
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
        #if DEBUG
        // This session is private to the resolver, so a fixture can only reach it from here.
        if PerfFixture.editorFixture { configuration.protocolClasses = [PerfSourceURLProtocol.self] }
        #endif
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
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard response.statusCode == 200 else { throw APIError.requestFailed(status: response.statusCode) }
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
        if let value = cachedDownload(key: key, id: id) { return try await decoderCompatible(value) }
        let (file, response) = try await downloads.download(from: sourceURL)
        defer { try? FileManager.default.removeItem(at: file) }
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard response.statusCode == 200 else { throw APIError.requestFailed(status: response.statusCode) }
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
        return try await decoderCompatible(resolved)
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
        // A converted proxy is reused across opens; its fingerprint is remembered by size and modification time.
        asset.fingerprint = try FileFingerprintCache(
            directory: diskCache.project.root.appendingPathComponent("decoded-sources/fingerprints", isDirectory: true)
        ).fingerprint(file: url)
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
        let clips = pool.clips.filter { required.contains($0.clipIndex) }
        // Several clips download at once (it was one after another, so three clips cost three round trips and three
        // transfers in series). The actor interleaves them at each await; the work each does is the same as before.
        try await withThrowingTaskGroup(of: (Int, ResolvedEditorSource).self) { group in
            var remaining = clips.makeIterator()
            var running = 0
            while running < Self.clipConcurrency, let clip = remaining.next() {
                group.addTask { (clip.clipIndex, try await self.resolveClip(clip, generation: generation)) }
                running += 1
            }
            while let (index, source) = try await group.next() {
                result[index] = source
                if let clip = remaining.next() {
                    group.addTask { (clip.clipIndex, try await self.resolveClip(clip, generation: generation)) }
                }
            }
        }
        return result
    }

    /// How many source clips resolve (download, hash, probe) at the same time.
    private static let clipConcurrency = 3

    /// One clip's source: from this session's memory, the on-disk cache, a download, or the device's own original.
    private func resolveClip(_ clip: NativeEditorSourcePool.Clip, generation: String) async throws -> ResolvedEditorSource {
        try Task.checkCancellation()
        guard let source = clip.nativeSource, !source.mediaID.isEmpty else {
            throw MediaEngineError.missingAsset("clip-\(clip.clipIndex)")
        }
        #if DEBUG
        NativePreviewDiagnostics.record("source-resolve", fields: ["index": String(clip.clipIndex)])
        #endif
        let key = "\(generation):\(clip.clipIndex):\(source.mediaID)"
        if !source.localRequired, let previous = cachedDownload(key: key, id: source.mediaID, clipIndex: clip.clipIndex) {
            return try await displaySized(decoderCompatible(previous), original: nil)
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
            guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
            guard response.statusCode == 200 else { throw APIError.requestFailed(status: response.statusCode) }
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
        return try await displaySized(decoderCompatible(resolved), original: source.original)
    }

    /// The clip's displayed (oriented) picture size, which the black-bars letterbox needs: a source without one
    /// is cover-filled instead, and the copy saved from the editor is cropped (KRI-1010). A device original states
    /// it in its approved descriptor, the same input the server's `display_dims` scales by; a download (cached
    /// before this existed or not) is probed like `resolveMedia`. Stills keep no size, and a failed probe leaves
    /// none, as before.
    private func displaySized(_ source: ResolvedEditorSource, original: OriginalMediaDescriptor?) async -> ResolvedEditorSource {
        guard source.asset.naturalSize == nil, !NativeDownloadedMedia.isStillImage(source.url) else { return source }
        var asset = source.asset
        if let width = original?.width, let height = original?.height, width > 0, height > 0 {
            let degrees = ((original?.orientationDegrees ?? 0) % 360 + 360) % 360
            let turned = degrees == 90 || degrees == 270
            asset.naturalSize = MediaSize(width: Double(turned ? height : width), height: Double(turned ? width : height))
        } else if let track = try? await AVURLAsset(url: source.url).loadTracks(withMediaType: .video).first,
                  let size = try? await track.load(.naturalSize), let transform = try? await track.load(.preferredTransform) {
            let rectangle = CGRect(origin: .zero, size: size).applying(transform)
            asset.naturalSize = MediaSize(width: abs(rectangle.width), height: abs(rectangle.height))
        }
        return ResolvedEditorSource(preserveAlpha: source.preserveAlpha, clipIndex: source.clipIndex, mediaID: source.mediaID,
                                    asset: asset, url: source.url)
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

/// `GET /sound-effects` (`SoundEffectSummary`, no query params -- search and
/// category grouping happen client-side, see `NativeSfxBrowse`).
struct NativeEditorSoundEffect: Decodable, Sendable, Identifiable, Equatable {
    let id: String
    let name: String
    let durationS: Double?
    let previewAudioURL: URL?
    let roleTags: [String]
    let category: String?
    let searchTerms: [String]
    enum CodingKeys: String, CodingKey {
        case id, name, durationS = "duration_s", previewAudioURL = "preview_audio_url"
        case roleTags = "role_tags", category, searchTerms = "search_terms"
    }
}

extension KriaAPI {
    func editorSoundEffects() async throws -> [NativeEditorSoundEffect] {
        struct Envelope: Decodable { let effects: [NativeEditorSoundEffect] }
        return try await request(path: "sound-effects", method: "GET", bodyData: nil, decode: Envelope.self).effects
    }
}

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
        // KRI-587: one editor open on a 10-clip edit writes ~45 events, so 40 lost a failure seconds later.
        if let data = try? JSONEncoder().encode(Array(events.suffix(150))) { try? data.write(to: url, options: .atomic) }
    }
    /// Recorded from every build (unlike `failure`, which is DEBUG-only): a TestFlight phone is exactly where a
    /// player item fails with nobody attached. Numeric AVFoundation identity only, so it is safe to upload.
    static func playerItemFailure(kind: PlaybackFailureReport.PlayerKind, error: Error?, sourceState: String) -> PlaybackFailureReport {
        let nsError = error.map { $0 as NSError }
        let underlying = nsError?.userInfo[NSUnderlyingErrorKey] as? NSError
        let report = PlaybackFailureReport(
            playerKind: kind,
            errorDomain: nsError?.domain ?? "unknown",
            errorCode: nsError?.code ?? 0,
            underlyingDomain: underlying?.domain,
            underlyingCode: underlying.map(\.code),
            sourceState: sourceState,
            appBuild: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
        var fields = ["kind": kind.rawValue, "domain": report.errorDomain, "code": String(report.errorCode), "state": sourceState]
        if let domain = report.underlyingDomain { fields["underlyingDomain"] = domain }
        if let code = report.underlyingCode { fields["underlyingCode"] = String(code) }
        record("player-item-failed", fields: fields)
        return report
    }
    /// The live editable preview could not be built or played, so the finished video is shown instead.
    /// Reuses the player-item report shape (`POST /me/jobs/{id}/playback-diagnostics`): the identity is the
    /// error's type and case name (letters, digits and underscores only, never its text, payload, URL or
    /// path) and its numeric code, so it is safe to upload from any build.
    static func previewFailure(error: Error, sourceState: String) -> PlaybackFailureReport {
        let nsError = error as NSError
        let underlying = nsError.userInfo[NSUnderlyingErrorKey] as? NSError
        // Foundation-style errors ("NSURLErrorDomain") already name themselves. A Swift enum's bridged
        // domain is just its type name, so add the case name; payloads (asset ids) are cut off at "(".
        var domain = nsError.domain
        if !domain.hasSuffix("Domain") {
            let caseName = String(String(describing: error).prefix { $0 != "(" && $0 != " " })
                .filter { $0.isLetter || $0.isNumber || $0 == "_" }.prefix(40)
            if !caseName.isEmpty { domain += "." + caseName }
        }
        let report = PlaybackFailureReport(
            playerKind: .live,
            errorDomain: String(("preview." + domain).prefix(120)),
            errorCode: nsError.code,
            underlyingDomain: underlying.map { String($0.domain.prefix(120)) },
            underlyingCode: underlying.map(\.code),
            sourceState: String(sourceState.prefix(32)),
            appBuild: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
        record("preview-failure-reported", fields: ["domain": report.errorDomain, "code": String(report.errorCode), "state": sourceState])
        return report
    }
    #if DEBUG
    static func failure(_ stage: String, error: Error) {
        var fields = ["type": String(describing: type(of: error)), "domain": (error as NSError).domain, "code": String((error as NSError).code)]
        if let feature = error as? NativePreviewFeatureError { fields["feature"] = feature.feature }
        if let engine = error as? MediaEngineError {
            switch engine {
            case .unsupportedCapability: fields["engineCase"] = "unsupportedCapability"
            case .missingAsset: fields["engineCase"] = "missingAsset"
            // Named rather than folded into "other": this one means the app
            // BUILD is missing a bundled branding file, so the diagnostic
            // should point at the install, not at the creator's edit.
            case .missingBrandingResource(let file):
                fields["engineCase"] = "missingBrandingResource"
                fields["brandingResource"] = file
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
    #endif
}

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
            if ProcessInfo.processInfo.arguments.contains("-native-library-inventory-audit") {
                let library = try await api.library()
                rows.append(["stage": "inventory", "jobs": String(library.count),
                    "uniqueJobs": String(Set(library.map(\.id)).count),
                    "readyLibraryJobs": String(library.filter { $0.status == .ready }.count),
                    "time": Date().ISO8601Format()])
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
                        for lane in ["media_overlays", "sound_effects", "visual_blocks", "motion_scenes", "camera_effects", "drawings", "narrated_timings", "story_timeline"] { row[lane] = String(count(variant[lane])) }
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
                        if editor.sourcePreviewState.isFailure,
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
