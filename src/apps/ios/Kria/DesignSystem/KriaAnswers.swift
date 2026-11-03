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

    private var content: some View {
        HStack(alignment: .center, spacing: 8) {
            if state == .sending {
                KriaSpinner(size: 16)
            } else if state == .selected {
                Image(systemName: "checkmark").font(.system(size: 13, weight: .bold))
            } else if typeMyOwn {
                Image(systemName: "pencil").font(.system(size: 13, weight: .semibold))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).kriaType(.button)
                if let caption {
                    Text(caption).kriaType(.caption)
                        .foregroundStyle(state == .selected ? KriaColor.paper.opacity(0.8) : KriaColor.mutedInk)
                }
            }
            .multilineTextAlignment(.leading)
            if isRecommended {
                Text("Recommended").kriaType(.caption).fixedSize()
                    .foregroundStyle(state == .selected ? KriaColor.paper.opacity(0.8) : KriaColor.mutedInk)
            }
        }
        .foregroundStyle(typeMyOwn && state == .idle ? KriaColor.mutedInk : (state == .selected ? KriaColor.paper : KriaColor.ink))
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
                    HStack(alignment: .center, spacing: 8) {
                        Text(title).kriaType(.button)
                        recommendedPill
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
            switch state {
            case .sending: KriaSpinner(size: 16, tint: KriaColor.ink)
            case .selected:
                Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 20, height: 20).background(KriaColor.paper, in: Circle())
            case .idle: EmptyView()
            }
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
