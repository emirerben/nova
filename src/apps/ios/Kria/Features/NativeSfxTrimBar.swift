import SwiftUI

/// Two-handle trim bar over the source sound. `start`/`end` are seconds into the source
/// (0...`source`). Handles keep a 44pt hit target and expose VoiceOver adjust actions.
struct NativeSfxTrimBar: View {
    let source: Double
    let start: Double
    let end: Double
    let bars: [Float]?
    var minimum = 0.05
    let onChange: (_ start: Double, _ end: Double) -> Void
    let onBegin: () -> Void
    let onEnd: () -> Void

    private let handleWidth: CGFloat = 16
    private let barHeight: CGFloat = 52
    @State private var dragOrigin: Double?

    var body: some View {
        GeometryReader { geo in
            let usable = max(1, geo.size.width - handleWidth * 2)
            let x: (Double) -> CGFloat = { handleWidth + CGFloat($0 / max(source, 0.0001)) * usable }
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 10).fill(KriaColor.softZinc)
                Group {
                    if let bars, !bars.isEmpty { NativeSfxWaveformBars(bars: bars) }
                    else { Capsule().fill(KriaColor.line).frame(height: 4) }
                }
                .padding(.horizontal, handleWidth)
                RoundedRectangle(cornerRadius: 8).stroke(KriaColor.sky, lineWidth: 2)
                    .background(KriaColor.sky.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
                    .frame(width: max(0, x(end) - x(start) + handleWidth * 2))
                    .offset(x: x(start) - handleWidth)
                    .allowsHitTesting(false)
                handle(label: "Trim start", value: start, position: x(start) - handleWidth / 2, usable: usable) { origin, delta in
                    onChange(min(max(0, origin + delta), end - minimum), end)
                } adjust: { step in onChange(min(max(0, start + step), end - minimum), end) }
                handle(label: "Trim end", value: end, position: x(end) - handleWidth / 2, usable: usable) { origin, delta in
                    onChange(start, max(start + minimum, min(source, origin + delta)))
                } adjust: { step in onChange(start, max(start + minimum, min(source, end + step))) }
            }
        }
        .frame(height: barHeight)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-sfx-trim-bar")
    }

    private func handle(label: String, value: Double, position: CGFloat, usable: CGFloat,
                        drag: @escaping (_ origin: Double, _ delta: Double) -> Void,
                        adjust: @escaping (Double) -> Void) -> some View {
        RoundedRectangle(cornerRadius: 5).fill(KriaColor.sky)
            .overlay(Capsule().fill(.white).frame(width: 2, height: 18))
            .frame(width: handleWidth, height: barHeight)
            .frame(width: 44, height: barHeight)
            .contentShape(Rectangle())
            .offset(x: position - (44 - handleWidth) / 2)
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { gesture in
                    if dragOrigin == nil { dragOrigin = value; onBegin() }
                    let delta = Double(gesture.translation.width / usable) * source
                    drag(dragOrigin ?? value, delta)
                }
                .onEnded { _ in dragOrigin = nil; onEnd() })
            .accessibilityElement()
            .accessibilityLabel(label)
            .accessibilityValue(String(format: "%.2f seconds", value))
            .accessibilityAdjustableAction { direction in
                onBegin()
                adjust(direction == .increment ? 0.05 : -0.05)
                onEnd()
            }
    }
}
