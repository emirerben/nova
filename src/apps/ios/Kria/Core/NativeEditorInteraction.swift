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
}
