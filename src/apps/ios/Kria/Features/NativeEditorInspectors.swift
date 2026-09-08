import SwiftUI

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
            NativeSelectedUnavailableInspector(selection: selection, session: session)
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
    @State private var look = "Original"
    @State private var transition = "cut"
    @State private var transitionDuration = 0.0

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
                    Slider(value: $sourceStart, in: 0...max(sourceEnd - 0.1, 0.1), step: 0.1) { Text("Source in") }
                        .onChange(of: sourceStart) { _, value in session.setClipSourceWindow(clipID: selection.id, startS: value, endS: sourceEnd) }
                    Slider(value: $sourceEnd, in: min(sourceStart + 0.1, clip.sourceDuration ?? sourceEnd)...max(clip.sourceDuration ?? sourceEnd, sourceStart + 0.1), step: 0.1) { Text("Source out") }
                        .onChange(of: sourceEnd) { _, value in session.setClipSourceWindow(clipID: selection.id, startS: sourceStart, endS: value) }
                }
            }
            .disabled(!session.canEditTimeline)
            Section("Look and transition") {
                Picker("Look", selection: $look) {
                    ForEach(["Original", "Warm", "Cool", "Mono"], id: \.self, content: Text.init)
                }
                .onChange(of: look) { _, value in session.setClipLookPreset(clipID: selection.id, preset: value == "Original" ? nil : value.lowercased()) }
                Picker("Transition", selection: $transition) {
                    ForEach(["cut", "crossfade", "dip"], id: \.self) { Text($0.capitalized).tag($0) }
                }
                .onChange(of: transition) { _, value in session.setClipTransition(clipID: selection.id, transition: value, durationS: transitionDuration) }
                Slider(value: $transitionDuration, in: 0...1, step: 0.05) { Text("Transition duration") }
                    .onChange(of: transitionDuration) { _, value in session.setClipTransition(clipID: selection.id, transition: transition, durationS: value) }
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
            look = slot?.lookPreset?.capitalized ?? "Original"
            transition = slot?.transitionAfter ?? "cut"
            transitionDuration = slot?.transitionDurationS ?? 0
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
                Slider(value: $size, in: 8...160, step: 1) { Text("Text size") }
                    .onChange(of: size) { _, value in session.setTextSize(id: selection.id, sizePX: value) }
                    .accessibilityLabel("Text size")
                    .accessibilityIdentifier("native-editor-selected-text-size")
                Slider(value: $width, in: 0.1...1, step: 0.01) { Text("Text width") }
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
                    ForEach(["none", "fade", "pop", "slide"], id: \.self) { Text($0.capitalized).tag($0) }
                }
                .onChange(of: animation) { _, value in session.setTextAnimation(id: selection.id, animation: value == "none" ? nil : value) }
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
                        }
                        .accessibilityLabel("Text color \(value)")
                    }
                }
                .disabled(!nativeEditorEditable(session, keys: ["text_elements"], fallback: session.canEditText))
                Button("Set highlight color") { session.setTextHighlightColor(id: selection.id, color: "#C5F82A") }
                Toggle("Shadow", isOn: $shadow)
                    .onChange(of: shadow) { _, value in session.setTextShadow(id: selection.id, enabled: value) }
                    .accessibilityIdentifier("native-editor-selected-text-shadow")
                Slider(value: $stroke, in: 0...12, step: 1) { Text("Stroke") }
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
                    Slider(value: $start, in: 0...max(record.endS, session.duration), step: 0.1) { Text("In") }
                        .onChange(of: start) { _, value in session.updateTextTiming(id: selection.id, startS: value) }
                        .accessibilityIdentifier("native-editor-selected-text-start")
                    Slider(value: $end, in: max(start, 0.1)...max(record.endS, session.duration), step: 0.1) { Text("Out") }
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
                size = nativeNumber(record.raw["font_size_px"]) ?? 48
                width = nativeNumber(record.raw["width"]) ?? 1
                x = nativeNumber(record.raw["x_frac"]) ?? 0.5
                y = nativeNumber(record.raw["y_frac"]) ?? 0.5
                alignment = nativeString(record.raw["alignment"]) ?? "center"
                animation = nativeString(record.raw["animation"]) ?? "none"
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
                    Slider(value: $start, in: 0...max(cue.endS, session.duration), step: 0.1) { Text("In") }
                        .onChange(of: start) { _, value in session.updateCaptionCue(id: selection.id, startS: value) }
                    Slider(value: $end, in: max(start, 0.1)...max(cue.endS, session.duration), step: 0.1) { Text("Out") }
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
                    ForEach(["Inter", "Fraunces", "Space Grotesk"], id: \.self, content: Text.init)
                }
                .onChange(of: font) { _, value in session.setCaptionFont(value) }
                Slider(value: $size, in: 12...120, step: 1) { Text("Caption size") }
                    .onChange(of: size) { _, value in session.setCaptionSize(value) }
                Slider(value: $y, in: 0...1, step: 0.01) { Text("Caption position") }
                    .onChange(of: y) { _, value in session.setCaptionPositionY(value) }
                HStack {
                    Text("Color"); Spacer()
                    ForEach(["#FFFFFF", "#C5F82A", "#111111"], id: \.self) { value in
                        Button { session.setCaptionColor(value) } label: { Circle().fill(nativeColor(value)).frame(width: 24, height: 24) }
                            .accessibilityLabel("Caption color \(value)")
                    }
                }
                Button("Set highlight color") { session.setCaptionHighlightColor("#C5F82A") }
                Toggle("Shadow", isOn: $shadow)
                    .onChange(of: shadow) { _, value in session.setCaptionShadowEnabled(value) }
                Slider(value: $stroke, in: 0...12, step: 1) { Text("Caption stroke") }
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
            size = nativeNumber(session.document.captionMeta["size_px"]) ?? 42
            y = nativeNumber(session.document.captionMeta["y_frac"]) ?? 0.82
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
    @State private var alignment = "start"
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
                Slider(value: $start, in: 0...max(session.duration, 0.1), step: 0.1) { Text("Music start") }
                    .onChange(of: start) { _, value in session.setMusicWindow(startS: value) }
                Picker("Alignment", selection: $alignment) {
                    ForEach(["start", "beat", "best_section"], id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ").capitalized).tag($0) }
                }
                .onChange(of: alignment) { _, value in session.setMusicWindow(alignment: value) }
                HStack {
                    Image(systemName: "speaker.wave.2")
                    Slider(value: $volume, in: 0...1) { Text("Music volume") }
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
                Slider(value: $originalVolume, in: 0...1, step: 0.01) { Text("Original audio level") }
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
            alignment = music?.alignment ?? "start"
            trackID = music?.trackID ?? selection.id
        }
    }
}

private struct NativeSelectedUnavailableInspector: View {
    let selection: EditorSelection
    @ObservedObject var session: NativeEditorSession

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

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(title, systemImage: "lock")
                .font(KriaFont.display(25))
            Text("This lane is visible in the timeline, but its renderer contract is not editable in the native editor yet. Your existing effect stays unchanged.")
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.zinc)
            if let item = session.timelineItems.first(where: { $0.selection == selection }) {
                LabeledContent("Timing", value: "\(nativeTimecode(item.start)) – \(nativeTimecode(item.end))")
                    .font(KriaFont.body(13))
            }
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
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
