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
    ///   - directSelected: a text is selected for direct manipulation (hold-select) with no panel open.
    static func resolve(hit: String?, panelOpen: Bool, selectedID: String?, onEditTab: Bool, keyboardUp: Bool,
                        directSelected: Bool = false) -> Outcome? {
        guard let hit else {
            // Empty canvas deselects while editing or direct-manipulating; in plain browse there is nothing to deselect.
            return (panelOpen || directSelected) ? Outcome(selectID: nil, opensPanel: false, showsEditTab: false, focusesField: false) : nil
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

    /// The slide whose block is under a content-space x (nil over a gap or past the last slide).
    static func slideIndex(atContentX x: CGFloat, count: Int, leading: CGFloat, pitch: CGFloat, tileWidth: CGFloat) -> Int? {
        let relative = x - leading
        guard relative >= 0, pitch > 0, count > 0 else { return nil }
        let index = Int((relative / pitch).rounded(.down))
        guard index < count, relative - CGFloat(index) * pitch <= tileWidth else { return nil }
        return index
    }

    /// The scroll offset that brings a block fully on screen, or nil when it already is. A block cut by
    /// (or beyond) an edge is centred so its neighbours peek in on both sides.
    static func offsetToReveal(index: Int, current: CGFloat, viewport: CGFloat, content: CGFloat, leading: CGFloat, pitch: CGFloat, tileWidth: CGFloat, margin: CGFloat = 8) -> CGFloat? {
        guard viewport > 0, content > viewport + 0.5 else { return nil }
        let minX = leading + CGFloat(index) * pitch, maxX = minX + tileWidth
        if minX >= current + margin, maxX <= current + viewport - margin { return nil }
        let centred = minX + tileWidth / 2 - viewport / 2
        return clampedOffset(centred, content: content, viewport: viewport)
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


/// Pure state machine for one finger on a slide-preview text: tap vs press-and-hold vs drag vs
/// hold-then-drag. The view feeds touch samples and timer fires in and applies the returned action.
///
/// - tap: lifted within `slop` before `holdDuration` -> open Edit text.
/// - drag: moved past `slop` first (on a text) -> move immediately, no hold needed (matches the native preview).
/// - hold: still within `slop` after `holdDuration` (only when `holdAllowed`) -> select for direct
///   manipulation (no panel, no keyboard); moving past `slop` afterwards continues as a drag in the same gesture;
///   lifting after a hold is NOT a tap.
struct SlidePostTouchResolver {
    /// A real finger lingers and drifts: a press released within `slop` and before `holdDuration` is a tap.
    /// 0.5s is the iOS long-press default; 10pt covers normal finger roll (synthetic XCUITest taps never drift).
    static let holdDuration: TimeInterval = 0.5
    static let slop: CGFloat = 10

    enum Action: Equatable {
        case none
        case beginHold
        /// `fromHold` is true when the drag continues a completed hold.
        case beginDrag(fromHold: Bool)
        case tap
        case endHold
        case endDrag
    }

    private enum Phase { case idle, pressing, held, dragging, ignored }
    private var phase = Phase.idle
    private var start = CGPoint.zero
    private var startTime = TimeInterval(0)
    private var onTarget = false
    private var holdAllowed = false

    var isIdle: Bool { phase == .idle }
    var isHeld: Bool { phase == .held }

    mutating func touchDown(at point: CGPoint, time: TimeInterval, onTarget: Bool, holdAllowed: Bool) {
        phase = .pressing
        start = point; startTime = time
        self.onTarget = onTarget; self.holdAllowed = holdAllowed
    }

    mutating func moved(to point: CGPoint) -> Action {
        let travelled = hypot(point.x - start.x, point.y - start.y)
        switch phase {
        case .pressing where travelled > Self.slop && !onTarget:
            phase = .ignored   // a swipe over empty canvas is neither a tap nor a drag
            return .none
        case .pressing where travelled > Self.slop:
            phase = .dragging
            return .beginDrag(fromHold: false)
        case .held where travelled > Self.slop:
            phase = .dragging
            return .beginDrag(fromHold: true)
        default:
            return .none
        }
    }

    mutating func holdTimerFired(at time: TimeInterval) -> Action {
        guard phase == .pressing, holdAllowed, onTarget, time - startTime >= Self.holdDuration - 0.001 else { return .none }
        phase = .held
        return .beginHold
    }

    mutating func touchUp(at point: CGPoint) -> Action {
        defer { phase = .idle }
        switch phase {
        case .pressing: return hypot(point.x - start.x, point.y - start.y) <= Self.slop ? .tap : .none
        case .held: return .endHold
        case .dragging: return .endDrag
        case .idle, .ignored: return .none
        }
    }

    /// A second finger / pinch took over: end without a tap.
    mutating func cancel() { phase = .ignored }
    mutating func reset() { phase = .idle }
}


/// What the rich (redesigned) slide editor shows in its preview. The canvas reproduces the server layout, so
/// the editor always shows the SOURCE media with the LIVE texts on top, in every state (draft, saved, rendered).
/// The server render is only for export/share: showing it would burn the text into pixels (nothing to tap)
/// and, with live texts too, double it.
enum SlidePostPreviewPolicy {
    /// The media behind the canvas. The rendered URL is used only by the legacy (non-rich) layout.
    static func mediaURL(rich: Bool, canExport: Bool, renderedURL: URL?, asset: SlidePostAsset?) -> URL? {
        if !rich, canExport, let renderedURL { return renderedURL }
        return asset?.sourceURL ?? asset?.displayURL ?? asset?.previewURL
    }

    /// The editable texts drawn by the canvas (`texts`, or the legacy single `text` lifted into one element).
    static func editableTexts(rich: Bool, edits: SlidePostEdits?) -> [SlidePostTextElement] {
        guard rich else { return [] }
        return edits?.effectiveTexts ?? []
    }
}
