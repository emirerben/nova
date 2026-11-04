import SwiftUI

/// Steer now: the row under a message that waits behind Kria's current work. "Up next" says the message is saved and
/// will be read next; "Steer now" (while the earlier turn can still be stopped) makes Kria switch to it at once. The
/// button is outlined, not Butter: the approval card's "Create this video" keeps the one primary action on screen.
struct UpNextRow: View {
    let strings: SteerStrings
    let canSteer: Bool
    let isSteering: Bool
    let note: String?
    let steer: () -> Void

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack(spacing: 10) {
                Spacer(minLength: 54)
                Text(strings.upNext)
                    .font(KriaFont.body(12).weight(.medium))
                    .foregroundStyle(KriaColor.zinc)
                    .accessibilityLabel(strings.upNext)
                    .accessibilityHint(strings.upNextHint)
                    .accessibilityIdentifier("chat-up-next")
                if canSteer || isSteering {
                    Button(action: steer) {
                        Text(isSteering ? strings.steering : strings.steerNow)
                            .font(KriaFont.body(13).weight(.semibold))
                            .foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 14)
                            .frame(minHeight: 44)
                            .overlay(Capsule().stroke(KriaColor.border, lineWidth: 1))
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(isSteering)
                    .accessibilityLabel(strings.steerNow)
                    .accessibilityHint(strings.steerNowHint)
                    .accessibilityIdentifier("chat-steer-now")
                }
            }
            if let note {
                Text(note)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.mutedInk)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.leading, 54)
                    .accessibilityLabel(note)
                    .accessibilityIdentifier("chat-steer-note")
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}
