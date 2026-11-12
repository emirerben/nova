import Foundation
#if canImport(AVFoundation)

/// Keeps an export's frames in order while several are in flight.
///
/// A frame has two ordered stages. The matte stage decides what the speaker matte answers (its hold and clip-cut state
/// follows edit time, so earlier frames commit first); the rest stage paints text, drawings and motion, whose stores also
/// follow frame order, and ends with the encoder handoff. Everything before the matte stage (decoding layers, building the
/// picture, Vision itself) runs concurrently. A frame is admitted only while fewer than `depth` frames are unfinished, so
/// the oldest unfinished frame can always run and the gates cannot deadlock.
///
/// `exclusive` frames (more than one matte per frame, such as split panels) run alone: all earlier frames finish first and
/// no later frame starts until they have.
final class RecipeFramePipeline: @unchecked Sendable {
    final class Frame: @unchecked Sendable {
        let number: Int
        fileprivate let exclusive: Bool
        fileprivate let follows: Int
        fileprivate unowned let owner: RecipeFramePipeline
        fileprivate var passedMatte = false
        fileprivate var passedRest = false
        fileprivate init(number: Int, exclusive: Bool, follows: Int, owner: RecipeFramePipeline) {
            self.number = number; self.exclusive = exclusive; self.follows = follows; self.owner = owner
        }
        /// Blocks until this frame may start.
        func admit() { owner.admit(self) }
        /// Runs `body` as this frame's matte stage; every earlier frame has committed.
        func ordered<T>(_ body: () -> T) -> T {
            owner.enterMatte(self)
            defer { owner.leaveMatte(self) }
            return body()
        }
        /// Marks the end of the matte stage for a frame that did not need it, and waits for the rest-stage turn.
        func beginRest() { owner.enterRest(self) }
        /// Idempotent: a frame that bails out early still lets the later ones through.
        func finish() { owner.enterRest(self); owner.leaveRest(self) }
    }

    let depth: Int
    private let condition = NSCondition()
    private var issued = 0
    private var nextMatte = 0
    private var nextRest = 0
    private var lastExclusive = -1

    init(depth: Int) { self.depth = max(1, depth) }

    /// Called in arrival order, which is the order the serial queue used to run frames in.
    func issue(exclusive: Bool) -> Frame {
        condition.lock(); defer { condition.unlock() }
        let frame = Frame(number: issued, exclusive: exclusive, follows: lastExclusive, owner: self)
        issued += 1
        if exclusive { lastExclusive = frame.number }
        return frame
    }

    private func admit(_ frame: Frame) {
        condition.lock(); defer { condition.unlock() }
        while !(frame.number < nextRest + depth && nextRest > frame.follows && (!frame.exclusive || nextRest == frame.number)) { condition.wait() }
    }

    private func enterMatte(_ frame: Frame) {
        condition.lock(); defer { condition.unlock() }
        precondition(!frame.passedMatte, "a frame takes its matte turn once; a second turn would wait for a number that has passed")
        while nextMatte != frame.number { condition.wait() }
    }

    private func leaveMatte(_ frame: Frame) {
        condition.lock(); defer { condition.unlock() }
        guard !frame.passedMatte else { return }
        frame.passedMatte = true
        nextMatte = frame.number + 1
        condition.broadcast()
    }

    private func enterRest(_ frame: Frame) {
        condition.lock()
        if !frame.passedMatte {
            while nextMatte != frame.number { condition.wait() }
            frame.passedMatte = true
            nextMatte = frame.number + 1
            condition.broadcast()
        }
        while nextRest != frame.number { condition.wait() }
        condition.unlock()
    }

    private func leaveRest(_ frame: Frame) {
        condition.lock(); defer { condition.unlock() }
        guard !frame.passedRest else { return }
        frame.passedRest = true
        nextRest = frame.number + 1
        condition.broadcast()
    }
}
#endif
