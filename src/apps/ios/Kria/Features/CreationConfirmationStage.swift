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
}

struct CreationConfirmationStage: View {
    let thread: CreationThread
    let isBusy: Bool
    var conflict: CreationConfirmationConflict? = nil
    var refreshDirection: () -> Void = {}
    let action: (String, [String: JSONValue]) -> Void

    private var cleanup: [String: JSONValue] { thread.speechCleanup ?? [:] }
    private var analysis: [String: JSONValue] { cleanup["analysis"]?.objectValue ?? [:] }
    private var hasCleanup: Bool { cleanup["applicable"]?.booleanValue == true }
    private var isFailure: Bool { thread.summary.status == .failed }
    private var hasVideo: Bool { attachedVideoClipCount(in: thread.state ?? [:]) > 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(isFailure ? "Your project is safe" : "Here’s the direction I’ll use")
                .font(KriaFont.display(24))
            Text(thread.creatorAgent?["summary"]?.stringValue ?? "Kria will use your footage and direction to make a new cut. Rendering starts only after you approve.")
                .font(KriaFont.body(14))
            if isFailure && thread.activeJobID == nil {
                Text("Kria couldn’t start the video. Your direction and footage are still saved.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if isBusy { ProgressView("Saving your choice…") }
            if let reason = thread.job?.failureReason, isFailure {
                Text(reason).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if let conflict { conflictNotice(conflict) }
            if hasCleanup {
                if let identifier = analysis["id"]?.stringValue { cleanupActions(identifier: identifier) }
                else { ProgressView("Preparing the speech check…") }
            } else {
                Button(isFailure ? "Retry generation" : "Create this video") {
                    action(isFailure ? "retry" : "generate", [:])
                }.buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
            }
            Text("You can also send a message to change the direction.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
        }
        .accessibilityIdentifier("creation-confirmation")
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
                Button("Clean up speech and create") {
                    action("generate", payload.merging(["speech_cleanup_choice": .string("clean")]) { _, new in new })
                }.buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
                Button("Keep original speech and create") {
                    action("generate", payload.merging(["speech_cleanup_choice": .string("keep_original")]) { _, new in new })
                }.disabled(isBusy || !hasVideo)
            } else {
                Button(isFailure ? "Retry generation" : "Create this video") {
                    action(isFailure ? "retry" : "generate", payload)
                }.buttonStyle(CanonicalPrimaryButtonStyle()).disabled(isBusy || !hasVideo)
            }
        default:
            Text("The speech check is unavailable. Reopen the project to refresh it.")
        }
    }
}
