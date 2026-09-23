import SwiftUI
import AVKit
import KriaMediaEngine
import UniformTypeIdentifiers

struct DeviceRenderPanel: View {
    let key: DeviceRenderKey
    let sessions: DeviceRenderSessions
    let retry: () async -> Void
    @State private var showsSourceRecovery = false
    var body: some View {
        VStack(spacing: 12) {
        DeviceRenderStatusCard(
            presentation: sessions.presentations[key] ?? DeviceRenderPresentation(phase: .preparing),
            retry: { Task { await retry() } },
            stop: { Task { await sessions.cancel(key) } },
            retryNeedsAttention: {
                let ok = await sessions.retryNeedsAttention(key)
                if ok { await retry() }
            }
        )
        if [.needsAttention, .cancelled].contains(sessions.presentations[key]?.phase ?? .preparing) {
            Button("Find original files") { showsSourceRecovery = true }
                .buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("device-render-find-originals")
        }
        }
        .sheet(isPresented: $showsSourceRecovery) {
            DeviceSourceRecoveryView(key: key, sessions: sessions, retry: retry)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }
}

private struct DeviceSourceRecoveryView: View {
    let key: DeviceRenderKey
    let sessions: DeviceRenderSessions
    let retry: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var targets: [DeviceRelinkTarget] = []
    @State private var loading = true
    @State private var message: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Find your originals").font(KriaFont.display(26))
                Text("Choose the original files used for this project. Kria checks that they match and keeps them on this iPhone.")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                if loading { ProgressView().accessibilityLabel("Checking original files") }
                ForEach(targets) { target in
                    DeviceOriginalRelinkRow(target: target) { file in
                        try await sessions.relink(target, for: key, from: file)
                        await refresh()
                        if targets.isEmpty && message == nil { await retry(); dismiss() }
                    }
                }
                if !loading && targets.isEmpty && message == nil {
                    Text("The original files are available on this iPhone.").font(KriaFont.body(14))
                }
                if let message { Text(message).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc) }
                Button("Done") { dismiss() }.buttonStyle(KriaSecondaryButtonStyle())
            }
            .padding(24)
        }
        .background(KriaColor.paper)
        .task { await refresh() }
    }

    private func refresh() async {
        loading = true
        defer { loading = false }
        do { targets = try await sessions.sourcesNeedingRelink(key); message = nil }
        catch { message = "Kria couldn’t check these files. Try again when the project has loaded." }
    }
}

private struct DeviceOriginalRelinkRow: View {
    let target: DeviceRelinkTarget
    let relink: (URL) async throws -> Void
    @State private var selecting = false
    @State private var checking = false
    @State private var message: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { selecting = true } label: {
                Label("Find \(target.title.lowercased())", systemImage: "folder")
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(checking)
            if checking { ProgressView("Checking original…").font(KriaFont.body(13)) }
            if let message { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
        }
        .fileImporter(isPresented: $selecting, allowedContentTypes: [.movie, .image, .audio], allowsMultipleSelection: false) { result in
            guard case .success(let files) = result, let file = files.first else { return }
            checking = true; message = nil
            Task {
                defer { checking = false }
                do { try await relink(file) }
                catch APIError.conflict { message = "A newer edit is available. Close this sheet and open it again." }
                catch { message = "That file couldn’t be verified. Choose the unmodified original used for this project." }
            }
        }
    }
}

/// `needsAttention` copy and retry-affordance rules, pulled out of `DeviceRenderStatusCard`
/// so they're unit-testable (mirrors `DeviceRenderButtonTitle`'s split in `DeviceRenderSessions.swift`).
enum DeviceRenderAttentionCopy {
    /// `reason_code`-keyed copy for a `needsAttention` phase. Falls back to a
    /// short server `reason` string, then a generic line, so an unrecognized
    /// or missing code never blanks the row.
    static let reasonMessage: [String: String] = [
        "export_failed": "The render couldn’t finish on this iPhone. Your project is saved.",
        "insufficient_storage": "This iPhone is low on storage. Free up space, then try again.",
        "thermal": "This iPhone needs to cool down before rendering again.",
        // Structural, not transient: retrying compiles the same recipe again and fails the
        // same way, so the "Try again" button is hidden for this reason (see `showsRetryButton`).
        "unsupported_recipe": "This edit uses something this iPhone can’t render yet. Start a new edit with a different style instead — trying again won’t change the result.",
        // Distinct from `unsupported_recipe`: the renderer itself is out of date for this
        // recipe (version/schema mismatch), not a specific feature it can't produce.
        // "Start a new edit" wouldn't fix that -- updating the app would.
        "renderer_outdated": "Update Kria to render this edit.",
        "cancelled_by_user": "Rendering was stopped. Your project is saved.",
    ]

    static func message(phase: DeviceRenderPhase, reasonCode: String?, fallback: String?) -> String? {
        guard phase == .needsAttention else { return fallback }
        if let reasonCode, let mapped = reasonMessage[reasonCode] { return mapped }
        if let fallback, fallback.count <= 160 { return fallback }
        return "Your project is saved. You can wait and try again."
    }

    /// `unsupported_recipe` means the compiled recipe itself is outside what this
    /// renderer can produce, not a transient device condition -- a blind retry
    /// would recompile the identical recipe and fail identically, so the button
    /// is hidden rather than offered. Every other `needsAttention` reason
    /// (thermal, storage, export failure) is transient and stays retryable.
    static func showsRetryButton(phase: DeviceRenderPhase, reasonCode: String?) -> Bool {
        guard [.needsAttention, .cancelled, .localReady].contains(phase) else { return false }
        // `renderer_outdated` is structural the same way `unsupported_recipe` is: retrying
        // recompiles the identical, still-too-new recipe and fails identically.
        let structuralReasons: Set<String> = ["unsupported_recipe", "renderer_outdated"]
        guard phase == .needsAttention, let reasonCode else { return true }
        return !structuralReasons.contains(reasonCode)
    }
}

struct DeviceRenderStatusCard: View {
    let presentation: DeviceRenderPresentation
    let retry: () -> Void
    let stop: () -> Void
    /// Calls the server `/device-render/retry` endpoint and re-reconciles.
    /// Nil in previews/older call sites, which fall back to the plain `retry`.
    var retryNeedsAttention: (() async -> Void)? = nil
    /// Injectable so the save action doesn't require Photos authorization in
    /// tests/previews — matches `EditorViews`' existing use of the same protocol.
    var photoLibrarySaver: any PhotoLibrarySaving = PhotoLibrarySaver()
    @State private var playback: LocalPlayback?
    @State private var isSaving = false
    @State private var saveMessage: String?
    @State private var isRetryingFailure = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(KriaFont.body(18)).bold()
            Text(detail).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
            if [.preparing, .rendering, .syncing].contains(presentation.phase) {
                ProgressView().accessibilityLabel(title)
            }
            if let message = needsAttentionAwareMessage {
                Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if let file = presentation.localFile {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { localActions(file) }
                    VStack(alignment: .leading, spacing: 12) { localActions(file) }
                }
                if let saveMessage {
                    Text(saveMessage).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                        .accessibilityIdentifier("device-render-save-message")
                }
            }
            if DeviceRenderAttentionCopy.showsRetryButton(phase: presentation.phase, reasonCode: presentation.reasonCode) {
                Button(presentation.localFile == nil ? "Try again" : "Retry sync") {
                    guard presentation.requiresServerRetry, let retryNeedsAttention else {
                        retry(); return
                    }
                    Task {
                        isRetryingFailure = true
                        await retryNeedsAttention()
                        isRetryingFailure = false
                    }
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(isRetryingFailure)
                .accessibilityIdentifier("device-render-retry")
            }
            if [.preparing, .rendering].contains(presentation.phase) {
                Button("Stop rendering", action: stop)
                    .font(KriaFont.body(14))
                    .frame(minHeight: 44)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.paper)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("device-render-status")
        .sheet(item: $playback) { local in LocalExportPlayback(file: local.url) }
    }

    @ViewBuilder private func localActions(_ file: URL) -> some View {
        Button("Play video") { playback = LocalPlayback(url: file) }
            .buttonStyle(KriaSecondaryButtonStyle())
        // The local file is already fully rendered on-device — unlike
        // EditorViews.saveToPhotos (cloud path), there's nothing to download first.
        Button {
            Task { await save(file) }
        } label: {
            if isSaving { ProgressView() } else { Label("Save", systemImage: "square.and.arrow.down") }
        }
        .buttonStyle(KriaSecondaryButtonStyle())
        .disabled(isSaving)
        .accessibilityIdentifier("device-render-save-to-photos")
        ShareLink(item: file) { Label("Share", systemImage: "square.and.arrow.up") }
            .buttonStyle(KriaSecondaryButtonStyle())
    }

    private func save(_ file: URL) async {
        isSaving = true
        saveMessage = nil
        defer { isSaving = false }
        do {
            try await photoLibrarySaver.saveVideo(at: file)
            saveMessage = "Saved to Photos."
        } catch {
            saveMessage = error.localizedDescription
        }
    }

    private var needsAttentionAwareMessage: String? {
        DeviceRenderAttentionCopy.message(phase: presentation.phase, reasonCode: presentation.reasonCode, fallback: presentation.message)
    }

    private var title: String {
        switch presentation.phase {
        case .preparing: "Preparing on your iPhone"
        case .rendering: "Rendering on your iPhone"
        case .localReady: "Ready on this iPhone"
        case .syncing: "Syncing your video"
        case .synced: "Your video is synced"
        case .cancelled: "Rendering stopped"
        case .needsAttention: "This edit needs your attention"
        case .superseded: "A newer edit is available"
        }
    }
    private var detail: String {
        switch presentation.phase {
        case .preparing: "Getting the original footage and selected assets ready."
        case .rendering: "Keep Kria open while the video finishes."
        case .localReady: "You can watch and share it now. It will appear on your other devices after syncing."
        case .syncing: "Your video is ready to watch and share while it uploads."
        case .synced: "Available in your Gallery and on your other devices."
        case .cancelled: "Your project and original footage are saved."
        case .needsAttention: "Your project is saved. You can wait and try again."
        case .superseded: "Kria will use the latest approved version."
        }
    }
}

#Preview("Rendering on iPhone") {
    DeviceRenderStatusCard(presentation: DeviceRenderPresentation(phase: .rendering), retry: {}, stop: {})
        .padding().background(KriaColor.paper)
}

#Preview("Ready to sync") {
    DeviceRenderStatusCard(presentation: DeviceRenderPresentation(phase: .localReady,
        localFile: URL(fileURLWithPath: "/preview.mp4"), message: "Syncing didn’t finish. Your video is saved on this iPhone."), retry: {}, stop: {})
        .padding().background(KriaColor.paper)
}

private struct LocalPlayback: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct LocalExportPlayback: View {
    let file: URL
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    var body: some View {
        NavigationStack {
            VideoPlayer(player: player)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .task { player = AVPlayer(url: file); player?.play() }
                .onDisappear { player?.pause(); player = nil }
        }
    }
}
