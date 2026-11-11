import Foundation

/// History created while one Save is awaiting acknowledgement, independent of the display stack's cap.
struct EditorSaveHistory<Snapshot: Equatable> {
    let id = UUID()
    let submitted: Snapshot
    private let limit: Int
    private var undo: [Snapshot] = []
    private(set) var redoCheckpoints: [Snapshot] = []
    private var redoBeforeLastAppend: [Snapshot]?
    private var recordedCheckpoint = false

    init(submitted: Snapshot, limit: Int = 100) {
        self.submitted = submitted
        self.limit = max(1, limit)
    }

    mutating func appendUndo(_ value: Snapshot, isRedo: Bool = false) {
        recordedCheckpoint = true
        redoBeforeLastAppend = redoCheckpoints
        if isRedo { _ = redoCheckpoints.popLast() }
        else { redoCheckpoints.removeAll() }
        undo.append(value)
        Self.trim(&undo, limit: limit)
    }

    mutating func recordUndo(to previous: Snapshot, from current: Snapshot) {
        recordedCheckpoint = true
        redoBeforeLastAppend = nil
        if undo.last == previous { undo.removeLast() }
        else {
            // Undoing a pre-submission edit is itself a new local change relative to the saved document.
            undo.append(current)
            Self.trim(&undo, limit: limit)
        }
        redoCheckpoints.append(current)
        Self.trim(&redoCheckpoints, limit: limit)
    }

    mutating func cancelLastCheckpoint(matching value: Snapshot) {
        guard undo.last == value else { return }
        undo.removeLast()
        if let redoBeforeLastAppend { redoCheckpoints = redoBeforeLastAppend }
        redoBeforeLastAppend = nil
    }

    func undoCheckpoints(current: Snapshot) -> [Snapshot] {
        // A gesture already in progress at submission can change without calling appendUndo again.
        undo.isEmpty && !recordedCheckpoint && current != submitted ? [submitted] : undo
    }

    private static func trim(_ values: inout [Snapshot], limit: Int) {
        if values.count > limit { values.removeFirst(values.count - limit) }
    }
}
