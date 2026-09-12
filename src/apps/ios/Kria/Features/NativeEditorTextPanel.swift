import SwiftUI
import UIKit

struct NativeEditorTextPanel: View {
    enum Tab: String, CaseIterable { case edit = "Edit text", style = "Style", animation = "Animation" }
    enum Phase: String, CaseIterable { case entrance = "In", exit = "Out", loop = "Loop" }
    let id: String
    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    @State private var tab: Tab = .style
    @State private var phase: Phase = .entrance
    @State private var typing = false
    @FocusState private var editingSize: Bool
    @State private var sizeInput = ""

    init(id: String, session: NativeEditorSession, initialTab: Tab = .style, onDone: @escaping () -> Void) {
        self.id = id; self.session = session; self.onDone = onDone
        _tab = State(initialValue: initialTab)
    }

    private var item: EditorTextElement? { session.document.textElements.first { $0.id == id } }
    private let palette = ["#FFFFFF", "#30352C", "#FFF0A6", "#9BCAFF", "#E7DDF5"]

    var body: some View {
        VStack(spacing: 6) {
            HStack {
                Text("Text").font(KriaFont.body(15).weight(.semibold))
                Spacer()
                Button("Done") { commitSize(); editingSize = false; typing = false; session.endTransaction(); onDone() }
                    .frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("native-editor-text-inspector-done")
            }
            Picker("Text controls", selection: $tab) {
                ForEach(Tab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("native-editor-text-tabs")
            ScrollView {
                VStack(spacing: 8) {
                    switch tab {
                    case .edit:
                        NativeExplicitLineTextEditor(text: Binding(
                            get: { item?.text ?? "" }, set: { session.updateTextContent(id: id, content: $0) }
                        ), focused: $typing, identifier: "native-editor-text-content")
                        .frame(height: 100)
                        .padding(8)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                        HStack {
                            timingField("Start", isStart: true)
                            timingField("End", isStart: false)
                        }
                    case .style: styleControls
                    case .animation: animationControls
                    }
                }.padding(.top, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            .disabled(!session.canEdit(.text))
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 8)
        .frame(maxHeight: tab == .edit ? 260 : 352)
        .background(KriaColor.paper)
        .overlay(alignment: .top) { KriaColor.line.opacity(0.4).frame(height: 1) }
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
        .onDisappear { session.endTransaction() }
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
            HStack {
                Text("Preset")
                Spacer()
                Menu {
                    ForEach(["Simple", "Bold", "Highlight"], id: \.self) { preset in
                        Button(preset) { session.applyTextPreset(id: id, preset: preset) }
                    }
                } label: { Label(string("editor_preset", "Simple"), systemImage: "chevron.down") }
                .accessibilityIdentifier("native-editor-text-preset")
            }
            .padding(.horizontal, 14).frame(height: 44)
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(KriaColor.line))

            HStack(spacing: 10) {
                Menu {
                    ForEach(["Inter Regular", "Inter", "Fraunces", "Space Grotesk"], id: \.self) { family in
                        Button(family) { session.setTextStyle(id: id, style: family) }
                    }
                } label: {
                    HStack { Text(string("font_family", "Inter")).lineLimit(1); Spacer(); Image(systemName: "chevron.down") }
                        .padding(.horizontal, 12).frame(height: 44)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                }
                .accessibilityIdentifier("native-editor-text-font")
                HStack(spacing: 0) {
                    ForEach(["left", "center", "right"], id: \.self) { alignment in
                        Button { session.setTextAlignment(id: id, alignment: alignment) } label: {
                            Image(systemName: "text.align\(alignment)")
                                .frame(width: 40, height: 44)
                                .background(string("alignment", "center") == alignment ? KriaColor.selectionSoft : KriaColor.softZinc)
                        }
                        .accessibilityLabel("Align text \(alignment)")
                        .accessibilityAddTraits(string("alignment", "center") == alignment ? .isSelected : [])
                    }
                }.clipShape(RoundedRectangle(cornerRadius: 10))
            }
            HStack(spacing: 12) {
                Text("Size")
                Spacer()
                Button {
                    commitSize(); editingSize = false
                    session.setTextSize(id: id, sizePX: max(8, currentSize - 4))
                } label: { Image(systemName: "minus").frame(width: 44, height: 44) }
                .accessibilityLabel("Decrease text size")
                TextField("Size", text: Binding(
                    get: { editingSize ? sizeInput : sizeLabel },
                    set: { sizeInput = $0 }
                ))
                .focused($editingSize)
                .keyboardType(.decimalPad)
                .multilineTextAlignment(.center)
                .monospacedDigit()
                .frame(width: 76, height: 44)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel("Text size")
                .accessibilityIdentifier("native-editor-text-size")
                Button {
                    commitSize(); editingSize = false
                    session.setTextSize(id: id, sizePX: currentSize + 4)
                } label: { Image(systemName: "plus").frame(width: 44, height: 44) }
                .accessibilityLabel("Increase text size")
            }
            HStack(spacing: 10) {
                Text("Color").frame(width: 46, alignment: .leading)
                ForEach(palette, id: \.self) { hex in
                    Button { session.setTextColor(id: id, color: hex) } label: {
                        Circle().fill(nativeEditorColor(hex))
                            .overlay(Circle().stroke(string("color", "#FFFFFF") == hex ? KriaColor.sky : KriaColor.line, lineWidth: 2))
                            .frame(width: 28, height: 28).frame(minWidth: 32, minHeight: 44)
                    }.accessibilityLabel("Text color \(hex)")
                }
                ColorPicker("Custom color", selection: colorBinding("color", fallback: "#FFFFFF"), supportsOpacity: false)
                    .labelsHidden().accessibilityLabel("Custom text color")
            }
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
            Picker("Animation phase", selection: $phase) {
                ForEach(Phase.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
            HStack(spacing: 6) {
                ForEach(phase == .loop ? ["None", "Pulse", "Bounce", "Float"] : ["None", "Fade", "Pop", "Slide", "Typewriter"], id: \.self) { effect in
                    Button { session.setTextPhase(id: id, phase: phaseKey, effect: effect.lowercased()) } label: {
                        VStack(spacing: 6) {
                            Text(effect == "Typewriter" ? "Te|" : "Text")
                                .font(KriaFont.body(20).weight(.semibold))
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
            HStack {
                Text("Speed").frame(width: 68, alignment: .leading)
                NativeEditorSlider(session: session, value: Binding(
                    get: { phases["speed"]?.numberValue ?? 1 },
                    set: { session.setTextAnimationSpeed(id: id, speed: $0) }
                ), in: 0.25...3, step: 0.25) { Text("Animation speed") }
                Text("\((phases["speed"]?.numberValue ?? 1).formatted())×").frame(width: 42)
            }
            Text("Changes motion speed. Text timing stays the same.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var sizeLabel: String { currentSize.formatted(.number.grouping(.never).precision(.fractionLength(0...1))) }
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
        HStack(spacing: 12) {
            Text(title).frame(width: 66, alignment: .leading)
            ColorPicker(title, selection: colorBinding(colorKey, fallback: "#18181B"), supportsOpacity: false).labelsHidden()
            NativeEditorSlider(session: session, value: Binding(
                get: { item?.raw[key]?.numberValue ?? 0 },
                set: {
                    session.updateTextRaw(id: id, key: key, value: .number($0))
                    if key == "shadow_opacity" { session.setTextShadow(id: id, enabled: $0 > 0) }
                }
            ), in: range) { Text(title) }
            Text("\(Int((item?.raw[key]?.numberValue ?? 0) * (unit == "%" ? 100 : 1)))\(unit)")
                .monospacedDigit().frame(width: 44, alignment: .trailing)
        }.frame(minHeight: 40)
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

/// The editor and canvas share explicit line breaks. Long lines scroll sideways.
struct NativeExplicitLineTextEditor: UIViewRepresentable {
    @Binding var text: String
    var focused: Binding<Bool>
    let identifier: String

    func makeUIView(context: Context) -> ExplicitLineTextView {
        let view = ExplicitLineTextView()
        view.delegate = context.coordinator
        view.font = .systemFont(ofSize: 17)
        view.backgroundColor = .clear
        view.textColor = .label
        view.returnKeyType = .default
        view.textContainer.widthTracksTextView = false
        view.textContainer.heightTracksTextView = false
        view.accessibilityLabel = "Text"
        view.accessibilityIdentifier = identifier
        return view
    }

    func updateUIView(_ view: ExplicitLineTextView, context: Context) {
        context.coordinator.parent = self
        if view.text != text { view.text = text }
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
    }
}

final class ExplicitLineTextView: UITextView {
    var wantsKeyboardFocus = false
    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil && wantsKeyboardFocus { becomeFirstResponder() }
    }
    override func layoutSubviews() {
        updateLineWidth()
        super.layoutSubviews()
    }
    func updateLineWidth() {
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
