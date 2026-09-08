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
    static let minimumHitTarget: CGFloat = 44

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

    /// Expands a visual region without moving its center until both dimensions
    /// meet the 44-point accessibility target.
    static func hitRect(_ rect: CGRect, minimum: CGFloat = minimumHitTarget) -> CGRect {
        guard minimum.isFinite, minimum > 0 else { return rect }
        let dx = max(0, (minimum - rect.width) / 2)
        let dy = max(0, (minimum - rect.height) / 2)
        return rect.insetBy(dx: -dx, dy: -dy)
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

    static func timelineProjection(
        slots: [EditorTimelineSlot],
        carousel: [String: JSONValue]?
    ) -> NativeEditorTimelineProjection {
        var baseWindows: [NativeEditorClipWindow] = []
        var cursor: TimeInterval = 0
        var previous: (slot: EditorTimelineSlot, duration: TimeInterval)?
        for (index, slot) in slots.enumerated() where !slot.removed {
            let duration = max(0.1, slot.durationS ?? 0.1)
            let overlap = previous.map {
                transitionOverlap(left: $0.slot, leftDuration: $0.duration, rightDuration: duration)
            } ?? 0
            let start = roundedMillis(cursor - overlap)
            let end = roundedMillis(start + duration)
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
        let carouselStart = roundedMillis(before.map { $0.end - incoming } ?? 0)
        let carouselEnd = roundedMillis(carouselStart + carouselDuration)
        let shift = max(
            0,
            roundedMillis(
                after.map { carouselStart + carouselDuration - outgoing - $0.start }
                    ?? (carouselDuration - incoming)
            )
        )
        let projectedWindows = baseWindows.enumerated().map { index, window in
            guard index >= insertionIndex else { return window }
            return NativeEditorClipWindow(
                sourceIndex: window.sourceIndex,
                start: roundedMillis(window.start + shift),
                end: roundedMillis(window.end + shift),
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
