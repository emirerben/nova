import SwiftUI

/// The Paper inspector shell keeps the preview visible while controls scroll.
struct NativeEditorLanePanel<Tab: Hashable & RawRepresentable, Content: View>: View where Tab.RawValue == String {
    let title: String
    let tabs: [Tab]
    @Binding var tab: Tab
    let onDone: () -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(spacing: 6) {
            HStack {
                Text(title).font(KriaFont.body(15).weight(.semibold))
                Spacer()
                Button("Done", action: onDone).frame(minWidth: 64, minHeight: 44)
                    .accessibilityIdentifier("native-editor-\(title.lowercased())-done")
            }
            HStack(spacing: 4) {
                ForEach(tabs, id: \.self) { value in
                    Button { tab = value } label: {
                        Text(value.rawValue)
                            .font(KriaFont.body(14).weight(value == tab ? .semibold : .regular))
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(value == tab ? KriaColor.selectionSoft : .clear, in: RoundedRectangle(cornerRadius: 10))
                    }
                    .accessibilityAddTraits(value == tab ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-\(title.lowercased())-tab-\(value.rawValue)")
                }
            }
            ScrollView { content().padding(.top, 8).padding(.bottom, 12) }
                .scrollDismissesKeyboard(.interactively)
        }
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: 352)
        .background(KriaColor.paper)
        .overlay(alignment: .top) { KriaColor.line.opacity(0.4).frame(height: 1) }
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-\(title.lowercased())-panel")
    }
}

struct NativeCaptionPanel: View {
    enum Tab: String, CaseIterable { case edit = "Edit captions", style = "Style", settings = "Settings" }
    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    @State private var tab: Tab = .edit
    @FocusState private var editingCueID: String?
    private var meta: [String: JSONValue] { session.document.captionMeta }
    private var appearance: [String: JSONValue] { meta["appearance"]?.objectValue ?? [:] }

    var body: some View {
        NativeEditorLanePanel(title: "Captions", tabs: Tab.allCases, tab: $tab, onDone: {
            editingCueID = nil; session.endTransaction(); onDone()
        }) {
            VStack(spacing: 8) {
                switch tab {
                case .edit: transcript
                case .style: style
                case .settings: settings
                }
                if !session.canEditCaptions {
                    Text("Captions aren’t available for this edit.").foregroundStyle(KriaColor.mutedInk)
                }
            }.disabled(!session.canEditCaptions)
        }
        .onChange(of: editingCueID) { old, new in
            if old != nil { session.endTransaction() }
            if new != nil { session.beginTransaction() }
        }
        .onChange(of: tab) { _, _ in editingCueID = nil; session.endTransaction() }
        .onDisappear { session.endTransaction() }
    }

    private var transcript: some View {
        VStack(spacing: 8) {
            if session.document.captionCues.isEmpty {
                Text("There are no captions in this edit.")
                    .frame(maxWidth: .infinity, minHeight: 80)
                    .foregroundStyle(KriaColor.mutedInk)
            }
            ForEach(Array(session.document.captionCues.enumerated()), id: \.element.id) { index, cue in
                HStack(spacing: 12) {
                    Text(String(index + 1)).font(KriaFont.body(12))
                        .foregroundStyle(KriaColor.mutedInk).frame(width: 22)
                    VStack(alignment: .leading, spacing: 5) {
                        if editingCueID == cue.id {
                            TextField("Caption", text: Binding(get: {
                                session.document.captionCues.first { $0.id == cue.id }?.text ?? ""
                            }, set: { session.updateCaptionCue(id: cue.id, text: $0) }), axis: .vertical)
                            .focused($editingCueID, equals: cue.id)
                            .accessibilityIdentifier("native-editor-caption-content-" + cue.id)
                        } else {
                            Text(cue.text).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        Text("\(time(cue.startS)) – \(time(cue.endS))")
                            .font(KriaFont.body(11)).monospacedDigit().foregroundStyle(KriaColor.mutedInk)
                    }
                    Image(systemName: "chevron.right").font(.system(size: 12))
                }
                .padding(.horizontal, 12).padding(.vertical, 8).frame(minHeight: 59)
                .background(session.selection == EditorSelection(kind: .captionCue, id: cue.id) ? KriaColor.selectionSoft : KriaColor.softZinc,
                            in: RoundedRectangle(cornerRadius: 10))
                .contentShape(Rectangle())
                .onTapGesture {
                    session.endTransaction()
                    session.select(EditorSelection(kind: .captionCue, id: cue.id))
                    editingCueID = cue.id
                }
                .accessibilityAction(named: "Edit caption") {
                    session.select(EditorSelection(kind: .captionCue, id: cue.id))
                    editingCueID = cue.id
                }
                .accessibilityIdentifier("native-editor-caption-row-" + cue.id)
            }
            Text("Tap a line to edit. Changes stay synced to speech.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var style: some View {
        VStack(spacing: 8) {
            NativeEditorMenuRow(title: "Display", value: meta["style"] == .string("word") ? "Word" : "Sentence") {
                Button("Sentence") { session.setCaptionDisplay("sentence") }
                Button("Word") { session.setCaptionDisplay("word") }
            }.disabled(!session.canEditCaptionAppearance)
                .accessibilityIdentifier("native-editor-caption-display")
            HStack(spacing: 10) {
                Menu {
                    Button("Default · TikTok Sans") { session.setCaptionFont(nil) }
                    ForEach(NativeEditorWireContract.captionFonts, id: \.self) { font in
                        Button(font) { session.setCaptionFont(font) }
                    }
                } label: {
                    HStack {
                        Text(meta["font"]?.stringValue ?? "TikTok Sans").lineLimit(1)
                        Spacer(minLength: 2)
                        Image(systemName: "chevron.down").font(.system(size: 10))
                    }.padding(.horizontal, 12).frame(maxWidth: .infinity, minHeight: 44)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                }.buttonStyle(.plain).accessibilityLabel("Caption font")
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
            HStack(spacing: 12) {
                Text("Color")
                Spacer(minLength: 0)
                ForEach(["#FFFFFF", "#30352C", "#FFF19E", "#9BCAFF", "#E5DAF5"], id: \.self) { hex in
                    Button { session.setCaptionMeta(key: "color", value: .string(hex)) } label: {
                        Circle().fill(nativeEditorColor(hex)).frame(width: 30, height: 30)
                            .overlay(Circle().stroke((meta["color"]?.stringValue ?? "#FFFFFF") == hex ? KriaColor.sky : .clear, lineWidth: 2))
                    }.frame(minWidth: 36, minHeight: 44).accessibilityLabel("Caption color " + hex)
                }
                ColorPicker("Custom caption color", selection: color("color", fallback: "#FFFFFF"), supportsOpacity: false).labelsHidden()
            }.frame(minHeight: 44)
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
                ColorPicker("Highlight color", selection: color("highlight_color", fallback: "#C5F82A"), supportsOpacity: false).frame(minHeight: 44)
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
