import SwiftUI

/// KRI-1501: the translated caption line's own panel ("separate track"). Lines: switch, language, per-line text.
/// Style: colour, font, outline. Position: size and place for all translated lines or the one selected.
/// Timing is never edited here: a translated line always shows for its spoken cue.
struct NativeCaptionTranslationPanel: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.nativeEditorPanelContentWidth) private var contentWidth
    enum Tab: String, CaseIterable { case lines = "Lines", style = "Style", position = "Position" }
    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    @State private var tab: Tab = .lines
    @State private var stylesOneLine = false
    @FocusState private var focusedLine: String?

    private var track: EditorCaptionTranslation? { session.document.captionTranslation }
    private var cues: [EditorCaptionCue] {
        session.document.captionCues.filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
    private var stacksControls: Bool { dynamicTypeSize.isAccessibilitySize || contentWidth < 300 }

    var body: some View {
        NativeEditorLanePanel(title: "Translation", tabs: track == nil ? [.lines] : Tab.allCases, tab: $tab, onDone: {
            focusedLine = nil
            onDone()
        }, heading: String(localized: "Translated line", comment: "Panel title: the smaller translated caption line"),
           tabTitle: { tab in
               switch tab {
               case .lines: String(localized: "Lines", comment: "Translation panel tab: the translated lines")
               case .style: String(localized: "Style")
               case .position: String(localized: "Position")
               }
           }) {
            VStack(spacing: 10) {
                switch tab {
                case .lines: lines
                case .style: style
                case .position: position
                }
            }
            .disabled(!session.canEditCaptionTranslation)
        }
        .onChange(of: focusedLine) { old, new in
            // One undo step per line typed, like a caption line.
            if old != nil { session.endEditTransaction() }
            if let new {
                session.beginEditTransaction()
                session.select(EditorSelection(kind: .captionTranslation, id: new), seekToStart: false)
                if let park = session.captionTranslationParkTime(id: new) { session.seek(to: park) }
            }
        }
        .onDisappear { if focusedLine != nil { focusedLine = nil; session.endEditTransaction() } }
    }

    // MARK: Lines

    private var lines: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let track {
                VStack(spacing: 0) {
                    NativeEditorSwitchRow(
                        title: String(localized: "Show translated line"), id: "native-editor-translation-visible",
                        isOn: Binding(get: { track.enabled }, set: { session.setCaptionTranslationEnabled($0) })
                    )
                }
                .padding(.horizontal, 12)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
            } else {
                Text("Add a smaller line under your captions in a second language. Pick the language:")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            languagePicker
            statusLine
            if track != nil {
                ForEach(Array(cues.enumerated()), id: \.element.id) { index, cue in
                    lineRow(cue, number: index + 1)
                }
            }
        }
    }

    private struct Language: Hashable, RawRepresentable { let rawValue: String }

    private var languagePicker: some View {
        let languages = session.captionTranslationLanguages.map(Language.init(rawValue:))
        let current = Language(rawValue: track?.language ?? "")
        let layout = stacksControls ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 12))
        return VStack(alignment: .leading, spacing: 6) {
            layout {
                Text("Language").frame(width: stacksControls ? nil : 74, alignment: .leading)
                NativeEditorPanelTabs(
                    tabs: languages,
                    selection: Binding(get: { current }, set: { picked in
                        guard picked != current || track == nil else { return }
                        Task { await session.translateCaptions(to: picked.rawValue) }
                    }),
                    accessibilityPrefix: "native-editor-translation-language",
                    identifier: { "native-editor-translation-language-" + $0.rawValue },
                    isEnabled: { _ in !isTranslating },
                    title: { NativeEditorSession.captionLanguageName($0.rawValue) }
                )
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            if let spoken = session.spokenCaptionLanguage {
                Text("Captions are in \(NativeEditorSession.captionLanguageName(spoken)).")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
        }
    }

    private var isTranslating: Bool {
        if case .translating = session.captionTranslationStatus { return true }
        return false
    }

    @ViewBuilder private var statusLine: some View {
        switch session.captionTranslationStatus {
        case .idle:
            EmptyView()
        case .translating(let language):
            HStack(spacing: 8) {
                ProgressView()
                Text("Translating into \(NativeEditorSession.captionLanguageName(language))…")
            }
            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("native-editor-translation-status")
        case .finished(_, let dropped) where dropped > 0:
            Text("\(dropped) lines couldn’t be translated, so they show no translated line.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("native-editor-translation-status")
        case .finished:
            EmptyView()
        case .failed(let language, let reason):
            Text(failureMessage(language: NativeEditorSession.captionLanguageName(language), reason: reason))
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("native-editor-translation-status")
        }
    }

    private func failureMessage(language: String, reason: CaptionTranslationFailure) -> String {
        switch reason {
        case .unmet: String(localized: "Couldn’t translate the captions into \(language). Nothing changed. Try again.")
        case .updateRequired: String(localized: "Update Kria to add a translated line.")
        case .disabled: String(localized: "Translated lines aren’t available right now. Nothing changed.")
        case .sameLanguage: String(localized: "Your captions are already in \(language). Pick another language.")
        case .noCaptions: String(localized: "There are no captions to translate yet.")
        case .tooLong: String(localized: "Too many lines to translate at once. Nothing changed.")
        case .rateLimited: String(localized: "Too many translations in a row. Try again in a minute.")
        case .offline: String(localized: "You’re offline. Connect and try again.")
        }
    }

    private func lineRow(_ cue: EditorCaptionCue, number: Int) -> some View {
        let selected = session.selection == EditorSelection(kind: .captionTranslation, id: cue.id)
        return HStack(alignment: .top, spacing: 10) {
            Text(String(number)).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .frame(minWidth: 22, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(cue.text).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                TextField(String(localized: "No translated line"), text: Binding(
                    get: { cue.raw["translation"]?.stringValue ?? "" },
                    set: { session.setCaptionTranslationText(id: cue.id, text: $0) }
                ), axis: .vertical)
                .font(KriaFont.body(16))
                .foregroundStyle(KriaColor.ink)
                .focused($focusedLine, equals: cue.id)
                .submitLabel(.done)
                .accessibilityLabel(String(localized: "Translated line \(number)"))
                .accessibilityIdentifier("native-editor-translation-line-" + cue.id)
                if session.captionTranslationIsStale(id: cue.id) {
                    HStack(spacing: 8) {
                        Text("The caption changed. This translation may be out of date.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Button {
                            Task { await session.retranslateCaption(id: cue.id) }
                        } label: {
                            Text("Re-translate").font(KriaFont.body(13).weight(.semibold))
                                .frame(minHeight: 44).contentShape(Rectangle())
                        }
                        .disabled(isTranslating)
                        .accessibilityIdentifier("native-editor-translation-retranslate-" + cue.id)
                    }
                }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8).frame(minHeight: 59)
        .background(selected ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
        .contentShape(Rectangle())
        .onTapGesture {
            session.select(EditorSelection(kind: .captionTranslation, id: cue.id), seekToStart: false)
            if let park = session.captionTranslationParkTime(id: cue.id) { session.seek(to: park) }
        }
    }

    // MARK: Style

    private var style: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Color")
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 44), spacing: 8)], spacing: 8) {
                    ForEach(CaptionTranslationLook.swatches, id: \.self) { hex in
                        Button { session.setCaptionTranslationColor(hex == CaptionTranslationLook.defaultColor ? nil : hex) } label: {
                            Circle().fill(nativeEditorColor(hex)).frame(width: 30, height: 30)
                                .overlay(Circle().stroke(currentColor == hex ? KriaColor.sky : .clear, lineWidth: 2))
                                .frame(minWidth: 44, minHeight: 44)
                        }
                        .accessibilityLabel(String(localized: "Translated line color \(hex)"))
                        .accessibilityAddTraits(currentColor == hex ? .isSelected : [])
                        .accessibilityIdentifier("native-editor-translation-color-" + hex.dropFirst())
                    }
                    ColorPicker(String(localized: "Custom translated line color"), selection: Binding(
                        get: { nativeEditorColor(currentColor) },
                        set: { session.setCaptionTranslationColor(nativeEditorHex($0)) }
                    ), supportsOpacity: false)
                    .labelsHidden().frame(minWidth: 44, minHeight: 44)
                }
            }
            HStack(spacing: 12) {
                Text("Font").frame(width: 66, alignment: .leading)
                NativeFontPicker(
                    selection: track?.font,
                    includeDefault: true,
                    defaultLabel: String(localized: "Same as captions"),
                    accessibilityLabelText: String(localized: "Translated line font"),
                    accessibilityID: "native-editor-translation-font"
                ) { session.setCaptionTranslationFont($0) }
            }
            HStack(spacing: 12) {
                Text(String(localized: "Outline", comment: "Caption text outline")).frame(width: 66, alignment: .leading)
                NativePaperSlider(session: session, value: Binding(
                    get: { track?.strokeWidth ?? CaptionTranslationLook.defaultStroke(spoken: spokenStroke) },
                    set: { session.setCaptionTranslationStroke($0) }
                ), bounds: 0...12, step: 1, label: String(localized: "Translated line outline"))
                Text("\(Int(track?.strokeWidth ?? CaptionTranslationLook.defaultStroke(spoken: spokenStroke))) px")
                    .monospacedDigit().frame(width: 44, alignment: .trailing)
            }
            .frame(minHeight: 44)
        }
    }

    private var currentColor: String { track?.color ?? CaptionTranslationLook.defaultColor }
    private var spokenStroke: Double { session.document.captionMeta["stroke_width"]?.numberValue ?? 4 }

    // MARK: Position

    private struct Scope: Hashable, RawRepresentable { let rawValue: String }

    /// The translated line last selected (on the preview, the timeline or in Lines).
    private var selectedLine: (id: String, number: Int)? {
        guard let selection = session.selection, selection.kind == .captionTranslation,
              let index = cues.firstIndex(where: { $0.id == selection.id }) else { return nil }
        return (selection.id, index + 1)
    }

    private var scope: NativeEditorSession.CaptionStyleScope {
        if stylesOneLine, let line = selectedLine { return .line(line.id) }
        return .all
    }

    private var position: some View {
        let all = Scope(rawValue: "all"), one = Scope(rawValue: "line")
        let line = selectedLine
        return VStack(alignment: .leading, spacing: 10) {
            NativeEditorPanelTabs(
                tabs: [all, one],
                selection: Binding(get: { scope == .all ? all : one }, set: { stylesOneLine = $0 == one }),
                accessibilityPrefix: "native-editor-translation-scope",
                identifier: { $0 == all ? "native-editor-translation-scope-all" : "native-editor-translation-scope-line" },
                isEnabled: { $0 == all || line != nil },
                title: { $0 == all ? String(localized: "All lines") : (line.map { String(localized: "Line \($0.number)") } ?? String(localized: "One line")) }
            )
            Text(line == nil ? String(localized: "To change one line, tap it on the video or in Lines first.")
                 : scope == .all ? String(localized: "Changes every translated line.") : String(localized: "Only this line changes."))
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            HStack {
                Text("Size").frame(width: 66, alignment: .leading)
                NativePaperSlider(session: session, value: Binding(
                    get: { session.captionTranslationSize(scope) },
                    set: { session.setCaptionTranslationSize(scope, size: $0) }
                ), bounds: CaptionTranslationLook.sizeRange, step: 1, label: String(localized: "Translated line size in output pixels"))
                Text("\(Int(session.captionTranslationSize(scope))) px").monospacedDigit().frame(width: 62)
            }
            .frame(minHeight: 44)
            Text("Position")
            let attached = session.captionTranslationIsAttached(scope)
            Button { session.placeCaptionTranslation(scope, y: nil) } label: {
                Label(String(localized: "Under the captions"), systemImage: attached ? "checkmark" : "text.below.photo")
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(attached ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(attached ? .isSelected : [])
            .accessibilityIdentifier("native-editor-translation-position-attached")
            HStack {
                ForEach([(String(localized: "Top", comment: "Caption position"), 0.15, "top"),
                         (String(localized: "Middle", comment: "Caption position"), 0.5, "middle"),
                         (String(localized: "Bottom", comment: "Caption position"), 0.85, "bottom")], id: \.2) { title, y, key in
                    Button(title) { session.placeCaptionTranslation(scope, y: y) }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                        .accessibilityIdentifier("native-editor-translation-position-" + key)
                }
            }
            Text("You can also drag or pinch a translated line on the video.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
        }
    }
}
