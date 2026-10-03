import SwiftUI

/// Slide-scoped text editing (Edit | Style). Reads and writes only through `SlidePostSession`,
/// so every change is one undoable step and nothing here knows about the video timeline editor.
struct SlidePostTextPanel: View {
    enum Tab: String { case edit = "Edit", style = "Style" }
    @ObservedObject var session: SlidePostSession
    let slideID: String
    @Binding var tab: Tab
    @State private var appliedMessage: String?
    @FocusState private var focusedTextID: String?

    private static let swatches = ["#FFFFFF", "#30352C", "#FFF0A6", "#9BCAFF", "#E7DDF5", "#A63224"]

    private var texts: [SlidePostTextElement] {
        session.draft?.slides.first { $0.id == slideID }?.edits?.effectiveTexts ?? []
    }
    private var selected: SlidePostTextElement? { texts.first { $0.id == session.selectedTextID } ?? texts.first }
    private var slideNumber: Int { (session.draft?.slides.firstIndex { $0.id == slideID } ?? 0) + 1 }

    var body: some View {
        VStack(spacing: 14) {
            segmented
            ScrollView(showsIndicators: false) {
                VStack(spacing: 16) {
                    if tab == .edit || selected == nil { editTab } else { styleTab }
                }
                .padding(.bottom, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            if tab == .style, let selected {
                applyToAll(selected)
            }
        }
        .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
        .background(KriaColor.paper, in: UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28, style: .continuous))
        .shadow(color: .black.opacity(0.07), radius: 10, y: -2)
        .excludesDrawerGesture()
        .onAppear { if session.selectedTextID == nil { session.selectedTextID = texts.first?.id } }
    }

    // MARK: Segmented control

    private var segmented: some View {
        HStack(spacing: 0) {
            ForEach([Tab.edit, Tab.style], id: \.self) { item in
                Button { tab = item } label: {
                    Text(item.rawValue).font(KriaFont.body(16).weight(tab == item ? .bold : .regular))
                        .foregroundStyle(tab == item ? KriaColor.ink : KriaColor.zinc)
                        .frame(maxWidth: .infinity, minHeight: 36)
                        .background(tab == item ? KriaColor.paper : .clear, in: Capsule())
                        .shadow(color: .black.opacity(tab == item ? 0.10 : 0), radius: 4, y: 1)
                }
                .buttonStyle(.plain).accessibilityAddTraits(tab == item ? .isSelected : [])
                .accessibilityIdentifier("slidepost-tab-\(item.rawValue.lowercased())")
            }
        }
        .padding(3).background(KriaColor.softZinc, in: Capsule())
    }

    // MARK: Edit tab

    private var editTab: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Slide \(slideNumber) · Text").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.zinc)
            ForEach(texts) { element in
                HStack(spacing: 8) {
                    TextField("Slide text", text: Binding(
                        get: { element.text },
                        set: { value in session.updateText(slideID: slideID, textID: element.id, coalescing: "edit") { $0.text = String(value.prefix(SlidePostTextElement.maxLength)) } }
                    ), axis: .vertical)
                    .font(KriaFont.body(16)).lineLimit(1...3).focused($focusedTextID, equals: element.id)
                    .padding(12)
                    .background(element.id == selected?.id ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .onTapGesture { session.selectedTextID = element.id; focusedTextID = element.id }
                    .accessibilityIdentifier("slidepost-text-field")
                    Button { session.removeText(slideID: slideID, textID: element.id) } label: {
                        Image(systemName: "trash").frame(width: 44, height: 44).foregroundStyle(KriaColor.failureText)
                    }
                    .accessibilityLabel("Remove text").accessibilityIdentifier("slidepost-text-remove")
                }
            }
            if texts.count < SlidePostEdits.maxTexts {
                Button {
                    if let id = session.addText(slideID: slideID) { focusedTextID = id }
                } label: { Label("Add text", systemImage: "plus").frame(maxWidth: .infinity, minHeight: 44) }
                    .buttonStyle(KriaSecondaryButtonStyle()).accessibilityIdentifier("slidepost-add-text")
            } else {
                Text("A slide holds up to 4 texts.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
        }
    }

    // MARK: Style tab

    @ViewBuilder private var styleTab: some View {
        if let element = selected {
            VStack(spacing: 16) {
                fontChips(element)
                colorSwatches(element)
                sizeRow(element)
                HStack(spacing: 10) { alignmentControl(element); positionControl(element) }
                toggles(element)
            }
        }
    }

    private func update(_ element: SlidePostTextElement, key: String? = nil, _ mutate: @escaping (inout SlidePostTextElement) -> Void) {
        session.updateText(slideID: slideID, textID: element.id, coalescing: key, mutate)
    }

    private func fontChips(_ element: SlidePostTextElement) -> some View {
        let names = [SlidePostTextElement.defaultFont] + NativeFontCatalog.shared.pickerFonts.filter { $0 != SlidePostTextElement.defaultFont }
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(names, id: \.self) { name in
                    let on = element.fontFamily == name
                    Button { update(element) { $0.fontFamily = name } } label: {
                        Text(name == SlidePostTextElement.defaultFont ? "Inter Bold" : name)
                            .font(NativeFontCatalog.shared.ctFont(name, size: 17).map(Font.init) ?? KriaFont.body(16))
                            .foregroundStyle(on ? KriaColor.plum : KriaColor.ink)
                            .padding(.horizontal, 16).frame(minHeight: 44)
                            .background(on ? KriaColor.lilac : KriaColor.paper, in: Capsule())
                            .overlay(Capsule().strokeBorder(on ? KriaColor.plum : KriaColor.line, lineWidth: on ? 1.5 : 1))
                    }
                    .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
                    .accessibilityLabel(name).accessibilityIdentifier("slidepost-font-\(name)")
                }
            }
            .padding(.vertical, 2)
        }
        .excludesDrawerGesture()
    }

    private func colorSwatches(_ element: SlidePostTextElement) -> some View {
        HStack(spacing: 10) {
            ForEach(Self.swatches, id: \.self) { hex in
                let on = element.color.uppercased() == hex
                Button { update(element) { $0.color = hex } } label: {
                    Circle().fill(Color(slideHex: hex)).frame(width: 32, height: 32)
                        .overlay(Circle().strokeBorder(KriaColor.line, lineWidth: 1))
                        .padding(5).overlay(Circle().strokeBorder(KriaColor.ink, lineWidth: on ? 2 : 0))
                        .frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain).accessibilityLabel("Color \(hex)").accessibilityAddTraits(on ? .isSelected : [])
                .accessibilityIdentifier("slidepost-color-\(hex.dropFirst())")
            }
            ColorPicker("Custom color", selection: Binding(
                get: { Color(slideHex: element.color) },
                set: { value in let hex = value.slideHexString; update(element, key: "color") { $0.color = hex } }
            ), supportsOpacity: false)
            .labelsHidden().scaleEffect(1.3).frame(width: 44, height: 44)
            .accessibilityLabel("Custom color").accessibilityIdentifier("slidepost-color-custom")
            Spacer(minLength: 0)
        }
    }

    private func sizeRow(_ element: SlidePostTextElement) -> some View {
        HStack(spacing: 12) {
            Text("Size").font(KriaFont.body(16)).foregroundStyle(KriaColor.zinc)
            Slider(value: Binding(
                get: { Double(element.sizePx) },
                set: { value in update(element, key: "size") { $0.sizePx = Int(value.rounded()) } }
            ), in: Double(SlidePostTextElement.sizeRange.lowerBound)...Double(SlidePostTextElement.sizeRange.upperBound), step: 1)
            .tint(KriaColor.ink).accessibilityLabel("Text size").accessibilityIdentifier("slidepost-size")
            Text("\(element.sizePx)").font(KriaFont.body(18).weight(.bold)).foregroundStyle(KriaColor.ink).frame(minWidth: 36, alignment: .trailing)
        }
    }

    private func alignmentControl(_ element: SlidePostTextElement) -> some View {
        segmentGroup {
            ForEach([("left", "text.alignleft"), ("center", "text.aligncenter"), ("right", "text.alignright")], id: \.0) { value, symbol in
                segment(on: element.alignment == value, label: "Align \(value)", id: "slidepost-align-\(value)") {
                    Image(systemName: symbol).font(.system(size: 16))
                } action: { update(element) { $0.alignment = value } }
            }
        }
    }
    private func positionControl(_ element: SlidePostTextElement) -> some View {
        segmentGroup {
            ForEach(["top", "center", "bottom"], id: \.self) { value in
                segment(on: element.position == value, label: value.capitalized, id: "slidepost-position-\(value)") {
                    Text(value.capitalized).font(KriaFont.body(15).weight(element.position == value ? .bold : .regular)).lineLimit(1).minimumScaleFactor(0.7)
                } action: { update(element) { $0.position = value; $0.xFrac = nil; $0.yFrac = nil } }
            }
        }
    }
    private func segmentGroup<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(spacing: 0) { content() }.padding(3).background(KriaColor.softZinc, in: Capsule())
    }
    private func segment<Label: View>(on: Bool, label: String, id: String, @ViewBuilder content: () -> Label, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            content().foregroundStyle(on ? KriaColor.ink : KriaColor.zinc).frame(maxWidth: .infinity, minHeight: 38)
                .background(on ? KriaColor.paper : .clear, in: Capsule()).shadow(color: .black.opacity(on ? 0.10 : 0), radius: 3, y: 1)
        }
        .buttonStyle(.plain).accessibilityLabel(label).accessibilityAddTraits(on ? .isSelected : []).accessibilityIdentifier(id)
    }

    private func toggles(_ element: SlidePostTextElement) -> some View {
        HStack(spacing: 10) {
            toggle("Outline", on: element.strokeWidth > 0, id: "slidepost-toggle-outline") { update(element) { $0.strokeWidth = $0.strokeWidth > 0 ? 0 : 6 } }
            toggle("Shadow", on: element.shadowEnabled, id: "slidepost-toggle-shadow") { update(element) { $0.shadowEnabled.toggle() } }
            toggle("Box", on: element.background == "box", id: "slidepost-toggle-box") { update(element) { $0.background = $0.background == "box" ? "none" : "box" } }
            Spacer(minLength: 0)
        }
    }
    private func toggle(_ title: String, on: Bool, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(KriaFont.body(16).weight(on ? .bold : .regular))
                .foregroundStyle(on ? KriaColor.mutedInk : KriaColor.ink).padding(.horizontal, 18).frame(minHeight: 44)
                .background(on ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                .overlay(Capsule().strokeBorder(on ? KriaColor.mutedInk : KriaColor.line, lineWidth: on ? 1.5 : 1))
        }
        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : []).accessibilityIdentifier(id)
    }

    private func applyToAll(_ element: SlidePostTextElement) -> some View {
        VStack(spacing: 6) {
            Button {
                session.applyStyleToAllSlides(slideID: slideID, textID: element.id)
                let others = (session.draft?.slides.count ?? 1) - 1
                appliedMessage = others > 0 ? "Style applied to \(others) other slide\(others == 1 ? "" : "s")" : nil
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            } label: {
                Label("Apply style to all slides", systemImage: "square.on.square").font(KriaFont.body(17).weight(.bold))
                    .foregroundStyle(KriaColor.ink).frame(maxWidth: .infinity, minHeight: 54).background(KriaColor.butter, in: Capsule())
            }
            .accessibilityIdentifier("slidepost-apply-all")
            if let appliedMessage {
                Text(appliedMessage).font(KriaFont.body(12)).foregroundStyle(KriaColor.success).accessibilityIdentifier("slidepost-apply-all-result")
            }
        }
        .onChange(of: session.selectedTextID) { _, _ in appliedMessage = nil }
    }
}

/// Look presets for the selected slide, shown in the same bottom panel position as the text panel.
struct SlidePostLookPanel: View {
    @ObservedObject var session: SlidePostSession
    let slideID: String
    private static let looks: [(String, String)] = [
        ("none", "Original"), ("stadium_diffusion", "Stadium Diffusion"), ("olive_film", "Olive Film"),
        ("smoky_split_tone", "Smoky Split-Tone"), ("golden_hour", "Golden Hour"), ("faded_analog", "Faded Analog"),
    ]
    private var current: String { session.draft?.slides.first { $0.id == slideID }?.edits?.lookPreset ?? "none" }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Look").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.zinc)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Self.looks, id: \.0) { value, title in
                        let on = current == value
                        Button { session.setLook(slideID: slideID, preset: value) } label: {
                            Text(title).font(KriaFont.body(16)).foregroundStyle(on ? KriaColor.plum : KriaColor.ink)
                                .padding(.horizontal, 16).frame(minHeight: 44)
                                .background(on ? KriaColor.lilac : KriaColor.paper, in: Capsule())
                                .overlay(Capsule().strokeBorder(on ? KriaColor.plum : KriaColor.line, lineWidth: on ? 1.5 : 1))
                        }
                        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : []).accessibilityIdentifier("slidepost-look-\(value)")
                    }
                }
            }
            .excludesDrawerGesture()
            Text("Save to apply this look to the rendered slide.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16).padding(.top, 18).padding(.bottom, 10).frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.paper, in: UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28, style: .continuous))
    }
}
