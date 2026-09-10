import SwiftUI

struct CreationConfirmationStage: View {
    let thread: CreationThread
    let isBusy: Bool
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
