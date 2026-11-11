import KriaMediaEngine
import SwiftUI
import Vision

/// The framed Talk-to-camera speaker, selected by tapping them on the preview: a size slider and the corner
/// shortcuts, for people who would rather not pinch. Moving and resizing by hand happen on the preview itself
/// (`NativeVideoPreview`); this panel and the gesture share an explicit frame target, one undo step each.
/// Rows: `docs/reviews/speaker-frame-direct-manipulation-failure-modes.md`. Identifiers: `native-editor-you-panel` / `-done`
/// (from the lane panel's title), `native-editor-speaker-size`, `native-editor-speaker-spot-<preset>`.
struct NativeSpeakerPanel: View {
    @Environment(\.nativeEditorPanelLifecycle) private var panelLifecycle
    @State private var lifecycleOwner = UUID()
    enum Tab: String { case speaker = "You" }

    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    @State private var tab = Tab.speaker
    @State private var sliderTarget: EditorSelection?

    private var target: EditorSelection {
        guard let selected = session.selection, selected.kind == .speaker else { return .speaker }
        return selected
    }

    private static let spots: [(preset: String, label: String, symbol: String)] = [
        ("small_top_left", "Top left", "arrow.up.left"), ("small_top_right", "Top right", "arrow.up.right"),
        ("small_bottom_left", "Bottom left", "arrow.down.left"), ("small_bottom_right", "Bottom right", "arrow.down.right"),
        ("full", "Full screen", "arrow.up.left.and.arrow.down.right"),
    ]

    var body: some View {
        let selected = target
        let scoped = session.usesScopedSpeakerFrame(for: selected)
        let inPanel = session.document.clips.first { $0.id == selected.id }?.splitSectionID != nil
        let width = session.speakerEditingFrame(for: selected)?.widthFraction ?? 1
        NativeEditorLanePanel(title: "You", tabs: [Tab](), tab: $tab, onDone: { session.endTransaction(); onDone() }) {
            VStack(alignment: .leading, spacing: 12) {
                Text(scoped
                    ? "Drag yourself to move. Pinch, or drag a corner, to resize. Changes affect only this clip."
                    : "Drag yourself on the video to move. Pinch, or drag a corner, to resize. One spot and size for the whole video.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-speaker-hint")
                HStack(spacing: 10) {
                    Text("Size").font(KriaFont.body(13).weight(.semibold))
                    NativeEditorSlider(session: session, value: Binding(
                        get: { session.speakerEditingFrame(for: selected)?.widthFraction ?? 1 },
                        set: { session.setSpeakerFrameWidth($0, for: sliderTarget ?? selected) }
                    ), in: SpeakerBackgroundLimits.minFrameWidthFraction...1, step: 0.01,
                    onEditingChanged: { sliderTarget = $0 ? selected : nil }) {
                        Text("Size")
                    }
                    .accessibilityValue("\(Int((width * 100).rounded())) percent of the \(inPanel ? "panel" : "video") width")
                    .accessibilityIdentifier("native-editor-speaker-size")
                    Text("\(Int((width * 100).rounded()))%").font(KriaFont.body(13).monospacedDigit())
                        .frame(minWidth: 40, alignment: .trailing)
                }
                HStack(spacing: 4) {
                    ForEach(Self.spots, id: \.preset) { spot in
                        let active = spot.preset == "full" ? session.speakerFrame(for: selected) == nil
                            : session.speakerFrame(for: selected)?.presetName == spot.preset
                        Button { session.setSpeakerFrame(preset: spot.preset, for: selected) } label: {
                            Image(systemName: spot.symbol).font(.system(size: 15, weight: .semibold))
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .foregroundStyle(KriaColor.ink)
                                .background(active ? KriaColor.sage : KriaColor.ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
                                .contentShape(RoundedRectangle(cornerRadius: 10))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(inPanel && spot.preset == "full" ? "Fill panel" : spot.label)
                        .accessibilityAddTraits(active ? .isSelected : [])
                        .accessibilityIdentifier("native-editor-speaker-spot-" + spot.preset)
                    }
                }
                if scoped {
                    Button("Use default framing") { session.resetSpeakerFrame(for: selected) }
                        .accessibilityIdentifier("native-editor-speaker-use-default")
                }
            }
            .disabled(!session.canEditSpeakerFrame(for: selected))
        }
        .onAppear {
            panelLifecycle?.register(owner: lifecycleOwner) { session.endTransaction() }
        }
        .onDisappear { panelLifecycle?.unregister(owner: lifecycleOwner); session.endTransaction() }
    }
}

/// Geometry and face lookup shared by the preview's speaker gesture.
enum NativeSpeakerPanelGeometry {
    static func pixelRect(_ rect: CGRect, canvas: CGSize) -> CGRect {
        CGRect(x: rect.minX * canvas.width, y: rect.minY * canvas.height,
               width: rect.width * canvas.width, height: rect.height * canvas.height)
    }

    static func canvasPoint(_ point: CGPoint, viewport: CGRect, canvas: CGSize) -> CGPoint {
        CGPoint(x: (viewport.minX + point.x * viewport.width) * canvas.width,
                y: (viewport.minY + point.y * viewport.height) * canvas.height)
    }

    static func movedPosition(from point: CGPoint, translation: CGSize, canvas: CGSize, viewport: CGRect) -> CGPoint {
        NativeTextTransformMath.movedPosition(from: point, translation: translation,
                                            canvas: CGSize(width: canvas.width * viewport.width, height: canvas.height * viewport.height))
    }
}

enum NativeSpeakerFrameGesture {
    /// How close (canvas fraction) the speaker's centre must come to the middle line before it snaps there.
    static let centerSnap = 0.02

    /// `value` pulled onto 0.5 within `centerSnap`, and whether it snapped.
    static func snappedToCenter(_ value: Double) -> (value: Double, snapped: Bool) {
        abs(value - 0.5) <= centerSnap ? (0.5, true) : (value, false)
    }

    /// The largest face Vision finds in a still of the framed speaker alone (canvas-sized, clear around the cut-out),
    /// in canvas fractions with the top-left origin. Nil when there is no face, or Vision fails (SF9: the guide then
    /// falls back to the whole speaker box).
    nonisolated static func faceRect(in image: CGImage) -> CGRect? {
        let request = VNDetectFaceRectanglesRequest()
        do { try VNImageRequestHandler(cgImage: image, options: [:]).perform([request]) } catch { return nil }
        guard let face = request.results?.max(by: { $0.boundingBox.width * $0.boundingBox.height < $1.boundingBox.width * $1.boundingBox.height })
        else { return nil }
        let box = face.boundingBox
        return CGRect(x: box.minX, y: 1 - box.maxY, width: box.width, height: box.height)
    }

    /// `rect` (canvas fractions) after the live gesture: scaled by `scale` about `anchor`, then moved by `translation`.
    static func transformed(_ rect: CGRect, scale: CGFloat, anchor: CGPoint, translation: CGPoint) -> CGRect {
        CGRect(x: anchor.x + (rect.minX - anchor.x) * scale + translation.x,
               y: anchor.y + (rect.minY - anchor.y) * scale + translation.y,
               width: rect.width * scale, height: rect.height * scale)
    }
}
