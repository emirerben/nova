import SwiftUI
import UIKit

/// One picture size for every tile in the format picker, so the Slider tile cannot drift from the rest.
enum FormatTileMetrics {
    static let image = CGSize(width: 156, height: 228)
}

/// A compact, independently pausable preview for the final creation format.
struct SlidePostFormatCover: View {
    /// Bundled JPEG names, in slide order. Add a photo here and in `project.yml`; the counter,
    /// dots and wrap-around all follow `frames.count`.
    static let frames = (1...7).map { "slider-lisbon-taberna-\($0)" }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @Binding var index: Int
    @Binding var paused: Bool
    var active = true
    @State private var mostlyVisible = true
    private let size = FormatTileMetrics.image
    private var count: Int { Self.frames.count }
    private var advances: Bool { !reduceMotion && !paused && active && mostlyVisible && scenePhase == .active }
    private var displayedIndex: Int { index % count }

    var body: some View {
        GeometryReader { proxy in
            // The strip is wider than its viewport, so it is pinned to the leading edge of a
            // viewport-sized frame. Without `alignment: .leading` the frame centres it and every
            // resting position shows two half frames.
            HStack(spacing: 0) {
                ForEach(Array(Self.frames.enumerated()), id: \.offset) { _, name in image(name) }
                // Repeat the first photo so the last swipe is a real last-to-first
                // transition. Index resets to zero only after this frame has held.
                image(Self.frames[0])
            }
            .frame(width: size.width * CGFloat(count + 1), height: size.height, alignment: .leading)
            .offset(x: -CGFloat(index) * size.width)
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.35), value: index)
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .leading)
            .overlay(alignment: .bottomLeading) { counter }
            .clipped()
            .onAppear { visibility(proxy.frame(in: .global)) }
            .onChange(of: proxy.frame(in: .global)) { _, frame in visibility(frame) }
        }
        .allowsHitTesting(false)
        .task(id: advances) {
            guard advances else { return }
            // An app/background pause can cancel the task during the repeated
            // frame's travel. Resume from the real first frame, never an
            // out-of-range index.
            if index == count {
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { index = 0 }
            }
            while !Task.isCancelled {
                // Keep the hold separate so every slide rests after its swipe.
                do { try await Task.sleep(for: .milliseconds(1200)) } catch { return }
                guard advances else { return }
                let wraps = index == count - 1
                withAnimation(.easeInOut(duration: 0.35)) { index += 1 }
                do { try await Task.sleep(for: .milliseconds(350)) } catch { return }
                if wraps {
                    var transaction = Transaction()
                    transaction.disablesAnimations = true
                    withTransaction(transaction) { index = 0 }
                }
            }
        }
        .onChange(of: reduceMotion) { _, enabled in if enabled { index = 0 } }
    }

    private var counter: some View {
        HStack(spacing: 6) {
            Text("\(displayedIndex + 1)/\(count)").font(KriaFont.body(11).weight(.semibold)).monospacedDigit()
            ForEach(0..<count, id: \.self) { item in
                Capsule().fill(item == displayedIndex ? Color.white : Color.white.opacity(0.5)).frame(width: item == displayedIndex ? 12 : 5, height: 5)
            }
        }
        .foregroundStyle(.white).padding(.leading, 9).padding(.bottom, 5)
    }

    private func image(_ name: String) -> some View {
        Group {
            if let url = Bundle.main.url(forResource: name, withExtension: "jpg"), let uiImage = UIImage(contentsOfFile: url.path) { Image(uiImage: uiImage).resizable().scaledToFill() }
            else { KriaColor.softZinc.overlay { Image(systemName: "photo") } }
        }.frame(width: size.width, height: size.height).clipped()
    }

    private func visibility(_ frame: CGRect) {
        let visible = frame.intersection(UIScreen.main.bounds)
        mostlyVisible = visible.width * visible.height / max(1, frame.width * frame.height) >= 0.65
    }
}
