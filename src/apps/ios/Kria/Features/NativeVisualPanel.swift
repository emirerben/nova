@preconcurrency import AVFoundation
import SwiftUI

struct NativeVisualPanel: View {
    enum Tab: String, CaseIterable { case edit = "Edit", animation = "Animation" }
    enum Category: String, CaseIterable { case media = "Media", cards = "Text cards", motion = "Motion", camera = "Camera FX" }
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let onDone: () -> Void
    @State private var tab: Tab = .edit
    @State private var category: Category = .media
    @State private var showsImporter = false
    @State private var cardPreset: String?
    @State private var cardText = ""
    @State private var motionPreset: String?
    @State private var motionAssetIDs: [String] = []
    @State private var phase = "entrance"
    /// nil = use the shape's own default strength; the inspector tunes it after.
    @State private var cameraIntensity: Double? = nil
    @FocusState private var editingText: Bool
    private let visualKinds: Set<EditorSelectionKind> = [.mediaOverlay, .visualBlock, .motionScene, .cameraEffect]
    private var selected: EditorSelection? { session.selection.flatMap { visualKinds.contains($0.kind) ? $0 : nil } }
    private var raw: [String: JSONValue] { selected.flatMap(session.visualRaw) ?? [:] }
    private var block: EditorVisualBlock? { session.document.visualBlocks.first { $0.id == selected?.id } }
    private var cardElement: EditorTextElement? {
        guard let block, block.kind == "text_card" else { return nil }
        return session.document.textElements.first { $0.raw["visual_block_id"] == .string(block.id) }
    }
    private var style: [String: JSONValue] { NativeVisualAuthoring.style(for: raw) }
    private var animation: [String: JSONValue] { style["animation"]?.objectValue ?? [:] }
    private var mediaSelected: Bool { selected?.kind == .mediaOverlay || block?.kind == "media" }
    private var canEditSelection: Bool {
        switch selected?.kind {
        case .mediaOverlay: return session.canEdit(.mediaOverlays)
        case .visualBlock: return session.canEdit(.visualBlocks)
        case .motionScene: return session.canEdit(.motionScenes)
        case .cameraEffect: return session.canEdit(.cameraEffects)
        default: return false
        }
    }

    private var editorTabs: [Tab] {
        guard selected != nil else { return [] }
        return mediaSelected || cardElement != nil ? [.edit, .animation] : [.edit]
    }
    private var availableCategories: [Category] {
        session.rendersOnDevice ? [.media] : Category.allCases
    }
    private var editorHeading: String { selected.map { "Edit " + label($0).lowercased() } ?? "Add visual" }
    private var addAction: (() -> Void)? { selected == nil ? nil : { openLibrary() } }

    private var removeAction: (() -> Void)? {
        guard let selected, canEditSelection else { return nil }
        return { session.removeVisualSelection(selected); openLibrary() }
    }

    private func openLibrary() {
        editingText = false
        session.endTransaction()
        session.select(nil)
        tab = .edit
        cardPreset = nil
        motionPreset = nil
    }

    var body: some View {
        NativeEditorLanePanel(title: "Visuals", tabs: editorTabs, tab: $tab, onDone: {
            editingText = false; session.endTransaction(); onDone()
        }, heading: editorHeading, onAdd: addAction,
            onDelete: removeAction) {
            VStack(spacing: 12) {
                if session.isAddingVisual { ProgressView("Opening visual…").frame(minHeight: 44) }
                ForEach(session.pendingEditorImports.filter { $0.lane == .visual }) { pending in
                    HStack(spacing: 8) {
                        if pending.status == "failed" { Image(systemName: "exclamationmark.circle").foregroundStyle(KriaColor.failureText) }
                        else { ProgressView().controlSize(.small) }
                        Text(pending.status == "failed" ? (pending.error ?? "Couldn’t prepare visual.") : "Preparing visual import…")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        Spacer()
                        if pending.status == "failed" && pending.retryable {
                            Button("Retry") { Task { await session.retryPendingEditorImport(pending) } }
                                .font(KriaFont.body(12)).frame(minHeight: 44)
                        }
                        Button("Remove", role: .destructive) { Task { await session.dismissPendingEditorImport(pending) } }
                            .font(KriaFont.body(12)).frame(minHeight: 44)
                    }
                    .accessibilityIdentifier("native-editor-pending-visual-import")
                }
                if mediaSelected && !session.canEdit("visual_editor_style") {
                    Text("Advanced visual editing isn’t available for this edit yet. Placement and timing are still editable.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("native-editor-visual-style-unavailable")
                }
                if selected == nil {
                    browse
                } else {
                    switch tab {
                    case .edit: placement
                    case .animation: animations
                    }
                }
                if let error = session.visualError {
                    Text(error).foregroundStyle(KriaColor.failureText).font(KriaFont.body(12))
                        .accessibilityIdentifier("native-editor-visual-error")
                }
            }
        }
        .sheet(isPresented: $showsImporter, onDismiss: { Task { await session.refreshVisualLibrary() } }) { visualImporter }
        .onChange(of: uploads.records) { _, _ in Task { await session.refreshVisualLibrary() } }
        // Un-choosing a visual in the picker removes it server-side without touching `records`.
        .onChange(of: uploads.photoSelections) { _, _ in Task { await session.refreshVisualLibrary() } }
        .task {
            await session.refreshVisualLibrary()
            // Pending assets may finish analysis after the upload record is removed.
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                if session.visualLibrary.contains(where: { ["pending", "analyzing", "processing"].contains($0.status) }) {
                    await session.refreshVisualLibrary()
                }
            }
        }
        .onChange(of: selected, initial: true) { _, _ in
            editingText = false
            tab = .edit
        }
        .onChange(of: editingText) { _, focused in
            if focused { session.beginTransaction() } else { session.endTransaction() }
        }
        .onChange(of: tab) { _, _ in editingText = false; session.endTransaction() }
        .onDisappear { session.endTransaction() }
    }

    private var visualImporter: some View {
        NavigationStack {
            ScrollView {
                FootagePickerView(
                    projectID: projectID,
                    uploads: uploads,
                    maximumClipCount: session.visualLibraryLimit,
                    attachedClipCount: session.visualLibrary.count,
                    attachedMediaIDs: Set(session.visualLibrary.map(\.id)),
                    role: .visual,
                    itemID: session.visualItemID
                )
                .padding(16)
            }
            .navigationTitle("Add photo or video")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { showsImporter = false }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private var browse: some View {
        VStack(spacing: 12) {
            HStack(spacing: 0) {
                ForEach(availableCategories, id: \.self) { value in
                    Button { category = value; cardPreset = nil; motionPreset = nil } label: {
                        Text(value.rawValue).font(KriaFont.body(13)).frame(maxWidth: .infinity, minHeight: 44)
                            .background(category == value ? KriaColor.selectionSoft : .clear, in: RoundedRectangle(cornerRadius: 9))
                    }.accessibilityAddTraits(category == value ? .isSelected : [])
                }
            }
            switch category {
            case .media: mediaLibrary
            case .cards: cards
            case .motion: motion
            case .camera: camera
            }

        }
    }

    private var mediaLibrary: some View {
        VStack(spacing: 12) {
            Button { showsImporter = true } label: {
                Label("Add photo or video", systemImage: "plus").frame(maxWidth: .infinity, minHeight: 44)
            }.buttonStyle(.plain)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(KriaColor.line, lineWidth: 1))
                .disabled(!session.canImportVisuals)
                .accessibilityIdentifier("native-editor-import-visual")
            if let message = session.visualImportUnavailableMessage {
                Text(message).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("native-editor-import-visual-unavailable")
            }
            if session.visualLibraryLoading && session.visualLibrary.isEmpty { ProgressView("Loading visuals…") }
            if session.visualLibrary.isEmpty && !session.visualLibraryLoading {
                Text("Your added photos and videos").font(KriaFont.body(14).weight(.medium))
                Text("Choose media from Photos or Files to place in your edit.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
            // The library is bounded. Eager, equal-height rows keep its scroll
            // extent stable while thumbnails load and analysis polls refresh.
            VStack(spacing: 10) {
                ForEach(Array(stride(from: 0, to: session.visualLibrary.count, by: 3)), id: \.self) { start in
                    HStack(alignment: .top, spacing: 10) {
                        ForEach(0..<3, id: \.self) { column in
                            if start + column < session.visualLibrary.count {
                                let asset = session.visualLibrary[start + column]
                                VStack(spacing: 0) {
                                    Button { Task { await session.addLibraryVisual(asset) } } label: { assetTile(asset) }
                                        .disabled(asset.status != "ready" || session.isAddingVisual || !session.canAuthorVisuals)
                                        .accessibilityIdentifier("native-editor-add-visual-" + asset.id)
                                    if asset.retryable == true {
                                        Button("Retry") { Task { await session.retryLibraryVisual(asset.id) } }
                                            .accessibilityLabel("Retry " + (asset.sourceFilename ?? "visual"))
                                    }
                                }.frame(maxWidth: .infinity)
                            } else {
                                Color.clear.frame(maxWidth: .infinity, minHeight: 116, maxHeight: 116)
                            }
                        }
                    }
                }
            }
            if session.visualError != nil { Button("Retry loading visuals") { Task { await session.refreshVisualLibrary() } } }
        }
    }

    private func assetTile(_ asset: CreationVisual) -> some View {
        VStack(spacing: 6) {
            NativeVisualThumbnail(asset: asset)
                .frame(height: 76).clipped().clipShape(RoundedRectangle(cornerRadius: 9))
            Text(asset.sourceFilename ?? "Visual").font(KriaFont.body(11)).lineLimit(1).frame(height: 14)
            Text(asset.status == "ready" ? " " : asset.status.capitalized)
                .font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk).lineLimit(1).frame(height: 14)
                .accessibilityHidden(asset.status == "ready")
        }
    }

    private var cards: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                ForEach(["Simple", "Bold"], id: \.self) { preset in
                    Button { cardPreset = preset; cardText = "" } label: {
                        VStack(spacing: 8) {
                            Text("Your\nstory.").font(KriaFont.body(preset == "Bold" ? 28 : 22).weight(preset == "Bold" ? .bold : .regular))
                                .foregroundStyle(preset == "Bold" ? .white : KriaColor.ink)
                                .frame(maxWidth: .infinity, minHeight: 100)
                                .background(preset == "Bold" ? KriaColor.ink : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                            Text(preset).font(KriaFont.body(13))
                        }
                    }.disabled(!session.canAuthorVisuals || !session.canEdit(.text))
                        .accessibilityIdentifier("native-editor-card-preset-" + preset.lowercased())
                }
            }
            if let cardPreset {
                TextField("Card text", text: $cardText, axis: .vertical).lineLimit(2...4)
                    .padding(12).background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityIdentifier("native-editor-new-card-text")
                HStack {
                    Button("Cancel") { self.cardPreset = nil }
                    Spacer()
                    Button("Add card") {
                        if session.addTextCard(text: cardText, bold: cardPreset == "Bold") != nil { self.cardPreset = nil; tab = .edit }
                    }.disabled(cardText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }.frame(minHeight: 44)
            } else {
                Text("Choose a card, then edit its text and style.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
        }
    }

    private var motion: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                ForEach(["card_stack", "film_strip"], id: \.self) { preset in
                    Button { motionPreset = preset; motionAssetIDs = [] } label: {
                        VStack(spacing: 8) {
                            ZStack {
                                RoundedRectangle(cornerRadius: 10).fill(KriaColor.softZinc)
                                HStack(spacing: preset == "card_stack" ? -4 : 4) {
                                    Rectangle().fill(Color(red: 0.87, green: 0.90, blue: 0.79))
                                        .overlay(Rectangle().stroke(.white, lineWidth: 2))
                                        .rotationEffect(.degrees(preset == "card_stack" ? -10 : 0))
                                    Rectangle().fill(KriaColor.sky)
                                        .overlay(Rectangle().stroke(.white, lineWidth: 2))
                                        .rotationEffect(.degrees(preset == "card_stack" ? 8 : 0))
                                }.frame(height: 66).padding(.horizontal, 18)
                            }.frame(maxWidth: .infinity, minHeight: 100)
                            Text(preset == "card_stack" ? "Card stack" : "Film strip")
                        }
                    }.disabled(!session.canEdit(.motionScenes))
                }
            }
            if let motionPreset {
                let minimum = motionPreset == "card_stack" ? 2 : 3
                let maximum = motionPreset == "card_stack" ? 6 : 8
                Text("Choose \(minimum)–\(maximum) photos in playback order.").font(KriaFont.body(12))
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(session.visualLibrary.filter { $0.kind == "image" && $0.status == "ready" }) { asset in
                        Button {
                            if motionAssetIDs.contains(asset.id) { motionAssetIDs.removeAll { $0 == asset.id } }
                            else if motionAssetIDs.count < maximum { motionAssetIDs.append(asset.id) }
                        } label: {
                            assetTile(asset).overlay(alignment: .topTrailing) {
                                if let index = motionAssetIDs.firstIndex(of: asset.id) {
                                    Text(String(index + 1)).padding(6).background(KriaColor.sky, in: Circle())
                                }
                            }
                        }.accessibilityAddTraits(motionAssetIDs.contains(asset.id) ? .isSelected : [])
                    }
                }
                Button("Add composition") {
                    let assets = motionAssetIDs.compactMap { id in session.visualLibrary.first { $0.id == id } }
                    Task { await session.addMotionComposition(preset: motionPreset, assets: assets) }
                }.frame(minHeight: 44).disabled(motionAssetIDs.count < minimum || session.isAddingVisual)
            } else {
                Text("Arrange your added photos into a moving composition.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
        }
    }

    private var camera: some View {
        VStack(spacing: 12) {
            cameraOption(
                easing: CameraEmphasis.holdEasing,
                icon: "plus.magnifyingglass",
                title: "Zoom in",
                detail: "Pushes in, holds through the moment, then eases back out."
            )
            cameraOption(
                easing: CameraEmphasis.pulseEasing,
                icon: "viewfinder",
                title: "Zoom pulse",
                detail: "A gentle push in and back out to emphasize a moment."
            )
            Text("Applies to footage. Adjust its range on the timeline.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        }
    }

    private func cameraOption(easing: String, icon: String, title: String, detail: String) -> some View {
        Button { session.addCameraPulse(easing: easing, intensity: cameraIntensity) } label: {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.system(size: 40)).frame(width: 90, height: 80)
                    .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 8) {
                    Text(title).fontWeight(.semibold)
                    Text(detail).font(KriaFont.body(13))
                }
            }
        }
        .disabled(!session.canEdit(.cameraEffects))
        .accessibilityIdentifier("native-visual-add-camera-\(easing)")
    }

    @ViewBuilder private var placement: some View {
        if let selected {
            VStack(spacing: 8) {
                if mediaSelected {
                    NativeEditorMenuRow(title: "Display", value: displayMode == "overlay" ? "Overlay" : "Fullscreen") {
                        Button("Overlay") {
                            if selected.kind == .mediaOverlay { session.setMediaOverlayDisplayMode(id: selected.id, mode: "pip") }
                            else { session.setVisualBlockDisplayMode(id: selected.id, mode: "overlay") }
                        }
                        Button("Fullscreen") {
                            if selected.kind == .mediaOverlay { session.setMediaOverlayDisplayMode(id: selected.id, mode: "fullscreen") }
                            else { session.setVisualBlockDisplayMode(id: selected.id, mode: "fullscreen") }
                        }
                    }
                    NativeEditorMenuRow(title: "Fit", value: style["fit_mode"] == .string("contain") ? "Fit" : "Fill") {
                        Button("Fill") { session.setVisualEditorStyle(selected, key: "fit_mode", value: .string("cover")) }
                        Button("Fit") { session.setVisualEditorStyle(selected, key: "fit_mode", value: .string("contain")) }
                    }.disabled(displayMode != "fullscreen" || !session.canEdit("visual_editor_style"))
                    if displayMode != "fullscreen" {
                        Text("Choose Fullscreen to adjust Fit or Fill.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                    let peers = session.visualLayerSelections(for: selected)
                    let layerIndex = peers.firstIndex(of: selected) ?? 0
                    NativeEditorMenuRow(title: "Layer", value: "\(layerIndex + 1) of \(peers.count)") {
                        Button("Bring forward") { session.moveVisualLayer(selected, by: 1) }.disabled(layerIndex >= peers.count - 1)
                        Button("Send backward") { session.moveVisualLayer(selected, by: -1) }.disabled(layerIndex == 0)
                    }
                    slider("Zoom", value: Binding(get: { style["zoom"]?.numberValue ?? 1 }, set: { session.setVisualEditorStyle(selected, key: "zoom", value: .number($0)) }), range: 1...4).disabled(!session.canEdit("visual_editor_style"))
                    slider("Rotation", value: Binding(get: { style["rotation_deg"]?.numberValue ?? 0 }, set: { session.setVisualEditorStyle(selected, key: "rotation_deg", value: .number($0)) }), range: -180...180).disabled(!session.canEdit("visual_editor_style"))
                    Text("Drag to position. Use the corner to resize or rotate.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                } else if selected.kind == .cameraEffect {
                    slider("Intensity", value: Binding(get: { raw["intensity"]?.numberValue ?? 0.04 }, set: { session.setCameraEffectIntensity(id: selected.id, intensity: $0) }), range: 0.01...0.08)
                    Text("Applies to footage. Adjust its range on the timeline.").font(KriaFont.body(12))
                } else if let element = cardElement {
                    TextField("Card text", text: Binding(get: { cardElement?.text ?? "" }, set: { session.updateTextContent(id: element.id, content: $0) }), axis: .vertical)
                        .focused($editingText).padding(12).background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                    Picker("Font", selection: Binding(get: { cardElement?.raw["font_family"]?.stringValue ?? "Inter" }, set: { session.setTextStyle(id: element.id, style: $0) })) {
                        ForEach(["Inter Regular", "Inter", "Fraunces", "Space Grotesk"], id: \.self) { Text($0).tag($0) }
                    }.frame(minHeight: 44)
                    ColorPicker("Text color", selection: Binding(get: { nativeEditorColor(cardElement?.raw["color"]?.stringValue ?? "#FFFFFF") }, set: { session.setTextColor(id: element.id, color: nativeEditorHex($0)) }), supportsOpacity: false).frame(minHeight: 44)
                    slider("Size", value: Binding(get: { cardElement.map(NativeEditorSession.textSize) ?? 72 }, set: { session.setTextSize(id: element.id, sizePX: $0) }), range: 8...240)
                } else if selected.kind == .motionScene {
                    Text("Arrange the composition’s timing on the timeline.").font(KriaFont.body(13))
                    if session.canAdjustMotionSpeed(id: selected.id) {
                    slider("Speed", value: Binding(get: { raw["motion"]?.objectValue?["speed"]?.numberValue ?? 1 }, set: { session.setMotionSpeed(id: selected.id, speed: $0) }), range: 0.5...4)
                    } else {
                        Text("This saved composition keeps its original animation.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                if mediaSelected {
                    NativeFootagePanel(session: session, selection: selected)
                }
                timing(selected)
            }.disabled(!canEditSelection)
            if !canEditSelection {
                Text("This visual is read-only in this edit.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
        } else { Text("Choose a visual on the timeline to edit it.").frame(minHeight: 80) }
    }

    @ViewBuilder private var animations: some View {
        if let selected, mediaSelected {
            VStack(spacing: 14) {
                Picker("Animation phase", selection: $phase) {
                    Text("In").tag("entrance"); Text("Out").tag("exit"); Text("Loop").tag("loop")
                }.pickerStyle(.segmented)
                HStack(spacing: 6) {
                    ForEach(phase == "loop" ? ["None", "Pulse", "Bounce", "Float"] : ["None", "Fade", "Pop", "Slide", "Zoom"], id: \.self) { effect in
                        Button { setAnimation(phase, value: .string(effect.lowercased()), selected: selected) } label: {
                            VStack(spacing: 6) {
                                Image(systemName: effect == "None" ? "nosign" : "photo")
                                    .font(.system(size: 22)).frame(maxWidth: .infinity, minHeight: 52)
                                    .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 9))
                                    .overlay(RoundedRectangle(cornerRadius: 9).stroke((animation[phase]?.stringValue ?? "none") == effect.lowercased() ? KriaColor.sky : .clear, lineWidth: 2))
                                Text(effect).font(KriaFont.body(11))
                            }
                        }.accessibilityLabel("\(phase == "entrance" ? "In" : phase == "exit" ? "Out" : "Loop") visual animation \(effect)")
                    }
                }
                slider("Animation speed", value: Binding(get: { animation["speed"]?.numberValue ?? 1 }, set: { setAnimation("speed", value: .number($0), selected: selected) }), range: 0.25...3)
                Text("Changes motion speed. Visual timing stays the same.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }.disabled(!session.canEdit("visual_editor_style"))
        } else if let element = cardElement {
            NativeEditorTextPanel(id: element.id, session: session, initialTab: .animation, embeddedAnimation: true, onDone: { tab = .edit })
        } else if selected?.kind == .motionScene || selected?.kind == .cameraEffect {
            placement
        } else { Text("Select a visual to animate.").frame(minHeight: 80) }
    }

    private var displayMode: String { raw["display_mode"]?.stringValue == "fullscreen" ? "fullscreen" : "overlay" }
    private func setAnimation(_ key: String, value: JSONValue, selected: EditorSelection) {
        var next = animation; next[key] = value
        session.setVisualEditorStyle(selected, key: "animation", value: .object(next), animationPhase: key)
    }
    private func slider(_ title: String, value: Binding<Double>, range: ClosedRange<Double>) -> some View {
        HStack {
            Text(title).frame(width: 68, alignment: .leading)
            NativePaperSlider(session: session, value: value, bounds: range, label: title)
            Text(value.wrappedValue.formatted(.number.precision(.fractionLength(0...2)))).monospacedDigit().frame(width: 48)
        }.frame(minHeight: 44)
    }
    private func timing(_ selected: EditorSelection) -> some View {
        let item = session.timelineItems.first { $0.selection == selected }
        return HStack {
            ForEach([true, false], id: \.self) { start in
                VStack(alignment: .leading, spacing: 4) {
                    Text(start ? "Start (seconds)" : "End (seconds)").font(KriaFont.body(12))
                    TextField(start ? "Start" : "End", value: Binding(get: { start ? item?.start ?? 0 : item?.end ?? 0 }, set: {
                        session.setVisualTiming(selected, outputTime: $0, isStart: start)
                    }), format: .number.precision(.fractionLength(0...2)))
                        .keyboardType(.decimalPad).padding(10).background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }
    private func label(_ selected: EditorSelection) -> String {
        switch selected.kind {
        case .mediaOverlay: return (raw["media_kind"] ?? raw["kind"]) == .string("video") ? "Video" : "Photo"
        case .motionScene: return session.document.motionScenes.first { $0.id == selected.id }?.preset?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Motion"
        case .cameraEffect: return "Zoom pulse"
        default: return session.document.visualBlocks.first { $0.id == selected.id }?.kind == "text_card" ? "Text card" : ((raw["media_kind"] ?? raw["kind"]) == .string("video") ? "Video" : "Photo")
        }
    }
    private func symbol(_ selected: EditorSelection) -> String {
        switch selected.kind { case .motionScene: return "rectangle.stack"; case .cameraEffect: return "viewfinder"; default: return "photo" }
    }
}


struct NativeVisualThumbnail: View {
    let asset: CreationVisual
    @State private var videoFrame: UIImage?
    static func thumbnailURL(for asset: CreationVisual) -> URL? {
        // Video preview_url is a still image, not a playable video asset.
        // Originals are available before AI analysis finishes.
        return asset.sourceURL ?? asset.displayURL
    }
    // Re-signing an unchanged object during polling must not restart decoding.
    static func thumbnailIdentity(for asset: CreationVisual) -> String {
        let url = thumbnailURL(for: asset)
        return [asset.id, asset.kind, url?.host ?? "", url?.path ?? ""].joined(separator: "|")
    }
    private var sourceURL: URL? { Self.thumbnailURL(for: asset) }
    var body: some View {
        GeometryReader { geometry in
            Group {
            if asset.kind == "video" {
                if let preview = asset.previewURL {
                    AsyncImage(url: preview) { phase in
                        if let image = phase.image { image.resizable().scaledToFill() }
                        else { videoFallback }
                    }
                } else { videoFallback }
            } else {
                AsyncImage(url: asset.displayURL ?? asset.sourceURL) { image in
                    image.resizable().scaledToFill()
                } placeholder: { placeholder }
            }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .clipped()
        }.accessibilityHidden(true)
            .task(id: Self.thumbnailIdentity(for: asset)) {
                guard asset.kind == "video", let sourceURL else { return }
                let frame = try? await Self.videoThumbnail(url: sourceURL)
                guard !Task.isCancelled else { return }
                videoFrame = frame
            }
    }
    @ViewBuilder private var videoFallback: some View {
        if let videoFrame { Image(uiImage: videoFrame).resizable().scaledToFill() }
        else { placeholder }
    }
    private var placeholder: some View {
        ZStack { KriaColor.softZinc; Image(systemName: asset.kind == "image" ? "photo" : "video") }
    }
    @MainActor static func videoThumbnail(url: URL) async throws -> UIImage {
        let task = Task.detached(priority: .utility) {
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 256, height: 256)
            return try await withTaskCancellationHandler {
                try Task.checkCancellation()
                return try await generator.image(at: .zero).image
            } onCancel: {
                generator.cancelAllCGImageGeneration()
            }
        }
        let image = try await withTaskCancellationHandler {
            try await task.value
        } onCancel: {
            task.cancel()
        }
        return UIImage(cgImage: image)
    }
}
