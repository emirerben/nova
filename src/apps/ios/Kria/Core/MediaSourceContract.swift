import Foundation
import AVFoundation
import UniformTypeIdentifiers
import KriaMediaEngine

/// Specific, user-facing reasons an uploaded clip's analysis-proxy contract can
/// fail its client-side validation. Mirrors the one-guard-many-causes pattern
/// `EditorSaveError` uses in `Services.swift`: each distinct violation gets
/// its own case and copy instead of one generic `APIError.invalidResponse`
/// for every cause (KRI-118).
enum MediaSourceContractError: Error, LocalizedError, Equatable, Sendable {
    /// The source file's container isn't one AVFoundation reliably transcodes
    /// on-device. Checked before touching the transcode output at all, so
    /// this fails with actionable copy instead of an opaque AVFoundation
    /// error surfacing deep inside the proxy transcode.
    case unsupportedContainer
    /// Neither the original nor the proxy has a video track to measure.
    case missingVideoTrack
    /// The proxy's duration drifted from the original's by more than the
    /// tolerance (or either duration is unusable), i.e. length changed
    /// somewhere between capture and upload.
    case durationMismatch
    /// The original has audio and the proxy doesn't, or vice versa.
    case audioPresenceMismatch
    /// Resolution, frame rate, or orientation fell outside what the analysis
    /// pipeline accepts.
    case unsupportedGeometry

    var errorDescription: String? {
        switch self {
        case .unsupportedContainer: "Export this as MP4 or MOV first."
        case .missingVideoTrack: "This file doesn't have a video track."
        case .durationMismatch: "This clip's length changed during upload. Try again."
        case .audioPresenceMismatch: "This clip's audio didn't match after upload. Try again."
        case .unsupportedGeometry: "This clip couldn't be processed. Try re-exporting it."
        }
    }
}

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
        // Belt-and-suspenders: by the time this runs, `proxy` already exists,
        // meaning the transcode itself succeeded reading `original`. This
        // check exists so a caller that validates the source container
        // *before* transcoding (the earliest point that actually avoids an
        // opaque AVFoundation failure) can reuse the exact same rule via
        // `validateSupportedContainer(_:)` below.
        try Self.validateSupportedContainer(original)
        let source = AVURLAsset(url: original)
        let reduced = AVURLAsset(url: proxy)
        guard let sourceTrack = try await source.loadTracks(withMediaType: .video).first,
              let proxyTrack = try await reduced.loadTracks(withMediaType: .video).first else {
            throw MediaSourceContractError.missingVideoTrack
        }
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
              abs(sourceDuration - duration) <= 0.1 else {
            throw MediaSourceContractError.durationMismatch
        }
        guard sourceHasAudio == proxyHasAudio else {
            throw MediaSourceContractError.audioPresenceMismatch
        }
        guard size.width > 0, size.height > 0, size.width <= 640, size.height <= 640,
              frameRate >= 1, frameRate <= 30, [0, 90, 180, 270].contains(orientation) else {
            throw MediaSourceContractError.unsupportedGeometry
        }
        return ProjectMediaUploadContract(purpose: .analysisProxy, proxy: AnalysisProxyDescriptor(
            original: OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount,
                durationS: sourceDuration, width: Int(sourceSize.width), height: Int(sourceSize.height),
                orientationDegrees: orientation, hasAudio: sourceHasAudio),
            durationS: duration, width: Int(size.width), height: Int(size.height), frameRate: frameRate
        ))
    }

    /// Fails fast when `url`'s container isn't one the on-device proxy
    /// transcode reliably handles, rather than letting an exotic container
    /// (e.g. `.avi`, `.mkv`, `.webm`) fail deep inside `AVAssetExportSession`
    /// with an opaque error. Keyed off the file extension because callers in
    /// this codebase already normalize a picked/downloaded file's extension
    /// to match its real container before this point (see
    /// `NativeDownloadedMedia.fileExtension` for the download path).
    ///
    /// Exposed so the transcode call site (`AVFoundationProxyGenerator` /
    /// `BackgroundUploads.prepare`) can call this BEFORE transcoding — the
    /// earliest point that actually avoids the opaque failure. `analysisProxy`
    /// above also calls it, but by then the transcode has already run.
    static func validateSupportedContainer(_ url: URL) throws {
        guard let type = UTType(filenameExtension: url.pathExtension.lowercased()),
              type.conforms(to: .mpeg4Movie) || type.conforms(to: .quickTimeMovie) else {
            throw MediaSourceContractError.unsupportedContainer
        }
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
