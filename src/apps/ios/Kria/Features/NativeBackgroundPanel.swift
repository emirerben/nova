import KriaMediaEngine
import SwiftUI

/// KRI-1118: the Background tool of a Talk-to-camera edit. One photo or video from the item's Visuals pool sits
/// behind the speaker for the whole video; "None" clears it. The pool grid is the editor's shared
/// `NativeVisualPoolPicker`; this panel decides what a tap writes: the `speaker_background` lane, never a visual block.
struct NativeBackgroundPanel: View {
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String { case background = "Background" }

    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let onDone: () -> Void
    @State private var tab = Tab.background

    var body: some View {
        NativeEditorLanePanel(title: "Background", tabs: [Tab](), tab: $tab, onDone: { session.endTransaction(); onDone() }) {
            NativeVisualPoolPicker(
                session: session, uploads: uploads, projectID: projectID, idPrefix: "native-editor-background",
                choices: session.speakerBackgroundChoices, selectedID: session.speakerBackground?.mediaID, noneTitle: "None",
                onNone: { _ = session.removeSpeakerBackground() },
                onSelect: { asset in
                    _ = session.setSpeakerBackground(mediaID: asset.id, kind: VisualMediaKind(rawValue: asset.kind) ?? .image)
                }
            )
        }
        .onAppear { panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() } }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
    }
}
