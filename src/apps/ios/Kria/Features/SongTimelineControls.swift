import SwiftUI

// The song timeline's controls around the lanes (KRI-575): the transport (the play button), the action bar for
// the selected clip or empty spot, and the tray of clips left off the song.

/// Filled capsule for the timeline's actions: fills not outlines, 44 pt tall.
struct SongTimelineChipStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var prominent = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(14).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 14)
            .frame(minWidth: 44, minHeight: 44)
            .background(prominent ? KriaColor.butter : KriaColor.paper, in: Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.7 : 1) : 0.4)
    }
}

/// Lays children out left to right and wraps them onto new rows, so the action chips fit any width.
struct SongTimelineWrap: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(subviews, width: proposal.width ?? .infinity)
        return CGSize(width: rows.map(\.width).max() ?? 0, height: rows.reduce(0) { $0 + $1.height } + CGFloat(max(0, rows.count - 1)) * spacing)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(subviews, width: bounds.width) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + spacing
        }
    }

    private struct Row { var indices: [Int] = []; var width: CGFloat = 0; var height: CGFloat = 0 }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> [Row] {
        var rows: [Row] = [Row()]
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let used = rows[rows.count - 1].width
            if !rows[rows.count - 1].indices.isEmpty, used + spacing + size.width > width { rows.append(Row()) }
            var row = rows[rows.count - 1]
            row.width += (row.indices.isEmpty ? 0 : spacing) + size.width
            row.height = max(row.height, size.height)
            row.indices.append(index)
            rows[rows.count - 1] = row
        }
        return rows
    }
}

// MARK: - Transport

/// Play/pause, previous/next clip, the time readout and zoom. Observes the clock; nothing else re-renders per frame.
struct SongTimelineTransport: View {
    @ObservedObject var clock: SongTimelineClock
    let stops: [SongTimelineMath.Stop]
    let canPlay: Bool
    let onPlayPause: () -> Void
    let onPrevious: () -> Void
    let onNext: () -> Void

    var body: some View {
        let hasPrevious = SongTimelineMath.previousStop(before: clock.currentTime, in: stops) != nil
        let hasNext = SongTimelineMath.nextStop(after: clock.currentTime, in: stops) != nil
        HStack(spacing: 6) {
            stepButton("backward.end.fill", label: "Previous clip", id: "song-timeline-prev", enabled: hasPrevious, action: onPrevious)
            playButton
            stepButton("forward.end.fill", label: "Next clip", id: "song-timeline-next", enabled: hasNext, action: onNext)
            timeReadout
            Spacer(minLength: 0)
            stepButton("minus.magnifyingglass", label: "Zoom out", id: "song-timeline-zoom-out", enabled: true) { clock.zoom(by: 1 / 1.4) }
            stepButton("plus.magnifyingglass", label: "Zoom in", id: "song-timeline-zoom-in", enabled: true) { clock.zoom(by: 1.4) }
        }
    }

    private var playButton: some View {
        Button(action: onPlayPause) {
            Image(systemName: clock.isPlaying ? "pause.fill" : "play.fill")
                .font(.system(size: 18, weight: .bold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 48, height: 48)
                .background(KriaColor.butter, in: Circle())
                .opacity(canPlay ? 1 : 0.4)
        }
        .buttonStyle(.plain)
        .disabled(!canPlay)
        .accessibilityLabel(clock.isPlaying ? "Pause song" : "Play song")
        .accessibilityIdentifier("song-timeline-play")
    }

    private var timeReadout: some View {
        let now = SongTimelineMath.timecode(clock.currentTime)
        let total = DurationFormatter.clock(clock.duration)
        return Text("\(now) / \(total)")
            .font(.system(size: 12, weight: .medium, design: .monospaced)).monospacedDigit()
            .foregroundStyle(KriaColor.mutedInk)
            .lineLimit(1).minimumScaleFactor(0.6)
            .padding(.leading, 2)
            .accessibilityElement()
            .accessibilityLabel("Current time")
            .accessibilityValue("\(now) of \(total)")
            .accessibilityIdentifier("song-timeline-current-time")
    }

    private func stepButton(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
                .opacity(enabled ? 1 : 0.35)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
    }
}

// MARK: - Action bar

/// What you can do with the selected clip (placed or in the tray) or empty spot. Any clip can start at the playhead
/// ("Move here" / "Add here"); a placed one can also be nudged or sent back to the tray. Dragging a clip works too.
struct SongTimelineActionBar: View {
    struct Actions {
        var moveToTray: () -> Void
        var nudgeEarlier: () -> Void
        var nudgeLater: () -> Void
        /// Starts the selected clip at the playhead.
        var here: () -> Void
        var preview: () -> Void
        var fill: () -> Void
    }

    let selection: SongTimelineSelection?
    let clipNumber: Int
    let isSending: Bool
    let actions: Actions

    var body: some View {
        Group {
            switch selection {
            case .block:
                SongTimelineWrap(spacing: 8) {
                    chip("Move here", id: "here", label: "Move this clip to start at the playhead", prominent: true, action: actions.here)
                    chip("−0.5 s", id: "earlier", label: "Move half a second earlier", action: actions.nudgeEarlier)
                    chip("+0.5 s", id: "later", label: "Move half a second later", action: actions.nudgeLater)
                    chip("Move to tray", id: "to-tray", action: actions.moveToTray)
                    chip("Preview clip", id: "preview", action: actions.preview)
                }
            case .tray:
                SongTimelineWrap(spacing: 8) {
                    chip("Add here", id: "here", label: "Add this clip to start at the playhead", prominent: true, action: actions.here)
                    chip("Preview clip", id: "preview", action: actions.preview)
                }
            case .gap:
                SongTimelineWrap(spacing: 8) {
                    chip("Fill with a clip", id: "fill", prominent: true, action: actions.fill)
                }
            case nil:
                Text("Drag a clip, or pick one and press Move here at the playhead.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(minHeight: 44, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-timeline-actions")
    }

    private func chip(_ title: String, id: String, label: String? = nil, prominent: Bool = false, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(SongTimelineChipStyle(prominent: prominent))
            .disabled(isSending)
            .accessibilityLabel(label ?? title)
            .accessibilityIdentifier("song-timeline-action-\(id)")
    }
}

// MARK: - Tray

/// The clips left off the song. Tap one to select it (then Add here, or drag it); press and hold, then drag, to lift
/// it onto the lanes: the card shows a floating copy under the finger and places the clip where it is released.
struct SongTimelineTray: View {
    struct Drag {
        var began: (String) -> Void
        var moved: (String, CGPoint) -> Void
        var ended: (String, CGPoint) -> Void
        var cancelled: () -> Void
    }

    let clips: [(id: String, media: CreationAttachedMedia, number: Int)]
    let isSending: Bool
    let selectedID: String?
    let drag: Drag
    let onTap: (String) -> Void
    let onMoveHere: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Tray").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(clips, id: \.id) { clip in chip(clip) }
                }
            }
        }
    }

    private func chip(_ clip: (id: String, media: CreationAttachedMedia, number: Int)) -> some View {
        let selected = selectedID == clip.id
        return VStack(spacing: 4) {
            CreationAttachmentThumbnail(media: clip.media)
                .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
            Text("Clip \(clip.number)").font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.ink)
        }
        .padding(6)
        .frame(minWidth: 52, minHeight: 44)
        .background(selected ? KriaColor.sky.opacity(0.45) : Color.clear, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .contentShape(Rectangle())
        .onTapGesture { if !isSending { onTap(clip.id) } }
        .gesture(SongTimelineTrayDragRecognizer(isEnabled: !isSending) { handleDrag($0, clip.id) })
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityLabel("Clip \(clip.number), in the tray")
        .accessibilityHint("Double tap to select, then use Add here. Press and hold, then drag, to place it on the song.")
        .accessibilityAction(named: "Add here") { if !isSending { onMoveHere(clip.id) } }
        .accessibilityIdentifier("song-timeline-tray-\(clip.id)")
    }

    private func handleDrag(_ press: UILongPressGestureRecognizer, _ id: String) {
        let point = press.location(in: nil)
        switch press.state {
        case .began: drag.began(id); drag.moved(id, point)
        case .changed: drag.moved(id, point)
        case .ended: drag.ended(id, point)
        case .cancelled, .failed: drag.cancelled()
        default: break
        }
    }
}

/// Press and hold (0.25 s) then drag. Holding first keeps a quick sideways flick scrolling the tray, and the
/// recognizer fails if the finger moves before the hold completes.
struct SongTimelineTrayDragRecognizer: UIGestureRecognizerRepresentable {
    let isEnabled: Bool
    let handler: (UILongPressGestureRecognizer) -> Void

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var isEnabled = true
        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool { isEnabled }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { false }
    }

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UILongPressGestureRecognizer {
        let press = UILongPressGestureRecognizer()
        press.minimumPressDuration = 0.25
        press.allowableMovement = 10
        press.delegate = context.coordinator
        return press
    }

    func updateUIGestureRecognizer(_ recognizer: UILongPressGestureRecognizer, context: Context) { context.coordinator.isEnabled = isEnabled }

    func handleUIGestureRecognizerAction(_ recognizer: UILongPressGestureRecognizer, context: Context) { handler(recognizer) }
}

/// Switches the scroll views around the tray off while a clip is being dragged out of it (the chat's vertical scroll
/// and the tray's own), so the drag does not also scroll them. The probe view sits in the tray, so the scroll views
/// are found by walking up from it.
@MainActor final class SongTimelineScrollFreezer {
    fileprivate weak var anchor: UIView?
    private var frozen: [UIScrollView] = []

    func freeze() {
        thaw()
        var view = anchor?.superview
        while let current = view {
            if let scroll = current as? UIScrollView, scroll.panGestureRecognizer.isEnabled {
                scroll.panGestureRecognizer.isEnabled = false
                frozen.append(scroll)
            }
            view = current.superview
        }
    }

    func thaw() {
        for scroll in frozen { scroll.panGestureRecognizer.isEnabled = true }
        frozen = []
    }
}

struct SongTimelineScrollFreezeProbe: UIViewRepresentable {
    let freezer: SongTimelineScrollFreezer
    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        freezer.anchor = view
        return view
    }
    func updateUIView(_ view: UIView, context: Context) { freezer.anchor = view }
}
