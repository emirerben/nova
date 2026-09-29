import SwiftUI

/// KRI-211: "Find original files" from the editor. The targets come from the editor's own source pool
/// (the sources whose resolution actually failed), not from a device-render manifest, so the sheet
/// always agrees with the preview that sent the creator here. Once every file matches, the live preview
/// is rebuilt.
struct EditorOriginalsRecoveryView: View {
    @ObservedObject var session: NativeEditorSession
    @Environment(\.dismiss) private var dismiss
    @State private var targets: [NativeEditorSession.OriginalRelinkTarget] = []
    @State private var loading = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Find your originals").font(KriaFont.display(26))
                Text("Choose the original files used for this project. Kria checks that they match and keeps them on this iPhone.")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                if loading { ProgressView().accessibilityLabel("Checking original files") }
                ForEach(targets) { target in
                    DeviceOriginalRelinkRow(title: target.title) { file in
                        try await session.relinkOriginal(target, from: file)
                        await refresh()
                        if targets.isEmpty { await rebuildAndClose() }
                    }
                }
                // Never claim the files are here while the preview says they are not.
                if !loading && targets.isEmpty {
                    Text(session.sourcePreviewState == .originalsUnavailable
                         ? "Kria couldn’t tell which files are missing. Open this edit on the device that imported it."
                         : "The original files are available on this iPhone.")
                        .font(KriaFont.body(14))
                        .accessibilityIdentifier("editor-originals-none")
                }
                Button("Done") { dismiss() }.buttonStyle(KriaSecondaryButtonStyle())
            }
            .padding(24)
        }
        .background(KriaColor.paper)
        .task { await refresh() }
    }

    private func refresh() async {
        loading = true
        defer { loading = false }
        targets = await session.originalsNeedingRelink()
    }

    private func rebuildAndClose() async {
        await session.refreshDeviceRender(retry: true)
        await session.prepareSourcePreview()
        dismiss()
    }
}
