import Foundation
import SwiftUI

/// Header Save states. A clean editor shows a quiet checkmark rather than an
/// actionable glyph, so a disabled Save can never read as a broken download
/// control — the confusion this fixes (KRI-91).
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

enum NativeEditorExportPhase: Equatable {
    case idle
    case preparing
    case savedToPhotos
    case failed(String)
}

/// A file ready to hand to Photos or the share sheet, and whether this class
/// owns it. Kria renders everything on device: exporting always produces
/// exactly what the preview shows, never a possibly-different cloud render —
/// see ``NativeEditorSession/videoDownloadRoute(deviceLocalFile:)``. Only the
/// `.sourcePreview` (freshly compiled) and `.server` (downloaded) routes
/// leave behind a file this class must clean up; a `.localFile` route points
/// at a device render `NativeEditorExporter` does not own.
private struct ResolvedExportFile {
    let fileURL: URL
    let cleanupURL: URL?
}

/// Owns export side effects for one editor screen. The session decides what
/// is exportable and how to get it (compile the live preview, reuse a device
/// render, or — only when the player itself is showing the rendered
/// fallback — download the matching cloud render); this class resolves that
/// route once, hands the result to Photos or the share sheet, and always
/// removes any temporary copy it made.
@MainActor final class NativeEditorExporter: ObservableObject {
    @Published private(set) var phase: NativeEditorExportPhase = .idle
    @Published var isSharing = false
    @Published private(set) var sharedFile: URL?
    private let photos: any PhotoLibrarySaving
    private let downloader: NativeEditorVideoDownloader
    private var sharedFileIsOwned = false
    private var dismissTask: Task<Void, Never>?

    init(
        photos: any PhotoLibrarySaving = PhotoLibrarySaver(),
        downloader: NativeEditorVideoDownloader = NativeEditorVideoDownloader()
    ) {
        self.photos = photos
        self.downloader = downloader
    }

    func saveToPhotos(from session: NativeEditorSession, api: any KriaAPIClient, deviceLocalFile: URL?) async {
        guard let resolved = await prepare(from: session, api: api, deviceLocalFile: deviceLocalFile) else { return }
        defer { if let cleanupURL = resolved.cleanupURL { try? FileManager.default.removeItem(at: cleanupURL) } }
        do {
            try await photos.saveVideo(at: resolved.fileURL)
            finish(.savedToPhotos)
        } catch {
            finish(.failed(error.localizedDescription))
        }
    }

    func share(from session: NativeEditorSession, api: any KriaAPIClient, deviceLocalFile: URL?) async {
        guard let resolved = await prepare(from: session, api: api, deviceLocalFile: deviceLocalFile) else { return }
        removeSharedFile()
        sharedFileIsOwned = resolved.cleanupURL != nil
        sharedFile = resolved.fileURL
        phase = .idle
        isSharing = true
    }

    func removeSharedFile() {
        guard let sharedFile else { return }
        if sharedFileIsOwned { try? FileManager.default.removeItem(at: sharedFile) }
        self.sharedFile = nil
        sharedFileIsOwned = false
    }

    func dismissStatus() {
        dismissTask?.cancel()
        phase = .idle
    }

    private func prepare(from session: NativeEditorSession, api: any KriaAPIClient, deviceLocalFile: URL?) async -> ResolvedExportFile? {
        guard phase != .preparing else { return nil }
        dismissTask?.cancel()
        phase = .preparing
        do {
            switch try session.videoDownloadRoute(deviceLocalFile: deviceLocalFile) {
            case .sourcePreview:
                let exported = try await session.exportDisplayedSourcePreview()
                return ResolvedExportFile(fileURL: exported.fileURL, cleanupURL: exported.cleanupURL)
            case let .localFile(file):
                return ResolvedExportFile(fileURL: file, cleanupURL: nil)
            case let .server(target):
                let file = try await downloader.download(api: api, target: target)
                return ResolvedExportFile(fileURL: file, cleanupURL: file)
            }
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
                detail: "Exporting the video shown in the preview.",
                systemImage: "arrow.down.circle",
                tint: KriaColor.ink,
                identifier: "native-editor-export-state"
            )
        case .savedToPhotos:
            NativeEditorBannerRow(
                title: "Saved to Photos",
                detail: "Your video is in your photo library.",
                systemImage: "checkmark.circle",
                tint: KriaColor.ink,
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
