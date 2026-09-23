import SwiftUI

/// A 409 the server returned when the creator confirmed Kria's direction,
/// turned into what the confirmation card shows next.
struct CreationConfirmationConflict: Equatable {
    /// Sent by "Refresh the direction" so Kria re-plans against the footage attached now.
    static let refreshDirectionMessage = "Keep the same plan with my current footage"
    static let changedMessage = "This project changed. Review the latest options and try again."
    static let missingReasonMessage = "This direction is out of date. Refresh it before creating the video."
    /// Actions the card sends through the Creator confirmation controller.
    static let confirmationActions: Set<String> = ["generate", "retry", "create_without_cleanup"]
    /// Revision and idempotency fences. The refresh after every conflict resolves them.
    private static let resolvedByRefresh: Set<String> = ["Creation thread changed", "Creator plan changed", "Idempotency key reused"]
    /// Reasons that clear on their own, so the same create button works afterwards.
    private static let resolvedByWaiting: Set<String> = ["Wait for the current render before confirming"]

    let message: String
    /// True when confirming again cannot succeed until Kria plans a new direction,
    /// for example after footage changed or the render attempts ran out.
    let offersDirectionRefresh: Bool
    /// The direction this conflict was reported against. A newer direction hides it.
    let planIdentity: String

    init(detail: String?, planIdentity: String) {
        self.planIdentity = planIdentity
        let detail = detail?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if Self.resolvedByWaiting.contains(detail) {
            message = Self.sentence(detail)
            offersDirectionRefresh = false
        } else if Self.resolvedByRefresh.contains(detail) || Self.isMachineCode(detail) {
            // Machine codes such as speech_cleanup_pending: the refreshed
            // speech-check card already shows the creator's next step.
            message = Self.changedMessage
            offersDirectionRefresh = false
        } else if detail.isEmpty {
            message = Self.missingReasonMessage
            offersDirectionRefresh = true
        } else {
            // Unknown reasons offer the refresh: a new direction is the one
            // path that cannot loop on a rejection the client can't classify.
            message = Self.sentence(detail)
            offersDirectionRefresh = true
        }
    }

    private static func isMachineCode(_ detail: String) -> Bool {
        !detail.isEmpty && !detail.contains(where: \.isWhitespace)
    }

    private static func sentence(_ detail: String) -> String {
        guard let last = detail.last, !".!?".contains(last) else { return detail }
        return detail + "."
    }
}

extension CreationThread {
    /// Identifies Kria's current direction. A new plan changes its version or hash.
    var creatorPlanIdentity: String {
        func text(_ value: JSONValue?) -> String {
            switch value {
            case let .string(string): string
            case let .number(number): String(number)
            default: ""
            }
        }
        return "\(text(creatorAgent?["version"]))|\(text(creatorAgent?["plan_hash"]))"
    }

    /// The structured `code`/`message` the server attached to the most recent
    /// `assistant_error` chat event (KRI-132) -- `_creator_agent_projection`
    /// doesn't expose `CreatorAgentSession.last_error` directly, so this reads
    /// it off the event log the client already has. Nil for a thread with no
    /// such event, or an older event shaped before the code was added.
    var lastAssistantErrorCode: String? {
        events.last(where: { $0.eventType == "assistant_error" })?.payload?["code"]?.stringValue
    }
    var lastAssistantErrorMessage: String? {
        events.last(where: { $0.eventType == "assistant_error" })?.payload?["message"]?.stringValue
    }
}

/// Non-retryable structural/deterministic rejections: confirming again fails
/// identically until something about the project changes (a different format,
/// a voiceover removed, re-enrolling), so "Retry generation" is misleading
/// here (KRI-132). Originally just the phone-gate family
/// (`app.tasks.content_plan_build.PHONE_GATE_MESSAGES`); KRI-118 items 1-2
/// (`app.routes.creator_agent`) added two more deterministic `last_error`
/// codes that fail the same session, before any Job exists, the same way:
/// - `strategy_invalid` (`app.services.creator_errors.CreatorStrategyError`)
/// - `phone_self_narration_multi_clip` (self-narration across 2+ clips has no
///   phone compiler yet -- `PHONE_GATE_MESSAGES["self_narration_multi_clip"]`)
/// - `speech_cleanup_unavailable_on_phone` (cleanup requested on an
///   analysis-proxy phone source, which never carries the real audio bytes)
/// All literals verified against `origin/feat/kri118-l4-thread-api-2026-09-23`.
/// `format_mismatch` (KRI-118 L4) was deliberately NOT added here: it is
/// raised synchronously as a 409 detail on the confirm action itself (never a
/// `last_error`/`assistant_error` code this property can see), and is already
/// surfaced correctly through `CreationConfirmationConflict`.
let nonRetryablePhoneGateErrorCodes: Set<String> = [
    "phone_not_enrolled", "phone_plan_unapproved", "phone_format_unavailable", "phone_voiceover_unavailable",
    "strategy_invalid", "phone_self_narration_multi_clip", "speech_cleanup_unavailable_on_phone",
]

/// KRI-118 item 2: `day_vlog`/`single_hero` still report `edit_format:
/// "montage"` to the chat picker (the picker only ever offers Montage for the
/// whole `GUIDED_EDIT_FORMATS` family), so the actual shape rides separately
/// on `creator_agent.story_shape` (`app.services.creator_sessions.
/// compile_active_plan` / `_creator_agent_projection`, lane L2/L4). Nil (no
/// subtitle) for any other or absent shape -- this is a label, not a new
/// picker entry.
func storyShapeSubtitle(creatorAgent: [String: JSONValue]?) -> String? {
    switch creatorAgent?["story_shape"]?.stringValue {
    case "day_vlog": "Day vlog"
    case "single_hero": "Single hero"
    default: nil
    }
}

/// KRI-118 item 3: server-authored "what Kria changed" notes -- `notices` are
/// the Main Creator's own server-repaired direction notes
/// (`compile_active_plan`, lane L2); `adjustments` are the guided
/// specialist's structural repair notes on the item's edit proposal
/// (`app.services.story_shapes.humanize_repairs`, lane L3). Merge
/// adjustments-first, then notices, dropping any repeat across the two lists
/// so the same note never shows twice. Empty when both are empty or absent.
func mergedWhatKriaChanged(creatorAgent: [String: JSONValue]?) -> [String] {
    let adjustments = (creatorAgent?["adjustments"]?.arrayValue ?? []).compactMap(\.stringValue)
    let notices = (creatorAgent?["notices"]?.arrayValue ?? []).compactMap(\.stringValue)
    var seen = Set<String>()
    return (adjustments + notices).filter { seen.insert($0).inserted }
}

struct CreationConfirmationStage: View {
    let thread: CreationThread
    let isBusy: Bool
    var responseStartedAt: Date? = nil
    var conflict: CreationConfirmationConflict? = nil
    var refreshDirection: () -> Void = {}
    let action: (String, [String: JSONValue]) -> Void

    private var cleanup: [String: JSONValue] { thread.speechCleanup ?? [:] }
    private var analysis: [String: JSONValue] { cleanup["analysis"]?.objectValue ?? [:] }
    private var hasCleanup: Bool { cleanup["applicable"]?.booleanValue == true }
    private var isFailure: Bool { thread.summary.status == .failed }
    private var hasVideo: Bool { attachedVideoClipCount(in: thread.state ?? [:]) > 0 }
    private var proposalSummary: String {
        thread.creatorAgent?["summary"]?.stringValue ?? "Kria will use your footage and direction to make a new cut. Rendering starts only after you approve."
    }
    /// KRI-132: a phone-gate rejection is structural, not a transient failure --
    /// retrying without changing the project (a different format, a voiceover
    /// removed, re-enrolling) fails the exact same way.
    private var isNonRetryablePhoneGateFailure: Bool {
        isFailure && thread.lastAssistantErrorCode.map(nonRetryablePhoneGateErrorCodes.contains) == true
    }

    private var storyShapeLabel: String? { storyShapeSubtitle(creatorAgent: thread.creatorAgent) }
    private var whatKriaChanged: [String] { mergedWhatKriaChanged(creatorAgent: thread.creatorAgent) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(isFailure ? "Your project is safe" : "Here’s the direction I’ll use")
                .font(KriaFont.display(24))
            if let storyShapeLabel {
                Text(storyShapeLabel)
                    .font(KriaFont.body(12).weight(.medium))
                    .foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("creation-confirmation-story-shape")
            }
            ChatResponseText(content: proposalSummary, startedAt: responseStartedAt)
                .font(KriaFont.body(14))
                .copyableMessage(proposalSummary, previewShape: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel("Kria: \(proposalSummary)")
            if isFailure && thread.activeJobID == nil {
                Text("Kria couldn’t start the video. Your direction and footage are still saved.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if isBusy { ProgressView("Saving your choice…") }
            // A sentence, never the raw failure_reason taxonomy code the user
            // used to see verbatim here (e.g. "phone_plan_unsupported") --
            // KRI-163. Falls back through whatever detail the thread actually
            // carries for this failure before giving up.
            if isFailure,
                let message = thread.job?.failureMessage ?? thread.lastAssistantErrorMessage
                    ?? thread.preparationMessage
            {
                Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if let conflict { conflictNotice(conflict) }
            if hasCleanup {
                if let identifier = analysis["id"]?.stringValue { cleanupActions(identifier: identifier) }
                else { ProgressView("Preparing the speech check…") }
            } else {
                retryOrCreateButton(payload: [:])
            }
            if !whatKriaChanged.isEmpty { whatKriaChangedDisclosure }
            Text("You can also send a message to change the direction.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
        }
        .accessibilityIdentifier("creation-confirmation")
    }

    private var whatKriaChangedDisclosure: some View {
        DisclosureGroup("What Kria changed (\(whatKriaChanged.count))") {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(whatKriaChanged.enumerated()), id: \.offset) { _, note in
                    Text(note).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                }
            }
            .padding(.top, 4)
            .accessibilityIdentifier("creation-confirmation-what-changed-list")
        }
        .font(KriaFont.body(13).weight(.medium))
        .foregroundStyle(KriaColor.ink)
        .accessibilityIdentifier("creation-confirmation-what-changed")
    }

    /// "Retry generation" for an ordinary failure; for a non-transient phone-gate
    /// rejection, the server's own message and no button that implies retrying
    /// could work (KRI-132) -- confirming again dispatches the identical request.
    @ViewBuilder private func retryOrCreateButton(payload: [String: JSONValue]) -> some View {
        if isNonRetryablePhoneGateFailure {
            Text(thread.lastAssistantErrorMessage ?? "This edit can’t render on your iPhone yet. Your project is saved.")
                .font(KriaFont.body(13))
                .foregroundStyle(KriaColor.failureText)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("creation-confirmation-phone-gate")
        } else {
            Button(isFailure ? "Retry generation" : "Create this video") {
                action(isFailure ? "retry" : "generate", payload)
            }.buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
        }
    }

    private func conflictNotice(_ conflict: CreationConfirmationConflict) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(conflict.message)
                .font(KriaFont.body(13))
                .foregroundStyle(KriaColor.failureText)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("creation-confirmation-conflict")
            if conflict.offersDirectionRefresh {
                Button("Refresh the direction", action: refreshDirection)
                    .buttonStyle(CanonicalSecondaryButtonStyle())
                    .disabled(isBusy)
                    .accessibilityIdentifier("refresh-direction")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    @ViewBuilder private func cleanupActions(identifier: String) -> some View {
        let payload: [String: JSONValue] = ["speech_cleanup_analysis_id": .string(identifier)]
        let outcomeFailed = cleanup["outcome"]?.objectValue?["status"]?.stringValue == "failed"
        switch analysis["status"]?.stringValue {
        case "queued", "running":
            ProgressView("Checking speech…")
        case "failed":
            Text("The speech check couldn’t finish.")
            Button("Retry speech check") { action("retry_speech_cleanup", payload) }.disabled(isBusy)
            Button("Create without cleanup") { action("create_without_cleanup", payload) }
                .buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
        case "ready", "no_findings":
            if outcomeFailed {
                Text("Speech cleanup couldn’t be applied.")
                Button("Retry cleanup") { action("retry", payload) }.disabled(isBusy || !hasVideo)
                Button("Create without cleanup") { action("create_without_cleanup", payload) }
                    .buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
            } else if cleanup["requires_choice"]?.booleanValue == true {
                Text("Choose whether to remove the detected pauses and retakes.")
                Button {
                    action("generate", payload.merging(["speech_cleanup_choice": .string("clean")]) { _, new in new })
                } label: {
                    Text("Clean up speech and create")
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 12)
                }
                .buttonStyle(CanonicalPrimaryButtonStyle())
                .disabled(isBusy || !hasVideo)
                Button {
                    action("generate", payload.merging(["speech_cleanup_choice": .string("keep_original")]) { _, new in new })
                } label: {
                    Text("Keep original speech and create")
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 12)
                }
                .buttonStyle(CanonicalSecondaryButtonStyle())
                .disabled(isBusy || !hasVideo)
            } else {
                retryOrCreateButton(payload: payload)
            }
        default:
            Text("The speech check is unavailable. Reopen the project to refresh it.")
        }
    }
}
