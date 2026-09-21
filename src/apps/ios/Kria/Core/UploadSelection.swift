import Foundation

// KRI-125: uploads start while the user is still choosing, and the picker remembers what they
// already chose. The types here are deliberately UI-free so the rules are unit-testable
// without SwiftUI; `BackgroundUploadCoordinator` owns the state and `FootagePickerView` is a
// thin view over it (the view can't own it: `AttachmentSheet` applies `.id(role)` to the
// picker, so switching Footage↔Visuals destroys every `@State` and any in-flight `Task`).

/// What became of one Photos asset the user picked, keyed by `PhotosPickerItem.itemIdentifier`
/// (a PHAsset local identifier).
///
/// Privacy: a PHAsset identifier is durable personal data about the user's library. It lives
/// only in on-device UserDefaults and is never placed in a request body.
struct PhotoSelectionEntry: Codable, Sendable, Equatable {
    /// The `enqueue` / `PreparingUpload` / `UploadRecoveryRecord` id this asset became.
    var recordID: UUID
    /// The server's id for the attached media (`visualReservationID` for `.visual`). `nil` until
    /// attach succeeds — which is what lets an entry outlive its `UploadRecoveryRecord`, deleted
    /// on attach, so an attached clip still shows as chosen the next time the picker opens.
    var mediaID: String?
    var role: CreationMediaRole
}

struct ProjectPhotoSelection: Codable, Sendable, Equatable {
    var byIdentifier: [String: PhotoSelectionEntry] = [:]

    /// Identifiers the picker should show as already chosen: assets still on their way to the
    /// server, plus attached ones **that are still attached**. Reconciling against the live media
    /// is the point: if the user removes a clip via the trash button (or on the web) its entry
    /// must stop counting, or the picker would preselect something that isn't there and the user
    /// could never add it again.
    func preselected(role: CreationMediaRole, attachedMediaIDs: Set<String>) -> [String] {
        byIdentifier
            .filter { _, entry in
                guard entry.role == role else { return false }
                guard let mediaID = entry.mediaID else { return true }
                return attachedMediaIDs.contains(mediaID)
            }
            .map(\.key)
            .sorted()
    }
}

/// What changed between the identifiers the picker shows now and the ones already accounted for.
/// Order follows the picker (`.continuousAndOrdered`), so uploads start in the order chosen.
struct PhotoSelectionDiff: Equatable, Sendable {
    let added: [String]
    let removed: [String]

    init(current: [String], known: [String]) {
        let knownSet = Set(known), currentSet = Set(current)
        added = current.filter { !knownSet.contains($0) }
        removed = known.filter { !currentSet.contains($0) }
    }

    var isEmpty: Bool { added.isEmpty && removed.isEmpty }
}

/// Result of the user un-choosing an asset, so the picker knows whether to let it go or put it back.
enum DeselectOutcome: Equatable, Sendable {
    /// Nothing had reached the server; whatever was in progress is unwound.
    case discarded
    /// The upload was in flight and has been cancelled.
    case cancelledUpload
    /// It was already attached; it has been detached and its file deleted.
    case detached
    /// The removal was refused (rendering in progress, offline, project changed). The picker must
    /// put the asset back so the screen doesn't claim something the project doesn't reflect.
    case refused(String)
    /// The coordinator had no record of it.
    case notTracked
    /// Another `deselect` for this asset is still running and will decide the outcome. Callers must
    /// not re-diff on this: the ledger hasn't changed yet, so the same removal would be found again
    /// and re-issued in a loop until the first one finished.
    case alreadyInProgress
}

/// One clip that failed, shown in place next to the ones that worked. A single global error
/// string can't do that: with uploads overlapping, a later success would erase an earlier failure.
struct UploadFailure: Identifiable, Equatable, Sendable {
    let id: UUID
    let projectID: UUID
    let role: CreationMediaRole
    let filename: String
    let message: String
}

/// FIFO limiter for the expensive part of preparing a clip (import + hash, and the proxy
/// transcode on the phone-render path). Not `actor`-isolated: everything that touches it is already
/// on the main actor, so an actor would only add hops.
///
/// A waiter that is cancelled while queued (the user un-chose the clip) leaves the queue at
/// once and never holds a slot, and a slot handed to a waiter that was cancelled in the same
/// instant is released by the caller's `defer` like any other.
@MainActor final class UploadAdmissionGate {
    private let limit: Int
    private var active = 0
    private var waiters: [(id: UUID, continuation: CheckedContinuation<Void, any Error>)] = []

    init(limit: Int) { self.limit = max(1, limit) }

    var activeCount: Int { active }
    var waitingCount: Int { waiters.count }

    func acquire() async throws {
        try Task.checkCancellation()
        if active < limit && waiters.isEmpty {
            active += 1
            return
        }
        let id = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
                // Cancelled between the check above and here: don't queue a waiter nobody will wake.
                if Task.isCancelled {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                waiters.append((id, continuation))
            }
        } onCancel: {
            Task { @MainActor in self.cancelWaiter(id) }
        }
    }

    func release() {
        if !waiters.isEmpty {
            // Hand the slot straight to the next in line: `active` is unchanged.
            waiters.removeFirst().continuation.resume()
        } else {
            active = max(0, active - 1)
        }
    }

    private func cancelWaiter(_ id: UUID) {
        guard let index = waiters.firstIndex(where: { $0.id == id }) else { return }
        waiters.remove(at: index).continuation.resume(throwing: CancellationError())
    }
}
