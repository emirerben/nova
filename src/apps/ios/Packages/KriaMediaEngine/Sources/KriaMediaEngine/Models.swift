import Foundation

// MARK: - Versioned recipe boundary

/// The portable, renderer-neutral edit description exchanged with the API and local renderer.
/// Deliberately contains metadata and file identifiers only: pixel buffers never cross this boundary.
public struct EditRecipe: Codable, Equatable, Sendable {
    public static let currentSchemaVersion = 1
    public var schemaVersion: Int
    public var rendererVersion: String
    public var canvas: Canvas
    public var frameRate: Double
    public var assets: [MediaAsset]
    public var tracks: [TimelineTrack]
    public var audio: AudioMixRecipe
    public var requiredCapabilities: Set<MediaCapability>

    public init(schemaVersion: Int = Self.currentSchemaVersion,
                rendererVersion: String = "kria-ios-1",
                canvas: Canvas = .vertical1080,
                frameRate: Double = 30,
                assets: [MediaAsset] = [], tracks: [TimelineTrack] = [],
                audio: AudioMixRecipe = .default, requiredCapabilities: Set<MediaCapability> = []) {
        self.schemaVersion = schemaVersion; self.rendererVersion = rendererVersion
        self.canvas = canvas; self.frameRate = frameRate; self.assets = assets
        self.tracks = tracks; self.audio = audio; self.requiredCapabilities = requiredCapabilities
    }

    public func validate() throws {
        guard schemaVersion > 0 && schemaVersion <= Self.currentSchemaVersion else { throw RecipeError.unsupportedSchema(schemaVersion) }
        guard frameRate > 0 && frameRate <= 240 else { throw RecipeError.invalidFrameRate(frameRate) }
        guard canvas.width > 0 && canvas.height > 0 && !tracks.flatMap(\.clips).isEmpty else { throw RecipeError.invalidTimeline }
        guard !assets.contains(where: { $0.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || $0.relativePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else { throw RecipeError.invalidTimeline }
        guard !tracks.flatMap(\.clips).contains(where: { $0.timelineStart < 0 || $0.sourceStart < 0 || $0.sourceDuration <= 0 || $0.duration <= 0 || $0.rate <= 0 || $0.volume < 0 }) else { throw RecipeError.invalidTimeline }
        guard !tracks.flatMap(\.clips).contains(where: { ($0.transition?.duration ?? 0) < 0 || ($0.transition?.duration ?? 0) > $0.duration }) else { throw RecipeError.invalidTimeline }
        guard !tracks.flatMap(\.clips).compactMap(\.text).contains(where: { $0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || $0.fontSize <= 0 || $0.colorRGBA.count != 4 || $0.colorRGBA.contains(where: { !$0.isFinite || $0 < 0 || $0 > 1 }) }) else { throw RecipeError.invalidTimeline }
        guard audio.musicVolume >= 0 && audio.musicVolume <= 2 && audio.originalVolume >= 0 && audio.originalVolume <= 2 && audio.fadeIn >= 0 && audio.fadeOut >= 0 else { throw RecipeError.invalidTimeline }
        let ids = Set(assets.map(\.id)); guard tracks.flatMap(\.clips).allSatisfy({ ids.contains($0.sourceAssetID) }) else { throw RecipeError.missingAssetReference }
        guard ids.count == assets.count else { throw RecipeError.invalidTimeline }
    }
}

public enum RecipeError: Error, Equatable, Sendable { case unsupportedSchema(Int), invalidFrameRate(Double), invalidTimeline, missingAssetReference }

/// Canonical wire coding for API payloads. Keeping this explicit avoids relying on a caller's encoder
/// settings and leaves the ordinary Codable conformance useful for local persistence.
public enum RecipeJSON {
    public static func encoder() -> JSONEncoder { let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase; return encoder }
    public static func decoder() -> JSONDecoder { let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase; return decoder }
    public static func encode(_ recipe: EditRecipe) throws -> Data { try encoder().encode(recipe) }
    public static func decode(_ data: Data) throws -> EditRecipe { try decoder().decode(EditRecipe.self, from: data) }
}

public enum RecipeMigrationError: Error, Equatable, Sendable { case invalidPayload, unsupportedSchema(Int) }

/// Migrates the first editor payload shape into the native track-based recipe. This is intentionally
/// data-only so a future app can migrate a project after a crash or an app upgrade without AVFoundation.
public enum RecipeMigration {
    public static func migrate(_ data: Data) throws -> EditRecipe {
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw RecipeMigrationError.invalidPayload }
        let schema = object["schema_version"] as? Int ?? 0
        guard schema <= EditRecipe.currentSchemaVersion else { throw RecipeMigrationError.unsupportedSchema(schema) }
        if schema == 0 {
            object["schema_version"] = EditRecipe.currentSchemaVersion
            object["renderer_version"] = object["renderer_version"] ?? "kria-ios-1"
            object["required_capabilities"] = object["required_capabilities"] ?? ["basicComposition", "local1080Export"]
            if object["frame_rate"] == nil, let fps = object["fps"] { object["frame_rate"] = fps }
            if object["canvas"] == nil {
                object["canvas"] = ["width": object["width"] ?? 1080, "height": object["height"] ?? 1920]
            }
            if object["assets"] == nil, let clips = object["clips"] as? [[String: Any]] {
                var assets: [[String: Any]] = []; var migrated: [[String: Any]] = []
                for (index, clip) in clips.enumerated() {
                    let source = String(describing: clip["source_ref"] ?? "asset-\(index)")
                    if !assets.contains(where: { ($0["id"] as? String) == source }) { assets.append(["id": source, "relative_path": source, "orientation_degrees": 0, "is_proxy_available": false]) }
                    migrated.append(["id": "clip-\(index)", "source_asset_id": source, "source_start": clip["in_s"] ?? 0, "source_duration": clip["duration_s"] ?? 0, "timeline_start": clips.prefix(index).reduce(0.0) { $0 + (($1["duration_s"] as? Double) ?? 0) }, "rate": 1.0, "transform": ["scale": 1.0, "rotation_degrees": 0.0, "position_x": 0.0, "position_y": 0.0], "volume": clip["volume"] ?? 1.0])
                }
                object["assets"] = assets
                object["tracks"] = [["id": "video", "kind": "video", "clips": migrated]]
            }
            if object["audio"] == nil { object["audio"] = ["music_asset_id": NSNull(), "music_volume": 1.0, "original_volume": 1.0, "fade_in": 0.0, "fade_out": 0.0, "duck_original_during_music": false] }
            object.removeValue(forKey: "width"); object.removeValue(forKey: "height"); object.removeValue(forKey: "fps"); object.removeValue(forKey: "clips")
        }
        do { let migratedData = try JSONSerialization.data(withJSONObject: object); return try RecipeJSON.decode(migratedData) } catch { throw RecipeMigrationError.invalidPayload }
    }
}

public struct Canvas: Codable, Equatable, Sendable {
    public var width: Int; public var height: Int
    public init(width: Int, height: Int) { self.width = width; self.height = height }
    public static let vertical1080 = Canvas(width: 1080, height: 1920)
}

public struct MediaAsset: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var relativePath: String
    public var fingerprint: AssetFingerprint?
    public var duration: TimeInterval?
    public var naturalSize: MediaSize?
    public var frameRate: Double?
    public var orientationDegrees: Int
    public var isProxyAvailable: Bool
    public init(id: String, relativePath: String, fingerprint: AssetFingerprint? = nil,
                duration: TimeInterval? = nil, naturalSize: MediaSize? = nil,
                frameRate: Double? = nil, orientationDegrees: Int = 0, isProxyAvailable: Bool = false) {
        self.id = id; self.relativePath = relativePath; self.fingerprint = fingerprint; self.duration = duration
        self.naturalSize = naturalSize; self.frameRate = frameRate; self.orientationDegrees = orientationDegrees; self.isProxyAvailable = isProxyAvailable
    }
}

public struct MediaSize: Codable, Equatable, Sendable { public var width: Double; public var height: Double; public init(width: Double, height: Double) { self.width = width; self.height = height } }
public struct AssetFingerprint: Codable, Equatable, Hashable, Sendable { public var algorithm: String; public var hex: String; public var byteCount: Int64; public init(algorithm: String = "sha256", hex: String, byteCount: Int64) { self.algorithm = algorithm; self.hex = hex; self.byteCount = byteCount } }

public struct TimelineTrack: Codable, Equatable, Sendable, Identifiable {
    public enum Kind: String, Codable, Sendable { case video, overlay, audio }
    public var id: String; public var kind: Kind; public var clips: [TimelineClip]
    public init(id: String, kind: Kind, clips: [TimelineClip] = []) { self.id = id; self.kind = kind; self.clips = clips }
}

public struct TimelineClip: Codable, Equatable, Sendable, Identifiable {
    public var id: String; public var sourceAssetID: String
    public var sourceStart: TimeInterval; public var sourceDuration: TimeInterval
    public var timelineStart: TimeInterval; public var rate: Double
    public var transform: MediaTransform; public var transition: Transition?
    public var text: TextTreatment?
    public var volume: Double
    public var duration: TimeInterval { sourceDuration / rate }
    // Use Swift's acronym-normalized spelling so convertToSnakeCase/convertFromSnakeCase agree.
    private enum CodingKeys: String, CodingKey { case id, sourceAssetID = "sourceAssetId", sourceStart, sourceDuration, timelineStart, rate, transform, transition, text, volume }
    public init(id: String, sourceAssetID: String, sourceStart: TimeInterval = 0, sourceDuration: TimeInterval,
                timelineStart: TimeInterval = 0, rate: Double = 1, transform: MediaTransform = .identity,
                transition: Transition? = nil, text: TextTreatment? = nil, volume: Double = 1) {
        self.id = id; self.sourceAssetID = sourceAssetID; self.sourceStart = sourceStart; self.sourceDuration = sourceDuration
        self.timelineStart = timelineStart; self.rate = rate; self.transform = transform; self.transition = transition; self.text = text; self.volume = volume
    }
}

public struct MediaTransform: Codable, Equatable, Sendable { public var scale: Double; public var rotationDegrees: Double; public var positionX: Double; public var positionY: Double; public init(scale: Double = 1, rotationDegrees: Double = 0, positionX: Double = 0, positionY: Double = 0) { self.scale = scale; self.rotationDegrees = rotationDegrees; self.positionX = positionX; self.positionY = positionY }; public static let identity = MediaTransform() }

public struct Transition: Codable, Equatable, Sendable { public enum Kind: String, Codable, Sendable { case crossfade }; public var kind: Kind; public var duration: TimeInterval; public init(kind: Kind = .crossfade, duration: TimeInterval = 0.35) { self.kind = kind; self.duration = duration } }

public struct TextTreatment: Codable, Equatable, Sendable {
    public var text: String; public var fontName: String; public var fontSize: Double; public var colorRGBA: [Double]
    public var anchor: TextAnchor; public var animation: TextAnimation
    private enum CodingKeys: String, CodingKey { case text, fontName, fontSize, colorRGBA = "colorRgba", anchor, animation }
    public init(text: String, fontName: String = "Helvetica-Bold", fontSize: Double = 72,
                colorRGBA: [Double] = [1, 1, 1, 1], anchor: TextAnchor = .center,
                animation: TextAnimation = .fadeScale) { self.text = text; self.fontName = fontName; self.fontSize = fontSize; self.colorRGBA = colorRGBA; self.anchor = anchor; self.animation = animation }
}
public enum TextAnchor: String, Codable, Sendable { case top, center, bottom }
public enum TextAnimation: String, Codable, Sendable { case none, fade, fadeScale }

public struct AudioMixRecipe: Codable, Equatable, Sendable {
    public var musicAssetID: String?; public var musicVolume: Double; public var originalVolume: Double
    public var fadeIn: TimeInterval; public var fadeOut: TimeInterval; public var duckOriginalDuringMusic: Bool
    public init(musicAssetID: String? = nil, musicVolume: Double = 1, originalVolume: Double = 1, fadeIn: TimeInterval = 0, fadeOut: TimeInterval = 0, duckOriginalDuringMusic: Bool = false) { self.musicAssetID = musicAssetID; self.musicVolume = musicVolume; self.originalVolume = originalVolume; self.fadeIn = fadeIn; self.fadeOut = fadeOut; self.duckOriginalDuringMusic = duckOriginalDuringMusic }
    public static let `default` = AudioMixRecipe()
}

public enum MediaCapability: String, Codable, Hashable, Sendable, CaseIterable { case basicComposition, animatedText, crossfade, audioMix, variableSpeed, alphaOverlay, hevcDecode, hdr, local1080Export }

public struct Waveform: Codable, Equatable, Sendable { public var sampleRate: Double; public var levels: [Float]; public init(sampleRate: Double, levels: [Float]) { self.sampleRate = sampleRate; self.levels = levels } }
public struct ThumbnailSample: Codable, Equatable, Sendable { public var time: TimeInterval; public var fileURL: URL; private enum CodingKeys: String, CodingKey { case time, fileURL = "fileUrl" }; public init(time: TimeInterval, fileURL: URL) { self.time = time; self.fileURL = fileURL } }
