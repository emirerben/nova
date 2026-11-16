import Foundation
import Observation

/// UI fixtures have their own local identity; HTTP ownership always comes from the token.
struct ReviewAccountScope {
    let uiOwner: String
    let transportOwner: String

    init?(uiOwner: String?, transportOwner: String?) {
        guard let uiOwner, !uiOwner.isEmpty, let transportOwner, !transportOwner.isEmpty else { return nil }
        self.uiOwner = uiOwner
        self.transportOwner = transportOwner
    }

    func matches(uiOwner: String?, transportOwner: String?) -> Bool {
        self.uiOwner == uiOwner && self.transportOwner == transportOwner
    }
}

/// The state machine behind `ReviewPlanView`: loads the plan snapshot, holds the creator's flags and edits, sends
/// the scoped turn, and follows its authoritative plan until the decisions are ready. The workspace owns the thread revision, the
/// delta feed and the approval and reaches them through `ReviewPlanActions`.
@MainActor @Observable
final class ReviewPlanModel {
    enum Phase: Equatable {
        /// Nothing in flight: the creator is reading, flagging or editing.
        case reviewing
        /// A turn is being sent / approved.
        case submitting
        /// The update plan is settling; `scope` are the sections being redone.
        case updating
    }

    private(set) var blocks: [PlanBlock]
    private(set) var snapshot: PlanSnapshot?
    private(set) var phase: Phase = .reviewing
    private(set) var isLoading: Bool
    private(set) var loadFailed = false
    /// A quiet one-liner for the last thing that went wrong.
    private(set) var notice: String?
    /// The sections being redone (shown as deciding cards) and the prompt the creator asked with.
    private(set) var updatingScope: [PlanSectionID] = []
    private(set) var askedPrompt: String?
    var draft = ReviewPlanDraft()

    private let actions: ReviewPlanActions
    private var followTask: Task<Void, Never>?
    private var followID: UUID?
    private var followExpectation: Bool?
    private var snapshotReadTask: Task<PlanSnapshot, Error>?
    private var snapshotReadID: UUID?
    private var readNotBefore: ContinuousClock.Instant?
    private var excessiveServerPause = false
    /// A scoped update can render into the same job. Its turn and event cursor distinguish its
    /// ready plan from an old ready projection that arrives before the update is visible.
    private var baselineJobID: String?
    private var baselineTurnID: String?
    private var baselineSequence = -1
    private var readSequence = 0
    private var initialFlag: PlanSectionID?
    /// The sheet was dismissed: an update already being sent still finishes (never half-send a turn), but nothing
    /// is followed afterwards.
    private var dismissed = false
    private let pollInterval: Duration
    private let giveUpAfter: Duration
    private let recoveryInterval: Duration
    private let readNow: () -> ContinuousClock.Instant
    private let readSleep: (Duration) async throws -> Void
    private let expectedThreadID: String?
    private let isCurrentScope: @MainActor () -> Bool

    init(seed: [PlanBlock], initialFlag: PlanSectionID?, actions: ReviewPlanActions,
         pollInterval: Duration = ReviewPlanModel.defaultPollInterval, giveUpAfter: Duration = .seconds(600),
         recoveryInterval: Duration = .seconds(30), expectedThreadID: String? = nil,
         isCurrentScope: @escaping @MainActor () -> Bool = { true },
         readNow: @escaping () -> ContinuousClock.Instant = { ContinuousClock.now },
         readSleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.blocks = seed
        self.isLoading = seed.isEmpty
        self.initialFlag = initialFlag
        self.actions = actions
        self.pollInterval = pollInterval
        self.giveUpAfter = giveUpAfter
        self.recoveryInterval = recoveryInterval
        self.expectedThreadID = expectedThreadID
        self.isCurrentScope = isCurrentScope
        self.readNow = readNow
        self.readSleep = readSleep
        if let initialFlag { draft.setFlagged([initialFlag]) }
    }

    /// UI tests poll quickly; the app polls at a calm pace.
    static var defaultPollInterval: Duration {
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") ? .milliseconds(400) : .milliseconds(1500)
    }

    // MARK: Derived

    var status: PlanSnapshot.Status? { snapshot?.status }
    var isBusy: Bool { phase != .reviewing }
    /// An update plan is settling, whether this sheet started it or it was already in flight when it opened.
    var isUpdating: Bool { phase != .reviewing || snapshot?.status == .updating }
    var hasChanges: Bool { blocks.contains { $0.changed } }
    /// After an update finished: changed sections show "Updated" with Undo.
    var showsUpdated: Bool { !isUpdating && hasChanges }
    var canUndoAll: Bool { showsUpdated && snapshot?.draft?.canUndo == true }
    var changedSections: [PlanSectionID] { blocks.filter(\.changed).map(\.section) }

    func block(_ section: PlanSectionID) -> PlanBlock? { blocks.first { $0.section == section } }

    /// Undo needs the draft head the server sent with the snapshot (its revision guards the restore); without one
    /// the button must not be offered.
    func canUndo(_ block: PlanBlock) -> Bool {
        block.changed && block.previous != nil && snapshot?.draft != nil
    }

    /// Whether the creator may flag or edit this section right now.
    func canChange(_ block: PlanBlock) -> Bool {
        block.section.isScopable && block.editable && !block.skipped && block.state == .decided && !isUpdating
    }

    /// The state a card shows. While the update has been sent but the server has not switched jobs yet, the
    /// sections being redone already read as deciding, so the cards never flash their old content as current.
    func displayState(_ block: PlanBlock) -> PlanBlockState {
        if phase != .reviewing, updatingScope.contains(block.section), snapshot?.jobID == baselineJobID, block.state == .decided {
            return .deciding
        }
        return block.state
    }

    var updateSummaryText: String? {
        if let text = snapshot?.updateSummary?.text, !text.isEmpty { return text }
        guard hasChanges else { return nil }
        let names = changedSections.map { $0.label.lowercased() }
        return String(localized: "Updated \(names.formatted(.list(type: .and))).")
    }

    /// Fraction of the redone sections the server has decided again.
    var updateProgress: Double {
        let scope = updatingScope.isEmpty ? (snapshot?.scope ?? []) : updatingScope
        guard !scope.isEmpty else { return 0 }
        let done = scope.filter { section in
            guard let block = block(section) else { return false }
            return displayState(block) == .decided
        }.count
        return max(0.08, Double(done) / Double(scope.count))
    }

    // MARK: Loading

    func load() async {
        await refresh(initial: true)
    }

    func refresh(initial: Bool = false) async {
        guard !dismissed, !Task.isCancelled, isCurrentScope(), phase != .submitting else { return }
        readSequence += 1
        let sequence = readSequence
        var failedRead = false
        do {
            let fresh = try await readSnapshot(sequence: sequence)
            guard !dismissed, !Task.isCancelled, isCurrentScope(), sequence == readSequence else { return }
            if apply(fresh) {
                loadFailed = false
                settleIfCurrent(fresh)
            } else { failedRead = true }
        } catch {
            guard !dismissed, !Task.isCancelled, isCurrentScope(), sequence == readSequence else { return }
            if error is CancellationError { return }
            failedRead = true
            if blocks.isEmpty { loadFailed = true }
        }
        isLoading = false
        resumeIfUpdating()
        if initial, failedRead, followTask == nil { startFollowing(awaitNewResult: false) }
    }

    /// The sheet opened while a plan was already updating: follow its decisions to the end.
    /// This snapshot does not establish rendered video or Photos output readiness.
    private func resumeIfUpdating() {
        guard followTask == nil, snapshot?.status == .updating else { return }
        phase = .updating
        startFollowing(awaitNewResult: followExpectation ?? false)
    }

    private func startFollowing(awaitNewResult: Bool) {
        guard !dismissed, followTask == nil else { return }
        let id = UUID()
        followID = id
        followExpectation = awaitNewResult
        followTask = Task { [weak self] in
            guard let self else { return }
            defer { self.releaseFollower(id) }
            await self.follow()
        }
    }

    private func releaseFollower(_ id: UUID) {
        guard followID == id else { return }
        followID = nil
        followTask = nil
    }

    /// One transport reader owns the GET. Later refreshes wait and supersede its
    /// application, while even a cancelled response retains its server pacing.
    private func readSnapshot(sequence: Int) async throws -> PlanSnapshot {
        while let active = snapshotReadTask {
            _ = try? await active.value
            guard !dismissed, !Task.isCancelled, isCurrentScope(), sequence == readSequence else { throw CancellationError() }
        }
        guard !excessiveServerPause else { throw ReviewReadPaused() }
        while let deadline = readNotBefore, deadline > readNow() {
            try await readSleep(readNow().duration(to: deadline))
            guard !dismissed, !Task.isCancelled, isCurrentScope(), sequence == readSequence else { throw CancellationError() }
        }
        guard !dismissed, !Task.isCancelled, isCurrentScope(), sequence == readSequence else { throw CancellationError() }
        // A different waiter can acquire the reader while this one sleeps.
        if snapshotReadTask != nil { return try await readSnapshot(sequence: sequence) }
        let id = UUID()
        snapshotReadID = id
        let task = Task<PlanSnapshot, Error> { [weak self, actions] in
            defer {
                if self?.snapshotReadID == id {
                    self?.snapshotReadTask = nil
                    self?.snapshotReadID = nil
                }
            }
            do { return try await actions.loadSnapshot() }
            catch {
                self?.recordRetryAfter(error)
                throw error
            }
        }
        snapshotReadTask = task
        return try await task.value
    }

    private struct ReviewReadPaused: Error {}

    private func recordRetryAfter(_ error: Error) {
        guard case let APIError.requestFailed(_, detail) = error,
              let hint = detail.retryAfter, hint.isFinite, hint > 0 else { return }
        // Reject an unusable interval rather than overflow a clock deadline or
        // silently retry early. Ordinary long hints retain automatic recovery.
        guard hint <= Double(Int32.max) else { excessiveServerPause = true; return }
        let deadline = readNow().advanced(by: .seconds(hint))
        if readNotBefore == nil || deadline > readNotBefore! { readNotBefore = deadline }
    }

    private func settleIfCurrent(_ fresh: PlanSnapshot) {
        guard let awaitNewResult = followExpectation else { return }
        let newResult = fresh.jobID != nil && fresh.jobID != baselineJobID
            || fresh.turnID != nil && fresh.turnID != baselineTurnID && fresh.nextAfterSequence > baselineSequence
        guard !awaitNewResult || newResult else { return }
        if fresh.status == .cancelled { finish(notice: String(localized: "The update was stopped.")) }
        if fresh.status == .ready { finish(notice: nil) }
    }

    @discardableResult
    private func apply(_ fresh: PlanSnapshot) -> Bool {
        if let expectedThreadID, !sameThread(fresh.threadID, expectedThreadID) { return false }
        if let current = snapshot {
            guard sameThread(fresh.threadID, current.threadID),
                  fresh.threadRevision >= current.threadRevision,
                  fresh.nextAfterSequence >= current.nextAfterSequence else { return false }
        }
        snapshot = fresh
        if fresh.status != .empty { blocks = fresh.blocks }
        let editable = Set(fresh.blocks.filter { canChange($0) }.map(\.section))
        if let pending = initialFlag {
            initialFlag = nil
            if editable.contains(pending) { draft.setFlagged(draft.flagged.union([pending])) }
        }
        draft.prune(keeping: editable)
        return true
    }

    private func sameThread(_ lhs: String, _ rhs: String) -> Bool {
        if let left = UUID(uuidString: lhs), let right = UUID(uuidString: rhs) { return left == right }
        return lhs == rhs
    }

    // MARK: Actions

    func toggle(_ section: PlanSectionID) {
        guard let block = block(section), canChange(block) || draft.isFlagged(section) else { return }
        notice = nil
        draft.toggle(section)
    }

    func submitUpdate() {
        guard phase == .reviewing, draft.canUpdate else { return }
        let scope = draft.scope
        let message = draft.message
        let edits = draft.manualEdits(titleBarID: titleBarID, captionOrder: captionLineOrder)
        askedPrompt = draft.trimmedPrompt.isEmpty ? nil : draft.trimmedPrompt
        begin(scope: scope) { [actions] in
            try await actions.update(scope, edits, message)
            return true
        }
    }

    func undo(_ section: PlanSectionID) {
        guard phase == .reviewing, let block = block(section), canUndo(block),
              let draftRevision = snapshot?.draft?.draftRevision else { return }
        askedPrompt = nil
        begin(scope: [section]) { [actions] in try await actions.undoSection(section, block.revision, draftRevision) }
    }

    func undoAll() {
        guard phase == .reviewing, canUndoAll, let draftRevision = snapshot?.draft?.draftRevision else { return }
        askedPrompt = nil
        begin(scope: changedSections) { [actions] in
            try await actions.undoAll(draftRevision)
            return true
        }
    }

    /// The sheet went away. Polling stops; a turn that is still being sent / approved is allowed to finish, so a
    /// dismissal never leaves a submitted draft unapproved.
    func cancelFollowing() {
        dismissed = true
        readSequence += 1
        snapshotReadTask?.cancel()
        if phase != .submitting {
            followTask?.cancel()
            followTask = nil
            followID = nil
        }
    }

    private var titleBarID: String? {
        if case .title(let payload)? = block(.title)?.payload { return payload.barID }
        return nil
    }

    private var captionLineOrder: [String] {
        if case .captions(let payload)? = block(.captions)?.payload { return payload.lines.map(\.id) }
        return []
    }

    /// `send` returns whether it queued a re-render (see `ReviewPlanActions.undoSection`).
    private func begin(scope: [PlanSectionID], send: @escaping @MainActor () async throws -> Bool) {
        guard !dismissed, !Task.isCancelled, isCurrentScope() else { return }
        notice = nil
        phase = .submitting
        updatingScope = scope
        baselineJobID = snapshot?.jobID
        baselineTurnID = snapshot?.turnID
        baselineSequence = snapshot?.nextAfterSequence ?? -1
        readSequence += 1
        snapshotReadTask?.cancel()
        followTask?.cancel()
        let id = UUID()
        followID = id
        followExpectation = nil
        followTask = Task { [weak self] in
            defer { self?.releaseFollower(id) }
            let rendering: Bool
            do {
                rendering = try await send()
            } catch is CancellationError {
                return
            } catch {
                guard let self, !self.dismissed, self.isCurrentScope() else { return }
                await self.failed(error)
                return
            }
            guard let self, !Task.isCancelled, self.isCurrentScope() else { return }
            self.draft = ReviewPlanDraft()
            if self.dismissed {
                self.finish(notice: nil)
            } else if rendering {
                self.phase = .updating
                self.followExpectation = true
                await self.follow()
            } else {
                // Nothing was queued (a restore that only changed the draft): show what the server has now.
                self.phase = .reviewing
                await self.refresh()
                self.finish(notice: nil)
            }
        }
    }

    private func failed(_ error: Error) async {
        phase = .reviewing
        updatingScope = []
        if let review = error as? ReviewPlanError {
            notice = review.message
        } else if let api = error as? APIError, api.isConflict {
            // Stale revision (the thread or the section moved on): show the latest plan and let them retry.
            notice = String(localized: "That changed in the meantime. Review the latest and try again.")
            await refresh()
        } else if let api = error as? APIError, case .requestFailed(let status, _) = api, status == 404 || status == 422 {
            notice = api.requestFailureDetail ?? String(localized: "Kria couldn’t make that change.")
        } else {
            notice = String(localized: "Kria couldn’t send that. Check your connection and try again.")
        }
    }

    /// A new job or a newer turn on the same job can finish this update. An unchanged old ready
    /// projection cannot. Reopened sheets can follow the update they already observed.
    private func follow() async {
        let started = readNow()
        var failures = 0
        var recovering = false
        while !Task.isCancelled, !dismissed, isCurrentScope() {
            readSequence += 1
            let sequence = readSequence
            do {
                let fresh = try await readSnapshot(sequence: sequence)
                guard !dismissed, !Task.isCancelled, isCurrentScope() else { return }
                if sequence == readSequence, apply(fresh) {
                    failures = 0
                    loadFailed = false
                    isLoading = false
                    settleIfCurrent(fresh)
                    if followExpectation == nil { return }
                }
            } catch is CancellationError {
                if dismissed || Task.isCancelled || !isCurrentScope() { return }
                // A visible refresh owns the newer read; it can settle this
                // update, otherwise this follower remains its slow fallback.
                if followExpectation == nil { return }
            } catch {
                guard !dismissed, !Task.isCancelled, isCurrentScope() else { return }
                if sequence == readSequence { failures += 1 }
                if excessiveServerPause {
                    notice = String(localized: "Kria asked us to pause checking this plan. Your update remains saved.")
                    return
                }
            }
            if failures >= 6 || readNow() - started >= giveUpAfter {
                recovering = true
                notice = phase == .updating ? String(localized: "Your update keeps going. Review will check again automatically.")
                    : String(localized: "Review will check the plan again automatically.")
            }
            let delay = recovering ? recoveryInterval : min(pollInterval * (1 << min(failures, 4)), .seconds(30))
            do { try await readSleep(delay) } catch { return }
        }
    }

    private func finish(notice: String?) {
        followExpectation = nil
        followTask?.cancel()
        followTask = nil
        followID = nil
        phase = .reviewing
        updatingScope = []
        self.notice = notice
    }
}
