import Foundation

/// Bounded value history for committed recipe edits. Transactions are values, making autosave and
/// undo/redo consume the same state and avoiding UI references into renderer objects.
public struct UndoRedoHistory<Value: Equatable & Sendable>: Sendable {
    public private(set) var current: Value
    public private(set) var undoCount = 0
    public private(set) var redoCount = 0
    private var undoStack: [Value] = []
    private var redoStack: [Value] = []
    public let capacity: Int
    public init(initial: Value, capacity: Int = 50) { self.current = initial; self.capacity = max(1, capacity) }
    public mutating func commit(_ value: Value) { guard value != current else { return }; undoStack.append(current); if undoStack.count > capacity { undoStack.removeFirst() }; current = value; redoStack.removeAll(); syncCounts() }
    @discardableResult public mutating func undo() -> Value? { guard let value = undoStack.popLast() else { return nil }; redoStack.append(current); current = value; syncCounts(); return current }
    @discardableResult public mutating func redo() -> Value? { guard let value = redoStack.popLast() else { return nil }; undoStack.append(current); current = value; syncCounts(); return current }
    public mutating func removeAllHistory() { undoStack.removeAll(); redoStack.removeAll(); syncCounts() }
    private mutating func syncCounts() { undoCount = undoStack.count; redoCount = redoStack.count }
}
