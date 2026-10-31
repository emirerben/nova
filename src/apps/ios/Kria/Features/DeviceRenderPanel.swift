import SwiftUI
import AVKit
import KriaMediaEngine
import UniformTypeIdentifiers

struct DeviceRenderPanel: View {
    let key: DeviceRenderKey
    let sessions: DeviceRenderSessions
    var retryApproved: (() async -> Void)? = nil
    /// Opens the conversation for an edit only a change in chat can fix
    /// (`contract_changed`, `requires_change`). Nil hides the call to action.
    var reviewInChat: (() -> Void)? = nil
    let retry: () async -> Void
    @State private var showsSourceRecovery = false
    /// KRI-211: the attention state is caused by originals that are not on this
    /// iPhone. Measured (not guessed from an error string) with the same check the
    /// recovery sheet uses, so the copy and the sheet can never disagree.
    @State private var originalsMissing = false
    var body: some View {
        VStack(spacing: 12) {
        DeviceRenderStatusCard(
            presentation: sessions.presentations[key] ?? DeviceRenderPresentation(phase: .preparing),
            retry: { Task {
                // Re-sending an existing local file needs no new render consent.
                // A fresh runtime export goes through chat approval only when the
                // server really needs a new attempt; a phone-local failure (server
                // still awaiting this iPhone) is retried right here (S1-k).
                if sessions.presentations[key]?.phase == .cancelled,
                   sessions.presentations[key]?.reasonCode == DeviceRenderSessions.pausedReason {
                    await sessions.resumeFromUser(key)
                } else if let retryApproved, sessions.retryNeedsApproval(key), sessions.presentations[key]?.phase != .localReady {
                    await retryApproved()
                } else { await retry() }
            } },
            stop: { Task { await sessions.cancel(key) } },
            retryNeedsAttention: {
                if let retryApproved { await retryApproved() }
                else {
                    let ok = await sessions.retryNeedsAttentionFromUser(key)
                    if ok { await retry() }
                }
            },
            originalsMissing: originalsMissing
        )
        if let reviewInChat, DeviceRenderAttentionCopy.needsChange(sessions.presentations[key]) {
            Button("Review the updated edit", action: reviewInChat)
                .buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("device-render-review-in-chat")
        } else if let reviewInChat, DeviceRenderAttentionCopy.needsUpdate(sessions.presentations[key]) {
            // The second way forward when the build is too old: ask Kria in chat for a
            // version this iPhone can render, instead of only waiting for an update.
            Button("Review in chat", action: reviewInChat)
                .buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("device-render-review-in-chat")
        }
        if DeviceRenderAttentionCopy.offersOriginalRecovery(sessions.presentations[key]) {
            Button("Find original files") { showsSourceRecovery = true }
                .buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("device-render-find-originals")
        }
        }
        .task(id: sessions.presentations[key]?.phase) { await refreshOriginalsMissing() }
        .sheet(isPresented: $showsSourceRecovery, onDismiss: { Task { await refreshOriginalsMissing() } }) {
            DeviceSourceRecoveryView(key: key, sessions: sessions, retry: retry)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }

    private func refreshOriginalsMissing() async {
        guard sessions.presentations[key]?.phase == .needsAttention else { originalsMissing = false; return }
        let missing = (try? await sessions.sourcesNeedingRelink(key)) ?? []
        originalsMissing = !missing.isEmpty
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
                    DeviceOriginalRelinkRow(title: target.title) { file in
                        do {
                            try await sessions.relink(target, for: key, from: file)
                        } catch APIError.conflict {
                            // KRI-935: the edit moved to a newer version while this sheet was open.
                            // Re-read what the newest version needs and link the chosen file to it.
                            await refresh()
                            if let current = targets.first(where: { $0.asset.id == target.asset.id }) {
                                try await sessions.relink(current, for: key, from: file)
                            }
                        }
                        await refresh()
                        if targets.isEmpty && message == nil { await resume(); dismiss() }
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

    /// Every original is back: finish the video without another tap. A server that
    /// already recorded `original_missing` re-pins the same approved recipe directly
    /// (no new approval), then the phone reconciles and starts it.
    private func resume() async {
        if sessions.presentations[key]?.requiresServerRetry == true {
            guard await sessions.retryNeedsAttention(key) else { return }
        }
        await retry()
    }

    private func refresh() async {
        loading = true
        defer { loading = false }
        do { targets = try await sessions.sourcesNeedingRelink(key); message = nil }
        catch { message = "Kria couldn’t check these files. Try again when the project has loaded." }
    }
}

struct DeviceOriginalRelinkRow: View {
    let title: String
    let relink: (URL) async throws -> Void
    @State private var selecting = false
    @State private var checking = false
    @State private var message: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { selecting = true } label: {
                Label("Find \(title.lowercased())", systemImage: "folder")
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
                catch APIError.conflict { message = "This edit changed while Kria checked the file. Choose it again." }
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
        // same way, so "Try again" is hidden and the card offers the conversation instead
        // (a change-required reason, see `changeReasons`).
        "unsupported_recipe": "This edit uses something this iPhone can’t render yet. Your project is saved. Review it in chat and Kria will make a version this iPhone can render.",
        // Distinct from `unsupported_recipe`: the renderer itself is out of date for this
        // recipe (version/schema mismatch), not a specific feature it can't produce.
        // "Start a new edit" wouldn't fix that -- updating the app would.
        "renderer_outdated": "This edit uses something newer than this version of Kria. Your project is saved.",
        "cancelled_by_user": "You paused this render. Your project is saved; resume it any time.",
        // KRI-141: the server's stale-render reaper (`device_render_reaper.py`) gave up
        // after a day with no word from this iPhone -- usually Kria was closed or the
        // phone slept mid-render. Transient, so "Try again" stays.
        "timed_out": "Kria stopped waiting after a day without hearing from this iPhone. Your project is saved. Keep Kria open and try again.",
        // This iPhone can't render the recipe right now for a reason of its own (not
        // heat or storage). Try again comes back once the phone can render it
        // (`DeviceRenderSessions.apply` clears the reason then).
        "device_unavailable": "This iPhone can’t render this edit right now. Your project is saved.",
        // An original clip (or its analysis proxy) isn't on this iPhone; relinking fixes it.
        "original_missing": "An original video for this edit isn’t on this iPhone. Find the original file and Kria will finish the video.",
        // The approved requirements behind the pinned recipe changed. Retrying re-pins
        // the same recipe and is refused again; the next step is in chat.
        "contract_changed": "This edit’s approved details changed after it was prepared. Your project is saved. Review the updated edit in chat.",
        // The job was cancelled outside this iPhone (another device, the web, or Kria).
        DeviceRenderSessions.jobCancelledReason: "This render was stopped. Your project is saved.",
        // A decline this build made earlier no longer holds: it renders the recipe now.
        renderableReason: "Kria can render this edit on your iPhone now. Try again to start it.",
    ]

    /// Set by the phone itself (never by the server): a `renderer_outdated` or
    /// `device_unavailable` record whose recipe this build can render now. Try again
    /// re-pins it with `renders_locally`, so the server takes it.
    static let renderableReason = "renderable_now"

    /// Reasons a retry can't change: the recipe, the app version or the
    /// rollout has to change first, so "Try again" is hidden for them.
    static let structuralReasons: Set<String> = Set(["unsupported_recipe", "renderer_outdated", "device_unavailable", DeviceRenderSessions.jobCancelledReason])
        .union(changeReasons)

    /// Reasons only a change to the edit fixes: Kria re-pins the same recipe on
    /// retry, and the server refuses it again (`/device-render/retry` answers
    /// `requires_change`). The way forward is the conversation. A recipe this
    /// iPhone declined (`unsupported_recipe`) is one of them from the moment the
    /// decline is recorded, not only after a refused retry.
    static let changeReasons: Set<String> = ["contract_changed", "requires_change", "unsupported_recipe"]

    /// The build is older than the recipe: the way forward is updating Kria.
    static let updateReason = "renderer_outdated"

    /// What the creator can do next about a render that cannot finish on its own.
    /// `renderNow`: this build renders a recipe it declined earlier (renderable_now); the
    /// step starts it, nothing failed.
    enum NextStep: Equatable { case retry, resume, reviewInChat, updateApp, renderNow }

    static func nextStep(_ presentation: DeviceRenderPresentation) -> NextStep {
        if needsChange(presentation) { return .reviewInChat }
        if needsUpdate(presentation) { return .updateApp }
        if presentation.reasonCode == renderableReason { return .renderNow }
        return isPaused(presentation) ? .resume : .retry
    }

    /// Phases a change or update reason belongs to: a render that stopped here
    /// (`needsAttention`) or one whose finished video this iPhone keeps but cannot sync
    /// (`localReady`). On any other phase the reason is stale and the card shows the phase.
    static let yourTurnPhases: Set<DeviceRenderPhase> = [.needsAttention, .localReady]

    /// A quiet network wait (a failed status read) sits on top of the card and keeps
    /// its way forward; only heat, storage or checking replace it.
    static func needsChange(_ presentation: DeviceRenderPresentation?) -> Bool {
        guard let presentation, presentation.waiting?.isQuiet ?? true, yourTurnPhases.contains(presentation.phase),
              let reasonCode = presentation.reasonCode else { return false }
        return changeReasons.contains(reasonCode)
    }

    /// This build is older than the recipe: "Update Kria" (and "Review in chat"), never a retry.
    static func needsUpdate(_ presentation: DeviceRenderPresentation?) -> Bool {
        guard let presentation, presentation.waiting?.isQuiet ?? true, yourTurnPhases.contains(presentation.phase) else { return false }
        return presentation.reasonCode == updateReason
    }

    /// The creator's own Stop: shown as a resumable pause, never a failure.
    static func isPaused(_ presentation: DeviceRenderPresentation) -> Bool {
        presentation.phase == .cancelled && presentation.reasonCode != DeviceRenderSessions.jobCancelledReason
    }

    /// "Find original files" only helps a render that failed or stopped on
    /// this phone, never one waiting on the phone or blocked by the app version.
    static func offersOriginalRecovery(_ presentation: DeviceRenderPresentation?) -> Bool {
        guard let presentation, presentation.waiting == nil,
              [.needsAttention, .cancelled].contains(presentation.phase) else { return false }
        // A pause is the creator's own Stop; its originals are not the problem.
        if presentation.reasonCode == DeviceRenderSessions.pausedReason { return false }
        guard let reasonCode = presentation.reasonCode else { return true }
        if reasonCode == renderableReason { return false }
        return !structuralReasons.contains(reasonCode) && !["thermal", "insufficient_storage"].contains(reasonCode)
    }

    /// Title, detail and quiet note for a render that waits instead of failing.
    static func waitingTitle(_ wait: DeviceRenderWait) -> String? {
        switch wait {
        case .cooling: "Waiting for your iPhone to cool down"
        case .storage(let bytes): bytes > 0 ? "Free up \(formattedBytes(bytes)) to render" : "Waiting for storage"
        case .checking: "Checking your render"
        case .connection, .server: nil
        }
    }

    static func waitingDetail(_ wait: DeviceRenderWait) -> String? {
        switch wait {
        case .cooling: "Rendering starts automatically when it’s cooler. Your project is saved."
        case .storage: "This iPhone is low on storage. Rendering starts automatically once there’s room."
        case .checking: "Kria is checking this render. It continues automatically."
        case .connection, .server: nil
        }
    }

    static func waitingNote(_ wait: DeviceRenderWait) -> String? {
        switch wait {
        case .connection: "Waiting for connection. Kria will continue automatically."
        case .server: "Kria is busy. Your render will continue automatically."
        case .cooling, .storage, .checking: nil
        }
    }

    static func formattedBytes(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func message(phase: DeviceRenderPhase, reasonCode: String?, fallback: String?) -> String? {
        // A pause says everything in its title and detail.
        if phase == .cancelled, reasonCode == DeviceRenderSessions.pausedReason { return nil }
        if phase == .cancelled, let reasonCode, let mapped = reasonMessage[reasonCode] { return mapped }
        guard phase == .needsAttention else { return fallback }
        if let reasonCode, let mapped = reasonMessage[reasonCode] { return mapped }
        if let fallback, fallback.count <= 160 { return fallback }
        if let reasonCode, changeReasons.contains(reasonCode) {
            return "This edit needs a change before it can render again. Your project is saved."
        }
        return "Your project is saved. You can wait and try again."
    }

    /// `unsupported_recipe` means the compiled recipe itself is outside what this
    /// renderer can produce, not a transient device condition -- a blind retry
    /// would recompile the identical recipe and fail identically, so the button
    /// is hidden rather than offered. Every other `needsAttention` reason
    /// (thermal, storage, export failure) is transient and stays retryable.
    static func showsRetryButton(phase: DeviceRenderPhase, reasonCode: String?, originalsMissing: Bool = false) -> Bool {
        guard [.needsAttention, .cancelled, .localReady].contains(phase) else { return false }
        // KRI-211: retrying cannot conjure files that are on another device.
        if originalsMissing && phase == .needsAttention { return false }
        // A job cancelled elsewhere is gone; retrying it would only answer 409.
        if phase == .cancelled, reasonCode == DeviceRenderSessions.jobCancelledReason { return false }
        // `renderer_outdated` is structural the same way `unsupported_recipe` is: retrying
        // recompiles the identical, still-too-new recipe and fails identically. A kept
        // video (`localReady`) with such a reason can't sync either: "Retry sync" would only
        // collect the same refusal, so the card offers the change or the update instead.
        guard yourTurnPhases.contains(phase), let reasonCode else { return true }
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
    /// KRI-211: this attention state is because the original clips are not on this iPhone.
    var originalsMissing = false
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
            if [.preparing, .rendering, .syncing].contains(presentation.phase), !waitsOnPhone {
                ProgressView().accessibilityLabel(title)
            }
            if let note = presentation.waiting.flatMap(DeviceRenderAttentionCopy.waitingNote) {
                Text(note).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("device-render-waiting")
            } else if originalsMissing && presentation.phase == .needsAttention {
                Text(NativeEditorSession.originalsUnavailableMessage).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("device-render-originals-missing")
            } else if let message = needsAttentionAwareMessage {
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
            if DeviceRenderAttentionCopy.needsUpdate(presentation) {
                Link("Update Kria", destination: KriaUpdateLink.destination)
                    .buttonStyle(KriaSecondaryButtonStyle())
                    .accessibilityIdentifier("device-render-update-app")
            }
            // Heat, storage and checking resume by themselves; quiet network waits keep the buttons.
            if presentation.waiting?.isQuiet != false,
               DeviceRenderAttentionCopy.showsRetryButton(phase: presentation.phase, reasonCode: presentation.reasonCode, originalsMissing: originalsMissing) {
                Button(DeviceRenderAttentionCopy.isPaused(presentation) ? "Resume" : presentation.localFile == nil ? "Try again" : "Retry sync") {
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
            if [.preparing, .rendering].contains(presentation.phase), presentation.waiting?.isQuiet != false {
                Button("Stop rendering", action: stop)
                    .font(KriaFont.body(14))
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("device-render-stop")
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
            .accessibilityIdentifier("device-render-play")
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
            .accessibilityIdentifier("device-render-share")
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

    /// Waiting on the phone itself (heat, storage) or on an unknown server
    /// state: nothing is rendering, so no spinner or Stop button.
    private var waitsOnPhone: Bool {
        guard let waiting = presentation.waiting else { return false }
        return !waiting.isQuiet && waiting != .checking
    }

    private var needsAttentionAwareMessage: String? {
        DeviceRenderAttentionCopy.message(phase: presentation.phase, reasonCode: presentation.reasonCode, fallback: presentation.message)
    }

    private var title: String {
        if presentation.outputVerificationPending { return "Checking your video" }
        if let attempt = presentation.repairAttempt { return "Repairing your video (\(attempt) of 2)" }
        if let title = presentation.waiting.flatMap(DeviceRenderAttentionCopy.waitingTitle) { return title }
        return switch presentation.phase {
        case .preparing: "Preparing on your iPhone"
        case .rendering: "Rendering on your iPhone"
        case .localReady: "Ready on this iPhone"
        case .syncing: "Syncing your video"
        case .synced: "Your video is synced"
        case .cancelled: DeviceRenderAttentionCopy.isPaused(presentation) ? "Rendering paused" : "Rendering stopped"
        case .needsAttention where presentation.reasonCode == "renderer_outdated": "Update Kria to render this edit"
        case .needsAttention where DeviceRenderAttentionCopy.needsChange(presentation): "This edit needs a change"
        case .needsAttention where presentation.reasonCode == "original_missing": "This edit needs its original clips"
        case .needsAttention where presentation.reasonCode == DeviceRenderAttentionCopy.renderableReason: "Ready to render on this iPhone"
        case .needsAttention: originalsMissing ? "This edit needs its original clips" : "This edit needs your attention"
        case .superseded: "Your edit changed"
        }
    }
    private var detail: String {
        if presentation.outputVerificationPending { return "Checking the exported picture, text and sound against your approved edit." }
        if let detail = presentation.waiting.flatMap(DeviceRenderAttentionCopy.waitingDetail) { return detail }
        if presentation.phase == .localReady, DeviceRenderAttentionCopy.needsChange(presentation) {
            return "You can watch and share it on this iPhone. To sync it, review the edit in chat and Kria will make a version this iPhone can render."
        }
        if presentation.phase == .localReady, DeviceRenderAttentionCopy.needsUpdate(presentation) {
            return "You can watch and share it on this iPhone. Update Kria to sync it."
        }
        if presentation.phase == .needsAttention, let reasonCode = presentation.reasonCode,
           DeviceRenderAttentionCopy.structuralReasons.contains(reasonCode) { return "Your project is saved." }
        return switch presentation.phase {
        case .preparing: "Getting the original footage and selected assets ready."
        case .rendering: "Keep Kria open while the video finishes."
        case .localReady: "You can watch and share it now. It will appear on your other devices after syncing."
        case .syncing: "Your video is ready to watch and share while it uploads."
        case .synced: "Available in your Gallery and on your other devices."
        case .cancelled: DeviceRenderAttentionCopy.isPaused(presentation)
            ? "Your project and original footage are saved. Resume when you’re ready."
            : "Your project and original footage are saved."
        case .needsAttention: originalsMissing ? "Your project is saved." : "Your project is saved. You can wait and try again."
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
