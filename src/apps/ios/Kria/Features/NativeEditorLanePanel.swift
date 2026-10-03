import SwiftUI

/// The Paper inspector shell keeps the preview visible while controls scroll.
struct NativeEditorLanePanel<Tab: Hashable & RawRepresentable, Content: View>: View where Tab.RawValue == String {
    @Environment(\.nativeEditorConnectedPanel) private var connected
    let title: String
    let tabs: [Tab]
    @Binding var tab: Tab
    let onDone: () -> Void
    var heading: String? = nil
    var onAdd: (() -> Void)? = nil
    var onDelete: (() -> Void)? = nil
    /// Add/Remove labels + ids default to the Visuals panel's; other lanes (Sounds) override them.
    var addLabel = "Add visual"
    var addID = "native-editor-add-another-visual"
    var removeLabel = "Remove visual"
    var removeID = "native-editor-remove-visual"
    /// Scroll-phase changes of the panel's own ScrollView (user drags, deceleration,
    /// idle). Lets a panel react to hand scrolling without a competing DragGesture.
    var onScrollPhase: ((ScrollPhase) -> Void)? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(spacing: 6) {
            VStack(spacing: 6) {
                HStack {
                    Text(heading ?? title).font(KriaFont.body(connected ? 18 : 15).weight(.semibold))
                    Spacer()
                    if let onAdd {
                        Button(action: onAdd) {
                            Image(systemName: "plus").frame(width: 44, height: 44)
                        }
                        .frame(minHeight: 44)
                        .accessibilityLabel(addLabel)
                        .accessibilityIdentifier(addID)
                    }
                    if let onDelete {
                        Button(role: .destructive, action: onDelete) {
                            Image(systemName: "trash").frame(minWidth: 44, minHeight: 44)
                        }
                        .accessibilityLabel(removeLabel)
                        .accessibilityIdentifier(removeID)
                    }
                    Button(action: onDone) {
                        Text("Done").frame(minWidth: 64, minHeight: 44)
                            .background(KriaColor.ink.opacity(0.06), in: Capsule())
                    }
                        .accessibilityIdentifier("native-editor-\(title.lowercased())-done")
                }
                if tabs.count > 1 {
                    NativeEditorPanelTabs(tabs: tabs, selection: $tab, accessibilityPrefix: "native-editor-\(title.lowercased())")
                }
            }
            .nativeEditorPanelResizeSurface()
            ScrollView { content().padding(.top, 8).padding(.bottom, 12) }
                .scrollDismissesKeyboard(.interactively)
                .accessibilityIdentifier("native-editor-\(title.lowercased())-scroll")
                .onScrollPhaseChange { _, phase, context in
                    onScrollPhase?(phase)
                    #if DEBUG
                    guard title == "Visuals" else { return }
                    let geometry = context.geometry
                    NativePreviewDiagnostics.record("gallery-scroll", fields: [
                        "phase": String(describing: phase),
                        "offset": String(Double(geometry.contentOffset.y)),
                        "contentHeight": String(Double(geometry.contentSize.height)),
                        "viewportHeight": String(Double(geometry.containerSize.height))
                    ])
                    #endif
                }
        }
        .padding(.horizontal, connected ? 24 : 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(connected ? Color.clear : KriaColor.paper)
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-\(title.lowercased())-panel")
    }
}

struct NativeCaptionPanel: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.nativeEditorPanelContentWidth) private var contentWidth
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var stacksControls: Bool { dynamicTypeSize.isAccessibilitySize || contentWidth < 300 }
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String, CaseIterable { case edit = "Edit captions", style = "Style", settings = "Settings" }
    @ObservedObject var session: NativeEditorSession
    /// KRI-240 (plan 026 D27): the caption line open in the edit bar. Owned by
    /// `NativeEditorView`, which lays the editor out around it (Variant A). A row tap
    /// sets it; keyboard focus is requested from it, never the other way round, so a
    /// refused focus leaves a usable bar instead of a stuck screen. The old two-tap
    /// flow (KRI-110 note) came from a SwiftUI `@FocusState` that never committed;
    /// the UIKit line editor takes focus in `didMoveToWindow`, like the Text panel.
    @Binding var editingCueID: String?
    /// Bumped by the editor when the preview is tapped during caption editing (D5).
    var loopToggleRequest = 0
    /// Field lines in the edit bar. `NativeEditorView` reserves the bar's height from
    /// this same number, so the drawn bar always matches the space above the keyboard.
    var editLines = 3
    /// Field line height, scaled by the editor alongside `editLines` for the same reason.
    var editLineHeight: CGFloat = 22
    let onDone: () -> Void
    @State private var tab: Tab = .edit
    @State private var fieldFocused = false
    @State private var lineOpen = false
    @State private var loopingCueID: String?
    @State private var followSuspended = false
    @State private var followResumeTask: Task<Void, Never>?
    @State private var removal: CaptionRemovalNotice?
    /// The line under the playhead (plan 026 D15). Set from the playback clock, which
    /// the session keeps off its own publisher, so the panel re-renders when the
    /// playing line changes rather than on every clock tick.
    @State private var playingID: String?
    /// The line just closed, scrolled back into view when the list returns (D4).
    @State private var returnRowID: String?
    private var meta: [String: JSONValue] { session.document.captionMeta }
    private var appearance: [String: JSONValue] { meta["appearance"]?.objectValue ?? [:] }
    private var units: [EditorCaptionCue] { session.document.captionUnits }

    var body: some View {
        // A container, not a Group: a Group copies onAppear/onDisappear onto each
        // branch, so swapping the list for the bar ended the line's transaction.
        VStack(spacing: 0) {
            if let id = editingCueID, let index = units.firstIndex(where: { $0.id == id }) {
                editBar(id: id, index: index)
            } else {
                NativeEditorLanePanel(title: "Captions", tabs: Tab.allCases, tab: $tab, onDone: {
                    leaveLine(to: nil); onDone()
                }, onScrollPhase: handleScrollPhase) {
                    VStack(spacing: 8) {
                        switch tab {
                        // KRI-216: each tab is gated by its own server capability
                        // (caption_cues for line edits, caption_meta for style and
                        // settings) rather than the coarse canEditCaptions, so a
                        // phone variant with only one lane editable doesn't show
                        // Save-refusing controls as enabled — or block a lane that
                        // IS editable just because the other one isn't. Read-only
                        // caption lines still seek (plan 026 D16).
                        case .edit: transcript
                        case .style: style.disabled(!session.canEditCaptionMeta)
                        case .settings: settings.disabled(!session.canEditCaptionMeta)
                        }
                        if !session.canEditCaptions {
                            Text("Captions aren’t available for this edit.").foregroundStyle(KriaColor.mutedInk)
                        }
                    }
                }
            }
        }
        .onChange(of: editingCueID) { _, new in
            // Cleared from outside (tool change, reload): close the open line once.
            if new == nil, lineOpen { session.endCaptionLineEdit(); lineOpen = false; loopingCueID = nil }
        }
        .onChange(of: units.map(\.id)) { _, ids in
            // The open line left the document (save, reload, rebase): close the bar so
            // the editor brings its chrome back instead of hiding it over the list.
            if let id = editingCueID, !ids.contains(id) { editingCueID = nil }
            playingID = playingLine(at: session.currentTime)
        }
        .onChange(of: session.canEditCaptionLines) { _, editable in
            if !editable, editingCueID != nil { leaveLine(to: nil) }
        }
        .onChange(of: tab) { _, _ in leaveLine(to: nil) }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background, editingCueID != nil { leaveLine(to: nil) }
        }
        .onChange(of: loopToggleRequest) { _, _ in toggleLoop() }
        .onChange(of: session.isPlaying) { _, playing in
            // A loop restart seeks (which pauses) and resumes in the same turn; onChange
            // sees only the settled value, so a real pause is what ends the loop.
            if playing { followSuspended = false } else if loopingCueID != nil, !loopRestartPending { loopingCueID = nil }
        }
        .onReceive(session.playbackClock.$currentTime) { time in
            let line = playingLine(at: time)
            if line != playingID { playingID = line }
            continueLoop(at: time)
        }
        .onAppear {
            panelLifecycle?.register(owner: lifecycleOwner) { leaveLine(to: nil) }
        }
        .onDisappear {
            panelLifecycle?.unregister(owner: lifecycleOwner)
            if lineOpen { session.endCaptionLineEdit(); lineOpen = false }
        }
    }

    // MARK: Browse (keyboard down)

    /// A hand scroll pauses follow; it resumes ~2s after the list settles. This replaces a
    /// `DragGesture` on the rows, which fought the ScrollView's own pan on iOS 18 (KRI-281).
    private func handleScrollPhase(_ phase: ScrollPhase) {
        switch phase {
        case .interacting, .decelerating:
            followResumeTask?.cancel()
            followSuspended = true
        case .idle:
            guard followSuspended else { return }
            followResumeTask?.cancel()
            followResumeTask = Task { @MainActor in
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled else { return }
                followSuspended = false
            }
        default:
            break
        }
    }

    private func playingLine(at time: TimeInterval) -> String? {
        session.captionTimelineRanges().last { $0.range.lowerBound <= time && time < $0.range.upperBound }?.id
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 8) {
                if let removal { removalNotice(removal) }
                if !session.canEditCaptionLines, session.canEditCaptions {
                    Text(session.capability(for: .captions)?.reason ?? "Caption lines can’t be edited for this video.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityIdentifier("native-editor-caption-readonly-reason")
                }
                if units.isEmpty {
                    Text("No captions on this edit yet")
                        .frame(maxWidth: .infinity, minHeight: 80)
                        .foregroundStyle(KriaColor.mutedInk)
                }
                ForEach(Array(units.enumerated()), id: \.element.id) { index, cue in
                    row(cue, index: index).id(cue.id)
                }
            }
            .onChange(of: playingID) { _, id in
                // Follow the playing line (plan 026 D15): never while the creator is
                // scrolling, editing, or using VoiceOver.
                guard let id, session.isPlaying, !followSuspended, !voiceOverEnabled, editingCueID == nil else { return }
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { proxy.scrollTo(id, anchor: .center) }
            }
            .onAppear {
                guard let id = returnRowID else { return }
                returnRowID = nil
                DispatchQueue.main.async { proxy.scrollTo(id, anchor: .center) }
            }
        }
    }

    private func row(_ cue: EditorCaptionCue, index: Int) -> some View {
        let playing = playingID == cue.id
        let selected = session.selection == EditorSelection(kind: .captionCue, id: cue.id)
        let edited = session.isCaptionUnitEdited(id: cue.id)
        let editable = session.canEditCaptionLines
        return HStack(alignment: .top, spacing: 10) {
            ZStack(alignment: .leading) {
                if edited { Circle().fill(KriaColor.sky).frame(width: 6, height: 6).offset(x: -9, y: 1) }
                Text(String(index + 1)).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
            .frame(minWidth: 22, alignment: .leading)
            .fixedSize(horizontal: true, vertical: false)
            VStack(alignment: .leading, spacing: 4) {
                Text(cue.text.isEmpty ? " " : cue.text)
                    .font(KriaFont.body(16).weight(playing ? .semibold : .regular))
                    .foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(time(cue.startS))
                    .font(KriaFont.body(12)).monospacedDigit().foregroundStyle(KriaColor.mutedInk)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8).frame(minHeight: 59)
        .background(playing || selected ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
        .overlay(alignment: .leading) {
            if playing { KriaColor.sky.frame(width: 3).padding(.vertical, 6) }
        }
        .contentShape(Rectangle())
        .onTapGesture { open(cue.id) }
        .contextMenu {
            if editable {
                Button("Delete", role: .destructive) { remove(cue.id, lineNumber: index + 1) }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Line \(index + 1), \(cue.text), \(spokenTime(cue.startS)) to \(spokenTime(cue.endS))")
        .accessibilityValue([playing ? "Playing" : nil, edited ? "Edited" : nil].compactMap { $0 }.joined(separator: ", "))
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { open(cue.id) }
        .accessibilityAction(named: editable ? "Edit caption" : "Play from here") { open(cue.id) }
        .accessibilityActions {
            if editable { Button("Delete") { remove(cue.id, lineNumber: index + 1) } }
        }
        .accessibilityIdentifier("native-editor-caption-row-" + cue.id)
    }

    // MARK: Edit bar (keyboard up)

    private func editBar(id: String, index: Int) -> some View {
        let isLast = index == units.count - 1
        return CaptionEditBar(
            lineNumber: index + 1,
            lineCount: units.count,
            timeRange: "\(time(units[index].startS))–\(time(units[index].endS))",
            spokenTimeRange: "\(spokenTime(units[index].startS)) to \(spokenTime(units[index].endS))",
            lineHeight: editLineHeight,
            lines: editLines,
            text: Binding(
                get: { units.first { $0.id == id }?.text ?? "" },
                set: { new in
                    // The first keystroke pauses playback and ends the loop (D4, D6);
                    // the removal notice's Undo would now revert typing, so it goes (R12).
                    if session.isPlaying { session.pausePlayback() }
                    loopingCueID = nil
                    removal = nil
                    session.updateCaptionCue(id: id, text: new)
                }
            ),
            focused: $fieldFocused,
            configuration: .captionLine(language: session.captionLanguage, isLast: isLast),
            lineID: id,
            canGoPrevious: index > 0,
            canGoNext: !isLast,
            removal: removal,
            canUndoRemoval: removal.map { session.undoHistoryVersion == $0.undoVersion } ?? false,
            onUndoRemoval: undoRemoval,
            onRemovalExpired: { expired in if removal == expired { removal = nil } },
            onPrevious: { if let previous = neighbour(of: id, offset: -1) { leaveLine(to: previous) } },
            onNext: { leaveLine(to: neighbour(of: id, offset: 1)) },
            onDone: { leaveLine(to: nil) }
        )
    }

    /// The line `offset` places from `id`, looked up when the key is pressed: two
    /// queued Tabs can arrive after the first one removed an emptied line.
    private func neighbour(of id: String, offset: Int) -> String? {
        guard let index = units.firstIndex(where: { $0.id == id }), units.indices.contains(index + offset) else { return nil }
        return units[index + offset].id
    }

    /// Row tap: seek to the line and, when lines are editable, open it in the bar
    /// with the keyboard up in one tap.
    private func open(_ id: String) {
        guard session.canEditCaptionLines else {
            session.select(EditorSelection(kind: .captionCue, id: id), seekToStart: false)
            if let park = session.captionParkTime(id: id) { session.seek(to: park) }
            return
        }
        enter(id)
    }

    private func enter(_ id: String) {
        // Save and tool changes flush the open line through the lifecycle (which
        // drops the registration once used), so every opened line re-registers.
        panelLifecycle?.register(owner: lifecycleOwner) { leaveLine(to: nil) }
        session.beginCaptionLineEdit(id: id)
        lineOpen = true
        editingCueID = id
        session.select(EditorSelection(kind: .captionCue, id: id), seekToStart: false)
        if let park = session.captionParkTime(id: id) { session.seek(to: park) }
        fieldFocused = true
        if let index = units.firstIndex(where: { $0.id == id }) {
            CaptionLineAnnouncer.post("Line \(index + 1) of \(units.count)")
        }
    }

    /// Commits the open line (plan 026 D5): an emptied line is removed inside the
    /// line's own transaction, so one Undo restores it with its text and timings (R12).
    private func leaveLine(to destination: String?) {
        loopingCueID = nil
        if let current = editingCueID, lineOpen {
            let index = units.firstIndex { $0.id == current }
            let text = units.first { $0.id == current }?.text ?? ""
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, session.canEditCaptionLines {
                session.deleteSelection(EditorSelection(kind: .captionCue, id: current))
                session.endCaptionLineEdit()
                removal = CaptionRemovalNotice(lineNumber: (index ?? 0) + 1, undoVersion: session.undoHistoryVersion)
            } else {
                session.endCaptionLineEdit()
            }
            lineOpen = false
        }
        if let destination, units.contains(where: { $0.id == destination }) {
            enter(destination)
        } else {
            if let left = editingCueID, units.contains(where: { $0.id == left }) { returnRowID = left }
            fieldFocused = false
            editingCueID = nil
        }
    }

    private func remove(_ id: String, lineNumber: Int) {
        if editingCueID == id { leaveLine(to: nil) }
        session.deleteSelection(EditorSelection(kind: .captionCue, id: id))
        removal = CaptionRemovalNotice(lineNumber: lineNumber, undoVersion: session.undoHistoryVersion)
    }

    private func undoRemoval() {
        guard let removal, session.undoHistoryVersion == removal.undoVersion else { self.removal = nil; return }
        let open = editingCueID
        if lineOpen { session.endCaptionLineEdit(); lineOpen = false }
        session.undo()
        self.removal = nil
        if let open, units.contains(where: { $0.id == open }) {
            session.beginCaptionLineEdit(id: open); lineOpen = true
        }
    }

    private func removalNotice(_ notice: CaptionRemovalNotice) -> some View {
        CaptionRemovalNoticeView(notice: notice, canUndo: session.undoHistoryVersion == notice.undoVersion,
                                 onUndo: undoRemoval, onExpire: { if removal == notice { removal = nil } })
    }

    // MARK: Loop-play (plan 026 D6)

    @State private var loopRestartPending = false

    private func toggleLoop() {
        guard let id = editingCueID, let range = session.captionTimelineRange(id: id) else { return }
        if loopingCueID == id {
            loopingCueID = nil
            if session.isPlaying { session.pausePlayback() }
            return
        }
        loopingCueID = id
        restartLoop(range)
    }

    private func restartLoop(_ range: ClosedRange<TimeInterval>) {
        session.seek(to: range.lowerBound)
        if !session.isPlaying { session.togglePlayback() }
    }

    private func continueLoop(at time: TimeInterval) {
        guard let id = loopingCueID, session.isPlaying, !loopRestartPending,
              let range = session.captionTimelineRange(id: id),
              time >= min(range.upperBound, session.duration) - 0.03 else { return }
        // Not inside the clock's publish: seeking re-sets the clock, and the outer
        // write would land last. A line ending at the video's end also pauses
        // playback first; the pending flag keeps that pause from ending the loop.
        loopRestartPending = true
        DispatchQueue.main.async {
            loopRestartPending = false
            guard loopingCueID == id, let range = session.captionTimelineRange(id: id) else { return }
            restartLoop(range)
        }
    }

    private func spokenTime(_ base: Double) -> String {
        let seconds = Int(session.timelineProjection.projectBaseTime(base).rounded())
        // English like the rest of the label ("Line 2, …, 1 second to 4 seconds"), not the device locale.
        return Duration.seconds(seconds).formatted(
            .units(allowed: [.minutes, .seconds], width: .wide).locale(Locale(identifier: "en_US"))
        )
    }

    private var style: some View {
        VStack(spacing: 8) {
            NativeEditorMenuRow(title: "Display", value: meta["style"] == .string("word") ? "Word" : "Sentence") {
                Button("Sentence") { session.setCaptionDisplay("sentence") }
                Button("Word") { session.setCaptionDisplay("word") }
            }.disabled(!session.canEditCaptionAppearance)
                .accessibilityIdentifier("native-editor-caption-display")
            let layout = stacksControls ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 10))
            layout {
                NativeFontPicker(
                    selection: meta["font"]?.stringValue,
                    includeDefault: true,
                    accessibilityLabelText: "Caption font",
                    accessibilityID: "native-editor-caption-font"
                ) { session.setCaptionFont($0) }
                HStack(spacing: 0) {
                    ForEach(["left", "center", "right"], id: \.self) { value in
                        Button { session.setCaptionAppearance(key: "alignment", value: .string(value)) } label: {
                            Image(systemName: "text.align" + value).frame(width: 44, height: 44)
                                .background((appearance["alignment"]?.stringValue ?? "center") == value ? KriaColor.selectionSoft : .clear)
                        }.accessibilityLabel("Align captions " + value)
                    }
                }.background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                    .disabled(!session.canEditCaptionAppearance)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Color")
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 44), spacing: 8)], spacing: 8) {
                    ForEach(["#FFFFFF", "#30352C", "#FFF19E", "#9BCAFF", "#E5DAF5"], id: \.self) { hex in
                        Button { session.setCaptionMeta(key: "color", value: .string(hex)) } label: {
                            Circle().fill(nativeEditorColor(hex)).frame(width: 30, height: 30)
                                .overlay(Circle().stroke((meta["color"]?.stringValue ?? "#FFFFFF") == hex ? KriaColor.sky : .clear, lineWidth: 2))
                                .frame(minWidth: 44, minHeight: 44)
                        }.accessibilityLabel("Caption color " + hex)
                    }
                    ColorPicker("Custom caption color", selection: color("color", fallback: "#FFFFFF"), supportsOpacity: false)
                        .labelsHidden().frame(minWidth: 44, minHeight: 44)
                }
            }
            effectRow("Outline", key: "stroke_width", colorKey: "stroke_color", fallback: 4, range: 0...12)
            effectRow("Shadow", key: "shadow_opacity", colorKey: "shadow_color", fallback: meta["shadow_enabled"] == .bool(false) ? 0 : 0.5, range: 0...1)
            HStack {
                Text("Size").frame(width: 66, alignment: .leading)
                NativePaperSlider(session: session, value: number("size_px", fallback: 78), bounds: 36...160, step: 1, label: "Caption size")
                Text(String(Int(meta["size_px"]?.numberValue ?? 78))).monospacedDigit().frame(width: 44)
            }.frame(minHeight: 44)
        }
    }

    private var highlightsWords: Bool { appearance["highlight_spoken_word"]?.boolValue ?? (meta["style"] == .string("word")) }

    private var settings: some View {
        VStack(spacing: 8) {
            NativeEditorMenuRow(title: "Show captions", value: meta["enabled"] != .bool(false) ? "On" : "Off") {
                Button("On") { session.setCaptionEnabled(true) }
                Button("Off") { session.setCaptionEnabled(false) }
            }.accessibilityIdentifier("native-editor-caption-visible")
            NativeEditorMenuRow(title: "Highlight spoken word", value: highlightsWords ? "On" : "Off") {
                Button("On") { session.setCaptionAppearance(key: "highlight_spoken_word", value: .bool(true)) }
                Button("Off") { session.setCaptionAppearance(key: "highlight_spoken_word", value: .bool(false)) }
            }.disabled(!session.canEditCaptionAppearance)
                .accessibilityIdentifier("native-editor-caption-highlight")
            if highlightsWords {
                ColorPicker("Highlight color", selection: color("highlight_color", fallback: "#C5F82A"), supportsOpacity: false)
                    .disabled(!session.canEditCaptionAppearance)
                    .frame(minHeight: 44)
            }
            Text("Applies to all captions in this edit.\nEdit individual lines in Edit captions.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func effectRow(_ title: String, key: String, colorKey: String, fallback: Double, range: ClosedRange<Double>) -> some View {
        HStack(spacing: 12) {
            Text(title).frame(width: 66, alignment: .leading)
            ColorPicker(title + " color", selection: color(colorKey, fallback: "#000000", nested: true), supportsOpacity: false)
                .labelsHidden().disabled(!session.canEditCaptionAppearance)
            NativePaperSlider(session: session, value: number(key, fallback: fallback, nested: key == "shadow_opacity"), bounds: range, step: key == "shadow_opacity" ? 0.01 : 1, label: title)
                .disabled(key == "shadow_opacity" && !session.canEditCaptionAppearance)
            Text("\(Int(((key == "shadow_opacity" ? appearance : meta)[key]?.numberValue ?? fallback) * (key == "shadow_opacity" ? 100 : 1)))\(key == "shadow_opacity" ? "%" : " px")")
                .monospacedDigit().frame(width: 44, alignment: .trailing)
        }.frame(minHeight: 44)
    }
    private func color(_ key: String, fallback: String, nested: Bool = false) -> Binding<Color> {
        Binding(get: { nativeEditorColor((nested ? appearance : meta)[key]?.stringValue ?? fallback) }, set: {
            let value = JSONValue.string(nativeEditorHex($0))
            if nested { session.setCaptionAppearance(key: key, value: value) } else { session.setCaptionMeta(key: key, value: value) }
        })
    }
    private func number(_ key: String, fallback: Double, nested: Bool = false) -> Binding<Double> {
        Binding(get: { (nested ? appearance : meta)[key]?.numberValue ?? fallback }, set: {
            if nested {
                session.setCaptionAppearance(key: key, value: .number($0))
                session.setCaptionShadowEnabled($0 > 0)
            } else { session.setCaptionMeta(key: key, value: .number($0.rounded())) }
        })
    }
    private func time(_ base: Double) -> String {
        let seconds = session.timelineProjection.projectBaseTime(base)
        return String(format: "%d:%04.1f", Int(seconds) / 60, seconds.truncatingRemainder(dividingBy: 60))
    }
}

/// "Line 4 removed · Undo" after a caption line is removed (plan 026 D3, R12).
struct CaptionRemovalNotice: Equatable {
    let id = UUID()
    let lineNumber: Int
    /// `undoHistoryVersion` right after the removal; Undo is offered only while it still matches.
    let undoVersion: Int
}

struct CaptionRemovalNoticeView: View {
    let notice: CaptionRemovalNotice
    let canUndo: Bool
    let onUndo: () -> Void
    let onExpire: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text("Line \(notice.lineNumber) removed").font(KriaFont.body(13)).foregroundStyle(KriaColor.ink)
                .lineLimit(1).minimumScaleFactor(0.8)
            if canUndo {
                Button(action: onUndo) {
                    Text("Undo").font(KriaFont.body(13).weight(.semibold))
                        .frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
                    .accessibilityIdentifier("native-editor-caption-undo-removal")
            }
        }
        // Grouped, not combined: Undo stays its own button for VoiceOver and tests.
        .accessibilityElement(children: .contain)
        .task(id: notice.id) {
            CaptionLineAnnouncer.post("Line \(notice.lineNumber) removed")
            do {
                try await Task.sleep(for: .seconds(4))
                onExpire()
            } catch {
                // Cancelled: the notice moved between the list and the bar.
            }
        }
    }
}

/// VoiceOver announcements for the caption editor (plan 026 D13).
enum CaptionLineAnnouncer {
    @MainActor static func post(_ message: String) {
        UIAccessibility.post(notification: .announcement, argument: message)
    }
}

/// KRI-240 Variant A edit bar (plan 026, board frame A): replaces the caption list
/// while a line is open. Row 1: "#N · start–end" and three equal 44pt buttons
/// (previous, next, approve) aligned to the field's trailing edge. Row 2: the
/// full-width line field (wraps, Return = Next).
struct CaptionEditBar: View {
    /// Room above the buttons so they clear the panel's 32pt rounded corner.
    static let topPadding: CGFloat = 12
    static let bottomPadding: CGFloat = 10
    static let navigationHeight: CGFloat = 44
    /// Gap between the button row and the field, so the buttons don't sit on the field's edge.
    static let rowSpacing: CGFloat = 6
    static let buttonSize: CGFloat = 44
    static let cornerRadius: CGFloat = 12

    /// Two field lines on short or narrow screens and at accessibility text sizes,
    /// three otherwise (plan 026 D8, D12). One rule for both the reserved height and
    /// the drawn field: an iPhone SE is short (667pt) but its panel is 303pt wide.
    static func fieldLineCount(isAccessibilitySize: Bool, screenHeight: CGFloat, contentWidth: CGFloat) -> Int {
        isAccessibilitySize || screenHeight < 700 || contentWidth < 300 ? 2 : 3
    }
    static func fieldHeight(lineHeight: CGFloat, lines: Int) -> CGFloat { CGFloat(lines) * lineHeight + 16 }
    static func height(lineHeight: CGFloat, lines: Int) -> CGFloat {
        topPadding + navigationHeight + rowSpacing + fieldHeight(lineHeight: lineHeight, lines: lines) + bottomPadding
    }

    let lineNumber: Int
    let lineCount: Int
    let timeRange: String
    let spokenTimeRange: String
    let lineHeight: CGFloat
    let lines: Int
    @Binding var text: String
    @Binding var focused: Bool
    let configuration: LineEditorConfiguration
    let lineID: String
    let canGoPrevious: Bool
    let canGoNext: Bool
    let removal: CaptionRemovalNotice?
    let canUndoRemoval: Bool
    let onUndoRemoval: () -> Void
    let onRemovalExpired: (CaptionRemovalNotice) -> Void
    let onPrevious: () -> Void
    let onNext: () -> Void
    let onDone: () -> Void

    var body: some View {
        VStack(spacing: Self.rowSpacing) {
            HStack(spacing: 8) {
                if let removal {
                    CaptionRemovalNoticeView(notice: removal, canUndo: canUndoRemoval, onUndo: onUndoRemoval,
                                             onExpire: { onRemovalExpired(removal) })
                } else {
                    Text("#\(lineNumber) · \(timeRange)")
                        .font(KriaFont.body(13)).monospacedDigit().foregroundStyle(KriaColor.mutedInk)
                        .lineLimit(1).minimumScaleFactor(0.7)
                        .accessibilityLabel("Line \(lineNumber) of \(lineCount)")
                        .accessibilityValue(spokenTimeRange)
                        .accessibilityIdentifier("native-editor-caption-position")
                }
                Spacer(minLength: 8)
                HStack(spacing: 8) {
                    squareButton(symbol: "chevron.left", label: "Previous line", id: "native-editor-caption-previous",
                                 style: .outline, enabled: canGoPrevious, action: onPrevious)
                    squareButton(symbol: "chevron.right", label: "Next line", id: "native-editor-caption-next",
                                 style: .outline, enabled: canGoNext, action: onNext)
                    squareButton(symbol: "checkmark", label: "Done", id: "native-editor-caption-edit-done",
                                 style: .approve, enabled: true, action: onDone)
                }
            }
            .frame(height: Self.navigationHeight)
            NativeExplicitLineTextEditor(
                text: $text, focused: $focused, identifier: "native-editor-caption-field",
                configuration: configuration,
                actions: LineEditorActions(onReturn: canGoNext ? onNext : onDone, onNextLine: onNext,
                                           onPreviousLine: onPrevious, onEscape: onDone),
                lineID: lineID
            )
            .frame(maxWidth: .infinity)
            .frame(height: Self.fieldHeight(lineHeight: lineHeight, lines: lines))
            .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
            .overlay(RoundedRectangle(cornerRadius: Self.cornerRadius).stroke(KriaColor.line, lineWidth: 1))
        }
        .padding(.top, Self.topPadding)
        .padding(.bottom, Self.bottomPadding)
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-caption-edit-bar")
    }

    private enum SquareStyle { case outline, approve }

    /// 44 × 44, 12pt corners — the same box for all three so they read as one group.
    /// The approve button is the bar's primary action (Butter, DESIGN.md §9).
    private func squareButton(symbol: String, label: String, id: String, style: SquareStyle, enabled: Bool,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(enabled ? KriaColor.ink : KriaColor.zinc.opacity(0.6))
                .frame(width: Self.buttonSize, height: Self.buttonSize)
                .background(style == .approve ? KriaColor.butter : KriaColor.paper,
                            in: RoundedRectangle(cornerRadius: Self.cornerRadius))
                .overlay {
                    if style == .outline {
                        RoundedRectangle(cornerRadius: Self.cornerRadius)
                            .stroke(KriaColor.line.opacity(enabled ? 1 : 0.5), lineWidth: 1)
                    }
                }
                .contentShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
    }
}

struct NativeEditorMenuRow<Content: View>: View {
    let title: String
    let value: String
    @ViewBuilder let content: () -> Content
    var body: some View {
        Menu(content: content) {
            HStack(spacing: 10) {
                Text(title)
                Spacer(minLength: 8)
                Text(value)
                Image(systemName: "chevron.down").font(.system(size: 10))
            }.padding(.horizontal, 12).frame(maxWidth: .infinity, minHeight: 44)
                .background(.white, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(KriaColor.line, lineWidth: 1))
        }.buttonStyle(.plain).accessibilityLabel(title).accessibilityValue(value)
    }
}

/// A small visual thumb with a full-height UIKit hit target and native VoiceOver adjustment.
struct NativePaperSlider: UIViewRepresentable {
    let session: NativeEditorSession
    @Binding var value: Double
    let bounds: ClosedRange<Double>
    var step: Double? = nil
    let label: String
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> UISlider {
        let slider = UISlider()
        slider.minimumTrackTintColor = UIColor(KriaColor.sky)
        slider.maximumTrackTintColor = UIColor(KriaColor.line).withAlphaComponent(0.65)
        let thumb = UIGraphicsImageRenderer(size: CGSize(width: 20, height: 20)).image { context in
            let circle = CGRect(x: 1, y: 1, width: 18, height: 18)
            UIColor.white.setFill(); context.cgContext.fillEllipse(in: circle)
            UIColor(KriaColor.line).setStroke(); context.cgContext.strokeEllipse(in: circle)
        }
        slider.setThumbImage(thumb, for: .normal)
        slider.addTarget(context.coordinator, action: #selector(Coordinator.begin), for: .touchDown)
        slider.addTarget(context.coordinator, action: #selector(Coordinator.changed(_:)), for: .valueChanged)
        slider.addTarget(context.coordinator, action: #selector(Coordinator.end), for: [.touchUpInside, .touchUpOutside, .touchCancel])
        return slider
    }
    func updateUIView(_ slider: UISlider, context: Context) {
        context.coordinator.parent = self
        slider.minimumValue = Float(bounds.lowerBound); slider.maximumValue = Float(bounds.upperBound)
        if !slider.isTracking { slider.value = Float(value) }
        slider.accessibilityLabel = label
        slider.isEnabled = context.environment.isEnabled
    }
    @MainActor final class Coordinator: NSObject {
        var parent: NativePaperSlider
        var editing = false
        init(_ parent: NativePaperSlider) { self.parent = parent }
        @objc func begin() { editing = true; parent.session.beginTransaction() }
        @objc func changed(_ sender: UISlider) {
            let single = !editing
            if single { parent.session.beginTransaction() }
            var next = Double(sender.value)
            if let step = parent.step { next = (next / step).rounded() * step }
            parent.value = min(parent.bounds.upperBound, max(parent.bounds.lowerBound, next))
            if single { parent.session.endTransaction() }
        }
        @objc func end() { editing = false; parent.session.endTransaction() }
    }
}
