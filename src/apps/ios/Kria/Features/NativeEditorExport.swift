import Foundation
import SwiftUI

/// Header Save states. A clean editor shows a quiet checkmark rather than an
/// actionable glyph, so a disabled Save can never read as a download control.
enum NativeEditorSaveControl: Equatable {
    case saved, unsaved, saving

    init(isSaving: Bool, hasUnsavedChanges: Bool) {
        self = isSaving ? .saving : hasUnsavedChanges ? .unsaved : .saved
    }

    var isEnabled: Bool { self == .unsaved }

    var accessibilityLabel: String {
        switch self {
        case .saved: "Saved"
        case .unsaved: "Save changes"
        case .saving: "Saving"
        }
    }
}

enum NativeEditorExportError: Error, LocalizedError, Equatable {
    case blocked(String)
    case stillRendering
    case renderFailed
    case unavailable

    var errorDescription: String? {
        switch self {
        case .blocked(let reason): reason
        case .stillRendering: "This video is still rendering. Try again when it’s ready."
        case .renderFailed: "This video didn’t finish rendering, so there’s nothing to export yet."
        case .unavailable: "This video isn’t ready to export yet."
        }
    }
}

/// Copies a finished render into a private temporary MP4. Photos and the share
/// sheet need a local file: signed URLs expire and would share as a link.
struct RenderedVideoDownloader: Sendable {
    typealias Fetch = @Sendable (URL) async throws -> (URL, URLResponse)
    private let fetch: Fetch

    init(fetch: @escaping Fetch = { try await URLSession.shared.download(from: $0) }) {
        self.fetch = fetch
    }

    func localCopy(of source: URL) async throws -> URL {
        let destination = FileManager.default.temporaryDirectory
            .appending(path: "kria-\(UUID().uuidString).mp4")
        if source.isFileURL {
            try FileManager.default.copyItem(at: source, to: destination)
            return destination
        }
        let (temporary, response) = try await fetch(source)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            try? FileManager.default.removeItem(at: temporary)
            throw APIError.requestFailed
        }
        try FileManager.default.moveItem(at: temporary, to: destination)
        return destination
    }
}

enum NativeEditorExportPhase: Equatable {
    case idle
    case preparing
    case savedToPhotos
    case failed(String)
}

/// Owns export side effects for one editor screen. The session decides what is
/// exportable; this downloads it once, hands it to Photos or the share sheet,
/// and always removes its temporary copy.
@MainActor final class NativeEditorExporter: ObservableObject {
    @Published private(set) var phase: NativeEditorExportPhase = .idle
    @Published var isSharing = false
    @Published private(set) var sharedFile: URL?
    private let downloader: RenderedVideoDownloader
    private let photos: any PhotoLibrarySaving
    private var dismissTask: Task<Void, Never>?

    init(downloader: RenderedVideoDownloader = RenderedVideoDownloader(), photos: any PhotoLibrarySaving = PhotoLibrarySaver()) {
        self.downloader = downloader
        self.photos = photos
    }

    func saveToPhotos(from session: NativeEditorSession) async {
        guard let file = await prepare(from: session) else { return }
        defer { try? FileManager.default.removeItem(at: file) }
        do {
            try await photos.saveVideo(at: file)
            finish(.savedToPhotos)
        } catch {
            finish(.failed(error.localizedDescription))
        }
    }

    func share(from session: NativeEditorSession) async {
        guard let file = await prepare(from: session) else { return }
        removeSharedFile()
        sharedFile = file
        phase = .idle
        isSharing = true
    }

    func removeSharedFile() {
        guard let sharedFile else { return }
        try? FileManager.default.removeItem(at: sharedFile)
        self.sharedFile = nil
    }

    func dismissStatus() {
        dismissTask?.cancel()
        phase = .idle
    }

    private func prepare(from session: NativeEditorSession) async -> URL? {
        guard phase != .preparing else { return nil }
        dismissTask?.cancel()
        phase = .preparing
        do {
            let render = try await session.exportRenderURL()
            return try await downloader.localCopy(of: render)
        } catch {
            finish(.failed(error.localizedDescription))
            return nil
        }
    }

    private func finish(_ next: NativeEditorExportPhase) {
        phase = next
        guard next == .savedToPhotos else { return }
        dismissTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            self?.phase = .idle
        }
    }
}

struct NativeEditorExportBanner: View {
    @ObservedObject var exporter: NativeEditorExporter

    @ViewBuilder var body: some View {
        switch exporter.phase {
        case .idle:
            EmptyView()
        case .preparing:
            NativeEditorBannerRow(
                title: "Preparing your video",
                detail: "Downloading the finished render.",
                systemImage: "arrow.down.circle",
                tint: KriaColor.ink,
                identifier: "native-editor-export-state"
            )
        case .savedToPhotos:
            NativeEditorBannerRow(
                title: "Saved to Photos",
                detail: "Your video is in your photo library.",
                systemImage: "checkmark.circle",
                tint: KriaColor.success,
                identifier: "native-editor-export-state"
            )
        case .failed(let message):
            Button(action: exporter.dismissStatus) {
                NativeEditorBannerRow(
                    title: "Couldn’t export this video",
                    detail: message,
                    systemImage: "exclamationmark.triangle",
                    tint: .red,
                    identifier: "native-editor-export-state"
                )
            }
            .buttonStyle(.plain)
            .accessibilityHint("Dismiss")
        }
    }
}
