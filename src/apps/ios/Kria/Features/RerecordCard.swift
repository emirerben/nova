import SwiftUI

struct RerecordActions {
    var start: () -> Void = {}
    var retry: () -> Void = {}
    var dismissFailure: () -> Void = {}
    var makeAgain: () -> Void = {}
}

/// The re-record entry and its progress on the ready video. Which of these shows is decided by `RerecordPhase`;
/// this view only draws it.
struct RerecordCard: View {
    let phase: RerecordPhase
    let actions: RerecordActions

    var body: some View {
        switch phase {
        case .ready:
            Button(action: actions.start) {
                Label("Re-record take", systemImage: "arrow.counterclockwise").frame(maxWidth: .infinity)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .accessibilityIdentifier("ready-rerecord")
        case .replacing:
            progress("Swapping in your new take…", note: "Your current video stays until the new one is ready.", id: "rerecord-replacing")
        case .planning:
            progress("Making your video again…", note: nil, id: "rerecord-planning")
        case let .failed(failure):
            VStack(alignment: .leading, spacing: 10) {
                Text(failure.message).font(KriaFont.body(14)).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("rerecord-failure")
                HStack(spacing: 8) {
                    if failure.retryable {
                        Button("Try again", action: actions.retry).buttonStyle(KriaPrimaryButtonStyle()).accessibilityIdentifier("rerecord-retry")
                    }
                    Button("Dismiss", action: actions.dismissFailure).buttonStyle(KriaSecondaryButtonStyle()).accessibilityIdentifier("rerecord-dismiss")
                }
            }
        case .planOwed:
            VStack(alignment: .leading, spacing: 10) {
                Text("Your new take is attached. Your current video stays until you make it again.")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                Button(action: actions.makeAgain) { Text("Make it again with my new take").frame(maxWidth: .infinity) }
                    .buttonStyle(KriaPrimaryButtonStyle()).accessibilityIdentifier("rerecord-make-again")
            }
        case .unavailable, .confirming:
            EmptyView()
        }
    }

    private func progress(_ title: String, note: String?, id: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) { ProgressView().controlSize(.small).tint(KriaColor.ink); Text(title).font(KriaFont.body(14).weight(.medium)) }
            if let note { Text(note).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(id)
    }
}
