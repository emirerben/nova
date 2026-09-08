import SwiftUI
import Photos
import AVKit

@MainActor final class EditorSession: ObservableObject {
    @Published var draft: EditorDraft
    @Published var isSaving = false
    @Published var error: String?
    private var history: [EditorDraft] = []
    private var refreshServerDraft: (() async throws -> EditorDraft)?
    var operations: EditorOperations
    init(draft: EditorDraft, operations: EditorOperations) { self.draft = draft; self.operations = operations }
    func load(api: KriaAPIClient, threadID: UUID) async {
        isSaving = true; defer { isSaving = false }
        do {
            draft = try await api.draft(threadID: threadID).editorDraft(projectID: threadID)
            // The diagnostic stays local until a native draft can mint the
            // same exact approval consumed by the cloud editor renderer.
            operations = LocalEditorOperations()
            refreshServerDraft = { try await api.draft(threadID: threadID).editorDraft(projectID: threadID) }
        } catch { self.error = error.localizedDescription }
    }
    func apply(_ operation: EditorOperation) {
        guard !isSaving else { return }
        history.append(draft)
        isSaving = true
        Task { [weak self] in
            guard let self else { return }
            do {
                draft = try await operations.apply(operation, to: draft)
            } catch {
                if let refreshServerDraft, let authoritative = try? await refreshServerDraft() {
                    draft = authoritative
                }
                self.error = error.localizedDescription
            }
            isSaving = false
        }
    }
    func undo() {
        guard !isSaving, let previous = history.popLast() else { return }
        draft = previous
    }
}

struct PocketEditorView: View {
    let project: ProjectSummary
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var session: EditorSession
    @State private var selectedTool: EditorTool = .timeline
    init(project: ProjectSummary) {
        self.project = project
        let empty = EditorDraft(projectID: project.id, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        _session = StateObject(wrappedValue: EditorSession(draft: empty, operations: LocalEditorOperations()))
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack { Button("Done") { dismiss() }.font(KriaFont.body(16).weight(.semibold)); Spacer(); Text("Editor").font(KriaFont.display(20)); Spacer(); Button(action: session.undo) { Image(systemName: "arrow.uturn.backward").frame(width: 44, height: 44) }.accessibilityLabel("Undo last edit").disabled(session.isSaving) }.padding(.horizontal, 16).frame(height: 56)
            VideoCanvasView()
            TimelineView(draft: session.draft)
            EditorToolBar(selected: $selectedTool, session: session)
            Text("Preview-only editor diagnostics. Changes are not sent to the renderer.")
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
                .padding(.horizontal, 16)
            Button("Done") { dismiss() }
                .buttonStyle(KriaPrimaryButtonStyle())
                .frame(maxWidth: .infinity)
                .padding(16)
                .disabled(session.isSaving)
        }.background(KriaColor.paper).navigationBarHidden(true).task { await session.load(api: model.api, threadID: project.id) }
    }
}

private struct VideoCanvasView: View {
    var body: some View { RoundedRectangle(cornerRadius: 22).fill(KriaColor.ink).aspectRatio(9/16, contentMode: .fit).frame(maxHeight: 380).overlay(VStack(spacing: 10) { Image(systemName: "play.fill").font(.title); Text("Preview").font(KriaFont.body(13)) }.foregroundStyle(KriaColor.lime)) .padding(.horizontal, 28).padding(.vertical, 12) }
}

struct TimelineView: View {
    let draft: EditorDraft
    var body: some View { VStack(alignment: .leading, spacing: 8) { HStack { Text("Timeline").font(KriaFont.body(14).weight(.semibold)); Spacer(); Text("\(draft.clips.count) clips · \(draft.revision) changes").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }; ScrollView(.horizontal, showsIndicators: false) { HStack(spacing: 5) { ForEach(Array(draft.clips.enumerated()), id: \.element.id) { index, clip in RoundedRectangle(cornerRadius: 8).fill(index.isMultiple(of: 2) ? KriaColor.ink.opacity(0.75) : KriaColor.zinc.opacity(0.55)).frame(width: max(76, CGFloat(clip.end - clip.start) * 16), height: 54).overlay(Text("Clip \(index + 1)").font(KriaFont.body(11)).foregroundStyle(.white)) } } } }.padding(.horizontal, 16).padding(.vertical, 10)
    }
}

enum EditorTool: String, CaseIterable, Identifiable { case timeline = "Clips", reorder = "Reorder", trim = "Trim", text = "Text", captions = "Captions", music = "Music"; var id: Self { self }; var icon: String { switch self { case .timeline: "square.stack.3d.up"; case .reorder: "arrow.up.arrow.down"; case .trim: "scissors"; case .text: "textformat"; case .captions: "captions.bubble"; case .music: "music.note" } } }

struct EditorToolBar: View {
    @Binding var selected: EditorTool
    @ObservedObject var session: EditorSession
    @State private var activeSheet: EditorTool?
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 18) {
                ForEach(EditorTool.allCases) { tool in
                    Button { selected = tool; perform(tool) } label: {
                        VStack(spacing: 6) {
                            Image(systemName: tool.icon).font(.headline)
                            Text(tool.rawValue).font(KriaFont.body(11))
                        }
                        .foregroundStyle(selected == tool ? KriaColor.ink : KriaColor.zinc)
                        .frame(minWidth: 54, minHeight: 52)
                    }
                    .accessibilityLabel(tool.rawValue)
                }
            }
            .padding(.horizontal, 14)
        }
        .frame(height: 74)
        .background(Color.white.opacity(0.7))
        .disabled(session.isSaving)
        .sheet(item: $activeSheet) { tool in
            switch tool {
            case .reorder: ReorderClipsView(session: session)
            case .trim: TrimClipsView(session: session)
            default: EmptyView()
            }
        }
    }
    private func perform(_ tool: EditorTool) {
        switch tool {
        case .reorder, .trim: activeSheet = tool
        case .text: session.apply(.addText("Your story"))
        case .captions: session.apply(.setCaptions(!session.draft.captions.enabled))
        case .music: session.apply(.setMusic(UUID(), "Kria original"))
        case .timeline: break
        }
    }
}

private struct ReorderClipsView: View {
    @ObservedObject var session: EditorSession
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List(Array(session.draft.clips.enumerated()), id: \.element.id) { index, clip in
                HStack {
                    Text("Clip \(index + 1)")
                    Spacer()
                    Button { session.apply(.reorder(from: index, to: index - 1)) } label: {
                        Image(systemName: "arrow.up").frame(width: 44, height: 44)
                    }
                    .disabled(index == 0 || session.isSaving)
                    .accessibilityLabel("Move clip \(index + 1) earlier")
                    Button { session.apply(.reorder(from: index, to: index + 1)) } label: {
                        Image(systemName: "arrow.down").frame(width: 44, height: 44)
                    }
                    .disabled(index == session.draft.clips.count - 1 || session.isSaving)
                    .accessibilityLabel("Move clip \(index + 1) later")
                }
                .accessibilityIdentifier("reorder-\(clip.id.uuidString)")
            }
            .navigationTitle("Reorder clips")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}

private struct TrimClipsView: View {
    @ObservedObject var session: EditorSession
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List(Array(session.draft.clips.enumerated()), id: \.element.id) { index, clip in
                VStack(alignment: .leading, spacing: 10) {
                    Text("Clip \(index + 1)").font(KriaFont.body(16).weight(.semibold))
                    Text("\(clip.trimIn, specifier: "%.1f")s – \(clip.trimOut, specifier: "%.1f")s")
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(KriaColor.zinc)
                    HStack {
                        Button("Trim start +0.1s") {
                            let start = min(clip.trimIn + 0.1, clip.trimOut - 0.1)
                            session.apply(.trim(clipID: clip.id, start: start, end: clip.trimOut))
                        }
                        .disabled(clip.trimOut - clip.trimIn <= 0.2 || session.isSaving)
                        Button("Trim end −0.1s") {
                            let end = max(clip.trimOut - 0.1, clip.trimIn + 0.1)
                            session.apply(.trim(clipID: clip.id, start: clip.trimIn, end: end))
                        }
                        .disabled(clip.trimOut - clip.trimIn <= 0.2 || session.isSaving)
                    }
                    .buttonStyle(.bordered)
                }
                .padding(.vertical, 4)
            }
            .navigationTitle("Trim clips")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}

struct ResultsView: View {
    let project: ProjectSummary
    let libraryJobID: UUID?
    @EnvironmentObject private var model: AppModel
    @State private var showShare = false
    @State private var playbackURL: URL?
    @State private var shareFileURL: URL?
    @State private var player: AVPlayer?
    @State private var message: String?
    @State private var isRefreshingPlayback = false
    @State private var isPreparingShare = false
    @State private var showsEditor = false

    init(project: ProjectSummary, libraryJobID: UUID? = nil) {
        self.project = project
        self.libraryJobID = libraryJobID
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Your video is ready.").font(KriaFont.display(34))
            Group {
                if let player { VideoPlayer(player: player).onDisappear { player.pause() } }
                else { RoundedRectangle(cornerRadius: 22).fill(KriaColor.ink).overlay(ProgressView().tint(KriaColor.lime)) }
            }.aspectRatio(9/16, contentMode: .fit).frame(maxHeight: 420).clipShape(RoundedRectangle(cornerRadius: 22))
            HStack {
                Button("Edit video") { showsEditor = true }
                    .buttonStyle(KriaPrimaryButtonStyle())
                Button("Save to Photos") { Task { await saveToPhotos() } }.buttonStyle(KriaPrimaryButtonStyle()).disabled(playbackURL == nil)
                Button(isPreparingShare ? "Preparing…" : "Share") { Task { await prepareShare() } }
                    .buttonStyle(KriaSecondaryButtonStyle())
                    .disabled(playbackURL == nil || isPreparingShare)
            }
            Button { Task { await refreshPlayback() } } label: { Label(isRefreshingPlayback ? "Refreshing link…" : "Refresh playback link", systemImage: "arrow.clockwise") }
                .font(KriaFont.body(14).weight(.semibold)).disabled(isRefreshingPlayback)
            Text(message ?? "Ready in your project receipt.").font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
        }
        .padding(24)
        .task { await refreshPlayback() }
        .sheet(isPresented: $showShare, onDismiss: removeShareFile) {
            if let shareFileURL { ShareSheetView(url: shareFileURL) }
        }
        .fullScreenCover(isPresented: $showsEditor) {
            NativeEditorView(
                project: project,
                libraryJobID: libraryJobID,
                onBack: { showsEditor = false }
            )
                .environmentObject(model)
        }
    }
    private func refreshPlayback() async {
        isRefreshingPlayback = true
        defer { isRefreshingPlayback = false }
        do {
            let jobID = project.activeJobID ?? project.id
            let url = try await model.api.playbackURL(jobID: jobID)
            player?.pause()
            playbackURL = url
            player = AVPlayer(url: url)
        } catch { message = error.localizedDescription }
    }
    private func saveToPhotos() async {
        guard let playbackURL else { return }
        do {
            let localFile = try await downloadVideo(from: playbackURL)
            defer { try? FileManager.default.removeItem(at: localFile) }
            try await PhotoLibrarySaver().saveVideo(at: localFile)
            message = "Saved to Photos."
        } catch { message = error.localizedDescription }
    }

    private func prepareShare() async {
        guard let playbackURL else { return }
        isPreparingShare = true
        defer { isPreparingShare = false }
        do {
            removeShareFile()
            shareFileURL = try await downloadVideo(from: playbackURL)
            showShare = true
        } catch { message = error.localizedDescription }
    }

    private func downloadVideo(from remoteURL: URL) async throws -> URL {
        let (temporary, response) = try await URLSession.shared.download(from: remoteURL)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError.requestFailed
        }
        let destination = FileManager.default.temporaryDirectory
            .appending(path: "kria-\(UUID().uuidString).mp4")
        try FileManager.default.moveItem(at: temporary, to: destination)
        return destination
    }

    private func removeShareFile() {
        guard let shareFileURL else { return }
        try? FileManager.default.removeItem(at: shareFileURL)
        self.shareFileURL = nil
    }
}

struct ShareSheetView: View {
    let url: URL
    var body: some View { VStack(spacing: 18) { Text("Share your cut").font(KriaFont.display(28)); ShareLink(item: url) { Label("Share video", systemImage: "square.and.arrow.up").frame(maxWidth: .infinity, minHeight: 48) }.buttonStyle(KriaPrimaryButtonStyle()); Text("A local copy is ready for the native share sheet.").font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }.padding(24).presentationDetents([.height(230)]) }
}
