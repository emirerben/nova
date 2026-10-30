import Foundation

// KRI-944: one state per camera-roll item, rendered by every tile and driven by one pure reducer. The first three
// states exist only on the phone (before the server knows the item); the rest mirror the server's derived
// `CollectionItemState`. Views read this enum and never combine `savedAsset != nil` with index buckets.

/// Why an item is waiting, failed or left out. Server reasons plus `uploadFailed`, which only the phone can raise.
enum CameraRollItemReason: String, Sendable, CaseIterable {
    case providerError = "provider_error"
    case budgetPaused = "budget_paused"
    case unsupportedType = "unsupported_type"
    case tooLarge = "too_large"
    case tooLong = "too_long"
    case icloudUnavailable = "icloud_unavailable"
    case noUsableMoment = "no_usable_moment"
    case accessRevoked = "access_revoked"
    case uploadFailed = "upload_failed"

    /// A reason the server sent that this build does not know is still a provider-side problem the creator can retry.
    init(serverValue: String?) {
        self = serverValue.flatMap(Self.init(rawValue:)) ?? .providerError
    }

    /// The one table of plain-language copy; tiles and summaries both read it.
    var message: String {
        switch self {
        case .providerError: "Analysis hit a snag on our side. Try again."
        case .budgetPaused: "Analysis is paused for now. Try again a little later."
        case .unsupportedType: "Kria can't read this file type. Photos need to be JPEG, PNG, WebP or HEIC."
        case .tooLarge: "This file is too large to analyze. Photos can be up to 25 MB."
        case .tooLong: "This video is longer than 30 minutes, which is too long to analyze."
        case .icloudUnavailable: "Couldn't download this from iCloud in time. Try again on Wi-Fi."
        case .noUsableMoment: "Kria couldn't find a usable moment in this one."
        case .accessRevoked: "Kria no longer has access to this item in Photos."
        case .uploadFailed: "The upload didn't finish. Check your connection and try again."
        }
    }
}

enum CameraRollItemPhase: String, Sendable, CaseIterable {
    case picking, exporting, uploading, queued, analyzing, ready, needsRetry, failed, excluded
}

enum CameraRollItemState: Equatable, Sendable {
    // Phone only.
    case picking
    case exporting
    /// 0...1 while bytes are in flight. Phone only: the reducer ignores a server `uploading` snapshot.
    case uploading(progress: Double)
    // Server derived.
    case queued
    case analyzing
    case ready
    /// Retryable: the next attempt may succeed.
    case needsRetry(CameraRollItemReason)
    /// Not retryable: the same input will fail again.
    case failed(CameraRollItemReason)
    /// Never analyzable, so it is left out without being an error.
    case excluded(CameraRollItemReason)

    var phase: CameraRollItemPhase {
        switch self {
        case .picking: .picking
        case .exporting: .exporting
        case .uploading: .uploading
        case .queued: .queued
        case .analyzing: .analyzing
        case .ready: .ready
        case .needsRetry: .needsRetry
        case .failed: .failed
        case .excluded: .excluded
        }
    }

    var reason: CameraRollItemReason? {
        switch self {
        case let .needsRetry(reason), let .failed(reason), let .excluded(reason): reason
        default: nil
        }
    }

    /// True while the only thing left is the server's work, which is what the 3 s poll waits on.
    var awaitsServer: Bool { self == .queued || self == .analyzing }
    /// Nothing will change without the creator acting.
    var isSettled: Bool {
        switch self {
        case .ready, .needsRetry, .failed, .excluded: true
        default: false
        }
    }
    var isRetryable: Bool { if case .needsRetry = self { true } else { false } }
    /// The phone is still moving this item toward the server.
    var isLocalOnly: Bool {
        switch self {
        case .picking, .exporting, .uploading: true
        default: false
        }
    }
    /// What the tile shows under the thumbnail when there is something to say.
    var message: String? { reason?.message }
}

/// Everything that can happen to one tile. Illegal combinations are ignored by the reducer, so a late progress tick
/// or a stale poll can never move an item backwards.
enum CameraRollItemEvent: Equatable, Sendable {
    case exportStarted
    case uploadProgress(Double)
    /// The bytes are finalized on the server; analysis is queued.
    case uploaded
    /// Preflight refused the file: it can never be analyzed.
    case rejected(CameraRollItemReason)
    /// A local step failed and trying again may work.
    case failedLocally(CameraRollItemReason)
    /// A local step failed and trying again cannot work.
    case unreachable(CameraRollItemReason)
    /// The creator paused or the task was cancelled before this item finished.
    case interrupted
    /// The creator asked the server to retry; optimistic until the next snapshot.
    case retryRequested
    case server(CameraRollItemState)
}

enum CameraRollItemReducer {
    static func reduce(_ state: CameraRollItemState, _ event: CameraRollItemEvent) -> CameraRollItemState {
        switch event {
        case .exportStarted:
            switch state {
            case .picking, .needsRetry: return .exporting
            default: return state
            }
        case let .uploadProgress(value):
            let clamped = min(1, max(0, value.isFinite ? value : 0))
            switch state {
            case .exporting: return .uploading(progress: clamped)
            case let .uploading(progress): return .uploading(progress: max(progress, clamped))
            default: return state
            }
        case .uploaded:
            return state.isLocalOnly ? .queued : state
        case let .rejected(reason):
            return state.isLocalOnly ? .excluded(reason) : state
        case let .failedLocally(reason):
            return state.isLocalOnly ? .needsRetry(reason) : state
        case let .unreachable(reason):
            return state.isLocalOnly ? .failed(reason) : state
        case .interrupted:
            return state.isLocalOnly ? .picking : state
        case .retryRequested:
            return state.isRetryable ? .analyzing : state
        case let .server(incoming):
            // `uploading` means the server is still finalizing bytes the phone already sent. Taking it would turn a
            // waiting item into a local-only one and stop the poll, so it never changes the tile.
            if case .uploading = incoming { return state }
            // A snapshot requested before a newer one landed must not walk a tile back to an earlier server phase.
            switch (state, incoming) {
            case (.ready, .queued), (.ready, .analyzing), (.analyzing, .queued): return state
            default: return incoming
            }
        }
    }
}

// MARK: - Idea actions

/// One reducer for every idea and event menu, so the surface never keeps a boolean per menu.
/// "Find ideas" on an empty event is `.anotherIdea`: the server decides how many ideas come back.
enum CameraRollIdeaAction: Sendable {
    case deleteIdea(storyID: UUID, group: String)
    case undoDeleteIdea(storyID: UUID)
    case anotherIdea(group: String)
    /// `members` are the group's collection members, kept so Undo can put them back.
    case deleteEvent(group: String, members: [CameraRollCollectionMember])
    case undoDeleteEvent(group: String)
    // Results fed back by whoever runs the effects.
    case ideasArrived(group: String, stories: [CameraRollStory])
    case generationFailed(group: String)
    /// A restore the server refused (for example past `restore_until`): the ideas disappear again.
    case restoreFailed(storyIDs: [UUID])
    case undoExpired
}

/// Server work the reducer asks for. The reducer stays pure; the caller performs these with `KriaAPIClient`.
enum CameraRollIdeaEffect: Equatable, Sendable {
    case deleteStory(UUID)
    case restoreStory(UUID)
    case generate(group: String, excluding: [UUID])
    case deleteGroup(String)
    case restoreGroup(group: String, members: [CameraRollCollectionMember], storyIDs: [UUID])
}

struct CameraRollIdeaBoard: Sendable {
    var storiesByGroup: [String: [CameraRollStory]] = [:]
    /// Events with an idea request in flight; also the guard against double taps.
    var generating: Set<String> = []
    /// The single toast-sized undo slot. A new deletion makes the previous one final.
    var undo: Undo?

    enum Undo: Sendable {
        case idea(group: String, story: CameraRollStory, index: Int)
        case event(group: String, stories: [CameraRollStory], members: [CameraRollCollectionMember])
    }

    @discardableResult
    mutating func reduce(_ action: CameraRollIdeaAction) -> [CameraRollIdeaEffect] {
        switch action {
        case let .deleteIdea(storyID, group):
            guard let index = storiesByGroup[group]?.firstIndex(where: { $0.id == storyID }),
                  let story = storiesByGroup[group]?.remove(at: index) else { return [] }
            undo = .idea(group: group, story: story, index: index)
            return [.deleteStory(storyID)]

        case let .undoDeleteIdea(storyID):
            guard case let .idea(group, story, index)? = undo, story.id == storyID else { return [] }
            var stories = storiesByGroup[group] ?? []
            stories.insert(story, at: min(index, stories.count))
            storiesByGroup[group] = stories
            undo = nil
            return [.restoreStory(storyID)]

        case let .anotherIdea(group):
            guard generating.insert(group).inserted else { return [] }
            // The server accepts at most 20 exclusions; the newest ideas are the ones a new batch must not repeat.
            return [.generate(group: group, excluding: (storiesByGroup[group] ?? []).suffix(20).map(\.id))]

        case let .deleteEvent(group, members):
            let stories = storiesByGroup.removeValue(forKey: group) ?? []
            generating.remove(group)
            undo = .event(group: group, stories: stories, members: members)
            return [.deleteGroup(group)]

        case let .undoDeleteEvent(group):
            guard case let .event(deleted, stories, members)? = undo, deleted == group else { return [] }
            if !stories.isEmpty { storiesByGroup[group] = stories }
            undo = nil
            return [.restoreGroup(group: group, members: members, storyIDs: stories.map(\.id))]

        case let .ideasArrived(group, stories):
            // A result for an event that was deleted meanwhile (or was never requested) is dropped.
            guard generating.remove(group) != nil else { return [] }
            var current = storiesByGroup[group] ?? []
            let known = Set(current.map(\.id))
            current.append(contentsOf: stories.filter { !known.contains($0.id) })
            storiesByGroup[group] = current
            return []

        case let .generationFailed(group):
            generating.remove(group)
            return []

        case let .restoreFailed(storyIDs):
            let gone = Set(storyIDs)
            for group in storiesByGroup.keys { storiesByGroup[group]?.removeAll { gone.contains($0.id) } }
            return []

        case .undoExpired:
            undo = nil
            return []
        }
    }
}
