import AVKit
import SwiftUI
import UIKit

/// Native workspace for a connected photo-and-video post. It owns proposal,
/// review and explicit creation; the creation chat only provisions its item
/// and supplies the existing attachment sheet through `onAddMedia`.
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
    @StateObject private var exporter = SlidePostExporter()
    @State private var showsConversation = false
    @State private var showsInspector = false
    @State private var instruction = ""
    @State private var selectedPosition = "center"
    @State private var selectedLook = "none"
    @State private var selectedProfile = "instagram_carousel"
    @State private var pendingUploads: [UploadRecoveryRecord] = []
    @State private var uploadFailures: [UploadFailure] = []
    @State private var uploadInFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [:]
    /// True while the software keyboard is up; text mode shrinks the stage so the Edit field stays visible,
    /// and the chat layout drops the strip and shrinks the preview so the thread and composer stay above it.
    @State private var keyboardUp = false
    @State private var photoSelections: [String: ProjectPhotoSelection] = [:]
    @State private var previewPlayer: AVPlayer?
    @State private var previewRetry = 0
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
    private var canRequestProposal: Bool { !session.isBusy && !session.hasConflict && !hasPendingAssets && !hasFailedAssets && !session.readyAssets.isEmpty }

    private var canCreateRender: Bool {
        guard !session.hasUnsavedChanges, !isRendering else { return false }
        return session.draft != nil && session.state?.canExport != true
    }
    private var pollingKey: String { "\(itemID ?? "")-\(session.state?.renderStatus ?? "")-\(hasPendingAssets)-\(session.isBusy)" }

    var body: some View {
        VStack(spacing: 0) {
            if showsRichWorkspace, let draft = session.draft {
                richHeader(draft)
                richWorkspace(draft)
            } else {
                header
                legacyScroll
            }
        }
        .onGeometryChange(for: CGSize.self, of: { $0.size }) { rootSize = $0 }
        .background(KriaColor.paper)
        .sheet(isPresented: $showsCaption) { captionSheet }
        .sheet(isPresented: $showsConversation) {
            SlidePostAISheet(
                session: session, api: model.api, itemID: itemID,
                chatEnabled: chatEditOn && showsRichWorkspace,
                canRequestProposal: canRequestProposal,
                uploadGuidance: hasFailedAssets ? "Resolve or remove failed photos and videos before asking Kria." : hasPendingAssets ? "Wait for every photo and video to finish importing before asking Kria." : nil
            )
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showsInspector) { inspector }
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
        .onChange(of: session.selectedID) { _, _ in loadInspectorValues() }
        .onChange(of: session.selectedID) { _, _ in previewPlayer?.pause() }
        .onChange(of: instruction) { _, value in session.instruction = value }
        // Media that finishes importing joins the post by itself; it waits out saves and AI edits.
        .onChange(of: autoAppendKey) { _, _ in session.appendNewlyReadyAssets() }
        .onAppear { session.appendNewlyReadyAssets() }
        .task(id: itemID) { await refreshOwner(); await refresh() }
        .task(id: pollingKey) {
            guard (isRendering || hasPendingAssets) && itemID != nil else { return }
            while !Task.isCancelled && (session.isRendering || hasPendingAssets) {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                await refresh()
            }
        }
    }

    private var legacyScroll: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let error = session.error { recovery(error) }
                if let message = session.operationMessage { status(message) }
                if hasPendingAssets || hasFailedUploads {
                    Button(hasFailedUploads ? "Review files that need retrying" : "Preparing your media… View files", action: addMedia)
                        .font(KriaFont.body(14)).frame(minHeight: 44)
                }
                ForEach(session.state?.validationErrors ?? [], id: \.message) { issue in
                    Text(issue.message).font(KriaFont.body(14)).foregroundStyle(KriaColor.failureText)
                }
                if let proposal = session.proposal {
                    proposalView(proposal)
                } else if let draft = session.draft {
                    editor(draft)
                } else {
                    startingPoint
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
    }

    private var header: some View {
        HStack {
            Button { if let onBack { onBack() } else { dismiss() } } label: { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                .accessibilityLabel("Back to creation")
            Spacer()
            Text("Photo & video post").font(KriaFont.display(20))
            Spacer()
            Button { Task { await save() } } label: { Text(session.isBusy ? "Saving…" : "Save") }
                .frame(width: 44, height: 44)
                .disabled(!session.hasUnsavedChanges || session.isBusy)
        }
        .padding(.horizontal, 16).frame(height: 56).background(Color.white)
    }

    private var startingPoint: some View {
        VStack(alignment: .leading, spacing: 16) {
            SlidePostHeading(title: "Start your post", detail: "Add photos and videos, then tell Kria how they should connect.")
            Button(action: addMedia) {
                Label("Add photos & videos", systemImage: "plus")
                    .frame(maxWidth: .infinity, minHeight: 54)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
                .disabled((onAddMedia == nil && ownerThread == nil) || session.isBusy)
            if session.readyAssets.isEmpty {
                Text(itemID == nil ? "Setting up your post…" : "Your ready photos and videos will appear here.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            } else {
                assetReceipts(session.readyAssets)
                Picker("Post format", selection: $selectedProfile) {
                    Text("Instagram carousel · mixed media · 4:5").tag("instagram_carousel")
                    Text("TikTok photo mode · photos only · 9:16").tag("tiktok_photo")
                }
                .pickerStyle(.menu)
                promptComposer
            }
        }
    }

    private var promptComposer: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Direction").font(KriaFont.body(14).weight(.semibold))
            TextField("Describe the post (optional)", text: $instruction, axis: .vertical)
                .lineLimit(2...5).padding(12).overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border))
            Button(session.isBusy ? "Thinking…" : "Ask Kria") { Task { await propose() } }
                .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                .disabled(!canRequestProposal || itemID == nil)
        }
    }

    private func proposalView(_ proposal: SlidePostProposal) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            SlidePostHeading(title: "Kria’s direction", detail: proposal.summary)
            proposalPreview(proposal.draft)
            if !proposal.draft.caption.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Proposed caption").font(KriaFont.body(13).weight(.semibold))
                    Text(proposal.draft.caption).font(KriaFont.body(14)).textSelection(.enabled)
                    Button("Copy caption") { UIPasteboard.general.string = proposal.draft.caption }.buttonStyle(KriaSecondaryButtonStyle())
                }
            }
            Picker("Post format", selection: $selectedProfile) {
                Text("Instagram carousel · mixed media · 4:5").tag("instagram_carousel")
                Text("TikTok photo mode · photos only · 9:16").tag("tiktok_photo")
            }
            .onChange(of: selectedProfile) { _, value in
                guard var proposal = session.proposal else { return }
                proposal.draft.platformProfile = value
                session.proposal = proposal
            }
            Text("Kria can propose order, cover, and caption. Text and look stay under your direct control after you create the post.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            Button(session.isBusy ? "Applying…" : "Apply proposal") { Task { await applyProposal() } }
                .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
            Button("Keep editing direction") { session.proposal = nil }
                .buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
        }
    }

    private func editor(_ draft: SlidePostDraft) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            ZStack(alignment: .bottomTrailing) {
                preview(draft)
                    .frame(maxWidth: .infinity, maxHeight: legacyPreviewCap)
                Button { showsConversation = true } label: {
                    Image(systemName: "sparkles").font(.system(size: 23, weight: .semibold)).foregroundStyle(.white).frame(width: 52, height: 52).background(KriaColor.ink, in: Circle())
                }
                .accessibilityLabel("Open Kria conversation")
                .accessibilityIdentifier("slidepost-openkria")
                .padding(14)
            }
            .background(SlidePostTone.stage)
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            thumbrail(draft)
            editorTools(draft)
            Button("Add photos & videos", action: addMedia)
                .buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
            if canCreateRender {
                Button(session.isBusy ? "Creating…" : "Create post") { Task { await create() } }
                .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                .disabled(session.isBusy)
                .accessibilityIdentifier("slidepost-create")
            }
            if isRendering {
                status("Preparing the current saved version…")
            } else if session.canExport {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Your ordered slides and caption are ready.").font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    HStack {
                        Button(exporter.isWorking ? "Preparing…" : "Save to Photos") { Task { await saveToPhotos() } }
                            .buttonStyle(KriaPrimaryButtonStyle()).disabled(exporter.isWorking)
                            .accessibilityIdentifier("slidepost-save-photos")
                        Button("Share files") { Task { await prepareShare() } }
                            .buttonStyle(KriaSecondaryButtonStyle()).disabled(exporter.isWorking)
                            .accessibilityIdentifier("slidepost-share-files")
                    }
                    if let message = exporter.message { Text(message).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }
                }
            }
        }
    }

    private func preview(_ draft: SlidePostDraft) -> some View {
        Rectangle().fill(KriaColor.softZinc)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Slide preview")
            .accessibilityIdentifier("slidepost-preview")
            .overlay { previewMediaView }
        .aspectRatio(draft.platformProfile == "instagram_carousel" ? CGFloat(4) / 5 : CGFloat(9) / 16, contentMode: .fit)
        .clipped()
        .contentShape(Rectangle())
        .overlay(alignment: textAlignment) { if !session.canExport, let text = session.selectedSlide?.edits?.text, !text.content.isEmpty { Text(text.content).font(KriaFont.display(26)).multilineTextAlignment(.center).padding(12).foregroundStyle(.white).shadow(radius: 3).padding(16) } }
        .overlay(alignment: .topLeading) { Text("Preview").font(KriaFont.body(11).weight(.semibold)).padding(8).background(.black.opacity(0.45), in: Capsule()).foregroundStyle(.white).padding(10) }
    }

    /// The selected slide's media (still, video or rendered output) with its player lifecycle.
    private var previewMediaView: some View {
        let asset = session.selectedAsset
        return Group {
            if let url = previewURL {
                if asset?.kind == "video" { VideoPlayer(player: previewPlayer) }
                else if url.isFileURL, let image = UIImage(contentsOfFile: url.path) { Image(uiImage: image).resizable().scaledToFill().accessibilityHidden(true) }
                else {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image): image.resizable().scaledToFill().accessibilityHidden(true)
                        case .failure:
                            VStack(spacing: 8) {
                                Image(systemName: "exclamationmark.triangle").foregroundStyle(KriaColor.zinc)
                                Text("Preview unavailable").font(KriaFont.body(12))
                                Button("Retry preview") { Task { await refresh(); previewRetry += 1 } }
                                    .buttonStyle(KriaSecondaryButtonStyle())
                            }
                        default: ProgressView()
                        }
                    }
                    .id(previewRetry)
                }
            } else { Image(systemName: "photo.on.rectangle").font(.largeTitle).foregroundStyle(KriaColor.zinc) }
        }
        .task(id: previewURL) {
            guard session.selectedAsset?.kind == "video", let previewURL else { previewPlayer?.pause(); previewPlayer = nil; return }
            previewPlayer?.pause(); previewPlayer = AVPlayer(url: previewURL)
        }
        .onDisappear { previewPlayer?.pause() }
    }

    private var previewURL: URL? {
        if session.canExport, let id = session.selectedSlide?.id,
           let rendered = session.state?.slides.first(where: { $0.id == id })?.url { return rendered }
        let asset = session.selectedAsset
        return asset?.sourceURL ?? asset?.displayURL ?? asset?.previewURL
    }
    private var textAlignment: Alignment {
        switch session.selectedSlide?.edits?.text?.position {
        case "top": .top
        case "bottom": .bottom
        default: .center
        }
    }

    private func thumbrail(_ draft: SlidePostDraft) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 9) {
                ForEach(Array(draft.slides.enumerated()), id: \.element.id) { index, slide in
                    Button { session.selectedID = slide.id } label: {
                        VStack(spacing: 4) {
                            SlidePostAssetThumbnail(asset: session.state?.assets.first { $0.id == slide.assetID })
                            Text("\(index + 1)").font(KriaFont.body(11).weight(.semibold))
                        }
                        .padding(4).background(slide.id == session.selectedID ? KriaColor.sky.opacity(0.55) : Color.clear, in: RoundedRectangle(cornerRadius: 9))
                    }
                    .buttonStyle(.plain).accessibilityLabel("Slide \(index + 1)")
                }
            }
        }
        .excludesDrawerGesture()
    }

    private func editorTools(_ draft: SlidePostDraft) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button("Move earlier") { moveSelected(-1) }.buttonStyle(KriaSecondaryButtonStyle()).disabled(session.selectedSlide == nil || session.isBusy)
                Button("Move later") { moveSelected(1) }.buttonStyle(KriaSecondaryButtonStyle()).disabled(session.selectedSlide == nil || session.isBusy)
            }
            HStack {
                Button("Set cover") { if let id = session.selectedSlide?.id { session.setCover(id: id) } }.buttonStyle(KriaSecondaryButtonStyle()).disabled(session.selectedSlide == nil || session.isBusy)
                Button("Text & look") { loadInspectorValues(); showsInspector = true }.buttonStyle(KriaSecondaryButtonStyle()).disabled(session.selectedSlide == nil || session.isBusy)
                Button("Remove", role: .destructive) { if let id = session.selectedSlide?.id { session.removeSlide(id: id) } }.disabled(session.selectedSlide == nil || session.isBusy)
            }
            if session.canUndo {
                Button("Undo applied change") { Task { await undo() } }
                    .buttonStyle(KriaSecondaryButtonStyle()).disabled(session.isBusy)
            }
            TextField("Caption", text: Binding(get: { draft.caption }, set: { updateCaption($0) }), axis: .vertical)
                .lineLimit(2...5).padding(12).overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border))
            Button("Copy caption") { UIPasteboard.general.string = draft.caption }
                .buttonStyle(KriaSecondaryButtonStyle()).disabled(draft.caption.isEmpty)
            let unused = session.readyAssets.filter { asset in !draft.slides.contains(where: { $0.assetID == asset.id }) }
            if !unused.isEmpty {
                Text("Add ready media").font(KriaFont.body(13).weight(.semibold))
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(unused) { asset in
                            Button { session.addAsset(id: asset.id) } label: { SlidePostAssetThumbnail(asset: asset).overlay(alignment: .bottomTrailing) { Image(systemName: "plus.circle.fill").foregroundStyle(KriaColor.ink, KriaColor.butter) } }
                                .buttonStyle(.plain).accessibilityLabel("Add \(asset.sourceFilename ?? "media")")
                        }
                    }
                }
                .excludesDrawerGesture()
            }
        }
    }

    private var inspector: some View {
        NavigationStack {
            Form {
                Section("Text") {
                    TextField("Slide text", text: Binding(get: { session.selectedSlide?.edits?.text?.content ?? "" }, set: { updateText($0) }), axis: .vertical)
                    Picker("Position", selection: $selectedPosition) { Text("Top").tag("top"); Text("Center").tag("center"); Text("Bottom").tag("bottom") }
                        .onChange(of: selectedPosition) { _, _ in updateText(session.selectedSlide?.edits?.text?.content ?? "") }
                }
                Section("Look") {
                    Picker("Preset", selection: $selectedLook) {
                        Text("Original").tag("none"); Text("Stadium Diffusion").tag("stadium_diffusion"); Text("Olive Film").tag("olive_film"); Text("Smoky Split-Tone").tag("smoky_split_tone"); Text("Golden Hour").tag("golden_hour"); Text("Faded Analog").tag("faded_analog")
                    }
                    .onChange(of: selectedLook) { _, value in updateLook(value) }
                    Text("Save to apply this look.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                }
            }
            .navigationTitle("Text & look")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showsInspector = false } } }
        }
        .presentationDetents([.medium, .large])
    }

    private func assetReceipts(_ assets: [SlidePostAsset]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) { HStack(spacing: 8) { ForEach(assets) { asset in SlidePostAssetThumbnail(asset: asset).frame(width: 76, height: 88) } } }
            .excludesDrawerGesture()
    }
    private func proposalPreview(_ draft: SlidePostDraft) -> some View { thumbrail(draft).padding(12).background(KriaColor.sage.opacity(0.35), in: RoundedRectangle(cornerRadius: 14)) }
    private func status(_ message: String) -> some View { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(KriaColor.sage.opacity(0.45), in: RoundedRectangle(cornerRadius: 12)) }
    private func recovery(_ message: String) -> some View { VStack(alignment: .leading, spacing: 8) { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText); Button("Reload saved post") { Task { await refresh() } }.buttonStyle(KriaSecondaryButtonStyle()); if session.hasUnsavedChanges || session.hasConflict { Button("Discard local changes") { Task { await discardLocalChanges() } }.buttonStyle(KriaSecondaryButtonStyle()) } }.padding(12).background(KriaColor.failureSoft, in: RoundedRectangle(cornerRadius: 12)) }

    // MARK: Redesigned workspace (capability `slide_post_rich_text`)

    private var richEnabled: Bool { effectiveCapabilities?.slidePostRichTextEnabled == true }
    private var chatEditOn: Bool { effectiveCapabilities?.slidePostChatComposerEnabled == true }
    private var showsRichWorkspace: Bool { richEnabled && session.proposal == nil && session.draft != nil }
    private var autoAppendKey: String {
        "\(session.readyAssets.map(\.id).joined(separator: ","))|\(session.isBusy)|\(session.isChatting)|\(session.draft != nil)|\(session.proposal != nil)"
    }
    private func pendingTiles(_ draft: SlidePostDraft) -> [SlidePostPendingTile] {
        SlidePostPendingMedia.tiles(
            assets: session.state?.assets ?? [], draftAssetIDs: Set(draft.slides.map(\.assetID)), records: pendingUploads,
            progress: uploadProgress, inFlight: uploadInFlight, failures: uploadFailures, projectID: uploadProjectID
        )
    }
    private func tapPending(_ tile: SlidePostPendingTile) {
        guard case .failed(_, let recordID) = tile.phase else { return }
        // A failed upload resumes from its record; a file that never uploaded has to be chosen again.
        if let recordID { Task { await model.uploads.retryUpload(recordID: recordID); await refresh() } } else { addMedia() }
    }
    /// The preview never grows past ~53.5% of the screen, so the strip and tool bar always stay visible.
    private var legacyPreviewCap: CGFloat { max(260, 0.535 * (rootSize.height > 0 ? rootSize.height : 800)) }
    /// Dates/places for Kria to sort and label by. Lane B ships the capture metadata; until a slide
    /// asset carries it this stays off, so the notice never appears on a guess.
    private var showsNoMetadataNotice: Bool { false }

    private func aspect(_ draft: SlidePostDraft) -> CGFloat { draft.platformProfile == "instagram_carousel" ? 4.0 / 5 : 9.0 / 16 }
    private func platformName(_ draft: SlidePostDraft) -> String { draft.platformProfile == "instagram_carousel" ? "Instagram" : "TikTok" }

    private var headerAction: SlidePostHeader.Action {
        if session.isBusy { .saving }
        else if session.hasUnsavedChanges { .save }
        else if isRendering { .rendering }
        else if session.canExport { .share }
        else if canCreateRender { .create }
        else { .save }
    }

    private func richHeader(_ draft: SlidePostDraft) -> some View {
        let action = headerAction
        return SlidePostHeader(
            action: action, actionEnabled: action == .create || (action == .save && session.hasUnsavedChanges),
            onBack: { if let onBack { onBack() } else { dismiss() } },
            onAction: { Task { if action == .create { await create() } else { await save() } } },
            onSaveToPhotos: { Task { await saveToPhotos() } },
            onShare: { Task { await prepareShare() } }
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

    @ViewBuilder private var richBanner: some View {
        if let error = session.error {
            HStack(spacing: 10) {
                Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).frame(maxWidth: .infinity, alignment: .leading)
                Button("Dismiss") { session.error = nil }.font(KriaFont.body(13).weight(.semibold)).frame(minHeight: 44)
            }
            .padding(.horizontal, 16).padding(.vertical, 4).background(KriaColor.failureSoft)
            .accessibilityIdentifier("slidepost-error")
        } else if let issue = session.state?.validationErrors.first {
            Text(issue.message).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                .padding(.horizontal, 16).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading).background(KriaColor.failureSoft)
        } else if let notice = session.autoAppendNotice {
            HStack(spacing: 10) {
                Text(notice).font(KriaFont.body(13)).foregroundStyle(KriaColor.ink).frame(maxWidth: .infinity, alignment: .leading)
                Button("Dismiss") { session.autoAppendNotice = nil }.font(KriaFont.body(13).weight(.semibold)).frame(minHeight: 44)
            }
            .padding(.horizontal, 16).padding(.vertical, 4).background(KriaColor.sage.opacity(0.45))
            .accessibilityIdentifier("slidepost-notice")
        } else if hasFailedUploads {
            Button(action: addMedia) {
                Text("Review files that need retrying")
                    .font(KriaFont.body(13).weight(.semibold)).frame(maxWidth: .infinity, minHeight: 44)
            }
            .background(KriaColor.sage.opacity(0.45))
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
        Button { showsConversation = true } label: {
            Image(systemName: "sparkles")
                .font(.system(size: 23))
                .foregroundStyle(.white)
                .frame(width: 52, height: 52)
                .background(KriaColor.ink, in: Circle())
        }
        .accessibilityLabel("Open Kria conversation")
        .accessibilityIdentifier("slidepost-openkria")
        .padding(.trailing, 16).padding(.bottom, 14)
    }

    private func richPreview(_ draft: SlidePostDraft, size: CGSize) -> some View {
        let index = (draft.slides.firstIndex { $0.id == session.selectedSlide?.id } ?? 0) + 1
        let texts = session.canExport ? [] : (session.selectedSlide?.edits?.effectiveTexts ?? [])
        return ZStack(alignment: .topLeading) {
            Rectangle().fill(KriaColor.softZinc)
                .frame(width: size.width, height: size.height)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Slide preview")
                .accessibilityIdentifier("slidepost-preview")
            previewMediaView.frame(width: size.width, height: size.height).clipped()
            SlidePostTextCanvas(
                texts: texts, size: size, selectedID: session.selectedTextID, interactive: mode == .text, tapsToEdit: mode != .text,
                onSelect: { id in session.selectedTextID = id; if id != nil { textTab = .style } },
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
        .excludesDrawerGestureWhen(mode == .text)
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
            hit: hit, panelOpen: mode == .text, selectedID: session.selectedTextID, onEditTab: textTab == .edit, keyboardUp: keyboardUp
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
        if instruction.isEmpty {
            if !session.instruction.isEmpty { instruction = session.instruction }
            else if !model.chatDrafts.draft(for: project.id).isEmpty {
                instruction = model.chatDrafts.draft(for: project.id)
                session.instruction = instruction
            }
        }
        selectedProfile = session.proposal?.draft.platformProfile ?? session.draft?.platformProfile ?? selectedProfile
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
    private func saveToPhotos() async {
        guard let itemID else { return }
        await exporter.saveToPhotos(session: session) {
            try await session.revalidateForExport(api: model.api, itemID: itemID)
        }
    }
    private func prepareShare() async {
        guard let itemID else { return }
        await exporter.prepareShare(session: session) {
            try await session.revalidateForExport(api: model.api, itemID: itemID)
        }
    }
    private func propose() async {
        guard let itemID, canRequestProposal else { return }
        let selected = SlidePostDraft(version: 1, platformProfile: selectedProfile, slides: session.readyAssets.map { .init(id: $0.id, assetID: $0.id, kind: $0.kind) })
        if let message = selected.validationMessage { session.error = message; return }
        // The session preserves the instruction by item; this local field
        // mirrors it while this workspace remains on screen.
        session.instruction = instruction
        let brief = instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Arrange these photos and videos into a cohesive post." : instruction
        instruction = brief
        await session.propose(api: model.api, itemID: itemID, instruction: brief, platformProfile: selectedProfile)

    }
    private func applyProposal() async { guard let itemID else { return }; await session.applyProposal(api: model.api, itemID: itemID) }
    private func create() async { guard let itemID else { return }; await session.create(api: model.api, itemID: itemID) }
    private func undo() async { guard let itemID else { return }; await session.undo(api: model.api, itemID: itemID) }
    private func save() async { guard let itemID else { return }; await session.save(api: model.api, itemID: itemID) }
    private func discardLocalChanges() async { guard let itemID else { return }; session.discardLocalChanges(); await session.refresh(api: model.api, itemID: itemID) }
    private func moveSelected(_ offset: Int) { if let id = session.selectedSlide?.id { session.moveSlide(id: id, offset: offset) } }
    private func updateCaption(_ value: String) { guard var draft = session.draft else { return }; draft.caption = String(value.prefix(2200)); session.draft = draft }
    private func loadInspectorValues() { selectedPosition = session.selectedSlide?.edits?.text?.position ?? "center"; selectedLook = session.selectedSlide?.edits?.lookPreset ?? "none" }
    private func updateText(_ value: String) { guard var slide = session.selectedSlide else { return }; let content = String(value.prefix(120)); slide.edits = SlidePostEdits(text: content.isEmpty ? nil : SlidePostText(content: content, position: selectedPosition), lookPreset: slide.edits?.lookPreset ?? "none"); session.updateSlide(slide) }
    private func updateLook(_ value: String) { guard var slide = session.selectedSlide else { return }; slide.edits = SlidePostEdits(text: slide.edits?.text, lookPreset: value); session.updateSlide(slide) }
}

private struct SlidePostHeading: View {
    let title: String
    let detail: String
    var body: some View { VStack(alignment: .leading, spacing: 6) { Text(title).font(KriaFont.display(29)); Text(detail).font(KriaFont.body(15)).foregroundStyle(KriaColor.zinc) } }
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
        if let url = asset?.previewURL ?? asset?.displayURL {
            if url.isFileURL, let image = UIImage(contentsOfFile: url.path) { Image(uiImage: image).resizable().scaledToFill().frame(width: size.width, height: size.height).clipped() }
            else { AsyncImage(url: url) { $0.resizable().scaledToFill().frame(width: size.width, height: size.height).clipped() } placeholder: { ProgressView() } }
        }
        else { Image(systemName: asset?.kind == "video" ? "video" : "photo").foregroundStyle(KriaColor.zinc) }
    }
}
