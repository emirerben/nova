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
    @StateObject private var session = SlidePostSession()
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
    @State private var photoSelections: [String: ProjectPhotoSelection] = [:]
    @State private var previewPlayer: AVPlayer?
    @State private var previewRetry = 0
    @State private var resolvedThread: CreationThread?
    @State private var resolvedCapabilities: CreationCapabilities?
    @State private var showsAttachments = false

    init(
        project: ProjectSummary,
        thread: CreationThread? = nil,
        capabilities: CreationCapabilities? = nil,
        capabilitiesLoaded: Bool = false,
        conversation: (() -> AnyView)? = nil,
        conversationAcceptedID: UUID? = nil,
        onBack: (() -> Void)? = nil,
        onAddMedia: (() -> Void)? = nil
    ) {
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
            || pendingUploads.contains { $0.projectID == uploadProjectID }
            || BackgroundUploadCoordinator.reservedCount(projectID: uploadProjectID, role: .visual, inFlight: uploadInFlight, records: pendingUploads, selections: photoSelections) > 0
    }
    private var hasFailedUploads: Bool { uploadFailures.contains { $0.projectID == uploadProjectID } || session.state?.assets.contains { $0.status == "failed" || ["missing", "expired"].contains($0.mediaStatus ?? "") } == true }
    private var canRequestProposal: Bool { !session.isBusy && !session.hasConflict && !hasPendingAssets && !hasFailedUploads && !session.readyAssets.isEmpty }

    private var canCreateRender: Bool {
        guard !session.hasUnsavedChanges, !isRendering else { return false }
        return session.draft != nil && session.state?.canExport != true
    }
    private var pollingKey: String { "\(itemID ?? "")-\(session.state?.renderStatus ?? "")-\(hasPendingAssets)-\(session.isBusy)" }

    var body: some View {
        VStack(spacing: 0) {
            header
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
        .background(KriaColor.paper)
        .sheet(isPresented: $showsConversation) {
            SlidePostAssistantSheet(
                session: session, api: model.api, itemID: itemID,
                canRequestProposal: canRequestProposal,
                uploadGuidance: hasFailedUploads ? "Resolve or remove failed photos and videos before asking Kria." : hasPendingAssets ? "Wait for every photo and video to finish importing before asking Kria." : nil
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
                .presentationDetents([.medium, .large])
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
        .onReceive(model.uploads.$photoSelections) { photoSelections = $0 }
        .onReceive(model.uploads.$failures) { uploadFailures = $0 }
        .onChange(of: conversationAcceptedID) { _, _ in showsConversation = false }
        .onChange(of: session.selectedID) { _, _ in loadInspectorValues() }
        .onChange(of: session.selectedID) { _, _ in previewPlayer?.pause() }
        .onChange(of: instruction) { _, value in session.instruction = value }
        .task(id: itemID) { await refreshOwner(); await refresh() }
        .task(id: pollingKey) {
            guard (isRendering || hasPendingAssets) && itemID != nil else { return }
            while !Task.isCancelled && (session.isRendering || hasPendingAssets) {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                await refresh()
            }
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
                Button { showsConversation = true } label: {
                    Image(systemName: "sparkles").font(.system(size: 23, weight: .semibold)).foregroundStyle(.white).frame(width: 52, height: 52).background(KriaColor.ink, in: Circle())
                }
                .accessibilityLabel("Open Kria conversation")
                .accessibilityIdentifier("slidepost-openkria")
                .padding(14)
            }
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
        let asset = session.selectedAsset
        return Rectangle().fill(KriaColor.softZinc)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Slide preview")
            .accessibilityIdentifier("slidepost-preview")
            .overlay {
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
        .aspectRatio(draft.platformProfile == "instagram_carousel" ? CGFloat(4) / 5 : CGFloat(9) / 16, contentMode: .fit)
        .clipped()
        .contentShape(Rectangle())
        .overlay(alignment: textAlignment) { if !session.canExport, let text = session.selectedSlide?.edits?.text, !text.content.isEmpty { Text(text.content).font(KriaFont.display(26)).multilineTextAlignment(.center).padding(12).foregroundStyle(.white).shadow(radius: 3).padding(16) } }
        .overlay(alignment: .topLeading) { Text("Preview").font(KriaFont.body(11).weight(.semibold)).padding(8).background(.black.opacity(0.45), in: Capsule()).foregroundStyle(.white).padding(10) }
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
    }
    private func proposalPreview(_ draft: SlidePostDraft) -> some View { thumbrail(draft).padding(12).background(KriaColor.sage.opacity(0.35), in: RoundedRectangle(cornerRadius: 14)) }
    private func status(_ message: String) -> some View { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(KriaColor.sage.opacity(0.45), in: RoundedRectangle(cornerRadius: 12)) }
    private func recovery(_ message: String) -> some View { VStack(alignment: .leading, spacing: 8) { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText); Button("Reload saved post") { Task { await refresh() } }.buttonStyle(KriaSecondaryButtonStyle()); if session.hasUnsavedChanges || session.hasConflict { Button("Discard local changes") { Task { await discardLocalChanges() } }.buttonStyle(KriaSecondaryButtonStyle()) } }.padding(12).background(KriaColor.failureSoft, in: RoundedRectangle(cornerRadius: 12)) }

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

private struct SlidePostAssetThumbnail: View {
    let asset: SlidePostAsset?
    var body: some View {
        Group {
            if let url = asset?.previewURL ?? asset?.displayURL {
                if url.isFileURL, let image = UIImage(contentsOfFile: url.path) { Image(uiImage: image).resizable().scaledToFill() }
                else { AsyncImage(url: url) { $0.resizable().scaledToFill() } placeholder: { ProgressView() } }
            }
            else { Image(systemName: asset?.kind == "video" ? "video" : "photo").foregroundStyle(KriaColor.zinc) }
        }
        .frame(width: 64, height: 76).background(KriaColor.softZinc).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

/// Contextual slide-post AI is deliberately separate from the generic creator
/// chat: it invokes only the slide proposal API, then requires Apply or Undo.
private struct SlidePostAssistantSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var session: SlidePostSession
    let api: any KriaAPIClient
    let itemID: String?
    let canRequestProposal: Bool
    let uploadGuidance: String?
    @State private var prompt = ""

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("Kria").font(KriaFont.display(24))
                TextField("Describe the post", text: $prompt, axis: .vertical)
                    .accessibilityLabel("Describe the post")
                    .accessibilityIdentifier("slidepost-prompt")
                    .lineLimit(2...5).padding(12).overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border))
                Button(session.isBusy ? "Thinking…" : "Propose changes") { Task { await propose() } }
                    .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                    .disabled(!canRequestProposal)
                    .accessibilityIdentifier("slidepost-ask")
                if let uploadGuidance {
                    Text(uploadGuidance).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                }
                if let proposal = session.proposal {
                    Text(proposal.summary).font(KriaFont.body(14))
                    Text(proposal.draft.caption)
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    Button("Apply proposal") { Task { await apply() } }
                        .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
                        .accessibilityIdentifier("slidepost-apply")
                }
                if session.canUndo {
                    Button("Undo applied change") { Task { await undo() } }
                        .buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
                }
                if let error = session.error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
                Spacer()
            }
            .padding(20).background(KriaColor.paper)
            .onAppear { prompt = session.instruction }
            .onChange(of: prompt) { _, value in session.instruction = value }
        }
    }

    private func propose() async {
        guard canRequestProposal, let itemID else { return }
        let brief = prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Arrange these photos and videos into a cohesive post." : prompt
        session.instruction = brief
        await session.propose(api: api, itemID: itemID, instruction: brief)
    }
    private func apply() async { guard let itemID else { return }; await session.applyProposal(api: api, itemID: itemID); if session.error == nil && session.proposal == nil { dismiss() } }
    private func undo() async { guard let itemID else { return }; await session.undo(api: api, itemID: itemID) }
}
