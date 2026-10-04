import SwiftUI

/// Slide-scoped text editing (Edit | Style). Reads and writes only through `SlidePostSession`,
/// so every change is one undoable step and nothing here knows about the video timeline editor.
struct SlidePostTextPanel: View {
    enum Tab: String { case edit = "Edit", style = "Style" }
    @ObservedObject var session: SlidePostSession
    let slideID: String
    @Binding var tab: Tab
    let onDone: () -> Void
    @State private var appliedMessage: String?
    @FocusState private var focusedTextID: String?

    private static let swatches = ["#FFFFFF", "#30352C", "#FFF0A6", "#9BCAFF", "#E7DDF5", "#A63224"]

    private var texts: [SlidePostTextElement] {
        session.draft?.slides.first { $0.id == slideID }?.edits?.effectiveTexts ?? []
    }
    private var selected: SlidePostTextElement? { texts.first { $0.id == session.selectedTextID } ?? texts.first }

    /// The editor's connected bottom shell: a 32pt-radius island holding the panel header
    /// (title + Done), the shared tab control and a scrolling body.
    var body: some View {
        VStack(spacing: 6) {
            VStack(spacing: 6) {
                HStack {
                    Text("Text").font(KriaFont.body(18).weight(.semibold))
                    Spacer()
                    Button(action: onDone) {
                        Text("Done").font(KriaFont.body(14)).frame(minWidth: 64, minHeight: 44)
                            .background(KriaColor.ink.opacity(0.06), in: Capsule())
                    }
                    .accessibilityIdentifier("slidepost-done")
                }
                NativeEditorPanelTabs(tabs: [Tab.edit, Tab.style], selection: $tab, accessibilityPrefix: "slidepost-tabs",
                                      identifier: { "slidepost-tab-\($0.rawValue.lowercased())" })
            }
            ScrollView(showsIndicators: false) {
                VStack(spacing: 8) {
                    if tab == .edit || selected == nil { editTab } else { styleTab }
                }
                .padding(.top, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            if tab == .style, let selected {
                applyToAll(selected)
            }
        }
        .padding(.horizontal, 24).padding(.top, 10).padding(.bottom, 8)
        .font(KriaFont.body(14)).tint(KriaColor.ink)
        .nativeEditorIslandSurface(cornerRadius: 32)
        .excludesDrawerGesture()
        .onAppear { if session.selectedTextID == nil { session.selectedTextID = texts.first?.id } }
    }

    // MARK: Edit tab

    private var editTab: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(texts) { element in
                HStack(spacing: 8) {
                    TextField("Slide text", text: Binding(
                        get: { element.text },
                        set: { value in session.updateText(slideID: slideID, textID: element.id, coalescing: "edit") { $0.text = String(value.prefix(SlidePostTextElement.maxLength)) } }
                    ), axis: .vertical)
                    .font(KriaFont.body(14)).lineLimit(1...3).focused($focusedTextID, equals: element.id)
                    .padding(10)
                    .background(element.id == selected?.id ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
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
                Text("A slide holds up to 4 texts.").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
            }
        }
    }

    // MARK: Style tab

    @ViewBuilder private var styleTab: some View {
        if let element = selected {
            VStack(spacing: 4) {
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
        let names = SlidePostTextElement.fontChoices()
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(names, id: \.self) { name in
                    let on = element.fontFamily == name
                    Button { update(element) { $0.fontFamily = name } } label: {
                        Text(name == SlidePostTextElement.defaultFont ? "Inter Bold" : name)
                            // Chrome stays Inter; the chosen font is applied to the canvas text, not the chip.
                            .font(KriaFont.body(14).weight(on ? .semibold : .regular))
                            .foregroundStyle(on ? KriaColor.mutedInk : KriaColor.ink)
                            .padding(.horizontal, 14).frame(minHeight: 44)
                            .background(on ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                            .overlay(Capsule().strokeBorder(on ? KriaColor.mutedInk : KriaColor.line, lineWidth: on ? 1.5 : 1))
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
        HStack(spacing: 0) {
            ForEach(Self.swatches, id: \.self) { hex in
                let on = element.color.uppercased() == hex
                Button { update(element) { $0.color = hex } } label: {
                    Circle().fill(Color(slideHex: hex))
                        .overlay(Circle().stroke(on ? KriaColor.sky : KriaColor.line, lineWidth: 2))
                        .frame(width: 28, height: 28).frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain).accessibilityLabel("Color \(hex)").accessibilityAddTraits(on ? .isSelected : [])
                .accessibilityIdentifier("slidepost-color-\(hex.dropFirst())")
            }
            ColorPicker("Custom color", selection: Binding(
                get: { Color(slideHex: element.color) },
                set: { value in let hex = value.slideHexString; update(element, key: "color") { $0.color = hex } }
            ), supportsOpacity: false)
            .labelsHidden().frame(minWidth: 44, minHeight: 44)
            .accessibilityLabel("Custom color").accessibilityIdentifier("slidepost-color-custom")
            Spacer(minLength: 0)
        }
    }

    private func sizeRow(_ element: SlidePostTextElement) -> some View {
        HStack(spacing: 12) {
            Text("Size")
            Slider(value: Binding(
                get: { Double(element.sizePx) },
                set: { value in update(element, key: "size") { $0.sizePx = Int(value.rounded()) } }
            ), in: Double(SlidePostTextElement.sizeRange.lowerBound)...Double(SlidePostTextElement.sizeRange.upperBound), step: 1)
            .tint(KriaColor.ink).accessibilityLabel("Text size").accessibilityIdentifier("slidepost-size")
            Text("\(element.sizePx)").monospacedDigit().frame(minWidth: 44, alignment: .trailing)
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
                    Text(value.capitalized).font(KriaFont.body(14).weight(element.position == value ? .semibold : .regular)).lineLimit(1).minimumScaleFactor(0.7)
                } action: { update(element) { $0.position = value; $0.xFrac = nil; $0.yFrac = nil } }
            }
        }
    }
    private func segmentGroup<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(spacing: 0) { content() }.clipShape(RoundedRectangle(cornerRadius: 10))
    }
    private func segment<Label: View>(on: Bool, label: String, id: String, @ViewBuilder content: () -> Label, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            content().foregroundStyle(KriaColor.ink).frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.selectionSoft : KriaColor.softZinc)
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
            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular))
                .foregroundStyle(on ? KriaColor.mutedInk : KriaColor.ink).padding(.horizontal, 16).frame(minHeight: 44)
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
                Label("Apply style to all slides", systemImage: "square.on.square").frame(maxWidth: .infinity)
            }
            .buttonStyle(KriaPrimaryButtonStyle())
            .accessibilityIdentifier("slidepost-apply-all")
            if let appliedMessage {
                Text(appliedMessage).font(KriaFont.body(11)).foregroundStyle(KriaColor.success).accessibilityIdentifier("slidepost-apply-all-result")
            }
        }
        .onChange(of: session.selectedTextID) { _, _ in appliedMessage = nil }
    }
}

/// Look presets for the selected slide, in the same connected bottom shell as the text panel.
struct SlidePostLookPanel: View {
    @ObservedObject var session: SlidePostSession
    let slideID: String
    let onDone: () -> Void
    private static let looks: [(String, String)] = [
        ("none", "Original"), ("stadium_diffusion", "Stadium Diffusion"), ("olive_film", "Olive Film"),
        ("smoky_split_tone", "Smoky Split-Tone"), ("golden_hour", "Golden Hour"), ("faded_analog", "Faded Analog"),
    ]
    private var current: String { session.draft?.slides.first { $0.id == slideID }?.edits?.lookPreset ?? "none" }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Look").font(KriaFont.body(18).weight(.semibold))
                Spacer()
                Button(action: onDone) {
                    Text("Done").font(KriaFont.body(14)).frame(minWidth: 64, minHeight: 44)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                }
                .accessibilityIdentifier("slidepost-done")
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Self.looks, id: \.0) { value, title in
                        let on = current == value
                        Button { session.setLook(slideID: slideID, preset: value) } label: {
                            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular))
                                .foregroundStyle(on ? KriaColor.mutedInk : KriaColor.ink)
                                .padding(.horizontal, 14).frame(minHeight: 44)
                                .background(on ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                                .overlay(Capsule().strokeBorder(on ? KriaColor.mutedInk : KriaColor.line, lineWidth: on ? 1.5 : 1))
                        }
                        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : []).accessibilityIdentifier("slidepost-look-\(value)")
                    }
                }
            }
            .excludesDrawerGesture()
            Text("Save to apply this look to the rendered slide.").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24).padding(.top, 10).padding(.bottom, 8).frame(maxWidth: .infinity, alignment: .leading)
        .font(KriaFont.body(14)).tint(KriaColor.ink)
        .nativeEditorIslandSurface(cornerRadius: 32)
    }
}
