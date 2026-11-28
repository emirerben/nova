import KriaMediaEngine
import SwiftUI
import UIKit

/// The Background tool of a Talk-to-camera edit. KRI-1656: every background is a bar on the timeline's BACKGROUND lane, so
/// this panel only adds them. Tapping a photo or video in the pool adds a bar from the playhead (the whole video when there is
/// none yet); tapping a swatch does the same with a plain colour. Selecting a bar opens its inspector: Crop, Replace, Remove.
/// Where no bar covers the video, her own take shows. The Speaker section draws the speaker small in a corner (presets; the
/// preview gesture places them freely).
/// KRI-1118: the background is the `speaker_background` lane, never a visual block. KRI-1162 added timed windows and the corner
/// speaker, KRI-1173 a plain colour in place of a photo or video.
struct NativeBackgroundPanel: View {
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String { case background = "Background" }

    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let onDone: () -> Void
    @State private var tab = Tab.background
    /// KRI-1558: the section under the playhead (`NativeSpeakerSectionTracking`), nil outside one.
    @State private var playheadSection: Int?

    var body: some View {
        NativeEditorLanePanel(title: "Background", tabs: [Tab](), tab: $tab, onDone: { session.endTransaction(); onDone() }, heading: String(localized: "Background")) {
            VStack(spacing: 12) {
            if let opening = session.speakerFullScreenOpening { fullScreenOpeningRow(opening) }
            if session.canEditSpeakerBackgroundColor { colorSection }
            if session.canEditSpeakerBackgroundColor { sectionTitle("Photo or video", id: "native-editor-background-media-title") }
            NativeVisualPoolPicker(
                session: session, uploads: uploads, projectID: projectID, idPrefix: "native-editor-background",
                choices: session.speakerBackgroundChoices, selectedID: session.backgroundMediaAtPlayhead, noneTitle: String(localized: "None", comment: "Choice: no background"),
                onNone: { session.endTransaction(); _ = session.removeSpeakerBackground() },
                onSelect: { asset in
                    session.endTransaction()
                    _ = session.addSpeakerBackground(mediaID: asset.id, kind: VisualMediaKind(rawValue: asset.kind) ?? .image)
                },
                // A colour is neither a pool visual nor nothing: no tile is lit for it.
                noneSelected: session.speakerBackground == nil,
                // With timed backgrounds a bar is removed from its own inspector; without them None is the only way to clear.
                showsNone: !session.canEditSpeakerBackgroundWindows
            )
            if session.canEditSpeakerBackgroundWindows { speakerSection }
            }
        }
        .onAppear { panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() } }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
        .modifier(NativeSpeakerSectionTracking(session: session, index: $playheadSection))
    }

    // MARK: Full-screen opening (Qendresa item 16)

    /// "Starts full screen · 7.2 s" with a remove control; shown only while the edit has an opening. Taking it away puts the
    /// speaker in the corner from the first frame (one undo step). Read-only while the server does not open the capability.
    private func fullScreenOpeningRow(_ opening: EditorFullScreenWindow) -> some View {
        let length = ((opening.endS - opening.startS) * 10).rounded() / 10
        let text = String(localized: "Starts full screen · \(length, format: .number.precision(.fractionLength(0...1))) s")
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
        ("white", String(localized: "White", comment: "Background colour"), "#FFFFFF"), ("black", String(localized: "Black", comment: "Background colour"), "#000000"), ("green", String(localized: "Green", comment: "Background colour"), "#00FF00"),
        ("blue", String(localized: "Blue", comment: "Background colour"), "#0000FF"), ("grey", String(localized: "Grey", comment: "Background colour"), "#808080"),
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
            _ = session.setSpeakerBackgroundColorFromPlayhead(hex: swatch.hex)
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
                _ = session.setSpeakerBackgroundColorFromPlayhead(hex: nativeEditorHex(picked))
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

    // MARK: Speaker (KRI-1162)

    private func sectionTitle(_ text: LocalizedStringKey, id: String) -> some View {
        Text(text).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier(id)
    }

    private static let speakerChoices: [(preset: String, label: String)] = [
        ("full", String(localized: "Full", comment: "Speaker position: whole screen")), ("small_bottom_right", String(localized: "Bottom right")), ("small_bottom_left", String(localized: "Bottom left")),
        ("small_top_right", String(localized: "Top right")), ("small_top_left", String(localized: "Top left")),
    ]

    /// KRI-1558: the section under the playhead whose frame the choices show and change, while the section controls are open.
    private var playheadLayout: EditorLayoutWindow? { playheadSection == nil ? nil : session.speakerControlSection }

    /// Which choice is lit: no frame is Full, a preset frame is that preset, anything else is Custom (kept untouched).
    /// KRI-1558: inside a section, that section's frame; an own-take or picture-only section lights none.
    private var speakerSelection: String {
        if let section = playheadLayout {
            guard section.layout == .small || section.layout == .full else { return "" }
            guard let frame = section.speakerFrame else { return "full" }
            return frame.presetName ?? "custom"
        }
        guard let frame = session.speakerFrame else { return "full" }
        return frame.presetName ?? "custom"
    }

    private var speakerSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Speaker", id: "native-editor-background-speaker-title")
            if playheadLayout != nil { NativeSpeakerSectionScope(session: session) }
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
