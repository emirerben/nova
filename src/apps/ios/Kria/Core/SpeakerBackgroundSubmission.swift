import Foundation

/// KRI-1119: tells the server which visual is the Talk-to-camera background. The upload itself is the normal Visuals
/// path; this only names it, with `PUT creation-threads/{id}/speaker-background` (docs/pipelines/speaker-background.md).
enum SpeakerBackgroundIntent: Equatable, Sendable {
    /// `clientUploadID` is `"ios-<recordID>"` of the reserved visual upload. The row may still be preparing.
    case set(clientUploadID: String, eventID: String)
    case clear(eventID: String)

    var clientUploadID: String? { if case .set(let id, _) = self { id } else { nil } }
    var eventID: String { switch self { case .set(_, let id), .clear(let id): id } }
}

/// How one statement ended, in the shape the camera flow needs.
enum SpeakerBackgroundOutcome: Equatable, Sendable { case saved, unavailable, failed }

/// The two calls the statement needs; `KriaAPIClient` inherits it, and tests use a small fake.
protocol SpeakerBackgroundClient: Sendable {
    func project(threadID: UUID) async throws -> CreationThread
    /// A nil `clientUploadID` clears the background (the body carries no reference at all).
    func setSpeakerBackground(threadID: UUID, clientUploadID: String?, expectedRevision: Int, clientEventID: String) async throws -> CreationThread
}

enum SpeakerBackgroundSubmission {
    case saved(CreationThread)
    /// 404 `speaker_background_unavailable`: the server turned it off. Dropped quietly, the camera already gates on the capability.
    case unavailable
    case failed

    /// Same policy as the song range: read the thread for its revision, send, and on a revision or idempotency conflict
    /// (the thread moved on, for instance while the upload finished) read again and retry ONCE with the same event id, so a
    /// retry of a request that did land is answered by the server instead of applied twice.
    static func submit(client: any SpeakerBackgroundClient, threadID: UUID, intent: SpeakerBackgroundIntent) async -> SpeakerBackgroundSubmission {
        var retried = false
        while true {
            do {
                let latest = try await client.project(threadID: threadID)
                let thread = try await client.setSpeakerBackground(threadID: threadID, clientUploadID: intent.clientUploadID,
                                                                   expectedRevision: latest.revision, clientEventID: intent.eventID)
                return .saved(thread)
            } catch {
                switch cause(of: error) {
                case .unavailable: return .unavailable
                case .failed: return .failed
                case .revisionChanged:
                    if retried { return .failed }
                    retried = true
                }
            }
        }
    }

    private enum Cause { case unavailable, revisionChanged, failed }

    private static func cause(of error: Error) -> Cause {
        guard let api = error as? APIError else { return .failed }
        switch api {
        case .requestFailed(let status, let detail):
            if status == 404, detail.code == "speaker_background_unavailable" { return .unavailable }
            return .failed
        case .conflict(let detail):
            // The thread has no plan item yet: a retry cannot help, the next opening will try again.
            if detail.code == "speaker_background_needs_item" { return .failed }
            return .revisionChanged
        default:
            return .failed
        }
    }
}
