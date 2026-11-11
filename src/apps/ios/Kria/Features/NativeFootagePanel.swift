import SwiftUI
import KriaMediaEngine

struct NativeFootagePanel: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    @State private var showCrop = false

    var body: some View {
        Group {
            if selection.kind == .clip {
                NativeSplitScreenControls(session: session, selection: selection)
            }
            if session.rendersOnDevice {
                deviceControls
            } else {
                editableControls
            }
        }
        // Recreate the controls when the inspector follows another item.
        .id(selection)
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
        .sheet(isPresented: $showCrop) { NativeCropSheet(session: session, selection: selection) }
        if let blocked { Text(blocked).font(.footnote).foregroundStyle(.secondary) }
    }

    /// Native speed is available only when the server explicitly verified this
    /// clip operation. Crop remains independently capability-gated.
    @ViewBuilder private var deviceControls: some View {
        Section("Speed and crop") {
            if selection.kind == .clip, session.canCropClips || session.footageCrop(for: selection) != nil {
                cropRow
            } else if session.footageCrop(for: selection) != nil {
                Button("Reset crop") { session.setFootageCrop(selection, crop: nil) }
            }
            if selection.kind == .clip, session.serverOffersClipPlaybackRate {
                LabeledContent("Speed", value: String(format: "%g×", session.footagePlaybackRate(for: selection)))
                NativeEditorSlider(
                    session: session,
                    value: Binding(
                        get: { session.footagePlaybackRate(for: selection) },
                        set: { session.setFootagePlaybackRate(selection, rate: $0) }
                    ),
                    in: 0.25...4,
                    step: 0.05
                ) { Text("Playback rate") }
                .accessibilityIdentifier("native-editor-playback-rate")
                .accessibilityValue(String(format: "%.2fx", session.footagePlaybackRate(for: selection)))
                .disabled(session.speedBlockedReason(for: selection) != nil || !session.canSetClipPlaybackRate)
                if let clip = session.timelineClips.first(where: { $0.id.uuidString == selection.id }) {
                    LabeledContent("Clip duration", value: nativeTimecode(clip.end - clip.start))
                        .accessibilityIdentifier("native-editor-speed-duration")
                        .accessibilityValue(nativeTimecode(clip.end - clip.start))
                }
                if let reason = session.speedBlockedReason(for: selection) {
                    Text(reason).font(.footnote).foregroundStyle(.secondary)
                } else if !session.canSetClipPlaybackRate {
                    Text("Speed isn’t available for this edit.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            } else {
                Text("Speed and looks aren’t available yet for edits rendered on this iPhone.").font(.footnote).foregroundStyle(.secondary)
            }
            if abs(session.footagePlaybackRate(for: selection) - 1) > 0.000_001 {
                Button("Reset speed") { session.setFootagePlaybackRate(selection, rate: 1) }
                    .disabled(selection.kind == .clip && (session.speedBlockedReason(for: selection) != nil || !session.canSetClipPlaybackRate))
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
            .disabled(session.speedBlockedReason(for: selection) != nil)
            if let reason = session.speedBlockedReason(for: selection) { Text(reason).font(.footnote).foregroundStyle(.secondary) }
            else { Text("Changes footage speed while animation timing stays separate.").font(.footnote).foregroundStyle(.secondary) }
        }
        // A clip is cropped only where the server explicitly lists `clips.source_crop`; an overlay or visual keeps its section gate.
        if selection.kind != .clip || session.serverOffersClipCrop || session.footageCrop(for: selection) != nil {
            Section("Crop") { cropRow }
        }
    }
}

/// The per-clip actions of the Adjust sheet. Creating and laying out a split moved to the Split tool (`NativeSplitPanel`);
/// panel heights are dragged on the preview and panels are reordered there too.
struct NativeSplitScreenControls: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    @Environment(\.dismiss) private var dismiss
    @State private var showsReplace = false

    private var slot: EditorTimelineSlot? {
        guard let clip = session.timelineClips.first(where: { $0.id.uuidString == selection.id }) else { return nil }
        return session.document.clips.first { $0.id == clip.slotID }
    }

    private var isSplitMember: Bool { slot?.splitSectionID != nil }

    var body: some View {
        Section("Clip") {
            actionError
            clipActions
        }
        .disabled(isSplitMember ? !session.canEditSplitScreen : !session.canEditTimeline)
    }

    @ViewBuilder private var actionError: some View {
        if let error = session.splitScreenActionError {
            Text(error).foregroundStyle(KriaColor.failureText)
                .accessibilityIdentifier("native-editor-split-error")
        }
    }

    @ViewBuilder private var clipActions: some View {
        if let speaker = session.speakerSelection(forClip: selection) {
            Button("Speaker position") {
                dismiss()
                session.select(speaker, seekToStart: false)
            }
            .accessibilityIdentifier("native-editor-clip-speaker-position")
        }
        LabeledContent("Footage", value: session.splitScreenSourceChoices.first { $0.clipIndex == slot?.clipIndex }?.label ?? "Clip")
            .accessibilityIdentifier("native-editor-split-source")
        Button("Cut at playhead") { session.splitSelectedClip() }
            .disabled(!session.canSplitSelectedClip)
            .accessibilityIdentifier("native-editor-split-cut")
            .accessibilityHint("Move the playhead inside this clip to cut it into two clips")
        Button("Duplicate clip") { session.duplicateSelectedClip() }
            .disabled(!session.canDuplicateSelectedClip)
            .accessibilityIdentifier("native-editor-split-duplicate")
            .accessibilityHint(isSplitMember ? "Requires enough unused time after this clip in the same panel" : "Adds another copy after this clip")
        replaceFootage
        if let error = session.addClipError {
            Text(error).font(.footnote).foregroundStyle(KriaColor.failureText)
                .accessibilityIdentifier("native-editor-split-source-error")
        }
    }

    /// A clip inside a split swaps its footage through the Gallery | Saved picker, or from footage already in the edit. An ordinary clip still picks from
    /// the footage already in the edit.
    @ViewBuilder private var replaceFootage: some View {
        if isSplitMember, let slotID = slot?.id {
            Button("Replace footage") { showsReplace = true }
                .disabled((!session.canAddTimelineMedia && session.splitScreenSourceChoices.isEmpty) || session.isAddingClip)
                .accessibilityIdentifier("native-editor-split-replace")
                .sheet(isPresented: $showsReplace) { NativeSplitReplaceSheet(session: session, slotID: slotID) }
            if let reason = session.addClipUnavailableReason {
                Text(reason).font(.footnote).foregroundStyle(.secondary)
            }
        } else {
            Menu {
                ForEach(session.splitScreenSourceChoices, id: \.clipIndex) { source in
                    Button(source.label) { session.replaceSelectedClipSource(clipIndex: source.clipIndex) }
                }
            } label: {
                HStack {
                    Text("Replace footage")
                    Spacer()
                    Image(systemName: "chevron.up.chevron.down").foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .disabled(session.splitScreenSourceChoices.isEmpty)
            .accessibilityIdentifier("native-editor-split-replace")
            if session.splitScreenSourceChoices.isEmpty {
                Text("Replacement footage is not available yet.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
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
