import Foundation
import AVFoundation
import KriaMediaEngine

struct OriginalMediaDescriptor: Codable, Sendable, Equatable {
    let sha256: String
    let byteCount: Int64
    let durationS: Double
    let width: Int
    let height: Int
    let orientationDegrees: Int
    let hasAudio: Bool
    enum CodingKeys: String, CodingKey {
        case sha256, width, height
        case byteCount = "byte_count", durationS = "duration_s", orientationDegrees = "orientation_degrees", hasAudio = "has_audio"
    }
}

struct AnalysisProxyDescriptor: Codable, Sendable, Equatable {
    let original: OriginalMediaDescriptor
    var timingVersion = 1
    let durationS: Double
    let width: Int
    let height: Int
    let frameRate: Double
    var orientationDegrees = 0
    enum CodingKeys: String, CodingKey {
        case original, width, height
        case timingVersion = "timing_version", durationS = "duration_s", frameRate = "frame_rate", orientationDegrees = "orientation_degrees"
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
}
