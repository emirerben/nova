import SwiftUI
import KriaMediaEngine

struct NativeFootagePanel: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    @State private var showCrop = false

    var body: some View {
        Group {
            if session.rendersOnDevice {
                deviceControls
            } else {
                editableControls
            }
        }
        // Recreate the controls when the inspector follows another item.
        .id(selection)
        .sheet(isPresented: $showCrop) { NativeCropSheet(session: session, selection: selection) }
    }

    /// Crop opens the shared editor; the clip's `clips.source_crop` capability (or, for an overlay, its section) decides.
    @ViewBuilder private var cropRow: some View {
        let blocked = selection.kind == .clip ? session.cropBlockedReason(for: selection) : nil
        Button { showCrop = true } label: {
            HStack {
                Label("Crop…", systemImage: "crop")
                Spacer()
                if session.footageCrop(for: selection) != nil { Text("Cropped").foregroundStyle(.secondary) }
            }
        }
        .disabled(blocked != nil)
        .accessibilityIdentifier("native-editor-footage-crop")
        if let blocked { Text(blocked).font(.footnote).foregroundStyle(.secondary) }
    }

    /// The phone renderer can't draw speed yet, so a device variant only offers to clear a saved speed. Crop is
    /// offered only when the server opens `clips.source_crop` for this variant.
    @ViewBuilder private var deviceControls: some View {
        Section("Speed and crop") {
            if selection.kind == .clip, session.canCropClips || session.footageCrop(for: selection) != nil {
                cropRow
            } else if session.footageCrop(for: selection) != nil {
                Button("Reset crop") { session.setFootageCrop(selection, crop: nil) }
            }
            Text("Speed and looks aren’t available yet for edits rendered on this iPhone.").font(.footnote).foregroundStyle(.secondary)
            if abs(session.footagePlaybackRate(for: selection) - 1) > 0.000_001 {
                Button("Reset speed") { session.setFootagePlaybackRate(selection, rate: 1) }
            }
        }
    }

    @ViewBuilder private var editableControls: some View {
        Section("Speed") {
            NativeEditorSlider(
                session: session,
                value: Binding(
                    get: { session.footagePlaybackRate(for: selection) },
                    set: { session.setFootagePlaybackRate(selection, rate: $0) }
                ),
                in: 0.25...4,
                step: 0.05
            ) { Text("Playback rate") }
            Text("Changes footage speed while animation timing stays separate.").font(.footnote).foregroundStyle(.secondary)
        }
        // A clip is cropped only where the server explicitly lists `clips.source_crop`; an overlay or visual keeps its section gate.
        if selection.kind != .clip || session.serverOffersClipCrop || session.footageCrop(for: selection) != nil {
            Section("Crop") { cropRow }
        }
    }
}

/// The shared Crop editor (KRI-44) for a clip, overlay or visual, writing through `setFootageCrop`.
/// `embedded` = already inside the editor inspector's navigation stack (and its Done button).
struct NativeCropSheet: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    var embedded = false
    @Environment(\.dismiss) private var dismiss
    @State private var crop: NormalizedSourceRect?

    private var content: some View {
        KriaCropFrameHost(
            load: { await session.cropSourceFrame(for: selection) },
            outputAspect: session.cropOutputAspect,
            crop: Binding(
                get: { crop },
                set: { crop = $0; session.setFootageCrop(selection, crop: $0) }
            ),
            onEditingChanged: { editing in
                if editing { session.beginTransaction() } else { session.endTransaction() }
            }
        )
        .padding(16)
        .background(KriaColor.paper)
        .onAppear { crop = session.footageCrop(for: selection) }
        .accessibilityIdentifier("native-editor-crop-sheet")
    }

    var body: some View {
        if embedded {
            content
        } else {
            NavigationStack {
                content
                    .navigationTitle("Crop")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { dismiss() }.accessibilityIdentifier("native-editor-crop-done")
                        }
                    }
            }
            .tint(KriaColor.ink)
            .presentationDetents([.large])
        }
    }
}
