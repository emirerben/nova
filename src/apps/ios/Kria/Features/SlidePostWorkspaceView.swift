import AVKit
import SwiftUI
import UIKit

/// Native workspace for a connected photo-and-video post. There is exactly ONE layout (the rich
/// editor: header, preview with the AI button, strip ending in "+ Add", tool dock). It never depends
/// on a fetched capability, so it can't silently fall back to another screen. The creation chat only
/// provisions the item and supplies the attachment sheet through `onAddMedia`.
struct SlidePostWorkspaceView: View {
    let project: ProjectSummary
    let thread: CreationThread?
    let capabilities: CreationCapabilities?
    let capabilitiesLoaded: Bool
    private let conversation: (() -> AnyView)?
    private let conversationAcceptedID: UUID?
    let onBack: (() -> Void)?
    let onAddMedia: (() -> Void)?

    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var session: SlidePostSession
    @StateObject private var exporter = SlidePostExporter.makeDefault()
    @State private var showsConversation = false
    /// Chat-edit staging as decided when the AI sheet opened; it never flips while the sheet is up.
    @State private var aiChatEnabled = false
    @State private var pendingUploads: [UploadRecoveryRecord] = []
    @State private var uploadFailures: [UploadFailure] = []
    @State private var uploadInFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [:]
    /// True while the software keyboard is up; text mode shrinks the stage so the Edit field stays visible,
    /// and the chat layout drops the strip and shrinks the preview so the thread and composer stay above it.
    @State private var keyboardUp = false
    @State private var photoSelections: [String: ProjectPhotoSelection] = [:]
    @State private var previewPlayer: AVPlayer?
    @State private var previewPlayerAssetID: String?
    @State private var resolvedThread: CreationThread?
    @State private var resolvedCapabilities: CreationCapabilities?
    @State private var showsAttachments = false
    @State private var mode: SlidePostMode = .browse
    @State private var textTab: SlidePostTextPanel.Tab = .edit
    @State private var showsCaption = false
    /// Bumped to put the keyboard back on the Edit field when the same text is tapped again.
    @State private var focusToken = 0
    @State private var uploadProgress: [UUID: Double] = [:]
    @State private var rootSize = CGSize.zero

    /// `session` is injectable so a parent (the chat workspace) can own the draft and stage edits
    /// into it; by default the workspace creates its own.
    init(
        project: ProjectSummary,
        thread: CreationThread? = nil,
        capabilities: CreationCapabilities? = nil,
        capabilitiesLoaded: Bool = false,
        conversation: (() -> AnyView)? = nil,
        conversationAcceptedID: UUID? = nil,
        onBack: (() -> Void)? = nil,
        onAddMedia: (() -> Void)? = nil,
        session: SlidePostSession? = nil
    ) {
        _session = StateObject(wrappedValue: session ?? SlidePostSession())
        self.project = project
        self.thread = thread
        self.capabilities = capabilities
        self.capabilitiesLoaded = capabilitiesLoaded
        self.conversation = conversation
        self.conversationAcceptedID = conversationAcceptedID
        self.onBack = onBack
        self.onAddMedia = onAddMedia
    }

    private var ownerThread: CreationThread? { thread ?? resolvedThread }
    private var effectiveCapabilities: CreationCapabilities? { capabilities ?? resolvedCapabilities }
    private var itemID: String? { ownerThread?.activePlanItemID ?? project.activePlanItemID ?? session.state?.itemID }
    private var isRendering: Bool { session.isRendering }
    private var uploadProjectID: UUID { ownerThread.flatMap { UUID(uuidString: $0.id) } ?? project.id }
    private var hasPendingAssets: Bool {
        session.state?.assets.contains { ["pending", "queued", "uploaded", "processing", "analyzing", "uploading"].contains($0.status) } == true
            // A record whose upload failed stays for Retry but is not in progress (KRI-211).
            || BackgroundUploadCoordinator.inProgressRecords(pendingUploads).contains { $0.projectID == uploadProjectID }
            || BackgroundUploadCoordinator.reservedCount(projectID: uploadProjectID, role: .visual, inFlight: uploadInFlight, records: pendingUploads, selections: photoSelections) > 0
    }
    private var hasSkippedFiles: Bool { uploadFailures.contains { $0.projectID == uploadProjectID } }
    /// The server still lists an asset it could not keep. A file that never got that far — one the
    /// picker couldn't read (`hasSkippedFiles`) — is simply left out and never blocks asking Kria (KRI-211).
    private var hasFailedAssets: Bool { session.state?.assets.contains { $0.status == "failed" || ["missing", "expired"].contains($0.mediaStatus ?? "") } == true }
    private var hasFailedUploads: Bool { hasSkippedFiles || hasFailedAssets }
    private var canRequestProposal: Bool { !session.isBusy && !hasPendingAssets && !hasFailedAssets && !session.readyAssets.isEmpty }

    private var pollingKey: String { "\(itemID ?? "")-\(session.state?.renderStatus ?? "")-\(hasPendingAssets)-\(session.isBusy)" }

    var body: some View {
        VStack(spacing: 0) {
            richHeader
            if let draft = session.draft {
                richWorkspace(draft)
            } else {
                emptyWorkspace
            }
        }
        .onGeometryChange(for: CGSize.self, of: { $0.size }) { rootSize = $0 }
        .background(KriaColor.paper)
        // The workspace draws its own back circle; the system bar would add a second back button.
        .navigationBarBackButtonHidden(true)
        .toolbar(.hidden, for: .navigationBar)
        .sheet(isPresented: $showsCaption) { captionSheet }
        .sheet(isPresented: $showsConversation) {
            SlidePostAISheet(
                session: session, api: model.api, itemID: itemID,
                chatEnabled: aiChatEnabled,
                canRequestProposal: canRequestProposal,
                uploadGuidance: hasFailedAssets ? "Resolve or remove failed photos and videos before asking Kria." : hasPendingAssets ? "Wait for every photo and video to finish importing before asking Kria." : nil
            )
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showsAttachments, onDismiss: { Task { await refresh() } }) {
            AttachmentSheet(projectID: uploadProjectID, maximumClipCount: 35, attachedClipCount: 0, format: .slides,
                            thread: ownerThread, capabilities: effectiveCapabilities,
                            capabilitiesLoaded: effectiveCapabilities != nil,
                            refresh: { await refreshOwner() })
                .environmentObject(model)
                .presentationDetents([.large])
        }
        .sheet(isPresented: $exporter.isSharing, onDismiss: exporter.discardShareDirectory) {
            SlidePostShareSheet(items: exporter.shareItems)
        }
        .onReceive(model.uploads.$records) { records in
            let previous = Set(pendingUploads.filter { $0.projectID == uploadProjectID }.map(\.id))
            pendingUploads = records
            let current = Set(records.filter { $0.projectID == uploadProjectID }.map(\.id))
            if previous != current { Task { await refresh() } }
        }
        .onReceive(model.uploads.$inFlight) { uploadInFlight = $0 }
        .onReceive(model.uploads.$progress) { uploadProgress = $0 }
        .onReceive(model.uploads.$photoSelections) { photoSelections = $0 }
        .onReceive(model.uploads.$failures) { uploadFailures = $0 }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardUp = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardUp = false }
        .onChange(of: conversationAcceptedID) { _, _ in showsConversation = false }
        .onChange(of: session.selectedID) { _, _ in previewPlayer?.pause() }
        // Media that finishes importing joins the post by itself; it waits out saves and AI edits.
        .onChange(of: autoAppendKey) { _, _ in seedAndAppend() }
        .onAppear { seedAndAppend() }
        .task(id: itemID) { await refreshOwner(); await refresh() }
        // Capabilities only decide optional AI behaviour, never the layout; ask early and keep trying.
        .task(id: capabilities == nil) { await loadCapabilitiesWithRetry() }
        .task(id: pollingKey) {
            guard (isRendering || hasPendingAssets) && itemID != nil else { return }
            while !Task.isCancelled && (session.isRendering || hasPendingAssets) {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                await refresh()
            }
        }
    }

    /// The selected slide's media (still, video or rendered output) with its player lifecycle.
    private func previewMediaView(rich: Bool) -> some View {
        let asset = session.selectedAsset
        let previewURL = previewURL(rich: rich)
        return ZStack {
            if let asset, let url = previewURL {
                if asset.kind == "video" {
                    // The poster is the cached thumbnail, so the frame is never blank while the player spins up.
                    SlidePostCachedImage(assetID: asset.id, variant: .preview, url: asset.previewMediaURL, onExpired: { await refresh() }, showsSpinner: false)
                    if previewPlayer != nil, previewPlayerAssetID == asset.id { VideoPlayer(player: previewPlayer) }
                } else {
                    SlidePostCachedImage(assetID: asset.id, variant: .preview, url: url, onExpired: { await refresh() },
                                         loadingIdentifier: "slidepost-preview-loading", showsRetry: true)
                        .accessibilityHidden(true)
                        .overlay { Color.clear.accessibilityElement().accessibilityIdentifier("slidepost-preview-image").accessibilityValue(asset.id) }
                }
            } else { Image(systemName: "photo.on.rectangle").font(.largeTitle).foregroundStyle(KriaColor.zinc) }
        }
        // Keyed by the stable asset, not the signed URL: a poll that re-signs the same video never rebuilds
        // the player, and selecting a photo leaves the last video's player warm (paused) for the return.
        .task(id: asset.map { $0.kind == "video" ? $0.id : "" } ?? "") {
            guard let asset, asset.kind == "video", let previewURL else { previewPlayer?.pause(); return }
            if previewPlayerAssetID == asset.id, previewPlayer != nil { return }
            previewPlayer?.pause()
            previewPlayer = AVPlayer(url: previewURL); previewPlayerAssetID = asset.id
        }
        .task(id: prefetchKey) { prefetchNeighbours() }
        .onDisappear { previewPlayer?.pause() }
    }

    /// Selected slide +/- 2 at preview size and every thumbnail, so selecting a neighbour is instant. Runs when
    /// the strip first appears and after any reorder/add/remove (the key changes with the slide order).
    private var prefetchKey: String {
        "\(session.selectedID ?? "")|\(session.draft?.slides.map(\.assetID).joined(separator: ",") ?? "")|\(session.state?.assets.count ?? 0)"
    }
    private func prefetchNeighbours() {
        guard let draft = session.draft, let assets = session.state?.assets else { return }
        let selected = draft.slides.firstIndex { $0.id == session.selectedID }
        SlidePostImageCache.shared.prefetch(SlidePostPrefetch.requests(slideAssetIDs: draft.slides.map(\.assetID), selectedIndex: selected, assets: assets))
    }

    /// Rich editor: always the SOURCE media (the live canvas draws the editable text over it); the render is
    /// for export only. Legacy layout: the render once the post is ready.
    private func previewURL(rich: Bool) -> URL? {
        let rendered = session.selectedSlide.flatMap { slide in session.state?.slides.first(where: { $0.id == slide.id })?.url }
        return SlidePostPreviewPolicy.mediaURL(rich: rich, canExport: session.canExport, renderedURL: rendered, asset: session.selectedAsset)
    }
    // MARK: Redesigned workspace (capability `slide_post_rich_text`)

    /// Chat-edit staging in the AI sheet is the one genuinely optional piece: unknown or off => propose flow.
    private var chatEditOn: Bool { effectiveCapabilities?.slidePostChatEditEnabled == true }
    private func seedAndAppend() {
        session.seedDraftIfNeeded()
        session.appendNewlyReadyAssets()
    }
    private func loadCapabilitiesWithRetry() async {
        guard capabilities == nil, resolvedCapabilities == nil else { return }
        for delay in [0.0, 1.0, 3.0, 8.0] {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            if Task.isCancelled { return }
            if let loaded = try? await model.api.creationCapabilities() { resolvedCapabilities = loaded; return }
        }
    }
    private var autoAppendKey: String {
        "\(session.readyAssets.map(\.id).joined(separator: ","))|\(session.isBusy)|\(session.isChatting)|\(session.draft != nil)|\(session.proposal != nil)"
    }
    private func pendingTiles(_ draft: SlidePostDraft?) -> [SlidePostPendingTile] {
        SlidePostPendingMedia.tiles(
            assets: session.state?.assets ?? [], draftAssetIDs: Set(draft?.slides.map(\.assetID) ?? []), records: pendingUploads,
            progress: uploadProgress, inFlight: uploadInFlight, failures: uploadFailures, projectID: uploadProjectID
        )
    }
    private func tapPending(_ tile: SlidePostPendingTile) {
        guard case .failed(_, let recordID) = tile.phase else { return }
        // A failed upload resumes from its record; a file that never uploaded has to be chosen again.
        if let recordID { Task { await model.uploads.retryUpload(recordID: recordID); await refresh() } } else { addMedia() }
    }
    /// Dates/places for Kria to sort and label by. Lane B ships the capture metadata; until a slide
    /// asset carries it this stays off, so the notice never appears on a guess.
    private var showsNoMetadataNotice: Bool { false }

    private func aspect(_ draft: SlidePostDraft) -> CGFloat { draft.platformProfile == "instagram_carousel" ? 4.0 / 5 : 9.0 / 16 }
    private func platformName(_ draft: SlidePostDraft) -> String { draft.platformProfile == "instagram_carousel" ? "Instagram" : "TikTok" }

    /// Why export cannot start right now (shown as a disabled row in the export menu); nil = it can.
    private var exportBlockReason: String? {
        if session.isChatting { return "Kria is editing. Export when it finishes." }
        if hasPendingAssets { return "Wait for photos and videos to finish importing." }
        guard let draft = session.draft else { return "Add photos and videos first." }
        return draft.validationMessage
    }

    private var richHeader: some View {
        SlidePostHeader(
            title: project.workspaceTitle,
            saveState: NativeEditorSaveControl(isSaving: session.isBusy, hasUnsavedChanges: session.hasUnsavedChanges),
            exportBlockReason: exportBlockReason,
            isExporting: session.isBusy || exporter.isBusy,
            onBack: { if let onBack { onBack() } else { dismiss() } },
            onSave: { Task { await save() } },
            onSaveToPhotos: { Task { await export(.photos) } },
            onShare: { Task { await export(.share) } }
        )
    }

    /// Status line + undo/redo, in the slot of the editor's transport row.
    private func transportRow(_ draft: SlidePostDraft) -> some View {
        let slideNumber = (draft.slides.firstIndex { $0.id == session.selectedSlide?.id } ?? 0) + 1
        let subtitle: String
        if mode == .text { subtitle = "Slide \(slideNumber) · Text" }
        else if mode == .look { subtitle = "Slide \(slideNumber) · Look" }
        else if session.hasUnsavedChanges { subtitle = "Unsaved changes" }
        else { subtitle = "\(platformName(draft)) · \(draft.platformProfile == "instagram_carousel" ? "4:5" : "9:16") · \(draft.slides.count) slide\(draft.slides.count == 1 ? "" : "s")" }
        return SlidePostTransportRow(
            subtitle: subtitle, unsaved: session.hasUnsavedChanges && mode != .text && mode != .look,
            canUndo: session.canUndoEdit || session.canUndo, canRedo: session.canRedoEdit,
            onUndo: { if session.canUndoEdit { session.undoEdit() } else { Task { await undo() } } },
            onRedo: { session.redoEdit() }
        )
    }

    /// The same screen before a draft exists (item still being set up, nothing imported yet): the
    /// same header, stage, strip with its "+ Add" block, and dock (inert), so the layout never changes.
    private var emptyWorkspace: some View {
        let waiting = session.state == nil && session.error == nil
        return VStack(spacing: 0) {
            richBanner
            ZStack {
                SlidePostTone.stage
                VStack(spacing: 10) {
                    if waiting || hasPendingAssets { ProgressView() }
                    Text(waiting ? "Setting up your post…" : hasPendingAssets ? "Preparing your photos and videos…" : "Add photos and videos to start your post.")
                        .font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.zinc).multilineTextAlignment(.center)
                }
                .padding(.horizontal, 32)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("slidepost-empty")
            }
            .overlay(alignment: .bottomTrailing) { aiButton }
            SlidePostTransportRow(subtitle: "No slides yet", unsaved: false, canUndo: false, canRedo: false, onUndo: {}, onRedo: {})
            VStack(spacing: 8) {
                SlidePostStrip(
                    slides: [], coverIndex: 0, selectedID: nil, asset: { _ in nil }, pending: pendingTiles(nil),
                    onSelect: { _ in }, onMove: { _, _ in }, onTapPending: tapPending, onAdd: addMedia
                )
                SlidePostToolBar(
                    mode: .browse, canCover: false, canRemove: false, canDuplicateText: false,
                    onText: {}, onCover: {}, onLook: {}, onRemove: {}, onDuplicateText: {}, onCaption: {}
                )
                .disabled(true).opacity(0.4)
            }
            .padding(.bottom, 6)
        }
        .background(KriaColor.paper)
    }

    private func richWorkspace(_ draft: SlidePostDraft) -> some View {
        let slide = session.selectedSlide
        let panelOpen = mode == .text || mode == .look
        let height = max(rootSize.height, 640)
        return VStack(spacing: 0) {
            richBanner
            if showsNoMetadataNotice, !panelOpen {
                SlidePostNotice(title: "No dates or places on these photos",
                                detail: "Kria can't sort by time or add locations. You can still reorder by hand, or ask for text you write yourself.")
                    .padding(.bottom, 8)
            }
            stage(draft, compact: panelOpen)
                .frame(maxHeight: panelOpen ? max(keyboardUp && mode == .text ? 144 : 150, 0.33 * height - (keyboardUp && mode == .text ? 96 : 0)) : .infinity)
                .animation(.easeOut(duration: 0.2), value: keyboardUp)
            if !(keyboardUp && mode == .text) { transportRow(draft) }
            if mode == .text, let slide {
                SlidePostTextPanel(session: session, slideID: slide.id, tab: $textTab, compact: keyboardUp, focusToken: focusToken, onDone: finishEditing)
                    .id(slide.id)
                    .frame(maxHeight: .infinity)
                    .padding(.horizontal, 12).padding(.bottom, NativeEditorIslandMetrics.bottomPadding)
            } else if mode == .look, let slide {
                SlidePostLookPanel(session: session, slideID: slide.id, onDone: finishEditing)
                    .frame(maxHeight: .infinity)
                    .padding(.horizontal, 12).padding(.bottom, NativeEditorIslandMetrics.bottomPadding)
            } else {
                browseControls(draft)
            }
        }
        .background(KriaColor.paper)
    }

    private var richBanner: some View {
        VStack(spacing: 0) {
            SlidePostExportBanner(exporter: exporter)
            statusBanner
        }
    }

    @ViewBuilder private var statusBanner: some View {
        if let error = session.error {
            Button { session.error = nil } label: {
                NativeEditorBannerRow(title: "Something went wrong", detail: error, systemImage: "exclamationmark.triangle", tint: .red, identifier: "slidepost-error")
            }
            .buttonStyle(.plain).accessibilityHint("Dismiss")
        } else if let issue = session.state?.validationErrors.first {
            NativeEditorBannerRow(title: "Needs attention", detail: issue.message, systemImage: "exclamationmark.triangle", tint: .red, identifier: "slidepost-validation")
        } else if let notice = session.autoAppendNotice {
            Button { session.autoAppendNotice = nil } label: {
                NativeEditorBannerRow(title: "Heads up", detail: notice, systemImage: "info.circle", tint: KriaColor.ink, identifier: "slidepost-notice")
            }
            .buttonStyle(.plain).accessibilityHint("Dismiss")
        } else if hasFailedUploads {
            Button(action: addMedia) {
                NativeEditorBannerRow(title: "Some files didn’t upload", detail: "Review files that need retrying.", systemImage: "arrow.clockwise", tint: KriaColor.ink, identifier: "slidepost-retry-files")
            }
            .buttonStyle(.plain)
        }
    }

    private func stage(_ draft: SlidePostDraft, compact: Bool, fixedHeight: CGFloat? = nil) -> some View {
        GeometryReader { geometry in
            let height = max(rootSize.height, 640)
            let cap = (compact ? 0.34 : 0.535) * height
            let ratio = aspect(draft)
            let previewHeight = fixedHeight ?? max(120, min(cap, (geometry.size.width - 36) / ratio, geometry.size.height - 24))
            ZStack {
                SlidePostTone.stage
                richPreview(draft, size: CGSize(width: previewHeight * ratio, height: previewHeight))
                    .position(x: geometry.size.width / 2, y: geometry.size.height / 2)
            }
        }
        .clipped()
        .overlay(alignment: .bottomTrailing) { if mode == .browse { aiButton } }
    }

    /// The editor's AI entry (same sparkles button as the video editor's preview), the page's only way into Kria chat.
    private var aiButton: some View {
        KriaAIButton(identifier: "slidepost-openkria") { aiChatEnabled = chatEditOn; showsConversation = true }
    }

    private func richPreview(_ draft: SlidePostDraft, size: CGSize) -> some View {
        let index = (draft.slides.firstIndex { $0.id == session.selectedSlide?.id } ?? 0) + 1
        let texts = SlidePostPreviewPolicy.editableTexts(rich: true, edits: session.selectedSlide?.edits)
        return ZStack(alignment: .topLeading) {
            Rectangle().fill(KriaColor.softZinc)
                .frame(width: size.width, height: size.height)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Slide preview")
                .accessibilityIdentifier("slidepost-preview")
            previewMediaView(rich: true).frame(width: size.width, height: size.height).clipped()
            SlidePostTextCanvas(
                texts: texts, size: size, selectedID: session.selectedTextID, interactive: mode == .text,
                directSelected: mode == .browse && session.selectedTextID != nil, tapsToEdit: mode != .text,
                onSelect: { id in session.selectedTextID = id; if id != nil { textTab = .style } },
                // Hold / plain drag on a text while browsing: select for transform only (no panel, tab or keyboard).
                onDirectSelect: { id in session.selectedTextID = id },
                onTapText: handleCanvasTap,
                onTransform: { id, key, mutate in
                    guard let slideID = session.selectedSlide?.id else { return }
                    session.updateText(slideID: slideID, textID: id, coalescing: key, mutate)
                }
            )
        }
        .frame(width: size.width, height: size.height)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(alignment: .topTrailing) {
            Text("\(index) / \(draft.slides.count)").font(KriaFont.body(13).weight(.bold)).foregroundStyle(.white)
                .padding(.horizontal, 12).padding(.vertical, 6).background(KriaColor.mutedInk.opacity(0.85), in: Capsule()).padding(12)
                .allowsHitTesting(false)
        }
        .overlay(alignment: .topLeading) {
            if isRendering && !session.isBusy {
                Text("Rendering…").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(.white)
                    .padding(.horizontal, 10).padding(.vertical, 5).background(.black.opacity(0.5), in: Capsule()).padding(12)
            }
        }
        .overlay { if session.isBusy { SlidePostVeil(message: session.operationMessage ?? "Saving your post…").clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous)) } }
        .excludesDrawerGestureWhen(mode == .text || (mode == .browse && session.selectedTextID != nil))
    }

    private func slideStrip(_ draft: SlidePostDraft) -> some View {
        SlidePostStrip(
            slides: draft.slides, coverIndex: draft.coverIndex, selectedID: session.selectedID,
            asset: { slide in session.state?.assets.first { $0.id == slide.assetID } },
            pending: pendingTiles(draft),
            onSelect: { session.selectedID = $0; session.selectedTextID = nil },
            onMove: { id, to in session.moveSlide(id: id, toIndex: to) },
            onTapPending: tapPending,
            onAdd: addMedia
        )
        .disabled(session.isChatting)
    }

    private func browseControls(_ draft: SlidePostDraft) -> some View {
        let slide = session.selectedSlide
        let hasTexts = !(slide?.edits?.effectiveTexts.isEmpty ?? true)
        return VStack(spacing: 8) {
            slideStrip(draft)
            SlidePostToolBar(
                mode: mode, canCover: slide != nil, canRemove: draft.slides.count > 1 && slide != nil,
                canDuplicateText: hasTexts && (slide?.edits?.effectiveTexts.count ?? 0) < SlidePostEdits.maxTexts,
                onText: beginTextEditing,
                onCover: { if let id = slide?.id { session.setCover(id: id); UINotificationFeedbackGenerator().notificationOccurred(.success) } },
                onLook: { mode = .look },
                onRemove: { if let id = slide?.id { session.removeSlide(id: id) } },
                onDuplicateText: {
                    if let id = slide?.id, let textID = session.selectedTextID ?? slide?.edits?.effectiveTexts.first?.id { session.duplicateText(slideID: id, textID: textID) }
                },
                onCaption: { showsCaption = true }
            )
        }
        .padding(.bottom, 6)
        .disabled(session.isBusy)
        .opacity(session.isBusy ? 0.5 : 1)
    }

    /// A tap on a text in the preview selects it and opens the Edit text tab with the keyboard up.
    private func handleCanvasTap(_ hit: String?) {
        guard let outcome = SlidePostTextTap.resolve(
            hit: hit, panelOpen: mode == .text, selectedID: session.selectedTextID, onEditTab: textTab == .edit, keyboardUp: keyboardUp,
            directSelected: mode == .browse && session.selectedTextID != nil
        ) else { return }
        session.selectedTextID = outcome.selectID
        guard outcome.selectID != nil else { return }
        if outcome.showsEditTab { textTab = .edit }
        if outcome.opensPanel { mode = .text }
        if outcome.focusesField { focusToken += 1 }
    }

    private func beginTextEditing() {
        guard let slide = session.selectedSlide else { return }
        if slide.edits?.effectiveTexts.isEmpty ?? true {
            session.addText(slideID: slide.id); textTab = .edit
        } else {
            if session.selectedTextID == nil || slide.edits?.effectiveTexts.contains(where: { $0.id == session.selectedTextID }) != true {
                session.selectedTextID = slide.edits?.effectiveTexts.first?.id
            }
            textTab = .style
        }
        mode = .text
    }
    private func finishEditing() {
        if let id = session.selectedSlide?.id { session.removeEmptyTexts(slideID: id) }
        session.selectedTextID = nil
        mode = .browse
    }

    private var captionSheet: some View {
        NavigationStack {
            Form {
                TextField("Caption", text: Binding(get: { session.draft?.caption ?? "" }, set: { session.setCaption($0) }), axis: .vertical)
                    .lineLimit(3...8).accessibilityIdentifier("slidepost-caption-field")
                Button("Copy caption") { UIPasteboard.general.string = session.draft?.caption }
                    .disabled(session.draft?.caption.isEmpty ?? true)
            }
            .navigationTitle("Caption")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showsCaption = false } } }
        }
        .presentationDetents([.medium])
    }

    private func refresh() async {
        guard let itemID else { return }
        await session.refresh(api: model.api, itemID: itemID)
        seedAndAppend()
    }
    private func refreshOwner() async {
        if resolvedThread == nil, thread == nil, let planID = project.activePlanItemID {
            let projects = (try? await model.api.projects()) ?? model.projects
            if let owner = projects.first(where: { $0.activePlanItemID == planID }) {
                resolvedThread = try? await model.api.project(threadID: owner.id)
            }
        }
        if resolvedCapabilities == nil, capabilities == nil { resolvedCapabilities = try? await model.api.creationCapabilities() }
        await refresh()
    }
    private func addMedia() { if let onAddMedia { onAddMedia() } else if ownerThread != nil { showsAttachments = true } }
    private func export(_ destination: SlidePostExporter.Destination) async {
        guard let itemID else { return }
        await exporter.export(destination, session: session, api: model.api, itemID: itemID)
    }
    private func undo() async { guard let itemID else { return }; await session.undo(api: model.api, itemID: itemID) }
    private func save() async { guard let itemID else { return }; await session.save(api: model.api, itemID: itemID) }
}

struct SlidePostAssetThumbnail: View {
    let asset: SlidePostAsset?
    var size = CGSize(width: 64, height: 76)
    var radius: CGFloat = 8
    var body: some View {
        // The photo is an overlay on a tile-sized base and hidden from accessibility, so an overflowing
        // aspect-fill image can never widen the tile's accessibility / hit frame (a 16:9 photo made a
        // 56pt tile read as 128pt wide).
        Color.clear.frame(width: size.width, height: size.height)
            .background(KriaColor.softZinc)
            .overlay { content.compositingGroup().accessibilityHidden(true) }
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
    @ViewBuilder private var content: some View {
        if let asset, let url = asset.thumbnailURL {
            // Same cache the preview uses: the strip tile is what the preview blurs up from.
            SlidePostCachedImage(assetID: asset.id, variant: .thumb, url: url)
                .frame(width: size.width, height: size.height).clipped()
        }
        else { Image(systemName: asset?.kind == "video" ? "video" : "photo").foregroundStyle(KriaColor.zinc) }
    }
}
