import SwiftUI
import UIKit

/// The panel a tap picked while the session has no selection of its own for it (an empty panel). It is valid only
/// while the session selection is still the one it had right after the tap.
struct NativeSplitPanelPick: Equatable {
    let sectionID: String
    let panel: Int
    let selection: EditorSelection?
}

/// A ghost tapped in the preview asks the Split tool to open its picker aimed at one panel. `token` makes a repeat
/// tap on the same panel a new request.
struct NativeSplitAddRequest: Equatable {
    let sectionID: String
    let panel: Int
    let token = UUID()
}

/// How the preview tells the editor a ghost was tapped (open the Split tool aimed at that panel). The editor sets it
/// in the environment; without it the preview draws no ghosts, so a host that cannot open the tool shows no dead taps.
struct NativeSplitGhostTap {
    let open: (_ sectionID: String, _ panel: Int) -> Void
}

private struct NativeSplitGhostTapKey: EnvironmentKey {
    nonisolated(unsafe) static let defaultValue: NativeSplitGhostTap? = nil
}

extension EnvironmentValues {
    var nativeSplitGhostTap: NativeSplitGhostTap? {
        get { self[NativeSplitGhostTapKey.self] }
        set { self[NativeSplitGhostTapKey.self] = newValue }
    }
}

/// What a panel holds at the playhead.
enum NativeSplitPlayback {
    enum PanelState { case playing, gap, empty }

    /// `empty`: the panel has no footage at all. `gap`: it has footage, but none at the playhead (before, between or
    /// after its clips). `playing`: a clip of it covers the playhead.
    @MainActor static func state(session: NativeEditorSession, context: NativeSplitContext, panel: Int) -> PanelState {
        let local = min(max(0, session.currentTime - context.sectionStart), context.section.durationS - 0.000001)
        let members = session.document.clips.filter {
            !$0.removed && $0.splitSectionID == context.sectionID && $0.splitPanelIndex == panel
        }
        guard !members.isEmpty else { return .empty }
        let covers = members.contains { $0.splitOffsetS <= local && local < $0.splitOffsetS + ($0.durationS ?? 0) }
        return covers ? .playing : .gap
    }
}

/// Preview controls for the split section under the playhead. The preview is the control surface: a Sky tint on the
/// selected panel, a white line between panels that drags to resize, and a round swap button on that line.
/// Everything is laid out from the panels drawn at the playhead (`NativeSplitContext.activePanels`), so a collapse
/// section's controls follow the picture when a panel ends.
/// A panel with no footage, or a gap at the playhead, wears a ghost: a "+" on a Sky-tinted fill that opens the Split
/// tool's picker aimed at that panel. A panel the layout draws gets the ghost across its own rect. A collapse section
/// does not draw a panel that has no footage now, so its ghost sits in the rect the panel would have in the full
/// layout when no panel is drawn at all, and otherwise as a compact pill stacked at the bottom edge, so the drawn
/// panels' rects stay exact. The pill is an editing aid on the preview only (nothing of it reaches the export) and
/// covers a small part of the bottom of the drawn footage. Only the ghost's own "+" block takes touches; the rest of its
/// fill, the drawn panels and the tint never do.
/// The tint and panel elements never take touches. Only the divider bands and the ghost blocks do, and this view sits above the canvas
/// gesture surface so the canvas move gesture and its empty tap (fullscreen) never see a touch that starts on a band.
struct NativeSplitPreviewOverlay: View {
    @ObservedObject var session: NativeEditorSession
    @Binding var pick: NativeSplitPanelPick?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.nativeSplitGhostTap) private var ghostTap

    static let canvasSpace = "native-split-preview-canvas"

    var body: some View {
        GeometryReader { proxy in
            if !session.isPlaying, !session.isShowingPreviousPreview, let context = session.splitContextAtPlayhead,
               proxy.size.width > 0, proxy.size.height > 0 {
                content(context: context, size: proxy.size)
            }
        }
        .coordinateSpace(name: Self.canvasSpace)
    }

    private func content(context: NativeSplitContext, size: CGSize) -> some View {
        let section = context.section
        let shown = NativeSplitPanelLayout.normalizedActive(context.activePanels, count: section.panelCount)
        let rects = NativeSplitPanelLayout.panelRects(weights: section.panelWeights, gutter: section.gutter, active: shown)
        let ys = NativeSplitPanelLayout.dividerYs(weights: section.panelWeights, gutter: section.gutter, active: shown)
        let selected = Self.selectedPanel(session: session, context: context, pick: pick)
        return ZStack(alignment: .topLeading) {
            ForEach(Array(zip(shown, rects)), id: \.0) { index, unit in
                let rect = Self.pixelRect(unit, in: size)
                ZStack {
                    Rectangle().fill(KriaColor.sky.opacity(selected == index ? 0.42 : 0))
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: selected)
                }
                .frame(width: rect.width, height: rect.height)
                .offset(x: rect.minX, y: rect.minY)
                .allowsHitTesting(false)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(NativeSplitScreenRows.panelTitle(index: index, count: section.panelCount)) panel")
                .accessibilityAddTraits(selected == index ? [.isButton, .isSelected] : .isButton)
                .accessibilityAction { Self.choose(panel: index, context: context, session: session, pick: $pick) }
                .accessibilityIdentifier("native-editor-split-panel-\(index)")
            }
            if let ghostTap, session.canEditSplitScreen {
                ghosts(context: context, shown: shown, rects: rects, size: size, open: ghostTap)
            }
            if session.canEditSplitScreen {
                ForEach(Array(ys.enumerated()), id: \.offset) { position, fraction in
                    NativeSplitDividerHandle(session: session, pick: $pick, section: section, sectionID: context.sectionID,
                                             upper: shown[position], lower: shown[position + 1],
                                             y: CGFloat(fraction) * size.height, canvas: size)
                }
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
    }

    /// Hard cut like the layout itself: a ghost appears and goes on the frame its panel's state changes.
    @ViewBuilder private func ghosts(context: NativeSplitContext, shown: [Int], rects: [CGRect], size: CGSize,
                                     open: NativeSplitGhostTap) -> some View {
        let section = context.section
        let states = (0..<section.panelCount).map { ($0, NativeSplitPlayback.state(session: session, context: context, panel: $0)) }
        let wanted = states.filter { $0.1 != .playing && session.canAddToSplitPanel(sectionID: context.sectionID, panel: $0.0) }
        let full = NativeSplitPanelLayout.panelRects(weights: section.panelWeights, gutter: section.gutter)
        // Panels the layout does not draw while others are: pills at the bottom edge, last panel lowest.
        let pills = wanted.filter { !shown.contains($0.0) }.map(\.0)
        ForEach(wanted, id: \.0) { panel, state in
            let title = NativeSplitScreenRows.panelTitle(index: panel, count: section.panelCount)
            let text = state == .gap ? "Add footage" : "Add photo or video"
            let tap = {
                Self.choose(panel: panel, context: context, session: session, pick: $pick)
                open.open(context.sectionID, panel)
            }
            if let position = shown.firstIndex(of: panel) {
                NativeSplitGhost(panel: panel, title: title, text: text, style: .panel(Self.pixelRect(rects[position], in: size)), action: tap)
            } else if shown.isEmpty, full.indices.contains(panel) {
                NativeSplitGhost(panel: panel, title: title, text: text, style: .panel(Self.pixelRect(full[panel], in: size)), action: tap)
            } else if let slot = pills.firstIndex(of: panel) {
                NativeSplitGhost(panel: panel, title: title, text: text,
                                 style: .pill(centerY: size.height - 8 - 22 - CGFloat(pills.count - 1 - slot) * 50, width: size.width), action: tap)
            }
        }
    }

    static func pixelRect(_ unit: CGRect, in size: CGSize) -> CGRect {
        CGRect(x: unit.minX * size.width, y: unit.minY * size.height,
               width: unit.width * size.width, height: unit.height * size.height)
    }

    /// The drawn panel under a canvas point, or nil in a gutter, a panel that has ended, or outside the canvas.
    static func panel(at point: CGPoint, in size: CGSize, context: NativeSplitContext) -> Int? {
        let section = context.section
        let shown = NativeSplitPanelLayout.normalizedActive(context.activePanels, count: section.panelCount)
        let rects = NativeSplitPanelLayout.panelRects(weights: section.panelWeights, gutter: section.gutter, active: shown)
        return zip(shown, rects).first { pixelRect($0.1, in: size).contains(point) }?.0
    }

    static func selectedPanel(session: NativeEditorSession, context: NativeSplitContext, pick: NativeSplitPanelPick?) -> Int? {
        if let pick, pick.sectionID == context.sectionID, pick.selection == session.selection,
           context.emptyPanels.contains(pick.panel) { return pick.panel }
        guard let selection = session.selection, selection.kind == .clip,
              let slot = session.document.clips.first(where: { $0.id == selection.id }),
              slot.splitSectionID == context.sectionID else { return nil }
        return slot.splitPanelIndex
    }

    /// Selects a panel through the session and remembers it locally, so an empty panel still shows as chosen.
    static func choose(panel: Int, context: NativeSplitContext, session: NativeEditorSession,
                       pick: Binding<NativeSplitPanelPick?>) {
        session.selectSplitPanel(sectionID: context.sectionID, panel: panel)
        pick.wrappedValue = NativeSplitPanelPick(sectionID: context.sectionID, panel: panel, selection: session.selection)
    }
}

/// One boundary between two panels: the 3pt line, the swap button and the drag band around them.
private struct NativeSplitDividerHandle: View {
    @ObservedObject var session: NativeEditorSession
    @Binding var pick: NativeSplitPanelPick?
    let section: EditorSplitScreenSection
    let sectionID: String
    /// The two drawn panels this line sits between. In a collapse section they need not be consecutive indexes.
    let upper: Int
    let lower: Int
    let y: CGFloat
    let canvas: CGSize

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @GestureState private var touching = false
    @State private var dragging = false
    @State private var wasEqual = false
    @State private var haptic = UIImpactFeedbackGenerator(style: .light)

    private let band: CGFloat = 44
    private let swapSize: CGFloat = 34
    private let moveThreshold: CGFloat = 4
    private let tapSlop: CGFloat = 10

    private var weights: (above: Double, below: Double)? {
        guard section.panelWeights.indices.contains(upper), section.panelWeights.indices.contains(lower) else { return nil }
        return (section.panelWeights[upper], section.panelWeights[lower])
    }

    private var ratioText: String {
        guard let pair = weights, pair.above + pair.below > 0 else { return "" }
        let upper = Int((pair.above / (pair.above + pair.below) * 100).rounded())
        return "\(upper) / \(100 - upper)"
    }

    private var isEqual: Bool {
        guard let pair = weights else { return false }
        return abs(pair.above - pair.below) <= 1e-6 * max(pair.above, pair.below, 1)
    }

    private var upperTitle: String {
        NativeSplitScreenRows.panelTitle(index: upper, count: section.panelCount).lowercased()
    }

    var body: some View {
        ZStack {
            Rectangle().fill(.white).frame(height: 3)
                .shadow(color: .black.opacity(0.16), radius: 1)
            Circle().fill(.white).frame(width: swapSize, height: swapSize)
                .shadow(color: KriaColor.ink.opacity(0.28), radius: 5, y: 2)
                .overlay {
                    Image(systemName: "arrow.up.arrow.down")
                        .font(.system(size: 14, weight: .semibold)).foregroundStyle(KriaColor.ink)
                }
                .allowsHitTesting(false)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Swap panels")
                .accessibilityAddTraits(.isButton)
                .accessibilityAction { swap() }
                .accessibilityIdentifier("native-editor-split-swap-\(upper)")
        }
        .frame(width: canvas.width, height: band)
        .overlay(alignment: .top) {
            if dragging {
                Text(ratioText)
                    .font(KriaFont.body(12).weight(.semibold)).monospacedDigit()
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10).frame(minHeight: 24)
                    .background(KriaColor.ink, in: Capsule())
                    .offset(y: y < 56 ? band + 6 : -30)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
        .contentShape(Rectangle())
        .gesture(dragGesture)
        .position(x: canvas.width / 2, y: y)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Panel divider")
        .accessibilityValue(ratioText)
        .accessibilityIdentifier("native-editor-split-divider-\(upper)")
        .accessibilityAction(named: "Make \(upperTitle) panel taller") { nudge(0.05) }
        .accessibilityAction(named: "Make \(upperTitle) panel shorter") { nudge(-0.05) }
        .onChange(of: touching) { _, now in
            if !now, dragging { cancel() }
        }
    }

    private var dragGesture: some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .named(NativeSplitPreviewOverlay.canvasSpace))
            .updating($touching) { _, state, _ in state = true }
            .onChanged { value in
                if !dragging {
                    guard hypot(value.translation.width, value.translation.height) >= moveThreshold else { return }
                    dragging = true
                    wasEqual = isEqual
                    haptic.prepare()
                    session.beginSplitDividerDrag(sectionID: sectionID, between: upper, and: lower)
                }
                session.updateSplitDividerDrag(deltaFraction: Double(value.translation.height / max(canvas.height, 1)))
                let equal = isEqual
                if equal, !wasEqual { haptic.impactOccurred() }
                wasEqual = equal
            }
            .onEnded { value in
                if dragging {
                    dragging = false
                    session.endSplitDividerDrag()
                } else if hypot(value.startLocation.x - canvas.width / 2, value.startLocation.y - y) <= band / 2,
                          hypot(value.translation.width, value.translation.height) < tapSlop {
                    swap()
                }
            }
    }

    private func cancel() {
        dragging = false
        session.cancelSplitDividerDrag()
    }

    private func nudge(_ delta: Double) {
        session.beginSplitDividerDrag(sectionID: sectionID, between: upper, and: lower)
        session.updateSplitDividerDrag(deltaFraction: delta)
        session.endSplitDividerDrag()
    }

    private func swap() {
        session.swapSplitPanels(sectionID: sectionID, upper, lower)
        if let current = pick, current.sectionID == sectionID {
            if current.panel == upper {
                pick = NativeSplitPanelPick(sectionID: sectionID, panel: lower, selection: session.selection)
            } else if current.panel == lower {
                pick = NativeSplitPanelPick(sectionID: sectionID, panel: upper, selection: session.selection)
            }
        }
    }
}

/// One ghost: the "+" circle and its words. `.panel` fills the panel's rect with a Sky tint and centres the block;
/// `.pill` is a compact Sky capsule at a fixed height. Only the block takes touches (44pt minimum).
private struct NativeSplitGhost: View {
    enum Style { case panel(CGRect), pill(centerY: CGFloat, width: CGFloat) }

    let panel: Int
    let title: String
    let text: String
    let style: Style
    let action: () -> Void

    var body: some View {
        switch style {
        case .panel(let rect):
            ZStack {
                KriaColor.sky.opacity(0.3).allowsHitTesting(false)
                button(stacked: rect.height >= 112)
            }
            .frame(width: rect.width, height: rect.height)
            .offset(x: rect.minX, y: rect.minY)
        case .pill(let centerY, let width):
            button(stacked: false)
                .background(KriaColor.sky, in: Capsule())
                .position(x: width / 2, y: centerY)
        }
    }

    /// The leaf sits in a container, as the divider's swap button does: the preview's own identifier lands on the
    /// container and leaves the leaf's alone.
    private func button(stacked: Bool) -> some View {
        ZStack { leaf(stacked: stacked) }.accessibilityElement(children: .contain)
    }

    private func leaf(stacked: Bool) -> some View {
        Group {
            if stacked {
                VStack(spacing: 8) { plus(44); label }
            } else {
                HStack(spacing: 8) { plus(style.isPill ? 30 : 36); label }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, stacked ? 8 : 4)
        .frame(minWidth: 44, minHeight: 44)
        .contentShape(Rectangle())
        .onTapGesture(perform: action)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction(.default) { action() }
        .accessibilityIdentifier("native-editor-split-ghost-\(panel)")
    }

    private var accessibilityText: String { "\(text), \(title) panel" }

    private func plus(_ size: CGFloat) -> some View {
        Circle().fill(.white).frame(width: size, height: size)
            .shadow(color: KriaColor.ink.opacity(0.18), radius: 4, y: 2)
            .overlay { Image(systemName: "plus").font(.system(size: size * 0.5, weight: .semibold)).foregroundStyle(KriaColor.ink) }
    }

    private var label: some View {
        Text(text).kriaType(.caption).foregroundStyle(KriaColor.ink)
            .lineLimit(1).minimumScaleFactor(0.85)
    }
}

private extension NativeSplitGhost.Style {
    var isPill: Bool { if case .pill = self { return true } else { return false } }
}
