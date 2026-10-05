import SwiftUI
import UIKit

/// KRI-443: the live plan feed that replaces "Kria is thinking" and "I'm shaping your video" after Create.
/// It shows each decision (title, clips, captions, music, sound effects, overlays, look) as the render
/// pipeline makes it. Display only: "Change" is a disabled placeholder until the review view (KRI-440/441).
///
/// Surface rules: a material container (never glass on an identified container), Inter only, fills not
/// strokes on leaf surfaces, Reduce Motion = fades only (no shimmer, no glow pulse), Reduce Transparency =
/// solid paper (handled by `kriaFloatingSurface`).
struct PlanBlockFeed: View {
    let feed: PlanBlockFeedState
    /// Stop is offered only while the server can still cancel this turn.
    var canStop = false
    var isStopping = false
    /// A quiet one-liner (for example "This render can't be stopped any more").
    var stopMessage: String?
    var stop: () -> Void = {}

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var expansionOverride: [PlanSectionID: Bool] = [:]
    @State private var reviewingAll = false
    @State private var announced: Set<PlanSectionID> = []
    @State private var announcedSeeded = false

    private var reduceMotion: Bool {
        systemReduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    private var waitingBlocks: [PlanBlock] { feed.blocks.filter { $0.state == .waiting } }
    private var activeBlocks: [PlanBlock] { feed.blocks.filter { $0.state != .waiting } }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            progressBar
            if !activeBlocks.isEmpty {
                VStack(spacing: 8) {
                    ForEach(activeBlocks) { block in
                        row(block)
                            .transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .bottom)))
                    }
                }
            }
            if !waitingBlocks.isEmpty { stillToDecide }
            if feed.isComplete { reviewEntry }
            if let stopMessage {
                Text(stopMessage)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("plan-feed.stop-message")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaFloatingSurface(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.42, dampingFraction: 0.86), value: feed)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: expansionOverride)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: reviewingAll)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("plan-feed")
        .onAppear { seedAnnouncements() }
        .onChange(of: feed) { _, _ in announceNewlyDecided() }
    }

    // MARK: Header

    private var title: String {
        let counts = "\(feed.decidedCount) of \(feed.totalCount) decided"
        return feed.isComplete ? "Plan ready · \(counts)" : "Planning · \(counts)"
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            Text(title)
                .font(KriaFont.body(15).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .accessibilityIdentifier("plan-feed.title")
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            if canStop || isStopping {
                Button(action: stop) {
                    Text(isStopping ? "Stopping…" : "Stop")
                        .font(KriaFont.body(14).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)
                        .background(KriaColor.softZinc, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(isStopping)
                .accessibilityLabel(isStopping ? "Stopping the render" : "Stop the render")
                .accessibilityIdentifier("plan-feed.stop")
            }
        }
    }

    private var progressBar: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(KriaColor.softZinc)
                Capsule().fill(KriaColor.sky)
                    .frame(width: max(feed.progress > 0 ? 8 : 0, proxy.size.width * feed.progress))
            }
        }
        .frame(height: 6)
        .accessibilityElement()
        .accessibilityLabel("Planning progress")
        .accessibilityValue("\(feed.decidedCount) of \(feed.totalCount) decided")
        .accessibilityIdentifier("plan-feed.progress")
    }

    // MARK: Rows

    private func isExpanded(_ block: PlanBlock) -> Bool {
        guard block.state == .decided else { return false }
        if let override = expansionOverride[block.section] { return override }
        if reviewingAll { return true }
        return block.section == feed.newestDecided
    }

    @ViewBuilder private func row(_ block: PlanBlock) -> some View {
        let expanded = isExpanded(block)
        let isNewest = block.state == .decided && block.section == feed.newestDecided
        VStack(alignment: .leading, spacing: 10) {
            Button { toggle(block) } label: { rowHeader(block, expanded: expanded) }
                .buttonStyle(.plain)
                .disabled(block.state != .decided)
                .accessibilityLabel(block.section.label)
                .accessibilityValue(accessibilityValue(block, expanded: expanded))
                .accessibilityHint(block.state == .decided ? (expanded ? "Collapses this decision" : "Expands this decision") : "")
                .accessibilityIdentifier("plan-feed.block.\(block.section.rawValue)")
            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    if block.skipped || !block.displaySummary.isEmpty {
                        Text(block.displaySummary)
                            .font(KriaFont.body(14))
                            .foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("plan-feed.summary.\(block.section.rawValue)")
                    }
                    if let detail = block.detail, !detail.isEmpty {
                        Text(detail)
                            .font(KriaFont.body(13))
                            .foregroundStyle(KriaColor.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("plan-feed.detail.\(block.section.rawValue)")
                    }
                    Text("Change")
                        .font(KriaFont.body(13).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 14)
                        .frame(minHeight: 44)
                        .background(KriaColor.softZinc, in: Capsule())
                        .opacity(0.45)
                        .accessibilityElement()
                        .accessibilityLabel("Change \(block.section.label)")
                        .accessibilityAddTraits([.isButton, .isStaticText])
                        .accessibilityHint("Not available yet")
                        .accessibilityIdentifier("plan-feed.change.\(block.section.rawValue)")
                }
                .padding(.leading, 34)
                .transition(.opacity)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(rowFill(block, glowing: isNewest), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay { if block.state == .deciding && !reduceMotion { PlanShimmer().clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous)).allowsHitTesting(false) } }
        .modifier(PlanGlow(active: isNewest && !feed.isComplete, reduceMotion: reduceMotion))
    }

    private func rowFill(_ block: PlanBlock, glowing: Bool) -> Color {
        switch block.state {
        case .waiting: KriaColor.softZinc
        case .deciding: KriaColor.butter
        case .decided: glowing ? KriaColor.successSoft : KriaColor.softZinc
        }
    }

    private func rowHeader(_ block: PlanBlock, expanded: Bool) -> some View {
        HStack(spacing: 10) {
            leadingIcon(block)
                .frame(width: 24, height: 24)
            Text(block.section.label)
                .font(KriaFont.body(14).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
            if block.state == .decided, !expanded {
                Text(block.displaySummary)
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.mutedInk)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 4)
            if block.state == .deciding {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini).tint(KriaColor.ink)
                    Text("Deciding")
                        .font(KriaFont.body(12).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                }
                .padding(.horizontal, 10)
                .frame(minHeight: 28)
                .background(KriaColor.paper.opacity(0.75), in: Capsule())
            } else if block.state == .decided {
                Image(systemName: "chevron.down")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(KriaColor.mutedInk)
                    .rotationEffect(.degrees(expanded ? 180 : 0))
            }
        }
        .frame(minHeight: 28)
        .contentShape(Rectangle())
    }

    @ViewBuilder private func leadingIcon(_ block: PlanBlock) -> some View {
        if block.state == .decided {
            Image(systemName: block.skipped ? "minus.circle.fill" : "checkmark.circle.fill")
                .font(.system(size: 20))
                .foregroundStyle(block.skipped ? KriaColor.zinc : KriaColor.success)
        } else {
            Image(systemName: block.section.systemImage)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(KriaColor.ink)
        }
    }

    private func accessibilityValue(_ block: PlanBlock, expanded: Bool) -> String {
        switch block.state {
        case .waiting: "waiting"
        case .deciding: "deciding"
        case .decided: "decided, \(expanded ? "expanded" : "collapsed"). \(block.displaySummary)"
        }
    }

    private func toggle(_ block: PlanBlock) {
        guard block.state == .decided else { return }
        expansionOverride[block.section] = !isExpanded(block)
    }

    // MARK: Waiting chips and review

    private var stillToDecide: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Still to decide")
                .font(KriaFont.body(12).weight(.semibold))
                .foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier("plan-feed.waiting-heading")
            PlanChipFlow(spacing: 8) {
                ForEach(waitingBlocks) { block in
                    HStack(spacing: 6) {
                        Image(systemName: block.section.systemImage).font(.system(size: 11, weight: .semibold))
                        Text(block.section.label).font(KriaFont.body(12).weight(.medium))
                    }
                    .foregroundStyle(KriaColor.mutedInk)
                    .padding(.horizontal, 12)
                    .frame(minHeight: 30)
                    .background(KriaColor.softZinc, in: Capsule())
                    .opacity(0.7)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(block.section.label), waiting")
                    .accessibilityIdentifier("plan-feed.chip.\(block.section.rawValue)")
                    .transition(.opacity)
                }
            }
        }
    }

    private var reviewEntry: some View {
        Button {
            reviewingAll.toggle()
            if reviewingAll { expansionOverride = [:] }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: reviewingAll ? "chevron.up" : "list.bullet.rectangle")
                    .font(.system(size: 13, weight: .semibold))
                Text(reviewingAll ? "Collapse plan" : "Review plan")
                    .font(KriaFont.body(15).weight(.semibold))
            }
            .foregroundStyle(KriaColor.ink)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(KriaColor.butter, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("plan-feed.review")
    }

    // MARK: VoiceOver

    private func seedAnnouncements() {
        guard !announcedSeeded else { return }
        announcedSeeded = true
        announced = Set(feed.blocks.filter { $0.state == .decided }.map(\.section))
    }

    private func announceNewlyDecided() {
        for block in feed.blocks where block.state == .decided && !announced.contains(block.section) {
            announced.insert(block.section)
            let spoken = block.skipped ? "\(block.section.label) not used" : "\(block.section.label) decided. \(block.displaySummary)"
            UIAccessibility.post(notification: .announcement, argument: spoken)
        }
    }
}

// MARK: - Motion helpers

/// A soft light sweep across a deciding row. Skipped entirely under Reduce Motion by the caller.
struct PlanShimmer: View {
    @State private var phase: CGFloat = -1

    var body: some View {
        GeometryReader { proxy in
            LinearGradient(
                colors: [KriaColor.paper.opacity(0), KriaColor.paper.opacity(0.55), KriaColor.paper.opacity(0)],
                startPoint: .leading, endPoint: .trailing
            )
            .frame(width: proxy.size.width * 0.5)
            .offset(x: phase * proxy.size.width)
        }
        .onAppear {
            withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) { phase = 1.2 }
        }
    }
}

/// A gentle glow around the newest decided row. A static soft shadow under Reduce Motion (no pulse).
private struct PlanGlow: ViewModifier {
    let active: Bool
    let reduceMotion: Bool
    @State private var pulse = false

    func body(content: Content) -> some View {
        content
            .shadow(color: KriaColor.success.opacity(active ? (pulse ? 0.28 : 0.12) : 0), radius: active ? 12 : 0, y: 2)
            .onAppear { startPulse() }
            .onChange(of: active) { _, _ in startPulse() }
    }

    private func startPulse() {
        guard active, !reduceMotion else { pulse = false; return }
        withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) { pulse = true }
    }
}

/// Wraps its children onto as many lines as the width needs.
struct PlanChipFlow: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = arrange(width: bounds.width, subviews: subviews)
        for (index, frame) in layout.frames.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY), proposal: .unspecified)
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            frames.append(CGRect(origin: CGPoint(x: x, y: y), size: size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), frames)
    }
}
