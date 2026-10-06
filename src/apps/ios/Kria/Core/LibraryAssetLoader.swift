import Foundation
import Photos
import PhotosUI
import SwiftUI

/// KRI-282 regression: a `PhotosPickerItem` built with `PhotosPickerItem(itemIdentifier:)` (what the in-app
/// gallery writes) carries NO item provider, so `loadTransferable` always fails ("This file couldn't be
/// read"). Real system-picker results do carry one. This type loads either kind: picker results through
/// their provider, gallery picks straight from Photos by `localIdentifier`.
struct PhotoItemFileLoader {
    var exporter: any LibraryAssetExporting = PhotosLibraryAssetExporter()

    /// - Parameters:
    ///   - hasProvider: whether the item came from the system picker (true) or was made from an identifier (false).
    ///   - viaProvider: loads the picker item's file; nil/throw means the provider could not deliver it.
    func load(identifier: String, hasProvider: Bool, viaProvider: () async throws -> URL?) async throws -> URL {
        if hasProvider {
            do { if let url = try await viaProvider() { return url } }
            catch is CancellationError { throw CancellationError() }
            catch { /* fall through: Photos can still hand the original over directly */ }
        }
        return try await exporter.exportFile(localIdentifier: identifier)
    }

    func load(item: PhotosPickerItem, identifier: String) async throws -> URL {
        try await load(identifier: identifier, hasProvider: !item.supportedContentTypes.isEmpty) {
            try await item.loadTransferable(type: ImportedMedia.self)?.url
        }
    }
}

/// Seam for tests: turns a Photos `localIdentifier` into a private temp file the upload pipeline owns.
protocol LibraryAssetExporting: Sendable {
    func exportFile(localIdentifier: String) async throws -> URL
}

enum LibraryAssetExportError: Error, Equatable {
    case assetNotFound
    case noResource
    case writeFailed
}

/// Which `PHAssetResource` carries the file we upload. Pure so every media kind is unit-testable.
enum LibraryResourcePicker {
    struct Candidate: Equatable { let type: PHAssetResourceType; let filename: String }

    /// Edited renders win over originals (trim, slo-mo ramp, crop, filters are baked in, as the system
    /// picker hands over). A Live Photo uses its still; the paired video is never the upload.
    static func pick(mediaType: PHAssetMediaType, from resources: [Candidate]) -> Candidate? {
        let order: [PHAssetResourceType] = mediaType == .video
            ? [.fullSizeVideo, .video]
            : [.fullSizePhoto, .photo, .alternatePhoto]
        for type in order { if let match = resources.first(where: { $0.type == type }) { return match } }
        return nil
    }
}

struct PhotosLibraryAssetExporter: LibraryAssetExporting {
    func exportFile(localIdentifier: String) async throws -> URL {
        let fetched = PHAsset.fetchAssets(withLocalIdentifiers: [localIdentifier], options: nil)
        guard let asset = fetched.firstObject else { throw LibraryAssetExportError.assetNotFound }
        let resources = PHAssetResource.assetResources(for: asset)
        let candidates = resources.map { LibraryResourcePicker.Candidate(type: $0.type, filename: $0.originalFilename) }
        guard let chosen = LibraryResourcePicker.pick(mediaType: asset.mediaType, from: candidates),
              let resource = resources.first(where: { $0.type == chosen.type && $0.originalFilename == chosen.filename }) else {
            throw LibraryAssetExportError.noResource
        }
        let destination = FileManager.default.temporaryDirectory
            .appending(path: "\(UUID().uuidString)-\(Self.safeName(resource.originalFilename))")
        guard FileManager.default.createFile(atPath: destination.path, contents: nil) else { throw LibraryAssetExportError.writeFailed }
        let box = RequestBox()
        do {
            try await withTaskCancellationHandler {
                try await Self.stream(resource, to: destination, box: box)
            } onCancel: {
                box.cancel()
            }
        } catch {
            try? FileManager.default.removeItem(at: destination)
            throw error
        }
        return destination
    }

    private static func safeName(_ name: String) -> String {
        let cleaned = name.replacingOccurrences(of: "/", with: "-")
        return cleaned.isEmpty ? "asset" : cleaned
    }

    /// Streams the resource to disk chunk by chunk (never the whole video in memory), allowing the iCloud
    /// download. Cancelling the task cancels the Photos request.
    private static func stream(_ resource: PHAssetResource, to destination: URL, box: RequestBox) async throws {
        let handle = try FileHandle(forWritingTo: destination)
        defer { try? handle.close() }
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        let failure = FailureBox()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let id = PHAssetResourceManager.default().requestData(for: resource, options: options, dataReceivedHandler: { chunk in
                do { try handle.write(contentsOf: chunk) } catch { failure.set(error) }
            }, completionHandler: { error in
                if let error = failure.value ?? error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            })
            box.set(id)
        }
    }

    private final class RequestBox: @unchecked Sendable {
        private let lock = NSLock()
        private var id: PHAssetResourceDataRequestID?
        private var cancelled = false
        func set(_ id: PHAssetResourceDataRequestID) {
            lock.lock(); defer { lock.unlock() }
            self.id = id
            if cancelled { PHAssetResourceManager.default().cancelDataRequest(id) }
        }
        func cancel() {
            lock.lock(); defer { lock.unlock() }
            cancelled = true
            if let id { PHAssetResourceManager.default().cancelDataRequest(id) }
        }
    }

    private final class FailureBox: @unchecked Sendable {
        private let lock = NSLock()
        private var error: Error?
        func set(_ error: Error) { lock.lock(); self.error = self.error ?? error; lock.unlock() }
        var value: Error? { lock.lock(); defer { lock.unlock() }; return error }
    }
}
