import Foundation
import Photos
import SwiftUI
import UIKit

@MainActor final class SlidePostExporter: ObservableObject {
    typealias Download = (URL) async throws -> (URL, URLResponse)
    typealias PhotosAuthorization = () async -> PHAuthorizationStatus
    typealias AtomicPhotoWrite = ([PhotoResource]) async throws -> Void
    typealias Revalidate = () async throws -> Void

    /// One slide handed to Photos. `creationDate` ascends with slide order so Photos' date sort
    /// (the only order it has; we deliberately create no album) shows the slides in post order.
    struct PhotoResource: Equatable {
        let url: URL
        let kind: String
        let creationDate: Date
    }

    /// What the export banner shows.
    enum Status: Equatable {
        case idle
        case saving
        case rendering
        case preparing(done: Int, total: Int)
        case savedToPhotos(Int)
        case photosDenied
        case notice(String)
        case failed(String)
    }
    enum Destination { case photos, share }

    @Published private(set) var status: Status = .idle
    @Published private(set) var isWorking = false
    /// True for the whole save -> generate -> wait -> export flow, not just the file work.
    @Published private(set) var isFlowActive = false
    @Published var shareItems: [URL] = []
    @Published var isSharing = false
    var isBusy: Bool { isWorking || isFlowActive }
    var message: String? {
        switch status {
        case .idle, .saving, .rendering: nil
        case .preparing(let done, let total): "Preparing \(done)/\(total)"
        case .savedToPhotos(let count): "Saved \(count) slides to Photos."
        case .photosDenied: SlidePostExportError.photosDenied.errorDescription
        case .notice(let text), .failed(let text): text
        }
    }
    private let downloadFile: Download
    private let authorizePhotos: PhotosAuthorization
    private let writePhotos: AtomicPhotoWrite
    private let now: () -> Date
    private var dismissTask: Task<Void, Never>?

    /// UI tests (`KRIA_SLIDE_POST_FIXTURE_PHOTOS=1`) save through a stub: no Photos permission prompt,
    /// no network, nothing written to the simulator library.
    static func makeDefault() -> SlidePostExporter {
        guard ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_FIXTURE_PHOTOS"] == "1" else { return SlidePostExporter() }
        return SlidePostExporter(
            downloadFile: { url in
                let file = FileManager.default.temporaryDirectory.appending(path: "fixture-\(UUID().uuidString)")
                try Data("fixture".utf8).write(to: file)
                return (file, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            },
            authorizePhotos: { .authorized },
            writePhotos: { _ in }
        )
    }

    init(
        downloadFile: @escaping Download = { try await URLSession.shared.download(from: $0) },
        authorizePhotos: @escaping PhotosAuthorization = { await PHPhotoLibrary.requestAuthorization(for: .addOnly) },
        writePhotos: @escaping AtomicPhotoWrite = { resources in
            try await PHPhotoLibrary.shared().performChanges {
                for resource in resources {
                    let request = PHAssetCreationRequest.forAsset()
                    request.creationDate = resource.creationDate
                    request.addResource(with: resource.kind == "video" ? .video : .photo, fileURL: resource.url, options: nil)
                }
            }
        },
        now: @escaping () -> Date = Date.init
    ) {
        self.downloadFile = downloadFile; self.authorizePhotos = authorizePhotos
        self.writePhotos = writePhotos; self.now = now
    }

    /// Ascending, one second apart, ending at `now` so nothing is dated in the future.
    static func creationDates(count: Int, now: Date) -> [Date] {
        (0..<count).map { now.addingTimeInterval(TimeInterval($0 - count + 1)) }
    }

    func dismissStatus() { dismissTask?.cancel(); status = .idle }
    private func set(_ new: Status, autoDismiss: Bool = false) {
        dismissTask?.cancel()
        status = new
        guard autoDismiss else { return }
        dismissTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            self?.status = .idle
        }
    }

    /// Save to Photos / Share from ANY state: save a dirty draft, generate when no current render
    /// exists, wait for the render, then export. Errors land in `status`; nothing throws.
    func export(
        _ destination: Destination, session: SlidePostSession, api: any KriaAPIClient, itemID: String,
        pollInterval: Duration = .seconds(1.5), maxPolls: Int = 200
    ) async {
        guard !isBusy else { return }
        isFlowActive = true; defer { isFlowActive = false }
        session.error = nil
        // Ask for Photos access BEFORE the (possibly long) render so a first-time prompt or a denial
        // never comes after the user has already waited for it.
        if destination == .photos {
            let permission = await authorizePhotos()
            guard permission == .authorized || permission == .limited else { set(.photosDenied); return }
        }
        var generated = 0
        var polls = 0
        while !session.canExport {
            // The caller cancels this task when the editor goes away; stop instead of finishing in the background.
            if Task.isCancelled { set(.idle); return }
            if session.hasUnsavedChanges || (!session.isRendering && generated < 2) {
                // `create` saves a dirty draft first, then dispatches the render.
                set(session.hasUnsavedChanges ? .saving : .rendering)
                if !session.hasUnsavedChanges { generated += 1 }
                await session.create(api: api, itemID: itemID)
                if let error = session.error { set(.failed(error)); return }
                continue
            }
            guard polls < maxPolls else { set(.failed("Your post is still rendering. Try again in a moment.")); return }
            polls += 1
            set(.rendering)
            try? await Task.sleep(for: pollInterval)
            if Task.isCancelled { set(.idle); return }
            await session.refresh(api: api, itemID: itemID)
            if let error = session.error { set(.failed(error)); return }
            if session.state?.renderStatus == "failed" { set(.failed("Rendering failed. Try again.")); return }
        }
        let revalidate: Revalidate = { try await session.revalidateForExport(api: api, itemID: itemID) }
        switch destination {
        case .photos: await saveToPhotos(session: session, revalidate: revalidate)
        case .share: await prepareShare(session: session, revalidate: revalidate)
        }
    }

    func saveToPhotos(session: SlidePostSession, revalidate: Revalidate) async {
        guard !isWorking else { return }
        isWorking = true; defer { isWorking = false }
        do {
            let permission = await authorizePhotos()
            guard permission == .authorized || permission == .limited else { throw SlidePostExportError.photosDenied }
            set(.preparing(done: 0, total: session.state?.draft?.slides.count ?? 0))
            try await revalidate()
            guard let snapshot = snapshot(from: session) else { return }
            let files = try await download(snapshot)
            defer { try? FileManager.default.removeItem(at: files.directory) }
            try await revalidate()
            // Do not commit a now-obsolete render after a refresh/edit landed
            // while its ordered media was downloading.
            guard session.canExport, session.state?.draft?.version == snapshot.version else {
                set(.notice("A newer post version is ready. Download it before saving."))
                return
            }
            let dates = Self.creationDates(count: files.ordered.count, now: now())
            try await writePhotos(zip(files.ordered, dates).map { PhotoResource(url: $0.url, kind: $0.kind, creationDate: $1) })
            set(.savedToPhotos(files.ordered.count), autoDismiss: true)
        } catch let error as SlidePostExportError where error == .photosDenied {
            set(.photosDenied)
        } catch { set(.failed(error.localizedDescription)) }
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
                set(.notice("A newer post version is ready. Download it before sharing."))
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
            set(.idle)
        } catch { set(.failed(error.localizedDescription)) }
    }

    func discardShareDirectory() {
        if let first = shareItems.first { try? FileManager.default.removeItem(at: first.deletingLastPathComponent()) }
        shareItems = []
        isSharing = false
    }

    private func snapshot(from session: SlidePostSession) -> Snapshot? {
        guard session.canExport, let state = session.state, let draft = state.draft else {
            set(.notice("Prepare the current saved version before exporting."))
            return nil
        }
        let ordered = zip(draft.slides, state.slides).compactMap { draftSlide, rendered -> Item? in
            guard draftSlide.id == rendered.id, let url = rendered.url, url.scheme == "https" else { return nil }
            return Item(url: url, kind: rendered.kind, index: 0)
        }
        guard ordered.count == draft.slides.count else { set(.notice("This render is incomplete. Retry preparation before exporting.")); return nil }
        return Snapshot(itemID: state.itemID, version: draft.version, caption: draft.caption, items: ordered.enumerated().map { Item(url: $0.element.url, kind: $0.element.kind, index: $0.offset) })
    }

    private func download(_ snapshot: Snapshot) async throws -> DownloadedFiles {
        let directory = FileManager.default.temporaryDirectory.appending(path: "kria-slide-post-\(snapshot.itemID)-\(snapshot.version)-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        do {
            var ordered: [Downloaded] = []
            for item in snapshot.items {
                set(.preparing(done: item.index, total: snapshot.items.count))
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

    private struct Snapshot { let itemID: String; let version: Int; let caption: String; let items: [Item] }
    private struct Item { let url: URL; let kind: String; let index: Int }
    private struct Downloaded { let url: URL; let kind: String }
    private struct DownloadedFiles { let directory: URL; let ordered: [Downloaded] }
}

private enum SlidePostExportError: LocalizedError, Equatable { case photosDenied, downloadFailed
    var errorDescription: String? { self == .photosDenied ? "Allow Photos access to save these slides, or use Share to save them in Files." : "Kria couldn’t download every slide for export." }
}

/// The export feedback row, in the video editor's `NativeEditorBannerRow` look.
struct SlidePostExportBanner: View {
    @ObservedObject var exporter: SlidePostExporter

    @ViewBuilder var body: some View {
        switch exporter.status {
        case .idle:
            EmptyView()
        case .saving, .rendering:
            row("Preparing your slides…", "Saving and rendering your post.", "arrow.down.circle", KriaColor.ink)
        case .preparing(let done, let total):
            row("Preparing your slides…", total > 0 ? "\(min(done + 1, total))/\(total)" : "Getting your slides ready.", "arrow.down.circle", KriaColor.ink)
        case .savedToPhotos(let count):
            row("Saved to Photos", "Saved \(count) slides to Photos.", "checkmark.circle", KriaColor.ink)
        case .photosDenied:
            dismissable("Photos access is off", exporter.message ?? "", KriaColor.ink)
        case .notice(let text):
            dismissable("Couldn’t save yet", text, KriaColor.ink)
        case .failed(let text):
            dismissable("Couldn’t export this post", text, KriaColor.ink)
        }
    }

    private func row(_ title: String, _ detail: String, _ symbol: String, _ tint: Color) -> some View {
        NativeEditorBannerRow(title: title, detail: detail, systemImage: symbol, tint: tint, identifier: "slidepost-export-state")
    }
    private func dismissable(_ title: String, _ detail: String, _ tint: Color) -> some View {
        Button(action: exporter.dismissStatus) {
            row(title, detail, "exclamationmark.triangle", tint)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Dismiss")
    }
}

struct SlidePostShareSheet: UIViewControllerRepresentable {
    let items: [URL]
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: items, applicationActivities: nil) }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
