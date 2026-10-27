import Foundation
import Photos

protocol PhotoLibrarySaving: Sendable {
    func saveVideo(at url: URL) async throws
}

struct PhotoLibrarySaver: PhotoLibrarySaving {
    private let authorize: @Sendable () async -> PHAuthorizationStatus
    private let writeVideo: @Sendable (URL) async throws -> Void

    init(
        authorize: @escaping @Sendable () async -> PHAuthorizationStatus = {
            await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        },
        writeVideo: @escaping @Sendable (URL) async throws -> Void = Self.writeVideo
    ) {
        self.authorize = authorize
        self.writeVideo = writeVideo
    }

    func saveVideo(at url: URL) async throws {
        try Task.checkCancellation()
        let authorization = await authorize()
        guard authorization == .authorized || authorization == .limited else { throw PhotoLibraryError.notAuthorized }
        try Task.checkCancellation()
        do {
            try await writeVideo(url)
        } catch {
            let failure = error as NSError
            guard failure.domain == PHPhotosErrorDomain,
                  failure.code == PHPhotosError.Code.operationInterrupted.rawValue else { throw error }
            // Photos defines 3301 as a failed, transient transaction. Keep the
            // same export alive until its one recovery attempt completes.
            try await Task.sleep(for: .milliseconds(500))
            try await writeVideo(url)
        }
    }

    static func writeVideo(at url: URL) async throws {
        try await PHPhotoLibrary.shared().performChanges {
            PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: url)
        }
    }
}

enum PhotoLibraryError: Error, LocalizedError {
    case notAuthorized
    var errorDescription: String? { "Kria needs Photos access to save this video." }
}
