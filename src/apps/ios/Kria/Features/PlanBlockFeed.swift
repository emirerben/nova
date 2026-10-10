import SwiftUI
import UIKit

/// KRI-443 / KRI-450: the live plan feed that replaces "Kria is thinking" and "I'm shaping your video" after
/// Create, restyled to the Paper -B artboards (LV1-B, LV2-B, LV3-B: "color behind the glass").
///
/// It shows each decision (title, clips, captions, music, sound effects, overlays, look, post caption) as the
/// render pipeline makes it: decided sections are 60pt glass rows, the newest decided one is expanded with its
/// structured content, a section being decided is a butter card with a spinner pill, and the rest wait as
/// "Still to decide" chips. A glass bottom panel carries the progress and Stop; once every section is decided
/// the panel offers the ink + butter "Review your video" CTA (when the server speaks contract v2).
///
/// Surface rules: glass only on leaf surfaces (`KriaGlassBackground`, three branches: Reduce Transparency ->
/// solid paper, iOS 26 -> Liquid Glass, else material), never an identifier on or above a glass view, Inter only,
/// fills not strokes, Reduce Motion = fades only and a still spinner.
struct PlanBlockFeed: View {
    let feed: PlanBlockFeedState
    /// Stop is offered only while the server can still cancel this turn.
    var canStop = false
    var isStopping = false
    /// A quiet one-liner (for example "This render can't be stopped any more").
    var stopMessage: String?
    var stop: () -> Void = {}
    /// Opens the Review view (contract v2). nil = the server does not speak v2: no CTA, and the feed keeps its
    /// own "Review plan" expand-all toggle. The section is the card whose Change was tapped (nil = the CTA).
    var openReview: ((PlanSectionID?) -> Void)?

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var expansionOverride: [PlanSectionID: Bool] = [:]
    @State private var reviewingAll = false
    @State private var announced: Set<PlanSectionID> = []
    @State private var announcedSeeded = false

    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }
    private var reviewAvailable: Bool { openReview != nil }

    private var waitingBlocks: [PlanBlock] { feed.blocks.filter { $0.state == .waiting } }
    private var activeBlocks: [PlanBlock] { feed.blocks.filter { $0.state != .waiting } }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            ForEach(activeBlocks) { block in
                card(block)
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .bottom)))
            }
            if !waitingBlocks.isEmpty { stillToDecide }
            bottomPanel
            if let stopMessage {
                Text(stopMessage)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("plan-feed.stop-message")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(alignment: .top) { KriaButterWash().padding(.horizontal, -24).padding(.vertical, -40) }
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.42, dampingFraction: 0.86), value: feed)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: expansionOverride)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: reviewingAll)
        // The identified "plan-feed" element is a plain marker leaf behind the content, not a container around
        // the glass surfaces (an identifier on an ancestor of a `.glassEffect()` view collapses its frame).
        // One atomic snapshot of every displayed section, so UI tests don't sample rows one query at a time.
        .background {
            Color.clear
                .accessibilityElement()
                .accessibilityLabel("Live plan")
                .accessibilityValue(feed.blocks.map { "\($0.section.rawValue)=\($0.state == .decided ? "decided" : $0.state == .deciding ? "deciding" : "waiting")" }.joined(separator: ","))
                .accessibilityIdentifier("plan-feed")
                .allowsHitTesting(false)
        }
        .onAppear { seedAnnouncements() }
        .onChange(of: feed) { _, _ in announceNewlyDecided() }
    }

    // MARK: Header

    private var header: some View {
        VStack(spacing: 2) {
            Text(feed.isComplete ? "Your plan is ready" : "Planning your video")
                .font(KriaFont.body(17).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .accessibilityIdentifier("plan-feed.header")
                .accessibilityAddTraits(.isHeader)
            Text(feed.isComplete
                 ? (reviewAvailable ? "Tap a row for details, or change it" : "Tap a row for details")
                 : "Blocks appear as Kria decides them")
                .font(KriaFont.body(13))
                .foregroundStyle(KriaColor.mutedInk)
        }
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.top, 4)
    }

    // MARK: Cards

    private func isExpanded(_ block: PlanBlock) -> Bool {
        guard block.state == .decided else { return false }
        if let override = expansionOverride[block.section] { return override }
        if reviewingAll { return true }
        return block.section == feed.newestDecided && !block.skipped
    }

    @ViewBuilder private func card(_ block: PlanBlock) -> some View {
        switch block.state {
        case .waiting: EmptyView()
        case .deciding: decidingCard(block)
        case .decided: isExpanded(block) ? AnyView(expandedCard(block)) : AnyView(collapsedRow(block))
        }
    }

    /// 60pt glass row: disc, title, one-line summary, chevron.
    private func collapsedRow(_ block: PlanBlock) -> some View {
        Button { toggle(block) } label: {
            HStack(spacing: 12) {
                KriaCheckDisc(size: 24, dash: block.skipped)
                VStack(alignment: .leading, spacing: 0) {
                    Text(block.section.cardTitle)
                        .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    Text(block.displaySummary)
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        .lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(KriaColor.zinc)
            }
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, minHeight: 60, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(block.section.label)
        .accessibilityValue(accessibilityValue(block, expanded: false))
        .accessibilityHint("Expands this decision")
        .accessibilityIdentifier("plan-feed.block.\(block.section.rawValue)")
        .kriaGlassCard(cornerRadius: 22)
    }

    /// The newest decided card (or any the creator opened): disc, title, Change, then the structured content.
    private func expandedCard(_ block: PlanBlock) -> some View {
        let isTitle = block.section == .title
        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 12) {
                Button { toggle(block) } label: {
                    HStack(alignment: .center, spacing: 12) {
                        KriaCheckDisc(size: 26, dash: block.skipped)
                        VStack(alignment: .leading, spacing: 0) {
                            if isTitle {
                                Text("Title").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                                Text(titleText(block))
                                    .font(KriaFont.body(20).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                    .fixedSize(horizontal: false, vertical: true)
                            } else {
                                Text(block.section.cardTitle)
                                    .font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                if block.skipped || (block.payload != nil && !(block.summary ?? "").isEmpty) {
                                    Text(block.displaySummary)
                                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.7))
                                        .fixedSize(horizontal: false, vertical: true)
                                        .accessibilityIdentifier("plan-feed.summary.\(block.section.rawValue)")
                                }
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(block.section.label)
                .accessibilityValue(accessibilityValue(block, expanded: true))
                .accessibilityHint("Collapses this decision")
                .accessibilityIdentifier("plan-feed.block.\(block.section.rawValue)")
                if !block.skipped, block.section.isScopable { changeButton(block) }
            }
            if !block.skipped { PlanBlockContent(block: block) }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassCard(cornerRadius: 28)
    }

    private func titleText(_ block: PlanBlock) -> String {
        if case .title(let payload) = block.payload { return payload.text }
        return block.displaySummary
    }

    /// "Change": opens the Review view on a v2 server; an inert, dimmed pill before then.
    private func changeButton(_ block: PlanBlock) -> some View {
        let live = reviewAvailable
        return Button { openReview?(block.section) } label: {
            KriaPlanPill(title: "Change", style: .change)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!live)
        .opacity(live ? 1 : 0.45)
        .accessibilityLabel("Change \(block.section.label)")
        .accessibilityHint(live ? "Opens the review" : "Not available yet")
        .accessibilityIdentifier("plan-feed.change.\(block.section.rawValue)")
    }

    /// A section being decided: butter gradient, a spinner pill with a verb, skeleton shapes.
    private func decidingCard(_ block: PlanBlock) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text(block.section.cardTitle)
                    .font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                Spacer(minLength: 8)
                KriaPlanPill(title: "", style: .working(block.section.workingVerb))
            }
            PlanSkeletonContent(section: block.section)
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(block.section.label)
        .accessibilityValue("deciding")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("plan-feed.block.\(block.section.rawValue)")
        .kriaButterCard(cornerRadius: 28)
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

    // MARK: Still to decide

    private var stillToDecide: some View {
        HStack(alignment: .top, spacing: 10) {
            Text("Still to decide")
                .font(KriaFont.body(13).weight(.medium))
                .foregroundStyle(KriaColor.mutedInk)
                .padding(.top, 6)
                .accessibilityIdentifier("plan-feed.waiting-heading")
            PlanChipFlow(spacing: 8) {
                ForEach(waitingBlocks) { block in
                    Text(block.section.cardTitle)
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        .padding(.horizontal, 12)
                        .frame(height: 30)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(block.section.label), waiting")
                        .accessibilityIdentifier("plan-feed.chip.\(block.section.rawValue)")
                        .transition(.opacity)
                }
            }
        }
        .padding(.horizontal, 4)
    }

    // MARK: Bottom panel

    private var statusText: String {
        let counts = "\(feed.decidedCount) of \(feed.totalCount) decided"
        return feed.isComplete ? "Plan ready · \(counts)" : "Planning · \(counts)"
    }

    /// Glass panel: status line, Stop, progress, and (once everything is decided) the Review CTA. Planning shows
    /// no Create button: Create already happened.
    private var bottomPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "sparkle")
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityHidden(true)
                Text(statusText)
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("plan-feed.title")
                    .accessibilityAddTraits(.isHeader)
                if canStop || isStopping {
                    Button(action: stop) {
                        Text(isStopping ? "Stopping…" : "Stop")
                            .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                            .padding(.horizontal, 6)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(isStopping)
                    .accessibilityLabel(isStopping ? "Stopping the render" : "Stop the render")
                    .accessibilityIdentifier("plan-feed-stop")
                }
            }
            KriaProgressTrack(progress: feed.progress)
                .accessibilityElement()
                .accessibilityLabel("Planning progress")
                .accessibilityValue("\(feed.decidedCount) of \(feed.totalCount) decided")
                .accessibilityIdentifier("plan-feed.progress")
            if feed.isComplete && !feed.isCancelled { completionAction }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassPanel(cornerRadius: 32)
    }

    @ViewBuilder private var completionAction: some View {
        if let openReview {
            Button { openReview(nil) } label: { Text("Review your video") }
                .buttonStyle(KriaInkCTAButtonStyle())
                .accessibilityIdentifier("plan-feed-review-cta")
        } else {
            // Older server: no Review view, so the feed keeps its own expand-all toggle.
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
