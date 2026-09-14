import SwiftUI
import KriaMediaEngine

struct NativeFootagePanel: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    @State private var rate = 1.0
    @State private var crop = NormalizedSourceRect(x: 0, y: 0, width: 1, height: 1)

    var body: some View {
        Section("Speed") {
            NativeEditorSlider(session: session, value: $rate, in: 0.25...4, step: 0.05) { Text("Playback rate") }
                .onChange(of: rate) { _, value in session.setFootagePlaybackRate(selection, rate: value) }
            Text("Changes footage speed while animation timing stays separate.").font(.footnote).foregroundStyle(.secondary)
        }
        Section("Freeform crop") {
            NativeCropCanvas(crop: $crop, onBegin: { session.beginTransaction() }, onCommit: { value in
                session.setFootageCrop(selection, crop: value)
                session.endTransaction()
            })
            Button("Reset crop") { session.setFootageCrop(selection, crop: nil); crop = .init(x: 0, y: 0, width: 1, height: 1) }
                .disabled(crop == .init(x: 0, y: 0, width: 1, height: 1))
        }
        .onAppear { rate = session.footagePlaybackRate(for: selection); crop = session.footageCrop(for: selection) ?? .init(x: 0, y: 0, width: 1, height: 1) }
    }
}

private struct NativeCropCanvas: View {
    @Binding var crop: NormalizedSourceRect
    let onBegin: () -> Void
    let onCommit: (NormalizedSourceRect) -> Void
    @State private var baseline: NormalizedSourceRect?
    private let minimum = 0.08

    var body: some View {
        GeometryReader { proxy in
            let rect = CGRect(x: crop.x * proxy.size.width, y: crop.y * proxy.size.height,
                              width: crop.width * proxy.size.width, height: crop.height * proxy.size.height)
            ZStack(alignment: .topLeading) {
                Color.black.opacity(0.18)
                Rectangle().path(in: rect).stroke(.white, lineWidth: 2)
                cropHandle(.topLeading, at: rect.origin, size: proxy.size)
                cropHandle(.topTrailing, at: CGPoint(x: rect.maxX, y: rect.minY), size: proxy.size)
                cropHandle(.bottomLeading, at: CGPoint(x: rect.minX, y: rect.maxY), size: proxy.size)
                cropHandle(.bottomTrailing, at: CGPoint(x: rect.maxX, y: rect.maxY), size: proxy.size)
            }
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .gesture(moveGesture(size: proxy.size))
        }
        .aspectRatio(9 / 16, contentMode: .fit)
        .frame(maxWidth: 220)
        .accessibilityIdentifier("native-editor-freeform-crop")
    }

    private enum Corner { case topLeading, topTrailing, bottomLeading, bottomTrailing }
    private func cropHandle(_ corner: Corner, at point: CGPoint, size: CGSize) -> some View {
        Circle().fill(.white).frame(width: 18, height: 18).position(point)
            .gesture(handleGesture(corner, size: size))
    }
    private func moveGesture(size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 3).onChanged { value in
            if baseline == nil { baseline = crop; onBegin() }
            guard let base = baseline else { return }
            crop = normalized(base, dx: value.translation.width / size.width, dy: value.translation.height / size.height)
        }.onEnded { _ in finish() }
    }
    private func handleGesture(_ corner: Corner, size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 0).onChanged { value in
            if baseline == nil { baseline = crop; onBegin() }
            guard let base = baseline else { return }
            let dx = value.translation.width / size.width, dy = value.translation.height / size.height
            crop = resized(base, corner: corner, dx: dx, dy: dy)
        }.onEnded { _ in finish() }
    }
    private func finish() { let value = crop; baseline = nil; onCommit(value) }
    private func normalized(_ value: NormalizedSourceRect, dx: Double, dy: Double) -> NormalizedSourceRect {
        let x = min(max(0, value.x + dx), 1 - value.width), y = min(max(0, value.y + dy), 1 - value.height)
        return .init(x: x, y: y, width: value.width, height: value.height)
    }
    private func resized(_ value: NormalizedSourceRect, corner: Corner, dx: Double, dy: Double) -> NormalizedSourceRect {
        var left = value.x, top = value.y, right = value.x + value.width, bottom = value.y + value.height
        switch corner { case .topLeading: left += dx; top += dy; case .topTrailing: right += dx; top += dy; case .bottomLeading: left += dx; bottom += dy; case .bottomTrailing: right += dx; bottom += dy }
        left = min(max(0, left), right - minimum); top = min(max(0, top), bottom - minimum)
        right = max(min(1, right), left + minimum); bottom = max(min(1, bottom), top + minimum)
        return .init(x: left, y: top, width: right - left, height: bottom - top)
    }
}
