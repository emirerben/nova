import CoreGraphics

/// Pure logic behind slide-to-select in media grids (KRI-282 follow-up). No UI types so it is unit-testable:
/// tile frames + a finger path in, the ordered set of touched tile indices out.
enum DragSelect {
    /// What the drag does to every tile it passes over. Decided by the FIRST tile touched (Photos behaviour).
    enum Mode: Equatable {
        case select, deselect
        var targetsSelected: Bool { self == .select }
        static func forFirstTile(isSelected: Bool) -> Mode { isSelected ? .deselect : .select }
    }

    /// Movement (points) before we decide whether a touch is a scroll or a drag-select.
    static let slop: CGFloat = 8

    /// A mostly-horizontal start begins selection; a mostly-vertical one is left to the scroll view. A vertical
    /// grid scroll never moves sideways, so this is the cheapest unambiguous split (and what Photos does).
    static func isHorizontalIntent(translation: CGSize) -> Bool {
        abs(translation.width) >= abs(translation.height)
    }

    /// Index of the tile whose frame contains `point` (lowest index wins on overlap).
    static func index(at point: CGPoint, in frames: [Int: CGRect]) -> Int? {
        frames.filter { $0.value.contains(point) }.keys.min()
    }

    /// Tiles crossed by the straight segment `from` -> `to`, in order. Sampled every ~4pt so a fast flick between
    /// two touch samples cannot skip a tile.
    static func indices(from: CGPoint, to: CGPoint, in frames: [Int: CGRect]) -> [Int] {
        let dx = to.x - from.x, dy = to.y - from.y
        let steps = max(1, Int((hypot(dx, dy) / 4).rounded(.up)))
        var result: [Int] = []
        for step in 0...steps {
            let t = CGFloat(step) / CGFloat(steps)
            if let hit = index(at: CGPoint(x: from.x + dx * t, y: from.y + dy * t), in: frames), result.last != hit {
                result.append(hit)
            }
        }
        return result
    }

    /// The set of tiles a gesture currently covers. The range follows the finger: stepping back onto the
    /// previous tile un-applies the last one. A tile touched twice without backtracking stays applied once.
    struct Trail: Equatable {
        private(set) var indices: [Int] = []
        private(set) var lastPoint: CGPoint?

        mutating func start(at point: CGPoint, tile: Int) {
            indices = [tile]
            lastPoint = point
        }

        mutating func advance(to point: CGPoint, in frames: [Int: CGRect]) {
            guard let from = lastPoint else { return }
            lastPoint = point
            for hit in DragSelect.indices(from: from, to: point, in: frames) { visit(hit) }
        }

        private mutating func visit(_ tile: Int) {
            if indices.last == tile { return }
            if indices.count >= 2, indices[indices.count - 2] == tile {
                indices.removeLast()
            } else if !indices.contains(tile) {
                indices.append(tile)
            }
        }
    }

    /// Whole-path convenience for tests: trail for a path of finger samples.
    static func touched(frames: [Int: CGRect], path: [CGPoint]) -> [Int] {
        guard let first = path.first, let tile = index(at: first, in: frames) else { return [] }
        var trail = Trail()
        trail.start(at: first, tile: tile)
        for point in path.dropFirst() { trail.advance(to: point, in: frames) }
        return trail.indices
    }

    /// Points per auto-scroll tick (positive = down) when the finger nears the top or bottom of the viewport;
    /// speed ramps up the closer to (or past) the edge. 0 in the middle.
    static func autoScrollStep(fingerY: CGFloat, viewportHeight: CGFloat, edge: CGFloat = 72, maxStep: CGFloat = 14) -> CGFloat {
        guard viewportHeight > edge * 2 else { return 0 }
        if fingerY < edge { return -maxStep * min(1, (edge - fingerY) / edge) }
        if fingerY > viewportHeight - edge { return maxStep * min(1, (fingerY - (viewportHeight - edge)) / edge) }
        return 0
    }
}
