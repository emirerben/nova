import Foundation
import Observation

/// The state machine behind `ReviewPlanView`: loads the plan snapshot, holds the creator's flags and edits, sends
/// the scoped turn, and follows the update render until it is ready. The workspace owns the thread revision, the
/// delta feed and the approval and reaches them through `ReviewPlanActions`.
@MainActor @Observable
final class ReviewPlanModel {
    enum Phase: Equatable {
        /// Nothing in flight: the creator is reading, flagging or editing.
        case reviewing
        /// A turn is being sent / approved.
        case submitting
        /// The update render is running; `scope` are the sections being redone.
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
    /// The job the creator was looking at when they pressed Update / Undo: the update is done once the
    /// snapshot shows a different job in `ready`.
    private var baselineJobID: String?
    private var initialFlag: PlanSectionID?
    /// The sheet was dismissed: an update already being sent still finishes (never half-send a turn), but nothing
    /// is followed afterwards.
    private var dismissed = false
    private let pollInterval: Duration
    private let giveUpAfter: Duration

    init(seed: [PlanBlock], initialFlag: PlanSectionID?, actions: ReviewPlanActions,
         pollInterval: Duration = ReviewPlanModel.defaultPollInterval, giveUpAfter: Duration = .seconds(600)) {
        self.blocks = seed
        self.isLoading = seed.isEmpty
        self.initialFlag = initialFlag
        self.actions = actions
        self.pollInterval = pollInterval
        self.giveUpAfter = giveUpAfter
        if let initialFlag { draft.setFlagged([initialFlag]) }
    }

    /// UI tests poll quickly; the app polls at a calm pace.
    static var defaultPollInterval: Duration {
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") ? .milliseconds(400) : .milliseconds(1500)
    }

    // MARK: Derived

    var status: PlanSnapshot.Status? { snapshot?.status }
    var isBusy: Bool { phase != .reviewing }
    /// An update render is running, whether this sheet started it or it was already in flight when it opened.
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
        return "Updated " + names.formatted(.list(type: .and)) + "."
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
        do {
            let fresh = try await actions.loadSnapshot()
            apply(fresh)
            loadFailed = false
        } catch {
            if blocks.isEmpty { loadFailed = true }
        }
        isLoading = false
        if initial { resumeIfUpdating() }
    }

    /// The sheet opened while an update render was already running (it was closed mid-update, or started from the
    /// chat): nothing else would ever poll, so follow it to its end.
    private func resumeIfUpdating() {
        guard phase == .reviewing, followTask == nil, snapshot?.status == .updating else { return }
        followTask = Task { [weak self] in await self?.follow(awaitNewJob: false) }
    }

    private func apply(_ fresh: PlanSnapshot) {
        snapshot = fresh
        if fresh.status != .empty { blocks = fresh.blocks }
        let editable = Set(fresh.blocks.filter { canChange($0) }.map(\.section))
        if let pending = initialFlag {
            initialFlag = nil
            if editable.contains(pending) { draft.setFlagged(draft.flagged.union([pending])) }
        }
        draft.prune(keeping: editable)
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
        if phase != .submitting {
            followTask?.cancel()
            followTask = nil
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
        notice = nil
        phase = .submitting
        updatingScope = scope
        baselineJobID = snapshot?.jobID
        followTask?.cancel()
        followTask = Task { [weak self] in
            let rendering: Bool
            do {
                rendering = try await send()
            } catch is CancellationError {
                return
            } catch {
                await self?.failed(error)
                return
            }
            guard let self, !Task.isCancelled else { return }
            self.draft = ReviewPlanDraft()
            if self.dismissed {
                self.finish(notice: nil)
            } else if rendering {
                self.phase = .updating
                await self.follow(awaitNewJob: true)
            } else {
                // Nothing was queued (a restore that only changed the draft): show what the server has now.
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
            notice = "That changed in the meantime. Review the latest and try again."
            await refresh()
        } else if let api = error as? APIError, case .requestFailed(let status, _) = api, status == 404 || status == 422 {
            notice = api.requestFailureDetail ?? "Kria couldn’t make that change."
        } else {
            notice = "Kria couldn’t send that. Check your connection and try again."
        }
    }

    /// Polls the snapshot until the update render is ready, stopped, or taking too long. `awaitNewJob`: the update
    /// this sheet sent must show up as a different job first (an old `ready` is not its result); false when
    /// following a render that was already running.
    private func follow(awaitNewJob: Bool) async {
        let started = ContinuousClock.now
        var failures = 0
        while !Task.isCancelled {
            do {
                let fresh = try await actions.loadSnapshot()
                failures = 0
                apply(fresh)
                if fresh.status == .cancelled {
                    finish(notice: "The update was stopped.")
                    return
                }
                if fresh.status == .ready, !awaitNewJob || fresh.jobID != baselineJobID {
                    finish(notice: nil)
                    return
                }
            } catch is CancellationError {
                return
            } catch {
                failures += 1
                if failures >= 6 {
                    finish(notice: "Kria lost the connection. Your update keeps going; reopen Review to see it.")
                    return
                }
            }
            if ContinuousClock.now - started > giveUpAfter {
                finish(notice: "This is taking longer than usual. It keeps going in the background.")
                return
            }
            try? await Task.sleep(for: pollInterval)
        }
    }

    private func finish(notice: String?) {
        phase = .reviewing
        updatingScope = []
        self.notice = notice
    }
}
