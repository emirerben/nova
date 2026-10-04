import SwiftUI

struct NativeEditorProjectHeader: View {
    let title: String
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var exporter: NativeEditorExporter
    let onBack: () -> Void
    let onChat: () -> Void
    let onSaveToPhotos: () -> Void
    let onShare: () -> Void
    /// Runs before Save, so an open caption line commits (and an emptied one is removed) first.
    var beforeSave: () -> Void = {}
    /// KRI-306: opens the video-shape sheet. The button only exists when the server
    /// advertises the shape capabilities; otherwise the slot stays an invisible balance.
    var onVideoShape: () -> Void = {}

    /// Same floating header as the chat (`WorkspaceTopRow` + `WorkspaceModeSwitch`),
    /// in the same place, so switching Chat <-> Editor feels like a tab switch.
    var body: some View {
        VStack(spacing: 0) {
            WorkspaceTopRow(title: title) {
                HStack(spacing: 8) {
                    Button(action: onBack) {
                        Image(systemName: "chevron.left")
                            .frame(width: 44, height: 44)
                            .kriaFloatingSurface(Circle())
                    }
                    .accessibilityLabel("Back to chat")
                    .accessibilityIdentifier("native-editor-back")
                    // Balances the two trailing actions so the title stays centered.
                    if session.hasVideoShapeCapability {
                        Button(action: onVideoShape) {
                            Image(systemName: "aspectratio")
                                .frame(width: 44, height: 44)
                                .kriaFloatingSurface(Circle())
                        }
                        .accessibilityLabel("Video shape")
                        .accessibilityHint("Choose vertical or landscape, and black bars or crop")
                        .accessibilityIdentifier("native-editor-video-shape-button")
                    } else {
                        Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
                    }
                }
            } trailing: {
                HStack(spacing: 8) {
                    exportMenu
                    saveButton
                }
            }
            WorkspaceModeSwitch(selected: .editor, onChat: onChat, chatIdentifier: "native-editor-chat-tab")
        }
        .buttonStyle(.plain)
        .foregroundStyle(KriaColor.ink)
    }

    /// Stays tappable while blocked so the menu can explain why export waits,
    /// instead of a disabled icon that reads as a broken download button.
    private var exportMenu: some View {
        Menu {
            if let reason = session.exportBlockReason {
                Button(reason, systemImage: "info.circle") {}
                    .disabled(true)
            } else {
                Button("Save to Photos", systemImage: "square.and.arrow.down", action: onSaveToPhotos)
                Button("Share", systemImage: "square.and.arrow.up", action: onShare)
            }
        } label: {
            Group {
                if exporter.phase == .preparing {
                    ProgressView().tint(KriaColor.ink)
                } else {
                    Image(systemName: "square.and.arrow.up")
                        .foregroundStyle(session.exportBlockReason == nil ? KriaColor.ink : KriaColor.zinc)
                }
            }
            .frame(width: 44, height: 44)
            .kriaFloatingSurface(Circle())
        }
        .disabled(exporter.phase == .preparing || session.document.editorState == "empty")
        .accessibilityLabel(exporter.phase == .preparing ? "Preparing video" : "Export video")
        .accessibilityIdentifier("native-editor-export")
    }

    private var saveButton: some View {
        let control = NativeEditorSaveControl(isSaving: session.isSaving, hasUnsavedChanges: session.hasUnsavedChanges)
        return Button { beforeSave(); Task { await session.save() } } label: {
            Group {
                switch control {
                case .saved:
                    Image(systemName: "checkmark").foregroundStyle(KriaColor.zinc)
                case .unsaved:
                    Text("Save")
                        .font(KriaFont.body(14).weight(.semibold))
                        .lineLimit(1)
                        .fixedSize()
                case .saving:
                    ProgressView().tint(KriaColor.ink)
                }
            }
            .frame(minWidth: 44, minHeight: 44)
            .kriaFloatingSurface(Capsule())
        }
        .disabled(!control.isEnabled)
        .accessibilityLabel(control.accessibilityLabel)
        .accessibilityIdentifier("native-editor-save")
    }
}
