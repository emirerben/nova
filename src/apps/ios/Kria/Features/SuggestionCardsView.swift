import SwiftUI

/// Everything a card surface needs from the chat that owns the set: the cards still worth drawing, the ledger
/// that tracks taps, and what a tap or a dismissal does. Handed to the ready stage and to the editor, so both
/// show the same cards and the same state.
struct SuggestionCardsPresentation {
    let cards: [SuggestionCard]
    let notice: SuggestionStaleNotice?
    let tap: (SuggestionCard) -> Void
    let dismiss: (SuggestionCard) -> Void
    let refresh: () -> Void
    /// The card now sending, so its row can say so and the others wait.
    let sendingID: String?

    var isEmpty: Bool { cards.isEmpty && notice == nil }
}

/// One suggestion: what it does, why it is offered, the measured fact behind it, and (hook and title cards) the exact
/// text the tap approves. The whole card is the tap target; the dismiss button is separate and always 44 pt.
/// Fills, not borders; Inter via `KriaFont`; sizes scale with Dynamic Type.
struct SuggestionCardView: View {
    let card: SuggestionCard
    let identifier: String
    var isSending = false
    var isBusy = false
    let tap: () -> Void
    let dismiss: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            Button(action: tap) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: card.symbolName)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(KriaColor.ink)
                            .frame(width: 30, height: 30)
                            .background(KriaColor.butter, in: Circle())
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(card.title)
                                .font(KriaFont.body(14).weight(.semibold))
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            if let why = card.why {
                                Text(why)
                                    .font(KriaFont.body(12))
                                    .foregroundStyle(KriaColor.mutedInk)
                                    .multilineTextAlignment(.leading)
                                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 1)
                                    .truncationMode(.tail)
                            }
                        }
                    }
                    // The words the creator is about to approve, whole and unclipped.
                    if let copy = card.copyText {
                        Text(copy)
                            .font(KriaFont.body(14).weight(.medium))
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 9)
                            .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                    if card.evidence.first != nil || card.costLabel != nil {
                        HStack(spacing: 6) {
                            if let evidence = card.evidence.first {
                                CardPill(text: evidence, systemImage: "waveform.path.ecg", fill: KriaColor.paper)
                            }
                            if let cost = card.costLabel {
                                CardPill(text: "\(cost) to generate", systemImage: nil, fill: KriaColor.butter)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 12)
                .padding(.vertical, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isBusy || isSending)
            .accessibilityLabel(card.accessibilityText)
            .accessibilityHint("Sends this to Kria")
            .accessibilityIdentifier(identifier)

            if isSending {
                ProgressView().controlSize(.small).tint(KriaColor.ink)
                    .frame(width: 44, height: 44)
            } else {
                Button(action: dismiss) {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(KriaColor.mutedInk)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss suggestion: \(card.title)")
                .accessibilityIdentifier("\(identifier)-dismiss")
            }
        }
        .frame(minHeight: 44)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .opacity(isSending ? 0.6 : 1)
    }
}

private struct CardPill: View {
    let text: String
    let systemImage: String?
    let fill: Color

    var body: some View {
        HStack(spacing: 4) {
            if let systemImage { Image(systemName: systemImage).font(.system(size: 10, weight: .semibold)) }
            Text(text).font(KriaFont.body(11).weight(.medium)).lineLimit(2).multilineTextAlignment(.leading)
        }
        .foregroundStyle(KriaColor.ink)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(fill, in: Capsule())
    }
}

/// "No longer fits" in place of the card the server refused, until the refreshed set arrives.
struct SuggestionStaleNoticeView: View {
    let identifier: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 13, weight: .semibold))
            Text(SuggestionStaleNotice.message)
                .font(KriaFont.body(13).weight(.medium))
                .multilineTextAlignment(.leading)
        }
        .foregroundStyle(KriaColor.ink)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .frame(minHeight: 44)
        .background(KriaColor.butterPale, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(identifier)
    }
}

/// The cards in rank order. Used by the chat's ready stage (`idPrefix` "suggestion-card") and the editor's Improve
/// sheet ("improve-card"): the two prefixes keep the identifiers unique while the editor sits above the chat.
struct SuggestionCardList: View {
    let presentation: SuggestionCardsPresentation
    let idPrefix: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if presentation.notice != nil {
                SuggestionStaleNoticeView(identifier: "\(idPrefix)-stale-notice")
            }
            ForEach(Array(presentation.cards.enumerated()), id: \.element.id) { index, card in
                SuggestionCardView(
                    card: card, identifier: "\(idPrefix)-\(index)",
                    isSending: presentation.sendingID == card.id,
                    isBusy: presentation.sendingID != nil,
                    tap: { presentation.tap(card) }, dismiss: { presentation.dismiss(card) }
                )
                .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: presentation.cards.map(\.id))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("\(idPrefix)s")
    }
}

/// The editor's "Improve" sheet. Reads the same set as the chat. A tap closes the sheet and sends the card's prompt
/// as a chat message, so the creator watches it land in the conversation.
struct ImproveSheet: View {
    let presentation: SuggestionCardsPresentation
    let close: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Improve")
                    .font(KriaFont.body(20).weight(.bold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Done", action: close)
                    .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .accessibilityIdentifier("improve-sheet-done")
            }
            .padding(.horizontal, 20)
            .padding(.top, 22)
            .padding(.bottom, 12)

            ScrollView {
                if presentation.isEmpty {
                    VStack(spacing: 14) {
                        Text("Nothing to improve right now. Kria checks again after you save.")
                            .font(KriaFont.body(14))
                            .foregroundStyle(KriaColor.mutedInk)
                            .multilineTextAlignment(.center)
                        Button("Check again", action: presentation.refresh)
                            .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                            .accessibilityIdentifier("improve-sheet-refresh")
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 36)
                    .padding(.horizontal, 20)
                    .accessibilityIdentifier("improve-sheet-empty")
                } else {
                    SuggestionCardList(presentation: presentation, idPrefix: "improve-card")
                        .padding(.horizontal, 20)
                        .padding(.bottom, 24)
                }
            }
        }
        .background(KriaColor.paper)
        .accessibilityIdentifier("improve-sheet")
    }
}

/// The editor's entry to the sheet: a capsule on the preview, drawn while the server's set carries suggestions.
struct ImproveEntryButton: View {
    let count: Int
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "wand.and.stars").font(.system(size: 13, weight: .semibold))
                Text("Improve").font(KriaFont.body(14).weight(.semibold))
                if count > 0 {
                    Text("\(count)")
                        .font(KriaFont.body(12).weight(.bold))
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(KriaColor.paper.opacity(0.25), in: Capsule())
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .background(KriaColor.ink, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(count > 0 ? "Improve, \(count) suggestions" : "Improve")
        .accessibilityIdentifier("native-editor-improve")
        .padding(.leading, 16)
        .padding(.bottom, 14)
    }
}
