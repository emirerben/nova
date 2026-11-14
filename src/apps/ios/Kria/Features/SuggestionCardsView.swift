import SwiftUI

/// Everything a card surface needs from the chat that owns the set: the cards still worth drawing, which of them the
/// composer draft already carries, and what a tap does to that draft. A tap never sends; the creator does, from the
/// composer. `isSelected` is a closure so only the chips read the draft: a keystroke redraws them, not the workspace.
struct SuggestionCardsPresentation {
    let cards: [SuggestionCard]
    /// A card is selected while one line of the composer's text is the card's prompt.
    let isSelected: (SuggestionCard) -> Bool
    /// Adds the card's prompt to the draft, or takes it out again.
    let toggle: (SuggestionCard) -> Void
    /// The image a paid card is making (or made), drawn above the chips until the creator reviews or dismisses it.
    var generated: GeneratedCardResult? = nil
    var reviewGenerated: () -> Void = {}
    var dismissGenerated: () -> Void = {}

    var isEmpty: Bool { cards.isEmpty && generated == nil }
}

/// The server's suggestions as AI chips (glass with a turning light), in rank order. Each tap adds the chip's prompt to
/// the composer on its own line, so several can be combined before Send. Hook and title chips show their exact words as
/// the caption, so the creator reads what a send would approve.
struct SuggestionCardChips: View {
    let presentation: SuggestionCardsPresentation
    let idPrefix: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let generated = presentation.generated {
                GeneratedCardRow(
                    result: generated, identifier: "\(idPrefix)-generated",
                    review: presentation.reviewGenerated, dismiss: presentation.dismissGenerated
                )
                .transition(.opacity)
            }
            PlanChipFlow(spacing: 8) {
                ForEach(Array(presentation.cards.enumerated()), id: \.element.id) { index, card in
                    let selected = presentation.isSelected(card)
                    KriaAnswerChip(
                        title: card.title, caption: card.copyText ?? card.why,
                        state: selected ? .selected : .idle, isSuggestion: true, isLead: index == 0
                    ) { presentation.toggle(card) }
                    .accessibilityLabel(card.accessibilityText)
                    .accessibilityHint(selected ? String(localized: "Takes this out of your message") : String(localized: "Adds this to your message"))
                    .accessibilityIdentifier("\(idPrefix)-\(index)")
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: presentation.cards.map(\.id))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("\(idPrefix)s")
    }
}

/// The image a paid card is making, then the way to look at it. It outlives the card: the server replaces the set the
/// moment the card is spent, so this row does not depend on any set.
struct GeneratedCardRow: View {
    let result: GeneratedCardResult
    let identifier: String
    let review: () -> Void
    let dismiss: () -> Void

    private var message: String {
        switch result.phase {
        case .making: String(localized: "Making your image…")
        case .ready: String(localized: "Your image is ready")
        case .unsure: String(localized: "Kria isn't sure your image finished")
        case .failed: String(localized: "Kria couldn't make the image")
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if result.phase == .making {
                    ProgressView().controlSize(.small).tint(KriaColor.ink)
                } else {
                    Image(systemName: result.phase == .failed ? "exclamationmark.circle" : "photo.badge.checkmark")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(KriaColor.ink)
                }
            }
            .frame(width: 30, height: 30)
            .background(KriaColor.butter, in: Circle())
            .accessibilityHidden(true)
            Text(message)
                .font(KriaFont.body(14).weight(.semibold))
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("\(identifier)-status")
            if result.phase == .ready || result.phase == .unsure {
                Button("Review", action: review)
                    .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .accessibilityIdentifier("\(identifier)-review")
            }
            if result.canDismiss {
                Button(action: dismiss) {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(KriaColor.mutedInk)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss")
                .accessibilityIdentifier("\(identifier)-dismiss")
            }
        }
        .padding(.leading, 12)
        .padding(.vertical, 6)
        .frame(minHeight: 44)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifier)
    }
}
