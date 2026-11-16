import SwiftUI
import UIKit

/// KRI-443 / KRI-450: the live plan feed that replaces "Kria is thinking" and "I'm shaping your video" after
/// Create, restyled to the Paper -B artboards (LV1-B, LV2-B, LV3-B: "color behind the glass").
///
/// It shows each decision (title, clips, captions, music, sound effects, overlays, look, post caption) as the
/// render pipeline makes it: decided sections are 60pt glass rows, the newest decided one is expanded with its
/// structured content, a section being decided is a softly pulsing butter card with a spinner pill, and the rest
/// wait as "Coming up" chips. Each section enters on its own (about 80 ms apart when a burst arrives). A
/// glass bottom panel carries the progress and Stop; once every section is decided it reads "Making your video"
/// with an indeterminate track (or the device's real build progress), so the feed never sits still.
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
    /// Opens the Review view (contract v2) on the card whose Change was tapped. nil = the server does not speak
    /// v2: Change is inert and the feed keeps its own "Review plan" expand-all toggle.
    var openReview: ((PlanSectionID) -> Void)?
    /// Planning until the server's plan is complete, then rendering. Computed by the host from the server's feed,
    /// because an iPhone build paces `feed` to the device and would never read as complete while it builds.
    var phase: PlanFeedPhase = .planning
    /// Where this iPhone's own build has got to, when the render runs on the device. nil = cloud render.
    var buildStage: DeviceBuildStage?

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var expansionOverride: [PlanSectionID: Bool] = [:]
    @State private var reviewingAll = false
    @State private var announced: Set<PlanSectionID> = []
    @State private var announcedSeeded = false
    @State private var shown = PlanBlockFeedState.empty

    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }
    private var reviewAvailable: Bool { openReview != nil }

    private static let revealInterval = Duration.milliseconds(80)

    /// What is on screen: `feed` revealed one section at a time. Everything below reads this, not `feed`.
    private var display: PlanBlockFeedState { shown.isEmpty ? feed : shown }
    /// Chips for sections the render has not filled in yet. The creator already approved the plan, so these are
    /// "coming up", not open decisions. Sound effects only exist when asked for, so a waiting one is never shown
    /// (it appears as a card if it is actually decided). KRI-1290.
    private var waitingBlocks: [PlanBlock] { display.blocks.filter { $0.state == .waiting && $0.section != .sfx } }
    private var activeBlocks: [PlanBlock] { display.blocks.filter { $0.state != .waiting } }
    /// "Making your video" only once the last section has entered.
    private var effectivePhase: PlanFeedPhase { display == feed ? phase : .planning }

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
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.42, dampingFraction: 0.86), value: display)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: expansionOverride)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: reviewingAll)
        // The identified "plan-feed" element is a plain marker leaf behind the content, not a container around
        // the glass surfaces (an identifier on an ancestor of a `.glassEffect()` view collapses its frame).
        // One atomic snapshot of every displayed section, so UI tests don't sample rows one query at a time.
        .background {
            Color.clear
                .accessibilityElement()
                .accessibilityLabel("Live plan")
                .accessibilityValue(display.blocks.map { "\($0.section.rawValue)=\($0.state == .decided ? "decided" : $0.state == .deciding ? "deciding" : "waiting")" }.joined(separator: ","))
                .accessibilityIdentifier("plan-feed")
                .allowsHitTesting(false)
        }
        .onAppear { seedAnnouncements() }
        .onChange(of: display) { _, _ in announceNewlyDecided() }
        .task(id: feed) { await reveal() }
    }

    /// Walks `shown` to `feed` one section at a time. A feed already on screen (re-opened chat) or a new job
    /// jumps straight there; `task(id:)` restarts the walk from wherever it stopped when `feed` changes again.
    private func reveal() async {
        while shown != feed {
            let (next, isVisible) = feed.step(after: shown)
            shown = next
            guard next != feed, isVisible else { continue }
            do { try await Task.sleep(for: Self.revealInterval) } catch { return }
        }
    }

    // MARK: Header

    private var header: some View {
        VStack(spacing: 2) {
            KriaShimmerText(
                effectivePhase == .planning ? String(localized: "Making your video") : String(localized: "Your plan is ready"),
                font: KriaFont.body(17).weight(.semibold), color: KriaColor.ink,
                isShimmering: effectivePhase == .planning
            )
            .accessibilityIdentifier("plan-feed.header")
            .accessibilityAddTraits(.isHeader)
            if effectivePhase != .planning {
                Text(reviewAvailable ? String(localized: "Tap a row for details, or change it") : String(localized: "Tap a row for details"))
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.mutedInk)
            }
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
        return block.section == display.newestDecided && !block.skipped
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
            KriaPlanPill(title: String(localized: "Change"), style: .change)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!live)
        .opacity(live ? 1 : 0.45)
        .accessibilityLabel("Change \(block.section.label)")
        .accessibilityHint(live ? String(localized: "Opens the review") : String(localized: "Not available yet"))
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
        .kriaSoftPulse()
    }

    private func accessibilityValue(_ block: PlanBlock, expanded: Bool) -> String {
        switch block.state {
        case .waiting: String(localized: "waiting")
        case .deciding: String(localized: "deciding")
        case .decided: expanded ? String(localized: "decided, expanded. \(block.displaySummary)") : String(localized: "decided, collapsed. \(block.displaySummary)")
        }
    }

    private func toggle(_ block: PlanBlock) {
        guard block.state == .decided else { return }
        expansionOverride[block.section] = !isExpanded(block)
    }

    // MARK: Still to decide

    private var stillToDecide: some View {
        HStack(alignment: .top, spacing: 10) {
            Text("Coming up")
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

    private var countsText: String { String(localized: "\(display.decidedCount) of \(display.totalCount) decided") }

    /// Glass panel: status line, Stop, progress, and (on an older server) the expand-all toggle. Planning shows no
    /// Create button: Create already happened.
    private var bottomPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "sparkle")
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityHidden(true)
                statusLine
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("plan-feed.title")
                    .accessibilityAddTraits(.isHeader)
                if canStop || isStopping {
                    Button(action: stop) {
                        Text(isStopping ? String(localized: "Stopping…") : String(localized: "Stop"))
                            .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                            .padding(.horizontal, 6)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(isStopping)
                    .accessibilityLabel(isStopping ? String(localized: "Stopping the render") : String(localized: "Stop the render"))
                    .accessibilityIdentifier("plan-feed-stop")
                }
            }
            progressTrack
            if effectivePhase != .planning && !display.isCancelled && !reviewAvailable { completionAction }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassPanel(cornerRadius: 32)
    }

    @ViewBuilder private var statusLine: some View {
        let font = KriaFont.body(15).weight(.semibold)
        switch effectivePhase {
        case .planning: Text("Planning · \(countsText)").font(font).foregroundStyle(KriaColor.ink)
        case .rendering: KriaShimmerText(String(localized: "Making your video"), font: font, color: KriaColor.ink)
        case .ready: Text("Plan ready · \(countsText)").font(font).foregroundStyle(KriaColor.ink)
        }
    }

    /// The device's real export progress while it composes; otherwise indeterminate while the video is made.
    private var renderFraction: Double? {
        if case .rendering(let fraction)? = buildStage { return fraction }
        return nil
    }

    @ViewBuilder private var progressTrack: some View {
        if effectivePhase == .rendering {
            KriaProgressTrack(progress: renderFraction)
                .accessibilityElement()
                .accessibilityLabel("Making your video")
                .accessibilityValue(renderFraction.map { String(localized: "\(Int(($0 * 100).rounded())) percent") } ?? String(localized: "in progress"))
                .accessibilityIdentifier("plan-feed.progress")
        } else {
            KriaProgressTrack(progress: display.progress)
                .accessibilityElement()
                .accessibilityLabel("Planning progress")
                .accessibilityValue(countsText)
                .accessibilityIdentifier("plan-feed.progress")
        }
    }

    /// Older server: no Review view, so the feed keeps its own expand-all toggle.
    private var completionAction: some View {
        Button {
            reviewingAll.toggle()
            if reviewingAll { expansionOverride = [:] }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: reviewingAll ? "chevron.up" : "list.bullet.rectangle")
                    .font(.system(size: 13, weight: .semibold))
                Text(reviewingAll ? String(localized: "Collapse plan") : String(localized: "Review plan"))
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
        announced = Set(display.blocks.filter { $0.state == .decided }.map(\.section))
    }

    private func announceNewlyDecided() {
        for block in display.blocks where block.state == .decided && !announced.contains(block.section) {
            announced.insert(block.section)
            let spoken = block.skipped ? String(localized: "\(block.section.label) not used") : String(localized: "\(block.section.label) decided. \(block.displaySummary)")
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
            subviews[index].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY), proposal: ProposedViewSize(frame.size))
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            // A chip wider than the row wraps its label instead of pushing the whole row past the screen.
            let size = subview.sizeThatFits(ProposedViewSize(width: width.isFinite ? width : nil, height: nil))
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            frames.append(CGRect(origin: CGPoint(x: x, y: y), size: size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), frames)
    }
}
