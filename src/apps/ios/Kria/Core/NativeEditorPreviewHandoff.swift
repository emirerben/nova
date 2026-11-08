import Foundation

/// Presentation/work identity mirrors Cunda's operation ownership; it never grants mutation rights.
struct NativeEditorPreviewOwner: Equatable, Sendable {
    var projectID: UUID
    var lifetime: UInt64
    var itemID: String?
    var jobID: UUID?
    var variant: String?
    var threadID: UUID?
}

struct NativeEditorPreviewWork: Equatable, Sendable {
    let owner: NativeEditorPreviewOwner
    let generation: String
    let sequence: Int
    let documentRevision: Int?

    init(owner: NativeEditorPreviewOwner, generation: String, sequence: Int, documentRevision: Int? = nil) {
        self.owner = owner; self.generation = generation; self.sequence = sequence
        self.documentRevision = documentRevision
    }
}

struct NativeEditorPreviewWorkTracker: Sendable {
    private(set) var active: NativeEditorPreviewWork?
    private(set) var awaitingFrame = false
    mutating func begin(_ ticket: NativeEditorPreviewWork) { active = ticket; awaitingFrame = false }
    mutating func waitForFrame(_ ticket: NativeEditorPreviewWork) {
        guard active == ticket else { return }
        awaitingFrame = true
    }
    func isPending(_ current: NativeEditorPreviewWork) -> Bool { active == current }
    @discardableResult mutating func end(_ ticket: NativeEditorPreviewWork) -> Bool {
        guard active == ticket else { return false }
        active = nil
        awaitingFrame = false
        return true
    }
    mutating func reset() { active = nil; awaitingFrame = false }
}

enum NativeEditorPreviewAdmission {
    static func canRetain(shown: NativeEditorPreviewWork, current: NativeEditorPreviewOwner,
                          readyFrameShown: Bool) -> Bool {
        readyFrameShown && shown.owner == current
    }
}

enum NativeEditorPreviousPreviewStatus: Equatable, Sendable {
    case updating, failed, cancelled
    var message: String {
        switch self {
        case .updating: "Updating preview — showing the previous version."
        case .failed: "Preview update failed — showing the previous version."
        case .cancelled: "Preview update stopped — showing the previous version."
        }
    }
}

enum NativeEditorPreviewHandoffError: LocalizedError {
    case candidateNotReady
    var errorDescription: String? { "The new preview could not get ready. Your previous preview is still shown." }
}
