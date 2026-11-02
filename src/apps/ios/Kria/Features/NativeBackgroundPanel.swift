import KriaMediaEngine
import SwiftUI

/// KRI-1118: the Background tool of a Talk-to-camera edit. One photo or video from the item's Visuals pool sits
/// behind the speaker for the whole video (the Fill); "None" clears it. The pool grid is the editor's shared
/// `NativeVisualPoolPicker`; this panel decides what a tap writes: the `speaker_background` lane, never a visual block.
/// KRI-1162: while the server opens `speaker_background_windows`, a Sequence of pool photos and videos takes turns
/// over the Fill, and a Speaker control draws the speaker small in a corner (presets only).
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
            if session.canEditSpeakerBackgroundWindows { sectionTitle("Fill", id: "native-editor-background-fill-title") }
            NativeVisualPoolPicker(
                session: session, uploads: uploads, projectID: projectID, idPrefix: "native-editor-background",
                choices: session.speakerBackgroundChoices, selectedID: session.speakerBackground?.mediaID, noneTitle: "None",
                onNone: { _ = session.removeSpeakerBackground() },
                onSelect: { asset in
                    _ = session.setSpeakerBackground(mediaID: asset.id, kind: VisualMediaKind(rawValue: asset.kind) ?? .image)
                }
            )
            if session.canEditSpeakerBackgroundWindows {
                sequenceSection
                speakerSection
            }
            }
        }
        .onAppear { panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() } }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
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
                Text("Custom position. Pick a corner to replace it.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-background-speaker-custom")
            }
        }
    }
}
