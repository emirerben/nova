import Foundation
import Photos
import SwiftUI
import UIKit

@MainActor final class SlidePostExporter: ObservableObject {
    typealias Download = (URL) async throws -> (URL, URLResponse)
    typealias PhotosAuthorization = () async -> PHAuthorizationStatus
    typealias AtomicPhotoWrite = ([(URL, String)]) async throws -> Void
    typealias Revalidate = () async throws -> Void
    @Published private(set) var message: String?
    @Published private(set) var isWorking = false
    @Published var shareItems: [URL] = []
    @Published var isSharing = false
    private let downloadFile: Download
    private let authorizePhotos: PhotosAuthorization
    private let writePhotos: AtomicPhotoWrite
    private let defaults: UserDefaults

    init(
        downloadFile: @escaping Download = { try await URLSession.shared.download(from: $0) },
        authorizePhotos: @escaping PhotosAuthorization = { await PHPhotoLibrary.requestAuthorization(for: .addOnly) },
        writePhotos: @escaping AtomicPhotoWrite = { resources in
            try await PHPhotoLibrary.shared().performChanges {
                for (url, kind) in resources {
                    let request = PHAssetCreationRequest.forAsset()
                    request.addResource(with: kind == "video" ? .video : .photo, fileURL: url, options: nil)
                }
            }
        },
        defaults: UserDefaults = .standard
    ) {
        self.downloadFile = downloadFile; self.authorizePhotos = authorizePhotos
        self.writePhotos = writePhotos; self.defaults = defaults
    }

    func saveToPhotos(session: SlidePostSession, revalidate: Revalidate) async {
        guard !isWorking else { return }
        isWorking = true; defer { isWorking = false }
        do {
            let permission = await authorizePhotos()
            guard permission == .authorized || permission == .limited else { throw SlidePostExportError.photosDenied }
            try await revalidate()
            guard let snapshot = snapshot(from: session) else { return }
            if receiptExists(itemID: snapshot.itemID, version: snapshot.version) {
                message = "This version is already saved to Photos."
                return
            }
            let files = try await download(snapshot)
            defer { try? FileManager.default.removeItem(at: files.directory) }
            try await revalidate()
            // Do not commit a now-obsolete render after a refresh/edit landed
            // while its ordered media was downloading.
            guard session.canExport, session.state?.draft?.version == snapshot.version else {
                message = "A newer post version is ready. Download it before saving."
                return
            }
            try await writePhotos(files.ordered.map { ($0.url, $0.kind) })
            saveReceipt(itemID: snapshot.itemID, version: snapshot.version)
            message = "Saved \(files.ordered.count) slides to Photos."
        } catch { message = error.localizedDescription }
    }

    func prepareShare(session: SlidePostSession, revalidate: Revalidate) async {
        guard !isWorking else { return }
        isWorking = true; defer { isWorking = false }
        do {
            try await revalidate()
            guard let snapshot = snapshot(from: session) else { return }
            let files = try await download(snapshot)
            var retainsDirectoryForShare = false
            defer {
                if !retainsDirectoryForShare { try? FileManager.default.removeItem(at: files.directory) }
            }
            try await revalidate()
            guard session.canExport, session.state?.draft?.version == snapshot.version else {
                message = "A newer post version is ready. Download it before sharing."
                return
            }
            let caption = files.directory.appending(path: "caption.txt")
            do {
                guard let captionData = snapshot.caption.data(using: .utf8) else { throw SlidePostExportError.downloadFailed }
                try captionData.write(to: caption, options: .atomic)
            } catch {
                throw error
            }
            shareItems = files.ordered.map(\.url) + [caption]; isSharing = true
            retainsDirectoryForShare = true
        } catch { message = error.localizedDescription }
    }

    func discardShareDirectory() {
        if let first = shareItems.first { try? FileManager.default.removeItem(at: first.deletingLastPathComponent()) }
        shareItems = []
        isSharing = false
    }

    private func snapshot(from session: SlidePostSession) -> Snapshot? {
        guard session.canExport, let state = session.state, let draft = state.draft else {
            message = "Prepare the current saved version before exporting."
            return nil
        }
        let ordered = zip(draft.slides, state.slides).compactMap { draftSlide, rendered -> Item? in
            guard draftSlide.id == rendered.id, let url = rendered.url, url.scheme == "https" else { return nil }
            return Item(url: url, kind: rendered.kind, index: 0)
        }
        guard ordered.count == draft.slides.count else { message = "This render is incomplete. Retry preparation before exporting."; return nil }
        return Snapshot(itemID: state.itemID, version: draft.version, caption: draft.caption, items: ordered.enumerated().map { Item(url: $0.element.url, kind: $0.element.kind, index: $0.offset) })
    }

    private func download(_ snapshot: Snapshot) async throws -> DownloadedFiles {
        let directory = FileManager.default.temporaryDirectory.appending(path: "kria-slide-post-\(snapshot.itemID)-\(snapshot.version)-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        do {
            var ordered: [Downloaded] = []
            for item in snapshot.items {
                let (temporary, response) = try await downloadFile(item.url)
                guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw SlidePostExportError.downloadFailed }
                let ext = item.url.pathExtension.isEmpty ? (item.kind == "video" ? "mov" : "jpg") : item.url.pathExtension
                let target = directory.appending(path: String(format: "%02d", item.index + 1) + "." + ext)
                try FileManager.default.moveItem(at: temporary, to: target)
                ordered.append(Downloaded(url: target, kind: item.kind))
            }
            return DownloadedFiles(directory: directory, ordered: ordered)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
    }

    private func receiptExists(itemID: String, version: Int) -> Bool { defaults.bool(forKey: "kria.slide-post.photos.\(itemID).\(version)") }
    private func saveReceipt(itemID: String, version: Int) { defaults.set(true, forKey: "kria.slide-post.photos.\(itemID).\(version)") }
    private struct Snapshot { let itemID: String; let version: Int; let caption: String; let items: [Item] }
    private struct Item { let url: URL; let kind: String; let index: Int }
    private struct Downloaded { let url: URL; let kind: String }
    private struct DownloadedFiles { let directory: URL; let ordered: [Downloaded] }
}

private enum SlidePostExportError: LocalizedError { case photosDenied, downloadFailed
    var errorDescription: String? { self == .photosDenied ? "Allow Photos access to save these slides, or use Share to save them in Files." : "Kria couldn’t download every slide for export." }
}

struct SlidePostShareSheet: UIViewControllerRepresentable {
    let items: [URL]
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: items, applicationActivities: nil) }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
