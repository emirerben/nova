import CoreGraphics
import Foundation

/// A value-only timeline projection. It is safe to rebuild from a document
/// snapshot and contains no playback or SwiftUI state.
struct NativeEditorTimelineItem: Equatable, Hashable, Sendable {
    let selection: EditorSelection
    let start: TimeInterval
    let end: TimeInterval
    let zIndex: Int
    let sourceIndex: Int

    init(selection: EditorSelection, start: TimeInterval, end: TimeInterval, zIndex: Int = 0, sourceIndex: Int = 0) {
        self.selection = selection; self.start = start; self.end = end; self.zIndex = zIndex; self.sourceIndex = sourceIndex
    }

    var id: String { selection.id }
    var kind: EditorSelectionKind { selection.kind }
}

struct NativeEditorTimelineLane: Equatable, Sendable {
    let item: NativeEditorTimelineItem
    let lane: Int
}

struct NativeEditorClipWindow: Equatable, Sendable {
    let sourceIndex: Int
    let start: TimeInterval
    let end: TimeInterval
    let overlapBefore: TimeInterval
}

/// One immutable output-clock projection shared by the filmstrip, timed
/// lanes, preview visibility, ruler, transport, and inverse scrubbing. Timed
/// records remain stored in the server's clip-only base clock; an inserted
/// Carousel is projected only at these view/playback boundaries.
struct NativeEditorTimelineProjection: Equatable, Sendable {
    let baseClipWindows: [NativeEditorClipWindow]
    let clipWindows: [NativeEditorClipWindow]
    let carouselItem: NativeEditorTimelineItem?
    let baseInsertionTime: TimeInterval?
    let downstreamShift: TimeInterval
    let totalDuration: TimeInterval

    func projectBaseTime(_ time: TimeInterval) -> TimeInterval {
        let safe = max(0, time)
        guard let baseInsertionTime, downstreamShift > 0 else { return roundedMillis(safe) }
        // Exact insertion-boundary points are right-biased.
        return roundedMillis(safe < baseInsertionTime ? safe : safe + downstreamShift)
    }

    func projectBaseInterval(start: TimeInterval, end: TimeInterval) -> (start: TimeInterval, end: TimeInterval) {
        let safeStart = max(0, start)
        let safeEnd = max(safeStart, end)
        guard let baseInsertionTime, downstreamShift > 0 else {
            return (roundedMillis(safeStart), roundedMillis(safeEnd))
        }
        let projectedStart = safeStart < baseInsertionTime ? safeStart : safeStart + downstreamShift
        // Crossing intervals keep their authored start and extend their end.
        let projectedEnd = safeEnd < baseInsertionTime ? safeEnd : safeEnd + downstreamShift
        return (roundedMillis(projectedStart), roundedMillis(projectedEnd))
    }

    func unprojectOutputTime(_ time: TimeInterval) -> TimeInterval {
        let safe = max(0, time)
        guard let baseInsertionTime, downstreamShift > 0 else { return roundedMillis(safe) }
        let carouselStart = carouselItem?.start ?? baseInsertionTime
        if safe < carouselStart { return roundedMillis(safe) }
        if safe < baseInsertionTime + downstreamShift { return roundedMillis(baseInsertionTime) }
        return roundedMillis(safe - downstreamShift)
    }

    private func roundedMillis(_ value: TimeInterval) -> TimeInterval {
        (value * 1_000).rounded() / 1_000
    }
}

enum NativeEditorInteraction {
    /// Detect edge crossings even when a drag event skips the alignment pixel.
    static func crossesAlignment(previousStart: TimeInterval, previousEnd: TimeInterval,
                                 start: TimeInterval, end: TimeInterval, boundaries: [TimeInterval]) -> Bool {
        boundaries.filter(\.isFinite).contains { boundary in
            [(previousStart, start), (previousEnd, end)].contains { previous, current in
                (previous < boundary && current >= boundary) || (previous > boundary && current <= boundary)
            }
        }
    }

    static func alignmentBoundary(start: TimeInterval, end: TimeInterval,
                                  boundaries: [TimeInterval], tolerance: TimeInterval) -> TimeInterval? {
        guard start.isFinite, end.isFinite, tolerance.isFinite, tolerance >= 0 else { return nil }
        return boundaries.filter(\.isFinite)
            .filter { min(abs(start - $0), abs(end - $0)) <= tolerance }
            .min { min(abs(start - $0), abs(end - $0)) < min(abs(start - $1), abs(end - $1)) }
    }

    static let minimumHitTarget: CGFloat = 44

    /// KRI-166: the gap after the last clip that should read as the branded
    /// Kria outro placeholder. A branded preview trusts a small gap (0.05s
    /// epsilon, never a bare `> 0` — float-residue rounds to millis
    /// elsewhere in this file); anywhere else a gap could just be encode
    /// rounding, so it needs a full second — a conservative round number
    /// below the outro's real 1.6s length, chosen for simplicity rather
    /// than matching it exactly.
    static func outroRange(
        lastClipEnd: TimeInterval,
        playbackDuration: TimeInterval,
        isBrandedPreview: Bool
    ) -> ClosedRange<TimeInterval>? {
        guard lastClipEnd.isFinite, playbackDuration.isFinite, playbackDuration >= lastClipEnd else { return nil }
        let threshold: TimeInterval = isBrandedPreview ? 0.05 : 1.0
        guard playbackDuration - lastClipEnd >= threshold else { return nil }
        return lastClipEnd...playbackDuration
    }

    /// Timeline intervals are half-open. An item ending exactly at the clock
    /// is inactive, preventing boundary captions/text from flashing together.
    static func isVisible(start: TimeInterval, end: TimeInterval, at time: TimeInterval) -> Bool {
        start.isFinite && end.isFinite && time.isFinite && start <= time && time < end
    }

    static func visible(_ items: [NativeEditorTimelineItem], at time: TimeInterval) -> [NativeEditorTimelineItem] {
        items.filter { isVisible(start: $0.start, end: $0.end, at: time) }
    }

    /// Lower z-indexes are painted first. Equal z-indexes use source order,
    /// then identity, so overlap cycling is deterministic across reloads.
    static func previewOrder(_ items: [NativeEditorTimelineItem]) -> [NativeEditorTimelineItem] {
        items.sorted {
            if $0.zIndex != $1.zIndex { return $0.zIndex < $1.zIndex }
            if $0.sourceIndex != $1.sourceIndex { return $0.sourceIndex < $1.sourceIndex }
            if $0.selection.kind != $1.selection.kind { return $0.selection.kind.rawValue < $1.selection.kind.rawValue }
            return $0.id < $1.id
        }
    }

    /// Returns topmost-first candidates, advancing after the current item.
    static func cycleSelection(in items: [NativeEditorTimelineItem], at time: TimeInterval, current: EditorSelection? = nil) -> EditorSelection? {
        let candidates = previewOrder(visible(items, at: time)).reversed()
        guard !candidates.isEmpty else { return nil }
        guard let current, let index = candidates.firstIndex(where: { $0.selection == current }) else { return candidates.first?.selection }
        return candidates[candidates.index(after: index) == candidates.endIndex ? candidates.startIndex : candidates.index(after: index)].selection
    }

    static func x(forTime time: TimeInterval, duration: TimeInterval, width: CGFloat) -> CGFloat {
        guard duration.isFinite, duration > 0, width.isFinite, width > 0, time.isFinite else { return 0 }
        return CGFloat(min(max(time / duration, 0), 1)) * width
    }

    static func time(forX x: CGFloat, duration: TimeInterval, width: CGFloat) -> TimeInterval {
        guard duration.isFinite, duration > 0, width.isFinite, width > 0, x.isFinite else { return 0 }
        return min(max(TimeInterval(x / width) * duration, 0), duration)
    }

    /// Signed auto-scroll velocity (points/second) for a finger at `x` during
    /// a timeline drag. Zero away from the edges; ramps quadratically toward
    /// `maxSpeed` as the finger nears either edge. `edgeZone` is capped to a
    /// quarter of the viewport width so the two zones can never overlap on a
    /// narrow screen.
    static func edgeAutoScrollVelocity(x: CGFloat, viewport: ClosedRange<CGFloat>, edgeZone: CGFloat, maxSpeed: CGFloat) -> CGFloat {
        guard x.isFinite, viewport.lowerBound.isFinite, viewport.upperBound.isFinite,
              edgeZone.isFinite, edgeZone > 0, maxSpeed.isFinite, maxSpeed > 0 else { return 0 }
        let width = viewport.upperBound - viewport.lowerBound
        guard width > 0 else { return 0 }
        let zone = min(edgeZone, width / 4)
        guard zone > 0 else { return 0 }
        if x <= viewport.lowerBound + zone {
            let depth = min(1, max(0, (viewport.lowerBound + zone - x) / zone))
            return -maxSpeed * depth * depth
        }
        if x >= viewport.upperBound - zone {
            let depth = min(1, max(0, (x - (viewport.upperBound - zone)) / zone))
            return maxSpeed * depth * depth
        }
        return 0
    }

    /// Converts a per-tick auto-scroll velocity into a clock delta (seconds).
    /// `elapsed` is clamped to one 30fps frame so a main-thread hitch (a
    /// debugger pause, a heavy render) can never jump the timeline in a
    /// single tick.
    static func edgeAutoScrollStep(velocity: CGFloat, elapsed: TimeInterval, pixelsPerSecond: CGFloat) -> TimeInterval {
        guard velocity.isFinite, elapsed.isFinite, elapsed > 0,
              pixelsPerSecond.isFinite, pixelsPerSecond > 0 else { return 0 }
        let bounded = min(elapsed, 1.0 / 30.0)
        return TimeInterval(velocity * bounded) / TimeInterval(pixelsPerSecond)
    }

    /// Expands a visual region without moving its center until both dimensions
    /// meet the 44-point accessibility target.
    static func hitRect(_ rect: CGRect, minimum: CGFloat = minimumHitTarget) -> CGRect {
        guard minimum.isFinite, minimum > 0 else { return rect }
        let dx = max(0, (minimum - rect.width) / 2)
        let dy = max(0, (minimum - rect.height) / 2)
        return rect.insetBy(dx: -dx, dy: -dy)
    }

    static func contains(_ point: CGPoint, in rect: CGRect, rotationDegrees: Double) -> Bool {
        let angle = -rotationDegrees * .pi / 180
        let x = point.x - rect.midX, y = point.y - rect.midY
        let unrotated = CGPoint(x: rect.midX + x * cos(angle) - y * sin(angle),
                                y: rect.midY + x * sin(angle) + y * cos(angle))
        return hitRect(rect).contains(unrotated)
    }

    /// Greedy interval coloring. Half-open intervals that touch at a boundary
    /// share a lane; preserving source order keeps the result stable.
    static func packLanes(_ items: [NativeEditorTimelineItem]) -> [NativeEditorTimelineLane] {
        var laneEnds: [TimeInterval] = []
        return items.enumerated().sorted {
            if $0.element.start != $1.element.start { return $0.element.start < $1.element.start }
            if $0.element.end != $1.element.end { return $0.element.end < $1.element.end }
            return $0.offset < $1.offset
        }.map { _, item in
            let lane = laneEnds.firstIndex(where: { item.start >= $0 }) ?? laneEnds.count
            if lane == laneEnds.count { laneEnds.append(item.end) } else { laneEnds[lane] = item.end }
            return NativeEditorTimelineLane(item: item, lane: lane)
        }
    }

    /// Mirrors the server/web slot walk: transition overlap is owned by the
    /// left clip, capped to 30% of both neighbors and ignored below 100 ms.
    static func transitionOverlap(
        left: EditorTimelineSlot,
        leftDuration: TimeInterval,
        rightDuration: TimeInterval
    ) -> TimeInterval {
        guard left.transitionAfter != "cut" else { return 0 }
        let requested = left.transitionDurationS ?? 0.3
        let overlap = min(0.3, requested, leftDuration * 0.3, rightDuration * 0.3)
        return overlap >= 0.1 ? roundedMillis(overlap) : 0
    }

    // MARK: Clip reorder (drag a clip block to a new position)

    /// Moves one slot and leaves the cut after each POSITION where it was.
    /// A transition's overlap is owned by the left clip and the last clip's
    /// transition is ignored, so a transition that travelled with its clip
    /// would change the video's length whenever the last clip moved. Keeping
    /// them by position means a reorder only permutes who plays where.
    /// (Overlaps are still capped at 30% of each neighbour, so clips under
    /// about 1 s can shift the total by a few milliseconds.)
    static func reorderedSlots(_ slots: [EditorTimelineSlot], from: Int, to: Int) -> [EditorTimelineSlot] {
        guard slots.indices.contains(from), slots.indices.contains(to), from != to else { return slots }
        let transitions = slots.map { ($0.transitionAfter, $0.transitionDurationS) }
        var moved = slots
        moved.insert(moved.remove(at: from), at: to)
        for index in moved.indices {
            moved[index].transitionAfter = transitions[index].0
            moved[index].transitionDurationS = transitions[index].1
        }
        return moved
    }

    /// Where a dragged clip would land. Clip widths differ, so this compares
    /// the lifted block's centre with the OTHER clips' original midpoints
    /// (static, so the answer cannot flicker as neighbours slide aside).
    static func clipReorderTarget(
        windows: [(start: TimeInterval, end: TimeInterval)],
        from: Int,
        liftedCenter: TimeInterval
    ) -> Int {
        guard windows.indices.contains(from) else { return from }
        return windows.enumerated().reduce(0) { count, entry in
            entry.offset != from && (entry.element.start + entry.element.end) / 2 < liftedCenter ? count + 1 : count
        }
    }

    /// The clip windows after moving `from` to `to`, in their new order. The
    /// gap before each position (negative = a transition overlap, positive =
    /// a carousel splice) stays with the position, so the total is unchanged.
    static func reorderedClipWindows(
        windows: [(start: TimeInterval, end: TimeInterval)],
        from: Int,
        to: Int
    ) -> [(start: TimeInterval, end: TimeInterval)] {
        guard windows.indices.contains(from), windows.indices.contains(to), from != to else { return windows }
        var durations = windows.map { $0.end - $0.start }
        durations.insert(durations.remove(at: from), at: to)
        var cursor = windows.first?.start ?? 0
        return durations.enumerated().map { index, duration in
            if index > 0 { cursor += windows[index].start - windows[index - 1].end }
            let start = cursor
            cursor = start + duration
            return (roundedMillis(start), roundedMillis(cursor))
        }
    }

    static func timelineProjection(
        slots: [EditorTimelineSlot],
        carousel: [String: JSONValue]?
    ) -> NativeEditorTimelineProjection {
        let speech = slots.contains { $0.id?.hasPrefix("native-phone-talking-source") == true }
        let renderedClock = slots.contains { !$0.removed && $0.raw["timing_mode"]?.stringValue == "rendered" }
        let continuousClock = speech || renderedClock
        var baseWindows: [NativeEditorClipWindow] = []
        // A source-window marker opts the whole timeline into the server's
        // 30fps layout clock: downstream legacy slots and transition edges
        // must share it or millisecond rounding accumulates frame drift.
        let usesSourceFrameClock = !continuousClock && slots.contains { !$0.removed && sourcePreservingOutputDuration($0) != nil }
        var cursor: TimeInterval = 0
        var previous: (slot: EditorTimelineSlot, duration: TimeInterval)?
        for (index, slot) in slots.enumerated() where !slot.removed {
            // KRI-852: source-backed speech must use the same continuous source clock as
            // its recipe (`speech_cut_state.authored_speech_windows`: duration_s on an
            // unrounded cursor, segments down to 1 ms); rounding each segment, to the
            // millisecond or to the 30fps frame clock, accumulates caption drift.
            // KRI-689: every other timeline follows the server's layout clock, 30fps
            // once a source-window marker is present, else milliseconds.
            let minimum = slot.id?.hasPrefix("native-phone-talking-source") == true ? 0.001 : 0.1
            let rawDuration = speech
                ? max(minimum, slot.durationS ?? minimum)
                : sourcePreservingOutputDuration(slot) ?? max(0.1, slot.durationS ?? 0.1)
            let duration = usesSourceFrameClock ? roundedFrame(rawDuration) : rawDuration
            let overlap = previous.map { prior -> Double in
                if continuousClock {
                    guard prior.slot.transitionAfter != "cut" else { return 0 }
                    let raw = min(0.3, prior.slot.transitionDurationS ?? 0.3, prior.duration * 0.3, duration * 0.3)
                    return raw >= 0.1 ? raw : 0
                }
                return timelineTransitionOverlap(left: prior.slot, leftDuration: prior.duration, rightDuration: duration, frameClock: usesSourceFrameClock)
            } ?? 0
            let start = continuousClock ? cursor - overlap : (usesSourceFrameClock ? roundedFrame(cursor - overlap) : roundedMillis(cursor - overlap))
            let end = continuousClock ? start + duration : (usesSourceFrameClock ? roundedFrame(start + duration) : roundedMillis(start + duration))
            baseWindows.append(
                NativeEditorClipWindow(
                    sourceIndex: index,
                    start: start,
                    end: end,
                    overlapBefore: overlap
                )
            )
            cursor = end
            previous = (slot, duration)
        }

        let baseDuration = baseWindows.last?.end ?? 0
        guard let carousel,
              let carouselDuration = carouselDuration(carousel),
              carouselDuration > 0 else {
            return NativeEditorTimelineProjection(
                baseClipWindows: baseWindows,
                clipWindows: baseWindows,
                carouselItem: nil,
                baseInsertionTime: nil,
                downstreamShift: 0,
                totalDuration: baseDuration
            )
        }

        let position = carousel["position"]?.stringValue ?? "intro"
        let insertionIndex: Int
        switch position {
        case "outro": insertionIndex = baseWindows.count
        case "middle": insertionIndex = baseWindows.count / 2
        default: insertionIndex = 0
        }
        let before = insertionIndex > 0 ? baseWindows[insertionIndex - 1] : nil
        let after = insertionIndex < baseWindows.count ? baseWindows[insertionIndex] : nil
        let baseInsertion = after?.start ?? before?.end ?? 0
        let incoming = carouselBoundaryOverlap(
            kind: carousel["transition_in"]?.stringValue,
            requested: carousel["transition_in_duration_s"]?.numberValue,
            beforeDuration: before.map { $0.end - $0.start },
            afterDuration: carouselDuration
        )
        let outgoing = carouselBoundaryOverlap(
            kind: carousel["transition_out"]?.stringValue,
            requested: carousel["transition_out_duration_s"]?.numberValue,
            beforeDuration: carouselDuration,
            afterDuration: after.map { $0.end - $0.start }
        )
        // Carousel timing is positional. Persisted absolute timestamps are
        // stale after clip edits and are not part of the ripple-v1 contract;
        // derive the splice from the active clip windows every time.
        func projectTime(_ value: TimeInterval) -> TimeInterval {
            renderedClock ? value : roundedMillis(value)
        }
        let carouselStart = projectTime(before.map { $0.end - incoming } ?? 0)
        let carouselEnd = projectTime(carouselStart + carouselDuration)
        let shift = max(
            0,
            projectTime(
                after.map { carouselStart + carouselDuration - outgoing - $0.start }
                    ?? (carouselDuration - incoming)
            )
        )
        let projectedWindows = baseWindows.enumerated().map { index, window in
            guard index >= insertionIndex else { return window }
            return NativeEditorClipWindow(
                sourceIndex: window.sourceIndex,
                start: projectTime(window.start + shift),
                end: projectTime(window.end + shift),
                overlapBefore: index == insertionIndex ? outgoing : window.overlapBefore
            )
        }
        let carouselItem = NativeEditorTimelineItem(
            selection: EditorSelection(
                kind: .carousel,
                id: carousel["id"]?.stringValue ?? "carousel-block"
            ),
            start: carouselStart,
            end: carouselEnd,
            zIndex: 180,
            sourceIndex: insertionIndex
        )
        return NativeEditorTimelineProjection(
            baseClipWindows: baseWindows,
            clipWindows: projectedWindows,
            carouselItem: carouselItem,
            baseInsertionTime: baseInsertion,
            downstreamShift: shift,
            totalDuration: max(carouselItem.end, projectedWindows.last?.end ?? 0)
        )
    }

    private static func timelineTransitionOverlap(
        left: EditorTimelineSlot, leftDuration: TimeInterval, rightDuration: TimeInterval, frameClock: Bool
    ) -> TimeInterval {
        guard left.transitionAfter != "cut" else { return 0 }
        let requested = left.transitionDurationS ?? 0.3
        let overlap = min(0.3, requested, leftDuration * 0.3, rightDuration * 0.3)
        guard overlap >= 0.1 else { return 0 }
        return frameClock ? roundedFrame(overlap) : roundedMillis(overlap)
    }

    private static func roundedFrame(_ value: TimeInterval) -> TimeInterval {
        (value * 30).rounded(.toNearestOrEven) / 30
    }

    /// `source_end_s` opts a slot into the exact source-window contract. The
    /// server places that window on its 30fps clock using ties-to-even; retain
    /// the raw end itself so future rate changes recover the exact source span.
    /// Legacy duration-only slots retain their millisecond projection.
    static func sourcePreservingOutputDuration(sourceStart: Double, sourceEnd: Double, rate: Double, renderedClock: Bool = false) -> TimeInterval? {
        guard sourceStart.isFinite, sourceEnd.isFinite, sourceEnd > sourceStart,
              rate.isFinite, rate > 0 else { return nil }
        let duration = (sourceEnd - sourceStart) / rate
        guard duration.isFinite else { return nil }
        return renderedClock ? max(0.1, duration) : max(0.1, (duration * 30).rounded(.toNearestOrEven) / 30)
    }

    private static func sourcePreservingOutputDuration(_ slot: EditorTimelineSlot) -> TimeInterval? {
        if slot.raw["timing_mode"]?.stringValue == "rendered",
           let end = slot.raw["source_end_s"]?.numberValue {
            let rate = slot.raw["playback_rate"]?.numberValue ?? 1
            guard end.isFinite, end > slot.inS, rate.isFinite, rate > 0 else { return nil }
            return (end - slot.inS) / rate
        }
        return sourcePreservingOutputDuration(
            sourceStart: slot.inS,
            sourceEnd: slot.raw["source_end_s"]?.numberValue ?? .nan,
            rate: slot.raw["playback_rate"]?.numberValue ?? 1
        )
    }

    private static func carouselDuration(_ raw: [String: JSONValue]) -> TimeInterval? {
        if let duration = raw["duration_s"]?.numberValue ?? raw["duration"]?.numberValue {
            return duration
        }
        if let start = raw["start_s"]?.numberValue, let end = raw["end_s"]?.numberValue {
            return end - start
        }
        return nil
    }

    private static func carouselBoundaryOverlap(
        kind: String?,
        requested: TimeInterval?,
        beforeDuration: TimeInterval?,
        afterDuration: TimeInterval?
    ) -> TimeInterval {
        guard kind == "crossfade", let beforeDuration, let afterDuration else { return 0 }
        let roundedRequest = ((requested ?? 0.4) * 10).rounded() / 10
        return roundedMillis(
            min(max(0.1, min(roundedRequest, 1)), beforeDuration * 0.3, afterDuration * 0.3)
        )
    }

    private static func roundedMillis(_ value: TimeInterval) -> TimeInterval {
        (value * 1_000).rounded() / 1_000
    }
}

/// Alignment cues have a narrow entry band and a wider release band so a
/// finger resting near a guide does not repeatedly trigger feedback.
struct NativeTextAlignmentFeedback {
    private var active: Set<String>?
    private var previousDistances: [String: Double] = [:]

    mutating func reset() { active = nil; previousDistances = [:] }

    mutating func update(center: CGPoint, size: CGSize, rotation: Double, canvas: CGSize) -> Bool {
        guard canvas.width > 0, canvas.height > 0,
              [center.x, center.y, size.width, size.height, rotation].allSatisfy(\.isFinite) else { return false }
        let radians = rotation * .pi / 180
        let halfWidth = (abs(cos(radians)) * size.width + abs(sin(radians)) * size.height) / 2
        let halfHeight = (abs(sin(radians)) * size.width + abs(cos(radians)) * size.height) / 2
        var distances: [String: Double] = [
            "center-x": center.x - canvas.width / 2, "center-y": center.y - canvas.height / 2,
            "left": center.x - halfWidth, "right": center.x + halfWidth - canvas.width,
            "top": center.y - halfHeight, "bottom": center.y + halfHeight - canvas.height
        ]
        for angle in [0, 90, 180, 270] {
            let delta = (rotation - Double(angle)).truncatingRemainder(dividingBy: 360)
            distances["angle-\(angle)"] = delta > 180 ? delta - 360 : delta < -180 ? delta + 360 : delta
        }
        let previous = active ?? []
        let next = Set(distances.compactMap { key, distance in
            abs(distance) <= (previous.contains(key) ? 5.0 : 2.0) ? key : nil
        })
        let crossings = Set(distances.compactMap { key, value -> String? in
            guard !previous.contains(key), let old = previousDistances[key], old * value < 0,
                  !key.hasPrefix("angle-") || abs(old - value) < 180 else { return nil }
            return key
        })
        let entering = active != nil && (!next.subtracting(previous).isEmpty || !crossings.isEmpty)
        active = next.union(crossings)
        previousDistances = distances
        return entering
    }
}

/// A small angular detent, followed by a smooth return to one-to-one motion.
/// No timer or input blocking: continuing the gesture always breaks through.
enum NativeTextRotationSnap {
    static func angle(_ raw: Double) -> Double {
        guard raw.isFinite else { return raw }
        let target = (raw / 90).rounded() * 90
        let offset = raw - target
        let distance = abs(offset)
        let hold = 2.0, release = 8.0
        if distance <= hold { return target }
        if distance >= release { return raw }
        let progress = (distance - hold) / (release - hold)
        let blend = progress * progress * (3 - 2 * progress)
        return target + offset * blend
    }
}
