import Foundation

/// The attached clip a re-recorded take replaces.
struct RerecordTarget: Equatable, Sendable, Identifiable {
    var id: String { mediaID }
    let mediaID: String
    let displayName: String
}

/// The server's receipt that one attached take was swapped for another (`state.source_replacement`).
struct SourceReplacement: Equatable, Sendable {
    let replacedMediaID: String
    let mediaID: String
    /// The job the old take made. Nil when the swap happened before any video existed.
    let supersededJobID: String?

    static func parse(_ state: [String: JSONValue]?) -> SourceReplacement? {
        guard let fields = state?["source_replacement"]?.objectValue,
              let replaced = fields["replaced_media_id"]?.stringValue, let media = fields["media_id"]?.stringValue else { return nil }
        return SourceReplacement(replacedMediaID: replaced, mediaID: media, supersededJobID: fields["superseded_job_id"]?.stringValue)
    }
}

/// Whether, and for which clip, the thread offers a re-record. The server leaves `media_capabilities.clips.replace`
/// out when its flag is off or the item does not speak from an embedded clip, so an old or flag-off server never
/// shows the action and never receives a request it would answer 404/422.
enum RerecordOffer: Equatable {
    case none
    case one(RerecordTarget)
    /// More than one clip is attached; the server accepts only the one the video speaks from, so the creator names it.
    case choose([RerecordTarget])

    static func resolve(thread: CreationThread?) -> RerecordOffer {
        guard let thread, thread.mediaCapabilities?["clips"]?.objectValue?["replace"] == .bool(true) else { return .none }
        let clips = CreationAttachedMedia.parse(thread.state ?? [:]).filter { $0.kind == "video" && !$0.isSong }
            .map { RerecordTarget(mediaID: $0.id, displayName: $0.displayName) }
        switch clips.count {
        case 0: return .none
        case 1: return .one(clips[0])
        default: return .choose(clips)
        }
    }
}

/// The follow-up planning turn the client still owes after a swap. Read from the thread, never from a flag the app
/// keeps, so a swap that finished while the app was gone still shows its pending turn when the chat reopens.
enum RerecordPlanDebt {
    static let message = "Make it again with my new take."

    /// Owed while the swap's marker still names the job on screen (the old video is still projected) and no message of
    /// the creator's came after the swap event. A new job moves `activeJobID`, which settles the marker by itself.
    static func owed(state: [String: JSONValue]?, activeJobID: String?, events: [ThreadEvent]) -> SourceReplacement? {
        guard let replacement = SourceReplacement.parse(state),
              let superseded = replacement.supersededJobID, superseded.lowercased() == activeJobID?.lowercased(),
              let swapped = events.last(where: { $0.eventType == "media_replaced" })
        else { return nil }
        let followedUp = events.contains { $0.sequence > swapped.sequence && $0.role == "user" && $0.eventType == "user_message" }
        return followedUp ? nil : replacement
    }
}

/// Why a swap did not happen. Every case leaves the old take attached and playing.
struct RerecordFailure: Equatable, Sendable {
    let code: String?
    let message: String
    /// Sending the same bytes again can succeed. False when the take itself is the problem and needs recording again.
    let retryable: Bool

    static let unchanged = "Your current take is unchanged."

    init(code: String?, message: String, retryable: Bool) {
        self.code = code; self.message = message; self.retryable = retryable
    }

    init(_ error: Error) {
        let code: String?
        switch error {
        case let APIError.conflict(detail): code = detail.code
        case let APIError.requestFailed(_, detail): code = detail.code
        default: code = nil
        }
        self = Self.known(code: code, transient: Self.isTransient(error))
    }

    private static func isTransient(_ error: Error) -> Bool {
        switch error {
        case APIError.offline: true
        case let APIError.requestFailed(status, _): status == 429 || (500...599).contains(status)
        case let error as URLError: error.code != .cancelled
        default: false
        }
    }

    static func known(code: String?, transient: Bool) -> Self {
        func make(_ text: String, retryable: Bool) -> Self { Self(code: code, message: "\(text) \(unchanged)", retryable: retryable) }
        switch code {
        case "media_replace_unavailable": return make("Re-recording isn’t available right now.", retryable: false)
        case "render_in_progress": return make("Your video is still being made. Try again when it’s done.", retryable: true)
        case "retake_target_missing", "revision_mismatch": return make("This chat changed. Close this and try again.", retryable: true)
        case "retake_target_not_speaker": return make("That isn’t the clip you speak in. Re-record the clip you speak in.", retryable: false)
        case "retake_speaker_unknown": return make("Make the video first, then re-record the clip you speak in.", retryable: false)
        case "retake_same_source", "source_already_attached": return make("That recording is already in your edit.", retryable: false)
        case "retake_source_unresolved": return make("Kria couldn’t use that take as your speaker. Record it again.", retryable: false)
        case "retake_has_no_audio": return make("That take has no sound. Record it again.", retryable: false)
        case "retake_unsupported_format", "retake_target_not_clip", "retake_kind_mismatch", "retake_library_unsupported", "device_render_unsupported":
            return make("This edit can’t swap its take.", retryable: false)
        case "upload_not_finished", "registration_changed", "identity_unavailable", "upload_verification_error", "media_verification_error":
            return make("Kria couldn’t finish checking your take.", retryable: true)
        case "thread_archived", "thread_deleted", "thread_not_found", "thread_id_invalid", "draft_missing":
            return make("This chat can’t take a new take.", retryable: false)
        default: return make("Kria couldn’t swap in your new take.", retryable: transient)
        }
    }
}

/// What the re-record flow is doing, as one value. The coordinator's records and failures and the thread already know
/// most of it (and survive an app kill), so only the steps before a take is handed over, and the turn just sent, are
/// kept locally.
enum RerecordPhase: Equatable {
    /// Nothing to offer: the server does not advertise replace, or nothing is attached to replace.
    case unavailable
    case ready(RerecordOffer)
    case confirming([RerecordTarget])
    /// A take is uploading or being swapped in. The old take is still the attached one.
    case replacing
    case failed(RerecordFailure)
    /// The swap is done and the new plan has not been asked for.
    case planOwed(SourceReplacement)
    case planning(SourceReplacement)

    /// The creator's own steps, kept in the view. Facts from the coordinator and the thread outrank them.
    enum Local: Equatable {
        case none
        /// Shown the trade-off; the creator has not committed yet.
        case confirming([RerecordTarget])
        /// The creator committed: when the swap lands, this session sends the new plan. Set before the camera opens
        /// and kept after it closes, so the swap is noticed whether or not the camera is still up.
        case swapping
        case planning(SourceReplacement)
    }

    struct Facts: Equatable {
        var offer: RerecordOffer
        var replaceInFlight: Bool
        var failure: RerecordFailure?
        var debt: SourceReplacement?

        /// The failed re-record's upload id, for Retry and Dismiss.
        func failureRecordID(in failures: [UploadFailure], project: UUID) -> UUID? {
            failures.first { $0.projectID == project && $0.replacesMediaID != nil }?.id
        }
    }

    static func resolve(local: Local, facts: Facts) -> Self {
        if facts.replaceInFlight { return .replacing }
        if let failure = facts.failure { return .failed(failure) }
        if let debt = facts.debt {
            if case let .planning(sent) = local, sent == debt { return .planning(debt) }
            return .planOwed(debt)
        }
        switch local {
        case let .confirming(targets): return .confirming(targets)
        case .none, .swapping, .planning: return facts.offer == .none ? .unavailable : .ready(facts.offer)
        }
    }
}
