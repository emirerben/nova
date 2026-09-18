import Foundation
import AVFoundation
import KriaMediaEngine

/// Mirrors `app.kria.media_sources.MediaSourceKind`. "image" is deliberately not a
/// case: Visuals-pool photos upload in full to the pool and render on the phone from
/// those pinned pool bytes through the manifest's "visual" kind (KRI-121), so no
/// photo ever needs an analysis proxy bound to a device original.
enum MediaSourceKind: String, Codable, Sendable, Equatable {
    case video, audio
}

struct OriginalMediaDescriptor: Codable, Sendable, Equatable {
    var kind: MediaSourceKind = .video
    let sha256: String
    let byteCount: Int64
    let durationS: Double
    /// Required for `.video`, absent for `.audio` — mirrors the server's per-kind
    /// validation in `OriginalMediaDescriptor.validate_kind_shape`.
    let width: Int?
    let height: Int?
    let orientationDegrees: Int
    let hasAudio: Bool

    enum CodingKeys: String, CodingKey {
        case kind, sha256, width, height
        case byteCount = "byte_count", durationS = "duration_s", orientationDegrees = "orientation_degrees", hasAudio = "has_audio"
    }

    init(kind: MediaSourceKind = .video, sha256: String, byteCount: Int64, durationS: Double,
         width: Int?, height: Int?, orientationDegrees: Int, hasAudio: Bool) {
        self.kind = kind; self.sha256 = sha256; self.byteCount = byteCount; self.durationS = durationS
        self.width = width; self.height = height; self.orientationDegrees = orientationDegrees; self.hasAudio = hasAudio
    }

    // Custom decode: `kind` predates this struct's earlier shape, so a payload
    // persisted (locally or server-side) before this field existed has no key
    // for it at all — Swift's synthesized Decodable would throw on a missing
    // non-optional key even though the property declares a default.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decodeIfPresent(MediaSourceKind.self, forKey: .kind) ?? .video
        sha256 = try c.decode(String.self, forKey: .sha256)
        byteCount = try c.decode(Int64.self, forKey: .byteCount)
        durationS = try c.decode(Double.self, forKey: .durationS)
        width = try c.decodeIfPresent(Int.self, forKey: .width)
        height = try c.decodeIfPresent(Int.self, forKey: .height)
        orientationDegrees = try c.decode(Int.self, forKey: .orientationDegrees)
        hasAudio = try c.decode(Bool.self, forKey: .hasAudio)
    }

    /// Matches the server's `validate_kind_shape`: dimensions are required for
    /// `.video` and forbidden for `.audio`; an audio original must be audible.
    func validate() throws {
        switch kind {
        case .video:
            guard width != nil, height != nil else { throw APIError.invalidResponse }
        case .audio:
            guard width == nil, height == nil, orientationDegrees == 0, hasAudio else { throw APIError.invalidResponse }
        }
    }
}

struct AnalysisProxyDescriptor: Codable, Sendable, Equatable {
    let original: OriginalMediaDescriptor
    var timingVersion = 1
    let durationS: Double
    /// Required for `.video`, absent for `.audio` — see `original.kind`.
    let width: Int?
    let height: Int?
    let frameRate: Double?
    var orientationDegrees = 0

    enum CodingKeys: String, CodingKey {
        case original, width, height
        case timingVersion = "timing_version", durationS = "duration_s", frameRate = "frame_rate", orientationDegrees = "orientation_degrees"
    }

    init(original: OriginalMediaDescriptor, timingVersion: Int = 1, durationS: Double,
         width: Int?, height: Int?, frameRate: Double?, orientationDegrees: Int = 0) {
        self.original = original; self.timingVersion = timingVersion; self.durationS = durationS
        self.width = width; self.height = height; self.frameRate = frameRate; self.orientationDegrees = orientationDegrees
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        original = try c.decode(OriginalMediaDescriptor.self, forKey: .original)
        timingVersion = try c.decodeIfPresent(Int.self, forKey: .timingVersion) ?? 1
        durationS = try c.decode(Double.self, forKey: .durationS)
        width = try c.decodeIfPresent(Int.self, forKey: .width)
        height = try c.decodeIfPresent(Int.self, forKey: .height)
        frameRate = try c.decodeIfPresent(Double.self, forKey: .frameRate)
        orientationDegrees = try c.decodeIfPresent(Int.self, forKey: .orientationDegrees) ?? 0
    }

    /// Matches the server's `validate_shape_and_timing`.
    func validate() throws {
        switch original.kind {
        case .video:
            guard width != nil, height != nil, frameRate != nil else { throw APIError.invalidResponse }
        case .audio:
            guard width == nil, height == nil, frameRate == nil else { throw APIError.invalidResponse }
        }
        guard abs(durationS - original.durationS) <= 0.1 else { throw APIError.invalidResponse }
    }
}

struct ProjectMediaUploadContract: Codable, Sendable, Equatable {
    let purpose: UploadPurpose
    let proxy: AnalysisProxyDescriptor?

    @MainActor static func analysisProxy(original: URL, proxy: URL, fingerprint: AssetFingerprint) async throws -> ProjectMediaUploadContract {
        let source = AVURLAsset(url: original)
        let reduced = AVURLAsset(url: proxy)
        guard let sourceTrack = try await source.loadTracks(withMediaType: .video).first,
              let proxyTrack = try await reduced.loadTracks(withMediaType: .video).first else { throw APIError.invalidResponse }
        let sourceSize = try await sourceTrack.load(.naturalSize)
        let sourceTransform = try await sourceTrack.load(.preferredTransform)
        let sourceDuration = try await source.load(.duration).seconds
        let sourceHasAudio = !(try await source.loadTracks(withMediaType: .audio)).isEmpty
        let size = try await proxyTrack.load(.naturalSize)
        let duration = try await reduced.load(.duration).seconds
        let frameRate = Double(try await proxyTrack.load(.nominalFrameRate))
        let proxyHasAudio = !(try await reduced.loadTracks(withMediaType: .audio)).isEmpty
        let orientation = (Int((atan2(sourceTransform.b, sourceTransform.a) * 180 / .pi).rounded()) % 360 + 360) % 360
        guard sourceDuration.isFinite, duration.isFinite, sourceDuration > 0,
              abs(sourceDuration - duration) <= 0.1, sourceHasAudio == proxyHasAudio,
              size.width > 0, size.height > 0, size.width <= 640, size.height <= 640,
              frameRate >= 1, frameRate <= 30, [0, 90, 180, 270].contains(orientation) else { throw APIError.invalidResponse }
        return ProjectMediaUploadContract(purpose: .analysisProxy, proxy: AnalysisProxyDescriptor(
            original: OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount,
                durationS: sourceDuration, width: Int(sourceSize.width), height: Int(sourceSize.height),
                orientationDegrees: orientation, hasAudio: sourceHasAudio),
            durationS: duration, width: Int(size.width), height: Int(size.height), frameRate: frameRate
        ))
    }

    /// KRI-93: the narration/voiceover counterpart to `analysisProxy`. No dimensions,
    /// no frame rate, no orientation — an audio original must be audible (recorded
    /// narration/voiceover with no track is not a usable source).
    ///
    /// Not yet called from any production path: `ProjectUploadDestination.resolve`
    /// still routes `.voiceover` attachments to `.cloud`/`.voiceoverUnavailableOnPhone` regardless
    /// of phone-rendering availability, because no recipe schema field can carry a
    /// narration track yet (see `MediaCapability.narrationAudio`,
    /// docs/reviews/kri-29/capability-matrix.md). This exists so that gate is a single,
    /// later, reviewable change — not also a from-scratch proxy-contract implementation.
    @MainActor static func audioAnalysisProxy(original: URL, proxy: URL, fingerprint: AssetFingerprint) async throws -> ProjectMediaUploadContract {
        let source = AVURLAsset(url: original)
        let reduced = AVURLAsset(url: proxy)
        let sourceDuration = try await source.load(.duration).seconds
        let duration = try await reduced.load(.duration).seconds
        let sourceHasAudio = !(try await source.loadTracks(withMediaType: .audio)).isEmpty
        let proxyHasAudio = !(try await reduced.loadTracks(withMediaType: .audio)).isEmpty
        guard sourceDuration.isFinite, duration.isFinite, sourceDuration > 0,
              abs(sourceDuration - duration) <= 0.1, sourceHasAudio, proxyHasAudio else { throw APIError.invalidResponse }
        let contract = ProjectMediaUploadContract(purpose: .analysisProxy, proxy: AnalysisProxyDescriptor(
            original: OriginalMediaDescriptor(kind: .audio, sha256: fingerprint.hex, byteCount: fingerprint.byteCount,
                durationS: sourceDuration, width: nil, height: nil, orientationDegrees: 0, hasAudio: sourceHasAudio),
            durationS: duration, width: nil, height: nil, frameRate: nil
        ))
        try contract.proxy?.validate()
        return contract
    }
}
