import SwiftUI
import UIKit

struct NativeEditorTextPanel: View {
    enum Tab: String, CaseIterable { case edit = "Edit text", style = "Style", animation = "Animation" }
    enum Phase: String, CaseIterable { case entrance = "In", exit = "Out", loop = "Loop" }
    let id: String
    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    let embeddedAnimation: Bool
    @State private var tab: Tab = .style
    @State private var phase: Phase = .entrance
    @State private var typing = false
    @FocusState private var editingSize: Bool
    @State private var sizeInput = ""
    @State private var ownerUUID = UUID()
    @StateObject private var previewClock = NativeTextAnimationPreviewClock()
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.nativeEditorConnectedPanel) private var isConnectedPanel
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @Environment(\.nativeEditorPanelContentWidth) private var panelContentWidth
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var editorHeight: CGFloat = 100

    init(id: String, session: NativeEditorSession, initialTab: Tab = .style, embeddedAnimation: Bool = false, onDone: @escaping () -> Void) {
        self.id = id; self.session = session; self.onDone = onDone
        self.embeddedAnimation = embeddedAnimation
        _tab = State(initialValue: initialTab)
    }

    private var item: EditorTextElement? { session.document.textElements.first { $0.id == id } }
    private let palette = ["#FFFFFF", "#30352C", "#FFF0A6", "#9BCAFF", "#E7DDF5"]
    private var usesAccessibilityLayout: Bool { dynamicTypeSize.isAccessibilitySize || panelContentWidth < 300 }

    @ViewBuilder var body: some View {
        if embeddedAnimation {
            animationControls
                .disabled(!session.canEdit(.text))
                .onDisappear { stopPreview(); session.endTransaction() }
        } else {
            editorBody
        }
    }

    private var editorBody: some View {
        VStack(spacing: 6) {
            VStack(spacing: 6) {
                HStack {
                    Text("Text").font(KriaFont.body(isConnectedPanel ? 18 : 15).weight(.semibold))
                    Spacer()
                    if session.textDeletion(id: id).isAllowed {
                        Button {
                            performOutgoingCleanup()
                            if session.deleteText(id: id) { onDone() }
                        } label: {
                            Label("Delete", systemImage: "trash")
                                .font(KriaFont.body(14).weight(.semibold))
                                .foregroundStyle(KriaColor.failureText)
                                .frame(minWidth: 64, minHeight: 44)
                        }
                        .accessibilityLabel("Delete text")
                        .accessibilityIdentifier("native-editor-text-delete-panel")
                    }
                    Button { performOutgoingCleanup(); onDone() } label: {
                        Text("Done").frame(minWidth: 64, minHeight: 44)
                            .background(isConnectedPanel ? KriaColor.ink.opacity(0.06) : Color.clear, in: Capsule())
                    }
                        .accessibilityIdentifier("native-editor-text-inspector-done")
                }
                NativeEditorPanelTabs(tabs: Tab.allCases, selection: $tab, accessibilityPrefix: "native-editor-text-tabs")
                    .accessibilityIdentifier("native-editor-text-tabs")
            }
            .nativeEditorPanelResizeSurface()
            ScrollView {
                VStack(spacing: 8) {
                    switch tab {
                    case .edit:
                        NativeExplicitLineTextEditor(text: Binding(
                            get: { item?.text ?? "" }, set: { session.updateTextContent(id: id, content: $0) }
                        ), focused: $typing, identifier: "native-editor-text-content")
                        .frame(minHeight: editorHeight)
                        .padding(8)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                        if usesAccessibilityLayout {
                            VStack(spacing: 8) {
                                timingField("Start", isStart: true)
                                timingField("End", isStart: false)
                            }
                        } else {
                            HStack {
                                timingField("Start", isStart: true)
                                timingField("End", isStart: false)
                            }
                        }
                    case .style: styleControls
                    case .animation: animationControls
                    }
                }.padding(.top, 8)
            }
            .accessibilityIdentifier("native-editor-text-inspector-scroll")
            .scrollDismissesKeyboard(.interactively)
            .disabled(!session.canEdit(.text))
        }
        .padding(.horizontal, isConnectedPanel ? 24 : 16)
        .padding(.bottom, 8)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(isConnectedPanel ? Color.clear : KriaColor.paper)
        .overlay(alignment: .top) {
            if !isConnectedPanel { KriaColor.line.opacity(0.4).frame(height: 1) }
        }
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .onChange(of: tab) { _, tab in
            commitSize(); editingSize = false; typing = tab == .edit
            if tab == .edit, let item {
                session.beginTransaction()
                session.updateTextContent(id: id, content: item.text)
            }
        }
        .onChange(of: editingSize) { _, focused in
            if focused { sizeInput = sizeLabel } else { commitSize() }
        }
        .onChange(of: typing) { _, value in
            if value { session.beginTransaction() } else { session.endTransaction() }
        }
        .onAppear {
            guard !embeddedAnimation else { return }
            panelLifecycle?.register(owner: ownerUUID, prepareToClose: {
                performOutgoingCleanup()
            })
        }
        .onDisappear {
            performOutgoingCleanup()
            panelLifecycle?.unregister(owner: ownerUUID)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-text-panel")
    }

    private func timingField(_ title: String, isStart: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title + " (seconds)").font(KriaFont.body(12))
            TextField(title, value: Binding(
                get: {
                    guard let item else { return 0 }
                    return session.timelineProjection.projectBaseTime(isStart ? item.startS : item.endS)
                },
                set: { value in
                    let base = session.timelineProjection.unprojectOutputTime(value)
                    session.updateTextTiming(id: id, startS: isStart ? base : nil, endS: isStart ? nil : base)
                }
            ), format: .number.precision(.fractionLength(0...2)))
            .keyboardType(.decimalPad)
            .padding(10)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
            .accessibilityIdentifier("native-editor-text-time-" + (isStart ? "start" : "end"))
        }
    }

    private var styleControls: some View {
        VStack(spacing: 4) {
            VStack(alignment: .leading, spacing: 6) {
                Menu {
                    ForEach(["Simple", "Bold", "Highlight"], id: \.self) { preset in
                        Button {
                            session.applyTextPreset(id: id, preset: preset)
                        } label: {
                            if string("editor_preset", "Simple") == preset {
                                Label(preset, systemImage: "checkmark")
                            } else {
                                Text(preset)
                            }
                        }
                    }
                } label: {
                    HStack(spacing: 12) {
                        Text(string("editor_preset", "Simple"))
                            .font(KriaFont.body(14))
                        Image(systemName: "chevron.down")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .padding(.horizontal, 12)
                    .frame(minWidth: 112, minHeight: 44, alignment: .leading)
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.line))
                }
                .accessibilityIdentifier("native-editor-text-preset")
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if usesAccessibilityLayout {
                VStack(spacing: 8) {
                    fontMenu(expanded: true)
                    alignmentControl(expanded: true)
                }
            } else {
                HStack(spacing: 10) {
                    fontMenu(expanded: false)
                    alignmentControl(expanded: false)
                }
            }
            sizeControl
            colorControl
            propertyRow("Outline", key: "stroke_width", range: 0...20, unit: "px", colorKey: "stroke_color")
            propertyRow("Shadow", key: "shadow_opacity", range: 0...1, unit: "%", colorKey: "shadow_color")
            positionControl("Horizontal position", key: "x_frac", fallback: 0.5)
            positionControl("Vertical position", key: "y_frac", fallback: defaultVerticalPosition)
            Stepper(value: Binding(
                get: { item?.raw["rotation_deg"]?.numberValue ?? 0 },
                set: { session.updateTextRaw(id: id, key: "rotation_deg", value: .number($0)) }
            ), in: -360...360, step: 5) {
                Text("Rotation \(Int(item?.raw["rotation_deg"]?.numberValue ?? 0))°")
            }
            .frame(minHeight: 44)
            .accessibilityLabel("Text rotation")
            .accessibilityValue("\(Int(item?.raw["rotation_deg"]?.numberValue ?? 0)) degrees")
            .accessibilityIdentifier("native-editor-text-rotation")
        }
    }

    private func fontMenu(expanded: Bool) -> some View {
        NativeFontPicker(
            selection: string("font_family", "Inter"),
            accessibilityID: "native-editor-text-font"
        ) { if let family = $0 { session.setTextStyle(id: id, style: family) } }
        .frame(maxWidth: expanded ? .infinity : nil)
    }

    private func alignmentControl(expanded: Bool) -> some View {
        HStack(spacing: 0) {
            ForEach(["left", "center", "right"], id: \.self) { alignment in
                Button { session.setTextAlignment(id: id, alignment: alignment) } label: {
                    Image(systemName: "text.align\(alignment)")
                        .frame(minWidth: 44, maxWidth: expanded ? .infinity : nil, minHeight: 44)
                        .background(string("alignment", "center") == alignment ? KriaColor.selectionSoft : KriaColor.softZinc)
                }
                .accessibilityLabel("Align text \(alignment)")
                .accessibilityAddTraits(string("alignment", "center") == alignment ? .isSelected : [])
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder private var sizeControl: some View {
        if usesAccessibilityLayout {
            VStack(alignment: .leading, spacing: 4) {
                Text("Size")
                HStack(spacing: 12) { sizeButtons }
            }
        } else {
            HStack(spacing: 12) {
                Text("Size")
                Spacer()
                sizeButtons
            }
        }
    }

    private var sizeButtons: some View {
        Group {
            Button {
                commitSize(); editingSize = false
                session.setTextSize(id: id, sizePX: max(8, currentSize - 4))
            } label: { Image(systemName: "minus").frame(minWidth: 44, minHeight: 44) }
            .accessibilityLabel("Decrease text size")
            TextField("Size", text: Binding(
                get: { editingSize ? sizeInput : sizeLabel },
                set: { sizeInput = $0 }
            ))
            .focused($editingSize)
            .keyboardType(.decimalPad)
            .multilineTextAlignment(.center)
            .monospacedDigit()
            .frame(minWidth: 76, minHeight: 44)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
            .accessibilityLabel("Text size")
            .accessibilityIdentifier("native-editor-text-size")
            Button {
                commitSize(); editingSize = false
                session.setTextSize(id: id, sizePX: currentSize + 4)
            } label: { Image(systemName: "plus").frame(minWidth: 44, minHeight: 44) }
            .accessibilityLabel("Increase text size")
        }
    }

    private var colorControl: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Color")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 44), spacing: 8)], spacing: 8) {
                colorChoices
            }
        }
    }

    private var colorChoices: some View {
        Group {
            ForEach(palette, id: \.self) { hex in
                Button { session.setTextColor(id: id, color: hex) } label: {
                    Circle().fill(nativeEditorColor(hex))
                        .overlay(Circle().stroke(string("color", "#FFFFFF") == hex ? KriaColor.sky : KriaColor.line, lineWidth: 2))
                        .frame(width: 28, height: 28).frame(minWidth: 44, minHeight: 44)
                }.accessibilityLabel("Text color \(hex)")
            }
            ColorPicker("Custom color", selection: colorBinding("color", fallback: "#FFFFFF"), supportsOpacity: false)
                .labelsHidden().frame(minWidth: 44, minHeight: 44).accessibilityLabel("Custom text color")
        }
    }

    private var defaultVerticalPosition: Double {
        switch string("position", "center") {
        case "top": 0.2
        case "bottom": 0.8
        default: 0.5
        }
    }

    private func positionControl(_ title: String, key: String, fallback: Double) -> some View {
        let value: Double = item?.raw[key]?.numberValue ?? fallback
        let percent: Int = Int((value * 100).rounded())
        let label: String = "\(title) \(percent)%"
        let axis: String = key == "x_frac" ? "x" : "y"
        let position: Binding<Double> = Binding<Double>(
            get: { item?.raw[key]?.numberValue ?? fallback },
            set: { (next: Double) in
                setPositionCoordinate(next, for: key)
            }
        )
        return Stepper(value: position, in: 0.0...1.0, step: 0.05) {
            Text(label)
        }
        .frame(minHeight: 44)
        .accessibilityLabel("Text \(title.lowercased())")
        .accessibilityValue("\(percent) percent")
        .accessibilityIdentifier("native-editor-text-position-" + axis)
    }

    private func setPositionCoordinate(_ next: Double, for key: String) {
        let x: Double = key == "x_frac" ? next : (item?.raw["x_frac"]?.numberValue ?? 0.5)
        let y: Double = key == "y_frac" ? next : (item?.raw["y_frac"]?.numberValue ?? defaultVerticalPosition)
        session.setTextPosition(id: id, x: x, y: y)
    }

    private var animationControls: some View {
        VStack(spacing: 14) {
            NativeEditorPanelTabs(tabs: Phase.allCases, selection: $phase, accessibilityPrefix: "native-editor-text-animation-phase")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: usesAccessibilityLayout ? 118 : 92), spacing: 8)], spacing: 10) {
                animationEffects
            }
            Button {
                previewClock.toggleManually()
            } label: {
                Label(previewClock.isPlaying ? "Pause previews" : "Play previews", systemImage: previewClock.isPlaying ? "pause.fill" : "play.fill")
                    .font(KriaFont.body(13).weight(.semibold))
                    .frame(minHeight: 44)
            }
            .accessibilityIdentifier("native-editor-text-animation-preview-toggle")
            if usesAccessibilityLayout {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Speed")
                    animationSpeedControl
                }
            } else {
                HStack {
                    Text("Speed").frame(width: 68, alignment: .leading)
                    animationSpeedControl
                }
            }
            Text("Changes motion speed. Text timing stays the same.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { startPreviewIfAllowed() }
        .onDisappear { previewClock.pauseForLifecycle() }
        .onChange(of: scenePhase) { _, next in
            if next == .active { startPreviewIfAllowed() } else { previewClock.pauseForLifecycle() }
        }
        .onChange(of: reduceMotion) { _, enabled in
            if enabled { previewClock.pauseForLifecycle() } else { startPreviewIfAllowed() }
        }
    }

    private var animationEffects: some View {
        ForEach(phase == .loop ? ["None", "Pulse", "Bounce", "Float"] : ["None", "Fade", "Pop", "Slide", "Typewriter"], id: \.self) { effect in
            Button { session.setTextPhase(id: id, phase: phaseKey, effect: effect.lowercased()) } label: {
                VStack(spacing: 6) {
                    NativeTextAnimationPreview(phase: phaseKey, effect: effect.lowercased(), speed: phases["speed"]?.numberValue ?? 1, clock: previewClock)
                        .frame(maxWidth: .infinity, minHeight: 52)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 9))
                        .overlay(RoundedRectangle(cornerRadius: 9).stroke(selectedPhase == effect.lowercased() ? KriaColor.sky : .clear, lineWidth: 2))
                    Text(effect).font(KriaFont.body(11)).lineLimit(1).minimumScaleFactor(0.8)
                }
            }
            .accessibilityLabel("\(phase.rawValue) animation \(effect)")
            .accessibilityAddTraits(selectedPhase == effect.lowercased() ? .isSelected : [])
        }
    }

    private var animationSpeedControl: some View {
        HStack {
            NativeEditorSlider(session: session, value: Binding(
                get: { phases["speed"]?.numberValue ?? 1 },
                set: { session.setTextAnimationSpeed(id: id, speed: $0) }
            ), in: 0.25...3, step: 0.25) { Text("Animation speed") }
            Text("\((phases["speed"]?.numberValue ?? 1).formatted())×").frame(minWidth: 42)
        }
    }

    private var sizeLabel: String { currentSize.formatted(.number.grouping(.never).precision(.fractionLength(0...1))) }
    private func performOutgoingCleanup() {
        commitSize()
        editingSize = false
        typing = false
        stopPreview()
        session.endTransaction()
    }

    private var previewAutoplayAllowed: Bool {
        NativeTextAnimationPreviewAutoplay.allows(
            reduceMotion: reduceMotion,
            environment: ProcessInfo.processInfo.environment
        )
    }

    private func startPreviewIfAllowed() {
        guard previewAutoplayAllowed else { return }
        previewClock.startAutoplay()
    }

    private func stopPreview() {
        previewClock.pauseForLifecycle()
    }
    private func commitSize() {
        guard !sizeInput.isEmpty else { return }
        defer { sizeInput = "" }
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        if let size = formatter.number(from: sizeInput)?.doubleValue {
            session.setTextSize(id: id, sizePX: size)
        }
    }
    private var currentSize: Double { item.map(NativeEditorSession.textSize) ?? 72 }

    private var phaseKey: String { phase == .entrance ? "entrance" : phase == .exit ? "exit" : "loop" }
    private var phases: [String: JSONValue] {
        item.map(NativeEditorSession.textPhases) ?? [:]
    }
    private var selectedPhase: String { phases[phaseKey]?.stringValue ?? "none" }
    private func string(_ key: String, _ fallback: String) -> String { item?.raw[key]?.stringValue ?? fallback }
    private func colorBinding(_ key: String, fallback: String) -> Binding<Color> {
        Binding(get: { nativeEditorColor(string(key, fallback)) }, set: {
            session.updateTextRaw(id: id, key: key, value: .string(nativeEditorHex($0)))
        })
    }
    private func propertyRow(_ title: String, key: String, range: ClosedRange<Double>, unit: String, colorKey: String) -> some View {
        Group {
            if usesAccessibilityLayout {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                    propertyControls(title, key: key, range: range, unit: unit, colorKey: colorKey)
                }
            } else {
                HStack(spacing: 12) {
                    Text(title).frame(width: 66, alignment: .leading)
                    propertyControls(title, key: key, range: range, unit: unit, colorKey: colorKey)
                }
            }
        }
        .frame(minHeight: 44)
    }

    private func propertyControls(_ title: String, key: String, range: ClosedRange<Double>, unit: String, colorKey: String) -> some View {
        HStack(spacing: 12) {
            ColorPicker(title, selection: colorBinding(colorKey, fallback: "#18181B"), supportsOpacity: false)
                .labelsHidden().frame(minWidth: 44, minHeight: 44)
            NativeEditorSlider(session: session, value: Binding(
                get: { item?.raw[key]?.numberValue ?? 0 },
                set: {
                    session.updateTextRaw(id: id, key: key, value: .number($0))
                    if key == "shadow_opacity" { session.setTextShadow(id: id, enabled: $0 > 0) }
                }
            ), in: range) { Text(title) }
            Text("\(Int((item?.raw[key]?.numberValue ?? 0) * (unit == "%" ? 100 : 1)))\(unit)")
                .monospacedDigit().frame(minWidth: 44, alignment: .trailing)
        }
    }
}

func nativeEditorColor(_ hex: String) -> Color {
    let value = UInt32(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0xFFFFFF
    return Color(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
}

func nativeEditorHex(_ color: Color) -> String {
    var red: CGFloat = 0; var green: CGFloat = 0; var blue: CGFloat = 0; var alpha: CGFloat = 0
    UIColor(color).getRed(&red, green: &green, blue: &blue, alpha: &alpha)
    return String(format: "#%02X%02X%02X", Int((red * 255).rounded()), Int((green * 255).rounded()), Int((blue * 255).rounded()))
}

/// Input behaviour for `NativeExplicitLineTextEditor`. The default value is the Text
/// tool's long-standing editor (explicit lines, UIKit keyboard defaults); KRI-240's
/// caption line editor opts into wrapping, Return = Next and transcript-safe input.
struct LineEditorConfiguration: Equatable {
    var wrapsLines = false
    var returnKeyType: UIReturnKeyType = .default
    /// Return calls `LineEditorActions.onReturn` instead of inserting a newline.
    var interceptsReturn = false
    var autocorrectionType: UITextAutocorrectionType = .default
    var spellCheckingType: UITextSpellCheckingType = .default
    var autocapitalizationType: UITextAutocapitalizationType = .sentences
    var smartQuotesType: UITextSmartQuotesType = .default
    var smartDashesType: UITextSmartDashesType = .default
    /// BCP-47 language whose installed keyboard is requested (`textInputMode`).
    var preferredLanguage: String? = nil
    /// Pasted newlines become spaces (a caption is one line of speech).
    var flattensNewlines = false
    var usesKriaBodyFont = false
    var accessibilityLabel = "Text"

    /// KRI-240 caption line: wraps, Return moves to the next line (Done on the last),
    /// never auto-replaces words, and asks for a keyboard in the caption language.
    /// Spell-check underlines only make sense on a keyboard in that language, so
    /// they are turned off when no matching keyboard is installed (plan 025 D11).
    @MainActor static func captionLine(language: String?, isLast: Bool,
                            installedLanguages: [String] = LineEditorConfiguration.installedKeyboardLanguages()) -> LineEditorConfiguration {
        var config = LineEditorConfiguration()
        config.wrapsLines = true
        config.returnKeyType = isLast ? .done : .next
        config.interceptsReturn = true
        config.autocorrectionType = .no
        if let language, !installedLanguages.contains(where: { Self.language($0, matches: language) }) {
            config.spellCheckingType = .no
        }
        config.smartQuotesType = .no
        config.smartDashesType = .no
        config.preferredLanguage = language
        config.flattensNewlines = true
        config.usesKriaBodyFont = true
        config.accessibilityLabel = "Caption"
        return config
    }

    @MainActor static func installedKeyboardLanguages() -> [String] {
        UITextInputMode.activeInputModes.compactMap { $0.primaryLanguage }
    }

    /// "tr" matches "tr-TR" / "tr_TR"; only the language subtag is compared.
    static func language(_ candidate: String, matches language: String) -> Bool {
        func subtag(_ value: String) -> String {
            String(value.lowercased().split(whereSeparator: { $0 == "-" || $0 == "_" }).first ?? "")
        }
        let wanted = subtag(language)
        return !wanted.isEmpty && subtag(candidate) == wanted
    }

    @MainActor func apply(to view: ExplicitLineTextView) {
        view.wrapsLines = wrapsLines
        view.textContainer.widthTracksTextView = wrapsLines
        view.returnKeyType = returnKeyType
        view.autocorrectionType = autocorrectionType
        view.spellCheckingType = spellCheckingType
        view.autocapitalizationType = autocapitalizationType
        view.smartQuotesType = smartQuotesType
        view.smartDashesType = smartDashesType
        view.preferredLanguage = preferredLanguage
        view.accessibilityLabel = accessibilityLabel
        if usesKriaBodyFont {
            let base = UIFont(name: "Inter-Regular", size: 17) ?? .systemFont(ofSize: 17)
            view.font = UIFontMetrics(forTextStyle: .body).scaledFont(for: base)
        }
    }
}

/// Return and hardware-keyboard handlers for a line editor (KRI-240, plan 025 D14).
struct LineEditorActions {
    var onReturn: (() -> Void)? = nil
    var onNextLine: (() -> Void)? = nil
    var onPreviousLine: (() -> Void)? = nil
    var onEscape: (() -> Void)? = nil
}

/// The editor and canvas share explicit line breaks. Long lines scroll sideways.
/// (`LineEditorConfiguration.captionLine` switches to wrapping for caption lines.)
struct NativeExplicitLineTextEditor: UIViewRepresentable {
    @Binding var text: String
    var focused: Binding<Bool>
    let identifier: String
    var configuration = LineEditorConfiguration()
    var actions = LineEditorActions()
    /// Changing this re-arms the editor for a different line: its undo history is
    /// cleared (so shake-to-undo can't edit the previous line) and the caret moves to the end.
    var lineID: String? = nil

    func makeUIView(context: Context) -> ExplicitLineTextView {
        let view = ExplicitLineTextView()
        view.delegate = context.coordinator
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = UIColor(KriaColor.paper)
        view.textColor = UIColor(KriaColor.ink)
        view.keyboardAppearance = .light
        view.returnKeyType = .default
        view.textContainer.widthTracksTextView = false
        view.textContainer.heightTracksTextView = false
        view.accessibilityLabel = "Text"
        view.accessibilityIdentifier = identifier
        configuration.apply(to: view)
        view.appliedConfiguration = configuration
        view.lineID = lineID
        return view
    }

    func updateUIView(_ view: ExplicitLineTextView, context: Context) {
        context.coordinator.parent = self
        view.lineActions = actions
        if view.appliedConfiguration != configuration {
            let inputChanged = view.appliedConfiguration.returnKeyType != configuration.returnKeyType
                || view.appliedConfiguration.preferredLanguage != configuration.preferredLanguage
            configuration.apply(to: view)
            view.appliedConfiguration = configuration
            if inputChanged && view.isFirstResponder { view.reloadInputViews() }
        }
        if view.text != text { view.text = text }
        if view.lineID != lineID {
            view.lineID = lineID
            view.undoManager?.removeAllActions()
            let end = (view.text ?? "").utf16.count
            view.selectedRange = NSRange(location: end, length: 0)
        }
        view.updateLineWidth()
        view.wantsKeyboardFocus = focused.wrappedValue
        if focused.wrappedValue && view.window != nil && !view.isFirstResponder { view.becomeFirstResponder() }
        if !focused.wrappedValue && view.isFirstResponder { view.resignFirstResponder() }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: NativeExplicitLineTextEditor
        init(_ parent: NativeExplicitLineTextEditor) { self.parent = parent }
        func textViewDidChange(_ textView: UITextView) {
            (textView as? ExplicitLineTextView)?.updateLineWidth()
            parent.text = textView.text
        }
        func textViewDidBeginEditing(_ textView: UITextView) { parent.focused.wrappedValue = true }
        func textViewDidEndEditing(_ textView: UITextView) { parent.focused.wrappedValue = false }
        func textView(_ textView: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
            let config = parent.configuration
            if let view = textView as? ExplicitLineTextView, view.allowsNextNewline {
                view.allowsNextNewline = false
                return true
            }
            if config.interceptsReturn, text == "\n" {
                parent.actions.onReturn?()
                return false
            }
            if config.flattensNewlines, text.contains(where: \.isNewline),
               let start = textView.position(from: textView.beginningOfDocument, offset: range.location),
               let end = textView.position(from: start, offset: range.length),
               let target = textView.textRange(from: start, to: end) {
                textView.replace(target, withText: text.components(separatedBy: .newlines).joined(separator: " "))
                return false
            }
            return true
        }
    }
}

final class ExplicitLineTextView: UITextView {
    var wantsKeyboardFocus = false
    var wrapsLines = false
    var preferredLanguage: String?
    var appliedConfiguration = LineEditorConfiguration()
    var lineActions = LineEditorActions()
    var lineID: String?
    /// Set by Shift-Return so the one newline it inserts passes the Return intercept.
    var allowsNextNewline = false

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil && wantsKeyboardFocus { becomeFirstResponder() }
    }
    override func layoutSubviews() {
        updateLineWidth()
        super.layoutSubviews()
    }

    /// KRI-240: request the caption language's keyboard when one is installed.
    override var textInputMode: UITextInputMode? {
        if let preferredLanguage,
           let mode = UITextInputMode.activeInputModes.first(where: {
               LineEditorConfiguration.language($0.primaryLanguage ?? "", matches: preferredLanguage)
           }) {
            return mode
        }
        return super.textInputMode
    }

    override var keyCommands: [UIKeyCommand]? {
        var commands = super.keyCommands ?? []
        guard lineActions.onNextLine != nil || lineActions.onEscape != nil else { return commands }
        let next = UIKeyCommand(input: "\t", modifierFlags: [], action: #selector(nextLineCommand))
        let previous = UIKeyCommand(input: "\t", modifierFlags: .shift, action: #selector(previousLineCommand))
        let escape = UIKeyCommand(input: UIKeyCommand.inputEscape, modifierFlags: [], action: #selector(escapeCommand))
        let newline = UIKeyCommand(input: "\r", modifierFlags: .shift, action: #selector(newlineCommand))
        for command in [next, previous, escape, newline] { command.wantsPriorityOverSystemBehavior = true }
        commands.append(contentsOf: [next, previous, escape, newline])
        return commands
    }
    @objc private func nextLineCommand() { lineActions.onNextLine?() }
    @objc private func previousLineCommand() { lineActions.onPreviousLine?() }
    @objc private func escapeCommand() { lineActions.onEscape?() }
    @objc private func newlineCommand() {
        allowsNextNewline = true
        insertText("\n")
        allowsNextNewline = false
    }

    func updateLineWidth() {
        guard !wrapsLines else { return }
        let font = font ?? .systemFont(ofSize: 17)
        let longest = (text ?? "").components(separatedBy: .newlines).map {
            ($0 as NSString).size(withAttributes: [.font: font]).width
        }.max() ?? 0
        let width = max(bounds.width - textContainerInset.left - textContainerInset.right,
                        ceil(longest) + textContainer.lineFragmentPadding * 2 + 24)
        if abs(textContainer.size.width - width) > 0.5 {
            textContainer.size = CGSize(width: width, height: .greatestFiniteMagnitude)
        }
    }
}
