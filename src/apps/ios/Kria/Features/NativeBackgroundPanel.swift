import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-1118: the Background tool of a Talk-to-camera edit. One photo or video from the item's Visuals pool sits
/// behind the speaker for the whole video (the Fill); "None" clears it. The pool grid is the editor's shared
/// `NativeVisualPoolPicker`; this panel decides what a tap writes: the `speaker_background` lane, never a visual block.
/// KRI-1162: while the server opens `speaker_background_windows`, a Sequence of pool photos and videos takes turns
/// over the Fill, and a Speaker control draws the speaker small in a corner (presets; the preview gesture places them freely).
/// KRI-1173: while the server opens `speaker_background_color`, a Colour section puts a plain colour behind the speaker
/// instead of a photo or video (five swatches and a custom colour); picking one replaces a media fill and vice versa.
struct NativeBackgroundPanel: View {
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String { case background = "Background" }

    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let onDone: () -> Void
    @State private var tab = Tab.background

    var body: some View {
        NativeEditorLanePanel(title: "Background", tabs: [Tab](), tab: $tab, onDone: { session.endTransaction(); onDone() }) {
            VStack(spacing: 12) {
            if let opening = session.speakerFullScreenOpening { fullScreenOpeningRow(opening) }
            if session.canEditSpeakerBackgroundColor { colorSection }
            if session.canEditSpeakerBackgroundWindows { sectionTitle("Fill", id: "native-editor-background-fill-title") }
            else if session.canEditSpeakerBackgroundColor { sectionTitle("Photo or video", id: "native-editor-background-media-title") }
            NativeVisualPoolPicker(
                session: session, uploads: uploads, projectID: projectID, idPrefix: "native-editor-background",
                choices: session.speakerBackgroundChoices, selectedID: session.speakerBackground?.mediaID, noneTitle: "None",
                onNone: { session.endTransaction(); _ = session.removeSpeakerBackground() },
                onSelect: { asset in
                    session.endTransaction()
                    _ = session.setSpeakerBackground(mediaID: asset.id, kind: VisualMediaKind(rawValue: asset.kind) ?? .image)
                },
                // A colour fill is neither a pool visual nor nothing: no tile is lit for it.
                noneSelected: session.speakerBackground == nil
            )
            // Qendresa item 18: aim a photo or video fill at its text. A colour has no picture to aim.
            if session.canEditSpeakerBackgroundFocus, session.speakerBackground?.mediaID != nil {
                NativeBackgroundAimControls(session: session, windowIndex: nil)
            }
            if session.canEditSpeakerBackgroundWindows {
                sequenceSection
                speakerSection
            }
            }
        }
        .onAppear { panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() } }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
    }

    // MARK: Full-screen opening (Qendresa item 16)

    /// "Starts full screen · 7.2 s" with a remove control; shown only while the edit has an opening. Taking it away puts the
    /// speaker in the corner from the first frame (one undo step). Read-only while the server does not open the capability.
    private func fullScreenOpeningRow(_ opening: EditorFullScreenWindow) -> some View {
        let length = ((opening.endS - opening.startS) * 10).rounded() / 10
        let text = "Starts full screen · " + (length == length.rounded() ? String(Int(length)) : String(format: "%.1f", length)) + " s"
        return HStack(spacing: 8) {
            Text(text).font(KriaFont.body(13)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("background.fullScreenOpening")
            Button { session.endTransaction(); _ = session.removeFullScreenOpening() } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 20, height: 20).background(KriaColor.ink.opacity(0.8), in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!session.canEditSpeakerFullOpening)
            .accessibilityLabel("Remove the full screen start")
            .accessibilityIdentifier("background.fullScreenOpening.remove")
        }
    }

    // MARK: Colour (KRI-1173)

    /// White, black, green screen, blue screen and a neutral grey, then any colour the system picker offers.
    static let colorSwatches: [(id: String, name: String, hex: String)] = [
        ("white", "White", "#FFFFFF"), ("black", "Black", "#000000"), ("green", "Green", "#00FF00"),
        ("blue", "Blue", "#0000FF"), ("grey", "Grey", "#808080"),
    ]

    private var currentColor: String? { session.speakerBackgroundColor }

    private var colorSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Colour", id: "native-editor-background-color-title")
            HStack(spacing: 6) {
                ForEach(Self.colorSwatches, id: \.id) { swatch in colorSwatch(swatch) }
                customColorControl
                Spacer(minLength: 0)
            }
        }
    }

    private func colorSwatch(_ swatch: (id: String, name: String, hex: String)) -> some View {
        let selected = currentColor == swatch.hex
        return Button {
            session.endTransaction()
            _ = session.setSpeakerBackgroundColor(hex: swatch.hex)
        } label: {
            Circle()
                .fill(nativeEditorColor(swatch.hex))
                .overlay(Circle().stroke(KriaColor.line, lineWidth: 1))
                .overlay {
                    if selected {
                        Image(systemName: "checkmark").font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white).frame(width: 20, height: 20).background(KriaColor.ink, in: Circle())
                    }
                }
                .frame(width: 32, height: 32)
                .padding(6)
                .background(selected ? KriaColor.sage : .clear, in: Circle())
                .frame(width: 44, height: 44)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(swatch.name)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("native-editor-background-color-" + swatch.id)
    }

    /// Any colour the system picker offers (no opacity: the wire is `#RRGGBB`). Dragging inside the picker is one undo step.
    private var customColorControl: some View {
        let custom = currentColor.map { hex in !Self.colorSwatches.contains { $0.hex == hex } } ?? false
        return ColorPicker("Custom colour", selection: Binding(
            get: { nativeEditorColor(currentColor ?? "#FFFFFF") },
            set: { picked in
                session.beginTransaction()
                _ = session.setSpeakerBackgroundColor(hex: nativeEditorHex(picked))
            }
        ), supportsOpacity: false)
        .labelsHidden()
        .frame(minWidth: 44, minHeight: 44)
        .padding(2)
        .background(custom ? KriaColor.sage : .clear, in: Circle())
        .accessibilityLabel("Custom colour")
        .accessibilityAddTraits(custom ? .isSelected : [])
        .accessibilityIdentifier("native-editor-background-color-custom")
    }

    // MARK: Sequence and Speaker (KRI-1162)

    private func sectionTitle(_ text: String, id: String) -> some View {
        Text(text).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier(id)
    }

    private func poolName(_ mediaID: String, kind: VisualMediaKind) -> String {
        session.visualLibrary.first { $0.id == mediaID }?.sourceFilename ?? (kind == .video ? "Video" : "Photo")
    }

    /// Tap a pool tile to add it after the last one; the chosen ones sit below in order, each with its number and an x.
    private var sequenceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Sequence", id: "native-editor-background-sequence-title")
            Text("Tap a photo or video to add it. Each shows for a few seconds, one after another.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(session.speakerBackgroundChoices) { asset in sequenceAddTile(asset) }
                }
            }
            if session.speakerBackgroundWindows.isEmpty {
                Text("Nothing in the sequence yet.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-background-sequence-empty")
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(Array(session.speakerBackgroundWindows.enumerated()), id: \.element.localID) { index, window in
                            sequenceTile(window, index: index)
                        }
                    }
                    .padding(.top, 6)
                }
            }
        }
    }

    private func sequenceAddTile(_ asset: CreationVisual) -> some View {
        let kind = VisualMediaKind(rawValue: asset.kind) ?? .image
        let name = asset.sourceFilename ?? (kind == .video ? "Video" : "Photo")
        return Button { session.addSpeakerBackgroundWindow(mediaID: asset.id, kind: kind) } label: {
            NativeVisualThumbnail(asset: asset)
                .frame(width: 56, height: 56)
                .background(KriaColor.softZinc)
                .clipShape(RoundedRectangle(cornerRadius: 9))
                .overlay(alignment: .bottomTrailing) {
                    Image(systemName: "plus").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 18, height: 18).background(KriaColor.ink.opacity(0.7), in: Circle()).padding(3)
                }
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(RoundedRectangle(cornerRadius: 9))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Add \(name) to the sequence")
        .accessibilityIdentifier("native-editor-background-sequence-add-" + asset.id)
    }

    private func sequenceTile(_ window: EditorSpeakerBackgroundWindow, index: Int) -> some View {
        let asset = session.visualLibrary.first { $0.id == window.mediaID }
        let name = poolName(window.mediaID, kind: window.kind)
        return ZStack(alignment: .topTrailing) {
            Group {
                if let asset { NativeVisualThumbnail(asset: asset) } else { Color.clear }
            }
            .frame(width: 64, height: 64)
            .background(KriaColor.softZinc)
            .clipShape(RoundedRectangle(cornerRadius: 9))
            .overlay(alignment: .topLeading) {
                Text("\(index + 1)").font(KriaFont.body(11).weight(.bold)).foregroundStyle(.white)
                    .frame(width: 20, height: 20).background(KriaColor.ink, in: Circle()).padding(4)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(index + 1), \(name)")
            .accessibilityIdentifier("native-editor-background-sequence-" + window.localID)
            Button { session.removeSpeakerBackgroundWindow(id: window.localID) } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 20, height: 20).background(KriaColor.ink.opacity(0.8), in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .offset(x: 12, y: -12)
            .accessibilityLabel("Remove \(name) from the sequence")
            .accessibilityIdentifier("native-editor-background-sequence-remove-" + window.localID)
        }
        .padding(.trailing, 6)
    }

    private static let speakerChoices: [(preset: String, label: String)] = [
        ("full", "Full"), ("small_bottom_right", "Bottom right"), ("small_bottom_left", "Bottom left"),
        ("small_top_right", "Top right"), ("small_top_left", "Top left"),
    ]

    /// Which choice is lit: no frame is Full, a preset frame is that preset, anything else is Custom (kept untouched).
    private var speakerSelection: String {
        guard let frame = session.speakerFrame else { return "full" }
        return frame.presetName ?? "custom"
    }

    private var speakerSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Speaker", id: "native-editor-background-speaker-title")
            HStack(spacing: 4) {
                ForEach(Self.speakerChoices, id: \.preset) { choice in
                    let selected = speakerSelection == choice.preset
                    Button { session.setSpeakerFrame(preset: choice.preset) } label: {
                        Text(choice.label).font(KriaFont.body(11).weight(selected ? .semibold : .regular))
                            .lineLimit(2).multilineTextAlignment(.center).minimumScaleFactor(0.8)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .foregroundStyle(KriaColor.ink)
                            .background(selected ? KriaColor.sage : KriaColor.ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
                            .contentShape(RoundedRectangle(cornerRadius: 10))
                    }
                    .buttonStyle(.plain)
                    .disabled(session.speakerBackground == nil)
                    .accessibilityLabel(choice.label)
                    .accessibilityAddTraits(selected ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-background-speaker-" + choice.preset)
                }
            }
            if speakerSelection == "custom" {
                Text("Placed by hand. Pick a corner to replace it.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-background-speaker-custom")
            }
            if session.canMoveSpeakerOnPreview {
                Label("Tap yourself on the video to move or resize.", systemImage: "hand.tap")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-background-speaker-hint")
            }
        }
    }
}

/// Qendresa item 18: aim a photo or video background at its readable text. Three buttons pick the side (the picture is
/// cover-fitted and slid so the left edge, the middle or the right edge of it shows) and a Zoom slider goes from 0.3x
/// (the whole picture, with black bands) to 3x. `windowIndex` nil aims the fill; otherwise that timed background.
/// Used by this panel (fill) and by the selected-background inspector (window). Disabled while the server does not
/// open `background_focus`. The caller hides it for a colour-only background.
struct NativeBackgroundAimControls: View {
    @ObservedObject var session: NativeEditorSession
    let windowIndex: Int?

    /// The buttons: x on the wire. A button is lit only when the current aim is within 0.005 of it.
    static let sides: [(token: String, label: String, x: Double)] = [("left", "Left", 0), ("center", "Centre", 0.5), ("right", "Right", 1)]
    static let litBand = 0.005

    private var idPrefix: String { windowIndex == nil ? "native-editor-background-aim-fill" : "native-editor-background-aim" }
    private var zoomID: String { windowIndex == nil ? "native-editor-background-fill-zoom" : "native-editor-background-zoom" }
    private var focus: SpeakerBackgroundFocus? { session.speakerBackgroundFocus(windowIndex: windowIndex) }
    private var x: Double { focus?.x ?? 0.5 }
    private var zoom: Double { focus?.zoom ?? 1 }

    /// The lit button for an aim: a never-aimed picture is the centre (today's drawing).
    static func lit(x: Double) -> String? {
        sides.first { abs($0.x - x) <= litBand }?.token
    }

    var body: some View {
        let lit = Self.lit(x: x)
        VStack(alignment: .leading, spacing: 8) {
            Text("Aim").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader)
            HStack(spacing: 4) {
                ForEach(Self.sides, id: \.token) { side in
                    let selected = lit == side.token
                    Button { session.endTransaction(); _ = session.setSpeakerBackgroundFocus(x: side.x, windowIndex: windowIndex) } label: {
                        Text(side.label).font(KriaFont.body(12).weight(selected ? .semibold : .regular))
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .foregroundStyle(KriaColor.ink)
                            .background(selected ? KriaColor.sage : KriaColor.ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
                            .contentShape(RoundedRectangle(cornerRadius: 10))
                    }
                    .buttonStyle(.plain)
                    .disabled(!session.canEditSpeakerBackgroundFocus)
                    .accessibilityLabel("Aim " + side.label.lowercased())
                    .accessibilityAddTraits(selected ? .isSelected : [])
                    .accessibilityIdentifier("\(idPrefix)-\(side.token)")
                }
            }
            if focus != nil, lit == nil {
                Text("Aimed at the text").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("\(idPrefix)-custom")
            }
            HStack {
                Text("Zoom").font(KriaFont.body(13)).foregroundStyle(KriaColor.ink)
                Spacer()
                Text(BackgroundZoomSlider.readout(zoom)).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityHidden(true)
                    .accessibilityIdentifier("\(zoomID)-value")
            }
            BackgroundZoomSlider(zoom: zoom, id: zoomID,
                                 onEditingChanged: { editing in editing ? session.beginTransaction() : session.endTransaction() }) {
                _ = session.setSpeakerBackgroundFocus(zoom: $0, windowIndex: windowIndex)
            }
            .disabled(!session.canEditSpeakerBackgroundFocus)
        }
    }
}

/// `TiltZoomSlider`'s twin for a background's zoom: 0.3x to 3x in hundredths with a detent at 1x (a drag within
/// `holdBand` of 1x lands on exactly 1x, with a light haptic on entering it).
struct BackgroundZoomSlider: View {
    let zoom: Double
    let id: String
    var onEditingChanged: (Bool) -> Void = { _ in }
    let onChange: (Double) -> Void
    @State private var held = false
    private let haptic = UISelectionFeedbackGenerator()

    static let range = NativeEditorSession.backgroundFocusZoomRange
    static let holdBand = 0.05
    static let step = 0.01

    static func readout(_ zoom: Double) -> String { String(format: "%gx", (zoom * 100).rounded() / 100) }

    /// `raw` snapped to the detent at 1x, else clamped and rounded to hundredths.
    static func snapped(_ raw: Double) -> Double {
        guard abs(raw - 1) >= holdBand else { return 1 }
        return min(range.upperBound, max(range.lowerBound, (raw * 100).rounded() / 100))
    }

    var body: some View {
        let binding = Binding<Double>(
            get: { zoom },
            set: { raw in
                let next = Self.snapped(raw)
                if next == 1, !held { haptic.selectionChanged() }
                held = next == 1
                onChange(next)
            }
        )
        Slider(value: binding, in: Self.range, step: Self.step) { editing in
            if editing { haptic.prepare() }
            onEditingChanged(editing)
        }
        .tint(KriaColor.ink)
        .frame(minHeight: 44)
        .accessibilityLabel("Zoom")
        .accessibilityValue(Self.readout(zoom))
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onChange(Self.snapped(zoom + 0.1))
            case .decrement: onChange(Self.snapped(zoom - 0.1))
            @unknown default: break
            }
        }
        .accessibilityIdentifier(id)
    }
}
