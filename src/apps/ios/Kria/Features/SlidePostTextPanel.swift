import SwiftUI

/// Slide-scoped text editing: the REAL native Text panel (Edit text | Style) over a slide-backed
/// adapter, plus the slide-only actions (add text, switch text, apply style to all slides).
/// Reads and writes only through `SlidePostSession`, so every change is one undoable step and
/// nothing here knows about the video timeline editor. Overlays/stickers are out of scope.
struct SlidePostTextPanel: View {
    enum Tab: String { case edit = "Edit", style = "Style" }
    @ObservedObject var session: SlidePostSession
    let slideID: String
    @Binding var tab: Tab
    /// Keyboard up on a short phone: the pinned Add text / Apply row steps aside so the text box stays visible.
    var compact = false
    let onDone: () -> Void
    @StateObject private var editor: SlidePostTextEditor
    @State private var appliedMessage: String?
    /// The tab the panel itself last reported; a binding change that differs came from outside
    /// (a canvas tap) and re-creates the panel on the requested tab.
    @State private var reportedTab: Tab
    @State private var reloadToken = 0

    init(session: SlidePostSession, slideID: String, tab: Binding<Tab>, compact: Bool = false, onDone: @escaping () -> Void) {
        self.session = session; self.slideID = slideID; self._tab = tab; self.compact = compact; self.onDone = onDone
        _editor = StateObject(wrappedValue: SlidePostTextEditor(session: session, slideID: slideID))
        _reportedTab = State(initialValue: tab.wrappedValue)
    }

    private var texts: [SlidePostTextElement] {
        session.draft?.slides.first { $0.id == slideID }?.edits?.effectiveTexts ?? []
    }
    private var selected: SlidePostTextElement? { texts.first { $0.id == session.selectedTextID } ?? texts.first }

    private var configuration: NativeTextPanelConfiguration {
        var value = NativeTextPanelConfiguration()
        value.hidesTiming = true
        value.hidesAnimation = true
        value.doneIdentifier = "slidepost-done"
        value.contentIdentifier = "slidepost-text-field"
        value.maxTextLength = SlidePostTextElement.maxLength
        value.focusesContentOnAppear = true
        value.onTabChange = { next in
            let mapped: Tab = next == .edit ? .edit : .style
            reportedTab = mapped; tab = mapped
        }
        return value
    }

    /// The editor's connected bottom shell: a 32pt-radius island holding the real Text panel and,
    /// pinned under it, the actions only slides have.
    var body: some View {
        GeometryReader { geometry in
            VStack(spacing: 6) {
                if let selected {
                    NativeEditorTextPanel(
                        id: selected.id, session: editor, initialTab: tab == .edit ? .edit : .style,
                        configuration: configuration, onDone: onDone
                    )
                    .id("\(selected.id)-\(reloadToken)")
                    // Scrolled content ends clear of the pinned action row below it.
                    .contentMargins(.bottom, 16, for: .scrollContent)
                    .environment(\.nativeEditorConnectedPanel, true)
                    .environment(\.nativeEditorPanelContentWidth, max(0, geometry.size.width - 72))
                } else {
                    emptyState
                }
                if !(compact && tab == .edit) {
                    Rectangle().fill(KriaColor.line.opacity(0.5)).frame(height: 1).padding(.horizontal, 24)
                    actions
                }
            }
            .padding(.top, 10).padding(.bottom, 8)
            .frame(width: geometry.size.width, height: geometry.size.height, alignment: .top)
        }
        .font(KriaFont.body(14)).tint(KriaColor.ink)
        .nativeEditorIslandSurface(cornerRadius: 32)
        .excludesDrawerGesture()
        .onAppear {
            editor.slideID = slideID
            if session.selectedTextID == nil || !texts.contains(where: { $0.id == session.selectedTextID }) { session.selectedTextID = texts.first?.id }
        }
        .onChange(of: tab) { _, next in
            if next != reportedTab { reportedTab = next; reloadToken += 1 }
        }
        .onChange(of: session.selectedTextID) { _, _ in appliedMessage = nil }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Text("No text on this slide yet.").font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
            Button(action: onDone) { Text("Done").frame(minWidth: 64, minHeight: 44) }.accessibilityIdentifier("slidepost-done")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: Slide-only actions

    private var actions: some View {
        VStack(spacing: 6) {
            if texts.count > 1 { textChips }
            HStack(spacing: 10) {
                if texts.count < SlidePostEdits.maxTexts {
                    Button {
                        tab = .edit; reportedTab = .edit
                        _ = session.addText(slideID: slideID)
                    } label: { Label("Add text", systemImage: "plus").frame(maxWidth: tab == .style ? nil : .infinity, minHeight: 44).fixedSize(horizontal: tab == .style, vertical: false) }
                        .buttonStyle(KriaSecondaryButtonStyle()).accessibilityIdentifier("slidepost-add-text")
                } else {
                    Text("A slide holds up to 4 texts.").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if tab == .style, let selected { applyToAllButton(selected) }
            }
            if let appliedMessage {
                Text(appliedMessage).font(KriaFont.body(11)).foregroundStyle(KriaColor.success).accessibilityIdentifier("slidepost-apply-all-result")
            }
        }
        .padding(.horizontal, 24)
    }

    private var textChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(texts.enumerated()), id: \.element.id) { index, element in
                    let on = element.id == selected?.id
                    Button { session.selectedTextID = element.id } label: {
                        Text(element.text.isEmpty ? "Text \(index + 1)" : String(element.text.prefix(14)))
                            .font(KriaFont.body(13).weight(on ? .semibold : .regular)).lineLimit(1)
                            .foregroundStyle(on ? KriaColor.mutedInk : KriaColor.ink)
                            .padding(.horizontal, 14).frame(minHeight: 44)
                            .background(on ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                            .overlay(Capsule().strokeBorder(on ? KriaColor.mutedInk : KriaColor.line, lineWidth: on ? 1.5 : 1))
                    }
                    .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
                    .accessibilityLabel("Text \(index + 1)").accessibilityIdentifier("slidepost-text-chip-\(index + 1)")
                }
            }
        }
        .excludesDrawerGesture()
    }

    private func applyToAllButton(_ element: SlidePostTextElement) -> some View {
        Button {
            session.applyStyleToAllSlides(slideID: slideID, textID: element.id)
            let others = (session.draft?.slides.count ?? 1) - 1
            appliedMessage = others > 0 ? "Style applied to \(others) other slide\(others == 1 ? "" : "s")" : nil
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        } label: {
            Text("Apply to all slides").frame(maxWidth: .infinity, minHeight: 44)
                .lineLimit(1).minimumScaleFactor(0.8)
        }
        .buttonStyle(KriaPrimaryButtonStyle())
        .accessibilityIdentifier("slidepost-apply-all")
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
