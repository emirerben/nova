import SwiftUI
import UIKit

/// The panel a tap picked while the session has no selection of its own for it (an empty panel). It is valid only
/// while the session selection is still the one it had right after the tap.
struct NativeSplitPanelPick: Equatable {
    let sectionID: String
    let panel: Int
    let selection: EditorSelection?
}

/// A request to open the Split tool's picker aimed at one panel (a timeline gap tap). `token` makes a repeat
/// request for the same panel a new one.
struct NativeSplitAddRequest: Equatable {
    let sectionID: String
    let panel: Int
    let token = UUID()
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
/// A panel with no footage shows its real look, the section background, as the export does. The picker and the
/// timeline gap are the ways in. The tint and panel elements never take touches. Only the divider bands do, and this
/// view sits above the canvas gesture surface so the canvas move gesture and its empty tap (fullscreen) never see a
/// touch that starts on a band.
struct NativeSplitPreviewOverlay: View {
    @ObservedObject var session: NativeEditorSession
    @Binding var pick: NativeSplitPanelPick?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
                .accessibilityLabel("\(NativeSplitScreenRows.displayTitle(NativeSplitScreenRows.panelTitle(index: index, count: section.panelCount))) panel")
                .accessibilityAddTraits(selected == index ? [.isButton, .isSelected] : .isButton)
                .accessibilityAction { Self.choose(panel: index, context: context, session: session, pick: $pick) }
                .accessibilityIdentifier("native-editor-split-panel-\(index)")
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
        NativeSplitScreenRows.displayTitle(NativeSplitScreenRows.panelTitle(index: upper, count: section.panelCount))
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
