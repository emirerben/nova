import SwiftUI

/// Steer now: the bar attached to the top of the chat input box that holds the message the creator sent while Kria was
/// still working (Codex's waiting-message bar, adapted to the phone and to Kria's light look). The message on one line,
/// then "↳ Steer" (only while the earlier turn can still be stopped), the trash and "⋯". Holding the bar opens the same
/// menu as "⋯": Edit message / Steer instead / Cancel message. Every control is a 44 pt target. Mockups and decisions:
/// docs/pipelines/steer-now.md.
struct WaitingMessageBar: View {
    let text: String
    let strings: SteerStrings
    let canSteer: Bool
    /// A request for this bar is in flight (or the message is still being sent): actions wait.
    let isBusy: Bool
    let note: String?
    let steer: () -> Void
    let remove: () -> Void
    let edit: () -> Void

    private static let shape = UnevenRoundedRectangle(
        topLeadingRadius: 12, bottomLeadingRadius: 0, bottomTrailingRadius: 0, topTrailingRadius: 12, style: .continuous
    )

    var body: some View {
        VStack(spacing: 6) {
            if let note {
                Text(note)
                    .font(KriaFont.body(12.5))
                    .foregroundStyle(KriaColor.mutedInk)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 8)
                    .accessibilityLabel(note)
                    .accessibilityIdentifier("chat-steer-note")
            }
            bar
        }
    }

    private var bar: some View {
        HStack(spacing: 0) {
            Image(systemName: "text.line.first.and.arrowtriangle.forward")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(KriaColor.mutedInk)
                .padding(.leading, 14).padding(.trailing, 10)
                .accessibilityHidden(true)
            Text(text)
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.ink)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel(strings.waitingLabel(text))
                .accessibilityHint(strings.waitingHint)
                .accessibilityIdentifier("chat-waiting-text")
            if canSteer {
                Button(action: steer) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.turn.down.right").font(.system(size: 13, weight: .semibold))
                        Text(strings.steer).font(KriaFont.body(13.5).weight(.semibold))
                    }
                    .foregroundStyle(KriaColor.mutedInk)
                    .padding(.horizontal, 6)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(strings.steer)
                .accessibilityHint(strings.steerHint)
                .accessibilityIdentifier("chat-steer-now")
            }
            Button(action: remove) {
                Image(systemName: "trash")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(KriaColor.mutedInk)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(strings.delete)
            .accessibilityIdentifier("chat-waiting-delete")
            Menu {
                menuItems
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(KriaColor.mutedInk)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel(strings.more)
            .accessibilityIdentifier("chat-waiting-more")
        }
        .frame(minHeight: 44)
        .background(KriaColor.fill, in: Self.shape)
        .overlay(Self.shape.stroke(KriaColor.line.opacity(0.75), lineWidth: 1))
        .contentShape(Self.shape)
        .contextMenu { menuItems }
        .disabled(isBusy)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("chat-waiting-bar")
    }

    @ViewBuilder private var menuItems: some View {
        Button(action: edit) { Label(strings.edit, systemImage: "pencil") }
        if canSteer {
            Button(action: steer) { Label(strings.steerInstead, systemImage: "arrow.turn.down.right") }
        }
        Button(role: .destructive, action: remove) { Label(strings.cancel, systemImage: "trash") }
    }
}
