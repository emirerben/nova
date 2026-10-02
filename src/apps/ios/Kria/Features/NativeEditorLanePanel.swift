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
                        .accessibilityLabel("Add visual")
                        .accessibilityIdentifier("native-editor-add-another-visual")
                    }
                    if let onDelete {
                        Button(role: .destructive, action: onDelete) {
                            Image(systemName: "trash").frame(minWidth: 44, minHeight: 44)
                        }
                        .accessibilityLabel("Remove visual")
                        .accessibilityIdentifier("native-editor-remove-visual")
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
    @ScaledMetric(relativeTo: .body) private var lineHeight: CGFloat = 22
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
    let onDone: () -> Void
    @State private var tab: Tab = .edit
    @State private var fieldFocused = false
    @State private var lineOpen = false
    @State private var loopingCueID: String?
    @State private var followSuspended = false
    @State private var removal: CaptionRemovalNotice?
    private var meta: [String: JSONValue] { session.document.captionMeta }
    private var appearance: [String: JSONValue] { meta["appearance"]?.objectValue ?? [:] }
    private var units: [EditorCaptionCue] { session.document.captionUnits }

    var body: some View {
        Group {
            if let id = editingCueID, let index = units.firstIndex(where: { $0.id == id }) {
                editBar(id: id, index: index)
            } else {
                NativeEditorLanePanel(title: "Captions", tabs: Tab.allCases, tab: $tab, onDone: {
                    leaveLine(to: nil); onDone()
                }) {
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
        .onChange(of: tab) { _, _ in leaveLine(to: nil) }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background, editingCueID != nil { leaveLine(to: nil) }
        }
        .onChange(of: loopToggleRequest) { _, _ in toggleLoop() }
        .onChange(of: session.isPlaying) { _, playing in
            if playing { followSuspended = false } else if loopingCueID != nil, !loopRestarting { loopingCueID = nil }
        }
        .onChange(of: session.currentTime) { _, time in continueLoop(at: time) }
        .onAppear {
            panelLifecycle?.register(owner: lifecycleOwner) { leaveLine(to: nil) }
        }
        .onDisappear {
            panelLifecycle?.unregister(owner: lifecycleOwner)
            if lineOpen { session.endCaptionLineEdit(); lineOpen = false }
        }
    }

    // MARK: Browse (keyboard down)

    private var playingID: String? {
        let time = session.currentTime
        return units.last { unit in
            guard let range = session.captionTimelineRange(id: unit.id) else { return false }
            return range.lowerBound <= time && time < range.upperBound
        }?.id
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
            .simultaneousGesture(DragGesture(minimumDistance: 12).onChanged { _ in followSuspended = true })
            .onChange(of: playingID) { _, id in
                // Follow the playing line (plan 026 D15): never while the creator is
                // scrolling, editing, or using VoiceOver.
                guard let id, session.isPlaying, !followSuspended, !voiceOverEnabled, editingCueID == nil else { return }
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(id, anchor: .center) }
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
                Text(String(index + 1)).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            .frame(width: 22, alignment: .leading)
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
        .accessibilityAction(named: "Delete") { if editable { remove(cue.id, lineNumber: index + 1) } }
        .accessibilityIdentifier("native-editor-caption-row-" + cue.id)
    }

    // MARK: Edit bar (keyboard up)

    private func editBar(id: String, index: Int) -> some View {
        let isLast = index == units.count - 1
        return CaptionEditBar(
            lineNumber: index + 1,
            lineCount: units.count,
            timeLabel: time(units[index].startS),
            lineHeight: lineHeight,
            lines: dynamicTypeSize.isAccessibilitySize || contentWidth < 300 ? 2 : 3,
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
            isLooping: loopingCueID == id,
            removal: removal,
            onUndoRemoval: undoRemoval,
            onRemovalExpired: { expired in if removal == expired { removal = nil } },
            onPrevious: { if index > 0 { leaveLine(to: units[index - 1].id) } },
            onNext: { leaveLine(to: isLast ? nil : units[index + 1].id) },
            onDone: { leaveLine(to: nil) },
            onLoop: toggleLoop
        )
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
                removal = CaptionRemovalNotice(lineNumber: (index ?? 0) + 1, undoDepth: session.undoHistoryCount)
            } else {
                session.endCaptionLineEdit()
            }
            lineOpen = false
        }
        if let destination, units.contains(where: { $0.id == destination }) {
            enter(destination)
        } else {
            fieldFocused = false
            editingCueID = nil
        }
    }

    private func remove(_ id: String, lineNumber: Int) {
        if editingCueID == id { leaveLine(to: nil) }
        session.deleteSelection(EditorSelection(kind: .captionCue, id: id))
        removal = CaptionRemovalNotice(lineNumber: lineNumber, undoDepth: session.undoHistoryCount)
    }

    private func undoRemoval() {
        guard let removal, session.undoHistoryCount == removal.undoDepth else { self.removal = nil; return }
        let open = editingCueID
        if lineOpen { session.endCaptionLineEdit(); lineOpen = false }
        session.undo()
        self.removal = nil
        if let open, units.contains(where: { $0.id == open }) {
            session.beginCaptionLineEdit(id: open); lineOpen = true
        }
    }

    private func removalNotice(_ notice: CaptionRemovalNotice) -> some View {
        CaptionRemovalNoticeView(notice: notice, canUndo: session.undoHistoryCount == notice.undoDepth,
                                 onUndo: undoRemoval, onExpire: { if removal == notice { removal = nil } })
    }

    // MARK: Loop-play (plan 026 D6)

    @State private var loopRestarting = false

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
        loopRestarting = true
        session.seek(to: range.lowerBound)
        if !session.isPlaying { session.togglePlayback() }
        loopRestarting = false
    }

    private func continueLoop(at time: TimeInterval) {
        guard let id = loopingCueID, session.isPlaying,
              let range = session.captionTimelineRange(id: id), time >= range.upperBound - 0.03 else { return }
        restartLoop(range)
    }

    private func spokenTime(_ base: Double) -> String {
        let seconds = Int(session.timelineProjection.projectBaseTime(base).rounded())
        return seconds >= 60 ? "\(seconds / 60) minutes \(seconds % 60) seconds" : "\(seconds) seconds"
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
    /// Undo-stack depth right after the removal; Undo is offered only while it still matches.
    let undoDepth: Int
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
                Button("Undo", action: onUndo)
                    .font(KriaFont.body(13).weight(.semibold))
                    .frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("native-editor-caption-undo-removal")
            }
        }
        .accessibilityElement(children: .combine)
        .task(id: notice.id) {
            CaptionLineAnnouncer.post("Line \(notice.lineNumber) removed")
            try? await Task.sleep(for: .seconds(4))
            onExpire()
        }
    }
}

/// VoiceOver announcements for the caption editor; tests swap `post` (XCUITest
/// can't observe announcements, plan 026 F15).
enum CaptionLineAnnouncer {
    @MainActor static var post: (String) -> Void = { message in
        UIAccessibility.post(notification: .announcement, argument: message)
    }
}

/// KRI-240 Variant A edit bar: replaces the caption list while a line is open.
/// Row 1: position, time, Previous / Next and Done. Row 2: the line field (wraps,
/// Return = Next) and loop-play.
struct CaptionEditBar: View {
    static let verticalPadding: CGFloat = 8
    static let navigationHeight: CGFloat = 44

    static func fieldHeight(lineHeight: CGFloat, lines: Int) -> CGFloat { CGFloat(lines) * lineHeight + 16 }
    static func height(lineHeight: CGFloat, lines: Int) -> CGFloat {
        verticalPadding * 2 + navigationHeight + fieldHeight(lineHeight: lineHeight, lines: lines)
    }

    let lineNumber: Int
    let lineCount: Int
    let timeLabel: String
    let lineHeight: CGFloat
    let lines: Int
    @Binding var text: String
    @Binding var focused: Bool
    let configuration: LineEditorConfiguration
    let lineID: String
    let canGoPrevious: Bool
    let canGoNext: Bool
    let isLooping: Bool
    let removal: CaptionRemovalNotice?
    let onUndoRemoval: () -> Void
    let onRemovalExpired: (CaptionRemovalNotice) -> Void
    let onPrevious: () -> Void
    let onNext: () -> Void
    let onDone: () -> Void
    let onLoop: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                if let removal {
                    CaptionRemovalNoticeView(notice: removal, canUndo: true, onUndo: onUndoRemoval,
                                             onExpire: { onRemovalExpired(removal) })
                } else {
                    Text("\(lineNumber) of \(lineCount) · \(timeLabel)")
                        .font(KriaFont.body(13)).monospacedDigit().foregroundStyle(KriaColor.mutedInk)
                        .lineLimit(1).minimumScaleFactor(0.7)
                        .accessibilityLabel("Line \(lineNumber) of \(lineCount)")
                        .accessibilityIdentifier("native-editor-caption-position")
                }
                Spacer(minLength: 4)
                navigationButton("chevron.left", label: "Previous line", id: "native-editor-caption-previous",
                                 enabled: canGoPrevious, action: onPrevious)
                navigationButton("chevron.right", label: "Next line", id: "native-editor-caption-next",
                                 enabled: canGoNext, action: onNext)
                Button(action: onDone) {
                    Text("Done").font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 16).frame(minHeight: 44)
                        .background(KriaColor.butter, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("native-editor-caption-edit-done")
            }
            .frame(height: Self.navigationHeight)
            HStack(alignment: .top, spacing: 8) {
                NativeExplicitLineTextEditor(
                    text: $text, focused: $focused, identifier: "native-editor-caption-field",
                    configuration: configuration,
                    actions: LineEditorActions(onReturn: canGoNext ? onNext : onDone, onNextLine: onNext,
                                               onPreviousLine: onPrevious, onEscape: onDone),
                    lineID: lineID
                )
                .frame(height: Self.fieldHeight(lineHeight: lineHeight, lines: lines))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.line, lineWidth: 1))
                Button(action: onLoop) {
                    Image(systemName: isLooping ? "pause.fill" : "play.fill")
                        .font(.system(size: 17)).foregroundStyle(KriaColor.plum)
                        .frame(width: 44, height: 44)
                        .background(KriaColor.lilac, in: RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(isLooping ? "Stop playing line" : "Play line")
                .accessibilityIdentifier("native-editor-caption-loop")
            }
        }
        .padding(.vertical, Self.verticalPadding)
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-caption-edit-bar")
    }

    private func navigationButton(_ symbol: String, label: String, id: String, enabled: Bool,
                                  action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 15, weight: .semibold))
                .foregroundStyle(enabled ? KriaColor.ink : KriaColor.zinc)
                .frame(width: 44, height: 44)
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.line, lineWidth: 1))
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
