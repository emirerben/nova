import KriaMediaEngine
import SwiftUI

/// KRI-1118: the Background tool of a Talk-to-camera edit. One photo or video from the item's Visuals pool sits
/// behind the speaker for the whole video; "None" clears it. Built on the Visuals panel's pool and importer, but
/// it writes the `speaker_background` lane, never a visual block.
struct NativeBackgroundPanel: View {
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String { case background = "Background" }

    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let onDone: () -> Void
    @State private var tab = Tab.background
    @State private var showsImporter = false

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 10, alignment: .top), count: 3)
    private var current: EditorSpeakerBackground? { session.speakerBackground }
    private var preparingCount: Int {
        session.visualLibrary.filter { $0.status != "ready" && $0.status != "failed" }.count
    }

    var body: some View {
        NativeEditorLanePanel(title: "Background", tabs: [Tab](), tab: $tab, onDone: { session.endTransaction(); onDone() }) {
            VStack(spacing: 12) {
                LazyVGrid(columns: columns, spacing: 10) {
                    noneTile
                    ForEach(session.speakerBackgroundChoices) { asset in visualTile(asset) }
                }
                if preparingCount > 0 {
                    Text("Getting your upload ready…").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("native-editor-background-preparing")
                }
                addButton
                if let message = importUnavailableMessage {
                    Text(message).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("native-editor-background-unavailable")
                }
                if let error = session.visualError {
                    Text(error).foregroundStyle(KriaColor.failureText).font(KriaFont.body(12))
                        .accessibilityIdentifier("native-editor-background-error")
                }
            }
        }
        .sheet(isPresented: $showsImporter, onDismiss: { Task { await session.refreshVisualLibrary() } }) { importer }
        .onChange(of: uploads.records) { _, _ in Task { await session.refreshVisualLibrary() } }
        .onChange(of: uploads.photoSelections) { _, _ in Task { await session.refreshVisualLibrary() } }
        .task {
            await session.pollVisualLibrary()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                if session.visualLibraryNeedsPolling { await session.pollVisualLibrary() }
            }
        }
        .onAppear { panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() } }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
    }

    private var importUnavailableMessage: String? {
        if session.visualItemID == nil { return "Open a saved edit to add photos or videos." }
        return nil
    }

    private var addButton: some View {
        Button { showsImporter = true } label: {
            Label("Add photo or video", systemImage: "plus")
                .font(KriaFont.body(14).weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(KriaColor.ink.opacity(0.06), in: Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(session.visualItemID == nil)
        .accessibilityIdentifier("native-editor-background-add")
    }

    private var noneTile: some View {
        let selected = current == nil
        return Button { session.removeSpeakerBackground() } label: {
            tile(selected: selected, label: "None") {
                Image(systemName: "slash.circle").font(.system(size: 22, weight: .regular)).foregroundStyle(KriaColor.ink)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("None")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("native-editor-background-none")
    }

    private func visualTile(_ asset: CreationVisual) -> some View {
        let selected = current?.mediaID == asset.id
        let kind = VisualMediaKind(rawValue: asset.kind) ?? .image
        let name = asset.sourceFilename ?? (kind == .video ? "Video" : "Photo")
        return Button { session.setSpeakerBackground(mediaID: asset.id, kind: kind) } label: {
            tile(selected: selected, label: name) {
                NativeVisualThumbnail(asset: asset)
                    .overlay(alignment: .bottomLeading) {
                        if kind == .video {
                            Image(systemName: "play.fill").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                                .padding(5).background(KriaColor.ink.opacity(0.55), in: Circle()).padding(4)
                        }
                    }
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(name)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("native-editor-background-visual-" + asset.id)
    }

    /// A fill tile: the selected one is sage with a check, never an outline.
    private func tile<Content: View>(selected: Bool, label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(spacing: 6) {
            content()
                .frame(maxWidth: .infinity).frame(height: 76)
                .background(KriaColor.softZinc)
                .clipShape(RoundedRectangle(cornerRadius: 9))
                .overlay(alignment: .topTrailing) {
                    if selected {
                        Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
                            .frame(width: 20, height: 20).background(KriaColor.ink, in: Circle()).padding(5)
                    }
                }
            Text(label).font(KriaFont.body(11).weight(selected ? .semibold : .regular)).lineLimit(1).frame(height: 14)
        }
        .padding(6)
        .background(selected ? KriaColor.sage : Color.clear, in: RoundedRectangle(cornerRadius: 12))
        .contentShape(RoundedRectangle(cornerRadius: 12))
    }

    private var importer: some View {
        NavigationStack {
            ScrollView {
                FootagePickerView(
                    projectID: projectID,
                    uploads: uploads,
                    maximumClipCount: session.visualLibraryLimit,
                    attachedClipCount: session.visualLibrary.count,
                    attachedMediaIDs: Set(session.visualLibrary.map(\.id)),
                    role: .visual,
                    itemID: session.visualItemID
                )
                .padding(16)
            }
            .navigationTitle("Add photo or video")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { showsImporter = false }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}
