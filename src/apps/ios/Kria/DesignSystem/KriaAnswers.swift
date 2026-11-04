import SwiftUI

/// Where an answer control is in its life. Selected is the ink fill everywhere; sending shows a spinner while
/// its siblings are disabled (they dim to 40%).
enum KriaAnswerState: Equatable { case idle, selected, sending }

/// A short suggestion (Paper S2.01): fill, 44pt minimum, `KriaType.button`. It is for suggestions only; a
/// question's answers are `KriaChoiceRow`s. A long label wraps and the chip grows, so nothing is cut off.
///
/// `typeMyOwn` is the pencil chip that moves the creator to the composer. `isRecommended` adds a quiet
/// "Recommended" tag beside the label: the butter treatment belongs to the answer block, not the chip.
struct KriaAnswerChip: View {
    let title: String
    var caption: String?
    var state: KriaAnswerState = .idle
    var isRecommended = false
    var typeMyOwn = false
    let action: () -> Void

    var body: some View {
        Button(action: action) { content }
            .buttonStyle(KriaAnswerChipStyle(state: state))
            .disabled(state == .sending)
            .accessibilityLabel(accessibilityTitle)
            .accessibilityAddTraits(state == .selected ? .isSelected : [])
    }

    /// The tag follows the label on the same line when both fit and drops under it when they do not, so a long
    /// label is never squeezed by the tag (Paper wraps the tag the same way).
    @ViewBuilder private var content: some View {
        if isRecommended {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .center, spacing: 8) { lead; label; recommendedTag }
                    .foregroundStyle(foreground)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .center, spacing: 8) { lead; label }
                    recommendedTag
                }
                .foregroundStyle(foreground)
            }
        } else {
            HStack(alignment: .center, spacing: 8) { lead; label }
                .foregroundStyle(foreground)
        }
    }

    @ViewBuilder private var lead: some View {
        if state == .sending {
            KriaSpinner(size: 16)
        } else if state == .selected {
            Image(systemName: "checkmark").font(.system(size: 13, weight: .bold))
        } else if typeMyOwn {
            Image(systemName: "pencil").font(.system(size: 13, weight: .semibold))
        }
    }

    private var label: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).kriaType(.button)
            if let caption {
                Text(caption).kriaType(.caption)
                    .foregroundStyle(state == .selected ? KriaColor.paper.opacity(0.8) : KriaColor.mutedInk)
            }
        }
        .multilineTextAlignment(.leading)
    }

    private var recommendedTag: some View {
        Text("Recommended").kriaType(.caption)
            .foregroundStyle(state == .selected ? KriaColor.paper.opacity(0.8) : KriaColor.mutedInk)
    }

    private var foreground: Color {
        typeMyOwn && state == .idle ? KriaColor.mutedInk : (state == .selected ? KriaColor.paper : KriaColor.ink)
    }

    private var accessibilityTitle: String {
        [title, caption, isRecommended ? "recommended" : nil].compactMap { $0 }.joined(separator: ", ")
    }
}

private struct KriaAnswerChipStyle: ButtonStyle {
    let state: KriaAnswerState
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 18).padding(.vertical, 8)
            .frame(minHeight: 44)
            .background(fill(pressed: configuration.isPressed), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            .opacity(isEnabled || state == .sending ? 1 : 0.4)
    }

    private func fill(pressed: Bool) -> Color {
        state == .selected ? KriaColor.ink : (pressed ? KriaColor.fillPressed : KriaColor.fill)
    }
}

/// The one answer block (Paper S2.02): full width, fill, radius 16. Every question stacks its options as
/// blocks, one under the other. The text wraps and the block grows in height; nothing is truncated.
///
/// The recommended block is filled with butter and carries a sentence-case "Recommended" pill beside its
/// title; its accessibility label adds ", recommended". Butter never sits beside red, so a screen that shows
/// a failure notice next to the options passes `isRecommended: false`. `typeMyOwn` is the pencil row.
struct KriaChoiceRow: View {
    let title: String
    var detail: String?
    var state: KriaAnswerState = .idle
    var isRecommended = false
    var typeMyOwn = false
    let action: () -> Void

    var body: some View {
        Button(action: action) { content }
            .buttonStyle(KriaChoiceRowStyle(state: state, isRecommended: isRecommended))
            .disabled(state == .sending)
            .accessibilityLabel([title, detail, isRecommended ? "recommended" : nil].compactMap { $0 }.joined(separator: ", "))
            .accessibilityAddTraits(state == .selected ? .isSelected : [])
    }

    private var content: some View {
        HStack(alignment: .center, spacing: 12) {
            if typeMyOwn { Image(systemName: "pencil").font(.system(size: 15, weight: .semibold)) }
            VStack(alignment: .leading, spacing: 2) {
                if isRecommended && state != .selected {
                    // Title and pill share a line when they fit; otherwise the pill drops under the title so the
                    // title keeps the whole width (it is never squeezed to a sliver at large text sizes).
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .center, spacing: 8) {
                            Text(title).kriaType(.button)
                            recommendedPill
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(title).kriaType(.button)
                            recommendedPill
                        }
                    }
                } else {
                    Text(title).kriaType(.button)
                }
                if let detail {
                    Text(detail).kriaType(.label)
                        .foregroundStyle(state == .selected ? KriaColor.paper.opacity(0.8) : KriaColor.mutedInk)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .multilineTextAlignment(.leading)
            if state == .selected {
                Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 20, height: 20).background(KriaColor.paper, in: Circle())
            }
        }
        // An overlay, so entering the sending state never changes the row's height: a taller or shorter row at the end of
        // the transcript made the conversation scroll re-measure without end while the send was pending.
        .overlay(alignment: .trailing) {
            if state == .sending { KriaSpinner(size: 16, tint: KriaColor.ink) }
        }
        .foregroundStyle(foreground)
    }

    private var recommendedPill: some View {
        Text("Recommended").kriaType(.caption).fontWeight(.semibold).fixedSize()
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var foreground: Color {
        if state == .selected { return KriaColor.paper }
        return typeMyOwn ? KriaColor.mutedInk : KriaColor.ink
    }
}

private struct KriaChoiceRowStyle: ButtonStyle {
    let state: KriaAnswerState
    let isRecommended: Bool
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 16).padding(.vertical, 14)
            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            .background(fill(pressed: configuration.isPressed), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .opacity(isEnabled || state == .sending ? 1 : 0.4)
    }

    private func fill(pressed: Bool) -> Color {
        if state == .selected { return KriaColor.ink }
        if isRecommended { return pressed ? KriaColor.butterPressed : KriaColor.butter }
        return pressed ? KriaColor.fillPressed : KriaColor.fill
    }
}

/// One answer in a `KriaAnswerStack`. `identifier` is the accessibility id UI tests find the block by.
struct KriaAnswerOption: Identifiable, Equatable {
    let id: String
    let title: String
    var detail: String? = nil
    var isRecommended = false
    var identifier: String? = nil
}

/// A question's answers as full-width blocks, one under the other (Paper S2.03, S2.05, S2.06), then the optional
/// "Type my own" row and the centered "Let Kria decide" button.
///
/// The stack stays active until the send succeeds. The tapped block shows a spinner while the others dim; when a
/// send that this stack started ends and the question is still open, the send failed: the blocks re-enable, the
/// recommended block drops to the neutral fill (butter never sits beside red) and a failure line is announced.
/// `isLocked` dims every block while the creator types their own answer.
struct KriaAnswerStack: View {
    let options: [KriaAnswerOption]
    /// The block that was tapped and is waiting for a second answer to go out with it (ink fill).
    var heldID: String?
    /// A send is in flight (this question's or anything else's); blocks are disabled until it ends.
    var isSending = false
    /// The block whose message is already in the transcript, when the question can tell which one it was.
    var pendingID: String?
    var isLocked = false
    var typeMyOwn: (() -> Void)?
    var decide: (() -> Void)?
    let select: (KriaAnswerOption) -> Void

    @State private var tappedID: String?
    @State private var sendFailed = false

    private let failureText = "Couldn’t send your answer. Tap it again."

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(options) { option in
                KriaChoiceRow(title: option.title, detail: option.detail, state: state(of: option),
                              isRecommended: option.isRecommended && !sendFailed) {
                    tappedID = option.id
                    sendFailed = false
                    select(option)
                }
                .disabled(isSending || isLocked || (heldID != nil && option.id != heldID))
                .modifier(OptionIdentifier(identifier: option.identifier))
            }
            if let typeMyOwn {
                KriaChoiceRow(title: "Type my own", typeMyOwn: true, action: typeMyOwn)
                    .disabled(isSending || isLocked || heldID != nil)
                    .accessibilityIdentifier("answer-type-my-own")
            }
            if sendFailed {
                KriaInlineFailure(text: failureText, announces: false).accessibilityIdentifier("answer-send-failed")
            }
            if let decide {
                Button("Let Kria decide", action: decide)
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
                    .disabled(isSending || isLocked)
                    .accessibilityIdentifier("answer-let-kria-decide")
            }
        }
        .onChange(of: isSending) { wasSending, nowSending in
            if wasSending, !nowSending, tappedID != nil {
                sendFailed = true
                tappedID = nil
                // Announced here, once, when the send fails: the failure line below only redraws on a re-render.
                KriaAnnouncements.post(failureText, highPriority: true, repeatsAfter: 0)
            }
        }
    }

    private func state(of option: KriaAnswerOption) -> KriaAnswerState {
        if option.id == heldID { return .selected }
        if isSending, option.id == (pendingID ?? tappedID) { return .sending }
        return .idle
    }

    private struct OptionIdentifier: ViewModifier {
        let identifier: String?
        func body(content: Content) -> some View {
            if let identifier { content.accessibilityIdentifier(identifier) } else { content }
        }
    }
}

/// A failure line: a small alert glyph and the reason in `failureText`, announced once when it appears. A
/// transcript re-render that redraws the same line does not announce it again; the caller that owns the failure
/// passes `announces: false` when it announces the event itself.
struct KriaInlineFailure: View {
    let text: String
    var announces = true

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "exclamationmark.circle").font(.system(size: 13, weight: .semibold))
                .accessibilityHidden(true)
            Text(text).kriaType(.label).fixedSize(horizontal: false, vertical: true)
        }
        .foregroundStyle(KriaColor.failureText)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
        .onAppear { if announces { KriaAnnouncements.post(text) } }
    }
}

/// Spoken announcements for failures and notices, once per distinct text. A view that is rebuilt (a transcript
/// re-render, a lazy row scrolling back in) draws the same failure again; without this it would be read out every
/// time. The same text is spoken again only after `repeatsAfter` seconds, so a real second failure still is.
@MainActor enum KriaAnnouncements {
    private static var spoken: [String: Date] = [:]

    static func post(_ text: String, highPriority: Bool = false, repeatsAfter window: TimeInterval = 30) {
        let now = Date()
        spoken = spoken.filter { now.timeIntervalSince($0.value) < 300 }
        if window > 0, let last = spoken[text], now.timeIntervalSince(last) < window { return }
        spoken[text] = now
        var attributed = AttributedString(text)
        if highPriority { attributed.accessibilitySpeechAnnouncementPriority = .high }
        AccessibilityNotification.Announcement(attributed).post()
    }
}

/// What Kria did for the creator, one line (Paper S2.08): the one green check disc and an ink label that
/// grows with its text.
struct KriaAnsweredReceipt: View {
    let text: String

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            KriaCheckDisc(size: 20)
            Text(text).kriaType(.label).foregroundStyle(KriaColor.ink).fixedSize(horizontal: false, vertical: true)
        }
        .frame(minHeight: 44, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// What the creator chose (Paper S2.08): a small "You chose" caption over a right-aligned ink capsule.
struct KriaYouChose: View {
    let text: String

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Text("You chose").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
            Text(text).kriaType(.button).foregroundStyle(KriaColor.paper)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 18).padding(.vertical, 12)
                .background(KriaColor.ink, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("You chose: \(text)")
    }
}

/// The quiet line a question leaves behind when a newer one replaced it (Paper S2.08).
struct KriaSupersededLine: View {
    var body: some View {
        Text("Replaced by a newer question.")
            .kriaType(.caption).foregroundStyle(KriaColor.zinc)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The shell every rich question card shares (Paper S2.10 to S2.13): fill, radius 20, padding 16, an optional
/// title and one helper line, then the card's own control.
struct KriaQuestionCard<Content: View>: View {
    var title: String?
    var helper: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if title != nil || helper != nil {
                VStack(alignment: .leading, spacing: 2) {
                    if let title {
                        Text(title).kriaType(.button).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityAddTraits(.isHeader)
                    }
                    if let helper {
                        Text(helper).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

/// Lays its children out left to right and starts a new line when the next one does not fit. Each child is offered
/// the container's width, so a chip whose label is wider than a whole line wraps its text inside the chip instead
/// of running off the edge.
struct KriaWrapLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = arrange(width: bounds.width, subviews: subviews)
        for (index, frame) in layout.frames.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                                  proposal: ProposedViewSize(width: frame.width, height: frame.height))
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            var size = subview.sizeThatFits(.unspecified)
            if width.isFinite, size.width > width { size = subview.sizeThatFits(ProposedViewSize(width: width, height: nil)) }
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            frames.append(CGRect(origin: CGPoint(x: x, y: y), size: size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), frames)
    }
}
