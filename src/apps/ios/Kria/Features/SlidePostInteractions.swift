import CoreGraphics
import Foundation

// Pure decision logic for the slide editor's two direct-manipulation gestures. The views only feed
// finger positions in and apply the answers, so the index math and the tap routing are unit-tested.

/// What a tap on the slide preview does.
enum SlidePostTextTap {
    struct Outcome: Equatable {
        /// The text to select (nil clears the selection).
        var selectID: String?
        /// Open the Text panel from browse/look.
        var opensPanel: Bool
        /// Show the panel's Edit tab.
        var showsEditTab: Bool
        /// Put the keyboard on the text field.
        var focusesField: Bool
    }

    /// - Parameters:
    ///   - hit: the text under the finger (nil for empty canvas).
    ///   - panelOpen: the Text panel is already showing.
    ///   - selectedID: the text currently selected.
    ///   - onEditTab: the panel is on Edit text.
    ///   - keyboardUp: the software keyboard is already up.
    static func resolve(hit: String?, panelOpen: Bool, selectedID: String?, onEditTab: Bool, keyboardUp: Bool) -> Outcome? {
        guard let hit else {
            // Empty canvas deselects while editing; in browse mode there is nothing to deselect.
            return panelOpen ? Outcome(selectID: nil, opensPanel: false, showsEditTab: false, focusesField: false) : nil
        }
        // Already editing this very text with the keyboard up: leave the field alone (no flicker).
        let alreadyTyping = panelOpen && selectedID == hit && onEditTab && keyboardUp
        return Outcome(selectID: hit, opensPanel: !panelOpen, showsEditTab: true, focusesField: !alreadyTyping)
    }
}

/// Index math for dragging a block along the slide strip.
enum SlidePostReorderMath {
    /// The slot a block lands in. `delta` is how far the finger has moved along the CONTENT (finger
    /// movement plus any auto-scroll), `pitch` the block width plus spacing.
    static func targetIndex(from: Int, delta: CGFloat, pitch: CGFloat, count: Int) -> Int {
        guard count > 0, pitch > 0 else { return 0 }
        let shift = (delta / pitch).rounded()
        // Guard the Int conversion for absurd drags before clamping.
        let bounded = min(max(shift, -CGFloat(count)), CGFloat(count))
        return min(max(from + Int(bounded), 0), count - 1)
    }

    /// Points per second to scroll while the finger is within `edge` of either side of the viewport
    /// (negative = toward the start). Zero in the middle, ramping to `maxSpeed` at the very edge, and
    /// zero when the content fits (nothing to scroll).
    static func autoScrollVelocity(fingerX: CGFloat, viewport: CGFloat, content: CGFloat, edge: CGFloat = 56, maxSpeed: CGFloat = 520) -> CGFloat {
        guard viewport > 0, content > viewport + 0.5, edge > 0 else { return 0 }
        let zone = min(edge, viewport / 2)
        if fingerX < zone { return -maxSpeed * min(1, (zone - max(fingerX, 0)) / zone) }
        if fingerX > viewport - zone { return maxSpeed * min(1, (min(fingerX, viewport) - (viewport - zone)) / zone) }
        return 0
    }

    static func clampedOffset(_ offset: CGFloat, content: CGFloat, viewport: CGFloat) -> CGFloat {
        min(max(offset, 0), max(0, content - viewport))
    }

    /// How far the lifted block sits from its resting place, in content coordinates.
    static func liftedOffset(fingerTravel: CGFloat, scrollTravel: CGFloat) -> CGFloat { fingerTravel + scrollTravel }

    /// Where a neighbour slides to while a block is lifted: one slot toward the gap the lifted block left.
    static func neighbourShift(index: Int, from: Int, target: Int, pitch: CGFloat) -> CGFloat {
        if from < target, index > from, index <= target { return -pitch }
        if from > target, index >= target, index < from { return pitch }
        return 0
    }
}
