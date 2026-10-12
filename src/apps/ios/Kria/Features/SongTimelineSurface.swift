import SwiftUI
import UIKit

/// What is selected on the song timeline: a placed clip or an empty spot (by `SongTimelineState.Gap.index`).
enum SongTimelineSelection: Equatable {
    case block(String)
    case gap(Int)
    /// A clip waiting in the tray.
    case tray(String)
}

/// A tray clip being dragged over the lanes: where it would start if released now.
struct SongTimelineExternalDrag: Equatable {
    let mediaID: String
    let rawDelta: Double
}

/// What the lanes ask the card to do. The card owns the arrangement (`SongTimelineState`), the audio and the notices.
struct SongTimelineSurfaceActions {
    var selectBlock: (String) -> Void
    var selectGap: (Int) -> Void
    var clearSelection: () -> Void
    /// A finger took the lanes: playback pauses. Returns whether it was playing, to resume afterwards.
    var scrubBegan: () -> Bool
    var scrubEnded: (Bool) -> Void
    /// A clip was picked up by dragging it: playback pauses and the clip is selected.
    var liftBegan: (String) -> Void
    /// A lifted clip was dropped with this raw start (the card snaps and validates it).
    var commitMove: (String, Double) -> Void
    var nudge: (String, Double) -> Void
    /// VoiceOver's "Move here": the clip starts at the playhead.
    var moveHere: (String) -> Void
    /// Moves the playhead to a time (keeps the audio following when it is playing).
    var seek: (Double) -> Void
}

/// Where the clips are on screen (global coordinates), so the lane recognizer can tell at touch-down whether a finger
/// landed on a clip (it then drags that clip) or on empty lane (it scrubs). Plain storage, not state: it is read on
/// the very first touch sample, before SwiftUI could re-render.
final class SongTimelineLaneGate {
    var blocks: [(mediaID: String, rect: CGRect)] = []
    /// The clip under the finger at touch-down, nil for empty lane or ruler.
    var touchedBlockID: String?
}

/// The song timeline lanes, laid out like the editor's `NativeMiniStrip`: a fixed playhead in the middle of the
/// viewport and every lane positioned from the clock, so panning the lanes seeks the song. Lanes top to bottom: the
/// ruler, the clips (placed takes and empty spots) and the song's waveform, all on ONE time axis.
struct SongTimelineSurface: View {
    enum Layout {
        static let readoutHeight: CGFloat = 20
        static let rulerTop: CGFloat = 22
        static let rulerHeight: CGFloat = 18
        static let clipTop: CGFloat = 46
        static let clipHeight: CGFloat = 64
        static let songTop: CGFloat = 116
        static let songHeight: CGFloat = 44
        static let total: CGFloat = 160
    }

    let state: SongTimelineState
    let question: SongOrderQuestion
    @ObservedObject var clock: SongTimelineClock
    let selection: SongTimelineSelection?
    /// Normalized waveform peaks across the whole song file; empty = a flat track.
    let bars: [Float]
    /// Length of the song's audio on the axis.
    let songSeconds: Double
    let isSending: Bool
    /// A tray clip being dragged over the lanes (the card tracks the finger).
    var externalDrag: SongTimelineExternalDrag?
    let clipNumber: (String) -> Int
    let media: (String) -> CreationAttachedMedia
    let actions: SongTimelineSurfaceActions

    private struct Lift: Equatable {
        let mediaID: String
        let startDelta: Double
        let anchorX: CGFloat
        let clockAtStart: Double
        var fingerX: CGFloat
        var lastSnap: Double?
    }

    /// Non-observed scratch values (touch samples must not re-render the lanes).
    private final class Scratch {
        var panStartTime: Double = 0
        var panResume = false
        var panActive = false
        var isLifting = false
        var globalFrame: CGRect = .zero
        var pinchBase: CGFloat?
    }

    @State private var lift: Lift?
    @State private var scratch = Scratch()
    @State private var gate = SongTimelineLaneGate()
    @State private var edgeScroller = NativeTimelineEdgeScroller()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Axis {
        let now: Double
        let playheadX: CGFloat
        let pps: CGFloat
        let width: CGFloat
        func x(_ time: Double) -> CGFloat {
            SongTimelineMath.x(forTime: time, now: now, playheadX: playheadX, pixelsPerSecond: pps)
        }
        var visibleStart: Double { now - Double(playheadX / pps) }
        var visibleEnd: Double { now + Double((width - playheadX) / pps) }
    }

    // MARK: body

    var body: some View {
        GeometryReader { proxy in
            let width = max(1, proxy.size.width)
            let axis = Axis(now: clock.currentTime, playheadX: width / 2, pps: clock.pixelsPerSecond, width: width)
            let shown = displayedState()
            lanes(axis, shown: shown)
                .frame(width: width, height: Layout.total, alignment: .topLeading)
                .clipped()
                .contentShape(Rectangle())
                .gesture(SongTimelineLaneRecognizer(gate: gate) { handleLane($0) })
                .simultaneousGesture(magnifyGesture)
                .onAppear { clock.fit(viewportWidth: width) }
                .onChange(of: width) { _, newWidth in clock.fit(viewportWidth: newWidth) }
        }
        .frame(height: Layout.total)
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { scratch.globalFrame = $0 }
        .onDisappear { cancelLift() }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-timeline-scroll")
    }

    /// What the lanes draw: the real arrangement, or while a clip is being dragged the arrangement as it would be
    /// if dropped right now (neighbours pushed aside), so the creator sees the result before letting go.
    private func displayedState() -> SongTimelineState {
        if let lift { return state.previewing(lift.mediaID, toDelta: rawDelta(lift)) }
        if let externalDrag { return state.previewing(externalDrag.mediaID, toDelta: externalDrag.rawDelta) }
        return state
    }

    private var draggedID: String? { lift?.mediaID ?? externalDrag?.mediaID }

    private func lanes(_ axis: Axis, shown: SongTimelineState) -> some View {
        let segments = shown.segments()
        let gaps = draggedID == nil ? shown.gaps() : []
        gate.blocks = blockRects(segments, axis)
        return ZStack(alignment: .topLeading) {
            Color.clear.contentShape(Rectangle()).onTapGesture { actions.clearSelection() }.accessibilityHidden(true)
            trackBackgrounds(axis)
            waveform(axis, shown: shown)
            outsideRegions(axis, segments: segments)
            ruler(axis)
            ForEach(gaps) { gapView($0, axis) }
            ForEach(segments) { segment in
                if segment.mediaID == draggedID { ghostView(segment, axis) } else { blockView(segment, axis) }
            }
            floatingBlock(axis)
            playhead(axis)
        }
    }

    /// Each clip's rectangle in global coordinates, topmost last, for the lane recognizer's touch-down test.
    private func blockRects(_ segments: [SongTimelineState.Segment], _ axis: Axis) -> [(mediaID: String, rect: CGRect)] {
        let origin = scratch.globalFrame.origin
        return segments.map { segment in
            let width = max(44, CGFloat(segment.end - segment.start) * axis.pps)
            let local = CGRect(x: axis.x(segment.start), y: Layout.clipTop, width: width, height: Layout.clipHeight)
            return (segment.mediaID, local.offsetBy(dx: origin.x, dy: origin.y))
        }
    }

    // MARK: lanes

    private func trackBackgrounds(_ axis: Axis) -> some View {
        let left = axis.x(0)
        let songWidth = max(0, axis.x(songSeconds) - left)
        return ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 8, style: .continuous).fill(KriaColor.fill)
                .frame(width: songWidth, height: Layout.clipHeight).offset(x: left, y: Layout.clipTop)
            RoundedRectangle(cornerRadius: 8, style: .continuous).fill(KriaColor.fill)
                .frame(width: songWidth, height: Layout.songHeight).offset(x: left, y: Layout.songTop)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func ruler(_ axis: Axis) -> some View {
        let step = SongTimelineMath.tickStep(pixelsPerSecond: axis.pps)
        let last = min(songSeconds, axis.visibleEnd)
        return Canvas { context, _ in
            let first = Int(max(0, axis.visibleStart) / step)
            let end = Int(max(0, last) / step)
            guard first <= end else { return }
            for index in first...end {
                let time = Double(index) * step
                let label = Text(DurationFormatter.clock(time)).font(KriaFont.body(10).weight(.medium)).foregroundColor(KriaColor.mutedInk)
                context.draw(label, at: CGPoint(x: axis.x(time) + 3, y: Layout.rulerTop + 7), anchor: .leading)
                let tick = CGRect(x: axis.x(time), y: Layout.rulerTop + 1, width: 1, height: Layout.rulerHeight - 2)
                context.fill(Path(tick), with: .color(KriaColor.line))
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func waveform(_ axis: Axis, shown: SongTimelineState) -> some View {
        let drawn = bars
        let seconds = songSeconds
        let placed = placedRange(shown)
        return ZStack(alignment: .topLeading) {
            Canvas { context, _ in
                if let placed {
                    let shade = CGRect(x: axis.x(placed.lowerBound), y: Layout.songTop,
                                       width: CGFloat(placed.upperBound - placed.lowerBound) * axis.pps, height: Layout.songHeight)
                    context.fill(Path(roundedRect: shade, cornerRadius: 8), with: .color(KriaColor.sky.opacity(0.35)))
                }
                drawBars(context: &context, bars: drawn, seconds: seconds, axis: axis)
            }
            Label("Song", systemImage: "music.note")
                .font(KriaFont.body(10).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                .labelStyle(.titleAndIcon)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(KriaColor.paper.opacity(0.85), in: Capsule())
                .offset(x: 4, y: Layout.songTop + 3)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func drawBars(context: inout GraphicsContext, bars: [Float], seconds: Double, axis: Axis) {
        let midY = Layout.songTop + Layout.songHeight / 2
        guard !bars.isEmpty, seconds > 0 else {
            let flat = CGRect(x: axis.x(0) + 4, y: midY - 1.5, width: max(0, axis.x(seconds) - axis.x(0) - 8), height: 3)
            context.fill(Path(roundedRect: flat, cornerRadius: 1.5), with: .color(KriaColor.line))
            return
        }
        let perBar = seconds / Double(bars.count)
        let first = max(0, Int(axis.visibleStart / perBar))
        let last = min(bars.count - 1, Int(axis.visibleEnd / perBar))
        guard first <= last else { return }
        let barWidth = max(1, CGFloat(perBar) * axis.pps - 1.5)
        for index in first...last {
            let height = max(2, CGFloat(bars[index]) * (Layout.songHeight - 8))
            let rect = CGRect(x: axis.x(Double(index) * perBar), y: midY - height / 2, width: barWidth, height: height)
            context.fill(Path(roundedRect: rect, cornerRadius: 1), with: .color(KriaColor.zinc.opacity(0.55)))
        }
    }

    private func placedRange(_ shown: SongTimelineState) -> ClosedRange<Double>? {
        let visible = shown.segments().filter { !$0.hidden }
        guard let start = visible.first?.start, let end = visible.map(\.end).max(), end > start else { return nil }
        return start...end
    }

    /// The song before the first clip and after the last one is not in the video: dim it.
    private func outsideRegions(_ axis: Axis, segments: [SongTimelineState.Segment]) -> some View {
        let visible = segments.filter { !$0.hidden }
        let startX = visible.first.map { axis.x($0.start) }
        let endX = visible.map(\.end).max().map { axis.x($0) }
        let height = Layout.songTop + Layout.songHeight - Layout.clipTop
        let songEnd = axis.x(songSeconds)
        return ZStack(alignment: .topLeading) {
            if let startX {
                let left = axis.x(0)
                Rectangle().fill(KriaColor.ink.opacity(0.10))
                    .frame(width: max(0, startX - left), height: height).offset(x: left, y: Layout.clipTop)
            }
            if let endX {
                Rectangle().fill(KriaColor.ink.opacity(0.10))
                    .frame(width: max(0, songEnd - endX), height: height).offset(x: endX, y: Layout.clipTop)
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func playhead(_ axis: Axis) -> some View {
        let time = SongTimelineMath.timecode(clock.currentTime)
        return ZStack(alignment: .topLeading) {
            Rectangle().fill(KriaColor.ink)
                .frame(width: 2, height: Layout.total - Layout.readoutHeight)
                .offset(x: axis.playheadX - 1, y: Layout.readoutHeight)
            Text(time)
                .font(.system(size: 11, weight: .semibold, design: .monospaced)).monospacedDigit()
                .foregroundStyle(KriaColor.ink)
                .padding(.horizontal, 7)
                .frame(height: Layout.readoutHeight - 2)
                .background(KriaColor.butter, in: Capsule())
                .fixedSize()
                .position(x: axis.playheadX, y: Layout.readoutHeight / 2 - 1)
                .accessibilityHidden(true)
        }
        .allowsHitTesting(false)
        .overlay(alignment: .topLeading) { playheadAccessibilityTarget(axis) }
    }

    /// VoiceOver sees the playhead as an adjustable element: swipe up or down to move it a second.
    private func playheadAccessibilityTarget(_ axis: Axis) -> some View {
        Color.clear
            .frame(width: 44, height: Layout.total)
            .allowsHitTesting(false)
            .offset(x: axis.playheadX - 22)
            .accessibilityElement()
            .accessibilityLabel("Playhead")
            .accessibilityValue(SongTimelineMath.timecode(clock.currentTime))
            .accessibilityHint("Swipe up or down to move one second")
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: actions.seek(clock.currentTime + 1)
                case .decrement: actions.seek(clock.currentTime - 1)
                @unknown default: break
                }
            }
            .accessibilityIdentifier("song-timeline-playhead")
    }

    // MARK: blocks and gaps

    private func rawDelta(_ lift: Lift) -> Double {
        SongTimelineMath.liftedDelta(startDelta: lift.startDelta, fingerTravel: lift.fingerX - lift.anchorX,
                                     pixelsPerSecond: clock.pixelsPerSecond, clockShift: clock.currentTime - lift.clockAtStart)
    }

    private func isOnScreen(x: CGFloat, width: CGFloat, axis: Axis) -> Bool {
        x + width > -8 && x < axis.width + 8
    }

    private func blockView(_ segment: SongTimelineState.Segment, _ axis: Axis) -> some View {
        let id = segment.mediaID
        let x = axis.x(segment.start)
        let width = max(44, CGFloat(segment.end - segment.start) * axis.pps)
        let uncertain = question.item(for: id)?.status.isUncertain ?? true
        return blockFace(id: id, width: width, uncertain: uncertain, hidden: segment.hidden,
                         selected: selection == .block(id), lifted: false)
            .onTapGesture { actions.selectBlock(id) }
            .accessibilityElement(children: .ignore)
            .accessibilityAddTraits(selection == .block(id) ? [.isButton, .isSelected] : .isButton)
            .frame(width: width, height: Layout.clipHeight)
            .offset(x: x, y: Layout.clipTop)
            .zIndex(1)
            .allowsHitTesting(isOnScreen(x: x, width: width, axis: axis))
            .accessibilityLabel(blockLabel(segment))
            .accessibilityHint("Double tap to select. Swipe up or down to move by half a second. Drag to move it anywhere on the song.")
            .accessibilityAdjustableAction { direction in
                guard !isSending else { return }
                switch direction {
                case .increment: actions.nudge(id, 0.5)
                case .decrement: actions.nudge(id, -0.5)
                @unknown default: break
                }
            }
            .accessibilityAction(named: "Move later") { if !isSending { actions.nudge(id, 0.5) } }
            .accessibilityAction(named: "Move earlier") { if !isSending { actions.nudge(id, -0.5) } }
            .accessibilityAction(named: "Move here") { if !isSending { actions.moveHere(id) } }
            .accessibilityIdentifier("song-timeline-block-\(id)")
    }

    /// Where the clip being dragged will land (after snapping and pushing), as a soft placeholder.
    private func ghostView(_ segment: SongTimelineState.Segment, _ axis: Axis) -> some View {
        let width = max(44, CGFloat(segment.end - segment.start) * axis.pps)
        return RoundedRectangle(cornerRadius: 10, style: .continuous)
            .fill(KriaColor.butter.opacity(0.6))
            .frame(width: width, height: Layout.clipHeight)
            .offset(x: axis.x(segment.start), y: Layout.clipTop)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
            .accessibilityIdentifier("song-timeline-ghost")
    }

    /// The clip being dragged, following the finger exactly (not snapped).
    @ViewBuilder private func floatingBlock(_ axis: Axis) -> some View {
        if let lift {
            let range = state.range(of: lift.mediaID, delta: rawDelta(lift))
            let width = max(44, CGFloat(range.end - range.start) * axis.pps)
            blockFace(id: lift.mediaID, width: width, uncertain: false, hidden: false, selected: false, lifted: true)
                .frame(width: width, height: Layout.clipHeight)
                .offset(x: axis.x(range.start), y: Layout.clipTop - 4)
                .zIndex(3)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    private func blockLabel(_ segment: SongTimelineState.Segment) -> String {
        let delta = state.delta(of: segment.mediaID) ?? segment.start
        var text = "Clip \(clipNumber(segment.mediaID)), starts at \(DurationFormatter.clock(max(0, delta)))"
        if segment.hidden { text += ", hidden behind another clip" }
        return text
    }

    private func blockFace(id: String, width: CGFloat, uncertain: Bool, hidden: Bool, selected: Bool, lifted: Bool) -> some View {
        let title = hidden ? "Clip \(clipNumber(id)) · hidden" : "Clip \(clipNumber(id))"
        return ZStack(alignment: .bottomLeading) {
            SongTimelineThumbnail(media: media(id), width: width, height: Layout.clipHeight)
                .background(lifted ? KriaColor.butter : KriaColor.sky)
            if selected { KriaColor.sky.opacity(0.4) }
            Text(title)
                .font(KriaFont.body(11).weight(.bold)).foregroundStyle(Color.white)
                .lineLimit(1)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(KriaColor.ink.opacity(0.7), in: Capsule())
                .padding(4)
        }
        .frame(width: width, height: Layout.clipHeight)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(alignment: .bottom) { selectionBar(selected) }
        .overlay(alignment: .topTrailing) { if uncertain { UncertainBadge().offset(x: -3, y: 3) } }
        .opacity(hidden ? 0.5 : 1)
        .shadow(color: lifted ? KriaColor.ink.opacity(0.25) : .clear, radius: 6, y: 3)
        .contentShape(Rectangle())
    }

    @ViewBuilder private func selectionBar(_ selected: Bool) -> some View {
        if selected {
            Capsule().fill(KriaColor.ink).frame(height: 4).padding(.horizontal, 10).padding(.bottom, 2)
        }
    }

    private func gapView(_ gap: SongTimelineState.Gap, _ axis: Axis) -> some View {
        let width = max(8, CGFloat(gap.length) * axis.pps)
        let hit = max(44, width)
        let x = axis.x(gap.start) - (hit - width) / 2
        let selected = selection == .gap(gap.index)
        let label = "Empty spot \(DurationFormatter.clock(gap.start)) to \(DurationFormatter.clock(gap.end))"
        return Button {
            if !isSending { actions.selectGap(gap.index) }
        } label: {
            gapFace(width: width, hit: hit, selected: selected)
        }
        .buttonStyle(.plain)
        .frame(width: hit, height: Layout.clipHeight)
        .offset(x: x, y: Layout.clipTop)
        .allowsHitTesting(isOnScreen(x: x, width: hit, axis: axis))
        .accessibilityLabel(label)
        .accessibilityHint("Double tap to select, then fill it with a clip")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("song-timeline-gap-\(gap.index)")
    }

    /// An empty spot is the one place an outline is right: it is a dashed hole where a clip could go.
    private func gapFace(width: CGFloat, hit: CGFloat, selected: Bool) -> some View {
        ZStack {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(selected ? KriaColor.sky.opacity(0.3) : Color.clear)
                .frame(width: width, height: Layout.clipHeight)
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(KriaColor.zinc, style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                .frame(width: width, height: Layout.clipHeight)
            if width >= 44 {
                VStack(spacing: 2) {
                    Image(systemName: "plus").font(.system(size: 14, weight: .semibold))
                    Text("Empty").font(KriaFont.body(10).weight(.medium))
                }
                .foregroundStyle(KriaColor.mutedInk)
            }
        }
        .frame(width: hit, height: Layout.clipHeight)
        .contentShape(Rectangle())
    }

    // MARK: pan = seek

    /// One recognizer for the whole lane. A finger that lands on a clip drags that clip (immediately, no long press);
    /// one that lands on empty lane or the ruler scrubs the playhead. Either way only a mostly-horizontal drag begins,
    /// so a vertical one still scrolls the chat.
    private func handleLane(_ pan: UIPanGestureRecognizer) {
        let finger = pan.location(in: nil).x
        switch pan.state {
        case .began:
            if let id = gate.touchedBlockID, !isSending {
                let travel = pan.translation(in: pan.view).x
                beginLift(id, anchorX: finger - travel, fingerX: finger)
            } else {
                scratch.panStartTime = clock.currentTime
                scratch.panResume = actions.scrubBegan()
                scratch.panActive = true
            }
        case .changed:
            if scratch.isLifting {
                moveLift(to: finger)
            } else if scratch.panActive {
                let travel = pan.translation(in: pan.view).x
                clock.seek(to: scratch.panStartTime - Double(travel / max(1, clock.pixelsPerSecond)))
            }
        case .ended:
            if scratch.isLifting { dropLift() } else { endScrub() }
        case .cancelled, .failed:
            if scratch.isLifting { cancelLift() } else { endScrub() }
        default:
            break
        }
    }

    private func endScrub() {
        guard scratch.panActive else { return }
        scratch.panActive = false
        actions.scrubEnded(scratch.panResume)
    }

    private var magnifyGesture: some Gesture {
        MagnifyGesture()
            .onChanged { value in
                let base = scratch.pinchBase ?? clock.pixelsPerSecond
                scratch.pinchBase = base
                clock.setPixelsPerSecond(base * value.magnification)
            }
            .onEnded { _ in scratch.pinchBase = nil }
    }

    // MARK: lift and move

    private func beginLift(_ id: String, anchorX: CGFloat, fingerX: CGFloat) {
        guard !isSending, lift == nil, let startDelta = state.delta(of: id) else { return }
        scratch.isLifting = true
        actions.liftBegan(id)
        lift = Lift(mediaID: id, startDelta: startDelta, anchorX: anchorX, clockAtStart: clock.currentTime, fingerX: fingerX, lastSnap: nil)
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        edgeScroller.onTick = { elapsed in tickEdgeScroll(elapsed) }
        edgeScroller.start()
    }

    private func moveLift(to fingerX: CGFloat) {
        guard var current = lift else { return }
        current.fingerX = fingerX
        lift = current
        tickSnapFeedback()
    }

    /// A light tick each time the lifted clip settles onto one of its candidate positions.
    private func tickSnapFeedback() {
        guard var current = lift else { return }
        let candidates = question.item(for: current.mediaID)?.candidates.map(\.deltaS) ?? []
        let snapped = state.snapped(current.mediaID, rawDelta(current))
        let onCandidate = candidates.contains(snapped)
        if onCandidate, current.lastSnap != snapped { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
        current.lastSnap = onCandidate ? snapped : nil
        if current.lastSnap != lift?.lastSnap { lift = current }
    }

    private func tickEdgeScroll(_ elapsed: TimeInterval) {
        guard let current = lift else { return }
        let frame = scratch.globalFrame
        guard frame.width > 0 else { return }
        let velocity = NativeEditorInteraction.edgeAutoScrollVelocity(
            x: current.fingerX, viewport: frame.minX...frame.maxX, edgeZone: 56, maxSpeed: frame.width * 1.2)
        guard velocity != 0 else { return }
        let step = NativeEditorInteraction.edgeAutoScrollStep(velocity: velocity, elapsed: elapsed, pixelsPerSecond: clock.pixelsPerSecond)
        clock.seek(to: clock.currentTime + step)
        tickSnapFeedback()
    }

    private func dropLift() {
        guard let current = lift else { return }
        let raw = rawDelta(current)
        endLift()
        actions.commitMove(current.mediaID, raw)
    }

    private func cancelLift() {
        guard lift != nil else { return }
        endLift()
    }

    private func endLift() {
        edgeScroller.stop()
        edgeScroller.onTick = nil
        scratch.isLifting = false
        lift = nil
    }
}

/// A take's still, tiled across the block so a long block reads as footage (the picker's thumbnail is a fixed 52x64).
/// The image is loaded once, not on every frame the playhead moves.
private struct SongTimelineThumbnail: View {
    let media: CreationAttachedMedia
    let width: CGFloat
    let height: CGFloat
    @State private var image: UIImage?

    private var tileWidth: CGFloat { max(height * 9 / 16, width / 24) }

    var body: some View {
        ZStack {
            if let image {
                HStack(spacing: 0) {
                    ForEach(0..<tileCount, id: \.self) { _ in
                        Image(uiImage: image).resizable().scaledToFill().frame(width: tileWidth, height: height).clipped()
                    }
                }
                .frame(width: width, height: height, alignment: .leading)
            } else if let url = media.previewURL {
                AsyncImage(url: url) { $0.resizable().scaledToFill() } placeholder: { placeholder }
            } else {
                placeholder
            }
        }
        .frame(width: width, height: height)
        .clipped()
        .task(id: media.id) { image = UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path) }
        .accessibilityHidden(true)
    }

    private var tileCount: Int { max(1, Int((width / tileWidth).rounded(.up))) }
    private var placeholder: some View { Image(systemName: "video").foregroundStyle(KriaColor.mutedInk) }
}

// MARK: - Lane recognizer

/// Pan that only BEGINS for a mostly-horizontal drag, so the chat's own vertical scroll keeps working over the
/// lanes (a SwiftUI `DragGesture` here swallows it, see `DragSelectRecognizer`). At touch-down it records which clip
/// (if any) is under the finger; the handler then drags that clip or, for empty lane, scrubs.
struct SongTimelineLaneRecognizer: UIGestureRecognizerRepresentable {
    let gate: SongTimelineLaneGate
    let handler: (UIPanGestureRecognizer) -> Void

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        let gate: SongTimelineLaneGate
        init(gate: SongTimelineLaneGate) { self.gate = gate }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            let point = touch.location(in: nil)
            gate.touchedBlockID = gate.blocks.last { $0.rect.contains(point) }?.mediaID
            return true
        }

        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
            guard let pan = gestureRecognizer as? UIPanGestureRecognizer else { return false }
            let translation = pan.translation(in: pan.view)
            return DragSelect.isHorizontalIntent(translation: CGSize(width: translation.x, height: translation.y))
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator(gate: gate) }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let pan = UIPanGestureRecognizer()
        pan.maximumNumberOfTouches = 1
        pan.delegate = context.coordinator
        return pan
    }

    func updateUIGestureRecognizer(_ recognizer: UIPanGestureRecognizer, context: Context) {}

    func handleUIGestureRecognizerAction(_ recognizer: UIPanGestureRecognizer, context: Context) { handler(recognizer) }
}
