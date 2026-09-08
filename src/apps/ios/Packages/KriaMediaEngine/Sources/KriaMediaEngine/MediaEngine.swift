import Foundation

#if canImport(CryptoKit)
import CryptoKit
#endif

#if canImport(AVFoundation)
import AVFoundation
import CoreMedia
import ImageIO
#if canImport(UIKit)
import UIKit
#endif
#endif

// MARK: - File coordination and assets

public struct ProjectDirectory: Sendable {
    public let root: URL
    public init(root: URL) { self.root = root }
    public var originals: URL { root.appendingPathComponent("originals", isDirectory: true) }
    public var proxies: URL { root.appendingPathComponent("proxies", isDirectory: true) }
    public var thumbnails: URL { root.appendingPathComponent("thumbnails", isDirectory: true) }
    public var waveforms: URL { root.appendingPathComponent("waveforms", isDirectory: true) }
    public var exports: URL { root.appendingPathComponent("exports", isDirectory: true) }
    public func createIfNeeded(fileManager: FileManager = .default) throws {
        for url in [root, originals, proxies, thumbnails, waveforms, exports] { try fileManager.createDirectory(at: url, withIntermediateDirectories: true) }
    }
}

public protocol AssetFileCoordinator: Sendable {
    func copyAsset(from source: URL, to destination: URL) throws
}

public struct CoordinatedFileCopier: AssetFileCoordinator {
    public init() {}
    public func copyAsset(from source: URL, to destination: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
#if canImport(Foundation)
        var coordinationError: NSError?
        var copyError: Error?
        let coordinator = NSFileCoordinator(filePresenter: nil)
        coordinator.coordinate(readingItemAt: source, options: [], error: &coordinationError) { coordinatedURL in
            do { try fm.copyItem(at: coordinatedURL, to: destination) } catch { copyError = error }
        }
        if let copyError { throw copyError }
        if let coordinationError { throw coordinationError }
#else
        try fm.copyItem(at: source, to: destination)
#endif
    }
}

public actor AssetImportCoordinator {
    private let project: ProjectDirectory
    private let copier: any AssetFileCoordinator
    public init(project: ProjectDirectory, copier: any AssetFileCoordinator = CoordinatedFileCopier()) { self.project = project; self.copier = copier }
    public func importAsset(from source: URL, id: String? = nil) throws -> MediaAsset {
        try project.createIfNeeded()
        let rawAssetID = id ?? UUID().uuidString
        let assetID = rawAssetID.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "\\", with: "_").replacingOccurrences(of: "..", with: "_")
        let ext = source.pathExtension.isEmpty ? "mov" : source.pathExtension
        let destination = project.originals.appendingPathComponent("\(assetID).\(ext)")
        let accessingSecurityScope = source.startAccessingSecurityScopedResource()
        defer { if accessingSecurityScope { source.stopAccessingSecurityScopedResource() } }
        try copier.copyAsset(from: source, to: destination)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: destination)
        return MediaAsset(id: assetID, relativePath: project.root.relativePath(to: destination), fingerprint: fingerprint)
    }
}

public extension URL {
    func relativePath(to url: URL) -> String {
        let base = standardizedFileURL.path.hasSuffix("/") ? standardizedFileURL.path : standardizedFileURL.path + "/"
        return url.standardizedFileURL.path.hasPrefix(base) ? String(url.standardizedFileURL.path.dropFirst(base.count)) : url.lastPathComponent
    }
}

// MARK: - Streaming fingerprints

public protocol AssetFingerprinter: Sendable { func fingerprint(file: URL) throws -> AssetFingerprint }

public struct SHA256Fingerprinter: AssetFingerprinter {
    public init() {}
    public func fingerprint(file: URL) throws -> AssetFingerprint {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var count: Int64 = 0
        var digest = SHA256Accumulator()
        while true {
            let data = try handle.read(upToCount: 1024 * 1024) ?? Data()
            if data.isEmpty { break }
            count += Int64(data.count); digest.update(data)
        }
        return AssetFingerprint(hex: digest.finalize(), byteCount: count)
    }
}

/// Small adapter keeps the public package usable on platforms where CryptoKit is unavailable.
private struct SHA256Accumulator {
#if canImport(CryptoKit)
    private var hasher = CryptoKit.SHA256()
    mutating func update(_ data: Data) { hasher.update(data: data) }
    mutating func finalize() -> String { hasher.finalize().map { String(format: "%02x", $0) }.joined() }
#else
    private var bytes = Data()
    mutating func update(_ data: Data) { bytes.append(data) }
    mutating func finalize() -> String {
        // CryptoKit is present on supported iOS/macOS SDKs. This deterministic fallback is only for
        // non-Apple package discovery/build environments and is never used by the app.
        var hash: UInt64 = 14695981039346656037
        for byte in bytes { hash = (hash ^ UInt64(byte)) &* 1099511628211 }
        return String(format: "%016llx", hash)
    }
#endif
}

// MARK: - Proxies, thumbnails, waveform seams

public protocol ProxyGenerating: Sendable {
    func makeProxy(for asset: URL, destination: URL, progress: (@Sendable (Double) -> Void)?) async throws -> URL
}
public struct ProxyPreset: Equatable, Sendable {
    public var width: Int; public var height: Int; public var videoCodec: String; public var audioCodec: String
    public init(width: Int = 960, height: Int = 540, videoCodec: String = "h264", audioCodec: String = "aac") { self.width = width; self.height = height; self.videoCodec = videoCodec; self.audioCodec = audioCodec }
    public static let `default` = ProxyPreset()
}
public protocol ThumbnailSampling: Sendable { func sample(asset: URL, at times: [TimeInterval], destinationDirectory: URL) async throws -> [ThumbnailSample] }
public protocol WaveformExtracting: Sendable { func extract(asset: URL, bucketCount: Int) async throws -> Waveform }

#if canImport(AVFoundation)
@MainActor public struct AVFoundationProxyGenerator: ProxyGenerating {
    public let preset: ProxyPreset
    public init(preset: ProxyPreset = .default) { self.preset = preset }
    public func makeProxy(for asset: URL, destination: URL, progress: (@Sendable (Double) -> Void)? = nil) async throws -> URL {
        let avAsset = AVURLAsset(url: asset)
        guard let session = AVAssetExportSession(asset: avAsset, presetName: AVAssetExportPresetMediumQuality) else { throw MediaEngineError.exportUnavailable }
        if let sourceTrack = try await avAsset.loadTracks(withMediaType: .video).first {
            let naturalSize = try await sourceTrack.load(.naturalSize)
            let preferredTransform = try await sourceTrack.load(.preferredTransform)
            let orientedRect = CGRect(origin: .zero, size: naturalSize).applying(preferredTransform)
            let orientedSize = CGSize(width: abs(orientedRect.width), height: abs(orientedRect.height))
            let landscape = orientedSize.width >= orientedSize.height
            let renderSize = landscape ? CGSize(width: preset.width, height: preset.height) : CGSize(width: preset.height, height: preset.width)
            let scale = min(renderSize.width / max(orientedSize.width, 1), renderSize.height / max(orientedSize.height, 1))
            let centered = preferredTransform
                .concatenating(CGAffineTransform(translationX: -orientedRect.minX, y: -orientedRect.minY))
                .concatenating(CGAffineTransform(scaleX: scale, y: scale))
                .concatenating(CGAffineTransform(translationX: (renderSize.width - orientedSize.width * scale) / 2, y: (renderSize.height - orientedSize.height * scale) / 2))
            let videoComposition = AVMutableVideoComposition()
            videoComposition.renderSize = renderSize
            videoComposition.frameDuration = CMTime(value: 1, timescale: 30)
            let instruction = AVMutableVideoCompositionInstruction()
            instruction.timeRange = CMTimeRange(start: .zero, duration: try await avAsset.load(.duration))
            let layer = AVMutableVideoCompositionLayerInstruction(assetTrack: sourceTrack)
            layer.setTransform(centered, at: .zero)
            instruction.layerInstructions = [layer]; videoComposition.instructions = [instruction]
            session.videoComposition = videoComposition
        }
        try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        session.outputURL = destination; session.outputFileType = .mp4; session.shouldOptimizeForNetworkUse = true
        progress?(0)
        await session.export()
        if Task.isCancelled { session.cancelExport(); throw CancellationError() }
        guard session.status == .completed else { throw session.error ?? MediaEngineError.exportFailed }
        progress?(1); return destination
    }
}

@MainActor public struct AVFoundationThumbnailSampler: ThumbnailSampling {
    public init() {}
    public func sample(asset: URL, at times: [TimeInterval], destinationDirectory: URL) async throws -> [ThumbnailSample] {
        try FileManager.default.createDirectory(at: destinationDirectory, withIntermediateDirectories: true)
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: asset)); generator.appliesPreferredTrackTransform = true; generator.maximumSize = CGSize(width: 360, height: 640)
        // ImageGenerator is stateful; serial sampling is deterministic and avoids sharing Core
        // Graphics objects across concurrency domains. Callers can shard requests across samplers.
        var samples: [ThumbnailSample] = []
        for (index, time) in times.enumerated() {
            let image = try generator.copyCGImage(at: CMTime(seconds: time, preferredTimescale: 600), actualTime: nil)
            let url = destinationDirectory.appendingPathComponent(String(format: "%04d.jpg", index))
            guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.jpeg" as CFString, 1, nil) else { throw MediaEngineError.thumbnailWriteFailed }
            CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.82] as CFDictionary)
            guard CGImageDestinationFinalize(dest) else { throw MediaEngineError.thumbnailWriteFailed }
            samples.append(ThumbnailSample(time: time, fileURL: url))
        }
        return samples
    }
}

public struct AVFoundationWaveformExtractor: WaveformExtracting {
    public init() {}
    public func extract(asset url: URL, bucketCount: Int) async throws -> Waveform {
        guard bucketCount > 0 else { return Waveform(sampleRate: 0, levels: []) }
        let asset = AVURLAsset(url: url); guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return Waveform(sampleRate: 0, levels: Array(repeating: 0, count: bucketCount)) }
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsNonInterleaved: false, AVNumberOfChannelsKey: 1, AVSampleRateKey: 8000])
        reader.add(output); guard reader.startReading() else { throw reader.error ?? MediaEngineError.waveformFailed }
        var values: [Float] = []; values.reserveCapacity(bucketCount * 32)
        while let sample = output.copyNextSampleBuffer(), let block = CMSampleBufferGetDataBuffer(sample) {
            var length = 0; var pointer: UnsafeMutablePointer<Int8>?; CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &pointer)
            if let pointer { let floats = UnsafeRawPointer(pointer).assumingMemoryBound(to: Float.self); values.append(contentsOf: UnsafeBufferPointer(start: floats, count: length / MemoryLayout<Float>.size).map { abs($0) }) }
            CMSampleBufferInvalidate(sample)
        }
        guard !values.isEmpty else { return Waveform(sampleRate: 0, levels: Array(repeating: 0, count: bucketCount)) }
        let stride = max(1, values.count / bucketCount); var levels = (0..<bucketCount).map { index in let slice = values.dropFirst(index * stride).prefix(stride); return min(1, slice.max() ?? 0) }
        if levels.count < bucketCount { levels += Array(repeating: 0, count: bucketCount - levels.count) }
        return Waveform(sampleRate: 8000 / Double(stride), levels: levels)
    }
}
#else
public struct AVFoundationProxyGenerator: ProxyGenerating { public let preset: ProxyPreset; public init(preset: ProxyPreset = .default) { self.preset = preset }; public func makeProxy(for: URL, destination: URL, progress: (@Sendable (Double) -> Void)? = nil) async throws -> URL { throw MediaEngineError.avFoundationUnavailable } }
public struct AVFoundationThumbnailSampler: ThumbnailSampling { public init() {}; public func sample(asset: URL, at: [TimeInterval], destinationDirectory: URL) async throws -> [ThumbnailSample] { throw MediaEngineError.avFoundationUnavailable } }
public struct AVFoundationWaveformExtractor: WaveformExtracting { public init() {}; public func extract(asset: URL, bucketCount: Int) async throws -> Waveform { throw MediaEngineError.avFoundationUnavailable } }
#endif

public enum MediaEngineError: Error, Equatable, Sendable { case avFoundationUnavailable, exportUnavailable, exportFailed, thumbnailWriteFailed, waveformFailed, insufficientStorage, unsupportedCapability, missingAsset(String), cancelled }
