#if DEBUG
import Foundation
import UniformTypeIdentifiers

/// UI tests only (`-ui-testing-chat` + `KRIA_SLIDE_POST_FIXTURE=1`): the sticker upload without storage. The real uploader
/// PUTs the bytes to a signed storage URL from a background URLSession, which a UI test cannot answer, so this one
/// speaks the same two API calls (reserve a slot, register the picture) to the fixture API and keeps the file on disk,
/// where the fixture serves it back as the asset's original. Everything after that (pool refresh, filename match, the
/// ready asset, the sticker) is the production code path.
@MainActor final class SlidePostFixtureStickerUploader: SlidePostStickerUploading {
    private let api: any KriaAPIClient
    init(api: any KriaAPIClient) { self.api = api }

    static var isActive: Bool {
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") && ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_FIXTURE"] == "1"
    }

    func uploadSticker(fileURL: URL, projectID: UUID, itemID: String) async -> Bool {
        let filename = fileURL.lastPathComponent
        do {
            let size = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { $0 } ?? 0
            let type = UTType(filenameExtension: fileURL.pathExtension)?.preferredMIMEType ?? "image/png"
            let target = try await api.reserveVisualUpload(itemID: itemID, clientUploadID: "fixture-\(UUID().uuidString)", filename: filename, contentType: type, size: Int64(size))
            let kept = FileManager.default.temporaryDirectory.appending(path: "fixture-sticker-\(filename)")
            try? FileManager.default.removeItem(at: kept)
            try FileManager.default.copyItem(at: fileURL, to: kept)
            _ = try await api.registerVisual(itemID: itemID, reservationID: target.reservationID, gcsPath: kept.absoluteString, contentType: type, filename: filename)
            try? FileManager.default.removeItem(at: fileURL)
            return true
        } catch { return false }
    }
}
#endif
