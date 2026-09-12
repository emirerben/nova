import Foundation

#if canImport(AVFoundation)
import AVFoundation
import CoreMedia
#endif

public enum ExportStatus: String, Codable, Sendable { case queued, exporting, completed, cancelled, failed, needsCloudFallback }
public struct LocalExportPreset: Equatable, Sendable {
    public var canvas: Canvas; public var videoCodec: String; public var audioCodec: String; public var videoBitrate: Int
    public init(canvas: Canvas = .vertical1080, videoCodec: String = "h264", audioCodec: String = "aac", videoBitrate: Int = 8_000_000) { self.canvas = canvas; self.videoCodec = videoCodec; self.audioCodec = audioCodec; self.videoBitrate = videoBitrate }
    public static let `default` = LocalExportPreset()
}
public struct ExportCheckpoint: Codable, Equatable, Sendable {
    public var exportID: String; public var status: ExportStatus; public var progress: Double; public var outputURL: URL?; public var errorDescription: String?
    private enum CodingKeys: String, CodingKey { case exportID = "exportId", status, progress, outputURL = "outputUrl", errorDescription = "errorDescription" }
    public init(exportID: String, status: ExportStatus = .queued, progress: Double = 0, outputURL: URL? = nil, errorDescription: String? = nil) { self.exportID = exportID; self.status = status; self.progress = progress; self.outputURL = outputURL; self.errorDescription = errorDescription }
}

public protocol ExportStatePersisting: Sendable { func save(_ checkpoint: ExportCheckpoint) throws; func load(exportID: String) throws -> ExportCheckpoint? }
public struct FileExportStateStore: ExportStatePersisting {
    public let directory: URL
    public init(directory: URL) { self.directory = directory }
    public func save(_ checkpoint: ExportCheckpoint) throws { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true); let data = try JSONEncoder().encode(checkpoint); try data.write(to: directory.appendingPathComponent(ExportStateStoreKey.fileName(for: checkpoint.exportID)), options: .atomic) }
    public func load(exportID: String) throws -> ExportCheckpoint? { let url = directory.appendingPathComponent(ExportStateStoreKey.fileName(for: exportID)); guard FileManager.default.fileExists(atPath: url.path) else { return nil }; return try JSONDecoder().decode(ExportCheckpoint.self, from: Data(contentsOf: url)) }
}
private enum ExportStateStoreKey { static func fileName(for id: String) -> String { id.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "\\", with: "_") + ".json" } }

public protocol LocalExporting: Sendable {
    func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint
}

#if canImport(AVFoundation)
@MainActor public struct AVFoundationLocalExporter: LocalExporting {
    public let stateStore: any ExportStatePersisting
    public let preset: LocalExportPreset
    public let instrumentation: (any MediaInstrumentation)?
    public init(stateStore: any ExportStatePersisting, preset: LocalExportPreset = .default, instrumentation: (any MediaInstrumentation)? = nil) { self.stateStore = stateStore; self.preset = preset; self.instrumentation = instrumentation }
    public func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String = UUID().uuidString, progress: (@Sendable (Double) -> Void)? = nil) async throws -> ExportCheckpoint {
        try recipe.validate()
        let resolvedOutput = outputURL.resolvingSymlinksInPath().standardizedFileURL
        guard outputURL.isFileURL, !assetURLs.values.contains(where: {
            $0.resolvingSymlinksInPath().standardizedFileURL == resolvedOutput
        }) else { throw NativePreviewFeatureError("Export-44") }
        var checkpoint = ExportCheckpoint(exportID: exportID, status: .exporting); try stateStore.save(checkpoint); progress?(0)
        let startedAt = Date()
        do {
            traceDeviceExportPhase("prepare")
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: assetURLs)
            traceDeviceExportPhase("prepared")
            guard preset.videoCodec == "h264", preset.audioCodec == "aac", preset.videoBitrate > 0 else { throw NativePreviewFeatureError("Export-51") }
            try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: outputURL.path) { try FileManager.default.removeItem(at: outputURL) }
            let writer = try await RecipeWriter(preview: preview, outputURL: outputURL, bitrate: preset.videoBitrate)
            traceDeviceExportPhase("writer_initialized")
            let task = Task.detached { try await writer.run(progress: progress) }
            try await withTaskCancellationHandler(operation: { try await task.value }, onCancel: { task.cancel() })
            try Task.checkCancellation()
            traceDeviceExportPhase("completed")
            checkpoint.status = .completed; checkpoint.progress = 1; checkpoint.outputURL = outputURL; try stateStore.save(checkpoint); progress?(1)
            instrumentation?.record(MetricEvent(name: .exportDuration, value: Date().timeIntervalSince(startedAt)))
            return checkpoint
        } catch is CancellationError { try? FileManager.default.removeItem(at: outputURL); checkpoint.status = .cancelled; try? stateStore.save(checkpoint); throw CancellationError() }
        catch { try? FileManager.default.removeItem(at: outputURL); checkpoint.status = .failed; checkpoint.errorDescription = String(describing: error); try? stateStore.save(checkpoint); throw error }
    }
}
#else
public struct AVFoundationLocalExporter: LocalExporting { public let stateStore: any ExportStatePersisting; public let preset: LocalExportPreset; public let instrumentation: (any MediaInstrumentation)?; public init(stateStore: any ExportStatePersisting, preset: LocalExportPreset = .default, instrumentation: (any MediaInstrumentation)? = nil) { self.stateStore = stateStore; self.preset = preset; self.instrumentation = instrumentation }; public func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String = UUID().uuidString, progress: (@Sendable (Double) -> Void)? = nil) async throws -> ExportCheckpoint { throw MediaEngineError.avFoundationUnavailable } }
#endif

/// Local account-free device tests only; release builds do not emit these stages.
func traceDeviceExportPhase(_ phase: String) {
    #if DEBUG
    if ProcessInfo.processInfo.arguments.contains("-device-effects") { print("KRIA_EXPORT_PHASE \(phase)") }
    #endif
}
