import SwiftUI

struct NativeEditorProjectHeader: View {
    let title: String
    @ObservedObject var session: NativeEditorSession
    let onBack: () -> Void
    let onChat: () -> Void
    let isDownloading: Bool
    let onDownload: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                Button(action: onBack) {
                    Image(systemName: "chevron.left").frame(width: 44, height: 44)
                }
                .accessibilityLabel("Back to chat")
                .accessibilityIdentifier("native-editor-back")
                Text(title)
                    .font(KriaFont.body(13).weight(.semibold))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity)
                Button("Save") { Task { await session.save() } }
                    .font(KriaFont.body(13).weight(.semibold))
                    .frame(minWidth: 48, minHeight: 44)
                    .disabled(session.isSaving || !session.hasUnsavedChanges)
                    .accessibilityLabel(session.isSaving ? "Saving changes" : session.hasUnsavedChanges ? "Save changes" : "Changes saved")
                    .accessibilityIdentifier("native-editor-save")
                Button(action: onDownload) {
                    Image(systemName: "square.and.arrow.down").frame(width: 44, height: 44)
                }
                .disabled(isDownloading || !session.canDownloadCurrentVideo)
                .accessibilityLabel(isDownloading ? "Saving video to Photos" : "Download video")
                .accessibilityIdentifier("native-editor-download")
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
}
