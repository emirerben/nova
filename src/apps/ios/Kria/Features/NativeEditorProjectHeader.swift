import SwiftUI

struct NativeEditorProjectHeader: View {
    let title: String
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var exporter: NativeEditorExporter
    let onBack: () -> Void
    let onChat: () -> Void
    let onSaveToPhotos: () -> Void
    let onShare: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                Button(action: onBack) {
                    Image(systemName: "chevron.left").frame(width: 44, height: 44)
                }
                .accessibilityLabel("Back to chat")
                .accessibilityIdentifier("native-editor-back")
                // Balances the two trailing actions so the title stays centered.
                Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
                Text(title)
                    .font(KriaFont.body(13).weight(.semibold))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity)
                exportMenu
                saveButton
            }
            .padding(.horizontal, 4)
            HStack(spacing: 4) {
                Button("Chat", action: onChat)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .accessibilityIdentifier("native-editor-chat-tab")
                Text("Editor")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityAddTraits(.isSelected)
            }
            .font(KriaFont.body(13))
            .padding(.horizontal, 16)
            .padding(.bottom, 6)
        }
        .buttonStyle(.plain)
        .foregroundStyle(KriaColor.ink)
        .background(KriaColor.paper)
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
        }
        .disabled(exporter.phase == .preparing)
        .accessibilityLabel(exporter.phase == .preparing ? "Preparing video" : "Export video")
        .accessibilityIdentifier("native-editor-export")
    }

    private var saveButton: some View {
        let control = NativeEditorSaveControl(isSaving: session.isSaving, hasUnsavedChanges: session.hasUnsavedChanges)
        return Button { Task { await session.save() } } label: {
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
        }
        .disabled(!control.isEnabled)
        .accessibilityLabel(control.accessibilityLabel)
        .accessibilityIdentifier("native-editor-save")
    }
}
