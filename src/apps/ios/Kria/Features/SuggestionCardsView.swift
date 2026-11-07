import SwiftUI

/// Everything a card surface needs from the chat that owns the set: the cards still worth drawing, the ledger
/// that tracks taps, and what a tap or a dismissal does. Handed to the ready stage and to the editor, so both
/// show the same cards and the same state.
struct SuggestionCardsPresentation {
    let cards: [SuggestionCard]
    /// Returns whether the tap went out. A refused tap (a send already in flight, no set id) changes nothing, so a
    /// surface that closes on a tap must not close on a refusal.
    let tap: (SuggestionCard) -> Bool
    let dismiss: (SuggestionCard) -> Void
    let refresh: () -> Void
    /// The card now sending, so its row can say so and the others wait.
    let sendingID: String?
    /// The image a paid card is making (or made), drawn above the cards until the creator reviews or dismisses it.
    var generated: GeneratedCardResult? = nil
    var reviewGenerated: () -> Void = {}
    var dismissGenerated: () -> Void = {}

    var isEmpty: Bool { cards.isEmpty && generated == nil }
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

/// The cards in rank order. Used by the chat's ready stage (`idPrefix` "suggestion-card") and the editor's Improve
/// sheet ("improve-card"): the two prefixes keep the identifiers unique while the editor sits above the chat.
struct SuggestionCardList: View {
    let presentation: SuggestionCardsPresentation
    let idPrefix: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The paid card the creator tapped and has not yet agreed to pay for. Nothing is sent while this is set.
    @State private var asking: SuggestionCard?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let generated = presentation.generated {
                GeneratedCardRow(
                    result: generated, identifier: "\(idPrefix)-generated",
                    review: presentation.reviewGenerated, dismiss: presentation.dismissGenerated
                )
                .transition(.opacity)
            }
            ForEach(Array(presentation.cards.enumerated()), id: \.element.id) { index, card in
                SuggestionCardView(
                    card: card, identifier: "\(idPrefix)-\(index)",
                    isSending: presentation.sendingID == card.id,
                    isBusy: presentation.sendingID != nil,
                    tap: {
                        if card.needsSpendConsent { asking = card } else { _ = presentation.tap(card) }
                    },
                    dismiss: { presentation.dismiss(card) }
                )
                .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: presentation.cards.map(\.id))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("\(idPrefix)s")
        .sheet(item: $asking) { card in
            SpendConsentSheet(
                card: card,
                confirm: { asking = nil; _ = presentation.tap(card) },
                cancel: { asking = nil }
            )
            .presentationDetents([.height(320)])
            .presentationDragIndicator(.visible)
        }
    }
}

/// "Costs $0.04": a paid card never sends on its first tap. The sheet says what is bought and what is not done
/// until the creator says so; "Not now" sends nothing and leaves the card where it was.
struct SpendConsentSheet: View {
    let card: SuggestionCard
    let confirm: () -> Void
    let cancel: () -> Void

    private var price: String { card.costLabel ?? "a small fee" }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Make this image?")
                .font(KriaFont.body(20).weight(.bold))
                .accessibilityAddTraits(.isHeader)
            Text(card.title)
                .font(KriaFont.body(15).weight(.semibold))
            Text("Costs \(price). Kria makes one image and shows it to you first. Nothing goes in your video until you say so.")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("suggestion-spend-terms")
            Spacer(minLength: 0)
            HStack(spacing: 12) {
                Button("Not now", action: cancel)
                    .buttonStyle(KriaSecondaryButtonStyle(minHeight: 48))
                    .accessibilityIdentifier("suggestion-spend-cancel")
                Button("Make it for \(price)", action: confirm)
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .accessibilityIdentifier("suggestion-spend-confirm")
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 24)
        .padding(.bottom, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.paper)
        .accessibilityIdentifier("suggestion-spend-sheet")
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
        case .making: "Making your image…"
        case .ready: "Your image is ready"
        case .unsure: "Kria isn't sure your image finished"
        case .failed: "Kria couldn't make the image"
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
            if result.phase != .making {
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
