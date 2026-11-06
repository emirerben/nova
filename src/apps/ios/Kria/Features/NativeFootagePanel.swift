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

struct NativeSplitScreenControls: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection

    private var slot: EditorTimelineSlot? {
        guard let clip = session.timelineClips.first(where: { $0.id.uuidString == selection.id }) else { return nil }
        return session.document.clips.first { $0.id == clip.slotID }
    }

    private var section: EditorSplitScreenSection? {
        session.document.splitScreenSections.first { $0.id == slot?.splitSectionID && $0.isValid }
    }

    var body: some View {
        if let section {
            Section("Split screen") {
                if let error = session.splitScreenActionError {
                    Text(error).foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("native-editor-split-error")
                }
                Picker("Layout", selection: Binding(
                    get: { self.section?.panelCount ?? section.panelCount },
                    set: { session.setSplitScreenPanelCount($0) }
                )) {
                    Text("2 panels").tag(2)
                    Text("3 panels").tag(3)
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("native-editor-split-count")

                Picker("Panel position", selection: Binding(
                    get: { slot?.splitPanelIndex ?? 0 },
                    set: { session.moveSelectedSplitScreenPanel(to: $0) }
                )) {
                    ForEach(0..<section.panelCount, id: \.self) { index in
                        Text(NativeSplitScreenRows.panelTitle(index: index, count: section.panelCount)).tag(index)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("native-editor-split-position")
                .accessibilityHint("Swaps this panel with the chosen position")

                Stepper(value: Binding(
                    get: { self.section?.durationS ?? section.durationS },
                    set: { session.setSplitScreenDuration($0) }
                ), in: 0.1...1800, step: 0.1) {
                    LabeledContent("Section duration", value: String(format: "%.1f s", section.durationS))
                }
                .accessibilityIdentifier("native-editor-split-duration")
                .accessibilityValue(String(format: "%.1f seconds", section.durationS))

                Stepper(value: Binding(
                    get: { slot?.splitOffsetS ?? 0 },
                    set: { session.setSelectedSplitScreenOffset($0) }
                ), in: 0...max(0, section.durationS - (slot?.durationS ?? 0.1)), step: 0.1) {
                    LabeledContent("Clip starts after", value: String(format: "%.1f s", slot?.splitOffsetS ?? 0))
                }
                .accessibilityIdentifier("native-editor-split-offset")

                Button("Keep this panel full screen") { session.removeSelectedSplitScreen() }
                    .accessibilityIdentifier("native-editor-split-remove")
                Text("Each panel has its own clips, crop, speed and audio. Changing position swaps the complete panels.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .disabled(!session.canEditSplitScreen)

            Section("Panel layout") {
                ForEach(0..<section.panelCount, id: \.self) { index in
                    let title = NativeSplitScreenRows.panelTitle(index: index, count: section.panelCount)
                    let weight = section.panelWeights[index]
                    LabeledContent(title + " height", value: String(format: "%.0f%%", weight / section.panelWeights.reduce(0, +) * 100))
                    NativeEditorSlider(session: session, value: Binding(
                        get: {
                            guard let current = self.section, current.panelWeights.indices.contains(index) else { return weight }
                            return current.panelWeights[index]
                        },
                        set: { session.setSplitScreenPanelWeight(index: index, weight: $0) }
                    ), in: 0.1...max(5, weight), step: 0.05) { Text(title + " panel height") }
                    .accessibilityIdentifier("native-editor-split-weight-\(index)")
                }
                Button("Equal panel heights") {
                    session.beginTransaction()
                    for index in 0..<section.panelCount { session.setSplitScreenPanelWeight(index: index, weight: 1) }
                    session.endTransaction()
                }
                .accessibilityIdentifier("native-editor-split-equal-heights")
                LabeledContent("Divider spacing", value: String(format: "%.1f%%", section.gutter * 100))
                NativeEditorSlider(session: session, value: Binding(
                    get: { self.section?.gutter ?? section.gutter },
                    set: { session.setSplitScreenGutter($0) }
                ), in: 0...0.1, step: 0.005) { Text("Divider spacing") }
                .accessibilityIdentifier("native-editor-split-gutter")
                ColorPicker("Background color", selection: Binding(
                    get: { nativeEditorColor(self.section?.backgroundColor ?? section.backgroundColor) },
                    set: { session.setSplitScreenBackgroundColor(nativeEditorHex($0)) }
                ), supportsOpacity: false)
                .accessibilityIdentifier("native-editor-split-background")
                .accessibilityValue(section.backgroundColor)
            }
            .disabled(!session.canEditSplitScreen)

            Section("Section order") {
                Button("Move section earlier") { session.moveSelectedSplitScreenSection(by: -1) }
                    .accessibilityIdentifier("native-editor-split-section-earlier")
                Button("Move section later") { session.moveSelectedSplitScreenSection(by: 1) }
                    .accessibilityIdentifier("native-editor-split-section-later")
            }
            .disabled(!session.canEditSplitScreen)

            Section("Panel clip") {
                Button("Move clip earlier in panel") { session.moveSelectedSplitScreenClip(by: -1) }
                    .accessibilityIdentifier("native-editor-split-clip-earlier")
                Button("Move clip later in panel") { session.moveSelectedSplitScreenClip(by: 1) }
                    .accessibilityIdentifier("native-editor-split-clip-later")
                clipActions
            }
            .disabled(!session.canEditSplitScreen)
        } else {
            if session.canCreateSplitScreen {
                Section("Split screen") {
                    Button("Create 2 panels") { session.createSplitScreen(panelCount: 2) }
                        .accessibilityIdentifier("native-editor-split-create-2")
                    Button("Create 3 panels") { session.createSplitScreen(panelCount: 3) }
                        .accessibilityIdentifier("native-editor-split-create-3")
                    Text("Start with this clip in each panel, then replace the footage or adjust each panel separately.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            Section("Clip") { clipActions }
                .disabled(!session.canEditTimeline)
        }
    }

    @ViewBuilder private var clipActions: some View {
        LabeledContent("Footage", value: session.splitScreenSourceChoices.first { $0.clipIndex == slot?.clipIndex }?.label ?? "Clip")
            .accessibilityIdentifier("native-editor-split-source")
        Button("Cut at playhead") { session.splitSelectedClip() }
            .disabled(!session.canSplitSelectedClip)
            .accessibilityIdentifier("native-editor-split-cut")
            .accessibilityHint("Move the playhead inside this clip to cut it into two clips")
        Button("Duplicate clip") { session.duplicateSelectedClip() }
            .disabled(!session.canDuplicateSelectedClip)
            .accessibilityIdentifier("native-editor-split-duplicate")
            .accessibilityHint(section == nil ? "Adds another copy after this clip" : "Requires enough unused time after this clip in the same panel")
        Menu("Replace footage") {
            ForEach(session.splitScreenSourceChoices, id: \.clipIndex) { source in
                Button(source.label) { session.replaceSelectedClipSource(clipIndex: source.clipIndex) }
            }
        }
        .disabled(session.splitScreenSourceChoices.isEmpty)
        .accessibilityIdentifier("native-editor-split-replace")
        if session.splitScreenSourceChoices.isEmpty {
            Text("Replacement footage is not available yet.")
                .font(.footnote).foregroundStyle(.secondary)
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
