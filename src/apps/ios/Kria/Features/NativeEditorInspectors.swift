import SwiftUI

private func nativeEditorWireLabel(_ value: String) -> String {
    switch value {
    case "none": return "Original"
    case "dip_to_black": return "Dip to Black"
    case "preserve_cuts": return "Preserve Cuts"
    case "resync_beats": return "Resync Beats"
    default: return value.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ").capitalized
    }
}

struct NativeEditorSlider<Label: View>: View {
    @ObservedObject var session: NativeEditorSession
    @Binding private var value: Double
    private let bounds: ClosedRange<Double>
    private let step: Double?
    private let label: () -> Label

    init(
        session: NativeEditorSession,
        value: Binding<Double>,
        in bounds: ClosedRange<Double>,
        step: Double? = nil,
        @ViewBuilder label: @escaping () -> Label
    ) {
        self.session = session
        self._value = value
        self.bounds = bounds
        self.step = step
        self.label = label
    }

    @ViewBuilder var body: some View {
        if let step {
            Slider(value: $value, in: bounds, step: step, onEditingChanged: editingChanged, label: label)
        } else {
            Slider(value: $value, in: bounds, onEditingChanged: editingChanged, label: label)
        }
    }

    private func editingChanged(_ isEditing: Bool) {
        if isEditing { session.beginTransaction() }
        else { session.endTransaction() }
    }
}

/// Routes a timeline selection to the narrowest inspector that can safely
/// edit it. Unknown/effect lanes remain visible and explainable rather than
/// pretending that a local control can persist a field the session cannot
/// write yet.
struct NativeSelectionInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        switch selection.kind {
        case .clip:
            NativeSelectedClipInspector(selection: selection, session: session)
        case .text:
            NativeSelectedTextInspector(selection: selection, session: session)
        case .captionCue:
            NativeSelectedCaptionInspector(selection: selection, session: session)
        case .music:
            NativeSelectedMusicInspector(selection: selection, session: session)
        case .soundEffect, .mediaOverlay, .visualBlock, .motionScene, .cameraEffect, .carousel:
            NativeSelectedEffectInspector(selection: selection, session: session)
        }
    }
}

/// Document-level lanes do not have a timeline selection of their own. Keep
/// their persisted state discoverable from Kria's inspector and expose only
/// the background-music mutation backed by the session API.
struct NativeDocumentInspector: View {
    @ObservedObject var session: NativeEditorSession
    @State private var backgroundLevel = 0.0

    private var canEditBackground: Bool {
        nativeEditorEditable(session, keys: ["background_music"], fallback: session.canEdit(.backgroundMusic))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Document lanes")
                .font(KriaFont.body(16).weight(.semibold))
            if let background = session.document.backgroundMusic {
                VStack(alignment: .leading, spacing: 8) {
                    Label(background.trackID ?? "Background music", systemImage: background.muted ? "speaker.slash" : "waveform")
                    NativeEditorSlider(session: session, value: $backgroundLevel, in: -40...0, step: 0.5) { Text("Background music level") }
                        .onChange(of: backgroundLevel) { _, value in session.setBackgroundMusicLevel(value) }
                        .accessibilityIdentifier("native-editor-background-music-level")
                    LabeledContent(
                        "Level",
                        value: backgroundLevel.formatted(.number.precision(.fractionLength(1))) + " dB"
                    )
                }
                .padding(12)
                .background(KriaColor.softZinc)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .disabled(!canEditBackground)
            } else {
                Text("No background music lane")
                    .foregroundStyle(KriaColor.zinc)
            }
            LabeledContent("Title", value: session.document.title ?? "Untitled")
            LabeledContent("Orientation", value: session.document.orientation ?? "9:16")
            LabeledContent("Lyrics", value: session.document.lyrics == nil ? "None" : "Available")
            if !canEditBackground {
                nativeLockedNote(session: session, keys: ["background_music"], fallback: "Background music is locked for this render.")
            }
            nativeLockedNoteIfNeeded(session: session, keys: ["title", "orientation", "lyrics"], fallback: "Title, orientation, and lyrics are read-only in this native pass.")
        }
        .onAppear { backgroundLevel = session.document.backgroundMusic?.gainDB ?? 0 }
    }
}

/// Tool-level browsing keeps non-clip lanes discoverable even before an item
/// is selected on the compact timeline. Selecting a row hands control back to
/// the contextual inspector through the shared cross-kind selection.
struct NativeEffectBrowserInspector: View {
    @ObservedObject var session: NativeEditorSession
    let kinds: Set<EditorSelectionKind>
    let title: String

    private var items: [NativeEditorTimelineItem] {
        var result = session.timelineItems.filter { kinds.contains($0.kind) }
        if kinds.contains(.carousel),
           let carousel = session.document.carouselMoment,
           let id = nativeString(carousel["id"]),
           let start = nativeNumber(carousel["start_s"]),
           let end = nativeNumber(carousel["end_s"]),
           end > start,
           !result.contains(where: { $0.selection == EditorSelection(kind: .carousel, id: id) }) {
            result.append(NativeEditorTimelineItem(selection: EditorSelection(kind: .carousel, id: id), start: start, end: end, zIndex: 180, sourceIndex: 0))
        }
        return result.sorted { $0.start == $1.start ? $0.id < $1.id : $0.start < $1.start }
    }

    private var capabilityKeys: [String] {
        kinds.flatMap { kind in
            switch kind {
            case .soundEffect: return ["sfx", "sound_effects"]
            case .mediaOverlay: return ["overlays", "media_overlays"]
            case .visualBlock: return ["visual_blocks"]
            case .motionScene: return ["motion_scenes"]
            case .cameraEffect: return ["camera_effects"]
            case .carousel: return ["carousel", "carousel_moment"]
            default: return []
            }
        }
    }

    var body: some View {
        Form {
            Section {
                Text("Select an existing lane to edit its timing and renderer-backed properties.")
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.zinc)
                if items.isEmpty {
                    Label("No \(title.lowercased()) are present in this render.", systemImage: "square.dashed")
                        .foregroundStyle(KriaColor.zinc)
                }
            }
            Section(title) {
                ForEach(items, id: \.selection) { item in
                    Button {
                        session.select(item)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(browserItemName(item))
                                Text("\(nativeTimecode(item.start)) – \(nativeTimecode(item.end))")
                                    .font(KriaFont.body(12))
                                    .foregroundStyle(KriaColor.zinc)
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                        }
                    }
                    .foregroundStyle(KriaColor.ink)
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("native-editor-browser-\(item.kind.rawValue)-\(item.id)")
                }
            }
            if kinds.count == 1, !capabilityKeys.isEmpty {
                nativeLockedNoteIfNeeded(session: session, keys: capabilityKeys, fallback: "Creation and editing are controlled by the render capabilities.")
            }
        }
    }

    private func browserItemName(_ item: NativeEditorTimelineItem) -> String {
        switch item.kind {
        case .soundEffect: return session.document.soundEffects.first(where: { $0.id == item.id })?.kind ?? "Sound effect"
        case .mediaOverlay: return session.document.mediaOverlays.first(where: { $0.id == item.id })?.kind ?? "Media overlay"
        case .visualBlock: return session.document.visualBlocks.first(where: { $0.id == item.id })?.kind ?? "Visual block"
        case .motionScene: return session.document.motionScenes.first(where: { $0.id == item.id })?.preset ?? "Motion scene"
        case .cameraEffect: return session.document.cameraEffects.first(where: { $0.id == item.id })?.effect ?? "Camera effect"
        case .carousel: return "Carousel moment"
        default: return item.kind.rawValue
        }
    }
}

private struct NativeSelectedClipInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession

    private var clip: EditorClip? {
        guard let id = UUID(uuidString: selection.id) else { return nil }
        return session.draft.clips.first(where: { $0.id == id })
    }
    private var slot: EditorTimelineSlot? {
        guard let slotID = clip?.slotID else { return nil }
        return session.document.clips.first(where: { $0.id == slotID })
    }
    @State private var sourceStart = 0.0
    @State private var sourceEnd = 1.0
    @State private var look = "none"
    @State private var transition = "cut"
    @State private var transitionDuration = 0.1

    var body: some View {
        Form {
            Section("Clip") {
                if let clip {
                    LabeledContent("In", value: nativeTimecode(clip.start))
                    LabeledContent("Out", value: nativeTimecode(clip.end))
                    LabeledContent("Duration", value: nativeTimecode(clip.end - clip.start))
                    LabeledContent("Source", value: nativeTimecode(clip.trimIn) + " – " + nativeTimecode(clip.trimOut))
                } else {
                    Text("This clip is no longer in the timeline.")
                }
            }
            Section("Order and source window") {
                Button("Move earlier") { session.moveSelected(by: -0.1) }
                Button("Move later") { session.moveSelected(by: 0.1) }
                Button("Slide source earlier") { session.slideSourceWindow(by: -0.1) }
                Button("Slide source later") { session.slideSourceWindow(by: 0.1) }
                if let clip {
                    NativeEditorSlider(session: session, value: $sourceStart, in: 0...max(sourceEnd - 0.1, 0.1), step: 0.1) { Text("Source in") }
                        .onChange(of: sourceStart) { _, value in session.setClipSourceWindow(clipID: selection.id, startS: value, endS: sourceEnd) }
                    NativeEditorSlider(session: session, value: $sourceEnd, in: min(sourceStart + 0.1, clip.sourceDuration ?? sourceEnd)...max(clip.sourceDuration ?? sourceEnd, sourceStart + 0.1), step: 0.1) { Text("Source out") }
                        .onChange(of: sourceEnd) { _, value in session.setClipSourceWindow(clipID: selection.id, startS: sourceStart, endS: value) }
                }
            }
            .disabled(!session.canEditTimeline)
            Section("Look and transition") {
                Picker("Look", selection: $look) {
                    ForEach(NativeEditorWireContract.lookPresets, id: \.self) { value in
                        Text(nativeEditorWireLabel(value)).tag(value)
                    }
                }
                .onChange(of: look) { _, value in session.setClipLookPreset(clipID: selection.id, preset: value) }
                Picker("Transition", selection: $transition) {
                    ForEach(NativeEditorWireContract.transitions, id: \.self) { value in
                        Text(nativeEditorWireLabel(value)).tag(value)
                    }
                }
                .onChange(of: transition) { _, value in session.setClipTransition(clipID: selection.id, transition: value, durationS: transitionDuration) }
                NativeEditorSlider(session: session, value: $transitionDuration, in: 0.1...1, step: 0.05) { Text("Transition duration") }
                    .onChange(of: transitionDuration) { _, value in session.setClipTransition(clipID: selection.id, transition: transition, durationS: value) }
                    .disabled(transition == "cut")
            }
            Section("Clip audio") {
                Label(clip?.muted == true ? "Muted" : "Original audio on", systemImage: clip?.muted == true ? "speaker.slash" : "speaker.wave.2")
                nativeLockedNote(session: session, keys: ["clip_audio", "timeline"], fallback: "Per-clip audio changes are not supported by the renderer yet.")
            }
            if !session.canEditTimeline {
                Section { Label("Clip edits are locked for this render.", systemImage: "lock") }
            }
            Section {
                Button("Delete clip", role: .destructive) {
                    session.removeClip(clipID: selection.id)
                    session.select(nil, seekToStart: false)
                }
                .disabled(!session.canEditTimeline || session.draft.clips.count <= 1)
                .accessibilityIdentifier("native-editor-selected-clip-delete")
            }
        }
        .onAppear {
            sourceStart = clip?.trimIn ?? 0; sourceEnd = clip?.trimOut ?? 1
            look = NativeEditorWireContract.lookPresets.contains(slot?.lookPreset ?? "") ? slot?.lookPreset ?? "none" : "none"
            transition = slot?.transitionAfter ?? "cut"
            transitionDuration = min(max(0.1, slot?.transitionDurationS ?? 0.1), 1)
        }
    }
}

private struct NativeSelectedTextInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession
    @State private var content = ""
    @State private var style = "Fraunces"
    @State private var start = 0.0
    @State private var end = 1.0
    @State private var size = 48.0
    @State private var width = 1.0
    @State private var x = 0.5
    @State private var y = 0.5
    @State private var alignment = "center"
    @State private var animation = "none"
    @State private var shadow = false
    @State private var behindSubject = false
    @State private var stroke = 0.0
    private let styles = ["Fraunces", "Inter", "Space Grotesk"]

    private var layer: EditorTextElement? { session.document.textElements.first(where: { $0.id == selection.id }) }
    private var canEditTextFields: Bool { nativeEditorEditable(session, keys: ["text_elements"], fallback: session.canEditText) }

    var body: some View {
        Form {
            Section("Text overlay") {
                TextField("Text", text: $content, axis: .vertical)
                    .lineLimit(1...4)
                    .accessibilityIdentifier("native-editor-selected-text-input")
                Button("Apply text") {
                    session.updateTextContent(id: selection.id, content: content)
                }
                .buttonStyle(KriaPrimaryButtonStyle())
                .disabled(!canEditTextFields || content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("native-editor-selected-text-apply")
            }
            Section("Style") {
                Picker("Text style", selection: $style) {
                    ForEach(styles, id: \.self, content: Text.init)
                }
                .pickerStyle(.menu)
                .onChange(of: style) { _, value in
                    session.setTextStyle(id: selection.id, style: value)
                }
                .disabled(!canEditTextFields)
                .accessibilityIdentifier("native-editor-selected-text-style")
            }
            Section("Layout") {
                NativeEditorSlider(session: session, value: $size, in: 8...160, step: 1) { Text("Text size") }
                    .onChange(of: size) { _, value in session.setTextSize(id: selection.id, sizePX: value) }
                    .accessibilityLabel("Text size")
                    .accessibilityIdentifier("native-editor-selected-text-size")
                NativeEditorSlider(session: session, value: $width, in: 0.1...1, step: 0.01) { Text("Text width") }
                    .onChange(of: width) { _, value in session.setTextWidth(id: selection.id, width: value) }
                    .accessibilityLabel("Text width")
                    .accessibilityIdentifier("native-editor-selected-text-width")
                Picker("Alignment", selection: $alignment) {
                    ForEach(["left", "center", "right"], id: \.self) { Text($0.capitalized).tag($0) }
                }
                .onChange(of: alignment) { _, value in session.setTextAlignment(id: selection.id, alignment: value) }
                .accessibilityIdentifier("native-editor-selected-text-alignment")
                Stepper("X \(Int(x * 100))%", value: $x, in: 0...1, step: 0.05)
                    .onChange(of: x) { _, _ in session.setTextPosition(id: selection.id, x: x, y: y) }
                    .accessibilityIdentifier("native-editor-selected-text-position-x")
                Stepper("Y \(Int(y * 100))%", value: $y, in: 0...1, step: 0.05)
                    .onChange(of: y) { _, _ in session.setTextPosition(id: selection.id, x: x, y: y) }
                    .accessibilityIdentifier("native-editor-selected-text-position-y")
                Picker("Animation", selection: $animation) {
                    ForEach(NativeEditorWireContract.textAnimations, id: \.self) { value in
                        Text(nativeEditorWireLabel(value)).tag(value)
                    }
                }
                .onChange(of: animation) { _, value in session.setTextAnimation(id: selection.id, animation: value) }
                .accessibilityIdentifier("native-editor-selected-text-animation")
                if !nativeEditorEditable(session, keys: ["text_elements"], fallback: session.canEditText) {
                    nativeLockedNote(session: session, keys: ["text_elements"], fallback: "Text layout is locked for this render.")
                }
            }
            .disabled(!canEditTextFields)
            Section("Appearance") {
                HStack {
                    Text("Color")
                    Spacer()
                    ForEach(["#111111", "#FFFFFF", "#C5F82A"], id: \.self) { value in
                        Button { session.setTextColor(id: selection.id, color: value) } label: {
                            Circle().fill(nativeColor(value)).frame(width: 24, height: 24)
                                .frame(minWidth: 44, minHeight: 44)
                                .contentShape(Rectangle())
                        }
                        .accessibilityLabel("Text color \(value)")
                    }
                }
                .disabled(!nativeEditorEditable(session, keys: ["text_elements"], fallback: session.canEditText))
                Button("Set highlight color") { session.setTextHighlightColor(id: selection.id, color: "#C5F82A") }
                Toggle("Shadow", isOn: $shadow)
                    .onChange(of: shadow) { _, value in session.setTextShadow(id: selection.id, enabled: value) }
                    .accessibilityIdentifier("native-editor-selected-text-shadow")
                NativeEditorSlider(session: session, value: $stroke, in: 0...12, step: 1) { Text("Stroke") }
                    .onChange(of: stroke) { _, value in session.setTextStroke(id: selection.id, width: value) }
                Toggle("Behind subject", isOn: $behindSubject)
                    .onChange(of: behindSubject) { _, value in session.setTextBehindSubject(id: selection.id, behind: value) }
                    .accessibilityIdentifier("native-editor-selected-text-behind-subject")
                if !nativeEditorEditable(session, keys: ["text_elements"], fallback: session.canEditText) {
                    nativeLockedNote(session: session, keys: ["text_elements"], fallback: "Text appearance is locked for this render.")
                }
            }
            .disabled(!canEditTextFields)
            Section("Timing") {
                if let record = session.document.textElements.first(where: { $0.id == selection.id }) {
                    NativeEditorSlider(session: session, value: $start, in: 0...max(record.endS, session.duration), step: 0.1) { Text("In") }
                        .onChange(of: start) { _, value in session.updateTextTiming(id: selection.id, startS: value) }
                        .accessibilityIdentifier("native-editor-selected-text-start")
                    NativeEditorSlider(session: session, value: $end, in: max(start, 0.1)...max(record.endS, session.duration), step: 0.1) { Text("Out") }
                        .onChange(of: end) { _, value in session.updateTextTiming(id: selection.id, endS: value) }
                        .accessibilityIdentifier("native-editor-selected-text-end")
                    LabeledContent("Range", value: "\(nativeTimecode(start)) – \(nativeTimecode(end))")
                } else {
                    Text("Timing is not available for this text layer.")
                        .foregroundStyle(KriaColor.zinc)
                }
            }
            .disabled(!canEditTextFields)
            if !session.canEditText {
                Section { Label("Text editing is locked for this render.", systemImage: "lock") }
            }
        }
        .onAppear {
            content = layer?.text ?? ""
            style = nativeString(layer?.raw["font_family"]) ?? "Fraunces"
            if let record = session.document.textElements.first(where: { $0.id == selection.id }) {
                start = record.startS; end = max(record.startS + 0.1, record.endS)
                size = nativeNumber(record.raw["size_px"]) ?? 48
                width = nativeNumber(record.raw["max_width_frac"]) ?? 1
                x = nativeNumber(record.raw["x_frac"]) ?? 0.5
                y = nativeNumber(record.raw["y_frac"]) ?? 0.5
                alignment = nativeString(record.raw["alignment"]) ?? "center"
                animation = nativeString(record.raw["effect"]) ?? "none"
                shadow = nativeBool(record.raw["shadow_enabled"]) ?? false
                behindSubject = nativeBool(record.raw["behind_subject"]) ?? false
                stroke = nativeNumber(record.raw["stroke_width"]) ?? 0
            }
        }
    }
}

private struct NativeSelectedCaptionInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession
    @State private var style = "Sentence"
    @State private var content = ""
    @State private var start = 0.0
    @State private var end = 1.0
    @State private var font = "Inter"
    @State private var size = 42.0
    @State private var y = 0.82
    @State private var shadow = false
    @State private var stroke = 0.0
    private let styles = ["Sentence", "Word"]

    private var cue: EditorCaptionCue? {
        session.document.captionCues.first(where: { $0.id == selection.id })
    }

    var body: some View {
        Form {
            Section("Caption") {
                TextField("Caption text", text: $content, axis: .vertical)
                    .lineLimit(1...4)
                    .accessibilityIdentifier("native-editor-selected-caption-input")
                Button("Apply caption") { session.updateCaptionCue(id: selection.id, text: content) }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(!session.canEditCaptions || content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if let cue {
                    NativeEditorSlider(session: session, value: $start, in: 0...max(cue.endS, session.duration), step: 0.1) { Text("In") }
                        .onChange(of: start) { _, value in session.updateCaptionCue(id: selection.id, startS: value) }
                    NativeEditorSlider(session: session, value: $end, in: max(start, 0.1)...max(cue.endS, session.duration), step: 0.1) { Text("Out") }
                        .onChange(of: end) { _, value in session.updateCaptionCue(id: selection.id, endS: value) }
                    LabeledContent("Range", value: "\(nativeTimecode(start)) – \(nativeTimecode(end))")
                }
            }
            .disabled(!session.canEditCaptions)
            Section("Caption settings") {
                Toggle("Captions", isOn: Binding(
                    get: { session.draft.captions.enabled },
                    set: { _ in session.toggleCaptions() }
                ))
                .disabled(!session.canEditCaptions)
                .accessibilityIdentifier("native-editor-selected-caption-toggle")

                Picker("Style", selection: $style) {
                    ForEach(styles, id: \.self, content: Text.init)
                }
                .pickerStyle(.menu)
                .onChange(of: style) { _, value in session.setCaptionStyle(value.lowercased()) }
                .disabled(!session.canEditCaptions)
                .accessibilityIdentifier("native-editor-selected-caption-style")
                Picker("Font", selection: $font) {
                    ForEach(NativeEditorWireContract.captionFonts, id: \.self, content: Text.init)
                }
                .onChange(of: font) { _, value in session.setCaptionFont(value) }
                NativeEditorSlider(session: session, value: $size, in: 36...160, step: 1) { Text("Caption size") }
                    .onChange(of: size) { _, value in session.setCaptionSize(value) }
                NativeEditorSlider(session: session, value: $y, in: 0.30...0.90, step: 0.01) { Text("Caption position") }
                    .onChange(of: y) { _, value in session.setCaptionPositionY(value) }
                HStack {
                    Text("Color"); Spacer()
                    ForEach(["#FFFFFF", "#C5F82A", "#111111"], id: \.self) { value in
                        Button { session.setCaptionColor(value) } label: {
                            Circle().fill(nativeColor(value)).frame(width: 24, height: 24)
                                .frame(minWidth: 44, minHeight: 44)
                                .contentShape(Rectangle())
                        }
                            .accessibilityLabel("Caption color \(value)")
                    }
                }
                Button("Set highlight color") { session.setCaptionHighlightColor("#C5F82A") }
                Toggle("Shadow", isOn: $shadow)
                    .onChange(of: shadow) { _, value in session.setCaptionShadowEnabled(value) }
                NativeEditorSlider(session: session, value: $stroke, in: 0...12, step: 1) { Text("Caption stroke") }
                    .onChange(of: stroke) { _, value in session.setCaptionStrokeWidth(value) }
                if !nativeEditorEditable(session, keys: ["caption_meta"], fallback: session.canEditCaptions) {
                    nativeLockedNote(session: session, keys: ["caption_meta"], fallback: "Caption appearance is locked for this render.")
                }
            }
            .disabled(!nativeEditorEditable(session, keys: ["caption_meta"], fallback: session.canEditCaptions))
            if !session.canEditCaptions {
                Section { Label("Caption changes are locked for this render.", systemImage: "lock") }
            }
        }
        .onAppear {
            style = session.draft.captions.style.capitalized
            content = cue?.text ?? ""
            start = cue?.startS ?? 0; end = cue?.endS ?? 1
            font = nativeString(session.document.captionMeta["font"]) ?? "Inter"
            size = min(max(36, nativeNumber(session.document.captionMeta["size_px"]) ?? 42), 160)
            y = min(max(0.30, nativeNumber(session.document.captionMeta["y_frac"]) ?? 0.82), 0.90)
            shadow = nativeBool(session.document.captionMeta["shadow_enabled"]) ?? false
            stroke = nativeNumber(session.document.captionMeta["stroke_width"]) ?? 0
        }
    }
}

private struct NativeSelectedMusicInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession
    @State private var volume = 0.75
    @State private var originalVolume = 1.0
    @State private var start = 0.0
    @State private var alignment = "preserve_cuts"
    @State private var trackID = ""

    private var music: EditorMusic? {
        guard session.document.music?.trackID == selection.id else { return nil }
        return session.document.music
    }

    var body: some View {
        Form {
            Section("Music") {
                TextField("Track ID", text: $trackID)
                    .accessibilityIdentifier("native-editor-selected-music-track")
                Button("Use track") { session.setMusic(trackID: trackID, startS: start, alignment: alignment) }
                    .disabled(!session.canEditMix || trackID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if let music { LabeledContent("Starts", value: nativeTimecode(music.startS)) }
                NativeEditorSlider(session: session, value: $start, in: 0...max(session.duration, 0.1), step: 0.1) { Text("Music start") }
                    .onChange(of: start) { _, value in session.setMusicWindow(startS: value) }
                Picker("Alignment", selection: $alignment) {
                    ForEach(NativeEditorWireContract.musicAlignments, id: \.self) { value in
                        Text(nativeEditorWireLabel(value)).tag(value)
                    }
                }
                .onChange(of: alignment) { _, value in session.setMusicWindow(alignment: value) }
                HStack {
                    Image(systemName: "speaker.wave.2")
                    NativeEditorSlider(session: session, value: $volume, in: 0...1) { Text("Music volume") }
                        .onChange(of: volume) { _, value in session.setMusicVolume(value) }
                    Text("\(Int(volume * 100))%")
                        .font(.system(.caption, design: .monospaced))
                        .frame(width: 42, alignment: .trailing)
                }
                .accessibilityIdentifier("native-editor-selected-music-volume")
                .disabled(!session.canEditMix)
                Button("Remove music", role: .destructive) { session.removeMusic(); session.select(nil, seekToStart: false) }
                    .disabled(!session.canEditMix)
                    .accessibilityIdentifier("native-editor-selected-music-remove")
            }
            .disabled(!session.canEditMix)
            Section("Original mix") {
                NativeEditorSlider(session: session, value: $originalVolume, in: 0...1, step: 0.01) { Text("Original audio level") }
                    .onChange(of: originalVolume) { _, value in session.setOriginalMixLevel(value) }
                    .accessibilityIdentifier("native-editor-original-mix")
                LabeledContent("Original audio", value: "\(Int(originalVolume * 100))%")
            }
            .disabled(!session.canEditMix)
            if !session.canEditMix {
                Section { Label("Music level is locked for this render.", systemImage: "lock") }
            }
        }
        .onAppear {
            volume = session.draft.music?.volume ?? 0
            originalVolume = nativeNumber(session.document.mix["original_level"]) ?? 1
            start = music?.startS ?? 0
            alignment = NativeEditorWireContract.musicAlignments.contains(music?.alignment ?? "") ? music?.alignment ?? "preserve_cuts" : "preserve_cuts"
            trackID = music?.trackID ?? selection.id
        }
    }
}

private struct NativeSelectedEffectInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession

    @State private var start = 0.0
    @State private var end = 1.0
    @State private var point = 0.0
    @State private var trimStart = 0.0
    @State private var trimEnd = 0.3
    @State private var gain = 1.0
    @State private var x = 0.5
    @State private var y = 0.5
    @State private var scale = 1.0
    @State private var displayMode = "pip"
    @State private var preset = "Default"
    @State private var visualDisplayMode = "fullscreen"
    @State private var visualFitMode = "contain"
    @State private var visualFocalX = 0.5
    @State private var visualFocalY = 0.5
    @State private var visualZoom = 1.0
    @State private var visualOverlayX = 0.5
    @State private var visualOverlayY = 0.5
    @State private var visualOverlayScale = 0.35
    @State private var intensity = 0.04
    @State private var easing = "sine_pulse"
    @State private var carouselPosition = "middle"

    private var title: String {
        switch selection.kind {
        case .soundEffect: return "Sound effect"
        case .mediaOverlay: return "Media overlay"
        case .visualBlock: return "Visual block"
        case .motionScene: return "Motion scene"
        case .cameraEffect: return "Camera effect"
        case .carousel: return "Carousel moment"
        default: return "Selected item"
        }
    }

    private var record: EditorTimedEffect? {
        switch selection.kind {
        case .soundEffect: return session.document.soundEffects.first(where: { $0.id == selection.id })
        case .mediaOverlay: return session.document.mediaOverlays.first(where: { $0.id == selection.id })
        default: return nil
        }
    }

    private var visualBlock: EditorVisualBlock? {
        session.document.visualBlocks.first(where: { $0.id == selection.id })
    }

    private var timing: (Double, Double)? {
        if let record { return (record.startS, record.endS) }
        switch selection.kind {
        case .visualBlock: if let value = session.document.visualBlocks.first(where: { $0.id == selection.id }) { return (value.startS, value.endS) }
        case .motionScene: if let value = session.document.motionScenes.first(where: { $0.id == selection.id }) { return (value.startS, value.endS) }
        case .cameraEffect: if let value = session.document.cameraEffects.first(where: { $0.id == selection.id }) { return (value.startS, value.endS) }
        case .carousel:
            if nativeString(session.document.carouselMoment?["id"]) == selection.id,
               let start = nativeNumber(session.document.carouselMoment?["start_s"]),
               let end = nativeNumber(session.document.carouselMoment?["end_s"]) { return (start, end) }
        default: break
        }
        return nil
    }

    private var capabilityKeys: [String] {
        switch selection.kind {
        case .soundEffect: return ["sfx", "sound_effects"]
        case .mediaOverlay: return ["overlays", "media_overlays"]
        case .visualBlock: return ["visual_blocks", "lanes.visual_blocks"]
        case .motionScene: return ["motion_scenes", "lanes.motion_scenes"]
        case .cameraEffect: return ["camera_effects", "lanes.camera_effects"]
        case .carousel: return ["carousel", "carousel_moment"]
        default: return []
        }
    }

    private var editable: Bool {
        switch selection.kind {
        case .soundEffect: return nativeEditorEditable(session, keys: ["sfx", "sound_effects"], fallback: session.canEdit(.soundEffects))
        case .mediaOverlay: return nativeEditorEditable(session, keys: ["overlays", "media_overlays"], fallback: session.canEdit(.mediaOverlays))
        case .visualBlock: return nativeEditorEditable(session, keys: ["visual_blocks", "lanes.visual_blocks"], fallback: session.canEdit(.visualBlocks))
        case .motionScene: return nativeEditorEditable(session, keys: ["motion_scenes", "lanes.motion_scenes"], fallback: session.canEdit(.motionScenes)) && !session.isMotionSceneReadOnly(id: selection.id)
        case .cameraEffect: return nativeEditorEditable(session, keys: ["camera_effects", "lanes.camera_effects"], fallback: session.canEdit(.cameraEffects))
        case .carousel: return nativeEditorEditable(session, keys: ["carousel", "carousel_moment"], fallback: session.canEdit(.carouselMoment))
        default: return false
        }
    }

    private var canEditTiming: Bool {
        let keys: [String]
        let section: EditorSection
        switch selection.kind {
        case .soundEffect: keys = ["sfx.timing", "sfx"]; section = .soundEffects
        case .mediaOverlay: keys = ["overlays.timing", "overlays"]; section = .mediaOverlays
        case .visualBlock:
            guard visualBlock?.kind != "montage" else { return false }
            keys = ["visual_blocks.timing", "visual_blocks"]; section = .visualBlocks
        case .motionScene: keys = ["motion_scenes.timing", "motion_scenes"]; section = .motionScenes
        case .cameraEffect: keys = ["camera_effects.timing", "camera_effects"]; section = .cameraEffects
        default: return false
        }
        return nativeEditorEditable(session, keys: keys, fallback: session.canEdit(section)) && editable
    }

    private var canEditLayers: Bool {
        if selection.kind == .visualBlock, visualBlock?.kind != "media" { return false }
        return nativeEditorEditable(session, keys: ["layers.reorder", "layer_order"], fallback: session.canEdit(.timeline)) && editable
    }

    private func changeTiming(start newStart: Double? = nil, end newEnd: Double? = nil) {
        switch selection.kind {
        case .soundEffect: session.setSoundEffectTiming(id: selection.id, startS: newStart, endS: newEnd)
        case .mediaOverlay: session.setMediaOverlayTiming(id: selection.id, startS: newStart, endS: newEnd)
        case .visualBlock: session.setVisualBlockTiming(id: selection.id, startS: newStart, endS: newEnd)
        case .motionScene: session.setMotionSceneTiming(id: selection.id, startS: newStart, endS: newEnd)
        case .cameraEffect: session.setCameraEffectTiming(id: selection.id, startS: newStart, endS: newEnd)
        default: break
        }
    }

    @ViewBuilder
    private var timingSection: some View {
        if let timing {
            Section("Timing") {
                if selection.kind == .soundEffect {
                    NativeEditorSlider(session: session, value: $point, in: 0...max(session.duration, 0.1), step: 0.05) { Text("Placement") }
                        .onChange(of: point) { _, value in session.setSoundEffectTiming(id: selection.id, atS: value) }
                        .accessibilityIdentifier("native-editor-selected-sfx-placement")
                } else {
                    NativeEditorSlider(session: session, value: $start, in: 0...max(end, session.duration), step: 0.05) { Text("In") }
                        .onChange(of: start) { _, value in changeTiming(start: value) }
                        .accessibilityIdentifier("native-editor-selected-\(selection.kind.rawValue)-start")
                    NativeEditorSlider(session: session, value: $end, in: max(start + 0.1, 0.1)...max(start + 0.1, session.duration), step: 0.05) { Text("Out") }
                        .onChange(of: end) { _, value in changeTiming(end: value) }
                        .accessibilityIdentifier("native-editor-selected-\(selection.kind.rawValue)-end")
                }
                LabeledContent("Range", value: "\(nativeTimecode(timing.0)) – \(nativeTimecode(timing.1))")
            }
            .disabled(!canEditTiming)
        }
    }

    @ViewBuilder
    private var sfxBody: some View {
        Section("Sound") {
            NativeEditorSlider(session: session, value: $trimStart, in: 0...max(trimEnd - 0.05, 0.05), step: 0.05) { Text("Trim in") }
                .onChange(of: trimStart) { _, value in session.setSoundEffectTrim(id: selection.id, trimStartS: value, trimEndS: trimEnd) }
                .accessibilityIdentifier("native-editor-selected-sfx-trim-start")
            NativeEditorSlider(session: session, value: $trimEnd, in: max(trimStart + 0.05, 0.05)...max(trimStart + 0.05, trimEnd + 1), step: 0.05) { Text("Trim out") }
                .onChange(of: trimEnd) { _, value in session.setSoundEffectTrim(id: selection.id, trimStartS: trimStart, trimEndS: value) }
                .accessibilityIdentifier("native-editor-selected-sfx-trim-end")
            NativeEditorSlider(session: session, value: $gain, in: 0...2, step: 0.05) { Text("Gain") }
                .onChange(of: gain) { _, value in session.setSoundEffectGain(id: selection.id, gain: value) }
                .accessibilityIdentifier("native-editor-selected-sfx-gain")
            LabeledContent("Gain", value: "\(Int(gain * 100))%")
        }
        .disabled(!editable)
        Section {
            Button("Remove sound effect", role: .destructive) {
                session.removeSoundEffect(id: selection.id)
                session.select(nil, seekToStart: false)
            }
            .disabled(!editable)
            .accessibilityIdentifier("native-editor-selected-sfx-remove")
        }
    }

    @ViewBuilder
    private var overlayBody: some View {
        Section("Overlay") {
            Picker("Display mode", selection: $displayMode) {
                Text("Picture in picture").tag("pip")
                Text("Fullscreen").tag("fullscreen")
            }
            .onChange(of: displayMode) { _, value in session.setMediaOverlayDisplayMode(id: selection.id, mode: value) }
            .accessibilityIdentifier("native-editor-selected-overlay-display-mode")
            NativeEditorSlider(session: session, value: $x, in: 0...1, step: 0.01) { Text("Horizontal position") }
                .onChange(of: x) { _, _ in session.setMediaOverlayPosition(id: selection.id, x: x, y: y) }
                .accessibilityIdentifier("native-editor-selected-overlay-position-x")
            NativeEditorSlider(session: session, value: $y, in: 0...1, step: 0.01) { Text("Vertical position") }
                .onChange(of: y) { _, _ in session.setMediaOverlayPosition(id: selection.id, x: x, y: y) }
                .accessibilityIdentifier("native-editor-selected-overlay-position-y")
            NativeEditorSlider(session: session, value: $scale, in: 0.05...1, step: 0.05) { Text("Scale") }
                .onChange(of: scale) { _, value in session.setMediaOverlayScale(id: selection.id, scale: value) }
                .accessibilityIdentifier("native-editor-selected-overlay-scale")
        }
        .disabled(!editable)
        layerOrderSection
        Section {
            Button("Remove overlay", role: .destructive) {
                session.removeMediaOverlay(id: selection.id)
                session.select(nil, seekToStart: false)
            }
            .disabled(!editable)
            .accessibilityIdentifier("native-editor-selected-overlay-remove")
        }
    }

    @ViewBuilder
    private var visualBody: some View {
        if visualBlock?.kind == "text_card" {
            Section("Text card") {
                Picker("Preset", selection: $preset) {
                    ForEach(["Default", "text-card-default", "text-card-bold"], id: \.self) { Text($0.replacingOccurrences(of: "-", with: " ").capitalized).tag($0) }
                }
                .onChange(of: preset) { _, value in session.setVisualBlockPreset(id: selection.id, preset: value == "Default" ? nil : value) }
                .accessibilityIdentifier("native-editor-selected-visual-preset")
            }
            .disabled(!editable)
        } else if visualBlock?.kind == "media" {
            Section("Media transform") {
                Picker("Display mode", selection: $visualDisplayMode) {
                    Text("Fullscreen").tag("fullscreen")
                    Text("Overlay").tag("overlay")
                }
                .onChange(of: visualDisplayMode) { _, value in session.setVisualBlockDisplayMode(id: selection.id, mode: value) }
                .accessibilityIdentifier("native-editor-selected-visual-display-mode")
                Picker("Fit", selection: $visualFitMode) {
                    Text("Contain").tag("contain")
                    Text("Cover").tag("cover")
                }
                .onChange(of: visualFitMode) { _, value in session.setVisualBlockTransform(id: selection.id, fitMode: value) }
                .accessibilityIdentifier("native-editor-selected-visual-fit")
                NativeEditorSlider(session: session, value: $visualFocalX, in: 0...1, step: 0.01) { Text("Focal horizontal") }
                    .onChange(of: visualFocalX) { _, value in session.setVisualBlockTransform(id: selection.id, focalX: value) }
                NativeEditorSlider(session: session, value: $visualFocalY, in: 0...1, step: 0.01) { Text("Focal vertical") }
                    .onChange(of: visualFocalY) { _, value in session.setVisualBlockTransform(id: selection.id, focalY: value) }
                NativeEditorSlider(session: session, value: $visualZoom, in: 1...4, step: 0.05) { Text("Zoom") }
                    .onChange(of: visualZoom) { _, value in session.setVisualBlockTransform(id: selection.id, zoom: value) }
                    .accessibilityIdentifier("native-editor-selected-visual-zoom")
                if visualDisplayMode == "overlay" {
                    NativeEditorSlider(session: session, value: $visualOverlayX, in: 0...1, step: 0.01) { Text("Overlay horizontal") }
                        .onChange(of: visualOverlayX) { _, value in session.setVisualBlockOverlayLayout(id: selection.id, x: value) }
                    NativeEditorSlider(session: session, value: $visualOverlayY, in: 0...1, step: 0.01) { Text("Overlay vertical") }
                        .onChange(of: visualOverlayY) { _, value in session.setVisualBlockOverlayLayout(id: selection.id, y: value) }
                    NativeEditorSlider(session: session, value: $visualOverlayScale, in: 0.05...1, step: 0.05) { Text("Overlay scale") }
                        .onChange(of: visualOverlayScale) { _, value in session.setVisualBlockOverlayLayout(id: selection.id, scale: value) }
                }
            }
            .disabled(!editable)
            layerOrderSection
        } else {
            Section("Montage") {
                Label("Shot choreography is preserved and previewed in the final render.", systemImage: "lock")
                    .foregroundStyle(KriaColor.zinc)
            }
        }
        Section {
            Button("Remove visual block", role: .destructive) {
                session.removeVisualBlock(id: selection.id)
                session.select(nil, seekToStart: false)
            }
            .disabled(!editable)
            .accessibilityIdentifier("native-editor-selected-visual-remove")
        }
    }

    @ViewBuilder
    private var layerOrderSection: some View {
        Section("Layer order") {
            Button("Bring forward") { session.moveLayer(selection: selection, by: 1) }
                .accessibilityIdentifier("native-editor-selected-layer-forward")
            Button("Send backward") { session.moveLayer(selection: selection, by: -1) }
                .accessibilityIdentifier("native-editor-selected-layer-backward")
        }
        .disabled(!canEditLayers)
    }

    @ViewBuilder
    private var advancedBody: some View {
        switch selection.kind {
        case .motionScene:
            Section("Motion") {
                LabeledContent("Preset", value: preset)
                if let reason = session.motionRuntimeMismatchReason(id: selection.id) {
                    Label(reason, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(KriaColor.zinc)
                }
            }
        case .cameraEffect:
            Section("Camera effect") {
                NativeEditorSlider(session: session, value: $intensity, in: 0...0.08, step: 0.001) { Text("Intensity") }
                    .onChange(of: intensity) { _, value in session.setCameraEffectIntensity(id: selection.id, intensity: value) }
                    .accessibilityIdentifier("native-editor-selected-camera-intensity")
                Picker("Easing", selection: $easing) {
                    Text("Sine pulse").tag("sine_pulse")
                }
                .onChange(of: easing) { _, value in session.setCameraEffectEasing(id: selection.id, easing: value) }
                .accessibilityIdentifier("native-editor-selected-camera-easing")
            }
            .disabled(!editable)
        case .carousel:
            Section("Carousel moment") {
                Picker("Position", selection: $carouselPosition) {
                    ForEach(["intro", "middle", "outro"], id: \.self) { Text($0.capitalized).tag($0) }
                }
                .onChange(of: carouselPosition) { _, value in session.setCarouselMomentPosition(value) }
                .accessibilityIdentifier("native-editor-selected-carousel-position")
                Button("Remove carousel moment", role: .destructive) {
                    session.removeCarouselMoment()
                    session.select(nil, seekToStart: false)
                }
                .disabled(!editable)
                .accessibilityIdentifier("native-editor-selected-carousel-remove")
            }
            .disabled(!editable)
        default: EmptyView()
        }
    }

    private var detail: (String, String)? {
        switch selection.kind {
        case .visualBlock:
            guard let value = session.document.visualBlocks.first(where: { $0.id == selection.id }) else { return nil }
            return ("Preset", value.raw["style_preset_id"]?.stringValue ?? value.raw["preset"]?.stringValue ?? value.kind)
        case .motionScene:
            guard let value = session.document.motionScenes.first(where: { $0.id == selection.id }) else { return nil }
            return ("Preset", value.preset ?? "Default")
        case .cameraEffect:
            guard let value = session.document.cameraEffects.first(where: { $0.id == selection.id }) else { return nil }
            return ("Effect", value.effect ?? "Default")
        case .carousel:
            return ("Moment", nativeString(session.document.carouselMoment?["id"]) ?? selection.id)
        default: return nil
        }
    }

    var body: some View {
        Form {
            Section {
                Label(title, systemImage: editable ? "slider.horizontal.3" : "lock")
                    .font(KriaFont.display(25))
                if let record, let kind = record.kind { LabeledContent("Kind", value: kind) }
                if let detail { LabeledContent(detail.0, value: detail.1) }
                if !editable {
                    nativeLockedNote(session: session, keys: capabilityKeys, fallback: "This object is read-only until its renderer capability is available.")
                }
            }
            timingSection
            switch selection.kind {
            case .soundEffect: sfxBody
            case .mediaOverlay: overlayBody
            case .visualBlock: visualBody
            case .motionScene, .cameraEffect, .carousel: advancedBody
            default: EmptyView()
            }
        }
        .onAppear { loadValues() }
    }

    private func loadValues() {
        if let timing { start = timing.0; end = max(timing.0 + 0.1, timing.1); point = timing.0 }
        switch selection.kind {
        case .soundEffect:
            trimStart = nativeNumber(record?.raw["trim_start_s"]) ?? 0
            trimEnd = max(trimStart + 0.05, nativeNumber(record?.raw["trim_end_s"]) ?? end)
            gain = nativeNumber(record?.raw["gain"] ?? record?.raw["gain_db"] ?? record?.raw["volume"]) ?? 1
        case .mediaOverlay:
            x = nativeNumber(record?.raw["x_frac"] ?? record?.raw["position_x"]) ?? 0.5
            y = nativeNumber(record?.raw["y_frac"] ?? record?.raw["position_y"]) ?? 0.5
            scale = nativeNumber(record?.raw["scale"] ?? record?.raw["zoom"]) ?? 1
            displayMode = nativeString(record?.raw["display_mode"]) ?? "pip"
        case .visualBlock:
            preset = nativeString(session.document.visualBlocks.first(where: { $0.id == selection.id })?.raw["style_preset_id"]) ?? "Default"
            let raw = session.document.visualBlocks.first(where: { $0.id == selection.id })?.raw
            let transform = nativeObject(raw?["transform"])
            visualDisplayMode = nativeString(raw?["display_mode"]) ?? "fullscreen"
            visualFitMode = nativeString(transform?["fit_mode"]) ?? "contain"
            visualFocalX = nativeNumber(transform?["focal_x"]) ?? 0.5
            visualFocalY = nativeNumber(transform?["focal_y"]) ?? 0.5
            visualZoom = nativeNumber(transform?["zoom"]) ?? 1
            visualOverlayX = nativeNumber(raw?["x_frac"]) ?? 0.5
            visualOverlayY = nativeNumber(raw?["y_frac"]) ?? 0.5
            visualOverlayScale = nativeNumber(raw?["scale"]) ?? 0.35
        case .motionScene:
            preset = session.document.motionScenes.first(where: { $0.id == selection.id })?.preset?.capitalized ?? "Default"
        case .cameraEffect:
            let raw = session.document.cameraEffects.first(where: { $0.id == selection.id })?.raw
            intensity = nativeNumber(raw?["intensity"]) ?? 0.04
            easing = nativeString(raw?["easing"]) ?? "sine_pulse"
        case .carousel:
            carouselPosition = nativeString(session.document.carouselMoment?["position"]) ?? "middle"
        default: break
        }
    }
}

@ViewBuilder
@MainActor
private func nativeLockedNote(session: NativeEditorSession, keys: [String], fallback: String) -> some View {
    let reason = keys.compactMap { session.document.capabilities[$0]?.reason }.first ?? fallback
    Label(reason, systemImage: "lock")
        .font(KriaFont.body(12))
        .foregroundStyle(KriaColor.zinc)
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityIdentifier("native-editor-capability-reason")
}

@ViewBuilder
@MainActor
private func nativeLockedNoteIfNeeded(session: NativeEditorSession, keys: [String], fallback: String) -> some View {
    if let capability = keys.compactMap({ session.document.capabilities[$0] }).first, !capability.editable {
        nativeLockedNote(session: session, keys: keys, fallback: fallback)
    }
}

@MainActor
private func nativeEditorEditable(_ session: NativeEditorSession, keys: [String], fallback: Bool) -> Bool {
    guard let capability = keys.compactMap({ session.document.capabilities[$0] }).first else { return fallback }
    return capability.editable && fallback
}

private func nativeNumber(_ value: JSONValue?) -> Double? {
    if case let .number(value) = value { return value }
    return nil
}

private func nativeString(_ value: JSONValue?) -> String? {
    if case let .string(value) = value { return value }
    return nil
}

private func nativeObject(_ value: JSONValue?) -> [String: JSONValue]? {
    if case let .object(value) = value { return value }
    return nil
}

private func nativeBool(_ value: JSONValue?) -> Bool? {
    if case let .bool(value) = value { return value }
    return nil
}

private func nativeColor(_ value: String) -> Color {
    switch value.uppercased() {
    case "#FFFFFF": return .white
    case "#C5F82A": return KriaColor.lime
    default: return KriaColor.ink
    }
}
